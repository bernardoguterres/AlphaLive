#!/usr/bin/env python3
"""
Full 6-Stock Audit - Test 3 profitable strategies on all 6 stocks
Total: 3 strategies × 6 stocks × 2 periods = 36 tests
"""

import os
import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# Only profitable strategies
STRATEGIES = {
    "rsi_mean_reversion": {
        "name": "rsi_mean_reversion",
        "parameters": {"period": 14, "oversold": 30, "overbought": 70},
        "description": "RSI mean reversion"
    },
    "ma_crossover": {
        "name": "ma_crossover",
        "parameters": {"fast_period": 10, "slow_period": 20},
        "description": "MA crossover"
    },
    "vwap_reversion": {
        "name": "vwap_reversion",
        "parameters": {"deviation_threshold": 2.0, "rsi_period": 14, "oversold": 30, "overbought": 70, "vwap_std_period": 20},
        "description": "VWAP reversion"
    }
}

# All 6 stocks
STOCKS = ["AAPL", "MSFT", "GOOGL", "AMZN", "SPY", "QQQ"]

# Test periods
PERIODS = {
    "pre_covid": {"start": "2015-01-01", "end": "2019-12-31", "name": "Pre-COVID"},
    "post_covid": {"start": "2022-01-01", "end": "2024-12-31", "name": "Post-COVID"}
}

BASE_CONFIG = {
    "schema_version": "1.0",
    "strategy": {},
    "ticker": "",
    "timeframe": "1Day",
    "risk": {
        "stop_loss_pct": 2.0,
        "take_profit_pct": 5.0,
        "max_position_size_pct": 30.0,  # OPTIMIZED
        "max_daily_loss_pct": 6.0,
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
        "backtest_id": "audit",
        "backtest_period": {"start": "2015-01-01", "end": "2019-12-31"},
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


def create_config(strategy_name, strategy_data, ticker):
    config = BASE_CONFIG.copy()
    config["strategy"] = strategy_data.copy()
    config["ticker"] = ticker
    config["risk"] = BASE_CONFIG["risk"].copy()
    config["execution"] = BASE_CONFIG["execution"].copy()
    config["safety_limits"] = BASE_CONFIG["safety_limits"].copy()
    config["metadata"] = BASE_CONFIG["metadata"].copy()

    config_path = f"configs/temp_{strategy_name}_{ticker}.json"
    Path("configs").mkdir(exist_ok=True)

    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)

    return config_path


def run_test(strategy_name, ticker, period_name, period_data):
    strategy_data = STRATEGIES[strategy_name]
    config_path = create_config(strategy_name, strategy_data, ticker)

    cmd = [
        sys.executable, "run.py",
        "--config", config_path,
        "--replay-mode",
        "--replay-start", period_data["start"],
        "--replay-end", period_data["end"],
        "--dry-run"
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        output = result.stdout + result.stderr

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

        status = "✓" if total_pnl > 0 else "✗"
        print(f"{status} {ticker}: {trades:2} trades | {win_rate:5.1f}% win | ${total_pnl:>10,.2f}")

        return {
            "strategy": strategy_name,
            "ticker": ticker,
            "period": period_name,
            "trades": trades,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "success": result.returncode == 0
        }
    except Exception as e:
        print(f"✗ {ticker}: Error - {e}")
        return {
            "strategy": strategy_name,
            "ticker": ticker,
            "period": period_name,
            "trades": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "success": False
        }
    finally:
        if os.path.exists(config_path):
            os.remove(config_path)


def main():
    print("="*80)
    print("FULL 6-STOCK AUDIT (30% Position Sizing)")
    print("="*80)
    print(f"Strategies: {len(STRATEGIES)} (profitable only)")
    print(f"Stocks: {len(STOCKS)} (all)")
    print(f"Periods: {len(PERIODS)} (pre/post COVID)")
    print(f"Total tests: {len(STRATEGIES) * len(STOCKS) * len(PERIODS)}")
    print("="*80)

    if not os.environ.get("ALPACA_API_KEY"):
        print("\n❌ Error: ALPACA_API_KEY not set")
        sys.exit(1)

    print(f"\nStarting at {datetime.now().strftime('%H:%M:%S')}")
    print("Estimated time: 30-40 minutes\n")

    all_results = []
    total_tests = len(STRATEGIES) * len(STOCKS) * len(PERIODS)
    current_test = 0

    for strategy_name in STRATEGIES.keys():
        print(f"\n{'='*80}")
        print(f"STRATEGY: {strategy_name.upper()}")
        print(f"{'='*80}")

        for period_name, period_data in PERIODS.items():
            print(f"\n{period_data['name']} (2015-2019):" if period_name == "pre_covid" else f"\n{period_data['name']} (2022-2024):")

            for ticker in STOCKS:
                current_test += 1
                print(f"[{current_test:2}/{total_tests}] {strategy_name:20} | {ticker:5} | ", end="")
                result = run_test(strategy_name, ticker, period_name, period_data)
                all_results.append(result)

    # Generate summary
    print("\n\n" + "="*80)
    print("FINAL RESULTS SUMMARY")
    print("="*80)

    grand_total = 0
    for strategy_name in STRATEGIES.keys():
        strategy_results = [r for r in all_results if r["strategy"] == strategy_name]

        pre = [r for r in strategy_results if r["period"] == "pre_covid"]
        post = [r for r in strategy_results if r["period"] == "post_covid"]

        pre_total = sum(r["total_pnl"] for r in pre)
        post_total = sum(r["total_pnl"] for r in post)
        strategy_total = pre_total + post_total
        grand_total += strategy_total

        print(f"\n{strategy_name.upper()}")
        print(f"  Pre-COVID:  ${pre_total:>12,.2f}")
        print(f"  Post-COVID: ${post_total:>12,.2f}")
        print(f"  Total:      ${strategy_total:>12,.2f}")

    print(f"\n{'='*80}")
    print(f"GRAND TOTAL (ALL 3 STRATEGIES, 6 STOCKS, 8 YEARS):")
    print(f"  ${grand_total:>12,.2f}")
    print(f"  Annual ROI: {(grand_total / 100000 / 8 * 100):.2f}%/year")
    print(f"{'='*80}")

    # Per-stock breakdown
    print(f"\n{'='*80}")
    print("PER-STOCK PERFORMANCE (All 3 strategies combined)")
    print("="*80)

    for ticker in STOCKS:
        ticker_results = [r for r in all_results if r["ticker"] == ticker]
        ticker_total = sum(r["total_pnl"] for r in ticker_results)
        ticker_trades = sum(r["trades"] for r in ticker_results)
        status = "✅" if ticker_total > 0 else "❌"
        print(f"{status} {ticker:5} | {ticker_trades:3} trades | ${ticker_total:>12,.2f}")

    print(f"\n{'='*80}")
    print(f"Completed at {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*80}")

    # Save detailed results
    with open("6_STOCK_AUDIT_RESULTS.json", "w") as f:
        json.dump({
            "test_date": datetime.now().isoformat(),
            "position_size": "30%",
            "capital": 100000,
            "grand_total": grand_total,
            "annual_roi": (grand_total / 100000 / 8 * 100),
            "results": all_results
        }, f, indent=2)

    print("\nDetailed results saved to: 6_STOCK_AUDIT_RESULTS.json")


if __name__ == "__main__":
    main()
