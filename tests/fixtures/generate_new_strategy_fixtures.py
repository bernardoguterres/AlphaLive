#!/usr/bin/env python3
"""
Generate expected signal fixtures for bollinger_rsi_combo and trend_adaptive_rsi.

Uses AlphaLab's backtest engine as the source of truth (stateful signal generation).
These fixtures are used by test_signal_parity.py to verify AlphaLive matches AlphaLab.
"""

import sys
import pandas as pd
from pathlib import Path

ALPHALIVE_ROOT = Path(__file__).parent.parent.parent
ALPHALAB_BACKEND = ALPHALIVE_ROOT.parent / "AlphaLab" / "backend"
FIXTURES_DIR = Path(__file__).parent

sys.path.insert(0, str(ALPHALAB_BACKEND))

from src.data.processor import FeatureEngineer
from src.strategies.implementations.bollinger_rsi_combo import BollingerRSICombo
from src.strategies.implementations.trend_adaptive_rsi import TrendAdaptiveRSI

STRATEGIES = [
    {
        "name": "bollinger_rsi_combo",
        "class": BollingerRSICombo,
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
        "class": TrendAdaptiveRSI,
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


def load_fixture() -> pd.DataFrame:
    fixture_path = FIXTURES_DIR / "aapl_fixture_500bars.csv"
    df = pd.read_csv(fixture_path)
    df.columns = df.columns.str.lower()
    return df.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    })


def main():
    print("=" * 60)
    print("Generating AlphaLab expected signal fixtures")
    print("Source of truth: AlphaLab stateful backtest engine")
    print("=" * 60)

    raw = load_fixture()
    print(f"\nLoaded fixture: {len(raw)} bars")

    engineer = FeatureEngineer()
    data = engineer.process(raw)
    print(f"Features added: {data.shape[1]} columns\n")

    SIGNAL_MAP = {1: "BUY", -1: "SELL", 0: "HOLD"}

    for strat in STRATEGIES:
        name = strat["name"]
        print(f"Generating signals for {name}...", end=" ", flush=True)

        strategy = strat["class"](strat["params"])
        signals_df = strategy.generate_signals(data)

        rows = []
        for i, (_, row) in enumerate(signals_df.iterrows()):
            rows.append({
                "bar_index": i,
                "signal": SIGNAL_MAP[row["signal"]],
            })

        out = pd.DataFrame(rows)
        non_hold = (out["signal"] != "HOLD").sum()
        buys = (out["signal"] == "BUY").sum()
        sells = (out["signal"] == "SELL").sum()

        output_path = FIXTURES_DIR / f"expected_signals_{name}.csv"
        out.to_csv(output_path, index=False)

        print(f"✓ {non_hold} signals ({buys} BUY, {sells} SELL) → {output_path.name}")

    print("\n✅ Done")


if __name__ == "__main__":
    main()
