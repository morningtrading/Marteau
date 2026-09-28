# Reading the tick output

Every line `jevloop run` prints (and every row in `~/.jev-loop/log.jsonl` /
the dashboard) is one block of the [nine-stage loop](README.md#the-nine-stage-loop).
This is a field-by-field guide to what each column means.

## Example line

```
tick 217 | mid 83,390.7 | regime mean_reverting 93% | tox 0.29 | env 2.1 | xh 2.8 | 579 ms | QUOTE_WIDE skew +0.0 | -
```

Read as: *Jev judged the market mean-reverting with 93% confidence, low
toxicity, a decent-but-not-excellent quoting environment, and healthy
execution. Code (`strategy.py` + `policy.py`) decided to post wide
defensive quotes with no inventory skew. Nothing filled this tick.*

## Field by field

| Field | Meaning |
|---|---|
| `tick N` | The block number: one loop iteration, one decision. Default cadence is one block every 2 seconds (`--bar` changes this to one per bar close instead). |
| `mid` | The current mid-price (average of best bid/ask), computed deterministically from the live order book — not a Jev judgment. |
| `regime <name> <conf>%` | Jev battery answer 1/7: which market regime the state looks like — `trending`, `mean_reverting`, `high_vol`, or `crisis` — with its confidence. `n/a` if no answer came back this tick (see `late` below). |
| `tox` | Jev battery answer 3/7, "toxic flow": a 0–1 probability that incoming order flow is informed/toxic rather than noise. Higher means more likely someone with an edge is trading against you. |
| `env` | Jev battery answer 5/7, "quote environment" score, 0–3: how favourable this state is for providing liquidity — 0 = Do not quote, 1 = Marginal, 2 = Standard, 3 = Excellent. This is what separates `QUOTE_WIDE`/`QUOTE_BOTH_SIDES` from `STAND_DOWN` in `policy.py`. |
| `xh` | Jev battery answer 7/7, "execution health" score, 0–3: Broken / Degraded / Normal / Optimal — Jev's read on whether fill quality, rejects, slippage, and latency are trending well or badly. |
| `<N> ms` / `late` | Round-trip latency of the Jev call this tick. `late` means the call didn't return inside the tick's budget (deadline minus overhead), so the loop held rather than act on a missing or stale decision — see [the fallback ladder](README.md#the-fallback-ladder). |
| Action (`QUOTE_WIDE`, `QUOTE_BOTH_SIDES`, `PULL_QUOTES`, `WIDEN`, `STAND_DOWN`, `HOLD (late)`) | What `compose_action()` in `policy.py` — code, not Jev — decided from the battery answers and your thresholds in `strategy.py`. See "Actions" below. |
| `skew ±N` | How far the bid/ask are shifted off mid to lean the position back to flat, in `[-1, 1]`. Negative skews toward selling (cutting a long); positive skews toward buying (cutting a short). `+0.0` means inventory is flat, so no skew is needed. |
| Last column (`-` or `filled buy/sell <qty> @ <price>`) | Whether an order filled this tick, read back from Alpaca — never assumed. `-` means nothing filled. |

Two battery answers aren't shown in the printed line but are in the JSON
log/dashboard: `direction` (up/down/neutral price bias, with confidence)
and `liquidity_stressed` (0–1, is the book thinner than its recent norm).

## Actions

| Action | Meaning |
|---|---|
| `QUOTE_BOTH_SIDES` | Environment is favourable and Jev is confident — quote both sides normally, skewed by inventory pressure. |
| `QUOTE_WIDE` | Environment is acceptable but not excellent — quote both sides, wider than normal. |
| `WIDEN` | Liquidity looks stressed (or, in `RULES_ONLY` mode, book imbalance is too high) — widen quotes defensively. |
| `PULL_QUOTES` | Toxic flow is high — pull resting quotes entirely rather than get picked off. |
| `STAND_DOWN` | Quote environment score is below the quoting floor — do nothing this tick. |
| `HOLD (late)` | The Jev call didn't return in time — hold rather than act on stale/missing data. Never places an order. |
| `KILL` | A hard risk limit was breached — cancel this run's resting orders, close the position, stop. |

## Why some ticks are `late`

`late` doesn't mean anything is broken. It means the decision client (the
direct TypeSafe API or the Vercel AI Gateway) didn't answer within the
tick's deadline — commonly because the upstream model is rate-limited or
briefly overloaded. The loop's whole design point is to never quote on a
stale or missing decision, so it holds instead. If most or all ticks are
late, check the banner line at startup for which decision client won,
and see the **Setup** section in [SKILL.md](SKILL.md) for the two ways to
reach Jev (direct TypeSafe key vs. the Vercel AI Gateway) and their
tradeoffs.
