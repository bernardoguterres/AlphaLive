"""
Signal Generation Engine

Generates buy/sell signals based on strategy logic and market data.
Supports 8 strategies with exact AlphaLab parity.

CRITICAL: Signal logic must match AlphaLab backtest exactly.
Any divergence means live results won't match backtest expectations.

Performance Budget: <5 seconds per strategy (all indicators + signal logic)
Expected: <0.5s for 200 bars on Railway's shared vCPU
"""

import logging
import os
import time
from typing import Callable, Dict, Any, Optional

import pandas as pd

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
        self._peak_price: float = 0.0  # for greenblatt_weekly trailing stop

        # vwap_reversion state machine (mirrors AlphaLab's position/cooldown
        # bookkeeping exactly; rewritten 2026-07-10 - was stateless, which
        # re-emitted entries every bar and never emitted the VWAP-return exit)
        self._vwap_position: int = 0  # 0=flat, 1=long, -1=short (bookkeeping)
        self._vwap_bars_since_signal: int = 10**9  # large = no cooldown at start
        self._is_new_bar: bool = True  # set per generate_signal() call

        # Minimum-hold gate for greenblatt_weekly's opt-in exits. Wired by
        # main.py to BotState.is_min_hold_met(); zero-arg, returns True when
        # the exit is allowed. None = no enforcement (tests, replay).
        # Must be checked INSIDE the signal method, before _in_position flips -
        # a veto after the SELL is emitted would desync engine state.
        self.min_hold_checker: Optional[Callable[[], bool]] = None

        # Indicator cache: skip recalculation when the same bar is checked again
        # (e.g. exit-checks during market hours reuse the morning's indicator values)
        self._cached_df: Optional[pd.DataFrame] = None
        self._cached_last_ts: Optional[Any] = None
        self._cached_df_len: int = 0

        # Dispatch table, derived from indicators.py's strategy registry rather
        # than a second hardcoded name list - adding a new strategy means one
        # @register_strategy_indicators-decorated function in indicators.py
        # plus one `_{name}_signal` method here, with only one place (the
        # indicator registry) declaring which strategies exist.
        self._dispatch: Dict[str, Callable] = {}
        for name in indicators.registered_strategy_names():
            handler = getattr(self, f"_{name}_signal", None)
            if handler is None:
                raise ValueError(
                    f"Strategy '{name}' is registered in indicators.py but has "
                    f"no matching SignalEngine._{name}_signal method"
                )
            self._dispatch[name] = handler

        logger.info(
            f"Signal engine initialized | Strategy: {self.strategy_name} | "
            f"Params: {self.params}"
        )

    def get_state(self) -> Dict[str, Any]:
        """Snapshot the stateful-strategy fields for persistence.

        Without persistence, a Railway restart mid-position makes stateful
        strategies (bollinger_rsi_combo, trend_adaptive_rsi, greenblatt_weekly)
        think they're flat: they can double-buy and never emit their exit,
        and the greenblatt trailing-stop peak resets to the post-restart price.
        """
        return {
            "in_position": self._in_position,
            "entry_price": self._entry_price,
            "peak_price": self._peak_price,
            "vwap_position": self._vwap_position,
            "vwap_bars_since_signal": self._vwap_bars_since_signal,
        }

    def restore_state(self, state: Optional[Dict[str, Any]]) -> None:
        """Restore fields captured by get_state(). None/missing keys keep defaults."""
        if not state:
            return
        self._in_position = bool(state.get("in_position", False))
        self._entry_price = float(state.get("entry_price", 0.0) or 0.0)
        self._peak_price = float(state.get("peak_price", 0.0) or 0.0)
        self._vwap_position = int(state.get("vwap_position", 0) or 0)
        self._vwap_bars_since_signal = int(
            state.get("vwap_bars_since_signal", 10**9) or 10**9
        )
        if self._in_position:
            logger.info(
                f"Signal engine state restored | {self.strategy_name}: in position "
                f"(entry ${self._entry_price:.2f}, peak ${self._peak_price:.2f})"
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
            logger.warning(
                "Insufficient data for signal generation (need at least 2 rows)"
            )
            return self._no_signal("Insufficient data", start_time)

        # Ensure required columns exist
        required_cols = ["open", "high", "low", "close", "volume"]
        missing_cols = [col for col in required_cols if col not in df.columns]
        if missing_cols:
            logger.error(f"Missing required columns: {missing_cols}")
            return self._no_signal(f"Missing columns: {missing_cols}", start_time)

        # Add indicators - skip if the last bar and row count are unchanged
        last_ts = df.index[-1] if len(df.index) else None
        cache_hit = (
            self._cached_df is not None
            and last_ts == self._cached_last_ts
            and len(df) == self._cached_df_len
        )
        # New-bar flag for stateful bar counting (vwap_reversion cooldown):
        # repeat checks on the same bar must not advance the count.
        self._is_new_bar = not cache_hit
        if cache_hit:
            df = self._cached_df
        else:
            try:
                df = indicators.add_all_for_strategy(
                    df, self.strategy_name, self.params
                )
            except Exception as e:
                logger.error(f"Failed to add indicators: {e}", exc_info=True)
                return self._no_signal(f"Indicator calculation failed: {e}", start_time)
            self._cached_df = df
            self._cached_last_ts = last_ts
            self._cached_df_len = len(df)

        # Bear market filter: block BUY signals when not in a position and price is
        # below a declining SMA_200. Allows SELL/exits to proceed normally.
        if not self._in_position and self._is_bear_market(df):
            result = {
                "signal": "HOLD",
                "confidence": 0.0,
                "reason": (
                    f"Bear market filter: price below declining SMA_200 "
                    f"({df['close'].iloc[-1]:.2f} < {df['sma_200'].iloc[-1]:.2f}) - BUY blocked"
                ),
                "indicators": {
                    "price": df["close"].iloc[-1],
                    "sma_200": df["sma_200"].iloc[-1],
                },
                "warmup_complete": True,
                "generation_time_ms": int((time.time() - start_time) * 1000),
            }
            logger.debug(f"Bear market filter triggered: {result['reason']}")
            return result

        # Route to strategy-specific logic
        try:
            handler = self._dispatch.get(self.strategy_name)
            if handler is None:
                logger.error(f"Unknown strategy: {self.strategy_name}")
                return self._no_signal(
                    f"Unknown strategy: {self.strategy_name}", start_time
                )
            result = handler(df)

        except Exception as e:
            logger.error(f"Signal generation failed: {e}", exc_info=True)
            return self._no_signal(f"Signal generation error: {e}", start_time)

        # Add generation time
        elapsed = time.time() - start_time
        result["generation_time_ms"] = int(elapsed * 1000)

        # Performance warning
        if elapsed > 5.0:
            logger.warning(
                f"Signal generation SLOW: {self.strategy_name} took {elapsed:.2f}s "
                f"(budget: 5s). Optimize indicators or reduce lookback."
            )
        else:
            logger.debug(f"Signal generation time: {elapsed:.3f}s")

        # Log signal
        if result["signal"] != "HOLD":
            logger.info(
                f"SIGNAL: {result['signal']} | Confidence: {result['confidence']:.2%} | "
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
                warmup_complete=False,
            )

        # Current and previous values
        fast_curr = df[fast_col].iloc[-1]
        fast_prev = df[fast_col].iloc[-2]
        slow_curr = df[slow_col].iloc[-1]
        slow_prev = df[slow_col].iloc[-2]

        current_price = df["close"].iloc[-1]

        # Extract indicator values
        indicators = {fast_col: fast_curr, slow_col: slow_curr, "price": current_price}

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
                "warmup_complete": True,
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
                "warmup_complete": True,
            }

        else:
            # No crossover
            return {
                "signal": "HOLD",
                "confidence": 0.0,
                "reason": f"No crossover (Fast={fast_curr:.2f}, Slow={slow_curr:.2f})",
                "indicators": indicators,
                "warmup_complete": True,
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
                warmup_complete=False,
            )

        rsi_curr = df[rsi_col].iloc[-1]
        current_price = df["close"].iloc[-1]

        indicators = {
            rsi_col: rsi_curr,
            "oversold": oversold,
            "overbought": overbought,
            "price": current_price,
        }

        # Check thresholds
        if rsi_curr < oversold:
            # Oversold - BUY
            distance = oversold - rsi_curr
            confidence = min(
                1.0, distance / oversold
            )  # Further from threshold = higher confidence

            return {
                "signal": "BUY",
                "confidence": confidence,
                "reason": (
                    f"RSI oversold: RSI({period})={rsi_curr:.2f} < {oversold} "
                    f"(distance: {distance:.2f})"
                ),
                "indicators": indicators,
                "warmup_complete": True,
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
                "warmup_complete": True,
            }

        else:
            # Neutral zone
            return {
                "signal": "HOLD",
                "confidence": 0.0,
                "reason": f"RSI neutral: {rsi_curr:.2f} (range: {oversold}-{overbought})",
                "indicators": indicators,
                "warmup_complete": True,
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
        rolling_high = df["rolling_high"].iloc[-1]
        volume_ma = df[f"volume_ma_{volume_ma_period}"].iloc[-1]
        atr = df[f"atr_{atr_period}"].iloc[-1]

        if pd.isna(rolling_high) or pd.isna(volume_ma) or pd.isna(atr):
            return self._no_signal(
                f"Warmup incomplete (need {max(lookback, volume_ma_period, atr_period)} bars)",
                time.time(),
                warmup_complete=False,
            )

        current_price = df["close"].iloc[-1]
        current_volume = df["volume"].iloc[-1]

        indicators = {
            "price": current_price,
            "rolling_high": rolling_high,
            "volume": current_volume,
            f"volume_ma_{volume_ma_period}": volume_ma,
            f"atr_{atr_period}": atr,
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
                "warmup_complete": True,
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
                "warmup_complete": True,
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
        bb_upper = df["bb_upper"].iloc[-1]
        bb_lower = df["bb_lower"].iloc[-1]
        bb_middle = df["bb_middle"].iloc[-1]
        volume_ma = df[f"volume_ma_{volume_ma_period}"].iloc[-1]

        if pd.isna(bb_upper) or pd.isna(bb_lower) or pd.isna(volume_ma):
            return self._no_signal(
                f"Warmup incomplete (need {max(period, volume_ma_period)} bars)",
                time.time(),
                warmup_complete=False,
            )

        # Need enough rows for confirmation check
        if len(df) < confirmation_bars:
            return self._no_signal(
                f"Need {confirmation_bars} bars for confirmation",
                time.time(),
                warmup_complete=False,
            )

        current_price = df["close"].iloc[-1]
        current_volume = df["volume"].iloc[-1]

        indicators = {
            "price": current_price,
            "bb_upper": bb_upper,
            "bb_middle": bb_middle,
            "bb_lower": bb_lower,
            "volume": current_volume,
            f"volume_ma_{volume_ma_period}": volume_ma,
        }

        # Check confirmation bars for upper breakout
        upper_confirmed = all(
            df["close"].iloc[-i] > df["bb_upper"].iloc[-i]
            for i in range(1, confirmation_bars + 1)
        )

        # Check confirmation bars for lower breakdown
        lower_confirmed = all(
            df["close"].iloc[-i] < df["bb_lower"].iloc[-i]
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
                "warmup_complete": True,
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
                "warmup_complete": True,
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
                "warmup_complete": True,
            }

    def _vwap_reversion_signal(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        VWAP Mean Reversion Strategy - stateful, mirroring AlphaLab exactly.

        State machine (position bookkeeping, matches AlphaLab bar-for-bar):
          flat:  BUY  when close < VWAP - dev*std AND RSI < oversold  -> long
                 SELL when close > VWAP + dev*std AND RSI > overbought -> short*
          long:  SELL when close >= VWAP (mean-reversion target reached)
          short: BUY  when close <= VWAP
        A cooldown of `cooldown_days` bars follows every signal.

        *Short entries are signal bookkeeping only - live execution is
        long-only (OrderManager blocks SELLs with no position) - but the
        state machine must track them or every subsequent signal diverges
        from the AlphaLab backtest.

        Rewritten 2026-07-10: was a stateless band-crosser (re-emitted
        entries every bar, no VWAP-return exit, no cooldown) on top of a
        cumulative VWAP - a different strategy than AlphaLab backtests.
        """
        deviation_threshold = self.params.get("deviation_threshold", 2.0)
        rsi_period = self.params.get("rsi_period", 14)
        oversold = self.params.get("oversold", 30)
        overbought = self.params.get("overbought", 70)
        cooldown = self.params.get("cooldown_days", 3)

        rsi_col = f"rsi_{rsi_period}"

        # Advance the bar counter once per new bar (repeat same-bar checks
        # must not consume cooldown).
        if self._is_new_bar and self._vwap_bars_since_signal < 10**9:
            self._vwap_bars_since_signal += 1

        # Check warmup
        vwap = df["vwap"].iloc[-1]
        vwap_std = df["vwap_std"].iloc[-1]
        rsi = df[rsi_col].iloc[-1]

        if pd.isna(vwap) or pd.isna(vwap_std) or pd.isna(rsi):
            return self._no_signal(
                "Warmup incomplete (VWAP/std/RSI not ready)",
                time.time(),
                warmup_complete=False,
            )

        current_price = df["close"].iloc[-1]
        upper_band = vwap + (deviation_threshold * vwap_std)
        lower_band = vwap - (deviation_threshold * vwap_std)
        deviation = (current_price - vwap) / vwap_std if vwap_std > 0 else 0

        indicators = {
            "price": current_price,
            "vwap": vwap,
            "vwap_std": vwap_std,
            "upper_band": upper_band,
            "lower_band": lower_band,
            "deviation": deviation,
            rsi_col: rsi,
            "position": self._vwap_position,
        }

        def _hold(reason: str) -> Dict[str, Any]:
            return {
                "signal": "HOLD",
                "confidence": 0.0,
                "reason": reason,
                "indicators": indicators,
                "warmup_complete": True,
            }

        def _emit(signal: str, confidence: float, reason: str) -> Dict[str, Any]:
            self._vwap_bars_since_signal = 0
            return {
                "signal": signal,
                "confidence": confidence,
                "reason": reason,
                "indicators": indicators,
                "warmup_complete": True,
            }

        # Cooldown - mirrors AlphaLab's `i - last_signal_idx <= cooldown`
        if self._vwap_bars_since_signal <= cooldown:
            return _hold(
                f"Cooldown: {self._vwap_bars_since_signal}/{cooldown} bars since last signal"
            )

        if self._vwap_position == 0:
            if current_price < lower_band and rsi < oversold:
                self._vwap_position = 1
                return _emit(
                    "BUY",
                    min(1.0, (lower_band - current_price) / lower_band * 10),
                    (
                        f"VWAP oversold reversion: Price {current_price:.2f} < "
                        f"VWAP-{deviation_threshold}σ={lower_band:.2f}, "
                        f"RSI={rsi:.2f} < {oversold}"
                    ),
                )
            if current_price > upper_band and rsi > overbought:
                self._vwap_position = -1
                return _emit(
                    "SELL",
                    min(1.0, (current_price - upper_band) / upper_band * 10),
                    (
                        f"VWAP overbought reversion: Price {current_price:.2f} > "
                        f"VWAP+{deviation_threshold}σ={upper_band:.2f}, "
                        f"RSI={rsi:.2f} > {overbought}"
                    ),
                )
            return _hold(
                f"No entry: deviation {deviation:.2f}σ, RSI {rsi:.1f} "
                f"(need <{oversold} below -{deviation_threshold}σ or "
                f">{overbought} above +{deviation_threshold}σ)"
            )

        if self._vwap_position == 1:
            if current_price >= vwap:
                self._vwap_position = 0
                return _emit(
                    "SELL",
                    0.8,
                    f"Exit long: price {current_price:.2f} returned to VWAP {vwap:.2f}",
                )
            return _hold(
                f"Holding long: price {current_price:.2f} below VWAP {vwap:.2f}"
            )

        # self._vwap_position == -1
        if current_price <= vwap:
            self._vwap_position = 0
            return _emit(
                "BUY",
                0.8,
                f"Exit short: price {current_price:.2f} returned to VWAP {vwap:.2f}",
            )
        return _hold(f"Holding short: price {current_price:.2f} above VWAP {vwap:.2f}")

    def _bollinger_rsi_combo_signal(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Bollinger Bands + RSI Combination Strategy.

        BUY: Price <= Lower BB AND RSI < oversold (default 45) - only when not in position
        SELL: Price >= Middle BB OR RSI > overbought (default 55) - only when in position

        Stateful: tracks in_position via self._in_position to match AlphaLab exactly.
        AlphaLab Parity: Matches bollinger_rsi_combo.py implementation.
        """
        bb_period = self.params.get("bb_period", 20)
        rsi_period = self.params.get("rsi_period", 14)
        rsi_oversold = self.params.get("rsi_oversold", 45)
        rsi_overbought = self.params.get("rsi_overbought", 55)
        exit_at_middle = self.params.get("exit_at_middle", True)

        bb_lower = df["bb_lower"].iloc[-1]
        bb_middle = df["bb_middle"].iloc[-1]
        bb_upper = df["bb_upper"].iloc[-1]
        rsi_col = f"rsi_{rsi_period}"
        rsi = df[rsi_col].iloc[-1]

        if pd.isna(bb_lower) or pd.isna(bb_middle) or pd.isna(rsi):
            return self._no_signal(
                f"Warmup incomplete (need {max(bb_period, rsi_period)} bars)",
                time.time(),
                warmup_complete=False,
            )

        current_price = df["close"].iloc[-1]

        indicators = {
            "price": current_price,
            "bb_lower": bb_lower,
            "bb_middle": bb_middle,
            "bb_upper": bb_upper,
            rsi_col: rsi,
        }

        if self._in_position:
            # Look for exit: price reached middle BB or RSI overbought
            exit_reason = None
            if exit_at_middle and current_price >= bb_middle:
                pnl_pct = (
                    (current_price - self._entry_price) / self._entry_price
                ) * 100
                exit_reason = f"BB middle reached (+{pnl_pct:.1f}%)"
            elif rsi > rsi_overbought:
                pnl_pct = (
                    (current_price - self._entry_price) / self._entry_price
                ) * 100
                exit_reason = f"RSI overbought {rsi:.1f} ({pnl_pct:+.1f}%)"

            if exit_reason:
                conf = min(1.0, (rsi - rsi_overbought) / (100 - rsi_overbought) + 0.5)
                self._in_position = False
                return {
                    "signal": "SELL",
                    "confidence": conf,
                    "reason": exit_reason,
                    "indicators": indicators,
                    "warmup_complete": True,
                }
        else:
            # Look for entry: price at/below lower BB AND RSI oversold
            if current_price <= bb_lower and rsi < rsi_oversold:
                bb_penetration = (bb_lower - current_price) / bb_lower * 100
                rsi_distance = (rsi_oversold - rsi) / rsi_oversold
                confidence = min(
                    1.0, max(0.3, (bb_penetration * 10 + rsi_distance) / 2)
                )
                self._in_position = True
                self._entry_price = current_price
                return {
                    "signal": "BUY",
                    "confidence": confidence,
                    "reason": f"BB lower touch + RSI {rsi:.1f}",
                    "indicators": indicators,
                    "warmup_complete": True,
                }

        return {
            "signal": "HOLD",
            "confidence": 0.0,
            "reason": (
                f"No setup: Price {current_price:.2f} in range "
                f"[{bb_lower:.2f}, {bb_middle:.2f}], RSI {rsi:.2f}"
            ),
            "indicators": indicators,
            "warmup_complete": True,
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
                warmup_complete=False,
            )

        sma_col = f"sma_{trend_sma}"
        rsi_col = f"rsi_{rsi_period}"

        if sma_col not in df.columns or rsi_col not in df.columns:
            return self._no_signal(
                f"Missing required indicators: {sma_col}, {rsi_col}",
                time.time(),
                warmup_complete=False,
            )

        current_price = df["close"].iloc[-1]
        sma_curr = df[sma_col].iloc[-1]
        rsi_curr = df[rsi_col].iloc[-1]

        if pd.isna(sma_curr) or pd.isna(rsi_curr):
            return self._no_signal(
                f"Warmup incomplete (need {max(trend_sma, rsi_period)} bars)",
                time.time(),
                warmup_complete=False,
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
            "sell_threshold": sell_threshold,
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
                    "warmup_complete": True,
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
                    "warmup_complete": True,
                }

        return {
            "signal": "HOLD",
            "confidence": 0.0,
            "reason": (
                f"No signal ({regime}): RSI {rsi_curr:.2f} in neutral zone "
                f"[{buy_threshold}, {sell_threshold}]"
            ),
            "indicators": indicators,
            "warmup_complete": True,
        }

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _greenblatt_weekly_signal(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Greenblatt Weekly strategy on the last weekly bar.

        Entry: Weekly RSI < rsi_oversold OR 10w/50w golden cross.
        Exit:  Trailing stop (20% below peak) always fires - bypasses min hold.
               RSI/SMA exits only if exit_rsi_overbought/exit_sma_cross=True,
               and only once self.min_hold_checker() returns True (wired by
               main.py to BotState.is_min_hold_met(); entry timestamps are
               recorded on every real BUY fill). Suppressed exits return HOLD
               without flipping _in_position.
        """
        p = self.params
        fast_sma = p.get("fast_sma", 10)
        slow_sma = p.get("slow_sma", 50)
        rsi_oversold = p.get("rsi_oversold", 35)
        rsi_overbought = p.get("rsi_overbought", 65)
        trailing_stop_pct = p.get("trailing_stop_pct", 0.20)
        exit_rsi = p.get("exit_rsi_overbought", False)
        exit_sma = p.get("exit_sma_cross", False)

        fast_col = f"sma_{fast_sma}"
        slow_col = f"sma_{slow_sma}"
        rsi_col = f"rsi_{p.get('rsi_period', 14)}"

        if len(df) < 2:
            return self._no_signal("Insufficient weekly bars", 0, warmup_complete=False)

        for col in [fast_col, slow_col, rsi_col]:
            if col not in df.columns or pd.isna(df[col].iloc[-1]):
                return self._no_signal(
                    f"Warmup incomplete: {col} not ready", 0, warmup_complete=False
                )

        cur = df.iloc[-1]
        prev = df.iloc[-2]

        price_now = cur["close"]
        rsi_now = cur[rsi_col]
        fast_now = cur[fast_col]
        slow_now = cur[slow_col]
        fast_prev = prev[fast_col]
        slow_prev = prev[slow_col]

        indicators_out = {
            "price": price_now,
            f"sma_{fast_sma}": fast_now,
            f"sma_{slow_sma}": slow_now,
            "rsi": rsi_now,
        }

        # Update peak price while in position
        if self._in_position and price_now > self._peak_price:
            self._peak_price = price_now

        # --- EXIT logic ---
        if self._in_position:
            trailing_stop_level = self._peak_price * (1 - trailing_stop_pct)
            if price_now <= trailing_stop_level:
                self._in_position = False
                self._entry_price = 0.0
                self._peak_price = 0.0
                return {
                    "signal": "SELL",
                    "confidence": 0.95,
                    "reason": (
                        f"Trailing stop: {price_now:.2f} ≤ {trailing_stop_level:.2f} "
                        f"({trailing_stop_pct:.0%} below peak {self._peak_price:.2f})"
                    ),
                    "indicators": indicators_out,
                    "warmup_complete": True,
                }

            sma_cross_down = (fast_now < slow_now) and (fast_prev >= slow_prev)
            rsi_ob = rsi_now > rsi_overbought

            # Optional exits - gated on minimum hold (trailing stop above is
            # NOT gated: it bypasses min hold by design). Checked BEFORE the
            # state flip so a suppressed exit leaves the engine in-position.
            if (exit_rsi and rsi_ob) or (exit_sma and sma_cross_down):
                if self.min_hold_checker is not None and not self.min_hold_checker():
                    return {
                        "signal": "HOLD",
                        "confidence": 0.0,
                        "reason": (
                            "Optional exit condition met but minimum hold "
                            "not elapsed - exit suppressed"
                        ),
                        "indicators": indicators_out,
                        "warmup_complete": True,
                    }
                reason = (
                    f"RSI overbought ({rsi_now:.1f} > {rsi_overbought})"
                    if (exit_rsi and rsi_ob)
                    else f"SMA death-cross: SMA{fast_sma}={fast_now:.2f} < SMA{slow_sma}={slow_now:.2f}"
                )
                self._in_position = False
                self._entry_price = 0.0
                self._peak_price = 0.0
                return {
                    "signal": "SELL",
                    "confidence": 0.75,
                    "reason": reason,
                    "indicators": indicators_out,
                    "warmup_complete": True,
                }

        # --- ENTRY logic ---
        sma_cross_up = (fast_now > slow_now) and (fast_prev <= slow_prev)
        rsi_os = rsi_now < rsi_oversold

        if not self._in_position and (rsi_os or sma_cross_up):
            reasons, confidence = [], 0.0
            if rsi_os:
                reasons.append(f"Weekly RSI oversold ({rsi_now:.1f} < {rsi_oversold})")
                confidence = max(confidence, 0.75)
            if sma_cross_up:
                reasons.append(
                    f"Weekly golden cross: SMA{fast_sma}={fast_now:.2f} > SMA{slow_sma}={slow_now:.2f}"
                )
                confidence = max(confidence, 0.80)
            if rsi_os and sma_cross_up:
                confidence = 0.90

            self._in_position = True
            self._entry_price = price_now
            self._peak_price = price_now
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
                f"Holding: RSI={rsi_now:.1f}, peak={self._peak_price:.2f}, "
                f"stop={self._peak_price*(1-trailing_stop_pct):.2f}"
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
        # Env toggle (default ON). MASTER_PLAN documented this toggle since
        # 2026-05 but it was never implemented - the filter was always-on.
        # Parity tests also need it off: the filter is a deliberate
        # AlphaLive-only overlay, not part of the shared signal logic.
        if os.environ.get("ENABLE_BEAR_MARKET_FILTER", "true").lower() in (
            "false", "0", "no",
        ):
            return False

        # Weekly strategies use slow_sma as the trend filter equivalent of SMA_200
        if self.strategy_name == "greenblatt_weekly":
            slow = self.params.get("slow_sma", 50)
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
        self, reason: str, start_time: float, warmup_complete: bool = True
    ) -> Dict[str, Any]:
        """Return a HOLD signal with reason."""
        elapsed = time.time() - start_time

        return {
            "signal": "HOLD",
            "confidence": 0.0,
            "reason": reason,
            "indicators": {},
            "warmup_complete": warmup_complete,
            "generation_time_ms": int(elapsed * 1000),
        }
