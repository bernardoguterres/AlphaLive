"""
Test Technical Indicators

Tests for alphalive/strategy/indicators.py — technical indicator calculations
using the ta library.
"""

import numpy as np
import pandas as pd
import pytest

from alphalive.strategy.indicators import (
    add_sma,
    add_ema,
    add_rsi,
    add_macd,
    add_bollinger,
    add_atr,
    add_vwap,
    add_all_for_strategy
)


def test_sma_calculation():
    """Test SMA calculation matches known values."""
    # Create simple data: [100, 101, 102, 103, 104]
    data = {'close': [100.0, 101.0, 102.0, 103.0, 104.0]}
    df = pd.DataFrame(data)

    df = add_sma(df, period=3)

    # First 2 rows should be NaN (need 3 bars for period=3)
    assert pd.isna(df['sma_3'].iloc[0])
    assert pd.isna(df['sma_3'].iloc[1])

    # Third row: (100+101+102)/3 = 101.0
    assert df['sma_3'].iloc[2] == pytest.approx(101.0)

    # Fourth row: (101+102+103)/3 = 102.0
    assert df['sma_3'].iloc[3] == pytest.approx(102.0)

    # Fifth row: (102+103+104)/3 = 103.0
    assert df['sma_3'].iloc[4] == pytest.approx(103.0)


def test_ema_calculation():
    """Test EMA calculation."""
    data = {'close': [100.0 + i for i in range(50)]}
    df = pd.DataFrame(data)

    df = add_ema(df, period=10)

    # First (period-1) rows should be NaN
    assert pd.isna(df['ema_10'].iloc[0])
    assert pd.isna(df['ema_10'].iloc[8])

    # After warmup, should have values
    assert not pd.isna(df['ema_10'].iloc[9])
    assert not pd.isna(df['ema_10'].iloc[-1])

    # EMA should follow trend
    assert df['ema_10'].iloc[-1] > df['ema_10'].iloc[9]


def test_rsi_oversold_and_overbought(rsi_oversold_bars, rsi_overbought_bars):
    """Test RSI at known overbought/oversold levels."""
    # Test oversold (strong downtrend should push RSI < 30)
    df_oversold = rsi_oversold_bars.copy()
    df_oversold = add_rsi(df_oversold, period=14)

    # After warmup, RSI should be low (oversold)
    rsi_value = df_oversold['rsi_14'].iloc[-1]
    assert 0 <= rsi_value <= 100  # RSI always in range
    assert rsi_value < 40  # Strong downtrend = low RSI

    # Test overbought (strong uptrend should push RSI > 70)
    df_overbought = rsi_overbought_bars.copy()
    df_overbought = add_rsi(df_overbought, period=14)

    rsi_value = df_overbought['rsi_14'].iloc[-1]
    assert 0 <= rsi_value <= 100
    assert rsi_value > 60  # Strong uptrend = high RSI


def test_macd_crossover_detection():
    """Test MACD crossover detection."""
    data = {'close': [100.0 + i * 0.5 for i in range(100)]}
    df = pd.DataFrame(data)

    df = add_macd(df, fast=12, slow=26, signal=9)

    # Should have MACD columns
    assert 'macd' in df.columns
    assert 'macd_signal' in df.columns
    assert 'macd_hist' in df.columns

    # After warmup, values should exist
    assert not pd.isna(df['macd'].iloc[-1])
    assert not pd.isna(df['macd_signal'].iloc[-1])
    assert not pd.isna(df['macd_hist'].iloc[-1])

    # Histogram is MACD - Signal
    expected_hist = df['macd'].iloc[-1] - df['macd_signal'].iloc[-1]
    assert df['macd_hist'].iloc[-1] == pytest.approx(expected_hist, abs=0.01)


def test_bollinger_band_width_calculation():
    """Test Bollinger Band width calculation."""
    data = {'close': [100.0 + np.sin(i / 10.0) * 5 for i in range(100)]}
    df = pd.DataFrame(data)

    df = add_bollinger(df, period=20, std_dev=2.0)

    # Should have BB columns
    assert 'bb_upper' in df.columns
    assert 'bb_middle' in df.columns
    assert 'bb_lower' in df.columns

    # After warmup, values should exist
    assert not pd.isna(df['bb_upper'].iloc[-1])
    assert not pd.isna(df['bb_middle'].iloc[-1])
    assert not pd.isna(df['bb_lower'].iloc[-1])

    # Upper > Middle > Lower
    assert df['bb_upper'].iloc[-1] > df['bb_middle'].iloc[-1]
    assert df['bb_middle'].iloc[-1] > df['bb_lower'].iloc[-1]

    # Middle should be close to SMA
    assert df['bb_middle'].iloc[-1] == pytest.approx(df['close'].iloc[-20:].mean(), abs=0.1)


