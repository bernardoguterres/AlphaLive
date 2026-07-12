"""Tests for alphalive/portfolio/target_weights.py (independent top-N
equal-weight target-position calculator - not shared with AlphaLab's
PortfolioConstructor, parity-tested against it separately in
tests/test_portfolio_parity.py)."""

import pytest

from alphalive.portfolio.target_weights import (
    TargetPosition,
    select_top_n,
    compute_target_positions,
)


def _candidates():
    return [
        {"ticker": "A", "combined_rank": 5},
        {"ticker": "B", "combined_rank": 1},
        {"ticker": "C", "combined_rank": 10},
        {"ticker": "D", "combined_rank": 3},
    ]


class TestSelectTopN:
    def test_selects_lowest_ranks_first(self):
        selected = select_top_n(_candidates(), top_n=2)
        assert [c["ticker"] for c in selected] == ["B", "D"]

    def test_top_n_larger_than_universe_returns_all(self):
        selected = select_top_n(_candidates(), top_n=100)
        assert len(selected) == 4


class TestComputeTargetPositions:
    def test_equal_weight_across_selected(self):
        prices = {"A": 100.0, "B": 50.0, "C": 200.0, "D": 25.0}
        positions = compute_target_positions(
            _candidates(), prices, portfolio_value=100_000, top_n=4
        )
        assert len(positions) == 4
        for p in positions:
            assert p.target_weight == pytest.approx(0.25)

    def test_top_n_restricts_selection(self):
        prices = {"A": 100.0, "B": 50.0, "C": 200.0, "D": 25.0}
        positions = compute_target_positions(
            _candidates(), prices, portfolio_value=100_000, top_n=2
        )
        tickers = {p.ticker for p in positions}
        assert tickers == {"B", "D"}
        assert "C" not in tickers  # worst rank, excluded

    def test_missing_price_ticker_excluded_and_weight_redistributed(self):
        prices = {"B": 50.0, "D": 25.0}  # A has no price data
        positions = compute_target_positions(
            _candidates(), prices, portfolio_value=90_000, top_n=3
        )
        tickers = {p.ticker for p in positions}
        assert tickers == {"B", "D"}
        for p in positions:
            assert p.target_weight == pytest.approx(
                0.5
            )  # redistributed across the 2 available

    def test_target_shares_scale_with_price(self):
        prices = {"A": 100.0, "B": 10.0}
        positions = compute_target_positions(
            [{"ticker": "A", "combined_rank": 1}, {"ticker": "B", "combined_rank": 2}],
            prices,
            portfolio_value=10_000,
            top_n=2,
        )
        by_ticker = {p.ticker: p for p in positions}
        # equal $5000 target each: A -> 50 shares @ $100, B -> 500 shares @ $10
        assert by_ticker["A"].target_shares == 50
        assert by_ticker["B"].target_shares == 500

    def test_no_available_prices_returns_empty(self):
        positions = compute_target_positions(
            _candidates(), {}, portfolio_value=100_000, top_n=4
        )
        assert positions == []

    def test_zero_or_negative_price_excluded(self):
        prices = {"A": 0.0, "B": -5.0, "D": 25.0}
        positions = compute_target_positions(
            _candidates(), prices, portfolio_value=90_000, top_n=4
        )
        tickers = {p.ticker for p in positions}
        assert tickers == {"D"}

    def test_returns_target_position_dataclass_with_rank(self):
        prices = {"B": 50.0}
        positions = compute_target_positions(
            _candidates(), prices, portfolio_value=50_000, top_n=4
        )
        assert isinstance(positions[0], TargetPosition)
        assert positions[0].rank == 1
