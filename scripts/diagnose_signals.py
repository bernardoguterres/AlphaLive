#!/usr/bin/env python3
"""
Signal Diagnosis Tool

Run this script to understand why your strategies aren't generating BUY signals.
Shows you the current indicator values and how close they are to triggering.
"""

import sys
import os
import json
from datetime import datetime, timedelta

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from alphalive.config import load_config_path, load_env
from alphalive.data.market_data import MarketDataFetcher
from alphalive.strategy.signal_engine import SignalEngine
from alphalive.strategy import indicators


def diagnose_strategy(config_path: str):
    """Diagnose why a strategy isn't generating signals."""

    print("=" * 80)
    print("ALPHALIVE SIGNAL DIAGNOSTIC TOOL")
    print("=" * 80)
    print()

    # Load config
    print(f"Loading config: {config_path}")
    strategy_configs = load_config_path(config_path)
    app_config = load_env()

    if len(strategy_configs) == 0:
        print("No strategy configs found")
        return

    strategy_config = strategy_configs[0]  # Use first strategy

    print(f"Config loaded")
    print(f"   Strategy: {strategy_config.strategy.name}")
    print(f"   Ticker: {strategy_config.ticker}")
    print(f"   Timeframe: {strategy_config.timeframe}")
    print(f"   Parameters: {strategy_config.strategy.parameters}")
    print()

    # Fetch market data
    print("Fetching market data...")
    market_data = MarketDataFetcher(
        api_key=app_config.broker.api_key, secret_key=app_config.broker.secret_key
    )

    try:
        df = market_data.get_latest_bars(
            strategy_config.ticker,
            strategy_config.timeframe,
            lookback_bars=200,  # Get plenty of history
        )
        print(f"Fetched {len(df)} bars")
        print(f"   Latest bar: {df.index[-1]}")
        print(f"   Close: ${df['close'].iloc[-1]:.2f}")
        print()
    except Exception as e:
        print(f"Failed to fetch data: {e}")
        return

    # Add indicators
    print("Calculating indicators...")
    try:
        df = indicators.add_all_for_strategy(
            df, strategy_config.strategy.name, strategy_config.strategy.parameters
        )
        print("Indicators calculated")
        print()
    except Exception as e:
        print(f"Failed to calculate indicators: {e}")
        return

    # Generate signal
    print("Generating signal...")
    signal_engine = SignalEngine(strategy_config)
    signal = signal_engine.generate_signal(df)

    print("=" * 80)
    print("CURRENT SIGNAL")
    print("=" * 80)
    print(f"Signal: {signal['signal']}")
    print(f"Confidence: {signal.get('confidence', 0):.1%}")
    print(f"Reason: {signal['reason']}")
    print(f"Warmup Complete: {signal.get('warmup_complete', 'Unknown')}")
    print()

    # Strategy-specific diagnosis
    print("=" * 80)
    print("DETAILED DIAGNOSIS")
    print("=" * 80)

    if strategy_config.strategy.name == "rsi_mean_reversion":
        diagnose_rsi(df, strategy_config.strategy.parameters, signal)

    elif strategy_config.strategy.name == "ma_crossover":
        diagnose_ma_crossover(df, strategy_config.strategy.parameters, signal)

    elif strategy_config.strategy.name == "momentum_breakout":
        diagnose_momentum(df, strategy_config.strategy.parameters, signal)

    elif strategy_config.strategy.name == "bollinger_breakout":
        diagnose_bollinger(df, strategy_config.strategy.parameters, signal)

    elif strategy_config.strategy.name == "vwap_reversion":
        diagnose_vwap(df, strategy_config.strategy.parameters, signal)

    print()
    print("=" * 80)
    print("RECOMMENDATIONS")
    print("=" * 80)

    # Generate recommendations
    if signal["signal"] == "HOLD":
        print("Currently no signal - here's why and how to fix it:")
        print()

        if strategy_config.strategy.name == "rsi_mean_reversion":
            params = strategy_config.strategy.parameters
            rsi_period = params.get("period", 14)
            rsi_col = f"rsi_{rsi_period}"
            current_rsi = df[rsi_col].iloc[-1]
            oversold = params.get("oversold", 30)
            overbought = params.get("overbought", 70)

            if current_rsi > oversold and current_rsi < 50:
                print(
                    f"RSI is {current_rsi:.1f} - above oversold threshold ({oversold})"
                )
                print(
                    f"   To get more BUY signals, increase oversold from {oversold} to 40 or 45"
                )
                print()
                print(f"   Edit your config file and change:")
                print(f'   "oversold": {oversold}  →  "oversold": 40')

            elif current_rsi < overbought and current_rsi > 50:
                print(
                    f"RSI is {current_rsi:.1f} - below overbought threshold ({overbought})"
                )
                print(
                    f"   To get more SELL signals, decrease overbought from {overbought} to 60 or 55"
                )
                print()
                print(f"   Edit your config file and change:")
                print(f'   "overbought": {overbought}  →  "overbought": 60')

            # Show recent history
            print()
            print("Recent RSI values (last 10 days):")
            for i in range(-10, 0):
                rsi_val = df[rsi_col].iloc[i]
                date = df.index[i].strftime("%Y-%m-%d")
                marker = (
                    "BUY"
                    if rsi_val < oversold
                    else ("SELL" if rsi_val > overbought else "")
                )
                print(f"   {date}: RSI={rsi_val:.1f} {marker}")

        elif strategy_config.strategy.name == "ma_crossover":
            params = strategy_config.strategy.parameters
            fast_period = params.get("fast_period", 10)
            slow_period = params.get("slow_period", 20)
            fast_col = f"sma_{fast_period}"
            slow_col = f"sma_{slow_period}"

            fast_curr = df[fast_col].iloc[-1]
            slow_curr = df[slow_col].iloc[-1]

            if fast_curr > slow_curr:
                print(f"Fast SMA ({fast_curr:.2f}) is above Slow SMA ({slow_curr:.2f})")
                print(
                    f"   Already in uptrend - waiting for crossover down to generate SELL"
                )
            else:
                print(f"Fast SMA ({fast_curr:.2f}) is below Slow SMA ({slow_curr:.2f})")
                print(f"   In downtrend - waiting for crossover up to generate BUY")

            print()
            print(f"   To get more crossovers, use faster periods:")
            print(f'   "fast_period": {fast_period}  →  "fast_period": 5')
            print(f'   "slow_period": {slow_period}  →  "slow_period": 15')

            # Show recent crossovers
            print()
            print("Recent crossover history:")
            crossovers = 0
            for i in range(-20, 0):
                fast_prev = df[fast_col].iloc[i - 1]
                fast_curr = df[fast_col].iloc[i]
                slow_prev = df[slow_col].iloc[i - 1]
                slow_curr = df[slow_col].iloc[i]

                if fast_prev <= slow_prev and fast_curr > slow_curr:
                    date = df.index[i].strftime("%Y-%m-%d")
                    print(f"{date}: BULLISH CROSSOVER")
                    crossovers += 1
                elif fast_prev >= slow_prev and fast_curr < slow_curr:
                    date = df.index[i].strftime("%Y-%m-%d")
                    print(f"{date}: BEARISH CROSSOVER")
                    crossovers += 1

            if crossovers == 0:
                print(f"No crossovers in last 20 bars - consider faster periods")

    else:
        print(f"Signal detected: {signal['signal']}")
        print(f"   Confidence: {signal['confidence']:.1%}")
        print(f"   System would attempt to trade if risk checks pass")

    print()


