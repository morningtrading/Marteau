# Marteau

A fork of [BOMBO](https://github.com/morningtrading/BOMBO)'s `jev-loop`: a
24/7 trading loop built around a Jev decision battery, for the video
*How to Use Jev to Build a 24/7 HFT Trading System*.

The split, in one sentence: code computes the state, Jev answers seven typed
questions about it in one call, your code composes those answers into an
action using thresholds you set in `strategy.py`, a risk engine can veto
any of it, and execution talks to a real venue's market data.

## How Marteau differs from BOMBO

BOMBO's `jev-loop` trades Alpaca (US equities and spot crypto, paper by
default, an opt-in live-trading path with a three-gate safety design).
Marteau is a **separate bot**, not an extension of that one: it trades
**Hyperliquid perpetuals** instead, and today it can only ever simulate
doing so.

Why perpetuals and not Hyperliquid spot: this project's strategy is
market-making / HFT, and Hyperliquid's liquidity concentrates in its perp
markets. Spot is comparatively thin there, so a market-maker built against
HL spot would be fighting worse liquidity for no reason. `assets.py`
defaults to `BTC-PERP` for that reason.

What exists so far:

- `jevloop/execution/hyperliquid.py` -- read-only market data from
  Hyperliquid's public, unauthenticated `/info` REST endpoint and public WS
  channels (`l2Book`, `trades`, `allMids`, `candle`): order book, recent
  trades, mark/oracle price, funding rate. No API key, no wallet, no
  signature anywhere in this file.
- `jevloop/assets.py` -- a `perp` asset class (`resolve_perp_symbol`)
  alongside the existing Alpaca-facing `crypto`/`us_equity` classes, with
  its own leverage, shorting-allowed, and liquidation-aware fields. The
  Alpaca classes are unchanged.
- `jevloop/state.py` -- the snapshot gained `funding_rate`, `basis_bps`
  (mark vs. oracle spread), and `cancel_rate_per_min` (derived locally from
  the dry loop's own simulated cancel/replace events, the same
  trailing-window pattern `trade_intensity_per_s` already used). All three
  are `None` on the Alpaca path, which never passes them.
- `jevloop/loop_hyperliquid.py` (`jev-loop run-hyperliquid`) -- **dry
  execution only**. Real market data, a real Jev battery call, the same
  code-owned policy/pricing/risk pipeline as the Alpaca loop, but every
  order is simulated locally: a resting simulated quote fills when a real
  trade prints at or through its price (see `_crossing_fills()`), a
  directional leg fills immediately at the current best bid/ask, and every
  simulated fill goes through the same `state.apply_fill()` the Alpaca loop
  uses for real ones.

**There is no live-trading path in Marteau yet, and no credential of any
kind for Hyperliquid anywhere in this repo.** Placing a real order on
Hyperliquid needs the signed `POST /exchange` endpoint and a wallet
signature (an EIP-712-signed request, typically via an "agent wallet"
authorized by the real account) -- fundamentally different from Alpaca's
API-key auth. `HyperliquidPerpClient.submit_limit_order`,
`submit_market_order`, and `cancel_own_orders` all raise
`LiveTradingNotImplemented` unconditionally; there is no flag, env var, or
code path that reaches `/exchange`. Building that safely -- its own
three-gate-style refusal mirroring `execution/alpaca.py`'s, plus a real
liquidation-price risk check in `risk.py` (which does not exist yet: the
current `max_leverage` check is a ceiling on the asset's *configured*
leverage, not a live margin/liquidation-price computation, since there is
no real account to measure margin against) -- is explicit, separate,
future work.

## Quick start

```bash
cd ~/.claude/skills/jev-loop
cp .env.example .env        # fill in ALPACA_API_KEY / ALPACA_SECRET_KEY at minimum
uv run pytest -q             # tests, no network needed
uv run python -m jevloop explain-split         # the split, as a table
uv run python -m jevloop validate-symbol AAPL  # resolve any symbol first
uv run python -m jevloop run --paper --ticks 30 --symbol BTC/USD
uv run python -m jevloop serve   # open http://127.0.0.1:8765
```

