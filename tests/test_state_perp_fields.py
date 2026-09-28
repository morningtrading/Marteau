import time

from jevloop.state import InventoryState, build_snapshot


def _base_kwargs(now, **extra):
    inv = InventoryState(equity_usd=1000.0, high_water_mark_usd=1000.0)
    kwargs = dict(
        as_of=now,
        mid=100.0,
        microprice=100.0,
        spread_bps=3.0,
        bid_depth=[(99.9, 1.0)],
        ask_depth=[(100.1, 1.0)],
        trade_prices=[(now, 100.0)],
        trade_sides=[(now, "buy")],
        inv=inv,
        data_timestamp=now,
    )
    kwargs.update(extra)
    return kwargs


def test_alpaca_path_unchanged_when_perp_kwargs_omitted():
    now = time.time()
    snap = build_snapshot(**_base_kwargs(now))
    assert snap["leverage"] == 1.0
    assert snap["funding_rate"] is None
    assert snap["basis_bps"] is None
    assert snap["cancel_rate_per_min"] is None


def test_leverage_passthrough():
    now = time.time()
    snap = build_snapshot(**_base_kwargs(now, leverage=3.0))
    assert snap["leverage"] == 3.0


def test_funding_rate_passthrough():
    now = time.time()
    snap = build_snapshot(**_base_kwargs(now, funding_rate=0.0001))
    assert snap["funding_rate"] == 0.0001


def test_basis_bps_computed_from_mark_vs_oracle():
    now = time.time()
    # mid (mark) = 100.0, oracle = 99.0 -> mark trading above fair value
    snap = build_snapshot(**_base_kwargs(now, oracle_px=99.0))
    assert snap["basis_bps"] > 0
    expected = round((100.0 - 99.0) / 99.0 * 10_000, 2)
    assert snap["basis_bps"] == expected


def test_basis_bps_none_without_oracle_px():
    now = time.time()
    snap = build_snapshot(**_base_kwargs(now))
    assert snap["basis_bps"] is None


def test_cancel_rate_counts_events_in_trailing_window():
    now = time.time()
    # 6 cancel events in the last 60s -> 6 per 60s -> 6 per minute
    events = [now - i * 5 for i in range(6)]
    snap = build_snapshot(**_base_kwargs(now, cancel_events=events, cancel_window_s=60.0))
    assert snap["cancel_rate_per_min"] == 6.0


def test_cancel_rate_ignores_events_outside_window():
    now = time.time()
    events = [now - 1000.0]  # far outside a 60s window
    snap = build_snapshot(**_base_kwargs(now, cancel_events=events, cancel_window_s=60.0))
    assert snap["cancel_rate_per_min"] == 0.0


def test_cancel_rate_zero_with_empty_events_list():
    now = time.time()
    snap = build_snapshot(**_base_kwargs(now, cancel_events=[]))
    assert snap["cancel_rate_per_min"] == 0.0