def diagnose_rsi(df, params, signal):
    """Diagnose RSI strategy."""
    period = params.get("period", 14)
    oversold = params.get("oversold", 30)
    overbought = params.get("overbought", 70)

    rsi_col = f"rsi_{period}"
    current_rsi = df[rsi_col].iloc[-1]

    print(f"RSI Mean Reversion Strategy")
    print(f"─" * 80)
    print(f"Current RSI({period}): {current_rsi:.2f}")
    print(f"Oversold threshold: {oversold} (BUY when RSI < {oversold})")
    print(f"Overbought threshold: {overbought} (SELL when RSI > {overbought})")
    print()

    distance_to_oversold = current_rsi - oversold
    distance_to_overbought = overbought - current_rsi

    if current_rsi < oversold:
        print(f"In OVERSOLD zone ({current_rsi:.1f} < {oversold})")
        print(f"   This SHOULD generate a BUY signal")
    elif current_rsi > overbought:
        print(f"In OVERBOUGHT zone ({current_rsi:.1f} > {overbought})")
        print(f"   This SHOULD generate a SELL signal")
    else:
        print(f"In NEUTRAL zone ({oversold} < {current_rsi:.1f} < {overbought})")
        print(f"   Distance to oversold: {distance_to_oversold:.1f} points")
        print(f"   Distance to overbought: {distance_to_overbought:.1f} points")
        print()
        print(
            f"RSI needs to move {distance_to_oversold:.1f} points DOWN to trigger BUY"
        )
        print(
            f"RSI needs to move {distance_to_overbought:.1f} points UP to trigger SELL"
        )


