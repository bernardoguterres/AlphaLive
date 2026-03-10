#!/usr/bin/env python3
"""
AlphaLive — Live Trading Execution Engine

Simple entry point for both local development and Railway deployment.
"""

import argparse
import os
import sys


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
    print(f"  Mode: {'DRY RUN (no real orders)' if args.dry_run else ('PAPER TRADING' if paper else '🔴 LIVE TRADING 🔴')}")
    print(f"  Platform: {'Railway' if os.environ.get('RAILWAY_ENVIRONMENT') else 'Local'}")
    print("=" * 80)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="AlphaLive: Execute trading strategies live via Alpaca",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Validate config and connections, then exit
  python run.py --validate-only

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
        """
    )

    parser.add_argument(
        "--config",
        default=os.environ.get("STRATEGY_CONFIG_DIR") or os.environ.get("STRATEGY_CONFIG", "configs/strategy.json"),
        help="Path to strategy JSON file or directory (default: STRATEGY_CONFIG_DIR or STRATEGY_CONFIG env var)"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("DRY_RUN", "false").lower() == "true",
        help="Log all actions but don't place real orders"
    )

    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate config and connections, then exit"
    )

    args = parser.parse_args()

    # Paper/live is controlled by ALPACA_PAPER env var, not CLI flag
    # (safer for Railway — can't accidentally pass --live flag)
    paper = os.environ.get("ALPACA_PAPER", "true").lower() == "true"

    # --- validate-only branch ---
    if args.validate_only:
        print("Validating configuration and connections...")
        print()

        from alphalive.config import load_config_path, load_env, validate_all
        from alphalive.broker.alpaca_broker import AlpacaBroker

        try:
            # Load config (supports single file or directory)
            strategy_configs = load_config_path(args.config)
            app_config = load_env()

            print(f"Loaded {len(strategy_configs)} strategy/strategies")
            print()

            # Validate
            if not validate_all(strategy_configs, app_config):
                print("❌ Configuration validation failed")
                sys.exit(1)

            print("✅ Configuration valid")
            print()

            # Test broker connection
            print("Testing Alpaca connection...")
            broker = AlpacaBroker(
                api_key=app_config.broker.api_key,
                secret_key=app_config.broker.secret_key,
                paper=app_config.broker.paper,
                base_url=app_config.broker.base_url
            )

            if broker.connect():
                print("✅ Broker connection successful")
                account = broker.get_account()
                print(f"   Account equity: ${account.equity:,.2f}")
                print()
            else:
                print("❌ Broker connection failed")
                sys.exit(1)

            # Test market data (use first strategy)
            print("Testing market data...")
            from alphalive.data.market_data import MarketDataFetcher

            market_data = MarketDataFetcher(
                api_key=app_config.broker.api_key,
                secret_key=app_config.broker.secret_key
            )

            test_strategy = strategy_configs[0]  # Use first strategy for testing

            try:
                df = market_data.get_latest_bars(
                    test_strategy.ticker,
                    test_strategy.timeframe,
                    lookback_bars=50
                )
                print(f"✅ Market data OK ({len(df)} bars fetched for {test_strategy.ticker})")
                print()
            except Exception as e:
                print(f"❌ Market data error: {e}")
                sys.exit(1)

            # Test signal generation
            print("Testing signal generation...")
            from alphalive.strategy.signal_engine import SignalEngine

            signal_engine = SignalEngine(test_strategy)
            test_signal = signal_engine.generate_signal(df)
            print(f"✅ Signal generation OK")
            print(f"   Test signal: {test_signal['signal']}")
            print(f"   Warmup complete: {test_signal.get('warmup_complete', True)}")
            print()

            print("=" * 80)
            print("✅ ALL VALIDATIONS PASSED")
            print("=" * 80)
            print()
            print("Ready to run:")
            print(f"  python run.py --config {args.config}")
            print()

            sys.exit(0)

        except Exception as e:
            print(f"❌ Validation error: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)

    # --- Normal run ---
    if not paper:
        print()
        print("⚠️  ⚠️  ⚠️  WARNING ⚠️  ⚠️  ⚠️")
        print("⚠️  LIVE TRADING MODE — REAL MONEY AT RISK  ⚠️")
        print("⚠️  ⚠️  ⚠️  WARNING ⚠️  ⚠️  ⚠️")
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
            paper=paper
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
