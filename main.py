"""TradeFlow CLI entry point."""

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="tradeflow", description="Autonomous MNQ futures trading bot."
    )
    parser.add_argument(
        "--paper", action="store_true", default=True, help="Paper trading mode (default)"
    )
    parser.add_argument(
        "--live", action="store_true", help="Live trading mode (requires explicit opt-in)"
    )
    parser.add_argument("--backtest", action="store_true", help="Run backtest mode")
    args = parser.parse_args()

    if args.live:
        print("Live mode is not yet implemented (Phase 7).", file=sys.stderr)
        return 1
    if args.backtest:
        print("Backtest mode is not yet implemented (Phase 6).", file=sys.stderr)
        return 1
    # Paper mode default — also not implemented in Phase 0, just confirms the scaffold parses
    print("TradeFlow Phase 0 scaffold. Paper trading loop lands in Phase 3 PR 6+.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
