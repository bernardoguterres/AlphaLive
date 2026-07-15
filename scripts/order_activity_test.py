"""Order-activity test: replay every production config over real market data.

Answers "how many orders would the bot actually place?" for the last week,
month, and year, per production config, using free yfinance data - no API
keys needed.

For each config in configs/production/:
  1. Download real bars (daily 5y; 15Min/1Hour limited by yfinance to ~60/730d)
  2. Replay bar-by-bar through the real SignalEngine (same code the bot runs)
  3. Convert signals to orders the way OrderManager would: BUY only when
     flat, SELL only when holding (a SELL with no position is blocked)
  4. Count orders in the trailing 7 / 30 / 365 calendar days

Downloaded CSVs are cached under data/activity_test/ (data/ is gitignored).

Usage:
    python scripts/order_activity_test.py                # all production configs
    python scripts/order_activity_test.py --configs ma_crossover_SPY.json
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from alphalive.config import load_config_path  # noqa: E402
from alphalive.strategy.signal_engine import SignalEngine  # noqa: E402

warnings.filterwarnings("ignore")

DATA_DIR = PROJECT_ROOT / "data" / "activity_test"
CONFIG_DIR = PROJECT_ROOT / "configs" / "production"

# yfinance interval + history limit per AlphaLive timeframe
TIMEFRAME_FETCH = {
    "1Day": ("1d", "5y"),
    "1Week": ("1d", "5y"),  # daily, resampled W-FRI like market_data.py
    "1Hour": ("1h", "730d"),  # yfinance hard limit for 1h
    "15Min": ("15m", "60d"),  # yfinance hard limit for 15m
}

WINDOWS = {"last_week": 7, "last_month": 30, "last_year": 365}

# Signal checks per bar mirror main.py cadence: one check per completed bar
# (1Day at 9:35, 1Hour hourly, 15Min per quarter hour, 1Week Mondays).


def fetch_bars(ticker: str, timeframe: str) -> pd.DataFrame:
    """Download (or load cached) OHLCV bars for a ticker/timeframe."""
    import yfinance as yf

    interval, period = TIMEFRAME_FETCH[timeframe]
    cache_file = DATA_DIR / f"{ticker}_{interval}_{period}.csv"

    if cache_file.exists():
        df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
    else:
        raw = yf.download(
            ticker,
            period=period,
            interval=interval,
            auto_adjust=True,
            progress=False,
        )
        if raw.empty:
            raise RuntimeError(f"yfinance returned no data for {ticker} {interval}")
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        df = raw.rename(
            columns={
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            }
        )[["open", "high", "low", "close", "volume"]]
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache_file)

    df.index = pd.to_datetime(df.index, utc=True)

    if timeframe == "1Week":
        # Mirror market_data._resample_to_weekly exactly
        df = (
            df.resample("W-FRI")
            .agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                volume=("volume", "sum"),
            )
            .dropna(subset=["close"])
        )
    return df


def replay_config(config, df: pd.DataFrame, warmup_bars: int = 200) -> list[dict]:
    """Replay bars through the real SignalEngine, emitting orders.

    Position-gated like OrderManager: BUY only when flat, SELL only when
    holding. Returns a list of {timestamp, side, reason} order events.
    """
    engine = SignalEngine(config)
    orders = []
    holding = False

    start = min(warmup_bars, max(len(df) - 1, 0))
    for i in range(start, len(df)):
        window = df.iloc[: i + 1]
        result = engine.generate_signal(window.copy())
        sig = result["signal"]
        if sig == "BUY" and not holding:
            holding = True
            orders.append(
                {
                    "timestamp": df.index[i],
                    "side": "BUY",
                    "reason": result["reason"][:70],
                }
            )
        elif sig == "SELL" and holding:
            holding = False
            orders.append(
                {
                    "timestamp": df.index[i],
                    "side": "SELL",
                    "reason": result["reason"][:70],
                }
            )
    return orders


def count_in_windows(orders: list[dict], data_end: pd.Timestamp) -> dict:
    counts = {}
    for name, days in WINDOWS.items():
        cutoff = data_end - timedelta(days=days)
        counts[name] = sum(1 for o in orders if o["timestamp"] >= cutoff)
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--configs",
        nargs="*",
        default=None,
        help="Specific config filenames (default: all in configs/production/)",
    )
    args = parser.parse_args()

    config_files = (
        [CONFIG_DIR / c for c in args.configs]
        if args.configs
        else sorted(CONFIG_DIR.glob("*.json"))
    )

    rows = []
    all_orders = {}
    for cf in config_files:
        try:
            config = load_config_path(str(cf))[0]
        except Exception as e:
            print(f"SKIP {cf.name}: config failed to load ({e})")
            continue

        try:
            df = fetch_bars(config.ticker, config.timeframe)
        except Exception as e:
            print(f"SKIP {cf.name}: data fetch failed ({e})")
            continue

        orders = replay_config(config, df)
        data_end = df.index[-1]
        counts = count_in_windows(orders, data_end)
        coverage_days = (df.index[-1] - df.index[0]).days

        rows.append(
            {
                "config": cf.name.replace(".json", ""),
                "timeframe": config.timeframe,
                "bars": len(df),
                "coverage": f"{coverage_days}d",
                **counts,
                "total": len(orders),
            }
        )
        all_orders[cf.name] = orders
        print(
            f"done {cf.name}: {len(orders)} orders over {coverage_days}d "
            f"({len(df)} bars)"
        )

    print("\n" + "=" * 100)
    print(
        f"{'config':<38} {'tf':<6} {'bars':>5} {'coverage':>9} "
        f"{'week':>5} {'month':>6} {'year':>5} {'total':>6}"
    )
    print("-" * 100)
    for r in rows:
        print(
            f"{r['config']:<38} {r['timeframe']:<6} {r['bars']:>5} "
            f"{r['coverage']:>9} {r['last_week']:>5} {r['last_month']:>6} "
            f"{r['last_year']:>5} {r['total']:>6}"
        )
    print("=" * 100)

    # Dump full order log for inspection
    log_path = DATA_DIR / "order_activity_log.json"
    log_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(),
                "orders": {
                    k: [{**o, "timestamp": o["timestamp"].isoformat()} for o in v]
                    for k, v in all_orders.items()
                },
            },
            indent=2,
        )
    )
    print(f"\nFull order log: {log_path}")


if __name__ == "__main__":
    main()
