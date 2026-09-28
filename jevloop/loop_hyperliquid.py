"""The Hyperliquid perp block loop -- dry execution only.

Same nine-stage shape as loop.py (see that file's docstring), reusing every
venue-agnostic piece of it unchanged: battery.py, policy.py, pricing.py,
ladder.py, strategy.py, and most of state.py. The two things that differ
are read-side market data (execution/hyperliquid.py's public /info + WS
instead of Alpaca's REST) and order placement, which in this module does
not exist: there is no signed /exchange call anywhere in this file, and no
wallet, agent-wallet address, or private key is ever read from the
environment. That is not a flag you can flip here -- see
execution/hyperliquid.py's LiveTradingNotImplemented, which every write
path in HyperliquidPerpClient raises unconditionally.

Instead, this loop simulates its own fills against the real order book and
the real trade tape:

  - a resting simulated quote is "filled" when a real trade prints at or
    through its price (a real sell at or below our bid crosses our bid; a
    real buy at or above our ask crosses our ask) -- see
    `_crossing_fills()`, a pure function so it is unit-testable without a
    network or a running loop
  - a directional leg (a simulated market order) fills immediately at the
    current best bid/ask, the same instant a real taker order would clear
    the top of the book for a similarly small size
  - every simulated fill goes through state.apply_fill(), the exact same
    function the Alpaca loop uses for real fills, so inventory, average
    entry price, and realised PnL are computed identically either way

This is real market data and a real Jev battery call, run through the same
code-owned policy, pricing, and risk engine as the Alpaca loop. Only the
"order" at the end is fake, and it is never labelled as anything else: every
log line's fill text starts with "dry:".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .assets import AssetSpec, UnknownSymbolError, resolve_perp_symbol, size_order
from .battery import run_battery
from .client import DecisionClientError, GatewayVerificationRequired, resolve_decision_client
from .execution.hyperliquid import HyperliquidAPIError, HyperliquidTradeTape, client_from_env
from .ladder import Rung, select_rung
from .limits import Limits, default_perp_limits
from .policy import (
    KILL,
    PULL_QUOTES,
    QUOTE_BOTH_SIDES,
    QUOTE_WIDE,
    STAND_DOWN,
    WIDEN,
    compose_action,
    fallback_action,
)
from .pricing import quote_prices
from .risk import check as risk_check
from .strategy import THRESHOLDS
from .state import (
    InventoryState,
    apply_fill,
    build_snapshot,
    compact_snapshot,
    record_fill_slippage,
    record_tick_outcome,
    update_vwap,
)

LOG_DIR = Path(os.environ.get("JEV_LOOP_HOME", str(Path.home() / ".jev-loop")))
LOG_FILE = LOG_DIR / "hyperliquid-log.jsonl"
LATEST_FILE = LOG_DIR / "hyperliquid-latest.json"
# Separate files from loop.py's log.jsonl/latest.json on purpose: Marteau's
# Hyperliquid loop and BOMBO's Alpaca loop are two different bots and should
# never overwrite each other's dashboard feed. dashboard/*.html is not wired
# to read these yet -- see README.md's "not yet implemented" list.

HISTORY_S = 2400.0  # same 40-minute price history window as loop.py
TRADE_TAPE_S = 60.0


def _crossing_fills(
    resting: dict, new_trades: list[tuple[float, float, float, str]]
) -> tuple[list[tuple[str, float, float, float]], dict]:
    """Pure simulation core, no I/O, so it is directly unit-testable.

    `resting` is {"bid": {"price": p, "qty": q}, "ask": {...}}, either key
    optionally absent. `new_trades` is real trades since the last check,
    oldest first, each (ts, price, size, side) with side "buy" | "sell" from
    the taker's perspective (matches HyperliquidTradeTape's shape).

    A real "sell" print at or below our resting bid price means an
    aggressive seller crossed the book down through at least our bid level,
    so our bid would have been filled up to the traded size. Symmetric for
    a "buy" print at or above our resting ask.

    Returns (fills, remaining_resting) where fills is a list of
    (side, qty, price, ts) -- side is "buy" for a filled bid, "sell" for a
    filled ask, i.e. the side *we* transacted, matching state.apply_fill's
    convention -- and remaining_resting drops any side that was fully
    consumed (partially filled sides keep their reduced qty resting).
    """
    resting = {k: dict(v) for k, v in resting.items()}
    fills: list[tuple[str, float, float, float]] = []

    for ts, price, size, side in new_trades:
        remaining_size = size
        bid = resting.get("bid")
        if side == "sell" and bid and price <= bid["price"] and remaining_size > 0:
            qty = min(bid["qty"], remaining_size)
            if qty > 0:
                fills.append(("buy", qty, bid["price"], ts))
                bid["qty"] -= qty
                remaining_size -= qty
                if bid["qty"] <= 1e-12:
                    resting.pop("bid", None)
                else:
                    resting["bid"] = bid

        ask = resting.get("ask")
        if side == "buy" and ask and price >= ask["price"] and remaining_size > 0:
            qty = min(ask["qty"], remaining_size)
            if qty > 0:
                fills.append(("sell", qty, ask["price"], ts))
                ask["qty"] -= qty
                if ask["qty"] <= 1e-12:
                    resting.pop("ask", None)
                else:
                    resting["ask"] = ask

    return fills, resting


def _fmt_money(x: float) -> str:
    return f"{x:,.1f}"


class _StopRequested(Exception):
    pass


def run(
    coin_symbol: str,
    ticks: int | None,
    mock: bool,
    limits: Limits,
) -> int:
    """Always dry execution. There is no --live, no --dry-execution flag to
    turn off, and no credential of any kind read from the environment: this
    is the only mode this loop has in this phase."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    try:
        spec = resolve_perp_symbol(coin_symbol)
    except UnknownSymbolError as exc:
        print(f"cannot start: {exc}")
        return 1

    hl = client_from_env(coin=spec.coin)
    client = resolve_decision_client(mock=mock)
    tape = HyperliquidTradeTape(spec.coin)
    tape.start()

    print(
        f"asset: {spec.symbol} (perp, hyperliquid, 24/7, leverage {spec.leverage:g}x, "
        f"min order ${spec.min_notional_usd:.2f})"
    )
    print(
        "DRY EXECUTION ONLY: real market data and a real Jev battery, but every "
        "order is simulated locally -- this build never calls Hyperliquid's signed "
        "/exchange endpoint, and there is no wallet key anywhere in this process."
    )
    if ticks is None:
        print("running continuously until stopped (Ctrl+C).")

    start_equity = limits.max_position_usd * 2  # no real account to read; an honest placeholder
    print(f"simulated starting equity: ${start_equity:.2f} (no real account -- see README.md)")
    inv = InventoryState(equity_usd=start_equity, high_water_mark_usd=start_equity)

    price_hist: list[tuple[float, float]] = []
    trade_tape: list[tuple[float, float, float, str]] = []
    cancel_events: list[float] = []
    resting: dict = {}
    rest_counter = 0
    last_trade_check = time.time() - TRADE_TAPE_S
    recent_ticks: list[dict] = []
    started_at = time.time()
    block = 0
    n = 0

    try:
        while ticks is None or n < ticks:
            tick_start = time.monotonic()
            block += 1
            now = time.time()

            try:
                book = hl.get_orderbook()
                bids, asks = book.get("b", []), book.get("a", [])
                mark_px, oracle_px, funding_rate = hl.get_mark_oracle_funding()
            except HyperliquidAPIError as exc:
                print(f"tick {block} | hyperliquid error: {exc}")
                _sleep_remaining(tick_start, limits.tick_seconds)
                n += 1
                continue

            new_trades = tape.get_recent_trades(last_trade_check)
            last_trade_check = now
            trade_tape.extend(new_trades)
            trade_tape = [t for t in trade_tape if t[0] >= now - TRADE_TAPE_S]
            update_vwap(inv, [(ts, p, sz) for ts, p, sz, _ in new_trades])

            mid = (
                (bids[0][0] + asks[0][0]) / 2
                if bids and asks
                else (mark_px or (trade_tape[-1][1] if trade_tape else 0.0))
            )
            spread_bps = (
                (asks[0][0] - bids[0][0]) / mid * 10_000 if (mid and bids and asks) else 0.0
            )
            if mid > 0:
                price_hist.append((now, mid))
            price_hist = [x for x in price_hist if x[0] >= now - HISTORY_S]

            # simulated fills: real trades crossing our resting simulated quotes
            fills, resting = _crossing_fills(resting, new_trades)
            fill_txt = "-"
            if fills:
                parts = []
                for side, qty, price, ts in fills:
                    apply_fill(inv, side, qty, price, ts)
                    record_fill_slippage(inv, expected_price=price, fill_price=price, side=side)
                    parts.append(f"dry fill {side} {qty:g} @ {price:,.2f}")
                fill_txt = "; ".join(parts)

            snapshot = build_snapshot(
                as_of=now,
                mid=mid,
                microprice=mid,
                spread_bps=spread_bps,
                bid_depth=bids,
                ask_depth=asks,
                trade_prices=price_hist,
                trade_sides=[(ts, side) for ts, _, _, side in trade_tape],
                inv=inv,
                data_timestamp=now,
                has_depth=True,
                leverage=spec.leverage,
                funding_rate=funding_rate,
                oracle_px=oracle_px,
                cancel_events=cancel_events,
            )
            equity_now = inv.equity_usd + inv.realised_pnl_usd + snapshot["unrealised_pnl_usd"]
            inv.high_water_mark_usd = max(inv.high_water_mark_usd, equity_now)
            record_tick_outcome(inv, equity_now)

            budget = max(0.05, limits.tick_seconds - (time.monotonic() - tick_start) - 0.15)
            answers, meta = None, {"model": None, "latency_ms": None, "route": None}
            jev_down = False
            decision_late = False
            battery_state = snapshot
            try:
                answers, meta = run_battery(client, battery_state, timeout=budget)
            except GatewayVerificationRequired as exc:
                print(f"tick {block} | gateway needs a card on file: {exc}, falling back to mock.")
                client = resolve_decision_client(mock=True)
                jev_down = True
            except DecisionClientError as exc:
                if "deadline" in str(exc):
                    decision_late = True
                else:
                    jev_down = True
                    print(f"tick {block} | decision client error: {exc}")

            if decision_late:
                action = None
            elif jev_down or answers is None:
                action = fallback_action(snapshot, limits)
            else:
                action = compose_action(answers, snapshot, limits)

            sigma = snapshot["realised_vol_short"] or 0.001
            spread_multiplier = (
                THRESHOLDS.both_sides_spread_multiplier
                if action and action.kind == QUOTE_BOTH_SIDES
                else 1.0
            )
            bid_px, ask_px = quote_prices(
                mid=mid,
                inventory=snapshot["inventory"],
                sigma=sigma,
                gamma=limits.as_gamma,
                kappa=limits.as_kappa,
                time_left_s=limits.as_horizon_s,
                spread_multiplier=spread_multiplier,
            )

            decision_conf = (
                answers.get("quote_environment", {}).get("confidence")
                if action is not None and answers is not None
                else None
            )
            execution_health_score = answers["execution_health"]["score"] if answers else None
            risk_kill_pre = snapshot["drawdown_pct"] > limits.max_drawdown_pct
            rung = select_rung(
                risk_kill=risk_kill_pre,
                decision_late=decision_late,
                jev_down=jev_down and not decision_late,
                decision_confidence=decision_conf,
                low_confidence_threshold=limits.low_confidence_threshold,
                execution_health_score=execution_health_score,
            )

            size_factor = limits.reduce_size_factor if rung == Rung.REDUCE else 1.0
            quote_notional = limits.quote_notional_usd * size_factor
            directional_notional = limits.directional_notional_usd * size_factor

            line_action = "HOLD (late)"
            kill = False
            if rung == Rung.HOLD_LATE or action is None:
                pass
            elif rung == Rung.KILL or (action and action.kind == KILL):
                line_action = "KILL (flatten)"
                kill = True
            else:
                line_action, extra_fill, resting, rest_counter, cancel_events = _simulate_action(
                    action=action,
                    bid_px=bid_px,
                    ask_px=ask_px,
                    spec=spec,
                    quote_notional=quote_notional,
                    directional_notional=directional_notional,
                    snapshot=snapshot,
                    limits=limits,
                    inv=inv,
                    resting=resting,
                    rest_counter=rest_counter,
                    cancel_events=cancel_events,
                    now=now,
                )
                if extra_fill != "-":
                    fill_txt = extra_fill if fill_txt == "-" else f"{fill_txt}; {extra_fill}"
                if line_action.startswith("KILL"):
                    # A hard risk limit tripped inside _simulate_action's own
                    # risk_check() call (e.g. max_position_usd), not caught by
                    # the rung/action.kind check above. Same pattern loop.py
                    # uses for the Alpaca path -- missing this meant a real
                    # risk breach printed "KILL (...)" every tick forever
                    # without ever actually flattening or stopping.
                    kill = True

            if kill:
                rung = Rung.KILL
                if inv.inventory != 0 and mid > 0:
                    side = "sell" if inv.inventory > 0 else "buy"
                    qty = abs(inv.inventory)
                    apply_fill(inv, side, qty, mid, now)
                    fill_txt = f"dry: KILL flatten {side} {qty:g} @ {mid:,.2f}"
                resting = {}

            record = {
                "tick": block,
                "ts": now,
                "symbol": spec.symbol,
                "mid": mid,
                "spread_bps": round(spread_bps, 2),
                "mark_px": mark_px,
                "oracle_px": oracle_px,
                "funding_rate": funding_rate,
                "basis_bps": snapshot["basis_bps"],
                "regime": answers["regime"]["choice"] if answers else None,
                "regime_conf": answers["regime"]["confidence"] if answers else None,
                "direction": answers["direction"]["choice"] if answers else None,
                "direction_conf": answers["direction"]["confidence"] if answers else None,
                "toxic_flow": answers["toxic_flow"]["noul"] if answers else None,
                "liquidity_stressed": answers["liquidity_stressed"]["noul"] if answers else None,
                "quote_environment": answers["quote_environment"]["score"] if answers else None,
                "inventory_pressure": answers["inventory_pressure"]["score"] if answers else None,
                "execution_health": answers["execution_health"]["score"] if answers else None,
                "action": action.kind if action else "HOLD_LATE",
                "action_reason": action.reason if action else "block deadline exceeded",
                "rung": rung.value,
                "latency_ms": meta.get("latency_ms"),
                "model": meta.get("model"),
                "route": meta.get("route"),
                "inventory": inv.inventory,
                "unrealised_pnl_usd": snapshot["unrealised_pnl_usd"],
                "drawdown_pct": snapshot["drawdown_pct"],
                "fill": fill_txt,
            }
            _append_log(record)
            recent_ticks.append(record)
            if len(recent_ticks) > 120:
                recent_ticks = recent_ticks[-120:]
            _write_latest(spec.symbol, block, recent_ticks, started_at)

            regime_txt = (
                f"{answers['regime']['choice']} {answers['regime']['confidence']*100:.0f}%"
                if answers
                else "n/a"
            )
            tox_txt = f"{answers['toxic_flow']['noul']:.2f}" if answers else "n/a"
            env_txt = f"{answers['quote_environment']['score']:.1f}" if answers else "n/a"
            xh_txt = f"{answers['execution_health']['score']:.1f}" if answers else "n/a"
            print(
                f"tick {block} | mid {_fmt_money(mid)} | funding {funding_rate:+.6f} | "
                f"basis {snapshot['basis_bps']} bps | regime {regime_txt} | "
                f"tox {tox_txt} | env {env_txt} | xh {xh_txt} | "
                f"{line_action} | {fill_txt}"
            )

            if rung == Rung.KILL:
                print(f"tick {block} | KILL: hard limit breached, {fill_txt}, stopping.")
                break

            n += 1
            _sleep_remaining(tick_start, limits.tick_seconds)

        return 0
    except (KeyboardInterrupt, _StopRequested):
        print(f"\ntick {block} | stopping: interrupt received. No real orders were ever placed.")
        return 0
    finally:
        tape.stop()


