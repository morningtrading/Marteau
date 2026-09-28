"""Hyperliquid perpetuals market data and dry-execution support.

Read-side only, and deliberately so. Everything in this file talks to the
public, unauthenticated Hyperliquid endpoints:

  - REST  POST https://api.hyperliquid.xyz/info   (l2Book, metaAndAssetCtxs,
          clearinghouseState, candleSnapshot, allMids -- no signature, no
          API key, just a JSON body naming what you want)
  - WS    wss://api.hyperliquid.xyz/ws            (l2Book, trades, allMids,
          candle channels -- subscribe by coin, no auth)

There is no wallet key, agent-wallet address, or EIP-712 signing anywhere in
this file, on purpose. Real order placement on Hyperliquid goes through the
signed POST /exchange endpoint, which needs a wallet signature (typically an
"agent wallet" authorized by the real account, not a simple API key). That
endpoint is never called here. HyperliquidPerpClient.submit_limit_order and
submit_market_order both raise NotImplementedError unconditionally, so there
is no code path in Marteau, at this stage, that can place a live order --
not behind a flag, not behind an env var, nothing to accidentally flip on.
Live trading is an explicit future phase with its own three-gate-style
safety design, mirroring execution/alpaca.py's live-trading refusal, plus a
liquidation-risk check that does not exist yet.

Mirrors the shape of execution/alpaca.py's AlpacaPaperClient (get_orderbook,
get_recent_trades, get_position_qty, get_account, is_market_open) so the
rest of the loop can read either venue through a similar interface. Where
Hyperliquid has no equivalent of a field (e.g. Alpaca's market-hours clock;
perps trade 24/7), the method returns the obviously-always-true answer
instead of pretending the concept exists.
"""

from __future__ import annotations

import collections
import threading
import time
from dataclasses import dataclass, field

try:
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "The 'requests' package is required. Run: uv pip install requests"
    ) from exc

INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"


class HyperliquidConfigError(Exception):
    pass


class HyperliquidAPIError(Exception):
    def __init__(self, status_code: int, body: str):
        super().__init__(f"HTTP {status_code}: {body[:300]}")
        self.status_code = status_code
        self.body = body


class LiveTradingNotImplemented(Exception):
    """Raised unconditionally by any method that would place, cancel, or
    modify a real order. Hyperliquid live trading (the signed /exchange
    endpoint, wallet-signature/EIP-712 auth via an agent wallet) is an
    explicit, separate phase the user will request later. Nothing in this
    file, or in loop_hyperliquid.py, can reach it: there is no credential
    loading path for a wallet key at all in this pass."""


class RateLimiter:
    """Same token-bucket-by-timestamp limiter as execution/alpaca.py: keeps
    calls under N per rolling 60s window."""

    def __init__(self, calls_per_minute: int):
        self.calls_per_minute = calls_per_minute
        self._timestamps: collections.deque[float] = collections.deque()

    def wait(self) -> None:
        now = time.monotonic()
        while self._timestamps and now - self._timestamps[0] > 60.0:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.calls_per_minute:
            sleep_for = 60.0 - (now - self._timestamps[0]) + 0.01
            if sleep_for > 0:
                time.sleep(sleep_for)
        self._timestamps.append(time.monotonic())


