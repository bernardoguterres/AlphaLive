#!/usr/bin/env python3
"""
AlphaLive - Live Trading Execution Engine

Simple entry point for both local development and Railway deployment.
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import numpy as np
import pandas as pd

from alphalive.utils.env_bool import read_bool_env


def print_banner(args, paper):
    """Print startup banner with configuration info."""
    print("=" * 80)
    print("  █████╗ ██╗     ██████╗ ██╗  ██╗ █████╗ ██╗     ██╗██╗   ██╗███████╗")
    print(" ██╔══██╗██║     ██╔══██╗██║  ██║██╔══██╗██║     ██║██║   ██║██╔════╝")
    print(" ███████║██║     ██████╔╝███████║███████║██║     ██║██║   ██║█████╗  ")
    print(" ██╔══██║██║     ██╔═══╝ ██╔══██║██╔══██║██║     ██║╚██╗ ██╔╝██╔══╝  ")
    print(" ██║  ██║███████╗██║     ██║  ██║██║  ██║███████╗██║ ╚████╔╝ ███████╗")
    print(" ╚═╝  ╚═╝╚══════╝╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝  ╚══════╝")
    print("=" * 80)
    print(f"  Config: {args.config}")

    if hasattr(args, "replay_mode") and args.replay_mode:
        print(f"Mode: REPLAY MODE (Historical Data Testing - FREE)")
        print(f"  Period: {args.replay_start} to {args.replay_end}")
    else:
        print(
            f"Mode: {'DRY RUN (no real orders)' if args.dry_run else ('PAPER TRADING' if paper else 'LIVE TRADING ')}"
        )

    print(
        f"  Platform: {'Railway' if os.environ.get('RAILWAY_ENVIRONMENT') else 'Local'}"
    )
    print("=" * 80)


def _read_bool_env_or_exit(var_name: str, default: bool) -> bool:
    """read_bool_env(), but exits with a clear message instead of a raw
    traceback if the env var is set to something unparseable - this runs
    before logging is configured, so a bare exception here would otherwise
    just print a Python traceback with no context."""
    try:
        return read_bool_env(var_name, default=default)
    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)


# --- Offline validation (run.py --validate-only) ---------------------------
# Checks only what can be checked without credentials, a broker or the
# network: the strategy JSON parses against the schema (and its migrations),
# the strategy set is deployable together (one strategy per ticker), trailing
# stops have persistent storage, and each strategy's signal code runs on
# synthetic bars. It never reads a .env file or Alpaca credentials and never
# builds a broker or data client; broker connectivity is only exercised by a
# real (dry-run/paper) start. tests/test_validate_offline.py runs it through
# this entry point with credential reads and network calls made to fail.


def _synthetic_bars(n: int = 300) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    idx = pd.bdate_range(end="2024-12-31", periods=n, tz="America/New_York")
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": np.full(n, 1e6),
        },
        index=idx,
    )


def validate_offline(config_path: str) -> int:
    """Validate strategy configuration offline; returns a process exit code."""
    from alphalive.config import load_config_path, validate_strategies
    from alphalive.state import check_trailing_stop_requirements
    from alphalive.strategy.signal_engine import SignalEngine

    print("Validating configuration offline (no credentials, broker or network)...")
    try:
        strategies = load_config_path(config_path)
    except Exception as e:  # schema/JSON/migration errors
        print(f"INVALID: could not load {config_path}: {e}")
        return 1
    print(f"Loaded {len(strategies)} strategy/strategies")

    errors = validate_strategies(strategies)
    for strategy in strategies:
        try:
            check_trailing_stop_requirements(strategy)
        except SystemExit:
            errors.append(
                f"{strategy.ticker}: trailing_stop_enabled requires PERSISTENT_STORAGE=true"
            )
        try:
            result = SignalEngine(strategy).generate_signal(_synthetic_bars())
            print(
                f"  {strategy.strategy.name}/{strategy.ticker}: signal code runs "
                f"(synthetic bars -> {result['signal']})"
            )
        except Exception as e:
            errors.append(f"{strategy.ticker}: signal generation failed: {e}")

    if errors:
        print("INVALID:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("VALID (offline checks only; broker connectivity is not tested)")
    return 0


def main():
    """Main entry point."""
    # Load .env before argparse builds its defaults below - --config and
    # --dry-run read os.environ at add_argument() time, which runs before
    # alphalive.config.load_env()'s own load_dotenv() call later in this
    # function. Without this, a .env-only setup (no shell export) silently
    # falls back to STRATEGY_CONFIG_DIR/STRATEGY_CONFIG's hardcoded default
    # and crashes with FileNotFoundError. Mirrors load_env()'s own logic.
    #
    # --validate-only is the exception: it is an offline check and must not
    # load credentials (it used to load .env, build the broker and call
    # GET /v2/account). It runs here, before .env, the broker or any
    # network-dependent startup, and takes --config or STRATEGY_CONFIG(_DIR)
    # from the shell only.
    if "--validate-only" in sys.argv[1:]:
        pre = argparse.ArgumentParser(add_help=False)
        pre.add_argument("--validate-only", action="store_true")
        pre.add_argument(
            "--config",
            default=os.environ.get("STRATEGY_CONFIG_DIR")
            or os.environ.get("STRATEGY_CONFIG"),
        )
        known, _ = pre.parse_known_args()
        if not known.config:
            print("--validate-only needs --config (or STRATEGY_CONFIG in the shell)")
            sys.exit(2)
        sys.exit(validate_offline(known.config))

    dotenv_path = Path(".env")
    if dotenv_path.exists():
        load_dotenv(dotenv_path)

    parser = argparse.ArgumentParser(
        description="AlphaLive: Execute trading strategies live via Alpaca",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Validate strategy config offline (no credentials, broker or network)
  python run.py --validate-only --config configs/example_strategy.json

  # Run in dry-run mode (log trades but don't execute)
  python run.py --dry-run

  # Run with specific config file
  python run.py --config configs/ma_crossover.json

  # Run on Railway (uses environment variables)
  python run.py

Environment Variables:
  STRATEGY_CONFIG       - Path to strategy JSON (or use --config)
  STRATEGY_CONFIG_DIR   - Path to directory of strategy JSONs (multi-strategy mode)
  ALPACA_API_KEY        - Alpaca API key (required)
  ALPACA_SECRET_KEY     - Alpaca secret key (required)
  ALPACA_PAPER          - Use paper trading (true/false, default: true)
  TELEGRAM_BOT_TOKEN    - Telegram bot token (optional)
  TELEGRAM_CHAT_ID      - Telegram chat ID (optional)
  DRY_RUN               - Log trades without executing (true/false, default: false)
  LOG_LEVEL             - Logging level (DEBUG/INFO/WARNING/ERROR, default: INFO)
  TRADING_PAUSED        - Pause all trading (true/false, default: false)
        """,
    )

    parser.add_argument(
        "--config",
        default=os.environ.get("STRATEGY_CONFIG_DIR")
        or os.environ.get("STRATEGY_CONFIG", "configs/strategy.json"),
        help="Path to strategy JSON file or directory (default: STRATEGY_CONFIG_DIR or STRATEGY_CONFIG env var)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=_read_bool_env_or_exit("DRY_RUN", default=False),
        help="Log all actions but don't place real orders",
    )

    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate strategy config offline (no credentials or network), then exit",
    )

    # Replay mode arguments
    parser.add_argument(
        "--replay-mode",
        action="store_true",
        help="Run in replay mode using historical data (FREE - no subscription needed)",
    )

    parser.add_argument(
        "--replay-start",
        type=str,
        default="2015-01-01",
        help="Replay start date (YYYY-MM-DD) - Default: 2015-01-01 (pre-COVID)",
    )

    parser.add_argument(
        "--replay-end",
        type=str,
        default="2019-12-31",
        help="Replay end date (YYYY-MM-DD) - Default: 2019-12-31 (pre-COVID)",
    )

    parser.add_argument(
        "--replay-speed",
        type=int,
        default=0,
        help="Replay speed multiplier (0=instant, 1=1sec per day, etc.) - Default: 0",
    )

    args = parser.parse_args()

    # Paper/live is controlled by ALPACA_PAPER env var, not CLI flag
    # (safer for Railway - can't accidentally pass --live flag)
    paper = _read_bool_env_or_exit("ALPACA_PAPER", default=True)

    # --- Normal run ---
    if not paper:
        print()
        print("WARNING ")
        print("LIVE TRADING MODE - REAL MONEY AT RISK ")
        print("WARNING ")
        print()

    # Print startup banner
    print_banner(args, paper)
    print()

    # Setup logging
    from alphalive.utils.logger import setup_logger

    setup_logger()

    # Run main loop
    from alphalive.main import main as run_main

    try:
        run_main(
            config_path=args.config,
            dry_run=args.dry_run,
            paper=paper,
            replay_mode=args.replay_mode,
            replay_start=args.replay_start,
            replay_end=args.replay_end,
            replay_speed=args.replay_speed,
        )
    except KeyboardInterrupt:
        print()
        print("Shutting down...")
        sys.exit(0)
    except Exception as e:
        print(f"Fatal error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
