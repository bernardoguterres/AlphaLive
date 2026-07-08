"""
Extended AlpacaBroker Method Tests

Covers connect(), account/position fetching, order placement/cancellation,
market hours, and bar fetching - all against a mocked alpaca-py SDK.
Never touches the real Alpaca API.
"""

import pytest
from unittest.mock import Mock, patch
from datetime import datetime

from alphalive.broker.alpaca_broker import AlpacaBroker
from alphalive.broker.base_broker import (
    BrokerError,
    AuthenticationError,
    OrderError,
)
from alpaca.common.exceptions import APIError


def _api_error(message, status_code):
    """Build an alpaca APIError whose .status_code resolves to the given code."""
    http_error = Mock()
    http_error.response = Mock(status_code=status_code)
    return APIError(message, http_error=http_error)


def _mock_account():
    account = Mock()
    account.status = "ACTIVE"
    account.equity = "100000.00"
    account.cash = "50000.00"
    account.buying_power = "200000.00"
    account.portfolio_value = "100000.00"
    account.long_market_value = "50000.00"
    account.short_market_value = "0.00"
    account.daytrade_count = 0
    account.pattern_day_trader = False
    return account


def _connected_broker(monkeypatch=None):
    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    broker.connected = True
    broker.trading_client = Mock()
    broker.data_client = Mock()
    return broker


# ---------------------------------------------------------------------------
# _execute() -- shared error-wrapping helper used by get_position(),
# place_market_order(), place_limit_order(), cancel_order(),
# get_order_status() and close_position(). These tests cover the general
# branches of the helper directly; the per-method tests further down only
# need to prove each method's *unique* behavior (success-path parsing and
# any method-specific 404 handling), since the wrapping logic itself is
# fully exercised here.
# ---------------------------------------------------------------------------


def test_execute_success_returns_result():
    broker = _connected_broker()
    func = Mock(return_value="ok")

    result = broker._execute(func, "arg1", error_context="ctx")

    assert result == "ok"
    func.assert_called_once_with("arg1")


def test_execute_404_with_on_404_returns_callback_result():
    broker = _connected_broker()
    func = Mock(side_effect=_api_error("not found", 404))

    result = broker._execute(func, on_404=lambda: "fallback", error_context="ctx")

    assert result == "fallback"


def test_execute_404_without_on_404_wraps_as_default_error_cls():
    """When no on_404 handler is given, a 404 is treated like any other
    APIError and wrapped in error_cls (default BrokerError)."""
    broker = _connected_broker()
    func = Mock(side_effect=_api_error("not found", 404))

    with pytest.raises(BrokerError, match="ctx"):
        broker._execute(func, error_context="ctx")


def test_execute_non_404_api_error_wraps_as_default_error_cls():
    broker = _connected_broker()
    func = Mock(side_effect=_api_error("bad request", 400))

    with pytest.raises(BrokerError, match="ctx"):
        broker._execute(func, error_context="ctx")


def test_execute_non_404_api_error_wraps_as_custom_error_cls():
    """error_cls is configurable so callers like place_market_order() can
    raise OrderError instead of the default BrokerError."""
    broker = _connected_broker()
    func = Mock(side_effect=_api_error("bad request", 400))

    with pytest.raises(OrderError, match="ctx"):
        broker._execute(func, error_cls=OrderError, error_context="ctx")


def test_execute_unexpected_exception_wraps_as_error_cls():
    broker = _connected_broker()
    func = Mock(side_effect=RuntimeError("boom"))

    with pytest.raises(BrokerError, match="ctx"):
        broker._execute(func, error_context="ctx")


def test_execute_404_with_on_404_does_not_call_error_cls():
    """Sanity check that the on_404 path short-circuits before any
    error_cls wrapping happens (no exception raised at all)."""
    broker = _connected_broker()
    func = Mock(side_effect=_api_error("not found", 404))
    on_404 = Mock(return_value=None)

    result = broker._execute(func, on_404=on_404, error_context="ctx")

    assert result is None
    on_404.assert_called_once()


