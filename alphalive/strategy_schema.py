"""
AlphaLive Strategy Schema - Pydantic v2 Models

This module defines the canonical schema for strategy configurations in AlphaLive.
Strategies exported from AlphaLab are validated against this schema before execution.

Schema Version: 1.0
"""

import logging
from typing import Any, Dict, Literal, Optional, Type

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


# Strategy names supported by AlphaLive
StrategyName = Literal[
    "ma_crossover",
    "rsi_mean_reversion",
    "momentum_breakout",
    "bollinger_breakout",
    "vwap_reversion",
    "bollinger_rsi_combo",  # Bollinger + RSI combination
    "trend_adaptive_rsi",  # Trend-adaptive RSI thresholds
    "greenblatt_weekly",  # Value factor strategy on weekly bars (weeks/months holding)
]

# Timeframes supported by AlphaLive
# 1Week: fetches daily bars from Alpaca and resamples internally
Timeframe = Literal["1Day", "1Hour", "15Min", "1Week"]

# Order types supported
OrderType = Literal["market", "limit"]


# ============================================================================
# Per-Strategy Parameter Models
# ============================================================================
#
# strategy.parameters was previously an untyped Dict[str, Any] - any field
# AlphaLab exported that AlphaLive didn't happen to read under the exact
# same name was silently dropped rather than flagged (audit 2026-07-13,
# Group 1 bug 1.5). These models make strategy.parameters strict per
# strategy (extra="forbid") so a field-name mismatch between AlphaLab's
# export and AlphaLive's own signal_engine.py/indicators.py fails loudly at
# config-load time instead of silently falling back to a default. Field
# names and defaults here are the AlphaLive-side canonical export contract
# (see AlphaLab's strategy_schema.py for the export-mapping layer that
# translates AlphaLab's internal strategy-class field names into these).


class MACrossoverStrategyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_type: Literal["ma_crossover"] = "ma_crossover"
    fast_period: int = Field(default=10, ge=2, le=500)
    slow_period: int = Field(default=20, ge=3, le=500)
    volume_confirmation: bool = Field(default=False)
    volume_avg_period: int = Field(default=20, ge=5, le=100)
    min_separation_pct: float = Field(default=0.0, ge=0.0, le=10.0)
    cooldown_days: int = Field(default=5, ge=0, le=100)


class RSIMeanReversionStrategyParams(BaseModel):
    """period is what signal_engine.py's _rsi_mean_reversion_signal() and
    indicators.py actually read; AlphaLab's export schema currently emits
    rsi_period instead (a pre-existing name mismatch independent of this
    session's Group 1 fixes - see audit Blocker #3, "rsi_mean_reversion
    runs a fundamentally different strategy live than backtested", which
    covers this strategy's parity gap and is scoped to a later session).
    rsi_period is accepted here too so real AlphaLab exports don't hard-fail
    on load, but it is NOT read by signal_engine.py today - only period is.
    """

    model_config = ConfigDict(extra="forbid")

    strategy_type: Literal["rsi_mean_reversion"] = "rsi_mean_reversion"
    period: int = Field(default=14, ge=2, le=100)
    rsi_period: Optional[int] = Field(default=None, ge=2, le=100)
    oversold: int = Field(default=30, ge=1, le=49)
    overbought: int = Field(default=70, ge=51, le=99)
    use_bb_confirmation: bool = Field(default=False)
    bb_period: int = Field(default=20, ge=5, le=100)
    bb_std: float = Field(default=2.0, ge=1.0, le=4.0)
    use_adx_filter: bool = Field(default=False)
    adx_threshold: int = Field(default=25, ge=10, le=50)
    stop_loss_atr_mult: float = Field(default=2.0, ge=0.5, le=10.0)
    max_holding_days: int = Field(default=40, ge=1, le=365)


class MomentumBreakoutStrategyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_type: Literal["momentum_breakout"] = "momentum_breakout"
    lookback: int = Field(default=20, ge=5, le=200)
    surge_pct: float = Field(default=1.5, ge=1.0, le=10.0)
    volume_ma_period: int = Field(default=20, ge=5, le=100)
    atr_period: int = Field(default=14, ge=2, le=100)
    rsi_min: int = Field(default=50, ge=30, le=80)
    stop_loss_atr_mult: float = Field(default=2.0, ge=0.5, le=10.0)
    trailing_stop_atr_mult: float = Field(default=3.0, ge=0.5, le=10.0)
    cooldown_days: int = Field(default=3, ge=0, le=100)


class BollingerBreakoutStrategyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_type: Literal["bollinger_breakout"] = "bollinger_breakout"
    period: int = Field(default=20, ge=5, le=100)
    std_dev: float = Field(default=2.0, ge=0.5, le=4.0)
    confirmation_bars: int = Field(default=2, ge=1, le=5)
    volume_ma_period: int = Field(default=20, ge=5, le=100)
    volume_filter: bool = Field(default=True)
    volume_threshold: float = Field(default=1.5, ge=1.0, le=5.0)
    cooldown_days: int = Field(default=3, ge=0, le=100)


class VWAPReversionStrategyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_type: Literal["vwap_reversion"] = "vwap_reversion"
    vwap_period: int = Field(default=20, ge=5, le=50)
    vwap_std_period: Optional[int] = Field(default=None, ge=5, le=50)
    deviation_threshold: float = Field(default=2.0, ge=0.5, le=5.0)
    rsi_period: int = Field(default=14, ge=5, le=30)
    oversold: int = Field(default=30, ge=10, le=40)
    overbought: int = Field(default=70, ge=60, le=90)
    cooldown_days: int = Field(default=3, ge=0, le=100)


class BollingerRSIComboStrategyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_type: Literal["bollinger_rsi_combo"] = "bollinger_rsi_combo"
    bb_period: int = Field(default=20, ge=5, le=100)
    bb_std: float = Field(default=2.0, ge=0.5, le=5.0)
    rsi_period: int = Field(default=14, ge=2, le=50)
    rsi_oversold: int = Field(default=45, ge=10, le=50)
    rsi_overbought: int = Field(default=55, ge=50, le=90)
    exit_at_middle: bool = Field(default=True)


class TrendAdaptiveRSIStrategyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_type: Literal["trend_adaptive_rsi"] = "trend_adaptive_rsi"
    rsi_period: int = Field(default=14, ge=2, le=50)
    trend_sma: int = Field(default=50, ge=10, le=200)
    trend_lookback: int = Field(default=5, ge=1, le=20)
    uptrend_buy: int = Field(default=45, ge=20, le=60)
    uptrend_sell: int = Field(default=65, ge=55, le=90)
    downtrend_buy: int = Field(default=35, ge=10, le=50)
    downtrend_sell: int = Field(default=55, ge=45, le=80)
    range_buy: int = Field(default=35, ge=10, le=50)
    range_sell: int = Field(default=65, ge=50, le=90)


class GreenblattWeeklyStrategyParams(BaseModel):
    """trailing_stop_fraction (0-1 fraction) is deliberately not named
    trailing_stop_pct - that name is reserved for Risk.trailing_stop_pct
    below, an absolute percentage. Same name, incompatible units, in the
    same document was audit bug 1.4.
    """

    model_config = ConfigDict(extra="forbid")

    strategy_type: Literal["greenblatt_weekly"] = "greenblatt_weekly"
    fast_sma: int = Field(default=10, ge=2, le=50)
    slow_sma: int = Field(default=50, ge=5, le=200)
    rsi_period: int = Field(default=14, ge=2, le=50)
    rsi_oversold: int = Field(default=35, ge=10, le=49)
    rsi_overbought: int = Field(default=65, ge=51, le=90)
    min_hold_bars: int = Field(default=52, ge=1, le=260)
    trailing_stop_fraction: float = Field(default=0.20, ge=0.05, le=0.50)
    exit_rsi_overbought: bool = Field(default=False)
    exit_sma_cross: bool = Field(default=False)


_STRATEGY_PARAM_MODELS: Dict[str, Type[BaseModel]] = {
    "ma_crossover": MACrossoverStrategyParams,
    "rsi_mean_reversion": RSIMeanReversionStrategyParams,
    "momentum_breakout": MomentumBreakoutStrategyParams,
    "bollinger_breakout": BollingerBreakoutStrategyParams,
    "vwap_reversion": VWAPReversionStrategyParams,
    "bollinger_rsi_combo": BollingerRSIComboStrategyParams,
    "trend_adaptive_rsi": TrendAdaptiveRSIStrategyParams,
    "greenblatt_weekly": GreenblattWeeklyStrategyParams,
}