def diagnose_ma_crossover(df, params, signal):
    """Diagnose MA crossover strategy."""
    fast_period = params.get("fast_period", 10)
    slow_period = params.get("slow_period", 20)

    fast_col = f"sma_{fast_period}"
    slow_col = f"sma_{slow_period}"

    fast_curr = df[fast_col].iloc[-1]
    fast_prev = df[fast_col].iloc[-2]
    slow_curr = df[slow_col].iloc[-1]
    slow_prev = df[slow_col].iloc[-2]

    print(f"Moving Average Crossover Strategy")
    print(f"─" * 80)
    print(f"Fast SMA({fast_period}): {fast_curr:.2f} (previous: {fast_prev:.2f})")
    print(f"Slow SMA({slow_period}): {slow_curr:.2f} (previous: {slow_prev:.2f})")
    print()

    if fast_curr > slow_curr:
        spread = fast_curr - slow_curr
        print(f"Fast SMA is ABOVE Slow SMA (+${spread:.2f})")
        print(f"   Currently in UPTREND")
        print(f"   Waiting for bearish crossover (Fast crosses below Slow) to SELL")
    else:
        spread = slow_curr - fast_curr
        print(f"Fast SMA is BELOW Slow SMA (-${spread:.2f})")
        print(f"   Currently in DOWNTREND")
        print(f"   Waiting for bullish crossover (Fast crosses above Slow) to BUY")

    # Check if we're close to a crossover
    if abs(fast_curr - slow_curr) / df["close"].iloc[-1] < 0.01:  # Within 1%
        print(f"Very close to crossover! (within 1% of price)")


def diagnose_momentum(df, params, signal):
    """Diagnose momentum breakout strategy."""
    lookback = params.get("lookback", 20)
    surge_pct = params.get("surge_pct", 1.5)
    volume_ma_period = params.get("volume_ma_period", 20)

    rolling_high = df["rolling_high"].iloc[-1]
    current_price = df["close"].iloc[-1]
    current_volume = df["volume"].iloc[-1]
    volume_ma = df[f"volume_ma_{volume_ma_period}"].iloc[-1]

    volume_surge = current_volume / volume_ma if volume_ma > 0 else 0

    print(f"Momentum Breakout Strategy")
    print(f"─" * 80)
    print(f"Current Price: ${current_price:.2f}")
    print(f"{lookback}-day High: ${rolling_high:.2f}")
    print(f"Current Volume: {current_volume:,.0f}")
    print(f"Avg Volume({volume_ma_period}): {volume_ma:,.0f}")
    print(f"Volume Surge: {volume_surge:.2f}x (need {surge_pct}x)")
    print()

    price_ok = current_price > rolling_high
    volume_ok = volume_surge > surge_pct

    print(f"{'' if price_ok else ''} Price > {lookback}-day high: {price_ok}")
    print(f"{'' if volume_ok else ''} Volume surge > {surge_pct}x: {volume_ok}")

    if not price_ok:
        gap = rolling_high - current_price
        print(f"Price needs to rise ${gap:.2f} to trigger breakout")

    if not volume_ok:
        needed_volume = volume_ma * surge_pct
        gap = needed_volume - current_volume
        print(f"Volume needs to increase {gap:,.0f} to meet surge requirement")


