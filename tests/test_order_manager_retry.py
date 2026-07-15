"""
Extended OrderManager tests covering _place_with_retry()'s status-code
routing, the limit-order execution path, slippage/partial-fill alerts, and
the generic order-failure branch. tests/test_order_manager.py already
covers the happy path, duplicate prevention, exits, and dry run - this file
fills in the retry/error branches that were still uncovered.
"""

from unittest.mock import Mock, patch
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from alpaca.common.exceptions import APIError

from alphalive.execution.order_manager import OrderManager
from alphalive.broker.base_broker import Order, OrderError
from alphalive.strategy_schema import StrategySchema

ET = ZoneInfo("America/New_York")


def _api_error(message, status_code):
    http_error = Mock()
    http_error.response = Mock(status_code=status_code)
    return APIError(message, http_error=http_error)


def _order(qty=66.0, filled_qty=66.0, filled_avg_price=150.0):
    return Order(
        id="order_123",
        symbol="AAPL",
        qty=qty,
        side="buy",
        order_type="market",
        limit_price=None,
        status="filled",
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
        submitted_at=datetime.now(ET),
        filled_at=datetime.now(ET),
    )


@pytest.fixture
def sample_config():
    return StrategySchema(
        schema_version="1.0",
        strategy={
            "name": "ma_crossover",
            "parameters": {"fast_period": 10, "slow_period": 20},
        },
        ticker="AAPL",
        timeframe="1Day",
        risk={
            "stop_loss_pct": 2.0,
            "take_profit_pct": 5.0,
            "max_position_size_pct": 10.0,
            "max_daily_loss_pct": 3.0,
            "max_open_positions": 5,
            "portfolio_max_positions": 10,
        },
        execution={"order_type": "market", "limit_offset_pct": 0.1, "cooldown_bars": 1},
        safety_limits={},
        metadata={
            "exported_from": "AlphaLab",
            "exported_at": "2024-01-01T00:00:00Z",
            "alphalab_version": "1.0.0",
            "backtest_id": "test",
            "backtest_period": {"start": "2022-01-01", "end": "2023-12-31"},
            "performance": {
                "sharpe_ratio": 1.5,
                "sortino_ratio": 2.0,
                "total_return_pct": 25.0,
                "max_drawdown_pct": 10.0,
                "win_rate_pct": 55.0,
                "profit_factor": 1.8,
                "total_trades": 100,
                "calmar_ratio": 2.5,
            },
        },
    )


@pytest.fixture
def mock_risk_manager():
    rm = Mock()
    rm.can_trade = Mock(return_value=(True, "OK"))
    rm.calculate_position_size = Mock(return_value=66)
    rm.risk_config = Mock(trailing_stop_enabled=False)
    return rm


@pytest.fixture
def om(sample_config, mock_risk_manager):
    broker = Mock()
    notifier = Mock()
    manager = OrderManager(
        broker=broker,
        risk_manager=mock_risk_manager,
        config=sample_config,
        notifier=notifier,
        dry_run=False,
    )
    return manager


# ---------------------------------------------------------------------------
# _place_with_retry() status-code routing
# ---------------------------------------------------------------------------


def test_place_with_retry_403_raises_value_error_no_retry(om):
    om.broker.place_market_order.side_effect = _api_error("no buying power", 403)

    with pytest.raises(ValueError, match="Insufficient buying power"):
        om._place_with_retry(lambda: om.broker.place_market_order(), ticker="AAPL")

    assert om.broker.place_market_order.call_count == 1
    om.notifier.send_alert.assert_called_once()


def test_place_with_retry_409_recovers_existing_order(om):
    """A 409 means a previous attempt DID place the order - recover it via
    client_order_id and return it as success instead of raising."""
    om.broker.place_market_order.side_effect = _api_error("dup client_order_id", 409)
    existing = _order()
    om.broker.get_order_by_client_id.return_value = existing

    result = om._place_with_retry(
        lambda: om.broker.place_market_order(),
        ticker="AAPL",
        client_order_id="AAPL_buy_20260710_093500",
    )

    assert result is existing
    om.broker.get_order_by_client_id.assert_called_once_with("AAPL_buy_20260710_093500")
    assert om.broker.place_market_order.call_count == 1  # no retry


def test_place_with_retry_409_reraises_when_recovery_fails(om):
    om.broker.place_market_order.side_effect = _api_error("dup client_order_id", 409)
    om.broker.get_order_by_client_id.return_value = None

    with pytest.raises(APIError):
        om._place_with_retry(
            lambda: om.broker.place_market_order(),
            ticker="AAPL",
            client_order_id="AAPL_buy_20260710_093500",
        )

    assert om.broker.place_market_order.call_count == 1