`--symbol` takes any crypto pair (24/7) or US equity ticker (market hours
only); `jevloop/assets.py` resolves it into endpoints, order-size rules,
and session rules. Orders are sized from a dollar target, not a fixed
quantity, so the same defaults work across assets.

No `AI_GATEWAY_API_KEY` or `TYPESAFE_API_KEY`? The loop runs on a
clearly-labelled mock decision client. It says so on the first line. The
mock answers with random numbers, so it never places orders, not even on
paper: a mock run is always a dry run.

## Quick start (Hyperliquid perps, dry execution)

No `.env` needed for this one -- there is nothing to configure, since there
is no credential of any kind for Hyperliquid in this phase:

```bash
uv run python -m jevloop validate-symbol BTC-PERP   # resolve a perp spec
uv run python -m jevloop run-hyperliquid --ticks 30 --symbol BTC-PERP
uv run python -m jevloop run-hyperliquid --mock --forever --symbol ETH-PERP
```

Every tick reads the real Hyperliquid order book, mark/oracle price, and
funding rate, and fires a real Jev battery call (or the mock, same as the
Alpaca loop). Every "fill" is simulated locally against real market data
and is always printed with a `dry:` prefix. `--mock` forces the mock
decision client; without it, the same `AI_GATEWAY_API_KEY`/
`TYPESAFE_API_KEY` resolution as the Alpaca loop applies. Optional: `uv pip
install websocket-client` (or `uv sync --extra hyperliquid`) enables the
real-time trade tape used to simulate fills against actual crossing trades;
without it the loop still runs, just with an empty trade tape (honest
degradation, not a fabricated one).

The dashboard (`jevloop serve`) does not read the Hyperliquid loop's feed
yet -- `run-hyperliquid` writes to `~/.jev-loop/hyperliquid-log.jsonl` and
`hyperliquid-latest.json`, separate files from the Alpaca loop's, so the
two never clobber each other, but `dashboard/*.html` is not wired to poll
the Hyperliquid ones. That is on the list of what is still missing, not
something silently broken.

## Running continuously

A plain run stops after `--ticks N`. To keep it going:

```bash
uv run python -m jevloop run --paper --forever --symbol BTC/USD
# or, equivalently:
uv run python -m jevloop run --paper --ticks 0 --symbol BTC/USD
```

Add `--bar 30m` (or `1m`, `5m`, `15m`, `1h`) to decide once per bar close,
UTC-aligned, instead of every 2 s. Returns and volatility still come from
real one-minute bars, refreshed at each close.

Stop it with Ctrl+C in the foreground. In the background:

```bash
nohup uv run python -m jevloop run --paper --forever --symbol BTC/USD \
  > ~/.jev-loop/continuous.log 2>&1 &
echo $! > ~/.jev-loop/continuous.pid
# ...later...
kill "$(cat ~/.jev-loop/continuous.pid)"
```

On Windows PowerShell there is no `nohup` or `kill`, and `jevloop` needs
the keys from `.env` loaded by uv. Run it in its own minimised window and
stop it with Ctrl+C in that window (a `Stop-Process` is a hard stop that
skips the order clean-up below):

```powershell
Start-Process powershell -WindowStyle Minimized -WorkingDirectory "$HOME\.claude\skills\jev-loop" -ArgumentList "-NoExit", "-Command", "uv run --env-file .env python -m jevloop run --paper --forever"
```

Either way, on Ctrl+C or `kill` the loop cancels the resting orders it
placed itself before it exits rather than leaving them open. It never
touches other orders on your Alpaca account. The dashboard (`jevloop
serve`) keeps reading the same `~/.jev-loop/latest.json` regardless of
whether the run is bounded or continuous.

## Your strategy lives in strategy.py