class BacktestPeriod(BaseModel):
    """Backtest period from AlphaLab"""

    start: str = Field(..., description="Start date (YYYY-MM-DD)")
    end: str = Field(..., description="End date (YYYY-MM-DD)")


class Performance(BaseModel):
    """Backtest performance metrics from AlphaLab"""

    sharpe_ratio: float = Field(..., description="Sharpe ratio")
    sortino_ratio: float = Field(..., description="Sortino ratio")
    total_return_pct: float = Field(..., description="Total return percentage")
    max_drawdown_pct: float = Field(..., description="Maximum drawdown percentage")
    win_rate_pct: float = Field(..., description="Win rate percentage")
    profit_factor: float = Field(..., description="Profit factor")
    total_trades: int = Field(..., description="Total number of trades")
    calmar_ratio: float = Field(..., description="Calmar ratio")


class Metadata(BaseModel):
    """Metadata about the strategy export from AlphaLab"""

    exported_from: str = Field(..., description="Source system (e.g., AlphaLab)")
    exported_at: str = Field(..., description="Export timestamp (ISO 8601)")
    alphalab_version: str = Field(..., description="AlphaLab version")
    backtest_id: str = Field(..., description="Backtest identifier")
    backtest_period: BacktestPeriod = Field(..., description="Backtest period")
    performance: Performance = Field(..., description="Backtest performance metrics")


class Risk(BaseModel):
    """Risk management parameters"""

    stop_loss_pct: float = Field(
        ..., description="Stop loss percentage", ge=0.1, le=50.0
    )
    take_profit_pct: float = Field(
        ..., description="Take profit percentage", ge=0.5, le=100.0
    )
    max_position_size_pct: float = Field(
        ..., description="Max position size as % of portfolio", ge=1.0, le=100.0
    )
    max_daily_loss_pct: float = Field(
        ..., description="Max daily loss as % of portfolio", ge=0.5, le=20.0
    )
    max_open_positions: int = Field(
        ..., description="Max open positions per strategy", ge=1, le=50
    )
    portfolio_max_positions: int = Field(
        ..., description="Max positions across all strategies", ge=1, le=100
    )
    trailing_stop_enabled: bool = Field(
        default=False, description="Enable trailing stop"
    )
    trailing_stop_pct: Optional[float] = Field(
        default=None, description="Trailing stop percentage", ge=0.5, le=20.0
    )
    commission_per_trade: float = Field(
        default=0.0, description="Commission per trade (USD)", ge=0.0, le=50.0
    )

    @field_validator("stop_loss_pct")
    @classmethod
    def validate_stop_loss(cls, v: float) -> float:
        """Validate stop loss and warn if unusually wide"""
        if v > 15.0:
            logger.warning(f"Very wide stop loss ({v}%) - verify this is intentional")
        return v

    @field_validator("take_profit_pct")
    @classmethod
    def validate_take_profit(cls, v: float) -> float:
        """Validate take profit and warn if aggressive"""
        if v > 50.0:
            logger.warning(
                f"Aggressive take profit target ({v}%) - may be difficult to achieve"
            )
        return v

    @field_validator("trailing_stop_pct")
    @classmethod
    def validate_trailing_stop(cls, v: Optional[float]) -> Optional[float]:
        """Validate trailing stop and warn if wide"""
        if v is not None and v > 10.0:
            logger.warning(
                f"Wide trailing stop ({v}%) - may give back significant gains"
            )
        return v

    @model_validator(mode="after")
    def validate_portfolio_positions(self) -> "Risk":
        """Ensure portfolio_max_positions >= max_open_positions"""
        if self.portfolio_max_positions < self.max_open_positions:
            raise ValueError(
                f"portfolio_max_positions ({self.portfolio_max_positions}) must be >= "
                f"max_open_positions ({self.max_open_positions})"
            )
        return self

    @model_validator(mode="after")
    def validate_trailing_stop_config(self) -> "Risk":
        """Ensure trailing_stop_pct is set if trailing_stop_enabled"""
        if self.trailing_stop_enabled and self.trailing_stop_pct is None:
            raise ValueError(
                "trailing_stop_pct must be set when trailing_stop_enabled is True"
            )
        return self


