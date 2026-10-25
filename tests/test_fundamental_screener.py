"""
Tests for alphalive.screener.fundamental_screener.FundamentalScreener
(Greenblatt Magic Formula: EBIT/EV and EBIT/(NWC + Net PPE)).

yfinance is mocked throughout - no network. Expected values are computed by
hand in each test; one test cross-checks against AlphaLab's independent
implementation when the sibling checkout is available.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from alphalive.screener.fundamental_screener import FundamentalScreener, ScreenerResult


def _info(**overrides):
    base = {
        "regularMarketPrice": 150.0,
        "enterpriseValue": 1_000.0,
        "marketCap": 2_000_000_000_000,
        "debtToEquity": 50.0,  # yfinance percentage -> 0.5 ratio
        "shortName": "Test Corp",
        "sector": "Technology",
    }
    base.update(overrides)
    return base


def _stmt(**rows):
    return pd.DataFrame({"2025": rows}).astype(float)


INCOME = _stmt(EBIT=100.0)
BALANCE = _stmt(**{"Working Capital": 150.0, "Net PPE": 250.0})


@pytest.fixture
def screener(tmp_path):
    return FundamentalScreener(
        universe=["AAPL", "MSFT", "GOOG"],
        output_path=str(tmp_path / "screener_output.json"),
        top_n=2,
        request_delay=0.0,
    )


def _patched(info=None, income=INCOME, balance=BALANCE):
    p = patch("alphalive.screener.fundamental_screener.yf.Ticker")
    m = p.start()
    m.return_value.info = info or _info()
    m.return_value.income_stmt = income
    m.return_value.balance_sheet = balance
    return p


def test_factors_match_hand_computation(screener):
    p = _patched(_info(debtToEquity=79.5))
    try:
        r = screener._fetch_one("AAPL")
    finally:
        p.stop()
    assert r.earnings_yield == pytest.approx(100 / 1_000)  # EBIT / EV
    assert r.return_on_capital == pytest.approx(100 / (150 + 250))  # EBIT / (NWC+PPE)
    assert r.invested_capital == 400.0
    assert r.debt_to_equity == pytest.approx(0.795)


def test_nwc_falls_back_to_current_assets_minus_liabilities():
    bal = _stmt(
        **{"Current Assets": 500.0, "Current Liabilities": 300.0, "Net PPE": 200.0}
    )
    r = FundamentalScreener.evaluate("X", _info(), INCOME, bal)
    assert r.return_on_capital == pytest.approx(100 / (200 + 200))


@pytest.mark.parametrize(
    "info,income,balance",
    [
        (_info(sector="Financial Services"), INCOME, BALANCE),  # excluded sector
        (_info(sector="Utilities"), INCOME, BALANCE),
        (_info(), _stmt(EBIT=-5.0), BALANCE),  # loss-making
        (_info(), INCOME, _stmt(**{"Working Capital": -300.0, "Net PPE": 100.0})),
        (_info(enterpriseValue=None), INCOME, BALANCE),
        (_info(), INCOME, _stmt(**{"Working Capital": 150.0})),  # no PPE
    ],
)
def test_unrankable_companies_are_excluded(info, income, balance):
    assert FundamentalScreener.evaluate("X", info, income, balance) is None


def test_no_market_price_or_exception_returns_none(screener):
    p = _patched({"regularMarketPrice": None})
    try:
        assert screener._fetch_one("AAPL") is None
    finally:
        p.stop()
    with patch(
        "alphalive.screener.fundamental_screener.yf.Ticker",
        side_effect=RuntimeError("network down"),
    ):
        assert screener._fetch_one("AAPL") is None


def _result(ticker, ey, roc, mcap_b=5.0, dte=0.5):
    return ScreenerResult(ticker, ticker, "Tech", ey, roc, 1.0, 1.0, 1.0, mcap_b, dte)


def test_filter_excludes_small_cap_and_high_leverage(screener):
    out = screener._filter(
        [
            _result("SMALL", 0.1, 0.1, mcap_b=0.5),
            _result("LEV", 0.1, 0.1, dte=3.0),
            _result("GOOD", 0.1, 0.1, mcap_b=10.0, dte=1.0),
        ]
    )
    assert [r.ticker for r in out] == ["GOOD"]


def test_rank_sums_factor_ranks_ties_by_universe_order(screener):
    a = _result("A", 0.20, 0.05)  # EY 1, ROC 3 -> 4
    b = _result("B", 0.05, 0.40)  # EY 3, ROC 1 -> 4
    c = _result("C", 0.10, 0.30)  # EY 2, ROC 2 -> 4
    d = _result("D", 0.01, 0.01)  # EY 4, ROC 4 -> 8
    ranked = screener._rank([a, b, c, d])
    assert [
        (r.ticker, r.earnings_yield_rank, r.roc_rank, r.combined_rank) for r in ranked
    ] == [("A", 1, 3, 4), ("B", 3, 1, 4), ("C", 2, 2, 4), ("D", 4, 4, 8)]


def test_run_saves_top_n(screener):
    p = _patched()
    try:
        candidates = screener.run()
    finally:
        p.stop()
    assert len(candidates) == 2
    saved = json.loads(screener.output_path.read_text())
    assert saved["universe_size"] == 3 and len(saved["candidates"]) == 2
    assert "return_on_capital" in saved["candidates"][0]
    assert screener.load_candidates() == [c.ticker for c in candidates]


def test_load_candidates_missing_or_corrupt_file(screener):
    assert screener.load_candidates() == []
    screener.output_path.write_text("not valid json{{{")
    assert screener.load_candidates() == []


def test_matches_alphalab_screener_on_identical_statements():
    """Cross-implementation check: same mocked fundamentals into AlphaLab's
    FundamentalScreener and this one -> same factors and ranking."""
    lab_backend = Path(__file__).resolve().parents[2] / "AlphaLab" / "backend"
    if not lab_backend.exists():
        pytest.skip("AlphaLab sibling checkout not available")
    sys.path.insert(0, str(lab_backend))
    try:
        from alphalab.screener.fundamental_screener import (
            FundamentalScreener as LabScreener,
        )
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"AlphaLab not importable: {e}")

    companies = {
        "AAA": (
            _info(enterpriseValue=2_000.0),
            _stmt(EBIT=300.0),
            _stmt(**{"Working Capital": 400.0, "Net PPE": 600.0}),
        ),
        "BBB": (
            _info(enterpriseValue=900.0),
            _stmt(EBIT=90.0),
            _stmt(**{"Working Capital": 50.0, "Net PPE": 150.0}),
        ),
        "CCC": (
            _info(enterpriseValue=5_000.0),
            _stmt(EBIT=200.0),
            _stmt(
                **{
                    "Current Assets": 800.0,
                    "Current Liabilities": 700.0,
                    "Net PPE": 100.0,
                }
            ),
        ),
        "FIN": (_info(sector="Financial Services"), INCOME, BALANCE),
    }

    def fake_ticker(sym):
        info, inc, bal = companies[sym]
        return type("T", (), {"info": info, "income_stmt": inc, "balance_sheet": bal})()

    with patch(
        "alphalab.screener.fundamental_screener.yf.Ticker", side_effect=fake_ticker
    ), patch("alphalab.screener.fundamental_screener.time.sleep"):
        lab = LabScreener(list(companies), request_delay=0).screen(top_n=10)
    with patch(
        "alphalive.screener.fundamental_screener.yf.Ticker", side_effect=fake_ticker
    ):
        live = FundamentalScreener(list(companies), request_delay=0)
        live_ranked = live._rank(live._filter(live._fetch_all()))

    assert [r.ticker for r in lab] == [r.ticker for r in live_ranked]
    for a, b in zip(lab, live_ranked):
        assert a.earnings_yield == pytest.approx(b.earnings_yield)
        assert a.return_on_capital == pytest.approx(b.return_on_capital)
        assert a.combined_rank == b.combined_rank