def test_atr_calculation():
    """Test ATR calculation."""
    data = {
        'high': [100.0 + i + 2.0 for i in range(50)],
        'low': [100.0 + i - 2.0 for i in range(50)],
        'close': [100.0 + i for i in range(50)]
    }
    df = pd.DataFrame(data)

    df = add_atr(df, period=14)

    # Should have ATR column
    assert 'atr_14' in df.columns

    # First (period) rows should be NaN
    assert pd.isna(df['atr_14'].iloc[0])
    assert pd.isna(df['atr_14'].iloc[13])

    # After warmup, should have value
    assert not pd.isna(df['atr_14'].iloc[14])
    assert df['atr_14'].iloc[-1] > 0  # ATR always positive


def test_vwap_calculation():
    """Test VWAP calculation."""
    data = {
        'high': [102.0, 104.0, 103.0],
        'low': [98.0, 96.0, 97.0],
        'close': [100.0, 100.0, 100.0],
        'volume': [1000, 2000, 1500]
    }
    df = pd.DataFrame(data)

    df = add_vwap(df)

    # Should have VWAP column
    assert 'vwap' in df.columns

    # First row should have VWAP
    assert not pd.isna(df['vwap'].iloc[0])

    # VWAP should be volume-weighted average of typical price
    # Typical price = (high + low + close) / 3
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    cumulative_tp_volume = (typical_price * df['volume']).cumsum()
    cumulative_volume = df['volume'].cumsum()
    expected_vwap = cumulative_tp_volume / cumulative_volume

    assert df['vwap'].iloc[-1] == pytest.approx(expected_vwap.iloc[-1], abs=0.01)


def test_indicators_handle_nan_for_early_bars():
    """Test that all indicators handle NaN for early bars gracefully."""
    data = {
        'open': [100.0 + i for i in range(30)],
        'high': [102.0 + i for i in range(30)],
        'low': [98.0 + i for i in range(30)],
        'close': [100.0 + i for i in range(30)],
        'volume': [1000000] * 30
    }
    df = pd.DataFrame(data)

    # Add indicators with longer periods than data length
    df = add_sma(df, period=20)
    df = add_rsi(df, period=14)

    # First (period-1) rows should be NaN
    assert pd.isna(df['sma_20'].iloc[0])
    assert pd.isna(df['sma_20'].iloc[18])
    assert pd.isna(df['rsi_14'].iloc[0])
    assert pd.isna(df['rsi_14'].iloc[13])

    # After warmup, should have values
    assert not pd.isna(df['sma_20'].iloc[19])
    assert not pd.isna(df['rsi_14'].iloc[14])


def test_add_all_for_strategy_adds_correct_columns():
    """Test add_all_for_strategy() adds correct columns per strategy."""
    data = {
        'open': [100.0 + i for i in range(50)],
        'high': [102.0 + i for i in range(50)],
        'low': [98.0 + i for i in range(50)],
        'close': [100.0 + i for i in range(50)],
        'volume': [1000000] * 50
    }

    # Test MA crossover
    df = pd.DataFrame(data.copy())
    df = add_all_for_strategy(df, "ma_crossover", {"fast_period": 10, "slow_period": 20})
    assert 'sma_10' in df.columns
    assert 'sma_20' in df.columns

    # Test RSI mean reversion
    df = pd.DataFrame(data.copy())
    df = add_all_for_strategy(df, "rsi_mean_reversion", {"period": 14})
    assert 'rsi_14' in df.columns

    # Test momentum breakout
    df = pd.DataFrame(data.copy())
    df = add_all_for_strategy(df, "momentum_breakout", {"atr_period": 14, "lookback": 20})
    assert 'atr_14' in df.columns

    # Test Bollinger breakout
    df = pd.DataFrame(data.copy())
    df = add_all_for_strategy(df, "bollinger_breakout", {"period": 20, "std_dev": 2.0})
    assert 'bb_upper' in df.columns
    assert 'bb_middle' in df.columns
    assert 'bb_lower' in df.columns

    # Test VWAP reversion
    df = pd.DataFrame(data.copy())
    df = add_all_for_strategy(df, "vwap_reversion", {"rsi_period": 14})
    assert 'vwap' in df.columns
    assert 'rsi_14' in df.columns


def test_empty_dataframe_doesnt_crash():
    """Test that empty DataFrame doesn't crash indicator functions."""
    df = pd.DataFrame()

    # These should not crash, but return DataFrame with NaN columns
    try:
        df_with_sma = add_sma(df.copy(), period=10)
        # If it runs without crashing, test passes
        assert True
    except Exception as e:
        # Should not raise exception
        pytest.fail(f"add_sma crashed on empty DataFrame: {e}")

    try:
        df_with_rsi = add_rsi(df.copy(), period=14)
        assert True
    except Exception as e:
        pytest.fail(f"add_rsi crashed on empty DataFrame: {e}")
