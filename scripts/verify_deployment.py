#!/usr/bin/env python3
"""
Deployment Verification Script

Runs a series of checks to verify AlphaLive is correctly configured for deployment.
Run this before deploying to Railway to catch configuration errors early.

Usage:
    python scripts/verify_deployment.py

Exit Codes:
    0: All checks passed, safe to deploy
    1: One or more checks failed
"""

import os
import sys
import json
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from alphalive.config import load_env, load_config_path, validate_all
from alphalive.strategy_schema import StrategySchema


def check_env_vars():
    """Verify required environment variables are set."""
    print("=" * 60)
    print("Checking Environment Variables")
    print("=" * 60)

    required_vars = {
        "ALPACA_API_KEY": "Alpaca API key",
        "ALPACA_SECRET_KEY": "Alpaca secret key",
    }

    optional_vars = {
        "TELEGRAM_BOT_TOKEN": "Telegram bot token",
        "TELEGRAM_CHAT_ID": "Telegram chat ID",
        "STRATEGY_CONFIG": "Strategy config path",
        "LOG_LEVEL": "Logging level",
        "ALPACA_PAPER": "Paper trading mode",
    }

    issues = []

    # Check required
    for var, desc in required_vars.items():
        if not os.getenv(var):
            print(f"✗ MISSING: {var} ({desc})")
            issues.append(f"Missing required env var: {var}")
        else:
            print(f"✓ {var}: set")

    # Check optional
    for var, desc in optional_vars.items():
        if os.getenv(var):
            print(f"✓ {var}: set")
        else:
            print(f"  {var}: not set (optional)")

    return issues


def check_strategy_configs():
    """Verify strategy JSON files are valid."""
    print("\n" + "=" * 60)
    print("Checking Strategy Configurations")
    print("=" * 60)

    issues = []

    # Get strategy path from env or default
    config_path = os.getenv("STRATEGY_CONFIG", "configs/")

    if not os.path.exists(config_path):
        print(f"✗ Strategy config not found: {config_path}")
        issues.append(f"Strategy config not found: {config_path}")
        return issues

    try:
        # Load all strategies
        strategies = load_config_path(config_path)

        if not strategies:
            print("✗ No strategies loaded")
            issues.append("No strategies found")
            return issues

        print(f"✓ Loaded {len(strategies)} strategy(ies)")

        # Validate each strategy
        for i, strategy in enumerate(strategies, 1):
            print(f"\n  Strategy {i}: {strategy.strategy.name}")
            print(f"    Ticker: {strategy.ticker}")
            print(f"    Timeframe: {strategy.timeframe}")
            print(f"    Stop Loss: {strategy.risk.stop_loss_pct}%")
            print(f"    Max Position Size: {strategy.risk.max_position_size_pct}%")
            print(f"    Max Trades/Day: {strategy.safety_limits.max_trades_per_day}")

            # Check for risky configurations
            if strategy.risk.stop_loss_pct > 10:
                print(f"    ⚠️  WARNING: Wide stop loss ({strategy.risk.stop_loss_pct}%)")

            if strategy.risk.max_position_size_pct > 20:
                print(f"    ⚠️  WARNING: Large position size ({strategy.risk.max_position_size_pct}%)")

            if strategy.safety_limits.max_trades_per_day > 50:
                print(f"    ⚠️  WARNING: High trade frequency ({strategy.safety_limits.max_trades_per_day}/day)")

        print(f"\n✓ All {len(strategies)} strategies validated successfully")

    except Exception as e:
        print(f"✗ Strategy validation failed: {e}")
        issues.append(f"Strategy validation error: {e}")

    return issues


def check_dependencies():
    """Verify required Python packages are installed."""
    print("\n" + "=" * 60)
    print("Checking Dependencies")
    print("=" * 60)

    issues = []

    required_packages = [
        "alpaca",
        "pandas",
        "numpy",
        "ta",
        "httpx",
        "pydantic",
        "dotenv",  # python-dotenv package imports as "dotenv"
    ]

    for package in required_packages:
        try:
            __import__(package.replace("-", "_"))
            print(f"✓ {package}: installed")
        except ImportError:
            print(f"✗ {package}: NOT installed")
            issues.append(f"Missing package: {package}")

    return issues


def check_security():
    """Run basic security checks."""
    print("\n" + "=" * 60)
    print("Checking Security")
    print("=" * 60)

    issues = []

    # Check .env is gitignored
    gitignore_path = Path(__file__).parent.parent / ".gitignore"
    if gitignore_path.exists():
        with open(gitignore_path) as f:
            gitignore = f.read()
            if ".env" in gitignore:
                print("✓ .env is in .gitignore")
            else:
                print("✗ .env NOT in .gitignore")
                issues.append(".env not in .gitignore")
    else:
        print("  .gitignore not found")

    # Check no .env file in git
    try:
        import subprocess
        result = subprocess.run(
            ["git", "ls-files", ".env"],
            cwd=Path(__file__).parent.parent,
            capture_output=True,
            text=True
        )
        if result.stdout.strip():
            print("✗ .env is tracked by git!")
            issues.append(".env is tracked by git")
        else:
            print("✓ .env not tracked by git")
    except Exception:
        print("  Could not check git status")

    # Check HEALTH_SECRET is set for production
    if not os.getenv("HEALTH_SECRET"):
        print("⚠️  WARNING: HEALTH_SECRET not set (health endpoint will be disabled)")
    else:
        print("✓ HEALTH_SECRET is set")

    return issues


def check_files():
    """Verify required files exist."""
    print("\n" + "=" * 60)
    print("Checking Required Files")
    print("=" * 60)

    issues = []

    root = Path(__file__).parent.parent
    required_files = [
        "run.py",
        "requirements.txt",
        "Dockerfile",
        "Procfile",
        "railway.toml",
        "CLAUDE.md",
        "README.md",
        "SETUP.md",
        "alphalive/main.py",
        "alphalive/config.py",
        "alphalive/strategy_schema.py",
    ]

    for file_path in required_files:
        full_path = root / file_path
        if full_path.exists():
            print(f"✓ {file_path}")
        else:
            print(f"✗ {file_path}: NOT FOUND")
            issues.append(f"Missing file: {file_path}")

    return issues


def main():
    """Run all verification checks."""
    print("\n" + "=" * 60)
    print("AlphaLive Deployment Verification")
    print("=" * 60 + "\n")

    all_issues = []

    # Run all checks
    all_issues.extend(check_files())
    all_issues.extend(check_env_vars())
    all_issues.extend(check_dependencies())
    all_issues.extend(check_strategy_configs())
    all_issues.extend(check_security())

    # Final summary
    print("\n" + "=" * 60)
    print("VERIFICATION SUMMARY")
    print("=" * 60)

    if not all_issues:
        print("✅ All checks passed!")
        print("   Safe to deploy to Railway.")
        return 0
    else:
        print(f"❌ {len(all_issues)} issue(s) found:\n")
        for i, issue in enumerate(all_issues, 1):
            print(f"  {i}. {issue}")
        print("\n⚠️  Fix issues above before deploying.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
