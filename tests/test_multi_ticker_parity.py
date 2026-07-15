#!/usr/bin/env python3
"""
Multi-Ticker Signal Parity Test

Extends C1 parity verification to SPY and MSFT with real market data (2022-2023).
Tests all 7 strategies on each ticker.

Key difference from test_signal_parity.py:
- Uses REAL yfinance data (not synthetic random walk)
- momentum_breakout generates actual signals on real data (SPY: 2, MSFT: 11)
- Covers broader market regimes: trending (2023 bull), volatile (2022 bear)

Usage:
    pytest tests/test_multi_ticker_parity.py -v
"""

import sys
from pathlib import Path
from datetime import datetime

import pandas as pd
import pytest

import os

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from alphalive.strategy.signal_engine import SignalEngine
from alphalive.strategy_schema import StrategySchema

STRATEGIES = [
    {"name": "ma_crossover", "params": {"fast_period": 20, "slow_period": 50}},
    {
        "name": "rsi_mean_reversion",
        "params": {"period": 14, "oversold": 30, "overbought": 70},
    },
    {
        "name": "momentum_breakout",
        "params": {
            "lookback": 20,
            "surge_pct": 1.5,
            "atr_period": 14,
            "volume_ma_period": 20,
        },
    },
    {
        "name": "bollinger_breakout",
        "params": {
            "period": 20,
            "std_dev": 2.0,
            "confirmation_bars": 2,
            "volume_ma_period": 20,
        },
    },
    {
        "name": "vwap_reversion",
        "params": {
            "deviation_threshold": 2.0,
            "rsi_period": 14,
            "oversold": 30,
            "overbought": 70,
            "vwap_std_period": 20,
        },
    },
    {
        "name": "bollinger_rsi_combo",
        "params": {
            "bb_period": 20,
            "bb_std": 2.0,
            "rsi_period": 14,
            "rsi_oversold": 45,
            "rsi_overbought": 55,
            "exit_at_middle": True,
        },
    },
    {
        "name": "trend_adaptive_rsi",
        "params": {
            "rsi_period": 14,
            "trend_sma": 50,
            "trend_lookback": 5,
            "uptrend_buy": 45,
            "uptrend_sell": 65,
            "downtrend_buy": 35,
            "downtrend_sell": 55,
            "range_buy": 35,
            "range_sell": 65,
        },
    },
]

TICKERS = ["SPY", "MSFT"]


def load_fixture(ticker: str) -> pd.DataFrame:
    path = PROJECT_ROOT / "tests" / "fixtures" / f"{ticker.lower()}_fixture_500bars.csv"
    if not path.exists():
        pytest.skip(
            f"Fixture not found: {path}. Run tests/fixtures/generate_new_strategy_fixtures.py"
        )
    df = pd.read_csv(path)
    df.columns = df.columns.str.lower()
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def load_expected_signals(ticker: str, strategy_name: str) -> pd.DataFrame:
    path = (
        PROJECT_ROOT
        / "tests"
        / "fixtures"
        / f"expected_signals_{ticker}_{strategy_name}.csv"
    )
    if not path.exists():
        pytest.skip(f"Expected signals not found: {path}")
    return pd.read_csv(path)


def make_config(strategy_name: str, params: dict, ticker: str) -> StrategySchema:
    return StrategySchema(
        schema_version="1.0",
        strategy={"name": strategy_name, "parameters": params},
        ticker=ticker,
        timeframe="1Day",
        risk={
            "stop_loss_pct": 2.0,
            "take_profit_pct": 5.0,
            "max_position_size_pct": 10.0,
            "max_daily_loss_pct": 5.0,
            "max_open_positions": 3,
            "portfolio_max_positions": 10,
        },
        execution={"order_type": "market"},
        safety_limits={},
        metadata={
            "exported_from": "multi-ticker-parity",
            "exported_at": datetime.now().isoformat(),
            "alphalab_version": "1.0.0",
            "backtest_id": f"parity_{strategy_name}_{ticker}",
            "backtest_period": {"start": "2022-01-01", "end": "2023-12-31"},
            "performance": {
                "sharpe_ratio": 1.5,
                "sortino_ratio": 2.0,
                "total_return_pct": 25.0,
                "max_drawdown_pct": 10.0,
                "win_rate_pct": 55.0,
                "profit_factor": 1.8,
                "total_trades": 100,
                "calmar_ratio": 2.5,
            },
        },
    )


def run_parity_check(ticker: str, strategy_name: str, params: dict) -> dict:
    df = load_fixture(ticker)
    expected = load_expected_signals(ticker, strategy_name)
    config = make_config(strategy_name, params, ticker)
    engine = SignalEngine(config)

    mismatches = []
    signal_count = 0

    for i in range(len(df)):
        result = engine.generate_signal(df.iloc[: i + 1].copy())
        actual = result["signal"]

        row = expected[expected["bar_index"] == i]
        if row.empty:
            continue

        expected_signal = row.iloc[0]["signal"]
        if expected_signal != "HOLD":
            signal_count += 1
        if actual != expected_signal:
            mismatches.append({"bar": i, "expected": expected_signal, "actual": actual})

    return {
        "mismatches": len(mismatches),
        "signals": signal_count,
        "details": mismatches[:3],
    }


# Parametrize over all ticker × strategy combinations
@pytest.mark.parametrize("ticker", TICKERS)
@pytest.mark.parametrize("strat", STRATEGIES, ids=[s["name"] for s in STRATEGIES])
def test_signal_parity(ticker, strat, monkeypatch):
    """All strategies must produce identical signals on real SPY and MSFT data.

    Bear-market filter handling differs by oracle provenance:
    - vwap_reversion's expected CSVs are generated from ALPHALAB (the true
      oracle, regenerated 2026-07-10 after the rolling-VWAP + stateful
      rewrite), which has no bear filter - so the filter is disabled.
    - The other strategies' expected CSVs are historical snapshots taken
      WITH AlphaLive's filter active (self-referential - they verify
      no-regression, not true AlphaLab parity). Regenerating them from
      AlphaLab is tracked as follow-up work; until then the filter stays
      on to match how they were captured.
    """
    monkeypatch.setenv(
        "ENABLE_BEAR_MARKET_FILTER",
        "false" if strat["name"] == "vwap_reversion" else "true",
    )
    result = run_parity_check(ticker, strat["name"], strat["params"])
    assert result["mismatches"] == 0, (
        f"{strat['name']} on {ticker}: {result['mismatches']} mismatches. "
        f"First 3: {result['details']}"
    )


@pytest.mark.parametrize("ticker", TICKERS)
def test_momentum_breakout_generates_signals_on_real_data(ticker):
    """momentum_breakout must generate at least 1 signal on real market data."""
    df = load_fixture(ticker)
    expected = load_expected_signals(ticker, "momentum_breakout")
    non_hold = (expected["signal"] != "HOLD").sum()
    assert non_hold >= 1, (
        f"momentum_breakout generated 0 signals on real {ticker} data - "
        "check strategy parameters or fixture quality"
    )
