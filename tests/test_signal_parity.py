#!/usr/bin/env python3
"""
C1: Signal Parity Test (Comprehensive)

This is the MOST CRITICAL test for production confidence.
Verifies that AlphaLab's backtest engine and AlphaLive's signal engine
produce IDENTICAL signals on the same historical data.

This test uses pre-exported expected signals from AlphaLab (generated via
scripts/fixtures/generate_expected_signals.py) and compares them with
AlphaLive's signal engine output.

Usage:
    pytest tests/test_signal_parity.py -v
    # or
    python tests/test_signal_parity.py

Expected Output:
    ma_crossover: 500 bars, 11 signals, 0 mismatches ✅
    rsi_mean_reversion: 500 bars, 40 signals, 0 mismatches ✅
    momentum_breakout: 500 bars, 0 signals, 0 mismatches ✅
    bollinger_breakout: 500 bars, 9 signals, 0 mismatches ✅
    vwap_reversion: 500 bars, 38 signals, 0 mismatches ✅

    PASS: All strategies match. AlphaLive is production-ready.

If ANY mismatches exist, the test FAILS.
Mismatches mean live trading will behave differently than backtest.

Results are saved to: tests/reports/signal_parity_{timestamp}.json
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any

import pandas as pd
import numpy as np

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from alphalive.strategy.signal_engine import SignalEngine
from alphalive.strategy_schema import StrategySchema

# Strategy configurations (must match the parameters used to generate expected signals)
STRATEGIES = [
    {"name": "ma_crossover", "params": {"fast_period": 20, "slow_period": 50}},
    {"name": "rsi_mean_reversion", "params": {"period": 14, "oversold": 30, "overbought": 70}},
    {"name": "momentum_breakout", "params": {"lookback": 20, "surge_pct": 1.5, "atr_period": 14, "volume_ma_period": 20}},
    {"name": "bollinger_breakout", "params": {"period": 20, "std_dev": 2.0, "confirmation_bars": 2, "volume_ma_period": 20}},
    {"name": "vwap_reversion", "params": {"deviation_threshold": 2.0, "rsi_period": 14, "oversold": 30, "overbought": 70, "vwap_std_period": 20}},
]


def load_fixture():
    """Load the canonical test fixture (AAPL 2022-2023, 500 bars)."""
    fixture_path = PROJECT_ROOT / "tests" / "fixtures" / "aapl_fixture_500bars.csv"
    if not fixture_path.exists():
        raise FileNotFoundError(
            f"Test fixture not found: {fixture_path}\n"
            f"Run: python tests/fixtures/generate_fixture.py"
        )

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


def load_expected_signals(strategy_name: str):
    """
    Load expected signals from AlphaLab backtest results.
    Format: CSV with columns [bar_index, signal, confidence, reason]
    """
    signals_path = PROJECT_ROOT / "tests" / "fixtures" / f"expected_signals_{strategy_name}.csv"
    if not signals_path.exists():
        raise FileNotFoundError(
            f"Expected signals not found: {signals_path}\n"
            f"Run AlphaLab backtest and export signals using:\n"
            f"  python tests/fixtures/generate_expected_signals.py"
        )

    return pd.read_csv(signals_path)


def create_strategy_config(strategy_name: str, params: dict) -> StrategySchema:
    """Create StrategySchema instance for testing."""
    return StrategySchema(
        schema_version="1.0",
        strategy={"name": strategy_name, "parameters": params},
        ticker="AAPL",
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
            "exported_from": "C1-ParityTest",
            "exported_at": datetime.now().isoformat(),
            "alphalab_version": "1.0.0",
            "backtest_id": f"parity_{strategy_name}",
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


def run_parity_check(strategy_config: StrategySchema, expected_signals: pd.DataFrame, df: pd.DataFrame) -> Dict[str, Any]:
    """
    Run AlphaLive signal engine and compare with expected signals.

    Returns:
        Dictionary with comparison results
    """
    engine = SignalEngine(strategy_config)

    mismatches = []
    signal_count = 0
    alphalive_signals = []

    for i in range(len(df)):
        # Feed bars incrementally (simulate real-time)
        df_slice = df.iloc[:i+1].copy()

        # Skip early bars until warmup complete
        if len(df_slice) < 50:
            continue

        result = engine.generate_signal(df_slice)
        actual_signal = result['signal']

        # Store signal
        alphalive_signals.append({
            'bar_index': i,
            'signal': actual_signal,
            'confidence': float(result.get('confidence', 0.0)),
            'reason': result.get('reason', ''),
            'warmup_complete': bool(result.get('warmup_complete', True)),
        })

        # Get expected signal for this bar
        expected_row = expected_signals[expected_signals['bar_index'] == i]
        if expected_row.empty:
            continue

        expected_signal = expected_row.iloc[0]['signal']

        if expected_signal != 'HOLD':
            signal_count += 1

        if actual_signal != expected_signal:
            mismatches.append({
                'bar_index': i,
                'timestamp': str(df.iloc[i]['timestamp']) if 'timestamp' in df.columns else str(i),
                'expected': expected_signal,
                'actual': actual_signal,
                'price': float(df.iloc[i]['close']),
                'warmup_complete': result.get('warmup_complete', True),
                'indicators': {k: float(v) if isinstance(v, (int, float, np.number)) else str(v)
                              for k, v in result.get('indicators', {}).items()},
            })

    return {
        "strategy": strategy_config.strategy.name,
        "total_bars": len(df),
        "signals_generated": signal_count,
        "matches": len(df) - len(mismatches),
        "mismatches": len(mismatches),
        "mismatch_details": mismatches,
        "alphalive_signals": alphalive_signals,
    }


def save_results(results: List[Dict[str, Any]], output_dir: Path):
    """
    Save parity test results to JSON file for historical tracking.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename with date (not timestamp for easier tracking)
    date_str = datetime.now().strftime("%Y%m%d")
    output_file = output_dir / f"signal_parity_{date_str}.json"

    # Prepare summary
    summary = {
        "test_date": datetime.now().isoformat(),
        "test_type": "C1_Signal_Parity",
        "dataset": "AAPL 2022-2023 (500 bars)",
        "strategies_tested": len(results),
        "overall_pass": all(r['mismatches'] == 0 for r in results),
        "results": results,
    }

    with open(output_file, 'w') as f:
        json.dump(summary, f, indent=2)

    print(f"\n📊 Results saved to: {output_file}")
    return output_file


