"""Asset resolver: turns any symbol into a spec.

Three asset classes: crypto (24/7 spot, e.g. BTC/USD, ETH/USD), us_equity
(market hours only, e.g. AAPL, SPY, TSLA, NVDA), and perp (24/7 Hyperliquid
perpetuals, e.g. BTC-PERP, ETH-PERP). Everything asset-specific -- which
venue endpoints to call, how big an order must be, how many decimals a
quantity can have, whether shorting is allowed, whether leverage applies,
whether the venue is open right now -- lives here. The rest of the loop
reads an AssetSpec and never special-cases a symbol.

The perp fields (venue, coin, leverage, liquidation_aware) are additive and
trailing with defaults, specifically so the existing crypto/us_equity path
through resolve_symbol() is byte-for-byte unchanged: every AssetSpec built
by resolve_symbol() still gets venue="alpaca", leverage=1.0,
liquidation_aware=False, exactly as before this file grew a perp class.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CRYPTO_DATA_BASE = "https://data.alpaca.markets/v1beta3/crypto/us"
EQUITY_DATA_BASE = "https://data.alpaca.markets/v2/stocks"
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"

_CRYPTO_PATTERN = re.compile(r"^[A-Z0-9]{2,10}/(USD|USDT|USDC)$")
_EQUITY_PATTERN = re.compile(r"^[A-Z]{1,5}$")
_PERP_PATTERN = re.compile(r"^[A-Z0-9]{2,10}-PERP$")

# Tick sizes for the symbols the video and the article actually name.
# Anything else falls back to the class default below.
_KNOWN_TICK_SIZE = {
    "BTC/USD": 0.01,
    "ETH/USD": 0.01,
}

# Hyperliquid perp tick size / quantity precision, for the pairs this repo
# actually defaults to. These are approximations, not pulled live from the
# venue: Hyperliquid's real tick size follows a five-significant-figure rule
# that moves with price, and szDecimals can change if the venue relists an
# asset. HyperliquidInfoClient.asset_ctx_for() can refine both at startup
# from metaAndAssetCtxs; these are the honest fallback when that call is
# skipped or fails, in the same spirit as _KNOWN_TICK_SIZE above.
_KNOWN_PERP_SPEC = {
    "BTC-PERP": {"coin": "BTC", "tick_size": 1.0, "qty_precision": 5},
    "ETH-PERP": {"coin": "ETH", "tick_size": 0.1, "qty_precision": 4},
}
_DEFAULT_PERP_LEVERAGE = 3.0  # conservative default; real per-asset max is in metaAndAssetCtxs


class UnknownSymbolError(Exception):
    pass


@dataclass(frozen=True)
class AssetSpec:
    symbol: str
    asset_class: str  # "crypto" | "us_equity" | "perp"
    tick_size: float
    min_notional_usd: float
    qty_precision: int
    shorting_allowed: bool
    is_24_7: bool
    has_depth: bool  # False on venues where only best bid/ask is available
    orderbook_url: str | None
    latest_trade_url: str
    recent_trades_url: str
    latest_quote_url: str
    # -- perp-specific, trailing with defaults so the two classes above are
    # unaffected (see module docstring) --
    venue: str = "alpaca"  # "alpaca" | "hyperliquid_perp"
    coin: str = ""  # Hyperliquid's own coin name, e.g. "BTC" for "BTC-PERP"
    leverage: float = 1.0  # 1.0 = no leverage (both Alpaca classes are spot/cash)
    liquidation_aware: bool = False  # True => risk.py must consider liquidation price
    funding_rate_url: str | None = None  # where funding/mark/oracle are read (metaAndAssetCtxs)


def resolve_symbol(raw: str) -> AssetSpec:
    """Resolve any symbol Lewis (or the viewer) might type. Raises
    UnknownSymbolError with a plain-English message on anything that
    matches neither a crypto pair nor a US equity ticker."""
    sym = raw.strip().upper()

    if _CRYPTO_PATTERN.match(sym):
        return AssetSpec(
            symbol=sym,
            asset_class="crypto",
            tick_size=_KNOWN_TICK_SIZE.get(sym, 0.01),
            min_notional_usd=10.0,  # Alpaca's crypto minimum order notional
            qty_precision=8,
            shorting_allowed=False,  # spot only, no naked shorting
            is_24_7=True,
            has_depth=True,  # crypto v1beta3 gives real L2 depth
            orderbook_url=f"{CRYPTO_DATA_BASE}/latest/orderbooks",
            latest_trade_url=f"{CRYPTO_DATA_BASE}/latest/trades",
            recent_trades_url=f"{CRYPTO_DATA_BASE}/trades",
            latest_quote_url=f"{CRYPTO_DATA_BASE}/latest/quotes",
        )

    if _EQUITY_PATTERN.match(sym):
        return AssetSpec(
            symbol=sym,
            asset_class="us_equity",
            tick_size=0.01,
            min_notional_usd=1.0,  # Alpaca supports fractional notional equity orders
            qty_precision=4,
            shorting_allowed=False,  # this account is cash, not marginable
            is_24_7=False,
            has_depth=False,  # the basic equities feed has no L2 book
            orderbook_url=None,
            latest_trade_url=f"{EQUITY_DATA_BASE}/trades/latest",
            recent_trades_url=f"{EQUITY_DATA_BASE}/trades",
            latest_quote_url=f"{EQUITY_DATA_BASE}/quotes/latest",
        )

    raise UnknownSymbolError(
        f"'{raw}' does not look like a crypto pair (BTC/USD, ETH/USD, ...) or a "
        "US equity ticker (AAPL, SPY, TSLA, NVDA, ...). Pick one of those forms."
    )


def resolve_perp_symbol(raw: str) -> AssetSpec:
    """Resolve a Hyperliquid perpetual symbol, e.g. "BTC-PERP" or "ETH-PERP".

    Kept separate from resolve_symbol() on purpose: mixing perp resolution
    into the Alpaca-facing function would risk changing its error message or
    behaviour for callers that only ever pass it Alpaca symbols. Nothing in
    execution/alpaca.py or loop.py calls this function.

    Perps are always 24/7, always have real L2 depth, and always allow
    shorting (a perp position is just a signed size, long or short, unlike
    Alpaca's cash account which can only go long or flat). leverage defaults
    to a conservative 3.0, not the venue's real per-asset maximum (which can
    be much higher) -- this is a starting point for dry-execution risk
    accounting, not a claim about what Hyperliquid allows.
    """
    sym = raw.strip().upper()

    if not _PERP_PATTERN.match(sym):
        raise UnknownSymbolError(
            f"'{raw}' does not look like a Hyperliquid perp symbol "
            "(BTC-PERP, ETH-PERP, ...). Pick one of those forms."
        )

    known = _KNOWN_PERP_SPEC.get(sym)
    coin = known["coin"] if known else sym.removesuffix("-PERP")
    tick_size = known["tick_size"] if known else 0.01
    qty_precision = known["qty_precision"] if known else 4

    return AssetSpec(
        symbol=sym,
        asset_class="perp",
        tick_size=tick_size,
        min_notional_usd=10.0,  # Hyperliquid's minimum order notional is $10
        qty_precision=qty_precision,
        shorting_allowed=True,  # perps: a short is just a negative signed size
        is_24_7=True,
        has_depth=True,  # l2Book gives real depth
        orderbook_url=HYPERLIQUID_INFO_URL,
        latest_trade_url=HYPERLIQUID_INFO_URL,  # trades: public WS channel, not a GET url
        recent_trades_url=HYPERLIQUID_INFO_URL,
        latest_quote_url=HYPERLIQUID_INFO_URL,
        venue="hyperliquid_perp",
        coin=coin,
        leverage=_DEFAULT_PERP_LEVERAGE,
        liquidation_aware=True,
        funding_rate_url=HYPERLIQUID_INFO_URL,
    )


def size_order(notional_usd: float, price: float, spec: AssetSpec) -> float:
    """Convert a target notional (dollars) into a quantity, rounded to the
    asset's precision, bumped up if rounding pushed it back under the
    venue's minimum notional.

    Sizing by notional rather than a fixed quantity is the fix for a real
    bug: a fixed 0.0002 BTC order is comfortably above Alpaca's $10 crypto
    minimum most of the time, but the same fixed quantity on a $30 stock,
    or during a crypto drawdown, can fall under the venue floor and get
    rejected. Sizing from a dollar target is honest across any asset and
    any price.
    """
    if price <= 0:
        raise ValueError("price must be positive")
    target = max(notional_usd, spec.min_notional_usd)
    qty = target / price
    quantized = round(qty, spec.qty_precision)
    if quantized * price < spec.min_notional_usd:
        step = 10 ** (-spec.qty_precision)
        guard = 0
        while quantized * price < spec.min_notional_usd and guard < 10_000:
            quantized = round(quantized + step, spec.qty_precision)
            guard += 1
    return quantized
