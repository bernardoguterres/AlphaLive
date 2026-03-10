"""
Technical Indicators

Calculates technical indicators using the ta library and pandas.
All functions operate on DataFrames with OHLCV columns (lowercase).

Performance: All functions use vectorized pandas/numpy operations.
Expected: <0.5s for 200 bars on Railway's shared vCPU.
"""

import logging
from typing import Dict, Any

import pandas as pd
import numpy as np
from ta.trend import SMAIndicator, EMAIndicator, ADXIndicator
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands, AverageTrueRange
from ta.trend import MACD
from ta.volume import OnBalanceVolumeIndicator

logger = logging.getLogger(__name__)


def add_sma(df: pd.DataFrame, period: int) -> pd.DataFrame:
    """
    Add Simple Moving Average.

    Args:
        df: DataFrame with 'close' column
        period: SMA period

    Returns:
        DataFrame with f"sma_{period}" column added

    Note:
        First (period - 1) rows will be NaN.
    """
    try:
        indicator = SMAIndicator(close=df['close'], window=period)
        df[f"sma_{period}"] = indicator.sma_indicator()
        logger.debug(f"Added SMA_{period}")
        return df
    except Exception as e:
        logger.error(f"Failed to calculate SMA_{period}: {e}")
        df[f"sma_{period}"] = np.nan
        return df


def add_ema(df: pd.DataFrame, period: int) -> pd.DataFrame:
    """
    Add Exponential Moving Average.

    Args:
        df: DataFrame with 'close' column
        period: EMA period

    Returns:
        DataFrame with f"ema_{period}" column added

    Note:
        First (period - 1) rows will be NaN.
    """
    try:
        indicator = EMAIndicator(close=df['close'], window=period)
        df[f"ema_{period}"] = indicator.ema_indicator()
        logger.debug(f"Added EMA_{period}")
        return df
    except Exception as e:
        logger.error(f"Failed to calculate EMA_{period}: {e}")
        df[f"ema_{period}"] = np.nan
        return df


def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    Add Relative Strength Index.

    Args:
        df: DataFrame with 'close' column
        period: RSI period (default 14)

    Returns:
        DataFrame with f"rsi_{period}" column added

    Note:
        First (period) rows will be NaN.
        RSI ranges from 0 to 100.
    """
    try:
        indicator = RSIIndicator(close=df['close'], window=period)
        df[f"rsi_{period}"] = indicator.rsi()
        logger.debug(f"Added RSI_{period}")
        return df
    except Exception as e:
        logger.error(f"Failed to calculate RSI_{period}: {e}")
        df[f"rsi_{period}"] = np.nan
        return df


def add_macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9
) -> pd.DataFrame:
    """
    Add MACD (Moving Average Convergence Divergence).

    Args:
        df: DataFrame with 'close' column
        fast: Fast EMA period (default 12)
        slow: Slow EMA period (default 26)
        signal: Signal line period (default 9)

    Returns:
        DataFrame with "macd", "macd_signal", "macd_hist" columns added

    Note:
        First (slow + signal - 1) rows will be NaN.
    """
    try:
        indicator = MACD(
            close=df['close'],
            window_fast=fast,
            window_slow=slow,
            window_sign=signal
        )
        df["macd"] = indicator.macd()
        df["macd_signal"] = indicator.macd_signal()
        df["macd_hist"] = indicator.macd_diff()
        logger.debug(f"Added MACD({fast},{slow},{signal})")
        return df
    except Exception as e:
        logger.error(f"Failed to calculate MACD: {e}")
        df["macd"] = np.nan
        df["macd_signal"] = np.nan
        df["macd_hist"] = np.nan
        return df


def add_bollinger(
    df: pd.DataFrame,
    period: int = 20,
    std_dev: float = 2.0
) -> pd.DataFrame:
    """
    Add Bollinger Bands.

    Args:
        df: DataFrame with 'close' column
        period: MA period (default 20)
        std_dev: Standard deviation multiplier (default 2.0)

    Returns:
        DataFrame with "bb_upper", "bb_middle", "bb_lower" columns added

    Note:
        First (period - 1) rows will be NaN.
    """
    try:
        indicator = BollingerBands(
            close=df['close'],
            window=period,
            window_dev=std_dev
        )
        df["bb_upper"] = indicator.bollinger_hband()
        df["bb_middle"] = indicator.bollinger_mavg()
        df["bb_lower"] = indicator.bollinger_lband()
        logger.debug(f"Added Bollinger Bands({period},{std_dev})")
        return df
    except Exception as e:
        logger.error(f"Failed to calculate Bollinger Bands: {e}")
        df["bb_upper"] = np.nan
        df["bb_middle"] = np.nan
        df["bb_lower"] = np.nan
        return df


def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    Add Average True Range.

    Args:
        df: DataFrame with 'high', 'low', 'close' columns
        period: ATR period (default 14)

    Returns:
        DataFrame with f"atr_{period}" column added

    Note:
        First (period) rows will be NaN.
    """
    try:
        indicator = AverageTrueRange(
            high=df['high'],
            low=df['low'],
            close=df['close'],
            window=period
        )
        df[f"atr_{period}"] = indicator.average_true_range()
        logger.debug(f"Added ATR_{period}")
        return df
    except Exception as e:
        logger.error(f"Failed to calculate ATR_{period}: {e}")
        df[f"atr_{period}"] = np.nan
        return df


