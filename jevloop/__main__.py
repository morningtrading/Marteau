"""CLI dispatcher: `jev-loop run|run-hyperliquid|calibrate|serve|validate-symbol|explain-split`.

Also invocable as `uv run python -m jevloop <command> ...` from inside the
skill directory, which is what the /jev-loop skill's SKILL.md tells Claude
Code to run.
"""

from __future__ import annotations

import sys

USAGE = (
    "usage: jev-loop <run|run-hyperliquid|calibrate|serve|serve-hyperliquid|"
    "validate-symbol|explain-split> [options]"
)


def _validate_symbol(argv: list[str]) -> int:
    from .assets import UnknownSymbolError, resolve_perp_symbol, resolve_symbol

    if not argv:
        print("usage: jev-loop validate-symbol <SYMBOL>")
        return 1
    try:
        spec = resolve_symbol(argv[0])
    except UnknownSymbolError:
        try:
            spec = resolve_perp_symbol(argv[0])
        except UnknownSymbolError as exc:
            print(str(exc))
            return 1

    if spec.venue == "hyperliquid_perp":
        session = "24/7 (Hyperliquid perp)"
    elif spec.is_24_7:
        session = "24/7 (crypto)"
    else:
        session = "market hours only (US equity)"
    print(f"resolved: {spec.symbol}")
    print(f"  asset class:       {spec.asset_class}")
    print(f"  venue:             {spec.venue}")
    print(f"  session:           {session}")
    print(f"  min order notional: ${spec.min_notional_usd:.2f}")
    print(f"  quantity precision: {spec.qty_precision} decimal places")
    print(f"  shorting allowed:   {spec.shorting_allowed}")
    print(f"  order book depth:   {'yes' if spec.has_depth else 'best bid/ask only'}")
    if spec.liquidation_aware:
        print(f"  leverage (config): {spec.leverage:g}x")
    return 0


def _explain_split(argv: list[str]) -> int:
    from .split import render_split_table

    print(render_split_table())
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(USAGE)
        return 1

    command, rest = sys.argv[1], sys.argv[2:]
    if command == "run":
        from . import loop

        return loop.main(rest)
    if command == "run-hyperliquid":
        from . import loop_hyperliquid

        return loop_hyperliquid.main(rest)
    if command == "calibrate":
        from . import calibrate

        return calibrate.main(rest)
    if command == "serve":
        from . import serve

        return serve.main(rest)
    if command == "serve-hyperliquid":
        from . import serve_hyperliquid

        return serve_hyperliquid.main(rest)
    if command == "validate-symbol":
        return _validate_symbol(rest)
    if command == "explain-split":
        return _explain_split(rest)

    print(f"unknown command: {command!r}. {USAGE}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
