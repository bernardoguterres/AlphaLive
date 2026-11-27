"""Shared daily risk accounting for a confirmed fill."""

from typing import Any


def project_fill_risk(
    counters: dict[str, Any], fill: dict[str, Any], loss_limit: int
) -> dict[str, Any]:
    """Return updated risk totals without mutating state or emitting alerts."""
    losses = counters.get("consecutive_losses", 0)
    breaker = counters.get("circuit_breaker", False)
    if fill.get("round_trip_closed") and fill.get("round_trip_pnl") is not None:
        losses = losses + 1 if float(fill["round_trip_pnl"]) < 0 else 0
        breaker = breaker or losses >= loss_limit
    return {
        "daily_pnl": counters.get("daily_pnl", 0.0)
        + float(fill.get("realized_pnl", 0.0)),
        "consecutive_losses": losses,
        "circuit_breaker": breaker,
    }