def _simulate_action(
    *,
    action,
    bid_px,
    ask_px,
    spec: AssetSpec,
    quote_notional,
    directional_notional,
    snapshot,
    limits,
    inv,
    resting: dict,
    rest_counter: int,
    cancel_events: list[float],
    now: float,
) -> tuple[str, str, dict, int, list[float]]:
    """The simulated equivalent of loop.py's _execute_action(): runs the same
    risk.check() as the Alpaca loop, then either simulates placing resting
    quotes / a directional leg, or does nothing, all purely in memory. Never
    touches a network write. Returns (line_action, fill_text, resting,
    rest_counter, cancel_events)."""
    order_notional_usd = max(quote_notional, directional_notional)
    verdict = risk_check(snapshot, order_notional_usd, limits, api_error_streak=0, decision_latency_ms=None)
    if not verdict.ok:
        kind = "KILL" if verdict.kill else "VETOED"
        return f"{kind} ({verdict.veto})", "-", resting, rest_counter, cancel_events

    fill_txt = "-"
    line_action = action.kind

    if action.kind in (PULL_QUOTES, STAND_DOWN):
        if resting:
            cancel_events.append(now)
        return line_action, fill_txt, {}, 0, cancel_events

    if action.kind == WIDEN:
        return f"{line_action} bid {bid_px*0.999:,.1f}/ask {ask_px*1.001:,.1f}", fill_txt, resting, rest_counter, cancel_events

    if action.kind in (QUOTE_BOTH_SIDES, QUOTE_WIDE):
        buy_qty = size_order(quote_notional, bid_px, spec)
        sell_qty = size_order(quote_notional, ask_px, spec)
        rest_counter += 1
        if not resting or rest_counter >= limits.rest_ticks:
            if resting:
                cancel_events.append(now)
            resting = {
                "bid": {"price": bid_px, "qty": buy_qty},
                "ask": {"price": ask_px, "qty": sell_qty},
            }
            rest_counter = 0
            fill_txt = f"dry: would quote {buy_qty:g}/{sell_qty:g} @ {bid_px:,.2f}/{ask_px:,.2f}"

        if action.direction_leg in ("up", "down"):
            side = "buy" if action.direction_leg == "up" else "sell"
            fill_px = ask_px if side == "buy" else bid_px
            leg_qty = size_order(directional_notional, fill_px, spec)
            apply_fill(inv, side, leg_qty, fill_px, now)
            record_fill_slippage(inv, expected_price=fill_px, fill_price=fill_px, side=side)
            leg_txt = f"dry: directional {side} {leg_qty:g} @ {fill_px:,.2f}"
            fill_txt = leg_txt if fill_txt == "-" else f"{fill_txt}; {leg_txt}"
            line_action = f"{action.kind} skew {action.skew:+.1f} + {side} leg"
        else:
            line_action = f"{action.kind} skew {action.skew:+.1f}"

        return line_action, fill_txt, resting, rest_counter, cancel_events

    return line_action, fill_txt, resting, rest_counter, cancel_events