def test_execute_retryable_error_still_retries_via_retry_with_backoff():
    """_execute delegates retry/backoff to _retry_with_backoff() -- a
    retryable 429 should still be retried MAX_RETRIES times before giving
    up. Note: _retry_with_backoff() raises RateLimitError once retries are
    exhausted, but since RateLimitError is not an APIError, _execute()'s
    generic `except Exception` branch re-wraps it as error_cls (this
    matches the pre-existing behavior of every method that previously
    called _retry_with_backoff() directly inside a try/except Exception
    block, so it is not a behavior change introduced by this helper)."""
    broker = _connected_broker()
    func = Mock(side_effect=_api_error("rate limited", 429))

    with patch("time.sleep"):
        with pytest.raises(BrokerError, match="ctx"):
            broker._execute(func, error_context="ctx")

    assert func.call_count == broker.MAX_RETRIES


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------


@patch("alphalive.broker.alpaca_broker.StockHistoricalDataClient")
@patch("alphalive.broker.alpaca_broker.TradingClient")
def test_connect_success(mock_trading_client_cls, mock_data_client_cls):
    mock_trading_client_cls.return_value.get_account.return_value = _mock_account()

    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    result = broker.connect()

    assert result is True
    assert broker.connected is True
    assert broker.trading_client is not None
    assert broker.data_client is not None


@patch("alphalive.broker.alpaca_broker.StockHistoricalDataClient")
@patch("alphalive.broker.alpaca_broker.TradingClient")
def test_connect_auth_failure(mock_trading_client_cls, mock_data_client_cls):
    mock_trading_client_cls.return_value.get_account.side_effect = _api_error(
        "bad creds", 401
    )

    broker = AlpacaBroker(api_key="bad", secret_key="bad", paper=True)

    with pytest.raises(AuthenticationError):
        broker.connect()
    assert broker.connected is False


@patch("alphalive.broker.alpaca_broker.StockHistoricalDataClient")
@patch("alphalive.broker.alpaca_broker.TradingClient")
def test_connect_api_error_non_auth(mock_trading_client_cls, mock_data_client_cls):
    mock_trading_client_cls.return_value.get_account.side_effect = _api_error(
        "server error", 500
    )

    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)

    with pytest.raises(BrokerError):
        broker.connect()


@patch("alphalive.broker.alpaca_broker.StockHistoricalDataClient")
@patch("alphalive.broker.alpaca_broker.TradingClient")
def test_connect_unexpected_error(mock_trading_client_cls, mock_data_client_cls):
    mock_trading_client_cls.return_value.get_account.side_effect = RuntimeError("boom")

    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)

    with pytest.raises(BrokerError):
        broker.connect()


# ---------------------------------------------------------------------------
# get_account()
# ---------------------------------------------------------------------------


def test_get_account_success():
    broker = _connected_broker()
    broker.trading_client.get_account.return_value = _mock_account()

    account = broker.get_account()

    assert account.equity == 100000.0
    assert account.buying_power == 200000.0
    assert account.account_status == "ACTIVE"


def test_get_account_not_connected():
    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    with pytest.raises(BrokerError, match="Not connected"):
        broker.get_account()


def test_get_account_failure_wraps_error():
    broker = _connected_broker()
    broker.trading_client.get_account.side_effect = RuntimeError("network down")

    with pytest.raises(BrokerError):
        broker.get_account()


# ---------------------------------------------------------------------------
# get_position() / get_all_positions()
# ---------------------------------------------------------------------------


def _mock_alpaca_position(symbol="AAPL", qty="10"):
    pos = Mock()
    pos.symbol = symbol
    pos.qty = qty
    pos.avg_entry_price = "150.00"
    pos.current_price = "155.00"
    pos.unrealized_pl = "50.00"
    pos.unrealized_plpc = "0.0333"
    pos.market_value = "1550.00"
    return pos


def test_get_position_found():
    broker = _connected_broker()
    broker.trading_client.get_open_position.return_value = _mock_alpaca_position()

    position = broker.get_position("AAPL")

    assert position.symbol == "AAPL"
    assert position.qty == 10.0


def test_get_position_not_found_returns_none():
    """Method-specific 404 behavior: get_position() returns None (does not
    raise) when Alpaca reports no open position for the symbol."""
    broker = _connected_broker()
    broker.trading_client.get_open_position.side_effect = _api_error("not found", 404)

    position = broker.get_position("AAPL")

    assert position is None


def test_get_all_positions():
    broker = _connected_broker()
    broker.trading_client.get_all_positions.return_value = [
        _mock_alpaca_position("AAPL"),
        _mock_alpaca_position("MSFT", qty="5"),
    ]

    positions = broker.get_all_positions()

    assert len(positions) == 2
    assert {p.symbol for p in positions} == {"AAPL", "MSFT"}


