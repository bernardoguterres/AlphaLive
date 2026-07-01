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
from ta.trend import SMAIndicator, EMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands, AverageTrueRange

logger = logging.getLogger(__name__)


def add_sma(df: pd.DataFrame, period: int) -> pd.DataFrame:
    """Add sma_{period} column. Returns a copy; first (period-1) rows are NaN."""
    try:
        indicator = SMAIndicator(close=df["close"], window=period)
        df = df.copy()
        df[f"sma_{period}"] = indicator.sma_indicator()
        logger.debug(f"Added SMA_{period}")
        return df
    except Exception as e:
        logger.error(f"Failed to calculate SMA_{period}: {e}")
        df[f"sma_{period}"] = np.nan
        return df


def add_ema(df: pd.DataFrame, period: int) -> pd.DataFrame:
    """Add ema_{period} column. Returns a copy; first (period-1) rows are NaN."""
    try:
        indicator = EMAIndicator(close=df["close"], window=period)
        df = df.copy()
        df[f"ema_{period}"] = indicator.ema_indicator()
        logger.debug(f"Added EMA_{period}")
        return df
    except Exception as e:
        logger.error(f"Failed to calculate EMA_{period}: {e}")
        df[f"ema_{period}"] = np.nan
        return df


def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Add rsi_{period} column. Returns a copy; first period rows are NaN."""
    try:
        indicator = RSIIndicator(close=df["close"], window=period)
        df = df.copy()
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
    signal: int = 9,
) -> pd.DataFrame:
    """Add macd, macd_signal, macd_hist columns. First (slow+signal-1) rows are NaN."""
    try:
        from ta.trend import MACD  # noqa: PLC0415

        indicator = MACD(
            close=df["close"], window_fast=fast, window_slow=slow, window_sign=signal
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
    df: pd.DataFrame, period: int = 20, std_dev: float = 2.0
) -> pd.DataFrame:
    """Add bb_upper, bb_middle, bb_lower columns. First (period-1) rows are NaN."""
    try:
        indicator = BollingerBands(close=df["close"], window=period, window_dev=std_dev)
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
    """Add atr_{period} column. First period rows are NaN."""
    try:
        indicator = AverageTrueRange(
            high=df["high"], low=df["low"], close=df["close"], window=period
        )
        df[f"atr_{period}"] = indicator.average_true_range()
        logger.debug(f"Added ATR_{period}")
        return df
    except Exception as e:
        logger.error(f"Failed to calculate ATR_{period}: {e}")
        df[f"atr_{period}"] = np.nan
        return df


def add_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Add adx_{period} column. First (period*2) rows are NaN."""
    try:
        from ta.trend import ADXIndicator  # noqa: PLC0415

        indicator = ADXIndicator(
            high=df["high"], low=df["low"], close=df["close"], window=period
        )
        df[f"adx_{period}"] = indicator.adx()
        logger.debug(f"Added ADX_{period}")
        return df
    except Exception as e:
        logger.error(f"Failed to calculate ADX_{period}: {e}")
        df[f"adx_{period}"] = np.nan
        return df


def add_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """Add cumulative vwap column. Cumulative from start of data; NaN on zero-volume rows."""
    try:
        # Typical price
        typical_price = (df["high"] + df["low"] + df["close"]) / 3

        # VWAP = cumulative(typical_price * volume) / cumulative(volume)
        df["vwap"] = (typical_price * df["volume"]).cumsum() / df["volume"].cumsum()

        logger.debug("Added VWAP")
        return df
    except Exception as e:
        logger.error(f"Failed to calculate VWAP: {e}")
        df["vwap"] = np.nan
        return df


def add_obv(df: pd.DataFrame) -> pd.DataFrame:
    """Add cumulative obv column."""
    try:
        from ta.volume import OnBalanceVolumeIndicator  # noqa: PLC0415

        indicator = OnBalanceVolumeIndicator(close=df["close"], volume=df["volume"])
        df["obv"] = indicator.on_balance_volume()
        logger.debug("Added OBV")
        return df
    except Exception as e:
        logger.error(f"Failed to calculate OBV: {e}")
        df["obv"] = np.nan
        return df


def _indicators_ma_crossover(df: pd.DataFrame, params: Dict[str, Any]) -> pd.DataFrame:
    fast_period = params.get("fast_period", 10)
    slow_period = params.get("slow_period", 20)
    df = add_sma(df, fast_period)
    df = add_sma(df, slow_period)
    logger.debug(
        f"Added indicators for ma_crossover: SMA_{fast_period}, SMA_{slow_period}"
    )
    return df


def _indicators_rsi_mean_reversion(
    df: pd.DataFrame, params: Dict[str, Any]
) -> pd.DataFrame:
    period = params.get("period", 14)
    df = add_rsi(df, period)
    logger.debug(f"Added indicators for rsi_mean_reversion: RSI_{period}")
    return df


