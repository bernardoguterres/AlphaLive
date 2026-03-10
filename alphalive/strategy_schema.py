"""
AlphaLive Strategy Schema - Pydantic v2 Models

This module defines the canonical schema for strategy configurations in AlphaLive.
Strategies exported from AlphaLab are validated against this schema before execution.

Schema Version: 1.0
"""

import logging
from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


# Strategy names supported by AlphaLive
StrategyName = Literal[
    "ma_crossover",
    "rsi_mean_reversion",
    "momentum_breakout",
    "bollinger_breakout",
    "vwap_reversion"
]

# Timeframes supported by AlphaLive
Timeframe = Literal["1Day", "1Hour", "15Min"]

# Order types supported
OrderType = Literal["market", "limit"]


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
    stop_loss_pct: float = Field(..., description="Stop loss percentage", ge=0.1, le=50.0)
    take_profit_pct: float = Field(..., description="Take profit percentage", ge=0.5, le=100.0)
    max_position_size_pct: float = Field(..., description="Max position size as % of portfolio", ge=1.0, le=100.0)
    max_daily_loss_pct: float = Field(..., description="Max daily loss as % of portfolio", ge=0.5, le=20.0)
    max_open_positions: int = Field(..., description="Max open positions per strategy", ge=1, le=50)
    portfolio_max_positions: int = Field(..., description="Max positions across all strategies", ge=1, le=100)
    trailing_stop_enabled: bool = Field(default=False, description="Enable trailing stop")
    trailing_stop_pct: Optional[float] = Field(default=None, description="Trailing stop percentage", ge=0.5, le=20.0)
    commission_per_trade: float = Field(default=0.0, description="Commission per trade (USD)", ge=0.0, le=50.0)

    @field_validator('stop_loss_pct')
    @classmethod
    def validate_stop_loss(cls, v: float) -> float:
        """Validate stop loss and warn if unusually wide"""
        if v > 15.0:
            logger.warning(f"Very wide stop loss ({v}%) — verify this is intentional")
        return v

    @field_validator('take_profit_pct')
    @classmethod
    def validate_take_profit(cls, v: float) -> float:
        """Validate take profit and warn if aggressive"""
        if v > 50.0:
            logger.warning(f"Aggressive take profit target ({v}%) — may be difficult to achieve")
        return v

    @field_validator('trailing_stop_pct')
    @classmethod
    def validate_trailing_stop(cls, v: Optional[float]) -> Optional[float]:
        """Validate trailing stop and warn if wide"""
        if v is not None and v > 10.0:
            logger.warning(f"Wide trailing stop ({v}%) — may give back significant gains")
        return v

    @model_validator(mode='after')
    def validate_portfolio_positions(self) -> 'Risk':
        """Ensure portfolio_max_positions >= max_open_positions"""
        if self.portfolio_max_positions < self.max_open_positions:
            raise ValueError(
                f"portfolio_max_positions ({self.portfolio_max_positions}) must be >= "
                f"max_open_positions ({self.max_open_positions})"
            )
        return self

    @model_validator(mode='after')
    def validate_trailing_stop_config(self) -> 'Risk':
        """Ensure trailing_stop_pct is set if trailing_stop_enabled"""
        if self.trailing_stop_enabled and self.trailing_stop_pct is None:
            raise ValueError("trailing_stop_pct must be set when trailing_stop_enabled is True")
        return self


class Execution(BaseModel):
    """Order execution parameters"""
    order_type: OrderType = Field(..., description="Order type (market or limit)")
    limit_offset_pct: float = Field(default=0.1, description="Limit order offset percentage")
    cooldown_bars: int = Field(default=1, description="Cooldown period in bars between trades", ge=0)