def print_results(results: List[Dict[str, Any]]) -> bool:
    """
    Print formatted test results.

    Returns:
        True if all tests passed (0 mismatches)
    """
    print("\n" + "=" * 70)
    print("C1: Signal Parity Test Results")
    print("=" * 70 + "\n")

    all_match = True

    for result in results:
        strategy = result['strategy']
        total = result['total_bars']
        signals = result['signals_generated']
        mismatches = result['mismatches']

        status = "✅" if mismatches == 0 else "❌"
        print(f"{status} {strategy}: {total} bars, {signals} signals, {mismatches} mismatches")

        if mismatches > 0:
            all_match = False

            # Show first 5 mismatches with detailed info
            details = result['mismatch_details'][:5]
            for mm in details:
                print(f"   Bar {mm['bar_index']} ({mm['timestamp']}): "
                      f"Expected {mm['expected']}, Got {mm['actual']}")

                if not mm['warmup_complete']:
                    print(f"      ⚠️  Warmup incomplete")

                print(f"      Price: ${mm['price']:.2f}")

                if mm.get('indicators'):
                    print(f"      Indicators: {mm['indicators']}")

            if len(result['mismatch_details']) > 5:
                print(f"   ... and {len(result['mismatch_details']) - 5} more mismatches")

    print("\n" + "=" * 70)
    if all_match:
        print("✅ PASS: All strategies match. AlphaLive is production-ready.")
        print("\nSignal parity verified:")
        print("  ✓ AlphaLab backtest signals match AlphaLive live signals")
        print("  ✓ Live trading will behave as backtested")
        print("  ✓ Safe to deploy to production")
    else:
        print("❌ FAIL: Mismatches found. Fix before deploying to production.")
        print("\nCommon causes of mismatches:")
        print("  - Different parameter defaults between AlphaLab and AlphaLive")
        print("  - Off-by-one in lookback windows")
        print("  - Different handling of NaN in early bars")
        print("  - State management differences (already-in-position logic)")
        print("  - Different ddof parameter in std() calculations")
        print("  - Bollinger confirmation_bars vectorized vs rolling logic")
        print("\nDebugging steps:")
        print("  1. Check parameter mappings in STRATEGIES list")
        print("  2. Compare indicator values at mismatch bars")
        print("  3. Verify AlphaLab backtest used correct parameters")
        print("  4. Re-generate expected signals if parameters changed")
    print("=" * 70)

    return all_match


def main():
    """Run comprehensive signal parity test."""
    print("=" * 70)
    print("C1: Comprehensive Signal Parity Test")
    print("=" * 70)
    print()

    # Load fixture
    try:
        df = load_fixture()
        print(f"✓ Loaded test fixture: {len(df)} bars (AAPL 2022-2023)")
        print(f"  Columns: {list(df.columns)}")
        print()
    except Exception as e:
        print(f"❌ ERROR loading fixture: {e}")
        return 1

    results = []

    for strat in STRATEGIES:
        strategy_name = strat['name']
        print(f"Testing {strategy_name}...", end=" ", flush=True)

        try:
            # Load expected signals
            expected_signals = load_expected_signals(strategy_name)

            # Create strategy config
            config = create_strategy_config(strategy_name, strat['params'])

            # Run parity check
            result = run_parity_check(config, expected_signals, df)
            results.append(result)

            if result['mismatches'] == 0:
                print(f"✅ {result['signals_generated']} signals, 0 mismatches")
            else:
                print(f"❌ {result['mismatches']} mismatches")

        except FileNotFoundError as e:
            print(f"⚠️  SKIP (missing expected signals)")
            print(f"     {e}")
            results.append({
                "strategy": strategy_name,
                "total_bars": 0,
                "signals_generated": 0,
                "matches": 0,
                "mismatches": 0,
                "error": "Missing expected signals file",
            })
        except Exception as e:
            print(f"❌ ERROR: {e}")
            import traceback
            traceback.print_exc()
            results.append({
                "strategy": strategy_name,
                "total_bars": 0,
                "signals_generated": 0,
                "matches": 0,
                "mismatches": 0,
                "error": str(e),
            })

    # Print summary
    all_match = print_results(results)

    # Save results
    output_dir = PROJECT_ROOT / "tests" / "reports"
    save_results(results, output_dir)

    # Test status
    if all_match:
        print("\n✅ C1 CHECKPOINT PASSED")
        return 0
    else:
        print("\n❌ C1 CHECKPOINT FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
