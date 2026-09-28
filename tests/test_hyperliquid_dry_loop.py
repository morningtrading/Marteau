"""Tests for loop_hyperliquid.py's simulation core.

_crossing_fills() is the heart of the dry-execution safety story: it is the
only thing standing in for a real fill on Hyperliquid, since submitting a
real order is never possible in this phase (see
execution/hyperliquid.py's LiveTradingNotImplemented). These tests exercise
it directly, with no network, no thread, and no running loop.
"""

from __future__ import annotations

from jevloop.limits import Limits, default_perp_limits
from jevloop.loop_hyperliquid import _crossing_fills


def test_no_trades_no_fills():
    fills, resting = _crossing_fills({"bid": {"price": 100.0, "qty": 1.0}}, [])
    assert fills == []
    assert resting == {"bid": {"price": 100.0, "qty": 1.0}}


def test_sell_at_or_below_bid_fills_the_bid():
    resting = {"bid": {"price": 100.0, "qty": 1.0}}
    fills, remaining = _crossing_fills(resting, [(1.0, 99.5, 0.4, "sell")])
    assert fills == [("buy", 0.4, 100.0, 1.0)]
    assert remaining["bid"]["qty"] == 0.6


def test_buy_at_or_above_ask_fills_the_ask():
    resting = {"ask": {"price": 100.2, "qty": 1.0}}
    fills, remaining = _crossing_fills(resting, [(1.0, 100.5, 0.3, "buy")])
    assert fills == [("sell", 0.3, 100.2, 1.0)]
    assert remaining["ask"]["qty"] == 0.7


def test_sell_above_bid_does_not_fill():
    resting = {"bid": {"price": 100.0, "qty": 1.0}}
    fills, remaining = _crossing_fills(resting, [(1.0, 100.5, 0.4, "sell")])
    assert fills == []
    assert remaining == resting


def test_buy_below_ask_does_not_fill():
    resting = {"ask": {"price": 100.2, "qty": 1.0}}
    fills, remaining = _crossing_fills(resting, [(1.0, 99.0, 0.4, "buy")])
    assert fills == []
    assert remaining == resting


def test_full_fill_removes_the_side():
    resting = {"bid": {"price": 100.0, "qty": 0.5}}
    fills, remaining = _crossing_fills(resting, [(1.0, 99.0, 10.0, "sell")])
    assert fills == [("buy", 0.5, 100.0, 1.0)]
    assert "bid" not in remaining


def test_multiple_trades_partially_consume_resting_qty():
    resting = {"bid": {"price": 100.0, "qty": 1.0}}
    trades = [
        (1.0, 99.0, 0.3, "sell"),
        (2.0, 99.5, 0.3, "sell"),
        (3.0, 100.0, 0.3, "sell"),
    ]
    fills, remaining = _crossing_fills(resting, trades)
    assert len(fills) == 3
    assert abs(sum(q for _, q, _, _ in fills) - 0.9) < 1e-9
    assert abs(remaining["bid"]["qty"] - 0.1) < 1e-9


def test_both_sides_can_fill_independently_in_one_tick():
    resting = {
        "bid": {"price": 100.0, "qty": 1.0},
        "ask": {"price": 100.2, "qty": 1.0},
    }
    trades = [(1.0, 99.9, 0.2, "sell"), (2.0, 100.3, 0.2, "buy")]
    fills, remaining = _crossing_fills(resting, trades)
    sides = {f[0] for f in fills}
    assert sides == {"buy", "sell"}
    assert remaining["bid"]["qty"] == 0.8
    assert remaining["ask"]["qty"] == 0.8


def test_input_resting_dict_not_mutated():
    resting = {"bid": {"price": 100.0, "qty": 1.0}}
    _crossing_fills(resting, [(1.0, 99.0, 0.5, "sell")])
    assert resting["bid"]["qty"] == 1.0  # caller's copy untouched


def test_default_perp_limits_raises_leverage_above_alpaca_default():
    alpaca = Limits()
    perp = default_perp_limits()
    assert alpaca.max_leverage == 1.0
    assert perp.max_leverage > 1.0
    # every other hard cap stays identical
    assert perp.max_position_usd == alpaca.max_position_usd
    assert perp.max_daily_loss_usd == alpaca.max_daily_loss_usd
    assert perp.max_drawdown_pct == alpaca.max_drawdown_pct
