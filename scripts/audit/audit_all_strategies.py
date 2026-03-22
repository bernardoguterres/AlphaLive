#!/usr/bin/env python3
"""
Comprehensive Strategy Audit
Tests all 5 AlphaLab strategies across pre-COVID and post-COVID periods
"""

import os
import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# All strategies to test
STRATEGIES = {
    "ma_crossover": {
        "name": "ma_crossover",
        "parameters": {
            "fast_period": 10,
            "slow_period": 20
        },
        "description": "Fast/slow MA crossover"
    },
    "rsi_mean_reversion": {
        "name": "rsi_mean_reversion",
        "parameters": {
            "period": 14,
            "oversold": 30,
            "overbought": 70
        },
        "description": "RSI mean reversion"
    },
    "momentum_breakout": {
        "name": "momentum_breakout",
        "parameters": {
            "lookback": 20,
            "surge_pct": 1.5,
            "atr_period": 14,
            "volume_ma_period": 20
        },
        "description": "Momentum breakout with volume confirmation"
    },
    "bollinger_breakout": {
        "name": "bollinger_breakout",
        "parameters": {
            "period": 20,
            "std_dev": 2.0,
            "confirmation_bars": 2,
            "volume_ma_period": 20
        },
        "description": "Bollinger Band breakout"
    },
    "vwap_reversion": {
        "name": "vwap_reversion",
        "parameters": {
            "deviation_threshold": 2.0,
            "rsi_period": 14,
            "oversold": 30,
            "overbought": 70,
            "vwap_std_period": 20
        },
        "description": "VWAP reversion with RSI confirmation"
    }
}

# Stocks to test
STOCKS = ["AAPL", "MSFT", "GOOGL", "AMZN", "SPY", "QQQ"]

# Test periods
PERIODS = {
    "pre_covid": {
        "start": "2015-01-01",
        "end": "2019-12-31",
        "name": "Pre-COVID (2015-2019)"
    },
    "post_covid": {
        "start": "2022-01-01",
        "end": "2024-12-31",
        "name": "Post-COVID (2022-2024)"
    }
}

# Base config template
BASE_CONFIG = {
    "schema_version": "1.0",
    "strategy": {},  # Will be filled per strategy
    "ticker": "",  # Will be filled per stock
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
        "backtest_id": "audit",
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


def create_config(strategy_name, strategy_data, ticker):
    """Create a strategy config for a specific stock."""
    config = BASE_CONFIG.copy()
    config["strategy"] = strategy_data.copy()
    config["ticker"] = ticker
    config["risk"] = BASE_CONFIG["risk"].copy()
    config["execution"] = BASE_CONFIG["execution"].copy()
    config["safety_limits"] = BASE_CONFIG["safety_limits"].copy()
    config["metadata"] = BASE_CONFIG["metadata"].copy()

    # Save to temp file
    config_path = f"configs/temp_{strategy_name}_{ticker}.json"
    Path("configs").mkdir(exist_ok=True)

    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)

    return config_path


def run_test(strategy_name, ticker, period_name, period_data):
    """Run a single test."""
    print(f"\n{'='*80}")
    print(f"Testing: {strategy_name} on {ticker} ({period_data['name']})")
    print(f"{'='*80}")

    # Create config
    strategy_data = STRATEGIES[strategy_name]
    config_path = create_config(strategy_name, strategy_data, ticker)

    # Run replay
    cmd = [
        sys.executable, "run.py",
        "--config", config_path,
        "--replay-mode",
        "--replay-start", period_data["start"],
        "--replay-end", period_data["end"],
        "--dry-run"
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5 minutes max
        )

        # Parse results
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

        return {
            "strategy": strategy_name,
            "ticker": ticker,
            "period": period_name,
            "trades": trades,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "success": result.returncode == 0
        }

    except subprocess.TimeoutExpired:
        print(f"⚠️  Timeout")
        return {
            "strategy": strategy_name,
            "ticker": ticker,
            "period": period_name,
            "trades": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "success": False
        }
    except Exception as e:
        print(f"❌ Error: {e}")
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
        # Cleanup
        if os.path.exists(config_path):
            os.remove(config_path)