def test_get_all_positions_error():
    broker = _connected_broker()
    broker.trading_client.get_all_positions.side_effect = RuntimeError("boom")

    with pytest.raises(BrokerError):
        broker.get_all_positions()


# ---------------------------------------------------------------------------
# place_market_order() / place_limit_order()
# ---------------------------------------------------------------------------


def _mock_alpaca_order(order_id="order-1", status="new", order_type="market"):
    from alpaca.trading.enums import OrderSide, OrderType as AlpacaOrderType

    order = Mock()
    order.id = order_id
    order.symbol = "AAPL"
    order.qty = "10"
    order.side = OrderSide.BUY
    order.order_type = (
        AlpacaOrderType.MARKET if order_type == "market" else AlpacaOrderType.LIMIT
    )
    order.limit_price = None
    order.status = Mock(value=status)
    order.filled_qty = "0"
    order.filled_avg_price = None
    order.submitted_at = datetime.now()
    order.filled_at = None
    return order


def test_place_market_order_success():
    broker = _connected_broker()
    broker.trading_client.submit_order.return_value = _mock_alpaca_order()

    order = broker.place_market_order("AAPL", 10, "buy")

    assert order.symbol == "AAPL"
    assert order.id == "order-1"
    broker.trading_client.submit_order.assert_called_once()


def test_place_market_order_invalid_params_raises_before_submit():
    broker = _connected_broker()

    with pytest.raises(ValueError):
        broker.place_market_order("AAPL", -5, "buy")

    broker.trading_client.submit_order.assert_not_called()


def test_place_market_order_api_error_wraps_as_order_error():
    """Confirms place_market_order() wires _execute() with error_cls=OrderError
    (not the default BrokerError) -- the general APIError-> error_cls and
    Exception -> error_cls wrapping mechanics are covered by the _execute()
    tests above."""
    broker = _connected_broker()
    broker.trading_client.submit_order.side_effect = _api_error("rejected", 422)

    with pytest.raises(OrderError):
        broker.place_market_order("AAPL", 10, "buy")


def test_place_limit_order_success():
    broker = _connected_broker()
    broker.trading_client.submit_order.return_value = _mock_alpaca_order(
        order_type="limit"
    )

    order = broker.place_limit_order("AAPL", 10, "sell", 160.0)

    assert order.symbol == "AAPL"
    broker.trading_client.submit_order.assert_called_once()


def test_place_limit_order_invalid_price():
    broker = _connected_broker()

    with pytest.raises(ValueError):
        broker.place_limit_order("AAPL", 10, "buy", -1.0)


def test_place_limit_order_api_error_wraps_as_order_error():
    """Confirms place_limit_order() wires _execute() with error_cls=OrderError."""
    broker = _connected_broker()
    broker.trading_client.submit_order.side_effect = _api_error("rejected", 422)

    with pytest.raises(OrderError):
        broker.place_limit_order("AAPL", 10, "buy", 100.0)


# ---------------------------------------------------------------------------
# cancel_order()
# ---------------------------------------------------------------------------


def test_cancel_order_success():
    broker = _connected_broker()

    result = broker.cancel_order("order-1")

    assert result is True
    broker.trading_client.cancel_order_by_id.assert_called_once_with("order-1")


def test_cancel_order_not_found_returns_false():
    """Method-specific 404 behavior: cancel_order() returns False (does not
    raise) when the order is already filled/canceled/missing. Also verifies
    the _NOT_FOUND sentinel correctly disambiguates this from a legitimate
    successful None result from cancel_order_by_id()."""
    broker = _connected_broker()
    broker.trading_client.cancel_order_by_id.side_effect = _api_error("not found", 404)

    result = broker.cancel_order("order-1")

    assert result is False


def test_cancel_order_api_error_wraps_as_broker_error():
    broker = _connected_broker()
    broker.trading_client.cancel_order_by_id.side_effect = _api_error("server", 500)

    with pytest.raises(BrokerError):
        broker.cancel_order("order-1")


# ---------------------------------------------------------------------------
# get_order_status()
# ---------------------------------------------------------------------------


def test_get_order_status_found():
    broker = _connected_broker()
    broker.trading_client.get_order_by_id.return_value = _mock_alpaca_order(
        status="filled"
    )

    order = broker.get_order_status("order-1")

    assert order.status == "filled"


def test_get_order_status_not_found():
    """Method-specific 404 behavior: get_order_status() returns None (does
    not raise) when the order id is unknown to Alpaca."""
    broker = _connected_broker()
    broker.trading_client.get_order_by_id.side_effect = _api_error("not found", 404)

    order = broker.get_order_status("order-1")

    assert order is None


