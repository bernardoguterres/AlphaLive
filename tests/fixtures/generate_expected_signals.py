#!/usr/bin/env python3
"""
Generate SELF-GENERATED regression fixtures for AlphaLive's own signal
engine - NOT independent cross-system parity evidence.

This runs AlphaLive's own signal engine against a local price fixture and
writes its OWN output as the "expected" baseline. That is only useful as a
determinism/regression check against a PREVIOUSLY-CAPTURED run of this same
script - it never compares against AlphaLab, so it can never demonstrate
AlphaLab/AlphaLive parity. See tests/test_multi_ticker_parity.py for actual
cross-system parity fixtures (those come from AlphaLab).

Safety (2026-09-11 pass 3): this script previously wrote to a fixed,
hardcoded path under tests/fixtures/ with no confirmation, and a single
invocation silently overwrote five tracked fixtures at once - the incident
that motivated hardening it. It now:
  - requires an explicit --output-dir (no default that points at a
    tracked/fixed fixture path);
  - requires either --strategy NAME (repeatable) or --all-strategies
    (never picks "all" by default);
  - refuses to overwrite an existing file unless --overwrite is passed;
  - prints exactly which files it is about to write before writing them.

Usage:
    python generate_expected_signals.py --output-dir /tmp/scratch --all-strategies
    python generate_expected_signals.py --output-dir /tmp/scratch --strategy ma_crossover --strategy vwap_reversion
    python generate_expected_signals.py --output-dir /tmp/scratch --all-strategies --overwrite
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

# Add AlphaLive to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from alphalive.strategy.signal_engine import SignalEngine
from alphalive.strategy_schema import StrategySchema

# Strategy configs with default parameters
STRATEGIES = {
    "ma_crossover": {"fast_period": 20, "slow_period": 50},
    "rsi_mean_reversion": {"period": 14, "oversold": 30, "overbought": 70},
    "momentum_breakout": {
        "lookback": 20,
        "surge_pct": 1.5,
        "atr_period": 14,
        "volume_ma_period": 20,
    },
    "bollinger_breakout": {
        "period": 20,
        "std_dev": 2.0,
        "confirmation_bars": 2,
        "volume_ma_period": 20,
    },
    "vwap_reversion": {
        "deviation_threshold": 2.0,
        "rsi_period": 14,
        "oversold": 30,
        "overbought": 70,
        "vwap_std_period": 20,
    },
}

DEFAULT_FIXTURE = Path(__file__).parent / "aapl_fixture_500bars.csv"


def load_fixture(fixture_path: Path) -> pd.DataFrame:
    """Load the price fixture the signal engine runs against."""
    df = pd.read_csv(fixture_path)
    df.columns = df.columns.str.lower()

    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])

    return df


def generate_signals_for_strategy(strategy_config, df: pd.DataFrame) -> pd.DataFrame:
    """Generate signals for all bars, feeding them incrementally."""
    engine = SignalEngine(strategy_config)

    signals = []
    for i in range(len(df)):
        df_slice = df.iloc[: i + 1].copy()
        result = engine.generate_signal(df_slice)
        signals.append({"bar_index": i, "signal": result["signal"]})

    return pd.DataFrame(signals)


def _build_strategy_config(strategy_name: str, params: dict) -> StrategySchema:
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
            "exported_from": "AlphaLive-SelfGeneratedRegression",
            "exported_at": "2024-01-01T00:00:00Z",
            "alphalab_version": "1.0.0",
            "backtest_id": f"regression_{strategy_name}",
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


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate self-generated AlphaLive regression fixtures (NOT "
            "cross-system parity evidence). Requires an explicit output "
            "directory and explicit strategy selection - never writes to a "
            "default or tracked fixture path."
        )
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Directory to write expected_signals_<strategy>.csv files into. "
        "Required - there is no default, and it is never the repository's "
        "tests/fixtures/ directory unless you explicitly pass that path "
        "(not recommended - use a scratch/tmp directory).",
    )
    strategy_group = parser.add_mutually_exclusive_group(required=True)
    strategy_group.add_argument(
        "--strategy",
        action="append",
        dest="strategies",
        choices=sorted(STRATEGIES.keys()),
        help="Generate for this one strategy. Repeatable for multiple "
        "specific strategies. Mutually exclusive with --all-strategies.",
    )
    strategy_group.add_argument(
        "--all-strategies",
        action="store_true",
        help="Generate for every known strategy. Mutually exclusive with "
        "--strategy.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow overwriting a file that already exists at the target "
        "path. Without this, an existing file causes the script to refuse "
        "to write it and exit with an error.",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_FIXTURE,
        help=f"Price fixture CSV to run the signal engine against "
        f"(default: {DEFAULT_FIXTURE}).",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    strategy_names = (
        sorted(STRATEGIES.keys()) if args.all_strategies else list(args.strategies)
    )

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    targets = {
        name: output_dir / f"expected_signals_{name}.csv" for name in strategy_names
    }

    print("=" * 60)
    print("Generating SELF-GENERATED regression fixtures")
    print("(AlphaLive's own output - NOT independent AlphaLab parity evidence)")
    print("=" * 60)
    print(f"Output directory: {output_dir}")
    print("Files that will be written:")
    for name, path in targets.items():
        exists = " (EXISTS - will be overwritten)" if path.exists() else ""
        print(f"  - {path}{exists}")
    print()

    if not args.overwrite:
        already_exist = [str(p) for p in targets.values() if p.exists()]
        if already_exist:
            print("ERROR: the following target files already exist and --overwrite")
            print("was not passed - refusing to write anything:")
            for p in already_exist:
                print(f"  - {p}")
            return 1

    df = load_fixture(args.fixture)
    print(f"Loaded fixture: {len(df)} bars from {args.fixture}")
    print()

    for strategy_name in strategy_names:
        print(f"Generating signals for {strategy_name}...", end=" ")

        config = _build_strategy_config(strategy_name, STRATEGIES[strategy_name])
        signals_df = generate_signals_for_strategy(config, df)
        signal_count = len(signals_df[signals_df["signal"] != "HOLD"])

        output_path = targets[strategy_name]
        signals_df.to_csv(output_path, index=False)

        print(f"{signal_count} signals generated, saved to {output_path}")

    print()
    print("=" * 60)
    print("Done. Reminder: this is self-generated regression data, not")
    print("independent cross-system (AlphaLab) parity evidence.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