def _sleep_remaining(tick_start: float, tick_seconds: float) -> None:
    elapsed = time.monotonic() - tick_start
    remaining = tick_seconds - elapsed
    if remaining > 0:
        time.sleep(remaining)


def _append_log(record: dict) -> None:
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _write_latest(symbol: str, block: int, ticks: list[dict], started_at: float) -> None:
    payload = {
        "generated_at": time.time(),
        "symbol": symbol,
        "block": block,
        "ticks": ticks,
        "stats": {"uptime_s": time.time() - started_at, "venue": "hyperliquid_perp (dry)"},
    }
    tmp = LATEST_FILE.with_name(f"{LATEST_FILE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload))
    try:
        os.replace(tmp, LATEST_FILE)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-loop run-hyperliquid")
    parser.add_argument("--mock", action="store_true", help="force the mock decision client")
    parser.add_argument("--ticks", type=int, default=None, help="stop after N ticks; 0 = forever")
    parser.add_argument("--forever", action="store_true", help="run continuously until stopped")
    parser.add_argument(
        "--symbol",
        default=os.environ.get("DEFAULT_HYPERLIQUID_SYMBOL", "BTC-PERP"),
        help="Hyperliquid perp symbol, e.g. BTC-PERP or ETH-PERP",
    )
    args = parser.parse_args(argv)
    ticks = None if (args.forever or args.ticks == 0) else args.ticks
    limits = default_perp_limits()
    return run(coin_symbol=args.symbol, ticks=ticks, mock=args.mock, limits=limits)


if __name__ == "__main__":
    sys.exit(main())
