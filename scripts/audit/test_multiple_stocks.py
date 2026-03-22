#!/usr/bin/env python3
"""
Test Replay Mode with Multiple Stocks

This script tests the MA Crossover strategy on multiple stocks
to see which ones perform best over the 2015-2019 period.
"""

import os
import sys
import json
import subprocess
from pathlib import Path

# Stocks to test (popular large-cap)
STOCKS_TO_TEST = [
    "AAPL",  # Apple
    "MSFT",  # Microsoft
    "GOOGL", # Google
    "AMZN",  # Amazon
    "SPY",   # S&P 500 ETF
    "QQQ",   # Nasdaq 100 ETF
]

# Test period (pre-COVID)
START_DATE = "2015-01-01"
END_DATE = "2019-12-31"

# Base strategy config (MA Crossover)
BASE_CONFIG = {
    "schema_version": "1.0",
    "strategy": {
        "name": "ma_crossover",
        "parameters": {
            "fast_period": 10,
            "slow_period": 20
        },
        "description": "Fast/slow MA crossover"
    },
    "ticker": "PLACEHOLDER",  # Will be replaced
    "timeframe": "1Day",
    "risk": {
        "stop_loss_pct": 2.0,
        "take_profit_pct": 5.0,
        "max_position_size_pct": 10.0,
        "max_daily_loss_pct": 3.0,
        "max_open_positions": 5,
        "portfolio_max_positions": 10,
        "trailing_stop_enabled": False,
        "trailing_stop_pct": 3.0,
        "commission_per_trade": 0.0
    },
    "execution": {
        "order_type": "market",
        "limit_offset_pct": 0.1,
        "cooldown_bars": 1
    },
    "safety_limits": {
        "max_trades_per_day": 20,
        "max_api_calls_per_hour": 500,
        "signal_generation_timeout_seconds": 5.0,
        "broker_degraded_mode_threshold_failures": 3
    },
    "metadata": {
        "exported_from": "AlphaLab",
        "exported_at": "2024-01-01T00:00:00Z",
        "alphalab_version": "0.2.0",
        "backtest_id": "test",
        "backtest_period": {
            "start": "2015-01-01",
            "end": "2019-12-31"
        },
        "performance": {
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "total_trades": 0,
            "calmar_ratio": 0.0
        }
    }
}


def create_config_for_stock(ticker):
    """Create a strategy config for a specific stock."""
    config = BASE_CONFIG.copy()
    config["ticker"] = ticker
    config["strategy"] = BASE_CONFIG["strategy"].copy()
    config["risk"] = BASE_CONFIG["risk"].copy()
    config["execution"] = BASE_CONFIG["execution"].copy()
    config["safety_limits"] = BASE_CONFIG["safety_limits"].copy()
    config["metadata"] = BASE_CONFIG["metadata"].copy()

    # Save to temp file
    config_path = f"configs/temp_ma_crossover_{ticker}.json"
    Path("configs").mkdir(exist_ok=True)

    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)

    return config_path


def run_replay_for_stock(ticker):
    """Run replay mode for a specific stock."""
    print(f"\n{'='*80}")
    print(f"Testing {ticker}...")
    print(f"{'='*80}\n")

    # Create config
    config_path = create_config_for_stock(ticker)

    # Run replay mode
    cmd = [
        "python", "run.py",
        "--config", config_path,
        "--replay-mode",
        "--replay-start", START_DATE,
        "--replay-end", END_DATE,
        "--dry-run"
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5 minutes max
        )

        # Extract results from output
        output = result.stdout + result.stderr

        # Parse results (look for final summary)
        trades = 0
        win_rate = 0.0
        total_pnl = 0.0

        for line in output.split('\n'):
            if "Total Trades:" in line:
                trades = int(line.split(":")[-1].strip())
            elif "Win Rate:" in line:
                win_rate = float(line.split(":")[1].replace("%", "").strip())
            elif "Total P&L:" in line:
                pnl_str = line.split("$")[-1].replace(",", "").strip()
                total_pnl = float(pnl_str)

        return {
            "ticker": ticker,
            "trades": trades,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "success": result.returncode == 0
        }

    except subprocess.TimeoutExpired:
        print(f"⚠️  Timeout testing {ticker}")
        return {
            "ticker": ticker,
            "trades": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "success": False
        }
    except Exception as e:
        print(f"❌ Error testing {ticker}: {e}")
        return {
            "ticker": ticker,
            "trades": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "success": False
        }
    finally:
        # Cleanup temp config
        if os.path.exists(config_path):
            os.remove(config_path)


def main():
    """Test multiple stocks and show results."""
    print("="*80)
    print("AlphaLive - Multi-Stock Replay Test")
    print("="*80)
    print(f"Strategy: MA Crossover (10/20)")
    print(f"Period: {START_DATE} to {END_DATE}")
    print(f"Stocks: {', '.join(STOCKS_TO_TEST)}")
    print("="*80)

    # Check environment
    if not os.environ.get("ALPACA_API_KEY"):
        print("\n❌ Error: ALPACA_API_KEY not set")
        print("   export ALPACA_API_KEY='your_key'")
        sys.exit(1)

    if not os.environ.get("ALPACA_SECRET_KEY"):
        print("\n❌ Error: ALPACA_SECRET_KEY not set")
        print("   export ALPACA_SECRET_KEY='your_secret'")
        sys.exit(1)

    print("\n✓ Environment configured")
    print("\nStarting tests... (this may take 5-10 minutes)\n")

    # Test each stock
    results = []
    for ticker in STOCKS_TO_TEST:
        result = run_replay_for_stock(ticker)
        results.append(result)

        # Show quick summary
        if result["success"]:
            print(f"✓ {ticker}: {result['trades']} trades, "
                  f"{result['win_rate']:.1f}% win rate, "
                  f"${result['total_pnl']:,.2f} P&L")
        else:
            print(f"✗ {ticker}: Failed to test")

    # Final summary
    print("\n" + "="*80)
    print("RESULTS SUMMARY (2015-2019)")
    print("="*80)
    print(f"{'Ticker':<8} {'Trades':<8} {'Win Rate':<12} {'Total P&L':<15} {'Status'}")
    print("-"*80)

    successful_results = [r for r in results if r["success"]]

    for result in sorted(successful_results, key=lambda x: x["total_pnl"], reverse=True):
        status = "✅" if result["total_pnl"] > 0 else "❌"
        print(f"{result['ticker']:<8} {result['trades']:<8} "
              f"{result['win_rate']:.1f}%{'':<8} "
              f"${result['total_pnl']:>12,.2f} {status}")

    # Show best performer
    if successful_results:
        best = max(successful_results, key=lambda x: x["total_pnl"])
        print("\n" + "="*80)
        print(f"🏆 BEST PERFORMER: {best['ticker']}")
        print(f"   Total P&L: ${best['total_pnl']:,.2f}")
        print(f"   Win Rate: {best['win_rate']:.1f}%")
        print(f"   Trades: {best['trades']}")
        print("="*80)

    print("\n💡 Recommendation:")
    profitable = [r for r in successful_results if r["total_pnl"] > 0]
    if len(profitable) >= len(successful_results) * 0.6:
        print("   ✅ MA Crossover (10/20) looks promising!")
        print("   Consider testing with Post-COVID data (2022-2024) next.")
    else:
        print("   ⚠️  Strategy underperformed on most stocks.")
        print("   Consider adjusting parameters in AlphaLab before live trading.")


if __name__ == "__main__":
    main()