Everything this loop ships with is a harness, not an edge: a generic
strategy pulled out of thin air, wired in only so a demo run shows real
fills. `jevloop/strategy.py` owns the seven tunable thresholds behind
`compose_action()` and an `apply_strategy()` hook that gets one last
look at every action before it goes near an order, free to change or
veto it. The shipped default matches exactly what the video ran.

## Reading the tick output

Every printed tick line, log row, and dashboard tile is explained field by
field in [TICK_OUTPUT.md](TICK_OUTPUT.md) -- what `regime`, `tox`, `env`,
`xh`, `skew`, and `late` mean, and what each action (`QUOTE_WIDE`,
`PULL_QUOTES`, `STAND_DOWN`, ...) does.

## The nine-stage loop

```
block event -> read the book -> state snapshot -> battery (seven Jev judgments)
  -> policy engine (strategy.py's thresholds + hook) -> pricing (Avellaneda-Stoikov, your code)
  -> risk veto (your code, absolute) -> post-only quotes + directional leg
  -> log, fills, inventory
```

## The fallback ladder

```
healthy + high confidence -> RUN
healthy + low confidence  -> REDUCE
late past the tick budget -> HOLD_LATE (never quote on stale state)
Jev unavailable            -> RULES_ONLY (deterministic fallback, no model)
hard limit breached        -> KILL (cancel own orders, market-close the position it built, stop)
```

## Live trading (opt-in, off by default)

Paper is the default everywhere: `execution/alpaca.py` will not reach
Alpaca's live endpoint unless all three of the following are true at
once.

```bash
export JEV_LOOP_ALLOW_LIVE=i-understand-the-risk
uv run python -m jevloop run --live --ticks 30 --symbol BTC/USD
# then type the exact confirmation phrase the CLI prints and asks for
```

Windows PowerShell sets the variable with
`$env:JEV_LOOP_ALLOW_LIVE = "i-understand-the-risk"` and runs
`uv run --env-file .env python -m jevloop run --live --ticks 30 --symbol BTC/USD`.

Missing the environment variable, missing `--live`, or typing anything
other than the exact confirmation phrase refuses to start; it never
falls back to paper silently. This is real money with no paper safety
net, at the same small dollar caps `limits.py` enforces on paper -- a
strategy can never raise them. Treat `--live` as what it is: a flip of
a switch made deliberately awkward so it only ever happens on purpose.

## Safety

- Paper by default, everywhere on the Alpaca side. Live trading exists only
  behind the three-gate opt-in above.
- The hard risk caps live in `jevloop/limits.py` and never change between
  paper and live. The tunable strategy thresholds live in `jevloop/strategy.py`
  instead -- change a number, restart, see different behaviour.
- The risk engine (`jevloop/risk.py`) never calls Jev and never delegates,
  and runs after `strategy.py`'s hook has had its say, not before.
- Long or flat only on Alpaca. Alpaca crypto is spot and this account can't
  short, so a SELL signal closes the long this run holds (never more than
  Alpaca says is held) and does nothing when flat ("no position to close").
  Hyperliquid perps allow short too, but Marteau's Hyperliquid side never
  places a real order of either sign -- see below.
- **Hyperliquid: dry execution only, no exceptions, in this phase.**
  `run-hyperliquid` has no `--live` flag and no `--dry-execution` flag to
  turn off; it is the only mode. There is no wallet key, agent-wallet
  address, or private key read from the environment anywhere in this repo.
  `HyperliquidPerpClient`'s order-placement methods
  (`submit_limit_order`/`submit_market_order`/`cancel_own_orders`) raise
  `LiveTradingNotImplemented` unconditionally -- not gated behind a check
  that could be bypassed, simply not implemented. `test_hyperliquid_client.py`
  asserts this, plus a structural scan for any credential-shaped token
  (`private_key`, `wallet_key`, `eth_account`, ...) appearing anywhere in
  `execution/hyperliquid.py`'s source.
- This is not investment advice and it does not claim a profit. It is a
  scaffold for a decision battery, a policy engine, and a risk layer around
  a fast model. Edge is still your job, and it lives in `strategy.py`.