class HyperliquidInfoClient:
    """Thin wrapper over POST /info. Every method here is a public read: no
    header, no key, no signature. clearinghouseState takes a wallet
    *address* (not a credential) because Hyperliquid's info endpoint will
    report any address's positions and margin to anyone who asks -- that is
    how the venue's public API works, not a Marteau-specific choice. Marteau
    itself never has an address configured in this phase, so that method is
    provided for completeness but is not called anywhere in the dry loop.
    """

    def __init__(self, base_url: str = INFO_URL, calls_per_minute: int = 100):
        self.base_url = base_url
        self._limiter = RateLimiter(calls_per_minute)

    def _post(self, body: dict) -> dict | list:
        self._limiter.wait()
        resp = requests.post(self.base_url, json=body, timeout=10)
        if resp.status_code >= 400:
            raise HyperliquidAPIError(resp.status_code, resp.text)
        return resp.json()

    def l2_book(self, coin: str) -> dict:
        """Real L2 depth. Returns the raw {"coin", "time", "levels": [bids, asks]}
        shape; each level is {"px": "...", "sz": "...", "n": int}."""
        return self._post({"type": "l2Book", "coin": coin})

    def meta_and_asset_ctxs(self) -> tuple[list[dict], list[dict]]:
        """Returns (universe, asset_ctxs): universe[i] describes asset i
        (name, szDecimals, maxLeverage, ...), asset_ctxs[i] carries its live
        numbers (markPx, oraclePx, funding, openInterest, dayNtlVlm). The two
        lists are parallel and index-aligned, per Hyperliquid's own schema."""
        data = self._post({"type": "metaAndAssetCtxs"})
        meta, asset_ctxs = data[0], data[1]
        return meta.get("universe", []), asset_ctxs

    def asset_ctx_for(self, coin: str) -> dict | None:
        """Convenience: find (universe entry, asset ctx) for one coin merged
        into a single dict, or None if the coin is not in the universe right
        now. Used to pull markPx/oraclePx/funding and to refine tick size /
        quantity precision at startup instead of trusting the hardcoded
        defaults in assets.py forever."""
        universe, asset_ctxs = self.meta_and_asset_ctxs()
        for u, ctx in zip(universe, asset_ctxs):
            if u.get("name") == coin:
                merged = dict(u)
                merged.update(ctx)
                return merged
        return None

    def all_mids(self) -> dict:
        """coin -> mid price string, for every actively-traded coin."""
        return self._post({"type": "allMids"})

    def candle_snapshot(
        self, coin: str, interval: str, start_ms: int, end_ms: int
    ) -> list[dict]:
        """Historical candles. interval like '1m', '5m', '1h'. Timestamps in
        epoch milliseconds, matching Hyperliquid's own convention (Alpaca's
        wrapper uses ISO-8601; this venue does not, so this file does not
        pretend otherwise)."""
        data = self._post(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": interval,
                    "startTime": start_ms,
                    "endTime": end_ms,
                },
            }
        )
        return data or []

    def clearinghouse_state(self, address: str) -> dict:
        """Positions, margin summary, and account value for any address.
        Public read: no signature required, just the address. Not called
        anywhere in the dry-execution loop, which has no address configured
        and tracks its simulated position locally instead (state.py's
        InventoryState, same as the Alpaca path)."""
        return self._post({"type": "clearinghouseState", "user": address})


@dataclass
class _WSChannelBuffer:
    coin: str
    channel: str
    maxlen: int = 5000
    items: collections.deque = field(default_factory=lambda: collections.deque(maxlen=5000))
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        # deque maxlen set again here so a caller-supplied maxlen sticks
        self.items = collections.deque(maxlen=self.maxlen)


class HyperliquidTradeTape:
    """Background WS subscriber for the public `trades` channel on one coin.

    Buffers (ts, price, size, side) tuples in a bounded deque, so the rest of
    the loop can ask "trades since T" the same way execution/alpaca.py's
    get_recent_trades(start_iso) does, just with epoch floats instead of
    ISO-8601 strings (Hyperliquid's own wire format is epoch milliseconds).

    Needs the optional `websocket-client` package. If it is not installed,
    or the connection cannot be made, this degrades honestly: start()
    prints a note and get_recent_trades() returns an empty list forever
    rather than raising or fabricating trades -- the same "honest
    degradation" the rest of this codebase uses for missing L2 depth on
    equities.
    """

    def __init__(self, coin: str, buffer_size: int = 5000):
        self.coin = coin
        self._buf = _WSChannelBuffer(coin=coin, channel="trades", maxlen=buffer_size)
        self._ws = None
        self._thread: threading.Thread | None = None
        self._connected = False
        self._stop = threading.Event()

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        try:
            import websocket  # websocket-client
        except ImportError:
            print(
                "hyperliquid trade tape: 'websocket-client' is not installed "
                "(uv pip install websocket-client). Recent-trade flow fields "
                "will read empty/None instead of a fabricated tape."
            )
            return

        def _on_message(ws, message):
            import json

            try:
                data = json.loads(message)
            except ValueError:
                return
            if data.get("channel") != "trades":
                return
            for t in data.get("data", []) or []:
                if t.get("coin") != self.coin:
                    continue
                try:
                    ts = float(t["time"]) / 1000.0
                    price = float(t["px"])
                    size = float(t["sz"])
                    side = "buy" if t.get("side") == "B" else "sell"
                except (KeyError, ValueError, TypeError):
                    continue
                with self._buf.lock:
                    self._buf.items.append((ts, price, size, side))

        def _on_open(ws):
            import json

            ws.send(
                json.dumps(
                    {
                        "method": "subscribe",
                        "subscription": {"type": "trades", "coin": self.coin},
                    }
                )
            )
            self._connected = True

        def _on_close(ws, *_args):
            self._connected = False

        def _on_error(ws, error):
            self._connected = False

        def _run():
            while not self._stop.is_set():
                try:
                    self._ws = websocket.WebSocketApp(
                        WS_URL,
                        on_open=_on_open,
                        on_message=_on_message,
                        on_close=_on_close,
                        on_error=_on_error,
                    )
                    self._ws.run_forever(ping_interval=30, ping_timeout=10)
                except Exception:
                    self._connected = False
                if self._stop.is_set():
                    break
                time.sleep(2.0)  # reconnect backoff

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

    def get_recent_trades(self, since_ts: float) -> list[tuple[float, float, float, str]]:
        """Trades strictly after `since_ts`, oldest first. Empty (never
        fabricated) if the tape never connected."""
        with self._buf.lock:
            items = list(self._buf.items)
        return sorted((t for t in items if t[0] > since_ts), key=lambda t: t[0])