def add_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    Add Average Directional Index.

    Args:
        df: DataFrame with 'high', 'low', 'close' columns
        period: ADX period (default 14)

    Returns:
        DataFrame with f"adx_{period}" column added

    Note:
        First (period * 2) rows will be NaN.
        ADX ranges from 0 to 100.
    """
    try:
        indicator = ADXIndicator(
            high=df['high'],
            low=df['low'],
            close=df['close'],
            window=period
        )
        df[f"adx_{period}"] = indicator.adx()
        logger.debug(f"Added ADX_{period}")
        return df
    except Exception as e:
        logger.error(f"Failed to calculate ADX_{period}: {e}")
        df[f"adx_{period}"] = np.nan
        return df


def add_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add Volume Weighted Average Price.

    Args:
        df: DataFrame with 'high', 'low', 'close', 'volume' columns

    Returns:
        DataFrame with "vwap" column added

    Formula:
        VWAP = cumsum(typical_price * volume) / cumsum(volume)
        where typical_price = (high + low + close) / 3

    Note:
        First row will be NaN if volume is 0.
        VWAP is cumulative, so it uses all data from start.
    """
    try:
        # Typical price
        typical_price = (df['high'] + df['low'] + df['close']) / 3

        # VWAP = cumulative(typical_price * volume) / cumulative(volume)
        df['vwap'] = (typical_price * df['volume']).cumsum() / df['volume'].cumsum()

        logger.debug("Added VWAP")
        return df
    except Exception as e:
        logger.error(f"Failed to calculate VWAP: {e}")
        df['vwap'] = np.nan
        return df