def main():
    """Run comprehensive audit."""
    print("="*80)
    print("COMPREHENSIVE STRATEGY AUDIT")
    print("="*80)
    print(f"Strategies: {len(STRATEGIES)}")
    print(f"Stocks: {len(STOCKS)}")
    print(f"Periods: {len(PERIODS)}")
    print(f"Total tests: {len(STRATEGIES) * len(STOCKS) * len(PERIODS)}")
    print("="*80)

    # Check environment
    if not os.environ.get("ALPACA_API_KEY"):
        print("\n❌ Error: ALPACA_API_KEY not set")
        sys.exit(1)

    if not os.environ.get("ALPACA_SECRET_KEY"):
        print("\n❌ Error: ALPACA_SECRET_KEY not set")
        sys.exit(1)

    print("\n✓ Environment configured")
    print(f"\nStarting audit at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("This will take approximately 30-60 minutes...\n")

    # Run all tests
    all_results = []
    total_tests = len(STRATEGIES) * len(STOCKS) * len(PERIODS)
    current_test = 0

    for strategy_name in STRATEGIES.keys():
        for ticker in STOCKS:
            for period_name, period_data in PERIODS.items():
                current_test += 1
                print(f"\n[{current_test}/{total_tests}] Testing {strategy_name} on {ticker} ({period_data['name']})")

                result = run_test(strategy_name, ticker, period_name, period_data)
                all_results.append(result)

                # Quick status
                if result["success"]:
                    status = "✓" if result["total_pnl"] > 0 else "✗"
                    print(f"{status} {ticker}: {result['trades']} trades, {result['win_rate']:.1f}% win rate, ${result['total_pnl']:,.2f} P&L")
                else:
                    print(f"✗ {ticker}: Failed")

    # Generate report
    generate_report(all_results)

    print(f"\n\n✅ Audit complete at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Results saved to COMPREHENSIVE_AUDIT_REPORT.md")


def generate_report(results):
    """Generate comprehensive audit report."""
    report = []

    report.append("# Comprehensive Strategy Audit Report\n")
    report.append(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    report.append(f"**Total Tests:** {len(results)}\n")
    report.append("\n---\n")

    # Summary by strategy
    report.append("\n## Summary by Strategy\n")

    for strategy_name in STRATEGIES.keys():
        strategy_results = [r for r in results if r["strategy"] == strategy_name]

        pre_covid = [r for r in strategy_results if r["period"] == "pre_covid"]
        post_covid = [r for r in strategy_results if r["period"] == "post_covid"]

        pre_total_pnl = sum(r["total_pnl"] for r in pre_covid)
        post_total_pnl = sum(r["total_pnl"] for r in post_covid)

        pre_profitable = len([r for r in pre_covid if r["total_pnl"] > 0])
        post_profitable = len([r for r in post_covid if r["total_pnl"] > 0])

        report.append(f"\n### {strategy_name}\n")
        report.append(f"**Pre-COVID (2015-2019):**\n")
        report.append(f"- Total P&L: ${pre_total_pnl:,.2f}\n")
        report.append(f"- Profitable stocks: {pre_profitable}/{len(STOCKS)}\n")
        report.append(f"\n**Post-COVID (2022-2024):**\n")
        report.append(f"- Total P&L: ${post_total_pnl:,.2f}\n")
        report.append(f"- Profitable stocks: {post_profitable}/{len(STOCKS)}\n")

        # Verdict
        if pre_total_pnl > 0 and post_total_pnl > 0:
            report.append(f"\n**Verdict:** ✅ CONSISTENT PROFIT (both periods)\n")
        elif pre_total_pnl > 0 and post_total_pnl < 0:
            report.append(f"\n**Verdict:** ⚠️ MARKET DEPENDENT (pre-COVID only)\n")
        elif pre_total_pnl < 0 and post_total_pnl > 0:
            report.append(f"\n**Verdict:** ⚠️ MARKET DEPENDENT (post-COVID only)\n")
        else:
            report.append(f"\n**Verdict:** ❌ NOT PROFITABLE\n")

    # Detailed results
    report.append("\n---\n")
    report.append("\n## Detailed Results\n")

    for strategy_name in STRATEGIES.keys():
        report.append(f"\n### {strategy_name} - Detailed Breakdown\n")

        for period_name, period_data in PERIODS.items():
            report.append(f"\n**{period_data['name']}:**\n")
            report.append("\n| Ticker | Trades | Win Rate | Total P&L | Status |\n")
            report.append("|--------|--------|----------|-----------|--------|\n")

            period_results = [r for r in results if r["strategy"] == strategy_name and r["period"] == period_name]
            period_results.sort(key=lambda x: x["total_pnl"], reverse=True)

            for r in period_results:
                status = "✅" if r["total_pnl"] > 0 else "❌"
                report.append(f"| {r['ticker']} | {r['trades']} | {r['win_rate']:.1f}% | ${r['total_pnl']:,.2f} | {status} |\n")

    # Best performers
    report.append("\n---\n")
    report.append("\n## Best Performers\n")

    # Pre-COVID best
    pre_covid_results = [r for r in results if r["period"] == "pre_covid" and r["success"]]
    if pre_covid_results:
        best_pre = max(pre_covid_results, key=lambda x: x["total_pnl"])
        report.append(f"\n**Best Pre-COVID:** {best_pre['strategy']} on {best_pre['ticker']}\n")
        report.append(f"- P&L: ${best_pre['total_pnl']:,.2f}\n")
        report.append(f"- Trades: {best_pre['trades']}\n")
        report.append(f"- Win Rate: {best_pre['win_rate']:.1f}%\n")

    # Post-COVID best
    post_covid_results = [r for r in results if r["period"] == "post_covid" and r["success"]]
    if post_covid_results:
        best_post = max(post_covid_results, key=lambda x: x["total_pnl"])
        report.append(f"\n**Best Post-COVID:** {best_post['strategy']} on {best_post['ticker']}\n")
        report.append(f"- P&L: ${best_post['total_pnl']:,.2f}\n")
        report.append(f"- Trades: {best_post['trades']}\n")
        report.append(f"- Win Rate: {best_post['win_rate']:.1f}%\n")

    # Worst performers
    report.append("\n## Worst Performers\n")

    if pre_covid_results:
        worst_pre = min(pre_covid_results, key=lambda x: x["total_pnl"])
        report.append(f"\n**Worst Pre-COVID:** {worst_pre['strategy']} on {worst_pre['ticker']}\n")
        report.append(f"- P&L: ${worst_pre['total_pnl']:,.2f}\n")
        report.append(f"- Trades: {worst_pre['trades']}\n")
        report.append(f"- Win Rate: {worst_pre['win_rate']:.1f}%\n")

    if post_covid_results:
        worst_post = min(post_covid_results, key=lambda x: x["total_pnl"])
        report.append(f"\n**Worst Post-COVID:** {worst_post['strategy']} on {worst_post['ticker']}\n")
        report.append(f"- P&L: ${worst_post['total_pnl']:,.2f}\n")
        report.append(f"- Trades: {worst_post['trades']}\n")
        report.append(f"- Win Rate: {worst_post['win_rate']:.1f}%\n")

    # Recommendations
    report.append("\n---\n")
    report.append("\n## Recommendations\n")

    # Find consistently profitable strategies
    consistent = []
    for strategy_name in STRATEGIES.keys():
        pre_pnl = sum(r["total_pnl"] for r in results if r["strategy"] == strategy_name and r["period"] == "pre_covid")
        post_pnl = sum(r["total_pnl"] for r in results if r["strategy"] == strategy_name and r["period"] == "post_covid")

        if pre_pnl > 0 and post_pnl > 0:
            consistent.append((strategy_name, pre_pnl, post_pnl))

    if consistent:
        report.append("\n### ✅ Consistently Profitable Strategies (Both Periods)\n")
        for strategy_name, pre_pnl, post_pnl in sorted(consistent, key=lambda x: x[1] + x[2], reverse=True):
            total = pre_pnl + post_pnl
            report.append(f"- **{strategy_name}**: Pre-COVID ${pre_pnl:,.2f}, Post-COVID ${post_pnl:,.2f}, Total ${total:,.2f}\n")
    else:
        report.append("\n### ⚠️ No Consistently Profitable Strategies\n")
        report.append("All strategies showed losses in at least one period.\n")

    # Market-dependent strategies
    market_dep = []
    for strategy_name in STRATEGIES.keys():
        pre_pnl = sum(r["total_pnl"] for r in results if r["strategy"] == strategy_name and r["period"] == "pre_covid")
        post_pnl = sum(r["total_pnl"] for r in results if r["strategy"] == strategy_name and r["period"] == "post_covid")

        if (pre_pnl > 0 and post_pnl < 0) or (pre_pnl < 0 and post_pnl > 0):
            market_dep.append((strategy_name, pre_pnl, post_pnl))

    if market_dep:
        report.append("\n### ⚠️ Market-Dependent Strategies\n")
        for strategy_name, pre_pnl, post_pnl in market_dep:
            report.append(f"- **{strategy_name}**: Pre-COVID ${pre_pnl:,.2f}, Post-COVID ${post_pnl:,.2f}\n")

    # Unprofitable strategies
    unprofitable = []
    for strategy_name in STRATEGIES.keys():
        pre_pnl = sum(r["total_pnl"] for r in results if r["strategy"] == strategy_name and r["period"] == "pre_covid")
        post_pnl = sum(r["total_pnl"] for r in results if r["strategy"] == strategy_name and r["period"] == "post_covid")

        if pre_pnl < 0 and post_pnl < 0:
            unprofitable.append((strategy_name, pre_pnl, post_pnl))

    if unprofitable:
        report.append("\n### ❌ Not Profitable (Both Periods)\n")
        for strategy_name, pre_pnl, post_pnl in unprofitable:
            total = pre_pnl + post_pnl
            report.append(f"- **{strategy_name}**: Pre-COVID ${pre_pnl:,.2f}, Post-COVID ${post_pnl:,.2f}, Total ${total:,.2f}\n")

    # Save report
    with open("COMPREHENSIVE_AUDIT_REPORT.md", "w") as f:
        f.write("".join(report))


if __name__ == "__main__":
    main()
