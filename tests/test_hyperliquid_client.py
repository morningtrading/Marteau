"""Tests for execution/hyperliquid.py. No real network calls: HTTP is
mocked at the requests.post boundary, exactly the way test_alpaca_guard.py
tests execution/alpaca.py without hitting the real Alpaca API.

The safety-critical assertions here are the LiveTradingNotImplemented ones:
this file exists to make it hard to ever accidentally wire a real order
through HyperliquidPerpClient.
"""

from __future__ import annotations

import pytest

from jevloop.execution.hyperliquid import (
    HyperliquidAPIError,
    HyperliquidInfoClient,
    HyperliquidPerpClient,
    LiveTradingNotImplemented,
    client_from_env,
)


class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def test_client_from_env_needs_no_credentials(monkeypatch):
    # There must be no environment variable this function reads at all.
    monkeypatch.delenv("HYPERLIQUID_API_KEY", raising=False)
    monkeypatch.delenv("HYPERLIQUID_PRIVATE_KEY", raising=False)
    client = client_from_env(coin="BTC")
    assert client.coin == "BTC"


def test_l2_book_shapes_like_alpaca_orderbook(monkeypatch):
    client = HyperliquidPerpClient(coin="BTC")

    def fake_post(url, json, timeout):
        assert json == {"type": "l2Book", "coin": "BTC"}
        return _FakeResponse(
            200,
            {
                "coin": "BTC",
                "time": 1234,
                "levels": [
                    [{"px": "100.0", "sz": "1.5", "n": 3}],
                    [{"px": "100.2", "sz": "2.0", "n": 1}],
                ],
            },
        )

    import jevloop.execution.hyperliquid as hl_mod

    monkeypatch.setattr(hl_mod.requests, "post", fake_post)
    book = client.get_orderbook()
    assert book["b"] == [(100.0, 1.5)]
    assert book["a"] == [(100.2, 2.0)]


def test_get_mark_oracle_funding(monkeypatch):
    client = HyperliquidPerpClient(coin="BTC")

    def fake_post(url, json, timeout):
        assert json == {"type": "metaAndAssetCtxs"}
        return _FakeResponse(
            200,
            [
                {"universe": [{"name": "BTC", "szDecimals": 5}]},
                [{"markPx": "100.5", "oraclePx": "100.4", "funding": "0.0001"}],
            ],
        )

    import jevloop.execution.hyperliquid as hl_mod

    monkeypatch.setattr(hl_mod.requests, "post", fake_post)
    mark, oracle, funding = client.get_mark_oracle_funding()
    assert mark == 100.5
    assert oracle == 100.4
    assert funding == 0.0001


def test_unknown_coin_raises_api_error(monkeypatch):
    client = HyperliquidPerpClient(coin="NOPE")

    def fake_post(url, json, timeout):
        return _FakeResponse(200, [{"universe": []}, []])

    import jevloop.execution.hyperliquid as hl_mod

    monkeypatch.setattr(hl_mod.requests, "post", fake_post)
    with pytest.raises(HyperliquidAPIError):
        client.get_mark_oracle_funding()


def test_get_position_qty_with_no_address_returns_zero():
    client = HyperliquidPerpClient(coin="BTC")
    assert client.get_position_qty(address=None) == 0.0


def test_get_account_with_no_address_returns_empty_dict():
    client = HyperliquidPerpClient(coin="BTC")
    assert client.get_account(address=None) == {}


def test_is_market_open_always_true():
    client = HyperliquidPerpClient(coin="BTC")
    assert client.is_market_open() is True


# -- the safety guarantee: no live order path exists ------------------


def test_submit_limit_order_refuses_unconditionally():
    client = HyperliquidPerpClient(coin="BTC")
    with pytest.raises(LiveTradingNotImplemented):
        client.submit_limit_order("buy", 1.0, 100.0)


def test_submit_market_order_refuses_unconditionally():
    client = HyperliquidPerpClient(coin="BTC")
    with pytest.raises(LiveTradingNotImplemented):
        client.submit_market_order("buy", 1.0)


def test_cancel_own_orders_refuses_unconditionally():
    client = HyperliquidPerpClient(coin="BTC")
    with pytest.raises(LiveTradingNotImplemented):
        client.cancel_own_orders()


def test_no_signing_or_private_key_symbol_anywhere_in_module():
    """Structural guard: scans the module source for anything that looks
    like credential-loading. This is intentionally blunt -- it exists so an
    accidental `os.environ.get("HYPERLIQUID_PRIVATE_KEY")` added later fails
    a test immediately, not just a code review."""
    import inspect

    import jevloop.execution.hyperliquid as hl_mod

    source = inspect.getsource(hl_mod)
    for banned in ("private_key", "PRIVATE_KEY", "wallet_key", "signTypedData", "eth_account"):
        assert banned not in source, f"found forbidden credential-shaped token: {banned!r}"


def test_info_client_raises_on_http_error(monkeypatch):
    client = HyperliquidInfoClient()

    def fake_post(url, json, timeout):
        return _FakeResponse(500, "server error")

    import jevloop.execution.hyperliquid as hl_mod

    monkeypatch.setattr(hl_mod.requests, "post", fake_post)
    with pytest.raises(HyperliquidAPIError):
        client.l2_book("BTC")
