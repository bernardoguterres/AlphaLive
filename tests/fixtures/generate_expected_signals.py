#!/usr/bin/env python3
"""
Generate expected signals for mini-checkpoint testing.
This runs AlphaLive's signal engine to create baseline expected signals.

IMPORTANT: In production, these should come from AlphaLab backtests.
This is a temporary workaround to test the mini-checkpoint infrastructure.
"""

import sys
from pathlib import Path
import pandas as pd

# Add AlphaLive to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from alphalive.strategy.signal_engine import SignalEngine
from alphalive.strategy_schema import StrategySchema

# Strategy configs with default parameters
STRATEGIES = [
    {"name": "ma_crossover", "params": {"fast_period": 20, "slow_period": 50}},
    {"name": "rsi_mean_reversion", "params": {"period": 14, "oversold": 30, "overbought": 70}},
    {"name": "momentum_breakout", "params": {"lookback": 20, "surge_pct": 1.5, "atr_period": 14, "volume_ma_period": 20}},
    {"name": "bollinger_breakout", "params": {"period": 20, "std_dev": 2.0, "confirmation_bars": 2, "volume_ma_period": 20}},
    {"name": "vwap_reversion", "params": {"deviation_threshold": 2.0, "rsi_period": 14, "oversold": 30, "overbought": 70, "vwap_std_period": 20}},
]

def load_fixture():
    """Load the generated fixture."""
    fixture_path = Path("tests/fixtures/aapl_fixture_500bars.csv")
    df = pd.read_csv(fixture_path)
    df.columns = df.columns.str.lower()

    if 'timestamp' in df.columns:
        df['timestamp'] = pd.to_datetime(df['timestamp'])

    return df

def generate_signals_for_strategy(strategy_config, df):
    """Generate signals for all bars."""
    engine = SignalEngine(strategy_config)

    signals = []
    for i in range(len(df)):
        # Feed bars incrementally (simulate real-time)
        df_slice = df.iloc[:i+1].copy()

        result = engine.generate_signal(df_slice)
        signal = result['signal']

        signals.append({
            'bar_index': i,
            'signal': signal
        })

    return pd.DataFrame(signals)

def main():
    print("=" * 60)
    print("Generating Expected Signals")
    print("=" * 60)
    print()

    # Load fixture
    df = load_fixture()
    print(f"Loaded fixture: {len(df)} bars")
    print()

    for strat in STRATEGIES:
        strategy_name = strat['name']
        print(f"Generating signals for {strategy_name}...", end=" ")

        # Create strategy config
        config = StrategySchema(
            schema_version="1.0",
            strategy={"name": strategy_name, "parameters": strat['params']},
            ticker="AAPL",
            timeframe="1Day",
            risk={
                "stop_loss_pct": 2.0,
                "take_profit_pct": 5.0,
                "max_position_size_pct": 10.0,
                "max_daily_loss_pct": 5.0,
                "max_open_positions": 3,
                "portfolio_max_positions": 10
            },
            execution={
                "order_type": "market"
            },
            safety_limits={},
            metadata={
                "exported_from": "AlphaLive-MiniCheckpoint",
                "exported_at": "2024-01-01T00:00:00Z",
                "alphalab_version": "1.0.0",
                "backtest_id": f"checkpoint_{strategy_name}",
                "backtest_period": {
                    "start": "2022-01-01",
                    "end": "2023-12-31"
                },
                "performance": {
                    "sharpe_ratio": 1.5,
                    "sortino_ratio": 2.0,
                    "total_return_pct": 25.0,
                    "max_drawdown_pct": 10.0,
                    "win_rate_pct": 55.0,
                    "profit_factor": 1.8,
                    "total_trades": 100,
                    "calmar_ratio": 2.5
                }
            }
        )

        # Generate signals
        signals_df = generate_signals_for_strategy(config, df)

        # Count non-HOLD signals
        signal_count = len(signals_df[signals_df['signal'] != 'HOLD'])

        # Save to CSV
        output_path = Path(f"tests/fixtures/expected_signals_{strategy_name}.csv")
        signals_df.to_csv(output_path, index=False)

        print(f"✓ {signal_count} signals generated, saved to {output_path.name}")

    print()
    print("=" * 60)
    print("✅ All expected signals generated")
    print("=" * 60)

if __name__ == "__main__":
    main()
