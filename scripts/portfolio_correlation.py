#!/usr/bin/env python3
"""
Portfolio Correlation Analysis

Loads all 1Day configs from configs/production/, runs each strategy's signal engine
bar-by-bar over 2 years of daily data (2022-2023), then computes and prints a
correlation matrix of the resulting signal series.

Highlights any pair with |correlation| > 0.7 as HIGH CORRELATION RISK.

Usage:
    python scripts/portfolio_correlation.py
"""

import sys
import os
import json
import logging
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
import yfinance as yf

# ---------------------------------------------------------------------------
# Path setup - standard AlphaLive pattern
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from alphalive.strategy_schema import StrategySchema
from alphalive.strategy.signal_engine import SignalEngine

# Suppress noisy INFO logs from StrategySchema validators and SignalEngine
logging.basicConfig(level=logging.WARNING)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CONFIGS_DIR = PROJECT_ROOT / "configs" / "production"
DATA_START = "2021-01-01"  # extra lead-in for indicator warm-up
DATA_END = "2023-12-31"
SIGNAL_START = "2022-01-01"  # correlation window we actually care about
SIGNAL_END = "2023-12-31"
HIGH_CORR_THRESHOLD = 0.7
LABEL_MAX_LEN = 25

# Signal integer encoding
SIGNAL_MAP = {"BUY": 1, "SELL": -1, "HOLD": 0}


# ---------------------------------------------------------------------------
# Step 1 - load configs
# ---------------------------------------------------------------------------


def load_configs(
    configs_dir: Path,
) -> Tuple[List[Tuple[str, StrategySchema]], List[Tuple[str, str]]]:
    """
    Returns:
        daily_configs  : list of (filename, StrategySchema) for 1Day configs
        skipped_configs: list of (filename, timeframe) for non-1Day configs
    """
    daily_configs = []
    skipped_configs = []

    json_files = sorted(configs_dir.glob("*.json"))
    if not json_files:
        print(f"ERROR: No JSON files found in {configs_dir}")
        sys.exit(1)

    for path in json_files:
        with open(path) as f:
            raw = json.load(f)

        schema = StrategySchema(**raw)
        fname = path.name

        if schema.timeframe == "1Day":
            daily_configs.append((fname, schema))
        else:
            skipped_configs.append((fname, schema.timeframe))

    return daily_configs, skipped_configs


# ---------------------------------------------------------------------------
# Step 2 - download price data (once per unique ticker)
# ---------------------------------------------------------------------------


