"""Tests for scripts/monthly_dca.py - the automated monthly SPY purchase."""

from datetime import datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import monthly_dca  # noqa: E402

ET = ZoneInfo("America/New_York")


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)


def _mock_trading(clock_open=True):
    trading = Mock()
    trading.get_clock.return_value = Mock(is_open=clock_open, next_open="tomorrow")
    trading.submit_order.return_value = Mock(id="dca-order-1")
    return trading


def test_client_order_id_is_month_scoped():
    now = datetime(2026, 7, 1, 10, 0, tzinfo=ET)
    assert monthly_dca.build_client_order_id("SPY", now) == "DCA_SPY_2026_07"


def test_buys_when_due(env):
    trading = _mock_trading()
    with patch("alpaca.trading.client.TradingClient", return_value=trading), \
         patch.object(monthly_dca, "datetime") as m_dt:
        m_dt.now.return_value = datetime(2026, 7, 1, 10, 0, tzinfo=ET)
        assert monthly_dca.run() == 0

    request = trading.submit_order.call_args[0][0]
    assert request.symbol == "SPY"
    assert float(request.notional) == 100.0
    assert request.client_order_id == "DCA_SPY_2026_07"


def test_skips_outside_window(env):
    trading = _mock_trading()
    with patch("alpaca.trading.client.TradingClient", return_value=trading), \
         patch.object(monthly_dca, "datetime") as m_dt:
        m_dt.now.return_value = datetime(2026, 7, 15, 10, 0, tzinfo=ET)
        assert monthly_dca.run() == 0

    trading.submit_order.assert_not_called()


def test_force_overrides_window(env):
    trading = _mock_trading()
    with patch("alpaca.trading.client.TradingClient", return_value=trading), \
         patch.object(monthly_dca, "datetime") as m_dt:
        m_dt.now.return_value = datetime(2026, 7, 15, 10, 0, tzinfo=ET)
        assert monthly_dca.run(force=True) == 0

    trading.submit_order.assert_called_once()


def test_dry_run_places_no_order(env):
    trading = _mock_trading()
    with patch("alpaca.trading.client.TradingClient", return_value=trading), \
         patch.object(monthly_dca, "datetime") as m_dt:
        m_dt.now.return_value = datetime(2026, 7, 1, 10, 0, tzinfo=ET)
        assert monthly_dca.run(dry_run=True) == 0

    trading.submit_order.assert_not_called()


def test_market_closed_waits(env):
    trading = _mock_trading(clock_open=False)
    with patch("alpaca.trading.client.TradingClient", return_value=trading), \
         patch.object(monthly_dca, "datetime") as m_dt:
        m_dt.now.return_value = datetime(2026, 8, 1, 10, 0, tzinfo=ET)  # Saturday
        assert monthly_dca.run() == 0

    trading.submit_order.assert_not_called()


def test_duplicate_month_409_is_success(env):
    """Second run in the same month: Alpaca 409s the duplicate
    client_order_id - treated as already-done, exit 0."""
    from alpaca.common.exceptions import APIError

    http_error = Mock()
    http_error.response = Mock(status_code=409)
    trading = _mock_trading()
    trading.submit_order.side_effect = APIError("duplicate client_order_id", http_error=http_error)

    with patch("alpaca.trading.client.TradingClient", return_value=trading), \
         patch.object(monthly_dca, "datetime") as m_dt:
        m_dt.now.return_value = datetime(2026, 7, 2, 10, 0, tzinfo=ET)
        assert monthly_dca.run() == 0


def test_missing_credentials_fails(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    assert monthly_dca.run() == 1
