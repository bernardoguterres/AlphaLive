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


def rsi_wilder(close: pd.Series, period: int) -> pd.Series:
    """Canonical RSI (Wilder, 1978), the single agreed-upon definition for
    deployable Alpha strategies (Prompt 2.5 remediation, 2026-08-15 - see
    FINAL_ENGINEERING_AUDIT.md / END_TO_END_VALIDATION.md).

    Used ONLY by rsi_mean_reversion (see _indicators_rsi_mean_reversion
    below) - deliberately NOT a change to the shared add_rsi()/`ta` library
    path the other five RSI-consuming strategies still use, to avoid
    silently altering their behaviour (blast-radius discipline).

    Independently re-implemented in AlphaLab's
    alphalab/strategies/implementations/rsi_mean_reversion.py::rsi_wilder()
    - identical formula, seeding, warm-up and NaN semantics, cross-checked
    by shared golden-value tests in both repos rather than a shared
    dependency, matching this project's deliberate "two independent
    implementations + parity test" pattern used everywhere else (see e.g.
    AlphaLive's portfolio/target_weights.py). Previously this strategy used
    the `ta` library's RSIIndicator, which uses a different smoothing/
    seeding convention than AlphaLab's old raw-EWM-no-warmup formula -
    neither side was wrong in isolation, but they didn't agree, which is
    the actual defect this function fixes.

    Semantics:
    - avg_gain/avg_loss seed at index `period` (0-indexed) as the simple
      mean of the first `period` gains/losses.
    - Every subsequent bar uses Wilder's recursive smoothing:
      avg = (prev_avg * (period - 1) + current) / period.
    - RSI = 100 - 100 / (1 + avg_gain / avg_loss).
    - avg_loss == 0 and avg_gain > 0 -> RSI = 100.
    - avg_gain == 0 and avg_loss == 0 -> RSI = 50.
    - Rows before index `period` are NaN.
    """
    n = len(close)
    if n <= period:
        return pd.Series(np.nan, index=close.index, dtype=float)

    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = np.full(n, np.nan)
    avg_loss = np.full(n, np.nan)

    avg_gain[period] = gain.iloc[1 : period + 1].mean()
    avg_loss[period] = loss.iloc[1 : period + 1].mean()

    gain_vals = gain.to_numpy()
    loss_vals = loss.to_numpy()
    for i in range(period + 1, n):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain_vals[i]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss_vals[i]) / period

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        rsi_vals = 100.0 - (100.0 / (1.0 + rs))

    flat = (avg_gain == 0) & (avg_loss == 0)
    pure_uptrend = (avg_loss == 0) & (avg_gain > 0)
    rsi_vals = np.where(flat, 50.0, rsi_vals)
    rsi_vals = np.where(pure_uptrend, 100.0, rsi_vals)
    rsi_vals[:period] = np.nan

    rsi = pd.Series(rsi_vals, index=close.index, dtype=float).clip(0, 100)
    rsi.iloc[:period] = np.nan
    return rsi


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


