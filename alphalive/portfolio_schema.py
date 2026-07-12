"""
AlphaLive Portfolio Strategy Schema - Pydantic v2 Models

Additive companion to strategy_schema.py: a multi-ticker "portfolio strategy"
config shape, for strategies that hold N ranked tickers as one basket (e.g.
a cross-sectional Greenblatt top-N) rather than one strategy owning exactly
one ticker (StrategySchema's model).

Deliberately kept as a SEPARATE schema/list rather than folding into
StrategySchema/List[StrategySchema], which is threaded through config.py and
main.py assuming a singular `ticker: str` field everywhere. This is additive:
existing single-ticker strategies and their loading/validation code are
completely untouched. See AlphaLive CLAUDE.md "Portfolio strategies" section
for the wiring status (schema + independent target-weight module built here;
main.py execution-loop wiring is a deliberately separate, deferred step -
see docs/STRATEGY_RESEARCH_PLAN.md M2 scope in the Alpha root docs/).

Schema Version: 1.0 (portfolio)
"""

import logging
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .strategy_schema import Execution, Metadata, Risk, SafetyLimits, Timeframe

logger = logging.getLogger(__name__)


# Strategy names supported for the portfolio (multi-ticker) shape. Kept
# separate from strategy_schema.py's StrategyName so a portfolio config can
# never be mistaken for (or accidentally validated against) the
# single-ticker schema.
PortfolioStrategyName = Literal["greenblatt_portfolio"]

# Only equal-weight is implemented, matching AlphaLab's PortfolioConstructor
# default for this milestone (see alphalive/portfolio/target_weights.py).
Weighting = Literal["equal_weight"]


class PortfolioStrategy(BaseModel):
    """Portfolio strategy configuration (the ranking/rebalance rule)."""

    name: PortfolioStrategyName = Field(..., description="Portfolio strategy name")
    top_n: int = Field(
        ..., description="Number of top-ranked tickers to hold", ge=2, le=50
    )
    rebalance_weeks: int = Field(
        default=52,
        description="Rebalance to target weights every N weeks (52 = annual, matching Greenblatt's own recommended cadence)",
        ge=1,
        le=104,
    )
    weighting: Weighting = Field(
        default="equal_weight", description="Position-sizing scheme"
    )
    parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Strategy-specific parameters (e.g. screener filters)",
    )
    description: Optional[str] = Field(
        default=None, description="Human-readable description"
    )


class PortfolioStrategySchema(BaseModel):
    """
    Complete portfolio (multi-ticker) strategy configuration schema for AlphaLive.

    Parallel to StrategySchema, for strategies that hold N tickers as one
    basket instead of one ticker each. `risk.max_position_size_pct` still
    applies per-ticker as a hard cap (defense in depth) even though
    per-ticker sizing is primarily driven by the computed target weight.
    """

    schema_version: Literal["1.0"] = Field(
        ..., description="Schema version (must be 1.0)"
    )
    strategy: PortfolioStrategy = Field(
        ..., description="Portfolio strategy configuration"
    )
    tickers: List[str] = Field(
        ...,
        description="Universe of candidate tickers the strategy ranks and selects from",
        min_length=2,
    )
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
        if v != "1.0":
            raise ValueError(f"Unsupported schema version: {v}. Expected 1.0")
        return v

    @field_validator("tickers")
    @classmethod
    def validate_tickers(cls, v: List[str]) -> List[str]:
        cleaned = []
        for t in v:
            t = t.strip().upper()
            if not t:
                raise ValueError("Ticker symbol cannot be empty")
            if not t.replace(".", "").replace("-", "").isalnum():
                raise ValueError(f"Invalid ticker symbol format: {t}")
            cleaned.append(t)
        if len(set(cleaned)) != len(cleaned):
            dupes = {t for t in cleaned if cleaned.count(t) > 1}
            raise ValueError(
                f"Duplicate tickers within one portfolio strategy's universe: {dupes}"
            )
        return cleaned

    @model_validator(mode="after")
    def validate_top_n_fits_universe(self) -> "PortfolioStrategySchema":
        if self.strategy.top_n > len(self.tickers):
            raise ValueError(
                f"strategy.top_n ({self.strategy.top_n}) cannot exceed the size of "
                f"tickers ({len(self.tickers)}) - there aren't enough candidates to rank"
            )
        return self

    @model_validator(mode="after")
    def log_configuration_summary(self) -> "PortfolioStrategySchema":
        logger.info(
            f"Portfolio strategy validated: {self.strategy.name} | "
            f"universe={len(self.tickers)} tickers, top_n={self.strategy.top_n}, "
            f"rebalance every {self.strategy.rebalance_weeks}w | "
            f"weighting={self.strategy.weighting}"
        )
        return self


def all_portfolio_tickers(
    portfolio_strategies: List[PortfolioStrategySchema],
) -> Dict[str, str]:
    """Map ticker -> owning portfolio strategy name, for duplicate-ticker
    checks against single-ticker StrategySchema configs (see config.py's
    validate_all). A ticker only ends up "claimed" by a portfolio strategy
    if it's within that strategy's top_n-sized universe consideration set -
    here we conservatively claim the WHOLE `tickers` universe, not just
    whichever names are currently ranked in the top_n, since the ranking
    can change at each rebalance and a currently-unranked ticker could be
    selected next time.
    """
    claimed: Dict[str, str] = {}
    for ps in portfolio_strategies:
        for t in ps.tickers:
            claimed[t] = ps.strategy.name
    return claimed
