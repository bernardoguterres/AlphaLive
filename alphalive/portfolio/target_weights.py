"""Independent (from AlphaLab) top-N equal-weight target-position calculator.

Deliberately does NOT import AlphaLab's PortfolioConstructor, and is not
generated from it - it is a second, independently-written implementation of
the same rank -> top-N -> equal-weight logic, exactly like every other
indicator/strategy in this codebase (see AlphaLive CLAUDE.md "Signal parity
is critical"). tests/test_portfolio_parity.py diffs this module's output
against a fixture AlphaLab exports, so the two implementations are checked
against each other rather than one importing the other.

Scope of this milestone (see docs/STRATEGY_RESEARCH_PLAN.md M2 in the Alpha
root docs/): this module computes target weights/shares for a given
snapshot (ranked candidates + current prices + portfolio value). It does
NOT decide *when* to rebalance, does not place orders, and is not yet wired
into main.py's live execution loop - that wiring is a deliberately separate,
later step given the blast radius of changing the 24/7 trading loop.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TargetPosition:
    ticker: str
    rank: int
    target_weight: float
    target_shares: int
    price: float


def select_top_n(ranked_candidates: list[dict], top_n: int) -> list[dict]:
    """ranked_candidates: list of {"ticker": str, "combined_rank": int},
    ascending combined_rank = better. Returns the top_n by rank."""
    return sorted(ranked_candidates, key=lambda c: c["combined_rank"])[:top_n]


def compute_target_positions(
    ranked_candidates: list[dict],
    prices: dict[str, float],
    portfolio_value: float,
    top_n: int,
) -> list[TargetPosition]:
    """Compute equal-weight target positions for the top_n ranked candidates
    that have a usable (positive) price.

    Mirrors AlphaLab's PortfolioConstructor: select top_n by rank, drop any
    without price data, equal-weight across whatever's left (so weight is
    1/N_available, not always 1/top_n - matching AlphaLab's behavior when a
    candidate has no price data for a given window).
    """
    selected = select_top_n(ranked_candidates, top_n)
    available = [c for c in selected if prices.get(c["ticker"], 0) > 0]
    if not available:
        return []

    weight = 1.0 / len(available)
    positions = []
    for c in available:
        ticker = c["ticker"]
        price = prices[ticker]
        target_dollars = portfolio_value * weight
        shares = int(target_dollars / price) if price > 0 else 0
        positions.append(
            TargetPosition(
                ticker=ticker,
                rank=c["combined_rank"],
                target_weight=weight,
                target_shares=shares,
                price=price,
            )
        )
    return positions
