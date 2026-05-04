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

        # State for stateful strategies (mirrors AlphaLab's in_position tracking)
        self._in_position = False
        self._entry_price: float = 0.0

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

        # Bear market filter: block BUY signals when not in a position and price is
        # below a declining SMA_200. Allows SELL/exits to proceed normally.
        if not self._in_position and self._is_bear_market(df):
            result = {
                "signal": "HOLD",
                "confidence": 0.0,
                "reason": (
                    f"Bear market filter: price below declining SMA_200 "
                    f"({df['close'].iloc[-1]:.2f} < {df['sma_200'].iloc[-1]:.2f}) — BUY blocked"
                ),
                "indicators": {"price": df["close"].iloc[-1], "sma_200": df["sma_200"].iloc[-1]},
                "warmup_complete": True,
                "generation_time_ms": int((time.time() - start_time) * 1000),
            }
            logger.debug(f"Bear market filter triggered: {result['reason']}")
            return result

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
            elif self.strategy_name == "bollinger_rsi_combo":
                result = self._bollinger_rsi_combo_signal(df)
            elif self.strategy_name == "trend_adaptive_rsi":
                result = self._trend_adaptive_rsi_signal(df)
            elif self.strategy_name == "greenblatt_weekly":
                result = self._greenblatt_weekly_signal(df)
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

    def _bollinger_rsi_combo_signal(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Bollinger Bands + RSI Combination Strategy.

        BUY: Price <= Lower BB AND RSI < oversold (default 45) — only when not in position
        SELL: Price >= Middle BB OR RSI > overbought (default 55) — only when in position

        Stateful: tracks in_position via self._in_position to match AlphaLab exactly.
        AlphaLab Parity: Matches bollinger_rsi_combo.py implementation.
        """
        bb_period = self.params.get("bb_period", 20)
        rsi_period = self.params.get("rsi_period", 14)
        rsi_oversold = self.params.get("rsi_oversold", 45)
        rsi_overbought = self.params.get("rsi_overbought", 55)
        exit_at_middle = self.params.get("exit_at_middle", True)

        bb_lower = df['bb_lower'].iloc[-1]
        bb_middle = df['bb_middle'].iloc[-1]
        bb_upper = df['bb_upper'].iloc[-1]
        rsi_col = f"rsi_{rsi_period}"
        rsi = df[rsi_col].iloc[-1]

        if pd.isna(bb_lower) or pd.isna(bb_middle) or pd.isna(rsi):
            return self._no_signal(
                f"Warmup incomplete (need {max(bb_period, rsi_period)} bars)",
                time.time(),
                warmup_complete=False
            )

        current_price = df['close'].iloc[-1]

        indicators = {
            "price": current_price,
            "bb_lower": bb_lower,
            "bb_middle": bb_middle,
            "bb_upper": bb_upper,
            rsi_col: rsi
        }

        if self._in_position:
            # Look for exit: price reached middle BB or RSI overbought
            exit_reason = None
            if exit_at_middle and current_price >= bb_middle:
                pnl_pct = ((current_price - self._entry_price) / self._entry_price) * 100
                exit_reason = f"BB middle reached (+{pnl_pct:.1f}%)"
            elif rsi > rsi_overbought:
                pnl_pct = ((current_price - self._entry_price) / self._entry_price) * 100
                exit_reason = f"RSI overbought {rsi:.1f} ({pnl_pct:+.1f}%)"

            if exit_reason:
                conf = min(1.0, (rsi - rsi_overbought) / (100 - rsi_overbought) + 0.5)
                self._in_position = False
                return {
                    "signal": "SELL",
                    "confidence": conf,
                    "reason": exit_reason,
                    "indicators": indicators,
                    "warmup_complete": True
                }
        else:
            # Look for entry: price at/below lower BB AND RSI oversold
            if current_price <= bb_lower and rsi < rsi_oversold:
                bb_penetration = (bb_lower - current_price) / bb_lower * 100
                rsi_distance = (rsi_oversold - rsi) / rsi_oversold
                confidence = min(1.0, max(0.3, (bb_penetration * 10 + rsi_distance) / 2))
                self._in_position = True
                self._entry_price = current_price
                return {
                    "signal": "BUY",
                    "confidence": confidence,
                    "reason": f"BB lower touch + RSI {rsi:.1f}",
                    "indicators": indicators,
                    "warmup_complete": True
                }

        return {
            "signal": "HOLD",
            "confidence": 0.0,
            "reason": (
                f"No setup: Price {current_price:.2f} in range "
                f"[{bb_lower:.2f}, {bb_middle:.2f}], RSI {rsi:.2f}"
            ),
            "indicators": indicators,
            "warmup_complete": True
        }

    def _trend_adaptive_rsi_signal(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Trend-Adaptive RSI Strategy.

        Adjusts RSI thresholds based on market regime:
        - Uptrend: Buy at RSI 45, Sell at RSI 65
        - Downtrend: Buy at RSI 35, Sell at RSI 55
        - Range: Buy at RSI 35, Sell at RSI 65

        Stateful: tracks in_position via self._in_position to match AlphaLab exactly.
        AlphaLab Parity: Matches trend_adaptive_rsi.py implementation.
        """
        rsi_period = self.params.get("rsi_period", 14)
        trend_sma = self.params.get("trend_sma", 50)
        trend_lookback = self.params.get("trend_lookback", 5)

        uptrend_buy = self.params.get("uptrend_buy", 45)
        uptrend_sell = self.params.get("uptrend_sell", 65)
        downtrend_buy = self.params.get("downtrend_buy", 35)
        downtrend_sell = self.params.get("downtrend_sell", 55)
        range_buy = self.params.get("range_buy", 35)
        range_sell = self.params.get("range_sell", 65)

        if len(df) < trend_lookback + 1:
            return self._no_signal(
                f"Need {trend_lookback + 1} bars for trend detection",
                time.time(),
                warmup_complete=False
            )

        sma_col = f"sma_{trend_sma}"
        rsi_col = f"rsi_{rsi_period}"

        if sma_col not in df.columns or rsi_col not in df.columns:
            return self._no_signal(
                f"Missing required indicators: {sma_col}, {rsi_col}",
                time.time(),
                warmup_complete=False
            )

        current_price = df['close'].iloc[-1]
        sma_curr = df[sma_col].iloc[-1]
        rsi_curr = df[rsi_col].iloc[-1]

        if pd.isna(sma_curr) or pd.isna(rsi_curr):
            return self._no_signal(
                f"Warmup incomplete (need {max(trend_sma, rsi_period)} bars)",
                time.time(),
                warmup_complete=False
            )

        # Detect market regime
        above_sma = current_price > sma_curr
        sma_prev = df[sma_col].iloc[-trend_lookback - 1]
        sma_slope = (sma_curr - sma_prev) / sma_prev if sma_prev > 0 else 0

        if above_sma and sma_slope > 0.005:
            regime = "uptrend"
            buy_threshold = uptrend_buy
            sell_threshold = uptrend_sell
        elif not above_sma and sma_slope < -0.005:
            regime = "downtrend"
            buy_threshold = downtrend_buy
            sell_threshold = downtrend_sell
        else:
            regime = "range"
            buy_threshold = range_buy
            sell_threshold = range_sell

        indicators = {
            "price": current_price,
            sma_col: sma_curr,
            rsi_col: rsi_curr,
            "regime": regime,
            "buy_threshold": buy_threshold,
            "sell_threshold": sell_threshold
        }

        if self._in_position:
            # Exit when RSI crosses above sell threshold
            if rsi_curr > sell_threshold:
                distance = rsi_curr - sell_threshold
                confidence = min(1.0, distance / (100 - sell_threshold))
                self._in_position = False
                return {
                    "signal": "SELL",
                    "confidence": confidence,
                    "reason": f"Sell {regime}: RSI {rsi_curr:.2f} > {sell_threshold}",
                    "indicators": indicators,
                    "warmup_complete": True
                }
        else:
            # Enter when RSI drops below buy threshold
            if rsi_curr < buy_threshold:
                distance = buy_threshold - rsi_curr
                confidence = min(1.0, distance / buy_threshold)
                self._in_position = True
                return {
                    "signal": "BUY",
                    "confidence": confidence,
                    "reason": f"Buy {regime}: RSI {rsi_curr:.2f} < {buy_threshold}",
                    "indicators": indicators,
                    "warmup_complete": True
                }

        return {
            "signal": "HOLD",
            "confidence": 0.0,
            "reason": (
                f"No signal ({regime}): RSI {rsi_curr:.2f} in neutral zone "
                f"[{buy_threshold}, {sell_threshold}]"
            ),
            "indicators": indicators,
            "warmup_complete": True
        }

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _greenblatt_weekly_signal(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Greenblatt Weekly strategy signal on the last weekly bar.

        Entry: Weekly RSI < rsi_oversold OR 10w/40w SMA golden cross.
        Exit:  Weekly RSI > rsi_overbought OR SMA death-cross (after min_hold_weeks).
        Stop:  Price <= entry_price - (stop_loss_atr_mult × ATR).

        Minimum hold is enforced by the caller (main.py) using entry_timestamp
        from state. The signal engine still returns SELL; main.py suppresses it
        unless min_hold is met or it's a stop-loss.
        """
        p = self.params
        fast_sma = p.get("fast_sma", 10)
        slow_sma = p.get("slow_sma", 40)
        rsi_oversold = p.get("rsi_oversold", 35)
        rsi_overbought = p.get("rsi_overbought", 65)
        stop_atr_mult = p.get("stop_loss_atr_mult", 2.0)

        fast_col = f"sma_{fast_sma}"
        slow_col = f"sma_{slow_sma}"

        if len(df) < 2:
            return self._no_signal("Insufficient weekly bars", 0, warmup_complete=False)

        # Check warmup
        required = [fast_col, slow_col, "rsi", "atr"]
        for col in required:
            if col not in df.columns or pd.isna(df[col].iloc[-1]):
                return self._no_signal(f"Warmup incomplete: {col} not ready", 0, warmup_complete=False)

        cur = df.iloc[-1]
        prev = df.iloc[-2]

        rsi_now = cur["rsi"]
        fast_now = cur[fast_col]
        slow_now = cur[slow_col]
        fast_prev = prev[fast_col]
        slow_prev = prev[slow_col]
        atr_now = cur["atr"]
        price_now = cur["close"]

        sma_cross_up = (fast_now > slow_now) and (fast_prev <= slow_prev)
        sma_cross_down = (fast_now < slow_now) and (fast_prev >= slow_prev)
        rsi_oversold_hit = rsi_now < rsi_oversold
        rsi_overbought_hit = rsi_now > rsi_overbought

        indicators_out = {
            "price": price_now,
            f"sma_{fast_sma}": fast_now,
            f"sma_{slow_sma}": slow_now,
            "rsi": rsi_now,
            "atr": atr_now,
        }

        # Stop loss check (if in position)
        if self._in_position and self._entry_price > 0:
            stop_price = self._entry_price - stop_atr_mult * atr_now
            if price_now <= stop_price:
                self._in_position = False
                self._entry_price = 0.0
                return {
                    "signal": "SELL",
                    "confidence": 0.95,
                    "reason": f"Weekly stop loss: {price_now:.2f} ≤ {stop_price:.2f}",
                    "indicators": indicators_out,
                    "warmup_complete": True,
                }

        # Exit signals (min hold enforced externally by main.py)
        if self._in_position and (rsi_overbought_hit or sma_cross_down):
            reason = (
                f"Weekly RSI overbought ({rsi_now:.1f} > {rsi_overbought})"
                if rsi_overbought_hit
                else f"Weekly SMA death-cross: SMA{fast_sma}={fast_now:.2f} < SMA{slow_sma}={slow_now:.2f}"
            )
            confidence = 0.80 if rsi_overbought_hit else 0.75
            self._in_position = False
            self._entry_price = 0.0
            return {
                "signal": "SELL",
                "confidence": confidence,
                "reason": reason,
                "indicators": indicators_out,
                "warmup_complete": True,
            }

        # Entry signals
        if not self._in_position and (rsi_oversold_hit or sma_cross_up):
            reasons = []
            confidence = 0.0
            if rsi_oversold_hit:
                reasons.append(f"Weekly RSI oversold ({rsi_now:.1f} < {rsi_oversold})")
                confidence = max(confidence, 0.75)
            if sma_cross_up:
                reasons.append(
                    f"Weekly golden cross: SMA{fast_sma}={fast_now:.2f} > SMA{slow_sma}={slow_now:.2f}"
                )
                confidence = max(confidence, 0.80)
            if rsi_oversold_hit and sma_cross_up:
                confidence = 0.90

            self._in_position = True
            self._entry_price = price_now
            return {
                "signal": "BUY",
                "confidence": confidence,
                "reason": " + ".join(reasons),
                "indicators": indicators_out,
                "warmup_complete": True,
            }

        return {
            "signal": "HOLD",
            "confidence": 0.0,
            "reason": (
                f"Holding: RSI={rsi_now:.1f}, SMA{fast_sma}={fast_now:.2f}, "
                f"SMA{slow_sma}={slow_now:.2f}"
                if self._in_position
                else f"No entry: RSI={rsi_now:.1f}, SMA{fast_sma}={fast_now:.2f} vs SMA{slow_sma}={slow_now:.2f}"
            ),
            "indicators": indicators_out,
            "warmup_complete": True,
        }

    def _is_bear_market(self, df: pd.DataFrame) -> bool:
        """Return True if price is below a declining trend SMA (bear market condition).

        For daily strategies: uses SMA_200 (200-day moving average).
        For greenblatt_weekly: uses the strategy's slow_sma (default 40 weeks ≈ 200 trading days).

        Requires both:
          1. Current price < trend SMA
          2. Trend SMA today < trend SMA 20 bars ago (declining)

        Returns False if insufficient data so the filter never blocks during warmup.
        """
        # Weekly strategies use slow_sma as the trend filter equivalent of SMA_200
        if self.strategy_name == "greenblatt_weekly":
            slow = self.params.get("slow_sma", 40)
            col = f"sma_{slow}"
            if col not in df.columns or len(df) < slow + 21:
                return False
            sma_now = df[col].iloc[-1]
            if pd.isna(sma_now):
                return False
            price_below = df["close"].iloc[-1] < sma_now
            if len(df) < 21:
                return False
            sma_prev = df[col].iloc[-21]
            if pd.isna(sma_prev) or sma_prev == 0:
                return False
            return price_below and (sma_now < sma_prev)

        # Daily strategies: standard SMA_200 filter
        if "sma_200" not in df.columns or len(df) < 221:
            return False

        sma_now = df["sma_200"].iloc[-1]
        if pd.isna(sma_now):
            return False

        price_below = df["close"].iloc[-1] < sma_now

        sma_prev = df["sma_200"].iloc[-21]  # 20 bars ago
        if pd.isna(sma_prev) or sma_prev == 0:
            return False

        sma_declining = sma_now < sma_prev

        return price_below and sma_declining

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
