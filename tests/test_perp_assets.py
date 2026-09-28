import pytest

from jevloop.assets import UnknownSymbolError, resolve_perp_symbol, resolve_symbol, size_order


def test_resolves_known_perp_symbols():
    for sym in ("BTC-PERP", "ETH-PERP"):
        spec = resolve_perp_symbol(sym)
        assert spec.asset_class == "perp"
        assert spec.venue == "hyperliquid_perp"
        assert spec.is_24_7 is True
        assert spec.has_depth is True
        assert spec.shorting_allowed is True
        assert spec.min_notional_usd == 10.0
        assert spec.leverage > 1.0
        assert spec.liquidation_aware is True


def test_perp_resolution_is_case_insensitive():
    assert resolve_perp_symbol("btc-perp").symbol == "BTC-PERP"


def test_perp_coin_strips_suffix():
    assert resolve_perp_symbol("BTC-PERP").coin == "BTC"
    assert resolve_perp_symbol("ETH-PERP").coin == "ETH"


def test_unknown_perp_symbol_raises_with_a_helpful_message():
    with pytest.raises(UnknownSymbolError) as exc_info:
        resolve_perp_symbol("not a real symbol!!")
    assert "PERP" in str(exc_info.value)


def test_perp_symbol_rejected_by_alpaca_resolver():
    # resolve_symbol() must stay Alpaca-only: a perp symbol is not a crypto
    # pair or an equity ticker, so it should raise there, not silently
    # resolve to something wrong.
    with pytest.raises(UnknownSymbolError):
        resolve_symbol("BTC-PERP")


def test_alpaca_specs_keep_their_original_defaults():
    # The perp fields are additive and trailing; every Alpaca-facing spec
    # must still report the old, unleveraged spot/cash defaults.
    spec = resolve_symbol("BTC/USD")
    assert spec.venue == "alpaca"
    assert spec.leverage == 1.0
    assert spec.liquidation_aware is False
    assert spec.coin == ""

    equity_spec = resolve_symbol("AAPL")
    assert equity_spec.venue == "alpaca"
    assert equity_spec.leverage == 1.0


def test_perp_has_an_orderbook_url():
    spec = resolve_perp_symbol("BTC-PERP")
    assert spec.orderbook_url is not None
    assert spec.funding_rate_url is not None


def test_size_order_meets_perp_minimum():
    spec = resolve_perp_symbol("BTC-PERP")
    qty = size_order(notional_usd=5.0, price=85_000.0, spec=spec)
    assert qty * 85_000.0 >= spec.min_notional_usd


def test_size_order_respects_perp_precision():
    spec = resolve_perp_symbol("ETH-PERP")
    qty = size_order(notional_usd=50.0, price=3_123.456, spec=spec)
    assert round(qty, spec.qty_precision) == qty
