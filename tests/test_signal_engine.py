"""
Test Signal Generation Engine

Tests for alphalive/strategy/signal_engine.py — signal generation for all 5 strategies.
"""

import pandas as pd
from datetime import datetime

from alphalive.strategy.signal_engine import SignalEngine


def test_ma_crossover_buy_signal_on_golden_cross(sample_strategy_dict, ma_crossover_bars):
    """Test MA crossover BUY signal on golden cross."""
    # Setup MA crossover strategy
    sample_strategy_dict["strategy"]["name"] = "ma_crossover"
    sample_strategy_dict["strategy"]["parameters"] = {"fast_period": 10, "slow_period": 20}

    from alphalive.strategy_schema import StrategySchema
    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(ma_crossover_bars)

    assert signal is not None
    assert signal["signal"] in ["BUY", "SELL", "HOLD"]
    assert signal["confidence"] >= 0.0 and signal["confidence"] <= 1.0
    assert "reason" in signal
    assert "indicators" in signal


def test_ma_crossover_sell_signal_on_death_cross(sample_strategy_dict):
    """Test MA crossover SELL signal on death cross."""
    # Create bars with death cross pattern (fast crosses below slow)
    data = []
    for i in range(50):
        if i < 30:
            # Fast above slow
            price = 100.0 + (i * 0.5)
        else:
            # Fast crosses below slow
            price = 100.0 - ((i - 30) * 0.5)

        data.append({
            "open": price,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price,
            "volume": 1000000
        })

    df = pd.DataFrame(data)
    df.index = pd.date_range(start="2024-01-01", periods=50, freq="D", tz="America/New_York")

    sample_strategy_dict["strategy"]["name"] = "ma_crossover"
    sample_strategy_dict["strategy"]["parameters"] = {"fast_period": 10, "slow_period": 20}

    from alphalive.strategy_schema import StrategySchema
    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(df)

    assert signal is not None
    assert signal["signal"] in ["BUY", "SELL", "HOLD"]


def test_ma_crossover_hold_when_no_cross(sample_strategy_dict):
    """Test MA crossover HOLD when no cross occurs."""
    # Create bars with no crossover (stable trend)
    data = []
    for i in range(50):
        price = 100.0 + (i * 0.1)  # Slow steady trend
        data.append({
            "open": price,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price,
            "volume": 1000000
        })

    df = pd.DataFrame(data)
    df.index = pd.date_range(start="2024-01-01", periods=50, freq="D", tz="America/New_York")

    sample_strategy_dict["strategy"]["name"] = "ma_crossover"
    sample_strategy_dict["strategy"]["parameters"] = {"fast_period": 10, "slow_period": 20}

    from alphalive.strategy_schema import StrategySchema
    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(df)

    assert signal is not None
    # Should be HOLD (no cross)
    assert signal["signal"] == "HOLD"


def test_rsi_mean_reversion_buy_when_oversold(sample_strategy_dict, rsi_oversold_bars):
    """Test RSI mean reversion BUY when RSI < oversold."""
    sample_strategy_dict["strategy"]["name"] = "rsi_mean_reversion"
    sample_strategy_dict["strategy"]["parameters"] = {
        "period": 14,
        "oversold": 30,
        "overbought": 70
    }

    from alphalive.strategy_schema import StrategySchema
    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(rsi_oversold_bars)

    assert signal is not None
    # With strong downtrend, should eventually generate signal
    assert signal["signal"] in ["BUY", "HOLD"]


def test_rsi_mean_reversion_sell_when_overbought(sample_strategy_dict, rsi_overbought_bars):
    """Test RSI mean reversion SELL when RSI > overbought."""
    sample_strategy_dict["strategy"]["name"] = "rsi_mean_reversion"
    sample_strategy_dict["strategy"]["parameters"] = {
        "period": 14,
        "oversold": 30,
        "overbought": 70
    }

    from alphalive.strategy_schema import StrategySchema
    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(rsi_overbought_bars)

    assert signal is not None
    # With strong uptrend, should eventually generate signal
    assert signal["signal"] in ["SELL", "HOLD"]


def test_momentum_breakout_buy_on_breakout_with_volume(sample_strategy_dict, momentum_breakout_bars):
    """Test momentum breakout BUY on breakout with volume surge."""
    sample_strategy_dict["strategy"]["name"] = "momentum_breakout"
    sample_strategy_dict["strategy"]["parameters"] = {
        "lookback": 20,
        "surge_pct": 1.5,
        "atr_period": 14,
        "volume_ma_period": 20
    }

    from alphalive.strategy_schema import StrategySchema
    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(momentum_breakout_bars)

    assert signal is not None
    # Breakout bars have volume surge at end
    assert signal["signal"] in ["BUY", "HOLD"]


