#!/usr/bin/env python3
"""
Live Signal Monitor - Watch for signals in real-time

Continuously monitors market data and shows how close the strategy is to generating a signal.
Useful for understanding why signals are/aren't being generated.

Usage:
    python scripts/live_signal_monitor.py --config configs/production/rsi_simple_SPY_15Min.json
    python scripts/live_signal_monitor.py --config configs/production/rsi_simple_SPY_15Min.json --watch
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import pandas as pd
import numpy as np
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands
from ta.trend import SMAIndicator
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame


def check_environment():
    """Check required environment variables are set"""
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        print("Error: Missing required environment variables")
        print("")
        print("Please set:")
        print("  export ALPACA_API_KEY='your_key'")
        print("  export ALPACA_SECRET_KEY='your_secret'")
        print("")
        print("Get keys from: https://app.alpaca.markets/paper/dashboard/overview")
        sys.exit(1)


def resolve_config_path(config_path: str) -> str:
    """Resolve config path relative to AlphaLive directory"""
    if os.path.isabs(config_path):
        if os.path.exists(config_path):
            return config_path

    # Try current directory first
    if os.path.exists(config_path):
        return os.path.abspath(config_path)

    # Try relative to AlphaLive directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    alphalive_dir = os.path.join(script_dir, "..")
    config_full_path = os.path.join(alphalive_dir, config_path)

    if os.path.exists(config_full_path):
        return config_full_path

    print(f"Error: Config file not found: {config_path}")
    print(f"   Looked in:")
    print(f"   - {os.path.abspath(config_path)}")
    print(f"   - {config_full_path}")
    sys.exit(1)


class LiveSignalMonitor:
    """Monitor live market data and signal proximity"""

    def __init__(self, config_path: str):
        self.config_path = resolve_config_path(config_path)
        self.config = self._load_config()

        # Initialize Alpaca data client
        api_key = os.getenv("ALPACA_API_KEY")
        secret_key = os.getenv("ALPACA_SECRET_KEY")

        self.data_client = StockHistoricalDataClient(api_key, secret_key)

    def _load_config(self) -> Dict:
        """Load strategy configuration"""
        try:
            with open(self.config_path, "r") as f:
                config = json.load(f)

            # Basic validation
            required = ["strategy", "ticker", "timeframe"]
            missing = [k for k in required if k not in config]
            if missing:
                raise ValueError(f"Config missing required fields: {missing}")

            return config
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in config file: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"Error loading config: {e}")
            sys.exit(1)

    def _get_timeframe(self) -> TimeFrame:
        """Convert config timeframe to Alpaca TimeFrame"""
        tf_map = {
            "1Day": TimeFrame.Day,
            "1Hour": TimeFrame.Hour,
            "15Min": TimeFrame.Minute,
        }
        return tf_map.get(self.config["timeframe"], TimeFrame.Day)

    def fetch_current_data(self, bars: int = 100) -> pd.DataFrame:
        """Fetch recent market data with retry logic"""
        ticker = self.config["ticker"]
        timeframe = self._get_timeframe()

        # For 15Min, we need to adjust the number of bars
        if timeframe == TimeFrame.Minute:
            bars = bars * 15  # Get enough 1min bars to construct 15min bars

        request = StockBarsRequest(
            symbol_or_symbols=ticker, timeframe=timeframe, limit=bars
        )

        # Retry logic
        max_retries = 3
        for attempt in range(max_retries):
            try:
                bars_data = self.data_client.get_stock_bars(request)
                df = bars_data.df

                if df.empty:
                    raise ValueError(f"No data returned for {ticker}")

                # Reset index to get timestamp as column
                df = df.reset_index()
                df.columns = [col.lower() for col in df.columns]

                return df

            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                wait_time = 2**attempt
                print(
                    f"API call failed, retrying in {wait_time}s... ({attempt+1}/{max_retries})"
                )
                time.sleep(wait_time)

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate indicators based on strategy using ta library"""
        strategy_name = self.config["strategy"]["name"]
        params = self.config["strategy"]["parameters"]

        if strategy_name in ["rsi_mean_reversion", "rsi_simple", "trend_adaptive_rsi"]:
            rsi_period = params.get("rsi_period", 14)
            rsi_indicator = RSIIndicator(close=df["close"], window=rsi_period)
            df["rsi"] = rsi_indicator.rsi()

        if strategy_name == "bollinger_rsi_combo" or "bollinger" in strategy_name:
            bb_period = params.get("bb_period", 20)
            bb_std = params.get("bb_std", 2.0)
            bb_indicator = BollingerBands(
                close=df["close"], window=bb_period, window_dev=bb_std
            )
            df["bb_upper"] = bb_indicator.bollinger_hband()
            df["bb_middle"] = bb_indicator.bollinger_mavg()
            df["bb_lower"] = bb_indicator.bollinger_lband()

        if strategy_name == "trend_adaptive_rsi":
            trend_sma = params.get("trend_sma", 50)
            sma_indicator = SMAIndicator(close=df["close"], window=trend_sma)
            df["sma"] = sma_indicator.sma_indicator()
            df["sma_slope"] = df["sma"].diff(params.get("trend_lookback", 5))

        if "ma_crossover" in strategy_name:
            fast_period = params.get("fast_period", 10)
            slow_period = params.get("slow_period", 30)
            fast_sma = SMAIndicator(close=df["close"], window=fast_period)
            slow_sma = SMAIndicator(close=df["close"], window=slow_period)
            df["sma_fast"] = fast_sma.sma_indicator()
            df["sma_slow"] = slow_sma.sma_indicator()

        return df

    def get_signal_proximity(self, df: pd.DataFrame) -> Dict:
        """Calculate how close we are to generating a signal"""
        strategy_name = self.config["strategy"]["name"]
        params = self.config["strategy"]["parameters"]

        latest = df.iloc[-1]
        proximity = {
            "timestamp": latest.get("timestamp", datetime.now()),
            "price": latest["close"],
            "strategy": strategy_name,
        }

        if strategy_name in ["rsi_mean_reversion", "rsi_simple"]:
            oversold = params.get("oversold", 30)
            overbought = params.get("overbought", 70)
            rsi = latest["rsi"]

            proximity["rsi"] = rsi
            proximity["oversold_threshold"] = oversold
            proximity["overbought_threshold"] = overbought
            proximity["distance_to_buy"] = rsi - oversold
            proximity["distance_to_sell"] = overbought - rsi

            if rsi < oversold:
                proximity["signal"] = "BUY"
                proximity["signal_strength"] = (oversold - rsi) / oversold * 100
            elif rsi > overbought:
                proximity["signal"] = "SELL"
                proximity["signal_strength"] = (
                    (rsi - overbought) / (100 - overbought) * 100
                )
            else:
                proximity["signal"] = "HOLD"
                proximity["signal_strength"] = 0

        elif strategy_name == "trend_adaptive_rsi":
            rsi = latest["rsi"]
            sma_slope = latest["sma_slope"]

            # Determine regime
            if sma_slope > 0:
                regime = "UPTREND"
                buy_threshold = params.get("uptrend_buy", 45)
                sell_threshold = params.get("uptrend_sell", 65)
            elif sma_slope < 0:
                regime = "DOWNTREND"
                buy_threshold = params.get("downtrend_buy", 35)
                sell_threshold = params.get("downtrend_sell", 55)
            else:
                regime = "RANGING"
                buy_threshold = params.get("range_buy", 35)
                sell_threshold = params.get("range_sell", 65)

            proximity["rsi"] = rsi
            proximity["sma_slope"] = sma_slope
            proximity["regime"] = regime
            proximity["buy_threshold"] = buy_threshold
            proximity["sell_threshold"] = sell_threshold
            proximity["distance_to_buy"] = rsi - buy_threshold
            proximity["distance_to_sell"] = sell_threshold - rsi

            if rsi < buy_threshold:
                proximity["signal"] = "BUY"
                proximity["signal_strength"] = (
                    (buy_threshold - rsi) / buy_threshold * 100
                )
            elif rsi > sell_threshold:
                proximity["signal"] = "SELL"
                proximity["signal_strength"] = (
                    (rsi - sell_threshold) / (100 - sell_threshold) * 100
                )
            else:
                proximity["signal"] = "HOLD"
                proximity["signal_strength"] = 0

        elif strategy_name == "bollinger_rsi_combo":
            rsi = latest["rsi"]
            price = latest["close"]
            bb_lower = latest["bb_lower"]
            bb_upper = latest["bb_upper"]
            bb_middle = latest["bb_middle"]

            rsi_buy = params.get("rsi_oversold", 45)
            rsi_sell = params.get("rsi_overbought", 55)

            proximity["rsi"] = rsi
            proximity["price"] = price
            proximity["bb_lower"] = bb_lower
            proximity["bb_middle"] = bb_middle
            proximity["bb_upper"] = bb_upper
            proximity["distance_to_bb_lower"] = price - bb_lower
            proximity["distance_to_bb_lower_pct"] = ((price / bb_lower) - 1) * 100

            # Check both conditions
            at_bb_lower = price <= bb_lower
            rsi_oversold = rsi < rsi_buy

            if at_bb_lower and rsi_oversold:
                proximity["signal"] = "BUY"
                proximity["signal_strength"] = 100
            elif price >= bb_middle or rsi > rsi_sell:
                proximity["signal"] = "SELL"
                proximity["signal_strength"] = 50
            else:
                proximity["signal"] = "HOLD"
                proximity["signal_strength"] = 0

            proximity["bb_lower_touch"] = at_bb_lower
            proximity["rsi_oversold"] = rsi_oversold

        elif "ma_crossover" in strategy_name:
            sma_fast = latest["sma_fast"]
            sma_slow = latest["sma_slow"]

            proximity["sma_fast"] = sma_fast
            proximity["sma_slow"] = sma_slow
            proximity["distance"] = sma_fast - sma_slow
            proximity["distance_pct"] = ((sma_fast / sma_slow) - 1) * 100

            if sma_fast > sma_slow:
                proximity["signal"] = "BUY"
                proximity["signal_strength"] = proximity["distance_pct"] * 10
            elif sma_fast < sma_slow:
                proximity["signal"] = "SELL"
                proximity["signal_strength"] = abs(proximity["distance_pct"]) * 10
            else:
                proximity["signal"] = "HOLD"
                proximity["signal_strength"] = 0

        return proximity

    def display_status(self, proximity: Dict):
        """Display current signal status with color"""
        print("\n" + "=" * 80)
        print(
            f"LIVE SIGNAL MONITOR - {self.config['ticker']} @ {self.config['timeframe']}"
        )
        print("=" * 80)
        print(f"Strategy: {proximity['strategy']}")
        print(f"Current Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Price: ${proximity['price']:.2f}")
        print()

        signal = proximity["signal"]
        signal_icon = {"BUY": "", "SELL": "", "HOLD": ""}.get(signal, "")

        print(f"Signal: {signal_icon} {signal}")
        print(f"Strength: {proximity['signal_strength']:.1f}%")
        print()

        # Strategy-specific details
        strategy_name = proximity["strategy"]

        if strategy_name in ["rsi_mean_reversion", "rsi_simple"]:
            print("-" * 80)
            print("RSI ANALYSIS")
            print("-" * 80)
            print(f"Current RSI: {proximity['rsi']:.2f}")
            print(f"Oversold Threshold: {proximity['oversold_threshold']}")
            print(f"Overbought Threshold: {proximity['overbought_threshold']}")
            print(f"Distance to BUY signal: {proximity['distance_to_buy']:.2f} points")
            print(
                f"Distance to SELL signal: {proximity['distance_to_sell']:.2f} points"
            )

            if proximity["distance_to_buy"] > 0:
                print(
                    f"\n Need RSI to drop {proximity['distance_to_buy']:.2f} points for BUY signal"
                )
            elif proximity["distance_to_sell"] > 0:
                print(
                    f"\n Need RSI to rise {proximity['distance_to_sell']:.2f} points for SELL signal"
                )

        elif strategy_name == "trend_adaptive_rsi":
            print("-" * 80)
            print("TREND ADAPTIVE RSI ANALYSIS")
            print("-" * 80)
            print(f"Market Regime: {proximity['regime']}")
            print(f"SMA Slope: {proximity['sma_slope']:.4f}")
            print(f"Current RSI: {proximity['rsi']:.2f}")
            print(
                f"Buy Threshold ({proximity['regime']}): {proximity['buy_threshold']}"
            )
            print(
                f"Sell Threshold ({proximity['regime']}): {proximity['sell_threshold']}"
            )
            print(f"Distance to BUY: {proximity['distance_to_buy']:.2f} points")
            print(f"Distance to SELL: {proximity['distance_to_sell']:.2f} points")

        elif strategy_name == "bollinger_rsi_combo":
            print("-" * 80)
            print("BOLLINGER + RSI COMBO ANALYSIS")
            print("-" * 80)
            print(f"Current Price: ${proximity['price']:.2f}")
            print(f"BB Lower: ${proximity['bb_lower']:.2f}")
            print(f"BB Middle: ${proximity['bb_middle']:.2f}")
            print(f"BB Upper: ${proximity['bb_upper']:.2f}")
            print(
                f"Distance to BB Lower: ${proximity['distance_to_bb_lower']:.2f} ({proximity['distance_to_bb_lower_pct']:.2f}%)"
            )
            print(f"Current RSI: {proximity['rsi']:.2f}")
            print()
            print(f"BB Lower Touch: {'YES' if proximity['bb_lower_touch'] else 'NO'}")
            print(f"RSI Oversold: {'YES' if proximity['rsi_oversold'] else 'NO'}")

            if not proximity["bb_lower_touch"]:
                print(
                    f"\n Need price to drop {proximity['distance_to_bb_lower_pct']:.2f}% to touch BB Lower"
                )

        elif "ma_crossover" in strategy_name:
            print("-" * 80)
            print("MA CROSSOVER ANALYSIS")
            print("-" * 80)
            print(f"Fast SMA: ${proximity['sma_fast']:.2f}")
            print(f"Slow SMA: ${proximity['sma_slow']:.2f}")
            print(
                f"Distance: ${proximity['distance']:.2f} ({proximity['distance_pct']:.2f}%)"
            )

            if proximity["distance"] > 0:
                print("\n Fast SMA above Slow SMA (Bullish)")
            else:
                print("\n Fast SMA below Slow SMA (Bearish)")

        print("=" * 80)

    def watch(self, interval: int = 60):
        """Continuously watch for signals"""
        print("Starting live signal monitoring...")
        print(f"Checking every {interval} seconds. Press Ctrl+C to stop.")

        try:
            while True:
                try:
                    print("Fetching market data...", end="", flush=True)
                    df = self.fetch_current_data()
                    print("")

                    print("Calculating indicators...", end="", flush=True)
                    df = self.calculate_indicators(df)
                    print("")

                    proximity = self.get_signal_proximity(df)
                    self.display_status(proximity)

                    if proximity["signal"] != "HOLD":
                        print(f"\n ALERT: {proximity['signal']} SIGNAL DETECTED!")

                    time.sleep(interval)

                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    print(f"\n Error: {e}")
                    print("Retrying in 30 seconds...")
                    time.sleep(30)

        except KeyboardInterrupt:
            print("\n\nStopping monitor...")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Monitor live signals")
    parser.add_argument("--config", required=True, help="Path to strategy config file")
    parser.add_argument(
        "--watch", action="store_true", help="Continuously watch for signals"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Check interval in seconds (for --watch mode)",
    )
    parser.add_argument("--version", action="version", version="AlphaLive Tools v1.0.0")

    args = parser.parse_args()

    # Check environment first
    check_environment()

    try:
        monitor = LiveSignalMonitor(args.config)

        if args.watch:
            monitor.watch(interval=args.interval)
        else:
            print("Fetching market data...", end="", flush=True)
            df = monitor.fetch_current_data()
            print("")

            print("Calculating indicators...", end="", flush=True)
            df = monitor.calculate_indicators(df)
            print("")

            proximity = monitor.get_signal_proximity(df)
            monitor.display_status(proximity)

            if proximity["signal"] != "HOLD":
                print(f"\n {proximity['signal']} SIGNAL ACTIVE!")

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