class Execution(BaseModel):
    """Order execution parameters"""

    order_type: OrderType = Field(..., description="Order type (market or limit)")
    limit_offset_pct: float = Field(
        default=0.1, description="Limit order offset percentage"
    )
    cooldown_bars: int = Field(
        default=1, description="Cooldown period in bars between trades", ge=0
    )


class SafetyLimits(BaseModel):
    """Safety limits to prevent runaway behavior"""

    max_trades_per_day: int = Field(
        default=20, description="Max trades per day", ge=1, le=200
    )
    max_api_calls_per_hour: int = Field(
        default=500, description="Max API calls per hour", ge=100, le=2000
    )
    signal_generation_timeout_seconds: float = Field(
        default=5.0,
        description="Timeout for signal generation in seconds",
        ge=1.0,
        le=30.0,
    )
    broker_degraded_mode_threshold_failures: int = Field(
        default=3,
        description="Number of broker failures before entering degraded mode",
        ge=1,
        le=10,
    )

    @field_validator("max_trades_per_day")
    @classmethod
    def validate_max_trades(cls, v: int) -> int:
        """Validate max trades per day and warn if high"""
        if v > 50:
            logger.warning(
                f"High trade frequency ({v} trades/day) - verify strategy logic"
            )
        return v


class Strategy(BaseModel):
    """Strategy configuration.

    parameters is validated against the strategy's own strict parameter
    model (see _STRATEGY_PARAM_MODELS above) and normalized back into a
    dict with defaults filled in, so downstream consumers (SignalEngine,
    main.py) can keep using plain dict .get() calls. A field AlphaLive
    doesn't recognize for this strategy - most often a name/unit mismatch
    with AlphaLab's export - now raises a validation error instead of
    being silently dropped.
    """

    name: StrategyName = Field(..., description="Strategy name")
    parameters: Dict[str, Any] = Field(
        default_factory=dict, description="Strategy-specific parameters"
    )
    description: Optional[str] = Field(
        default=None, description="Human-readable strategy description"
    )

    @model_validator(mode="after")
    def validate_and_normalize_parameters(self) -> "Strategy":
        model_cls = _STRATEGY_PARAM_MODELS.get(self.name)
        if model_cls is None:
            raise ValueError(
                f"No parameter schema registered for strategy '{self.name}'"
            )
        validated = model_cls.model_validate(self.parameters)
        self.parameters = validated.model_dump()
        return self


class StrategySchema(BaseModel):
    """
    Complete strategy configuration schema for AlphaLive.

    This is the canonical representation of a strategy exported from AlphaLab
    and imported into AlphaLive for live trading.
    """

    schema_version: Literal["1.0"] = Field(
        ..., description="Schema version (must be 1.0)"
    )
    strategy: Strategy = Field(..., description="Strategy configuration")
    ticker: str = Field(..., description="Ticker symbol (e.g., AAPL)")
    timeframe: Timeframe = Field(..., description="Trading timeframe")
    risk: Risk = Field(..., description="Risk management parameters")
    execution: Execution = Field(..., description="Execution parameters")
    safety_limits: SafetyLimits = Field(
        default_factory=SafetyLimits,
        description="Safety limits (defaults applied if missing)",
    )
    metadata: Metadata = Field(..., description="Export metadata from AlphaLab")

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, v: str) -> str:
        """Ensure schema version is exactly 1.0"""
        if v != "1.0":
            raise ValueError(f"Unsupported schema version: {v}. Expected 1.0")
        return v

    @field_validator("ticker")
    @classmethod
    def validate_ticker(cls, v: str) -> str:
        """Validate ticker symbol format"""
        v = v.strip().upper()
        if not v:
            raise ValueError("Ticker symbol cannot be empty")
        if not v.replace(".", "").replace("-", "").isalnum():
            raise ValueError(f"Invalid ticker symbol format: {v}")
        return v

    @model_validator(mode="after")
    def log_configuration_summary(self) -> "StrategySchema":
        """Log a summary of the validated configuration"""
        logger.info(
            f"Strategy validated: {self.strategy.name} on {self.ticker} @ {self.timeframe} | "
            f"SL: {self.risk.stop_loss_pct}% | TP: {self.risk.take_profit_pct}% | "
            f"Max Positions: {self.risk.max_open_positions} (Portfolio: {self.risk.portfolio_max_positions})"
        )
        return self