def add_obv(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add On-Balance Volume.

    Args:
        df: DataFrame with 'close', 'volume' columns

    Returns:
        DataFrame with "obv" column added

    Note:
        OBV is cumulative from start of data.
    """
    try:
        indicator = OnBalanceVolumeIndicator(
            close=df['close'],
            volume=df['volume']
        )
        df['obv'] = indicator.on_balance_volume()
        logger.debug("Added OBV")
        return df
    except Exception as e:
        logger.error(f"Failed to calculate OBV: {e}")
        df['obv'] = np.nan
        return df


def add_all_for_strategy(
    df: pd.DataFrame,
    strategy_name: str,
    params: Dict[str, Any]
) -> pd.DataFrame:
    """
    Add all indicators needed for a specific strategy.

    This is the main function to call. It adds only the indicators
    required by the strategy to minimize computation time.

    Args:
        df: DataFrame with OHLCV columns
        strategy_name: Strategy name (e.g., "ma_crossover")
        params: Strategy parameters dict

    Returns:
        DataFrame with all required indicators added

    Raises:
        ValueError: If strategy_name is unknown

    Performance:
        Expected <0.3s for 200 bars on Railway.
    """
    if strategy_name == "ma_crossover":
        # Needs: SMA (fast and slow periods)
        fast_period = params.get("fast_period", 10)
        slow_period = params.get("slow_period", 20)
        df = add_sma(df, fast_period)
        df = add_sma(df, slow_period)
        logger.debug(f"Added indicators for ma_crossover: SMA_{fast_period}, SMA_{slow_period}")

    elif strategy_name == "rsi_mean_reversion":
        # Needs: RSI
        period = params.get("period", 14)
        df = add_rsi(df, period)
        logger.debug(f"Added indicators for rsi_mean_reversion: RSI_{period}")

    elif strategy_name == "momentum_breakout":
        # Needs: ATR (for trailing stop), rolling high, volume MA
        atr_period = params.get("atr_period", 14)
        df = add_atr(df, atr_period)

        # Add rolling high for breakout detection
        lookback = params.get("lookback", 20)
        df['rolling_high'] = df['high'].rolling(window=lookback).max()

        # Add volume MA for surge detection
        volume_ma_period = params.get("volume_ma_period", 20)
        df[f'volume_ma_{volume_ma_period}'] = df['volume'].rolling(window=volume_ma_period).mean()

        logger.debug(
            f"Added indicators for momentum_breakout: "
            f"ATR_{atr_period}, rolling_high_{lookback}, volume_ma_{volume_ma_period}"
        )

    elif strategy_name == "bollinger_breakout":
        # Needs: Bollinger Bands, volume MA
        period = params.get("period", 20)
        std_dev = params.get("std_dev", 2.0)
        df = add_bollinger(df, period, std_dev)

        # Add volume MA for confirmation
        volume_ma_period = params.get("volume_ma_period", 20)
        df[f'volume_ma_{volume_ma_period}'] = df['volume'].rolling(window=volume_ma_period).mean()

        logger.debug(
            f"Added indicators for bollinger_breakout: "
            f"BB({period},{std_dev}), volume_ma_{volume_ma_period}"
        )

    elif strategy_name == "vwap_reversion":
        # Needs: VWAP, RSI, std dev of price from VWAP
        df = add_vwap(df)

        rsi_period = params.get("rsi_period", 14)
        df = add_rsi(df, rsi_period)

        # Calculate standard deviation of price from VWAP
        vwap_std_period = params.get("vwap_std_period", 20)
        df['vwap_std'] = (df['close'] - df['vwap']).rolling(window=vwap_std_period).std()

        logger.debug(
            f"Added indicators for vwap_reversion: "
            f"VWAP, RSI_{rsi_period}, vwap_std_{vwap_std_period}"
        )

    else:
        raise ValueError(
            f"Unknown strategy: {strategy_name}. "
            f"Supported: ma_crossover, rsi_mean_reversion, momentum_breakout, "
            f"bollinger_breakout, vwap_reversion"
        )

    return df


def calculate_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate ALL available indicators (for testing/analysis).

    Warning: This is slower than add_all_for_strategy.
    Only use for testing or manual analysis.

    Args:
        df: DataFrame with OHLCV columns

    Returns:
        DataFrame with all indicators added
    """
    logger.info("Calculating all indicators (this is slow - use add_all_for_strategy in production)")

    # Moving averages
    for period in [10, 20, 50, 100, 200]:
        df = add_sma(df, period)
        df = add_ema(df, period)

    # Momentum
    for period in [7, 14, 21]:
        df = add_rsi(df, period)

    # MACD
    df = add_macd(df)

    # Volatility
    for period in [10, 14, 20]:
        df = add_bollinger(df, period)
        df = add_atr(df, period)

    # Trend
    for period in [14, 20]:
        df = add_adx(df, period)

    # Volume
    df = add_vwap(df)
    df = add_obv(df)

    logger.info(f"Added {len(df.columns) - 5} indicator columns")  # -5 for OHLCV
    return df