class HyperliquidPerpClient:
    """Read-side market data client for one Hyperliquid perp, shaped after
    AlpacaPaperClient's interface. No wallet, no API key, no signature.

    submit_limit_order / submit_market_order / cancel_own_orders all raise
    LiveTradingNotImplemented unconditionally -- see the module docstring.
    Order placement in this phase happens entirely inside loop_hyperliquid.py
    as a local simulation against real market data, never through this
    client.
    """

    def __init__(self, coin: str, calls_per_minute: int = 100):
        self.coin = coin
        self.info = HyperliquidInfoClient(calls_per_minute=calls_per_minute)

    # -- market data -----------------------------------------------
    def get_orderbook(self) -> dict:
        """Shaped like Alpaca's crypto orderbook ({"b": [...], "a": [...]},
        each level a (price, size) pair) so state-building code can treat
        both venues the same way."""
        book = self.info.l2_book(self.coin)
        levels = book.get("levels") or [[], []]
        bids_raw, asks_raw = (levels + [[], []])[:2]
        return {
            "b": [(float(l["px"]), float(l["sz"])) for l in bids_raw],
            "a": [(float(l["px"]), float(l["sz"])) for l in asks_raw],
        }

    def get_mark_oracle_funding(self) -> tuple[float, float, float]:
        """(mark_px, oracle_px, funding_rate). funding_rate is Hyperliquid's
        current hourly funding rate for this coin, as a fraction (e.g.
        0.0001 = 0.01% per hour), straight from metaAndAssetCtxs -- no
        annualising or rescaling done here."""
        ctx = self.info.asset_ctx_for(self.coin)
        if ctx is None:
            raise HyperliquidAPIError(404, f"{self.coin} not found in universe")
        mark_px = float(ctx.get("markPx", 0.0) or 0.0)
        oracle_px = float(ctx.get("oraclePx", 0.0) or 0.0)
        funding = float(ctx.get("funding", 0.0) or 0.0)
        return mark_px, oracle_px, funding

    def get_position_qty(self, address: str | None = None) -> float:
        """Real signed position size for `address`, or 0.0 if no address is
        configured. Marteau's dry-execution loop has no address in this
        phase and tracks its simulated inventory locally instead (see
        state.InventoryState) -- this method exists for interface parity and
        for a future phase, not called from loop_hyperliquid.py today."""
        if not address:
            return 0.0
        state = self.info.clearinghouse_state(address)
        for p in state.get("assetPositions", []) or []:
            pos = p.get("position", {})
            if pos.get("coin") == self.coin:
                return float(pos.get("szi", 0.0) or 0.0)
        return 0.0

    def get_account(self, address: str | None = None) -> dict:
        """Real account value/margin for `address`, or an empty dict if no
        address is configured (dry-execution has none)."""
        if not address:
            return {}
        return self.info.clearinghouse_state(address)

    def is_market_open(self) -> bool:
        """Perpetuals trade 24/7. Always True, unlike Alpaca equities."""
        return True

    # -- order placement: deliberately not implemented -----------------
    def submit_limit_order(self, side: str, qty: float, limit_price: float, tif: str = "gtc"):
        raise LiveTradingNotImplemented(
            "Hyperliquid live order submission is not implemented in this phase. "
            "This is intentional: Marteau's Hyperliquid loop is dry-execution "
            "only. Real submission needs the signed /exchange endpoint, an "
            "agent-wallet EIP-712 signature, and its own three-gate-style "
            "safety design (see execution/alpaca.py's live-trading refusal), "
            "none of which exist yet."
        )

    def submit_market_order(self, side: str, qty: float):
        raise LiveTradingNotImplemented(
            "Hyperliquid live order submission is not implemented in this phase. "
            "See submit_limit_order's message."
        )

    def cancel_own_orders(self):
        raise LiveTradingNotImplemented(
            "Hyperliquid live order cancellation is not implemented in this phase. "
            "There are never any real resting orders to cancel: dry-execution "
            "only simulates them locally."
        )


def client_from_env(coin: str = "BTC") -> HyperliquidPerpClient:
    """Builds the read-only market-data client. Reads no environment
    variable at all: there is no credential of any kind for Hyperliquid in
    this phase, so there is nothing to resolve from os.environ. Kept as a
    function (rather than just calling the constructor everywhere) so a
    later phase can add config resolution here without changing every call
    site -- the same shape as execution/alpaca.py's client_from_env, minus
    the credentials."""
    return HyperliquidPerpClient(coin=coin)
