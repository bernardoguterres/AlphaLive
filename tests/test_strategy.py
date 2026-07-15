"""
Test Strategy Components

Tests for indicators and signal generation engine.
"""

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from alphalive.strategy.indicators import (
    add_sma,
    add_rsi,
    add_adx,
    add_vwap,
    add_obv,
    add_all_for_strategy,
)


@pytest.fixture
def sample_ohlcv_data():
    """Create sample OHLCV data for testing."""
    dates = pd.date_range(start="2024-01-01", periods=100, freq="D")
    np.random.seed(42)

    # Generate realistic price data
    close_prices = 100 + np.cumsum(np.random.randn(100) * 2)
    high_prices = close_prices + np.random.rand(100) * 2
    low_prices = close_prices - np.random.rand(100) * 2
    open_prices = close_prices + np.random.randn(100) * 1

    df = pd.DataFrame(
        {
            "timestamp": dates,
            "open": open_prices,
            "high": high_prices,
            "low": low_prices,
            "close": close_prices,
            "volume": np.random.randint(1000000, 5000000, 100),
        }
    )

    return df


def test_add_adx(sample_ohlcv_data):
    """Test ADX calculation."""
    df = add_adx(sample_ohlcv_data, period=14)

    assert "adx_14" in df.columns
    # ADX should be between 0 and 100
    valid_adx = df["adx_14"].dropna()
    assert (valid_adx >= 0).all()
    assert (valid_adx <= 100).all()


def test_add_vwap(sample_ohlcv_data):
    """Test VWAP calculation."""
    df = add_vwap(sample_ohlcv_data)

    assert "vwap" in df.columns
    # VWAP should be positive
    valid_vwap = df["vwap"].dropna()
    assert (valid_vwap > 0).all()
    # VWAP should be in reasonable range relative to price
    assert (valid_vwap < df["high"].max() * 1.5).all()
    assert (valid_vwap > df["low"].min() * 0.5).all()


def test_add_obv(sample_ohlcv_data):
    """Test OBV calculation."""
    df = add_obv(sample_ohlcv_data)

    assert "obv" in df.columns
    # OBV is cumulative, should have values
    assert df["obv"].notna().sum() > 0


def test_add_all_for_ma_crossover(sample_ohlcv_data):
    """Test adding indicators for MA crossover strategy."""
    params = {"fast_period": 10, "slow_period": 20}
    df = add_all_for_strategy(sample_ohlcv_data, "ma_crossover", params)

    assert "sma_10" in df.columns
    assert "sma_20" in df.columns


def test_add_all_for_rsi_mean_reversion(sample_ohlcv_data):
    """Test adding indicators for RSI mean reversion strategy."""
    params = {"period": 14}
    df = add_all_for_strategy(sample_ohlcv_data, "rsi_mean_reversion", params)

    assert "rsi_14" in df.columns


def test_add_all_for_momentum_breakout(sample_ohlcv_data):
    """Test adding indicators for momentum breakout strategy."""
    params = {"lookback": 20, "atr_period": 14, "volume_ma_period": 20}
    df = add_all_for_strategy(sample_ohlcv_data, "momentum_breakout", params)

    assert "atr_14" in df.columns
    assert "rolling_high" in df.columns
    assert "volume_ma_20" in df.columns


def test_add_all_for_bollinger_breakout(sample_ohlcv_data):
    """Test adding indicators for Bollinger breakout strategy."""
    params = {"period": 20, "std_dev": 2.0, "volume_ma_period": 20}
    df = add_all_for_strategy(sample_ohlcv_data, "bollinger_breakout", params)

    assert "bb_upper" in df.columns
    assert "bb_middle" in df.columns
    assert "bb_lower" in df.columns
    assert "volume_ma_20" in df.columns


def test_add_all_for_vwap_reversion(sample_ohlcv_data):
    """Test adding indicators for VWAP reversion strategy."""
    params = {"rsi_period": 14, "vwap_std_period": 20}
    df = add_all_for_strategy(sample_ohlcv_data, "vwap_reversion", params)

    assert "vwap" in df.columns
    assert "rsi_14" in df.columns
    assert "vwap_std" in df.columns


def test_add_all_for_unknown_strategy(sample_ohlcv_data):
    """Test that unknown strategy raises ValueError."""
    with pytest.raises(ValueError, match="Unknown strategy"):
        add_all_for_strategy(sample_ohlcv_data, "unknown_strategy", {})


def test_nan_handling(sample_ohlcv_data):
    """Test that indicators handle NaN gracefully."""
    # Take only first 5 rows (not enough for most indicators)
    df_short = sample_ohlcv_data.head(5)

    # Should not raise errors, just have NaN values
    df = add_sma(df_short, period=20)
    assert df["sma_20"].isna().all()

    df = add_rsi(df_short, period=14)
    assert df["rsi_14"].isna().all()