def test_place_with_retry_409_reraises_without_client_order_id(om):
    om.broker.place_market_order.side_effect = _api_error("dup client_order_id", 409)

    with pytest.raises(APIError):
        om._place_with_retry(lambda: om.broker.place_market_order(), ticker="AAPL")

    assert om.broker.place_market_order.call_count == 1


# ---------------------------------------------------------------------------
# Bug 2.3 regression: the 409-recovery path is only reachable in production
# through AlpacaBroker._execute(), which wraps every real APIError into
# OrderError before OrderManager ever sees it. The tests above use a mocked
# broker that raises the raw APIError directly, which never actually
# exercised _place_with_retry()'s `except AlpacaAPIError` clause the way
# the real broker does - OrderError has no status_code at all in the old
# code, so `except AlpacaAPIError` never matched it and this whole 409
# recovery path was dead code (blind retries / duplicate orders instead).
# These tests raise OrderError (with status_code, as AlpacaBroker._execute()
# now sets it) to match what the mocked broker in the tests above does not.
# ---------------------------------------------------------------------------


def test_place_with_retry_recovers_via_wrapped_order_error_on_409(om):
    """Simulates a client-side timeout on an order that Alpaca actually
    filled: the retried placement gets back a 409 (duplicate
    client_order_id) from Alpaca, wrapped into OrderError by the real
    broker - the bot must recover the real fill via get_order_by_client_id,
    not blindly retry or double-submit."""
    om.broker.place_market_order.side_effect = OrderError(
        "Failed to place order: dup client_order_id", status_code=409
    )
    existing = _order()
    om.broker.get_order_by_client_id.return_value = existing

    result = om._place_with_retry(
        lambda: om.broker.place_market_order(),
        ticker="AAPL",
        client_order_id="AAPL_buy_20260714_093500",
    )

    assert result is existing
    om.broker.get_order_by_client_id.assert_called_once_with("AAPL_buy_20260714_093500")
    assert (
        om.broker.place_market_order.call_count == 1
    )  # no blind retry, no duplicate order


def test_place_with_retry_wrapped_order_error_403_no_retry(om):
    om.broker.place_market_order.side_effect = OrderError(
        "Failed to place order: no buying power", status_code=403
    )

    with pytest.raises(ValueError, match="Insufficient buying power"):
        om._place_with_retry(lambda: om.broker.place_market_order(), ticker="AAPL")

    assert om.broker.place_market_order.call_count == 1


def test_place_with_retry_order_error_without_status_code_retries_then_succeeds(om):
    """OrderError wrapping a non-APIError failure (e.g. a network error the
    broker still routes through error_cls) has no status_code - must fall
    back to backoff retry, not crash on a None status comparison."""
    om.broker.place_market_order.side_effect = [
        OrderError("Failed to place order: connection reset"),
        _order(),
    ]

    with patch("time.sleep"):
        result = om._place_with_retry(
            lambda: om.broker.place_market_order(), ticker="AAPL"
        )

    assert result.id == "order_123"
    assert om.broker.place_market_order.call_count == 2


def test_execute_signal_passes_idempotency_key_to_broker(om):
    """The key must actually reach the broker as client_order_id - it was
    previously generated and logged but never sent, so restart/retry
    duplicate protection did not exist."""
    om.broker.place_market_order.side_effect = None
    om.broker.place_market_order.return_value = _order()

    result = om.execute_signal(
        ticker="AAPL",
        signal={"signal": "BUY", "reason": "test"},
        current_price=150.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )

    assert result["status"] == "success"
    _, kwargs = om.broker.place_market_order.call_args
    key = kwargs["client_order_id"]
    assert key is not None and key.startswith("AAPL_buy_")


def test_place_with_retry_422_market_closed_halts_and_raises(om):
    om.broker.place_market_order.side_effect = _api_error("market is closed", 422)

    with pytest.raises(RuntimeError, match="Market closed"):
        om._place_with_retry(lambda: om.broker.place_market_order(), ticker="AAPL")

    om.notifier.send_alert.assert_called_once()


def test_place_with_retry_422_invalid_symbol_pauses_trading(om):
    om.broker.place_market_order.side_effect = _api_error("unknown symbol XYZ", 422)

    with patch.dict("os.environ", {}, clear=False):
        with pytest.raises(ValueError, match="Invalid symbol"):
            om._place_with_retry(lambda: om.broker.place_market_order(), ticker="XYZ")
        import os

        assert os.environ["TRADING_PAUSED"] == "true"


def test_place_with_retry_422_other_reraises_no_retry(om):
    om.broker.place_market_order.side_effect = _api_error("invalid qty", 422)

    with pytest.raises(APIError):
        om._place_with_retry(lambda: om.broker.place_market_order(), ticker="AAPL")

    assert om.broker.place_market_order.call_count == 1