class SafetyLimits(BaseModel):
    """Safety limits to prevent runaway behavior"""
    max_trades_per_day: int = Field(default=20, description="Max trades per day", ge=1, le=200)
    max_api_calls_per_hour: int = Field(default=500, description="Max API calls per hour", ge=100, le=2000)
    signal_generation_timeout_seconds: float = Field(
        default=5.0,
        description="Timeout for signal generation in seconds",
        ge=1.0,
        le=30.0
    )
    broker_degraded_mode_threshold_failures: int = Field(
        default=3,
        description="Number of broker failures before entering degraded mode",
        ge=1,
        le=10
    )

    @field_validator('max_trades_per_day')
    @classmethod
    def validate_max_trades(cls, v: int) -> int:
        """Validate max trades per day and warn if high"""
        if v > 50:
            logger.warning(f"High trade frequency ({v} trades/day) — verify strategy logic")
        return v


class Strategy(BaseModel):
    """Strategy configuration"""
    name: StrategyName = Field(..., description="Strategy name")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="Strategy-specific parameters")
    description: Optional[str] = Field(default=None, description="Human-readable strategy description")


class StrategySchema(BaseModel):
    """
    Complete strategy configuration schema for AlphaLive.

    This is the canonical representation of a strategy exported from AlphaLab
    and imported into AlphaLive for live trading.
    """
    schema_version: Literal["1.0"] = Field(..., description="Schema version (must be 1.0)")
    strategy: Strategy = Field(..., description="Strategy configuration")
    ticker: str = Field(..., description="Ticker symbol (e.g., AAPL)")
    timeframe: Timeframe = Field(..., description="Trading timeframe")
    risk: Risk = Field(..., description="Risk management parameters")
    execution: Execution = Field(..., description="Execution parameters")
    safety_limits: SafetyLimits = Field(
        default_factory=SafetyLimits,
        description="Safety limits (defaults applied if missing)"
    )
    metadata: Metadata = Field(..., description="Export metadata from AlphaLab")

    @field_validator('schema_version')
    @classmethod
    def validate_schema_version(cls, v: str) -> str:
        """Ensure schema version is exactly 1.0"""
        if v != "1.0":
            raise ValueError(f"Unsupported schema version: {v}. Expected 1.0")
        return v

    @field_validator('ticker')
    @classmethod
    def validate_ticker(cls, v: str) -> str:
        """Validate ticker symbol format"""
        v = v.strip().upper()
        if not v:
            raise ValueError("Ticker symbol cannot be empty")
        if not v.replace(".", "").replace("-", "").isalnum():
            raise ValueError(f"Invalid ticker symbol format: {v}")
        return v

    @model_validator(mode='after')
    def log_configuration_summary(self) -> 'StrategySchema':
        """Log a summary of the validated configuration"""
        logger.info(
            f"Strategy validated: {self.strategy.name} on {self.ticker} @ {self.timeframe} | "
            f"SL: {self.risk.stop_loss_pct}% | TP: {self.risk.take_profit_pct}% | "
            f"Max Positions: {self.risk.max_open_positions} (Portfolio: {self.risk.portfolio_max_positions})"
        )
        return self


# Backward compatibility helper
def load_strategy_with_defaults(data: Dict[str, Any]) -> StrategySchema:
    """
    Load a strategy configuration with backward compatibility.

    If safety_limits is missing from the input data, default values are applied:
    - max_trades_per_day: 20
    - max_api_calls_per_hour: 500
    - signal_generation_timeout_seconds: 5.0
    - broker_degraded_mode_threshold_failures: 3

    Args:
        data: Raw strategy configuration dictionary

    Returns:
        Validated StrategySchema instance
    """
    if "safety_limits" not in data:
        logger.info("safety_limits block missing — applying defaults for backward compatibility")
        data["safety_limits"] = {
            "max_trades_per_day": 20,
            "max_api_calls_per_hour": 500,
            "signal_generation_timeout_seconds": 5.0,
            "broker_degraded_mode_threshold_failures": 3
        }

    return StrategySchema(**data)
