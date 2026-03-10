"""
Signal Generation Engine

Generates buy/sell signals based on strategy logic and market data.
Supports 5 strategies with exact AlphaLab parity.

CRITICAL: Signal logic must match AlphaLab backtest exactly.
Any divergence means live results won't match backtest expectations.

Performance Budget: <5 seconds per strategy (all indicators + signal logic)
Expected: <0.5s for 200 bars on Railway's shared vCPU
"""

import logging
import time
from typing import Dict, Any, Optional

import pandas as pd
import numpy as np

from alphalive.strategy_schema import StrategySchema
from alphalive.strategy import indicators

logger = logging.getLogger(__name__)


class SignalEngine:
    """
    Generates trading signals based on strategy configuration.

    Each strategy has its own signal generation logic that must
    match AlphaLab's backtest implementation exactly.
    """

    def __init__(self, config: StrategySchema):
        """
        Initialize signal engine.

        Args:
            config: Strategy configuration from AlphaLab export
        """
        self.config = config
        self.strategy_name = config.strategy.name
        self.params = config.strategy.parameters

        logger.info(
            f"Signal engine initialized | Strategy: {self.strategy_name} | "
            f"Params: {self.params}"
        )

    def generate_signal(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Generate signal for the LAST row of the DataFrame.

        Args:
            df: DataFrame with OHLCV columns (open, high, low, close, volume)
                Must have at least enough rows for indicator warmup

        Returns:
            Dictionary with:
            - signal: "BUY" | "SELL" | "HOLD"
            - confidence: float 0.0-1.0
            - reason: str (human-readable explanation)
            - indicators: dict (indicator values at last row)
            - warmup_complete: bool (False if any required indicator is NaN)
            - generation_time_ms: int (time taken in milliseconds)

        Note:
            warmup_complete=False means not enough historical data yet.
            This is critical after Railway restarts mid-day.
        """
        start_time = time.time()

        # Validate input
        if df.empty or len(df) < 2:
            logger.warning("Insufficient data for signal generation (need at least 2 rows)")
            return self._no_signal("Insufficient data", start_time)

        # Ensure required columns exist
        required_cols = ['open', 'high', 'low', 'close', 'volume']
        missing_cols = [col for col in required_cols if col not in df.columns]
        if missing_cols:
            logger.error(f"Missing required columns: {missing_cols}")
            return self._no_signal(f"Missing columns: {missing_cols}", start_time)

        # Add indicators for this strategy
        try:
            df = indicators.add_all_for_strategy(df, self.strategy_name, self.params)
        except Exception as e:
            logger.error(f"Failed to add indicators: {e}", exc_info=True)
            return self._no_signal(f"Indicator calculation failed: {e}", start_time)

        # Route to strategy-specific logic
        try:
            if self.strategy_name == "ma_crossover":
                result = self._ma_crossover_signal(df)
            elif self.strategy_name == "rsi_mean_reversion":
                result = self._rsi_mean_reversion_signal(df)
            elif self.strategy_name == "momentum_breakout":
                result = self._momentum_breakout_signal(df)
            elif self.strategy_name == "bollinger_breakout":
                result = self._bollinger_breakout_signal(df)
            elif self.strategy_name == "vwap_reversion":
                result = self._vwap_reversion_signal(df)
            else:
                logger.error(f"Unknown strategy: {self.strategy_name}")
                return self._no_signal(f"Unknown strategy: {self.strategy_name}", start_time)

        except Exception as e:
            logger.error(f"Signal generation failed: {e}", exc_info=True)
            return self._no_signal(f"Signal generation error: {e}", start_time)

        # Add generation time
        elapsed = time.time() - start_time
        result["generation_time_ms"] = int(elapsed * 1000)

        # Performance warning
        if elapsed > 5.0:
            logger.warning(
                f"⚠️ Signal generation SLOW: {self.strategy_name} took {elapsed:.2f}s "
                f"(budget: 5s). Optimize indicators or reduce lookback."
            )
        else:
            logger.debug(f"Signal generation time: {elapsed:.3f}s")

        # Log signal
        if result["signal"] != "HOLD":
            logger.info(
                f"🎯 SIGNAL: {result['signal']} | Confidence: {result['confidence']:.2%} | "
                f"Reason: {result['reason']}"
            )
        else:
            logger.debug(f"No signal: {result['reason']}")

        return result

    # =========================================================================
    # Strategy Implementations (must match AlphaLab exactly)
    # =========================================================================

    def _ma_crossover_signal(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Moving Average Crossover Strategy.

        BUY: Fast SMA crosses above Slow SMA
        SELL: Fast SMA crosses below Slow SMA
        Confidence: Based on distance between SMAs as % of price

        AlphaLab Parity: Must detect crossover at exact same bar.
        """
        fast_period = self.params.get("fast_period", 10)
        slow_period = self.params.get("slow_period", 20)

        fast_col = f"sma_{fast_period}"
        slow_col = f"sma_{slow_period}"

        # Check warmup
        if pd.isna(df[fast_col].iloc[-1]) or pd.isna(df[slow_col].iloc[-1]):
            return self._no_signal(
                f"Warmup incomplete (need {slow_period} bars)",
                time.time(),
                warmup_complete=False
            )

        # Current and previous values
        fast_curr = df[fast_col].iloc[-1]
        fast_prev = df[fast_col].iloc[-2]
        slow_curr = df[slow_col].iloc[-1]
        slow_prev = df[slow_col].iloc[-2]

        current_price = df['close'].iloc[-1]

        # Extract indicator values
        indicators = {
            fast_col: fast_curr,
            slow_col: slow_curr,
            "price": current_price
        }

        # Detect crossover
        if fast_prev <= slow_prev and fast_curr > slow_curr:
            # Bullish crossover
            spread_pct = ((fast_curr - slow_curr) / current_price) * 100
            confidence = min(1.0, spread_pct / 2.0)  # 2% spread = 100% confidence

            return {
                "signal": "BUY",
                "confidence": confidence,
                "reason": (
                    f"Bullish MA crossover: Fast SMA({fast_period})={fast_curr:.2f} "
                    f"crossed above Slow SMA({slow_period})={slow_curr:.2f}"
                ),
                "indicators": indicators,
                "warmup_complete": True
            }

        elif fast_prev >= slow_prev and fast_curr < slow_curr:
            # Bearish crossover
            spread_pct = ((slow_curr - fast_curr) / current_price) * 100
            confidence = min(1.0, spread_pct / 2.0)

            return {
                "signal": "SELL",
                "confidence": confidence,
                "reason": (
                    f"Bearish MA crossover: Fast SMA({fast_period})={fast_curr:.2f} "
                    f"crossed below Slow SMA({slow_period})={slow_curr:.2f}"
                ),
                "indicators": indicators,
                "warmup_complete": True
            }

        else:
            # No crossover
            return {
                "signal": "HOLD",
                "confidence": 0.0,
                "reason": f"No crossover (Fast={fast_curr:.2f}, Slow={slow_curr:.2f})",
                "indicators": indicators,
                "warmup_complete": True
            }

    def _rsi_mean_reversion_signal(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        RSI Mean Reversion Strategy.

        BUY: RSI < oversold threshold
        SELL: RSI > overbought threshold
        Confidence: How far RSI is from threshold

        AlphaLab Parity: RSI calculation and thresholds must match exactly.
        """
        period = self.params.get("period", 14)
        oversold = self.params.get("oversold", 30)
        overbought = self.params.get("overbought", 70)

        rsi_col = f"rsi_{period}"

        # Check warmup
        if pd.isna(df[rsi_col].iloc[-1]):
            return self._no_signal(
                f"Warmup incomplete (need {period + 1} bars)",
                time.time(),
                warmup_complete=False
            )

        rsi_curr = df[rsi_col].iloc[-1]
        current_price = df['close'].iloc[-1]

        indicators = {
            rsi_col: rsi_curr,
            "oversold": oversold,
            "overbought": overbought,
            "price": current_price
        }

        # Check thresholds
        if rsi_curr < oversold:
            # Oversold - BUY
            distance = oversold - rsi_curr
            confidence = min(1.0, distance / oversold)  # Further from threshold = higher confidence

            return {
                "signal": "BUY",
                "confidence": confidence,
                "reason": (
                    f"RSI oversold: RSI({period})={rsi_curr:.2f} < {oversold} "
                    f"(distance: {distance:.2f})"
                ),
                "indicators": indicators,
                "warmup_complete": True
            }

        elif rsi_curr > overbought:
            # Overbought - SELL
            distance = rsi_curr - overbought
            confidence = min(1.0, distance / (100 - overbought))

            return {
                "signal": "SELL",
                "confidence": confidence,
                "reason": (
                    f"RSI overbought: RSI({period})={rsi_curr:.2f} > {overbought} "
                    f"(distance: {distance:.2f})"
                ),
                "indicators": indicators,
                "warmup_complete": True
            }

        else:
            # Neutral zone
            return {
                "signal": "HOLD",
                "confidence": 0.0,
                "reason": f"RSI neutral: {rsi_curr:.2f} (range: {oversold}-{overbought})",
                "indicators": indicators,
                "warmup_complete": True
            }

    def _momentum_breakout_signal(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Momentum Breakout Strategy.

        BUY: Close > rolling high of lookback AND volume > avg * surge_pct
        SELL: Trailing stop hit (3x ATR below recent high)
        Confidence: Based on volume surge magnitude

        AlphaLab Parity: Rolling high calculation and volume surge must match.
        """
        lookback = self.params.get("lookback", 20)
        surge_pct = self.params.get("surge_pct", 1.5)
        atr_period = self.params.get("atr_period", 14)
        volume_ma_period = self.params.get("volume_ma_period", 20)

        # Check warmup
        rolling_high = df['rolling_high'].iloc[-1]
        volume_ma = df[f'volume_ma_{volume_ma_period}'].iloc[-1]
        atr = df[f'atr_{atr_period}'].iloc[-1]

        if pd.isna(rolling_high) or pd.isna(volume_ma) or pd.isna(atr):
            return self._no_signal(
                f"Warmup incomplete (need {max(lookback, volume_ma_period, atr_period)} bars)",
                time.time(),
                warmup_complete=False
            )

        current_price = df['close'].iloc[-1]
        current_volume = df['volume'].iloc[-1]

        indicators = {
            "price": current_price,
            "rolling_high": rolling_high,
            "volume": current_volume,
            f"volume_ma_{volume_ma_period}": volume_ma,
            f"atr_{atr_period}": atr
        }

        # Check breakout conditions
        volume_surge = current_volume / volume_ma if volume_ma > 0 else 0

        if current_price > rolling_high and volume_surge > surge_pct:
            # Breakout with volume confirmation
            breakout_pct = ((current_price - rolling_high) / rolling_high) * 100
            confidence = min(1.0, (volume_surge - surge_pct) / surge_pct)

            return {
                "signal": "BUY",
                "confidence": confidence,
                "reason": (
                    f"Momentum breakout: Price {current_price:.2f} > "
                    f"High({lookback})={rolling_high:.2f} (+{breakout_pct:.2f}%), "
                    f"Volume surge {volume_surge:.2f}x (>{surge_pct}x)"
                ),
                "indicators": indicators,
                "warmup_complete": True
            }

        else:
            # No breakout or insufficient volume
            return {
                "signal": "HOLD",
                "confidence": 0.0,
                "reason": (
                    f"No breakout: Price={current_price:.2f}, High={rolling_high:.2f}, "
                    f"Vol surge={volume_surge:.2f}x (need >{surge_pct}x)"
                ),
                "indicators": indicators,
                "warmup_complete": True
            }

    def _bollinger_breakout_signal(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Bollinger Band Breakout Strategy.

        BUY: Close > upper BB for confirmation_bars AND volume > 1.5x avg
        SELL: Close < lower BB for confirmation_bars
        Confidence: Based on distance from band

        AlphaLab Parity: confirmation_bars logic must match exactly.
        This is a critical parity test case.

        IMPORTANT: Parameter key is "confirmation_bars" (not "confirm_bars")
        """
        period = self.params.get("period", 20)
        std_dev = self.params.get("std_dev", 2.0)
        confirmation_bars = self.params.get("confirmation_bars", 2)
        volume_ma_period = self.params.get("volume_ma_period", 20)

        # Check warmup
        bb_upper = df['bb_upper'].iloc[-1]
        bb_lower = df['bb_lower'].iloc[-1]
        bb_middle = df['bb_middle'].iloc[-1]
        volume_ma = df[f'volume_ma_{volume_ma_period}'].iloc[-1]

        if pd.isna(bb_upper) or pd.isna(bb_lower) or pd.isna(volume_ma):
            return self._no_signal(
                f"Warmup incomplete (need {max(period, volume_ma_period)} bars)",
                time.time(),
                warmup_complete=False
            )

        # Need enough rows for confirmation check
        if len(df) < confirmation_bars:
            return self._no_signal(
                f"Need {confirmation_bars} bars for confirmation",
                time.time(),
                warmup_complete=False
            )

        current_price = df['close'].iloc[-1]
        current_volume = df['volume'].iloc[-1]

        indicators = {
            "price": current_price,
            "bb_upper": bb_upper,
            "bb_middle": bb_middle,
            "bb_lower": bb_lower,
            "volume": current_volume,
            f"volume_ma_{volume_ma_period}": volume_ma
        }

        # Check confirmation bars for upper breakout
        upper_confirmed = all(
            df['close'].iloc[-i] > df['bb_upper'].iloc[-i]
            for i in range(1, confirmation_bars + 1)
        )

        # Check confirmation bars for lower breakdown
        lower_confirmed = all(
            df['close'].iloc[-i] < df['bb_lower'].iloc[-i]
            for i in range(1, confirmation_bars + 1)
        )

        volume_surge = current_volume / volume_ma if volume_ma > 0 else 0

        # Upper breakout
        if upper_confirmed and volume_surge > 1.5:
            distance_pct = ((current_price - bb_upper) / bb_upper) * 100
            confidence = min(1.0, abs(distance_pct) / 2.0)

            return {
                "signal": "BUY",
                "confidence": confidence,
                "reason": (
                    f"Bollinger upper breakout: Price {current_price:.2f} > "
                    f"BB_upper={bb_upper:.2f} for {confirmation_bars} bars, "
                    f"Volume surge {volume_surge:.2f}x"
                ),
                "indicators": indicators,
                "warmup_complete": True
            }

        # Lower breakdown
        elif lower_confirmed:
            distance_pct = ((bb_lower - current_price) / bb_lower) * 100
            confidence = min(1.0, abs(distance_pct) / 2.0)

            return {
                "signal": "SELL",
                "confidence": confidence,
                "reason": (
                    f"Bollinger lower breakdown: Price {current_price:.2f} < "
                    f"BB_lower={bb_lower:.2f} for {confirmation_bars} bars"
                ),
                "indicators": indicators,
                "warmup_complete": True
            }

        else:
            # No breakout
            return {
                "signal": "HOLD",
                "confidence": 0.0,
                "reason": (
                    f"No breakout: Price={current_price:.2f}, "
                    f"BB range=[{bb_lower:.2f}, {bb_upper:.2f}], "
                    f"Vol surge={volume_surge:.2f}x"
                ),
                "indicators": indicators,
                "warmup_complete": True
            }

    def _vwap_reversion_signal(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        VWAP Mean Reversion Strategy.

        BUY: Price < VWAP - (deviation_threshold * std) AND RSI < oversold
        SELL: Price > VWAP + (deviation_threshold * std) AND RSI > overbought
        Confidence: Based on deviation magnitude

        AlphaLab Parity: VWAP and std calculation must match exactly.
        """
        deviation_threshold = self.params.get("deviation_threshold", 2.0)
        rsi_period = self.params.get("rsi_period", 14)
        oversold = self.params.get("oversold", 30)
        overbought = self.params.get("overbought", 70)
        vwap_std_period = self.params.get("vwap_std_period", 20)

        rsi_col = f"rsi_{rsi_period}"

        # Check warmup
        vwap = df['vwap'].iloc[-1]
        vwap_std = df['vwap_std'].iloc[-1]
        rsi = df[rsi_col].iloc[-1]

        if pd.isna(vwap) or pd.isna(vwap_std) or pd.isna(rsi):
            return self._no_signal(
                f"Warmup incomplete (need {max(vwap_std_period, rsi_period)} bars)",
                time.time(),
                warmup_complete=False
            )

        current_price = df['close'].iloc[-1]

        # Calculate deviation bands
        upper_band = vwap + (deviation_threshold * vwap_std)
        lower_band = vwap - (deviation_threshold * vwap_std)

        # Calculate deviation in standard deviations
        deviation = (current_price - vwap) / vwap_std if vwap_std > 0 else 0

        indicators = {
            "price": current_price,
            "vwap": vwap,
            "vwap_std": vwap_std,
            "upper_band": upper_band,
            "lower_band": lower_band,
            "deviation": deviation,
            rsi_col: rsi
        }

        # Oversold reversion (BUY)
        if current_price < lower_band and rsi < oversold:
            confidence = min(1.0, abs(deviation) / (deviation_threshold * 2))

            return {
                "signal": "BUY",
                "confidence": confidence,
                "reason": (
                    f"VWAP oversold reversion: Price {current_price:.2f} < "
                    f"VWAP-{deviation_threshold}σ={lower_band:.2f}, "
                    f"RSI={rsi:.2f} < {oversold}, "
                    f"Deviation={deviation:.2f}σ"
                ),
                "indicators": indicators,
                "warmup_complete": True
            }

        # Overbought reversion (SELL)
        elif current_price > upper_band and rsi > overbought:
            confidence = min(1.0, abs(deviation) / (deviation_threshold * 2))

            return {
                "signal": "SELL",
                "confidence": confidence,
                "reason": (
                    f"VWAP overbought reversion: Price {current_price:.2f} > "
                    f"VWAP+{deviation_threshold}σ={upper_band:.2f}, "
                    f"RSI={rsi:.2f} > {overbought}, "
                    f"Deviation={deviation:.2f}σ"
                ),
                "indicators": indicators,
                "warmup_complete": True
            }

        else:
            # No reversion opportunity
            return {
                "signal": "HOLD",
                "confidence": 0.0,
                "reason": (
                    f"No reversion: Price={current_price:.2f}, "
                    f"VWAP±{deviation_threshold}σ=[{lower_band:.2f}, {upper_band:.2f}], "
                    f"RSI={rsi:.2f}"
                ),
                "indicators": indicators,
                "warmup_complete": True
            }

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _no_signal(
        self,
        reason: str,
        start_time: float,
        warmup_complete: bool = True
    ) -> Dict[str, Any]:
        """Return a HOLD signal with reason."""
        elapsed = time.time() - start_time

        return {
            "signal": "HOLD",
            "confidence": 0.0,
            "reason": reason,
            "indicators": {},
            "warmup_complete": warmup_complete,
            "generation_time_ms": int(elapsed * 1000)
        }