def test_place_with_retry_429_retries_then_succeeds(om):
    om.broker.place_market_order.side_effect = [
        _api_error("rate limited", 429),
        _order(),
    ]

    with patch("time.sleep"):
        result = om._place_with_retry(
            lambda: om.broker.place_market_order(), ticker="AAPL", max_retries=3
        )

    assert result.id == "order_123"
    assert om.broker.place_market_order.call_count == 2


def test_place_with_retry_429_exhausts_retries(om):
    om.broker.place_market_order.side_effect = _api_error("rate limited", 429)

    with patch("time.sleep"):
        with pytest.raises(APIError):
            om._place_with_retry(
                lambda: om.broker.place_market_order(), ticker="AAPL", max_retries=2
            )

    assert om.broker.place_market_order.call_count == 2


def test_place_with_retry_5xx_retries_then_succeeds(om):
    om.broker.place_market_order.side_effect = [
        _api_error("server error", 500),
        _order(),
    ]

    with patch("time.sleep"):
        result = om._place_with_retry(
            lambda: om.broker.place_market_order(), ticker="AAPL", max_retries=3
        )

    assert result.id == "order_123"


def test_place_with_retry_5xx_exhausts_retries(om):
    om.broker.place_market_order.side_effect = _api_error("server error", 500)

    with patch("time.sleep"):
        with pytest.raises(APIError):
            om._place_with_retry(
                lambda: om.broker.place_market_order(), ticker="AAPL", max_retries=2
            )


def test_place_with_retry_non_api_exception_retries_then_succeeds(om):
    om.broker.place_market_order.side_effect = [
        ConnectionError("network blip"),
        _order(),
    ]

    with patch("time.sleep"):
        result = om._place_with_retry(
            lambda: om.broker.place_market_order(), ticker="AAPL", max_retries=3
        )

    assert result.id == "order_123"


def test_place_with_retry_non_api_exception_exhausts_retries(om):
    om.broker.place_market_order.side_effect = ConnectionError("network down")

    with patch("time.sleep"):
        with pytest.raises(ConnectionError):
            om._place_with_retry(
                lambda: om.broker.place_market_order(), ticker="AAPL", max_retries=2
            )


# ---------------------------------------------------------------------------
# execute_signal() - limit orders, slippage/partial-fill alerts, error path
# ---------------------------------------------------------------------------


def test_execute_signal_limit_order_uses_calculated_limit_price(om):
    om.config.execution.order_type = "limit"
    om.broker.place_limit_order.return_value = _order()

    result = om.execute_signal(
        ticker="AAPL",
        signal={"signal": "BUY", "reason": "test"},
        current_price=150.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )

    assert result["status"] == "success"
    om.broker.place_limit_order.assert_called_once()
    _, kwargs = om.broker.place_limit_order.call_args
    assert kwargs["limit_price"] == pytest.approx(150.0 * 1.001)  # BUY offset applied


def test_execute_signal_high_slippage_sends_alert(om):
    # filled at 160 vs expected 150 -> ~6.7% slippage
    om.broker.place_market_order.return_value = _order(filled_avg_price=160.0)

    om.execute_signal(
        ticker="AAPL",
        signal={"signal": "BUY", "reason": "test"},
        current_price=150.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )

    slippage_alerts = [
        c.args[0]
        for c in om.notifier.send_alert.call_args_list
        if "slippage" in c.args[0].lower()
    ]
    assert len(slippage_alerts) == 1


def test_execute_signal_partial_fill_sends_alert(om):
    om.broker.place_market_order.return_value = _order(qty=66.0, filled_qty=30.0)

    result = om.execute_signal(
        ticker="AAPL",
        signal={"signal": "BUY", "reason": "test"},
        current_price=150.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )

    assert result["filled_qty"] == 30.0
    partial_alerts = [
        c.args[0]
        for c in om.notifier.send_alert.call_args_list
        if "partial" in c.args[0].lower()
    ]
    assert len(partial_alerts) == 1


def test_execute_signal_order_error_returns_error_status(om):
    om.broker.place_market_order.side_effect = RuntimeError("broker unreachable")

    result = om.execute_signal(
        ticker="AAPL",
        signal={"signal": "BUY", "reason": "test"},
        current_price=150.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )

    assert result["status"] == "error"
    om.notifier.send_error_alert.assert_called_once()


# ---------------------------------------------------------------------------
# close_position() dry-run and error-notification path
# ---------------------------------------------------------------------------


def test_close_position_dry_run_does_not_call_broker(om):
    om.dry_run = True

    result = om.close_position("AAPL", reason="test exit")

    assert result["status"] == "success"
    om.broker.close_position.assert_not_called()


def test_close_position_error_notifies(om):
    om.broker.close_position.side_effect = RuntimeError("broker down")

    result = om.close_position("AAPL", reason="test exit")

    assert result["status"] == "error"
    om.notifier.send_error_alert.assert_called_once()
