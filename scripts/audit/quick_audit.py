#!/usr/bin/env python3
"""
Quick Strategy Audit - Tests all 5 strategies on representative stocks
AAPL (individual stock) and SPY (ETF) across pre/post COVID
Total: 5 strategies × 2 stocks × 2 periods = 20 tests (~20-30 minutes)
"""

import os
import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# All strategies
STRATEGIES = {
    "ma_crossover": {
        "name": "ma_crossover",
        "parameters": {"fast_period": 10, "slow_period": 20},
        "description": "MA crossover"
    },
    "rsi_mean_reversion": {
        "name": "rsi_mean_reversion",
        "parameters": {"period": 14, "oversold": 30, "overbought": 70},
        "description": "RSI mean reversion"
    },
    "momentum_breakout": {
        "name": "momentum_breakout",
        "parameters": {"lookback": 20, "surge_pct": 1.5, "atr_period": 14, "volume_ma_period": 20},
        "description": "Momentum breakout"
    },
    "bollinger_breakout": {
        "name": "bollinger_breakout",
        "parameters": {"period": 20, "std_dev": 2.0, "confirmation_bars": 2, "volume_ma_period": 20},
        "description": "Bollinger breakout"
    },
    "vwap_reversion": {
        "name": "vwap_reversion",
        "parameters": {"deviation_threshold": 2.0, "rsi_period": 14, "oversold": 30, "overbought": 70, "vwap_std_period": 20},
        "description": "VWAP reversion"
    }
}

# Representative stocks: AAPL (individual) and SPY (ETF)
STOCKS = ["AAPL", "SPY"]

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
        "max_position_size_pct": 30.0,  # OPTIMIZED: 30% for best risk/reward
        "max_daily_loss_pct": 6.0,  # Increased to match larger positions
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
    print(f"\nTesting: {strategy_name} on {ticker} ({period_data['name']})")

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
        print(f"{status} {ticker}: {trades} trades, {win_rate:.1f}% win, ${total_pnl:,.2f} P&L")

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
    print("QUICK STRATEGY AUDIT")
    print("="*80)
    print(f"Strategies: {len(STRATEGIES)} (all 5)")
    print(f"Stocks: {len(STOCKS)} (AAPL, SPY)")
    print(f"Periods: {len(PERIODS)} (Pre/Post COVID)")
    print(f"Total tests: {len(STRATEGIES) * len(STOCKS) * len(PERIODS)}")
    print("="*80)

    if not os.environ.get("ALPACA_API_KEY"):
        print("\n❌ Error: ALPACA_API_KEY not set")
        sys.exit(1)

    print(f"\nStarting at {datetime.now().strftime('%H:%M:%S')}")
    print("Estimated time: 20-30 minutes\n")

    all_results = []
    total_tests = len(STRATEGIES) * len(STOCKS) * len(PERIODS)
    current_test = 0

    for strategy_name in STRATEGIES.keys():
        for ticker in STOCKS:
            for period_name, period_data in PERIODS.items():
                current_test += 1
                print(f"\n[{current_test}/{total_tests}]", end=" ")
                result = run_test(strategy_name, ticker, period_name, period_data)
                all_results.append(result)

    # Generate report
    print("\n\n" + "="*80)
    print("QUICK AUDIT RESULTS")
    print("="*80)

    for strategy_name in STRATEGIES.keys():
        print(f"\n{strategy_name.upper()}")
        print("-" * 80)

        strategy_results = [r for r in all_results if r["strategy"] == strategy_name]

        for period_name in ["pre_covid", "post_covid"]:
            period_results = [r for r in strategy_results if r["period"] == period_name]
            period_name_display = "Pre-COVID (2015-2019)" if period_name == "pre_covid" else "Post-COVID (2022-2024)"

            print(f"\n{period_name_display}:")
            for r in period_results:
                status = "✅ PROFIT" if r["total_pnl"] > 0 else "❌ LOSS  "
                print(f"  {status} | {r['ticker']:4} | {r['trades']:2} trades | {r['win_rate']:5.1f}% win | ${r['total_pnl']:>9,.2f}")

        # Summary
        pre = [r for r in strategy_results if r["period"] == "pre_covid"]
        post = [r for r in strategy_results if r["period"] == "post_covid"]
        pre_pnl = sum(r["total_pnl"] for r in pre)
        post_pnl = sum(r["total_pnl"] for r in post)

        print(f"\nSummary: Pre ${pre_pnl:,.2f} | Post ${post_pnl:,.2f}", end=" | ")

        if pre_pnl > 0 and post_pnl > 0:
            print("✅ CONSISTENTLY PROFITABLE")
        elif pre_pnl > 0 and post_pnl < 0:
            print("⚠️  BULL MARKET ONLY")
        elif pre_pnl < 0 and post_pnl > 0:
            print("⚠️  VOLATILE MARKET ONLY")
        else:
            print("❌ NOT PROFITABLE")

    # Overall recommendations
    print("\n\n" + "="*80)
    print("RECOMMENDATIONS")
    print("="*80)

    consistent = []
    for strategy_name in STRATEGIES.keys():
        pre_pnl = sum(r["total_pnl"] for r in all_results if r["strategy"] == strategy_name and r["period"] == "pre_covid")
        post_pnl = sum(r["total_pnl"] for r in all_results if r["strategy"] == strategy_name and r["period"] == "post_covid")

        if pre_pnl > 0 and post_pnl > 0:
            consistent.append((strategy_name, pre_pnl, post_pnl, pre_pnl + post_pnl))

    if consistent:
        print("\n✅ CONSISTENTLY PROFITABLE (Use these):")
        for strategy, pre, post, total in sorted(consistent, key=lambda x: x[3], reverse=True):
            print(f"   {strategy:20} | Pre: ${pre:>8,.2f} | Post: ${post:>8,.2f} | Total: ${total:>9,.2f}")

    market_dep = []
    for strategy_name in STRATEGIES.keys():
        pre_pnl = sum(r["total_pnl"] for r in all_results if r["strategy"] == strategy_name and r["period"] == "pre_covid")
        post_pnl = sum(r["total_pnl"] for r in all_results if r["strategy"] == strategy_name and r["period"] == "post_covid")

        if (pre_pnl > 0 and post_pnl < 0) or (pre_pnl < 0 and post_pnl > 0):
            market_dep.append((strategy_name, pre_pnl, post_pnl))

    if market_dep:
        print("\n⚠️  MARKET-DEPENDENT (Use with caution):")
        for strategy, pre, post in market_dep:
            print(f"   {strategy:20} | Pre: ${pre:>8,.2f} | Post: ${post:>8,.2f}")

    unprofitable = []
    for strategy_name in STRATEGIES.keys():
        pre_pnl = sum(r["total_pnl"] for r in all_results if r["strategy"] == strategy_name and r["period"] == "pre_covid")
        post_pnl = sum(r["total_pnl"] for r in all_results if r["strategy"] == strategy_name and r["period"] == "post_covid")

        if pre_pnl < 0 and post_pnl < 0:
            unprofitable.append((strategy_name, pre_pnl, post_pnl, pre_pnl + post_pnl))

    if unprofitable:
        print("\n❌ NOT PROFITABLE (Avoid these):")
        for strategy, pre, post, total in unprofitable:
            print(f"   {strategy:20} | Pre: ${pre:>8,.2f} | Post: ${post:>8,.2f} | Total: ${total:>9,.2f}")

    print(f"\n\nCompleted at {datetime.now().strftime('%H:%M:%S')}")
    print("="*80)


if __name__ == "__main__":
    main()
