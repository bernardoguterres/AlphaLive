"""
C1-style parity test: alphalive/portfolio/target_weights.py vs AlphaLab's
PortfolioConstructor.

AlphaLab is the oracle (see AlphaLab/scripts/generate_portfolio_fixtures.py -
NOT imported here, this repo only reads its exported CSV, exactly like every
other C1 parity fixture). This test independently recomputes the same
scenario via AlphaLive's own, separately-written
alphalive/portfolio/target_weights.py and asserts the two agree - the same
"two independent implementations must match" philosophy as
test_signal_parity.py / test_greenblatt_parity.py, extended to portfolio
construction.

Regenerate the fixture (deterministic, no network calls):
    cd AlphaLab/backend && source venv/bin/activate && cd ..
    python scripts/generate_portfolio_fixtures.py
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from alphalive.portfolio.target_weights import compute_target_positions

PROJECT_ROOT = Path(__file__).parent.parent
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "expected_portfolio_positions.csv"

TOP_N = 4
PORTFOLIO_VALUE = 100_000.0

# Must match AlphaLab/scripts/generate_portfolio_fixtures.py's CANDIDATES
# exactly - this is AlphaLive's independent re-declaration of the same
# scenario inputs, not a shared import.
CANDIDATES = [
    {"ticker": "AAA", "combined_rank": 3},
    {"ticker": "BBB", "combined_rank": 1},
    {"ticker": "CCC", "combined_rank": 6},
    {"ticker": "DDD", "combined_rank": 4},
    {"ticker": "EEE", "combined_rank": 2},
    {"ticker": "FFF", "combined_rank": 5},
]
PRICES = {
    "AAA": 100.0,
    "BBB": 50.0,
    "CCC": 200.0,
    "DDD": 25.0,
    "EEE": 150.0,
    "FFF": 80.0,
}


def _load_expected() -> dict[str, dict]:
    if not FIXTURE_PATH.exists():
        pytest.skip(
            f"Fixture not found at {FIXTURE_PATH} - regenerate with "
            "AlphaLab/scripts/generate_portfolio_fixtures.py"
        )
    expected = {}
    with open(FIXTURE_PATH) as f:
        for row in csv.DictReader(f):
            expected[row["ticker"]] = {
                "rank": int(row["rank"]),
                "target_weight": float(row["target_weight"]),
                "target_shares": int(row["target_shares"]),
                "price": float(row["price"]),
            }
    return expected


class TestPortfolioParity:
    def test_selected_tickers_match(self):
        expected = _load_expected()
        actual = compute_target_positions(CANDIDATES, PRICES, PORTFOLIO_VALUE, TOP_N)
        actual_tickers = {p.ticker for p in actual}

        assert actual_tickers == set(expected.keys())

    def test_target_weights_match(self):
        expected = _load_expected()
        actual = compute_target_positions(CANDIDATES, PRICES, PORTFOLIO_VALUE, TOP_N)

        mismatches = []
        for p in actual:
            exp = expected[p.ticker]
            if abs(p.target_weight - exp["target_weight"]) > 1e-9:
                mismatches.append((p.ticker, p.target_weight, exp["target_weight"]))

        assert mismatches == [], f"target_weight mismatches: {mismatches}"

    def test_target_shares_match(self):
        expected = _load_expected()
        actual = compute_target_positions(CANDIDATES, PRICES, PORTFOLIO_VALUE, TOP_N)

        mismatches = []
        for p in actual:
            exp = expected[p.ticker]
            if p.target_shares != exp["target_shares"]:
                mismatches.append((p.ticker, p.target_shares, exp["target_shares"]))

        assert mismatches == [], f"target_shares mismatches: {mismatches}"

    def test_ranks_match(self):
        expected = _load_expected()
        actual = compute_target_positions(CANDIDATES, PRICES, PORTFOLIO_VALUE, TOP_N)

        for p in actual:
            assert p.rank == expected[p.ticker]["rank"]

    def test_parity_is_not_vacuous(self):
        """Load-bearing guard, per the exact 'zero matched zero vacuously'
        lesson documented in this repo's own CLAUDE.md (the vwap_reversion
        bug where two independent implementations both emitted ~no signals
        and parity passed for the wrong reason). A trivial/broken
        implementation that always returns everything, or nothing, or all
        -equal shares must NOT be able to pass this parity test by accident.
        """
        actual = compute_target_positions(CANDIDATES, PRICES, PORTFOLIO_VALUE, TOP_N)

        # The ranking logic must actually discriminate: exactly TOP_N of 6
        # candidates selected, not all 6 and not 0.
        assert len(actual) == TOP_N
        assert len(actual) < len(CANDIDATES)

        # The two worst-ranked candidates (CCC rank 6, FFF rank 5) must be
        # excluded - proves top_n filtering is load-bearing, not a no-op.
        selected_tickers = {p.ticker for p in actual}
        assert "CCC" not in selected_tickers
        assert "FFF" not in selected_tickers

        # target_shares must differ across tickers (since prices differ),
        # even though target_weight is identical for all - proves the
        # share-sizing math actually uses price, not a constant.
        shares = [p.target_shares for p in actual]
        assert (
            len(set(shares)) > 1
        ), "all target_shares identical - sizing math is not price-aware"