def test_get_order_status_api_error_wraps_as_broker_error():
    broker = _connected_broker()
    broker.trading_client.get_order_by_id.side_effect = _api_error("server", 500)

    with pytest.raises(BrokerError):
        broker.get_order_status("order-1")


# ---------------------------------------------------------------------------
# close_position()
# ---------------------------------------------------------------------------


def test_close_position_success():
    broker = _connected_broker()
    broker.trading_client.get_open_position.return_value = _mock_alpaca_position()
    broker.trading_client.close_position.return_value = _mock_alpaca_order()

    order = broker.close_position("AAPL")

    assert order.symbol == "AAPL"
    broker.trading_client.close_position.assert_called_once_with("AAPL")


def test_close_position_no_position_raises_value_error():
    """Method-specific 404 behavior: close_position() raises ValueError (not
    None/False like get_position()/cancel_order()) when there is no open
    position for the symbol -- it delegates to get_position() internally and
    translates that method's None result into a ValueError."""
    broker = _connected_broker()
    broker.trading_client.get_open_position.side_effect = _api_error("not found", 404)

    with pytest.raises(ValueError, match="No position found"):
        broker.close_position("AAPL")

    broker.trading_client.close_position.assert_not_called()


def test_close_position_api_error_wraps_as_order_error():
    """Confirms close_position() wires _execute() with error_cls=OrderError
    for the close_position API call itself, and that the outer
    ValueError/OrderError/Exception handling in close_position() re-raises
    that OrderError without double-wrapping it."""
    broker = _connected_broker()
    broker.trading_client.get_open_position.return_value = _mock_alpaca_position()
    broker.trading_client.close_position.side_effect = _api_error("rejected", 422)

    with pytest.raises(OrderError) as exc_info:
        broker.close_position("AAPL")

    # Message should appear exactly once -- proof there's no double-wrapping
    # from both _execute() and the outer except-block wrapping the same error.
    assert str(exc_info.value).count("Failed to close position") == 1


# ---------------------------------------------------------------------------
# is_market_open() / get_market_hours()
# ---------------------------------------------------------------------------


def test_is_market_open_true():
    broker = _connected_broker()
    broker.trading_client.get_clock.return_value = Mock(is_open=True)

    assert broker.is_market_open() is True


def test_is_market_open_false():
    broker = _connected_broker()
    broker.trading_client.get_clock.return_value = Mock(is_open=False)

    assert broker.is_market_open() is False


def test_is_market_open_error():
    broker = _connected_broker()
    broker.trading_client.get_clock.side_effect = RuntimeError("boom")

    with pytest.raises(BrokerError):
        broker.is_market_open()


def test_get_market_hours():
    broker = _connected_broker()
    clock = Mock(
        is_open=True,
        next_open=datetime(2024, 1, 2, 9, 30),
        next_close=datetime(2024, 1, 2, 16, 0),
        timestamp=datetime(2024, 1, 2, 10, 0),
    )
    broker.trading_client.get_clock.return_value = clock

    hours = broker.get_market_hours()

    assert hours["is_open"] is True
    assert hours["next_open"] == clock.next_open


def test_get_market_hours_error():
    broker = _connected_broker()
    broker.trading_client.get_clock.side_effect = RuntimeError("boom")

    with pytest.raises(BrokerError):
        broker.get_market_hours()


# ---------------------------------------------------------------------------
# get_bars()
# ---------------------------------------------------------------------------


def test_get_bars_success():
    broker = _connected_broker()

    bar = Mock(
        timestamp=datetime(2024, 1, 2, 9, 30),
        open=150.0,
        high=151.0,
        low=149.0,
        close=150.5,
        volume=1000,
    )
    broker.data_client.get_stock_bars.return_value = {"AAPL": [bar]}

    result = broker.get_bars("AAPL", "1Day")

    assert len(result) == 1
    assert result[0]["close"] == 150.5
    assert result[0]["volume"] == 1000


def test_get_bars_symbol_missing_returns_empty():
    broker = _connected_broker()
    broker.data_client.get_stock_bars.return_value = {}

    result = broker.get_bars("AAPL", "1Day")

    assert result == []


def test_get_bars_invalid_timeframe():
    broker = _connected_broker()

    with pytest.raises(ValueError, match="Unsupported timeframe"):
        broker.get_bars("AAPL", "3Min")