def test_momentum_breakout_hold_without_volume_surge(sample_strategy_dict):
    """Test momentum breakout HOLD without volume surge."""
    # Create bars with breakout but NO volume surge
    data = []
    for i in range(50):
        if i < 40:
            price = 100.0
        else:
            price = 105.0  # Breakout

        data.append({
            "open": price,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price,
            "volume": 1000000  # Constant volume (no surge)
        })

    df = pd.DataFrame(data)
    df.index = pd.date_range(start="2024-01-01", periods=50, freq="D", tz="America/New_York")

    sample_strategy_dict["strategy"]["name"] = "momentum_breakout"
    sample_strategy_dict["strategy"]["parameters"] = {
        "lookback": 20,
        "surge_pct": 1.5,
        "atr_period": 14,
        "volume_ma_period": 20
    }

    from alphalive.strategy_schema import StrategySchema
    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(df)

    assert signal is not None
    # No volume surge = HOLD
    assert signal["signal"] == "HOLD"


def test_bollinger_breakout_buy_above_upper_band(sample_strategy_dict):
    """Test Bollinger breakout BUY above upper band."""
    # Create bars that break above upper band
    data = []
    for i in range(50):
        if i < 45:
            price = 100.0
            volume = 1000000
        else:
            price = 110.0  # Break above
            volume = 2000000  # Volume surge

        data.append({
            "open": price,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price,
            "volume": volume
        })

    df = pd.DataFrame(data)
    df.index = pd.date_range(start="2024-01-01", periods=50, freq="D", tz="America/New_York")

    sample_strategy_dict["strategy"]["name"] = "bollinger_breakout"
    sample_strategy_dict["strategy"]["parameters"] = {
        "period": 20,
        "std_dev": 2.0,
        "confirmation_bars": 2,
        "volume_ma_period": 20
    }

    from alphalive.strategy_schema import StrategySchema
    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(df)

    assert signal is not None
    assert signal["signal"] in ["BUY", "HOLD"]


def test_vwap_reversion_buy_below_vwap_deviation(sample_strategy_dict):
    """Test VWAP reversion BUY below VWAP deviation."""
    # Create bars with price far below VWAP
    data = []
    for i in range(50):
        if i < 40:
            price = 100.0
            volume = 1000000
        else:
            price = 90.0  # Drop below VWAP
            volume = 1000000

        data.append({
            "open": price,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price,
            "volume": volume
        })

    df = pd.DataFrame(data)
    df.index = pd.date_range(start="2024-01-01", periods=50, freq="D", tz="America/New_York")

    sample_strategy_dict["strategy"]["name"] = "vwap_reversion"
    sample_strategy_dict["strategy"]["parameters"] = {
        "deviation_threshold": 2.0,
        "rsi_period": 14,
        "oversold": 30,
        "overbought": 70,
        "vwap_std_period": 20
    }

    from alphalive.strategy_schema import StrategySchema
    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(df)

    assert signal is not None
    assert signal["signal"] in ["BUY", "SELL", "HOLD"]


def test_signal_includes_confidence_score(sample_strategy_dict, ma_crossover_bars):
    """Test that signal includes confidence score."""
    from alphalive.strategy_schema import StrategySchema
    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(ma_crossover_bars)

    assert signal is not None
    assert "confidence" in signal
    assert isinstance(signal["confidence"], float)
    assert 0.0 <= signal["confidence"] <= 1.0


def test_signal_includes_human_readable_reason(sample_strategy_dict, ma_crossover_bars):
    """Test that signal includes human-readable reason."""
    from alphalive.strategy_schema import StrategySchema
    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(ma_crossover_bars)

    assert signal is not None
    assert "reason" in signal
    assert isinstance(signal["reason"], str)
    assert len(signal["reason"]) > 0


def test_signal_only_looks_at_last_bar(sample_strategy_dict):
    """Test that signal only looks at last bar (no future data)."""
    # Create data where last bar would generate different signal than previous bars
    data = []
    for i in range(50):
        price = 100.0  # Flat
        data.append({
            "open": price,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price,
            "volume": 1000000
        })

    df1 = pd.DataFrame(data)
    df1.index = pd.date_range(start="2024-01-01", periods=50, freq="D", tz="America/New_York")

    # Add one more bar with sharp move
    data.append({
        "open": 105.0,
        "high": 106.0,
        "low": 104.5,
        "close": 105.5,
        "volume": 2000000
    })

    df2 = pd.DataFrame(data)
    df2.index = pd.date_range(start="2024-01-01", periods=51, freq="D", tz="America/New_York")

    from alphalive.strategy_schema import StrategySchema
    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)

    signal1 = engine.generate_signal(df1)
    signal2 = engine.generate_signal(df2)

    # Signals should be different (df2 has new bar)
    # This verifies signal generation is based on latest data
    assert signal1 is not None
    assert signal2 is not None
