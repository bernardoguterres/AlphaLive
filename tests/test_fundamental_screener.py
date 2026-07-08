"""
Tests for alphalive.screener.fundamental_screener.FundamentalScreener.

yfinance is mocked throughout - no real network calls. Uses tmp_path for
the output file so nothing leaks outside the pytest sandbox.
"""

import json
from datetime import date
from unittest.mock import Mock, patch

import pytest

from alphalive.screener.fundamental_screener import FundamentalScreener, ScreenerResult


def _mock_yf_info(**overrides):
    base = {
        "regularMarketPrice": 150.0,
        "trailingPE": 20.0,
        "returnOnEquity": 0.25,
        "marketCap": 2_000_000_000_000,
        "debtToEquity": 50.0,  # yfinance percentage -> 0.5 ratio
        "shortName": "Test Corp",
        "sector": "Technology",
    }
    base.update(overrides)
    return base


@pytest.fixture
def screener(tmp_path):
    return FundamentalScreener(
        universe=["AAPL", "MSFT", "GOOG"],
        output_path=str(tmp_path / "screener_output.json"),
        top_n=2,
        min_market_cap_b=1.0,
        max_debt_to_equity=2.0,
        request_delay=0.0,
    )


# ---------------------------------------------------------------------------
# _fetch_one()
# ---------------------------------------------------------------------------


def test_fetch_one_success_normalizes_debt_to_equity(screener):
    with patch("alphalive.screener.fundamental_screener.yf.Ticker") as m_ticker:
        m_ticker.return_value.info = _mock_yf_info(debtToEquity=79.5)
        result = screener._fetch_one("AAPL")

    assert result is not None
    assert result.debt_to_equity == pytest.approx(0.795)
    assert result.earnings_yield == pytest.approx(1.0 / 20.0)


def test_fetch_one_no_market_price_returns_none(screener):
    with patch("alphalive.screener.fundamental_screener.yf.Ticker") as m_ticker:
        m_ticker.return_value.info = {"regularMarketPrice": None}
        assert screener._fetch_one("AAPL") is None


def test_fetch_one_missing_pe_or_roe_returns_none(screener):
    with patch("alphalive.screener.fundamental_screener.yf.Ticker") as m_ticker:
        m_ticker.return_value.info = _mock_yf_info(trailingPE=None)
        assert screener._fetch_one("AAPL") is None

    with patch("alphalive.screener.fundamental_screener.yf.Ticker") as m_ticker:
        m_ticker.return_value.info = _mock_yf_info(returnOnEquity=None)
        assert screener._fetch_one("AAPL") is None


def test_fetch_one_negative_pe_returns_none(screener):
    with patch("alphalive.screener.fundamental_screener.yf.Ticker") as m_ticker:
        m_ticker.return_value.info = _mock_yf_info(trailingPE=-5.0)
        assert screener._fetch_one("AAPL") is None


def test_fetch_one_yfinance_exception_returns_none(screener):
    with patch("alphalive.screener.fundamental_screener.yf.Ticker", side_effect=RuntimeError("network down")):
        assert screener._fetch_one("AAPL") is None


# ---------------------------------------------------------------------------
# _filter() / _rank()
# ---------------------------------------------------------------------------


def _result(ticker, ey_pe=20.0, roe=0.2, mcap_b=5.0, dte=0.5):
    return ScreenerResult(
        ticker=ticker, company_name=ticker, sector="Tech",
        earnings_yield=1.0 / ey_pe, return_on_equity=roe,
        pe_ratio=ey_pe, market_cap_b=mcap_b, debt_to_equity=dte, combined_rank=0,
    )


def test_filter_excludes_small_cap_and_high_leverage(screener):
    results = [
        _result("SMALL", mcap_b=0.5),  # below min_market_cap_b
        _result("LEVERED", dte=3.0),  # above max_debt_to_equity
        _result("GOOD", mcap_b=10.0, dte=1.0),
    ]
    filtered = screener._filter(results)
    assert [r.ticker for r in filtered] == ["GOOD"]


def test_rank_combines_earnings_yield_and_roe_ranks(screener):
    # A: best EY, worst ROE. B: worst EY, best ROE. C: middle on both.
    a = _result("A", ey_pe=5.0, roe=0.05)   # EY rank 1, ROE rank 3 -> 4
    b = _result("B", ey_pe=50.0, roe=0.40)  # EY rank 3, ROE rank 1 -> 4
    c = _result("C", ey_pe=10.0, roe=0.20)  # EY rank 2, ROE rank 2 -> 4

    ranked = screener._rank([a, b, c])

    # All tied at combined_rank 4 -> stable sort preserves input order
    assert [r.combined_rank for r in ranked] == [4, 4, 4]
    assert {r.ticker for r in ranked} == {"A", "B", "C"}


# ---------------------------------------------------------------------------
# run() / run_if_due()
# ---------------------------------------------------------------------------


def test_run_fetches_filters_ranks_and_saves(screener):
    with patch("alphalive.screener.fundamental_screener.yf.Ticker") as m_ticker:
        m_ticker.return_value.info = _mock_yf_info()
        candidates = screener.run()

    assert len(candidates) == 2  # top_n=2, all 3 tickers qualify but capped
    assert screener.output_path.exists()

    saved = json.loads(screener.output_path.read_text())
    assert saved["universe_size"] == 3
    assert len(saved["candidates"]) == 2


def test_run_if_due_skips_when_not_first_of_month(screener):
    with patch("alphalive.screener.fundamental_screener.date") as m_date:
        m_date.today.return_value = date(2024, 1, 15)
        result = screener.run_if_due()

    assert result is None
    assert not screener.output_path.exists()


def test_run_if_due_runs_on_first_of_month(screener):
    with patch("alphalive.screener.fundamental_screener.date") as m_date, \
         patch("alphalive.screener.fundamental_screener.yf.Ticker") as m_ticker:
        m_date.today.return_value = date(2024, 2, 1)
        m_ticker.return_value.info = _mock_yf_info()
        result = screener.run_if_due()

    assert result is not None
    assert len(result) == 2


# ---------------------------------------------------------------------------
# load_candidates()
# ---------------------------------------------------------------------------


def test_load_candidates_no_file_returns_empty(screener):
    assert screener.load_candidates() == []


def test_load_candidates_reads_tickers_from_saved_output(screener):
    with patch("alphalive.screener.fundamental_screener.yf.Ticker") as m_ticker:
        m_ticker.return_value.info = _mock_yf_info()
        screener.run()

    tickers = screener.load_candidates()
    assert len(tickers) == 2
    assert all(isinstance(t, str) for t in tickers)


def test_load_candidates_corrupt_file_returns_empty(screener):
    screener.output_path.write_text("not valid json{{{")
    assert screener.load_candidates() == []