def test_get_bars_unexpected_error():
    broker = _connected_broker()
    broker.data_client.get_stock_bars.side_effect = RuntimeError("boom")

    with pytest.raises(BrokerError):
        broker.get_bars("AAPL", "1Day")


# ---------------------------------------------------------------------------
# _retry_with_backoff() additional paths
# ---------------------------------------------------------------------------


def test_retry_with_backoff_server_error_exhausts_retries():
    broker = _connected_broker()
    mock_func = Mock(side_effect=_api_error("server error", 500))

    with patch("time.sleep"):
        with pytest.raises(BrokerError):
            broker._retry_with_backoff(mock_func)

    assert mock_func.call_count == broker.MAX_RETRIES


def test_retry_with_backoff_rate_limit_exhausts_retries():
    broker = _connected_broker()
    mock_func = Mock(side_effect=_api_error("rate limited", 429))

    from alphalive.broker.base_broker import RateLimitError

    with patch("time.sleep"):
        with pytest.raises(RateLimitError):
            broker._retry_with_backoff(mock_func)

    assert mock_func.call_count == broker.MAX_RETRIES


def test_retry_with_backoff_other_api_error_no_retry():
    """Non-401/403/429/5xx APIErrors (e.g. 404) are re-raised as-is (not
    wrapped) so callers like get_position()/cancel_order() can branch on
    the original status_code."""
    broker = _connected_broker()
    mock_func = Mock(side_effect=_api_error("bad request", 400))

    with pytest.raises(APIError):
        broker._retry_with_backoff(mock_func)

    assert mock_func.call_count == 1


def test_retry_with_backoff_connection_error_then_success():
    broker = _connected_broker()
    mock_func = Mock(side_effect=[ConnectionError("down"), "ok"])

    with patch("time.sleep"):
        result = broker._retry_with_backoff(mock_func)

    assert result == "ok"
    assert mock_func.call_count == 2


def test_retry_with_backoff_connection_error_exhausts_retries():
    broker = _connected_broker()
    mock_func = Mock(side_effect=ConnectionError("down"))

    with patch("time.sleep"):
        with pytest.raises(BrokerError):
            broker._retry_with_backoff(mock_func)

    assert mock_func.call_count == broker.MAX_RETRIES


def test_retry_with_backoff_unexpected_error_no_retry():
    broker = _connected_broker()
    mock_func = Mock(side_effect=RuntimeError("boom"))

    with pytest.raises(BrokerError):
        broker._retry_with_backoff(mock_func)

    assert mock_func.call_count == 1


# ---------------------------------------------------------------------------
# Additional edge cases for higher-value coverage
# ---------------------------------------------------------------------------


def test_custom_base_url_overrides_default():
    broker = AlpacaBroker(
        api_key="test", secret_key="test", paper=True, base_url="https://custom.example"
    )
    assert broker.base_url == "https://custom.example"


def test_get_historical_bars_multiindex_and_tz_naive():
    """Alpaca sometimes returns a MultiIndex(symbol, timestamp) DataFrame with
    a tz-naive index - both need to be normalized before returning to callers."""
    import pandas as pd

    broker = _connected_broker()

    dates = pd.date_range("2024-01-02", periods=3, freq="D")
    idx = pd.MultiIndex.from_arrays(
        [["AAPL"] * len(dates), dates], names=["symbol", "timestamp"]
    )
    mock_df = pd.DataFrame(
        {
            "Open": [150.0, 151.0, 152.0],
            "High": [151.0, 152.0, 153.0],
            "Low": [149.0, 150.0, 151.0],
            "Close": [150.5, 151.5, 152.5],
            "Volume": [1000, 1100, 1200],
        },
        index=idx,
    )
    mock_bars = Mock()
    mock_bars.df = mock_df
    broker.data_client.get_stock_bars.return_value = mock_bars

    df = broker.get_historical_bars(
        "AAPL", "1Day", datetime(2024, 1, 1), datetime(2024, 1, 5)
    )

    assert "close" in df.columns
    assert df.index.tz is not None
    assert not isinstance(df.index, pd.MultiIndex)


def test_get_historical_bars_unexpected_error_wraps_as_broker_error():
    broker = _connected_broker()
    broker.data_client.get_stock_bars.side_effect = RuntimeError("boom")

    with pytest.raises(BrokerError):
        broker.get_historical_bars(
            "AAPL", "1Day", datetime(2024, 1, 1), datetime(2024, 1, 5)
        )