def download_price_data(tickers: List[str]) -> Dict[str, pd.DataFrame]:
    """
    Download 2 years of daily OHLCV from yfinance for the given tickers.
    We fetch from DATA_START (before SIGNAL_START) so indicators can warm up.

    Returns a dict: ticker -> DataFrame with lowercase OHLCV columns.
    """
    print(
        f"\nDownloading daily price data for {len(tickers)} ticker(s): {', '.join(sorted(tickers))}"
    )
    print(
        f"  Period : {DATA_START} to {DATA_END}  (extra lead-in for indicator warm-up)"
    )

    price_data: Dict[str, pd.DataFrame] = {}

    for ticker in sorted(tickers):
        print(f"  Fetching {ticker} ...", end=" ", flush=True)
        raw = yf.download(
            ticker, start=DATA_START, end=DATA_END, auto_adjust=True, progress=False
        )

        if raw.empty:
            print(f"WARNING: no data returned for {ticker} - skipping")
            continue

        # Flatten MultiIndex first, then lowercase
        df = raw.copy()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]

        df.index = pd.to_datetime(df.index)
        df = df.sort_index()

        # Keep only the columns SignalEngine needs
        required = ["open", "high", "low", "close", "volume"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            print(f"WARNING: {ticker} data is missing columns {missing} - skipping")
            continue

        df = df[required].dropna()
        print(f"{len(df)} bars")
        price_data[ticker] = df

    return price_data


# ---------------------------------------------------------------------------
# Step 3 - run signal engine bar-by-bar for one config
# ---------------------------------------------------------------------------


def generate_signal_series(
    fname: str,
    schema: StrategySchema,
    price_df: pd.DataFrame,
) -> pd.Series:
    """
    Run SignalEngine.generate_signal() bar-by-bar on price_df.

    Yields a pd.Series indexed by date with values in {-1, 0, 1}.
    Only dates within [SIGNAL_START, SIGNAL_END] are kept.
    """
    engine = SignalEngine(schema)
    signals: Dict[pd.Timestamp, int] = {}

    # Minimum bars needed before the first signal check - use slow_period, rsi_period, etc.
    # We just start from bar index 60 to guarantee enough indicator history.
    warmup_bars = 60

    total_bars = len(price_df)
    for i in range(warmup_bars, total_bars):
        # Slice up to and including bar i (simulate "latest data known at bar i")
        window = price_df.iloc[: i + 1]
        date = price_df.index[i]

        # Only record signals inside the correlation window
        if date < pd.Timestamp(SIGNAL_START) or date > pd.Timestamp(SIGNAL_END):
            # Still need to run the engine to advance stateful strategies
            # (bollinger_rsi_combo, trend_adaptive_rsi track _in_position)
            engine.generate_signal(window)
            continue

        result = engine.generate_signal(window)
        signals[date] = SIGNAL_MAP.get(result["signal"], 0)

    if not signals:
        return pd.Series(dtype=int, name=fname)

    series = pd.Series(signals, name=fname)
    series.index = pd.to_datetime(series.index)
    return series


# ---------------------------------------------------------------------------
# Step 4 - build aligned signal DataFrame
# ---------------------------------------------------------------------------


def build_signal_matrix(
    daily_configs: List[Tuple[str, StrategySchema]],
    price_data: Dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """
    Run the signal engine for each 1Day config and align all series by date.
    Missing dates are filled with 0 (HOLD).
    Labels are truncated to LABEL_MAX_LEN characters.
    """
    all_series: List[pd.Series] = []
    failed: List[str] = []

    print(
        f"\nGenerating bar-by-bar signals for {len(daily_configs)} 1Day config(s) ..."
    )

    for fname, schema in daily_configs:
        ticker = schema.ticker
        label = fname.replace(".json", "")[:LABEL_MAX_LEN]

        if ticker not in price_data:
            print(f"  SKIP  {label} - no price data for {ticker}")
            failed.append(fname)
            continue

        print(f"  Running {label} ({ticker}) ...", end=" ", flush=True)
        try:
            series = generate_signal_series(fname, schema, price_data[ticker])
            series.name = label
            all_series.append(series)
            buy_count = (series == 1).sum()
            sell_count = (series == -1).sum()
            print(f"done  [{len(series)} dates | BUY={buy_count} SELL={sell_count}]")
        except Exception as exc:
            print(f"ERROR: {exc}")
            failed.append(fname)

    if not all_series:
        print("\nERROR: No signal series were produced. Exiting.")
        sys.exit(1)

    # Outer join - all trading dates from any config; fill missing with 0 (HOLD)
    matrix = pd.concat(all_series, axis=1, join="outer").fillna(0).astype(int)
    matrix.sort_index(inplace=True)
    return matrix


# ---------------------------------------------------------------------------
# Step 5 - compute and print correlation matrix
# ---------------------------------------------------------------------------


def print_correlation_matrix(corr: pd.DataFrame) -> None:
    pd.options.display.float_format = "{:+.3f}".format
    pd.options.display.max_columns = None
    pd.options.display.width = 200

    print("\n" + "=" * 80)
    print("SIGNAL CORRELATION MATRIX  (BUY=+1, HOLD=0, SELL=-1  |  2022-2023 daily)")
    print("=" * 80)
    print(corr.to_string())
    print("=" * 80)


# ---------------------------------------------------------------------------
# Step 6 - flag high-correlation pairs
# ---------------------------------------------------------------------------


def flag_high_correlation_pairs(corr: pd.DataFrame) -> List[Tuple[str, str, float]]:
    """Return list of (label_a, label_b, correlation) where |corr| > threshold."""
    high_pairs: List[Tuple[str, str, float]] = []
    labels = corr.columns.tolist()
    n = len(labels)

    for i in range(n):
        for j in range(i + 1, n):
            val = corr.iloc[i, j]
            if abs(val) > HIGH_CORR_THRESHOLD:
                high_pairs.append((labels[i], labels[j], val))

    # Sort descending by |correlation|
    high_pairs.sort(key=lambda t: abs(t[2]), reverse=True)
    return high_pairs


def print_high_correlation_warnings(high_pairs: List[Tuple[str, str, float]]) -> None:
    print("\n" + "=" * 80)
    print(f"HIGH CORRELATION RISK  (|correlation| > {HIGH_CORR_THRESHOLD})")
    print("=" * 80)

    if not high_pairs:
        print("  None found - all strategy pairs are below the threshold.")
    else:
        print(f"  {len(high_pairs)} pair(s) flagged:\n")
        for label_a, label_b, val in high_pairs:
            direction = "POSITIVE" if val > 0 else "NEGATIVE"
            print(
                f"  WARNING  {direction} correlation {val:+.3f}  |  "
                f"{label_a}  <->  {label_b}"
            )
        print()
        print(
            "  Highly correlated strategies tend to enter and exit simultaneously,\n"
            "  concentrating portfolio risk. Consider whether these configs represent\n"
            "  genuinely distinct exposures or redundant allocations."
        )

    print("=" * 80)


# ---------------------------------------------------------------------------
# Step 7 - summary
# ---------------------------------------------------------------------------


def print_summary(
    total_configs: int,
    daily_configs: List[Tuple[str, StrategySchema]],
    skipped_configs: List[Tuple[str, str]],
) -> None:
    n_daily = len(daily_configs)
    n_skipped = len(skipped_configs)

    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"  Total configs found      : {total_configs}")
    print(f"  1Day configs (included)  : {n_daily}")
    print(f"  Non-1Day configs (skipped): {n_skipped}")

    if skipped_configs:
        print()
        print("  Skipped configs (timeframe not comparable to daily signals):")
        for fname, tf in sorted(skipped_configs):
            print(f"    {fname}  [{tf}]")

    print("=" * 80)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print("=" * 80)
    print("ALPHALIVE - PORTFOLIO CORRELATION ANALYSIS")
    print(f"  Configs dir : {CONFIGS_DIR}")
    print(f"  Data window : {DATA_START} to {DATA_END}")
    print(f"  Corr window : {SIGNAL_START} to {SIGNAL_END}")
    print(f"  High-corr   : |r| > {HIGH_CORR_THRESHOLD}")
    print("=" * 80)

    # 1. Load configs
    daily_configs, skipped_configs = load_configs(CONFIGS_DIR)
    total_configs = len(daily_configs) + len(skipped_configs)

    print(
        f"\nLoaded {total_configs} config(s): {len(daily_configs)} daily, {len(skipped_configs)} non-daily"
    )

    if not daily_configs:
        print("ERROR: No 1Day configs found - nothing to correlate.")
        sys.exit(1)

    # 2. Download price data (one fetch per unique ticker)
    unique_tickers = list({schema.ticker for _, schema in daily_configs})
    price_data = download_price_data(unique_tickers)

    if not price_data:
        print("ERROR: No price data downloaded. Check network/yfinance.")
        sys.exit(1)

    # 3 + 4. Generate signals and build aligned matrix
    signal_matrix = build_signal_matrix(daily_configs, price_data)

    print(
        f"\nSignal matrix: {signal_matrix.shape[0]} dates x {signal_matrix.shape[1]} strategies"
    )

    # 5. Compute Pearson correlation
    corr = signal_matrix.corr(method="pearson")

    # 6. Print matrix
    print_correlation_matrix(corr)

    # 7. Flag high-correlation pairs
    high_pairs = flag_high_correlation_pairs(corr)
    print_high_correlation_warnings(high_pairs)

    # 8. Summary
    print_summary(total_configs, daily_configs, skipped_configs)


if __name__ == "__main__":
    main()