def diagnose_bollinger(df, params, signal):
    """Diagnose Bollinger breakout strategy."""
    confirmation_bars = params.get("confirmation_bars", 2)
    volume_ma_period = params.get("volume_ma_period", 20)

    current_price = df["close"].iloc[-1]
    bb_upper = df["bb_upper"].iloc[-1]
    bb_lower = df["bb_lower"].iloc[-1]
    bb_middle = df["bb_middle"].iloc[-1]
    current_volume = df["volume"].iloc[-1]
    volume_ma = df[f"volume_ma_{volume_ma_period}"].iloc[-1]

    volume_surge = current_volume / volume_ma if volume_ma > 0 else 0

    print(f"Bollinger Breakout Strategy")
    print(f"─" * 80)
    print(f"Current Price: ${current_price:.2f}")
    print(f"BB Upper: ${bb_upper:.2f}")
    print(f"BB Middle: ${bb_middle:.2f}")
    print(f"BB Lower: ${bb_lower:.2f}")
    print(f"Volume Surge: {volume_surge:.2f}x (need 1.5x)")
    print()

    # Check confirmation
    upper_confirmed = all(
        df["close"].iloc[-i] > df["bb_upper"].iloc[-i]
        for i in range(1, min(confirmation_bars + 1, len(df)))
    )

    print(f"Upper breakout: {'' if upper_confirmed and volume_surge > 1.5 else ''}")
    print(f"   Price > upper band for {confirmation_bars} bars: {upper_confirmed}")
    print(f"   Volume surge > 1.5x: {volume_surge > 1.5}")


def diagnose_vwap(df, params, signal):
    """Diagnose VWAP reversion strategy."""
    deviation_threshold = params.get("deviation_threshold", 2.0)
    rsi_period = params.get("rsi_period", 14)
    oversold = params.get("oversold", 30)
    overbought = params.get("overbought", 70)

    current_price = df["close"].iloc[-1]
    vwap = df["vwap"].iloc[-1]
    vwap_std = df["vwap_std"].iloc[-1]
    rsi = df[f"rsi_{rsi_period}"].iloc[-1]

    upper_band = vwap + (deviation_threshold * vwap_std)
    lower_band = vwap - (deviation_threshold * vwap_std)

    print(f"VWAP Reversion Strategy")
    print(f"─" * 80)
    print(f"Current Price: ${current_price:.2f}")
    print(f"VWAP: ${vwap:.2f}")
    print(f"Upper Band (+{deviation_threshold}σ): ${upper_band:.2f}")
    print(f"Lower Band (-{deviation_threshold}σ): ${lower_band:.2f}")
    print(
        f"RSI({rsi_period}): {rsi:.2f} (oversold<{oversold}, overbought>{overbought})"
    )
    print()

    price_above = current_price > upper_band
    price_below = current_price < lower_band
    rsi_oversold = rsi < oversold
    rsi_overbought = rsi > overbought

    print(f"BUY conditions:")
    print(
        f"{'' if price_below else ''} Price < VWAP - {deviation_threshold}σ: {price_below}"
    )
    print(f"{'' if rsi_oversold else ''} RSI < {oversold}: {rsi_oversold}")
    print()
    print(f"SELL conditions:")
    print(
        f"{'' if price_above else ''} Price > VWAP + {deviation_threshold}σ: {price_above}"
    )
    print(f"{'' if rsi_overbought else ''} RSI > {overbought}: {rsi_overbought}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python diagnose_signals.py <config_path>")
        print()
        print("Examples:")
        print(
            "  python scripts/diagnose_signals.py configs/production/rsi_mean_reversion_SPY.json"
        )
        print(
            "  python scripts/diagnose_signals.py configs/production/ma_crossover_AAPL.json"
        )
        sys.exit(1)

    config_path = sys.argv[1]

    if not os.path.exists(config_path):
        print(f"Config file not found: {config_path}")
        sys.exit(1)

    diagnose_strategy(config_path)
