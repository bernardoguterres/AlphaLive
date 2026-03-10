#!/usr/bin/env python3
"""
Mini-Checkpoint: Verify signal parity between AlphaLab and AlphaLive.
Run after A5c + B4 are complete. DO NOT PROCEED to B5 until this passes.

Usage:
    python scripts/mini_checkpoint.py

Expected output:
    ✓ ma_crossover: 47 signals, 0 mismatches
    ✓ rsi_mean_reversion: 23 signals, 0 mismatches
    ✓ momentum_breakout: 31 signals, 0 mismatches
    ✓ bollinger_breakout: 19 signals, 0 mismatches
    ✓ vwap_reversion: 28 signals, 0 mismatches

    PASS: All 5 strategies match. Proceed to B5.
"""

import sys
import pandas as pd
from pathlib import Path

# Add AlphaLive modules to path
sys.path.insert(0, str(Path(__file__).parent.parent))

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
    """Load the canonical 500-bar fixture."""
    fixture_path = Path("tests/fixtures/aapl_fixture_500bars.csv")
    if not fixture_path.exists():
        print(f"❌ ERROR: Fixture not found at {fixture_path}")
        print("   Create this file first (copy from AlphaLab).")
        sys.exit(1)

    df = pd.read_csv(fixture_path)
    # Ensure lowercase column names (AlphaLive standard)
    df.columns = df.columns.str.lower()

    # Parse timestamp/date column
    if 'timestamp' in df.columns:
        df['timestamp'] = pd.to_datetime(df['timestamp'])
    elif 'date' in df.columns:
        df['timestamp'] = pd.to_datetime(df['date'])
        df = df.drop(columns=['date'])

    return df

def load_alphalab_signals(strategy_name):
    """
    Load expected signals from AlphaLab backtest results.
    You'll need to export these from AlphaLab after running backtest on the fixture.
    Format: CSV with columns [bar_index, signal] where signal is BUY/SELL/HOLD
    """
    signals_path = Path(f"tests/fixtures/expected_signals_{strategy_name}.csv")
    if not signals_path.exists():
        print(f"⚠️  WARNING: Expected signals not found for {strategy_name}")
        print(f"   Run backtest in AlphaLab and export signals to {signals_path}")
        return None

    return pd.read_csv(signals_path)

def run_parity_check(strategy_config, expected_signals, df):
    """Run signal engine and compare with expected signals."""
    engine = SignalEngine(strategy_config)

    mismatches = []
    signal_count = 0

    for i in range(len(df)):
        # Feed bars incrementally (simulate real-time)
        df_slice = df.iloc[:i+1].copy()

        if len(df_slice) < 50:  # Skip warmup period
            continue

        result = engine.generate_signal(df_slice)
        actual_signal = result['signal']

        # Get expected signal for this bar
        expected_row = expected_signals[expected_signals['bar_index'] == i]
        if expected_row.empty:
            continue

        expected_signal = expected_row.iloc[0]['signal']

        if expected_signal != 'HOLD':
            signal_count += 1

        if actual_signal != expected_signal:
            mismatches.append({
                'bar': i,
                'timestamp': df.iloc[i]['timestamp'] if 'timestamp' in df.columns else i,
                'expected': expected_signal,
                'actual': actual_signal,
                'warmup_complete': result['warmup_complete']
            })

    return mismatches, signal_count

def main():
    print("=" * 60)
    print("Mini-Checkpoint: Signal Parity Verification")
    print("=" * 60)
    print()

    # Load fixture
    try:
        df = load_fixture()
        print(f"Loaded fixture: {len(df)} bars (AAPL 2022-2023)")
        print(f"Columns: {list(df.columns)}")
        print()
    except Exception as e:
        print(f"❌ ERROR loading fixture: {e}")
        sys.exit(1)

    all_passed = True

    for strat in STRATEGIES:
        strategy_name = strat['name']
        print(f"Testing {strategy_name}...", end=" ")

        # Load expected signals from AlphaLab
        expected_signals = load_alphalab_signals(strategy_name)
        if expected_signals is None:
            print("⚠️  SKIP (no expected signals)")
            all_passed = False
            continue

        # Create strategy config
        try:
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
        except Exception as e:
            print(f"❌ ERROR creating config: {e}")
            all_passed = False
            continue

        # Run parity check
        try:
            mismatches, signal_count = run_parity_check(config, expected_signals, df)

            if len(mismatches) == 0:
                print(f"✓ {signal_count} signals, 0 mismatches")
            else:
                print(f"❌ {len(mismatches)} mismatches")
                for mm in mismatches[:5]:  # Show first 5
                    warmup_status = "(warmup incomplete)" if not mm['warmup_complete'] else ""
                    print(f"   Bar {mm['bar']} ({mm['timestamp']}): expected {mm['expected']}, got {mm['actual']} {warmup_status}")
                if len(mismatches) > 5:
                    print(f"   ... and {len(mismatches)-5} more")
                all_passed = False
        except Exception as e:
            print(f"❌ ERROR during parity check: {e}")
            import traceback
            traceback.print_exc()
            all_passed = False

    print()
    print("=" * 60)
    if all_passed:
        print("✅ PASS: All strategies match. Proceed to B5.")
        print("=" * 60)
        sys.exit(0)
    else:
        print("❌ FAIL: Fix mismatches before proceeding to B5.")
        print("=" * 60)
        sys.exit(1)

if __name__ == "__main__":
    main()