def _indicators_momentum_breakout(
    df: pd.DataFrame, params: Dict[str, Any]
) -> pd.DataFrame:
    atr_period = params.get("atr_period", 14)
    df = add_atr(df, atr_period)
    # Shifted: use PREVIOUS bar's max to match AlphaLab's high_n.iloc[i-1] logic
    lookback = params.get("lookback", 20)
    df["rolling_high"] = df["high"].rolling(window=lookback).max().shift(1)
    volume_ma_period = params.get("volume_ma_period", 20)
    df[f"volume_ma_{volume_ma_period}"] = (
        df["volume"].rolling(window=volume_ma_period).mean()
    )
    logger.debug(
        f"Added indicators for momentum_breakout: "
        f"ATR_{atr_period}, rolling_high_{lookback}, volume_ma_{volume_ma_period}"
    )
    return df


def _indicators_bollinger_breakout(
    df: pd.DataFrame, params: Dict[str, Any]
) -> pd.DataFrame:
    period = params.get("period", 20)
    std_dev = params.get("std_dev", 2.0)
    df = add_bollinger(df, period, std_dev)
    volume_ma_period = params.get("volume_ma_period", 20)
    df[f"volume_ma_{volume_ma_period}"] = (
        df["volume"].rolling(window=volume_ma_period).mean()
    )
    logger.debug(
        f"Added indicators for bollinger_breakout: BB({period},{std_dev}), volume_ma_{volume_ma_period}"
    )
    return df


def _indicators_vwap_reversion(
    df: pd.DataFrame, params: Dict[str, Any]
) -> pd.DataFrame:
    df = add_vwap(df)
    rsi_period = params.get("rsi_period", 14)
    df = add_rsi(df, rsi_period)
    vwap_std_period = params.get("vwap_std_period", 20)
    df["vwap_std"] = (df["close"] - df["vwap"]).rolling(window=vwap_std_period).std()
    logger.debug(
        f"Added indicators for vwap_reversion: VWAP, RSI_{rsi_period}, vwap_std_{vwap_std_period}"
    )
    return df


def _indicators_bollinger_rsi_combo(
    df: pd.DataFrame, params: Dict[str, Any]
) -> pd.DataFrame:
    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.0)
    df = add_bollinger(df, bb_period, bb_std)
    rsi_period = params.get("rsi_period", 14)
    df = add_rsi(df, rsi_period)
    logger.debug(
        f"Added indicators for bollinger_rsi_combo: BB({bb_period},{bb_std}), RSI_{rsi_period}"
    )
    return df


def _indicators_trend_adaptive_rsi(
    df: pd.DataFrame, params: Dict[str, Any]
) -> pd.DataFrame:
    trend_sma = params.get("trend_sma", 50)
    df = add_sma(df, trend_sma)
    rsi_period = params.get("rsi_period", 14)
    df = add_rsi(df, rsi_period)
    logger.debug(
        f"Added indicators for trend_adaptive_rsi: SMA_{trend_sma}, RSI_{rsi_period}"
    )
    return df


def _indicators_greenblatt_weekly(
    df: pd.DataFrame, params: Dict[str, Any]
) -> pd.DataFrame:
    # Runs on weekly bars. Exit uses trailing stop pct (no ATR needed).
    # Bear market filter uses slow_sma instead of sma_200 for this strategy.
    fast_sma = params.get("fast_sma", 10)
    slow_sma = params.get("slow_sma", 50)
    df = add_sma(df, fast_sma)
    df = add_sma(df, slow_sma)
    rsi_period = params.get("rsi_period", 14)
    df = add_rsi(df, rsi_period)
    logger.debug(
        f"Added indicators for greenblatt_weekly: SMA_{fast_sma}, SMA_{slow_sma}, RSI_{rsi_period}"
    )
    return df


# Dispatch table — adding a new strategy requires only a new _indicators_* function + one entry here
_STRATEGY_INDICATOR_DISPATCH: Dict[str, Any] = {
    "ma_crossover": _indicators_ma_crossover,
    "rsi_mean_reversion": _indicators_rsi_mean_reversion,
    "momentum_breakout": _indicators_momentum_breakout,
    "bollinger_breakout": _indicators_bollinger_breakout,
    "vwap_reversion": _indicators_vwap_reversion,
    "bollinger_rsi_combo": _indicators_bollinger_rsi_combo,
    "trend_adaptive_rsi": _indicators_trend_adaptive_rsi,
    "greenblatt_weekly": _indicators_greenblatt_weekly,
}


def add_all_for_strategy(
    df: pd.DataFrame,
    strategy_name: str,
    params: Dict[str, Any],
) -> pd.DataFrame:
    """Add all indicators needed for a specific strategy.

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
    handler = _STRATEGY_INDICATOR_DISPATCH.get(strategy_name)
    if handler is None:
        raise ValueError(
            f"Unknown strategy: {strategy_name}. "
            f"Supported: {', '.join(sorted(_STRATEGY_INDICATOR_DISPATCH))}"
        )

    df = df.copy()
    # SMA_200 is always added — used by the bear market filter to block BUY signals
    # when price is below a declining 200-day SMA.
    df = add_sma(df, 200)
    return handler(df, params)


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
    logger.info(
        "Calculating all indicators (this is slow - use add_all_for_strategy in production)"
    )

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