def add_vwap(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """Add ROLLING vwap column over `period` bars, matching AlphaLab.

    Was cumulative-from-start-of-loaded-history until 2026-07-10 - a signal
    parity bug: after years of uptrend a cumulative VWAP sits far below spot,
    so vwap_reversion's `close < vwap - 2*std` entry almost never fired live
    (4 candidate bars in 5y of SPY vs ~13 AlphaLab round trips), and the
    value even depended on how many lookback bars were fetched. The C1
    parity fixtures (500 bars, 2022-2023) produced ~no signals under either
    definition, so zero-matched-zero and the divergence passed unnoticed.
    """
    try:
        # Typical price
        typical_price = (df["high"] + df["low"] + df["close"]) / 3

        # Rolling VWAP = sum(tp * volume, period) / sum(volume, period)
        df["vwap"] = (typical_price * df["volume"]).rolling(period).sum() / df[
            "volume"
        ].rolling(period).sum()

        logger.debug(f"Added rolling VWAP ({period} bars)")
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


# Single source of truth for which strategies exist. SignalEngine derives its
# own dispatch table from these same keys (looking up a same-named
# `_{name}_signal` method) instead of maintaining a second, separately-typed
# strategy-name list that has to be kept in lockstep by hand - see
# register_strategy_indicators() below.
_STRATEGY_INDICATOR_REGISTRY: Dict[str, Any] = {}


def register_strategy_indicators(name: str):
    """Decorator: registers an indicator-computation function under a strategy name.

    Adding a new strategy means adding one function here decorated with
    this, plus one `_{name}_signal` method on SignalEngine - no separate
    dispatch dict to keep in sync in either file.
    """

    def decorator(fn):
        _STRATEGY_INDICATOR_REGISTRY[name] = fn
        return fn

    return decorator


def registered_strategy_names() -> list:
    """Strategy names with registered indicator functions, sorted."""
    return sorted(_STRATEGY_INDICATOR_REGISTRY)


@register_strategy_indicators("ma_crossover")
def _indicators_ma_crossover(df: pd.DataFrame, params: Dict[str, Any]) -> pd.DataFrame:
    fast_period = params.get("fast_period", 10)
    slow_period = params.get("slow_period", 20)
    df = add_sma(df, fast_period)
    df = add_sma(df, slow_period)
    # Added 2026-08-15 (parity remediation): volume_confirmation was accepted
    # by the schema but signal_engine.py never computed the volume average it
    # needs - the column simply didn't exist, so the filter silently couldn't
    # be applied. Always compute it (cheap); the signal only reads it when
    # volume_confirmation=True, matching AlphaLab's own params.get() pattern
    # (momentum_breakout uses the identical volume_ma_{period} naming).
    volume_avg_period = params.get("volume_avg_period", 20)
    df[f"volume_ma_{volume_avg_period}"] = (
        df["volume"].rolling(window=volume_avg_period).mean()
    )
    logger.debug(
        f"Added indicators for ma_crossover: SMA_{fast_period}, SMA_{slow_period}, "
        f"volume_ma_{volume_avg_period}"
    )
    return df


@register_strategy_indicators("rsi_mean_reversion")
def _indicators_rsi_mean_reversion(
    df: pd.DataFrame, params: Dict[str, Any]
) -> pd.DataFrame:
    # Uses the canonical rsi_wilder() (Prompt 2.5 remediation, 2026-08-15),
    # not the shared add_rsi()/`ta` library path the other five
    # RSI-consuming strategies still use - see rsi_wilder()'s docstring for
    # why. Column name (rsi_{period}) is unchanged, so signal_engine.py's
    # existing lookup needs no change.
    period = params.get("period", 14)
    df = df.copy()
    df[f"rsi_{period}"] = rsi_wilder(df["close"], period)

    # ATR (for the ATR-based stop-loss), BB/ADX only when the matching
    # confirmation flags are set - added 2026-08-15 alongside the
    # signal_engine.py state-machine rewrite that now mirrors AlphaLab's
    # RSIMeanReversion entry/exit/cooldown/stop-loss logic instead of a
    # stateless single-bar threshold check (a real, previously-hidden
    # parity gap: AlphaLive re-fired BUY every time RSI dipped below
    # oversold with no position/cooldown/exit tracking at all - see
    # FINAL_ENGINEERING_AUDIT.md / END_TO_END_VALIDATION.md).
    df = add_atr(df, 14)
    if params.get("use_bb_confirmation"):
        bb_period = params.get("bb_period", 20)
        bb_std = params.get("bb_std", 2.0)
        df = add_bollinger(df, bb_period, bb_std)
    if params.get("use_adx_filter"):
        df = add_adx(df, 14)

    logger.debug(
        f"Added indicators for rsi_mean_reversion: RSI_{period} (Wilder), ATR_14"
    )
    return df


@register_strategy_indicators("momentum_breakout")
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


@register_strategy_indicators("bollinger_breakout")
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


@register_strategy_indicators("vwap_reversion")
def _indicators_vwap_reversion(
    df: pd.DataFrame, params: Dict[str, Any]
) -> pd.DataFrame:
    # AlphaLab uses vwap_period for BOTH the rolling VWAP window and the
    # deviation-std window - mirror that (vwap_std_period kept as an override
    # for backward compatibility with existing configs).
    vwap_period = params.get("vwap_period", 20)
    df = add_vwap(df, period=vwap_period)
    rsi_period = params.get("rsi_period", 14)
    df = add_rsi(df, rsi_period)
    vwap_std_period = params.get("vwap_std_period", vwap_period)
    df["vwap_std"] = (df["close"] - df["vwap"]).rolling(window=vwap_std_period).std()
    logger.debug(
        f"Added indicators for vwap_reversion: VWAP, RSI_{rsi_period}, vwap_std_{vwap_std_period}"
    )
    return df


@register_strategy_indicators("bollinger_rsi_combo")
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


@register_strategy_indicators("trend_adaptive_rsi")
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


@register_strategy_indicators("greenblatt_weekly")
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
    handler = _STRATEGY_INDICATOR_REGISTRY.get(strategy_name)
    if handler is None:
        raise ValueError(
            f"Unknown strategy: {strategy_name}. "
            f"Supported: {', '.join(registered_strategy_names())}"
        )

    df = df.copy()
    # SMA_200 is always added - used by the bear market filter to block BUY signals
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
