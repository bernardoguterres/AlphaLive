"""
Test Broker Implementation

Tests for BaseBroker interface and AlpacaBroker implementation.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

from alphalive.broker.base_broker import (
    BaseBroker,
    Position,
    Order,
    Account,
    BrokerError,
    AuthenticationError,
    RateLimitError,
    OrderError
)


def test_position_dataclass():
    """Test Position dataclass."""
    position = Position(
        symbol="AAPL",
        qty=10.0,
        side="long",
        avg_entry_price=150.0,
        current_price=155.0,
        unrealized_pl=50.0,
        unrealized_plpc=3.33,
        market_value=1550.0
    )

    assert position.symbol == "AAPL"
    assert position.qty == 10.0
    assert position.unrealized_pl == 50.0


def test_order_dataclass():
    """Test Order dataclass."""
    order = Order(
        id="order123",
        symbol="AAPL",
        qty=10.0,
        side="buy",
        order_type="market",
        limit_price=None,
        status="filled",
        filled_qty=10.0,
        filled_avg_price=150.0,
        submitted_at=datetime.now(),
        filled_at=datetime.now()
    )

    assert order.id == "order123"
    assert order.symbol == "AAPL"
    assert order.status == "filled"


def test_account_dataclass():
    """Test Account dataclass."""
    account = Account(
        equity=100000.0,
        cash=50000.0,
        buying_power=200000.0,
        portfolio_value=100000.0,
        long_market_value=50000.0,
        short_market_value=0.0,
        daytrade_count=0,
        pattern_day_trader=False,
        account_status="ACTIVE"
    )

    assert account.equity == 100000.0
    assert account.buying_power == 200000.0
    assert account.account_status == "ACTIVE"


def test_broker_exceptions():
    """Test broker exception hierarchy."""
    # Test base exception
    error = BrokerError("Test error")
    assert str(error) == "Test error"
    assert isinstance(error, Exception)

    # Test authentication error
    auth_error = AuthenticationError("Invalid credentials")
    assert isinstance(auth_error, BrokerError)

    # Test rate limit error
    rate_error = RateLimitError("Too many requests")
    assert isinstance(rate_error, BrokerError)

    # Test order error
    order_error = OrderError("Order failed")
    assert isinstance(order_error, BrokerError)


@pytest.mark.skipif(
    True,
    reason="Requires alpaca-py library and API credentials"
)
def test_alpaca_broker_initialization():
    """Test AlpacaBroker initialization."""
    from alphalive.broker.alpaca_broker import AlpacaBroker

    broker = AlpacaBroker(
        api_key="test_key",
        secret_key="test_secret",
        paper=True
    )

    assert broker.api_key == "test_key"
    assert broker.secret_key == "test_secret"
    assert broker.paper is True
    assert broker.base_url == "https://paper-api.alpaca.markets"
    assert broker.connected is False


@pytest.mark.skipif(
    True,
    reason="Requires alpaca-py library and API credentials"
)
def test_alpaca_broker_live_mode():
    """Test AlpacaBroker live mode URL."""
    from alphalive.broker.alpaca_broker import AlpacaBroker

    broker = AlpacaBroker(
        api_key="test_key",
        secret_key="test_secret",
        paper=False
    )

    assert broker.base_url == "https://api.alpaca.markets"


@pytest.mark.skipif(
    True,
    reason="Requires alpaca-py library and API credentials"
)
def test_alpaca_broker_validate_order_params():
    """Test AlpacaBroker order parameter validation."""
    from alphalive.broker.alpaca_broker import AlpacaBroker

    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)

    # Valid params - should not raise
    broker._validate_order_params("AAPL", 10, "buy")

    # Invalid symbol
    with pytest.raises(ValueError, match="Symbol must be a non-empty string"):
        broker._validate_order_params("", 10, "buy")

    # Invalid qty (zero)
    with pytest.raises(ValueError, match="Quantity must be a positive integer"):
        broker._validate_order_params("AAPL", 0, "buy")

    # Invalid qty (negative)
    with pytest.raises(ValueError, match="Quantity must be a positive integer"):
        broker._validate_order_params("AAPL", -10, "buy")

    # Invalid side
    with pytest.raises(ValueError, match="Side must be 'buy' or 'sell'"):
        broker._validate_order_params("AAPL", 10, "invalid")

    # Invalid limit price (zero)
    with pytest.raises(ValueError, match="Limit price must be a positive number"):
        broker._validate_order_params("AAPL", 10, "buy", limit_price=0)

    # Invalid limit price (negative)
    with pytest.raises(ValueError, match="Limit price must be a positive number"):
        broker._validate_order_params("AAPL", 10, "buy", limit_price=-10.0)


@pytest.mark.skipif(
    True,
    reason="Requires alpaca-py library and API credentials"
)
def test_alpaca_broker_ensure_connected():
    """Test AlpacaBroker connection check."""
    from alphalive.broker.alpaca_broker import AlpacaBroker

    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)

    # Should raise when not connected
    with pytest.raises(BrokerError, match="Not connected to Alpaca"):
        broker._ensure_connected()

    # Should not raise when connected
    broker.connected = True
    broker.trading_client = Mock()
    broker._ensure_connected()  # Should not raise


@pytest.mark.skipif(
    True,
    reason="Requires alpaca-py library and API credentials"
)
@patch('alphalive.broker.alpaca_broker.TradingClient')
@patch('alphalive.broker.alpaca_broker.StockHistoricalDataClient')
def test_alpaca_broker_retry_logic(mock_data_client, mock_trading_client):
    """Test AlpacaBroker retry logic with exponential backoff."""
    from alphalive.broker.alpaca_broker import AlpacaBroker
    from alpaca.common.exceptions import APIError

    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)

    # Test successful retry after transient error
    mock_func = Mock(side_effect=[
        APIError("Rate limited", status_code=429),
        APIError("Rate limited", status_code=429),
        "success"
    ])

    with patch('time.sleep'):  # Mock sleep to speed up test
        result = broker._retry_with_backoff(mock_func)
        assert result == "success"
        assert mock_func.call_count == 3

    # Test fatal error (no retry)
    mock_func = Mock(side_effect=APIError("Invalid credentials", status_code=401))

    with pytest.raises(AuthenticationError):
        broker._retry_with_backoff(mock_func)

    # Should only call once (no retry)
    assert mock_func.call_count == 1


@pytest.mark.skipif(
    True,
    reason="Requires alpaca-py library and API credentials"
)
def test_alpaca_broker_convert_position():
    """Test AlpacaBroker position conversion."""
    from alphalive.broker.alpaca_broker import AlpacaBroker

    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)

    # Create mock Alpaca position
    mock_position = Mock()
    mock_position.symbol = "AAPL"
    mock_position.qty = "10"
    mock_position.avg_entry_price = "150.00"
    mock_position.current_price = "155.00"
    mock_position.unrealized_pl = "50.00"
    mock_position.unrealized_plpc = "0.0333"
    mock_position.market_value = "1550.00"

    # Convert
    position = broker._convert_position(mock_position)

    assert position.symbol == "AAPL"
    assert position.qty == 10.0
    assert position.side == "long"
    assert position.avg_entry_price == 150.0
    assert position.unrealized_plpc == 3.33  # Converted to percentage


@pytest.mark.skipif(
    True,
    reason="Requires alpaca-py library and API credentials"
)
def test_alpaca_broker_convert_order():
    """Test AlpacaBroker order conversion."""
    from alphalive.broker.alpaca_broker import AlpacaBroker
    from alpaca.trading.enums import OrderSide, OrderType as AlpacaOrderType

    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)

    # Create mock Alpaca order
    mock_order = Mock()
    mock_order.id = "order123"
    mock_order.symbol = "AAPL"
    mock_order.qty = "10"
    mock_order.side = OrderSide.BUY
    mock_order.order_type = AlpacaOrderType.MARKET
    mock_order.limit_price = None
    mock_order.status = Mock(value="filled")
    mock_order.filled_qty = "10"
    mock_order.filled_avg_price = "150.00"
    mock_order.submitted_at = datetime.now()
    mock_order.filled_at = datetime.now()

    # Convert
    order = broker._convert_order(mock_order)

    assert order.id == "order123"
    assert order.symbol == "AAPL"
    assert order.qty == 10.0
    assert order.side == "buy"
    assert order.order_type == "market"
    assert order.status == "filled"


def test_base_broker_is_abstract():
    """Test that BaseBroker cannot be instantiated directly."""
    with pytest.raises(TypeError, match="Can't instantiate abstract class"):
        BaseBroker()


@pytest.mark.skipif(
    True,
    reason="Requires alpaca-py library and API credentials"
)
@patch('alphalive.broker.alpaca_broker.StockHistoricalDataClient')
def test_get_historical_bars(mock_data_client):
    """Test get_historical_bars method returns proper DataFrame."""
    import pandas as pd
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from alphalive.broker.alpaca_broker import AlpacaBroker

    ET = ZoneInfo("America/New_York")
    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    broker.connected = True
    broker.data_client = mock_data_client.return_value

    # Mock the response
    mock_bars = Mock()
    mock_df = pd.DataFrame({
        'open': [150.0, 151.0, 152.0],
        'high': [151.0, 152.0, 153.0],
        'low': [149.0, 150.0, 151.0],
        'close': [150.5, 151.5, 152.5],
        'volume': [1000000, 1100000, 1200000]
    }, index=pd.DatetimeIndex([
        datetime(2024, 1, 2, 9, 30, tzinfo=ET),
        datetime(2024, 1, 3, 9, 30, tzinfo=ET),
        datetime(2024, 1, 4, 9, 30, tzinfo=ET)
    ]))
    mock_bars.df = mock_df
    broker.data_client.get_stock_bars.return_value = mock_bars

    # Call method
    start = datetime(2024, 1, 1, tzinfo=ET)
    end = datetime(2024, 1, 5, tzinfo=ET)
    df = broker.get_historical_bars("AAPL", "1Day", start, end)

    # Verify result
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 3
    assert 'open' in df.columns
    assert 'close' in df.columns
    assert 'volume' in df.columns
    assert df.index.tz is not None  # Timezone-aware
    assert df['close'].iloc[0] == 150.5


@pytest.mark.skipif(
    True,
    reason="Requires alpaca-py library and API credentials"
)
def test_get_historical_bars_invalid_timeframe():
    """Test get_historical_bars raises error for invalid timeframe."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from alphalive.broker.alpaca_broker import AlpacaBroker

    ET = ZoneInfo("America/New_York")
    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    broker.connected = True
    broker.data_client = Mock()

    start = datetime(2024, 1, 1, tzinfo=ET)
    end = datetime(2024, 1, 5, tzinfo=ET)

    with pytest.raises(ValueError, match="Unsupported timeframe"):
        broker.get_historical_bars("AAPL", "invalid", start, end)


@pytest.mark.skipif(
    True,
    reason="Requires alpaca-py library and API credentials"
)
@patch('alphalive.broker.alpaca_broker.StockHistoricalDataClient')
def test_get_historical_bars_empty_data(mock_data_client):
    """Test get_historical_bars raises error when no data returned."""
    import pandas as pd
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from alphalive.broker.alpaca_broker import AlpacaBroker

    ET = ZoneInfo("America/New_York")
    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    broker.connected = True
    broker.data_client = mock_data_client.return_value

    # Mock empty response
    mock_bars = Mock()
    mock_bars.df = pd.DataFrame()
    broker.data_client.get_stock_bars.return_value = mock_bars

    start = datetime(2024, 1, 1, tzinfo=ET)
    end = datetime(2024, 1, 5, tzinfo=ET)

    with pytest.raises(ValueError, match="No historical data returned"):
        broker.get_historical_bars("AAPL", "1Day", start, end)
