"""
Test Market Data Fetcher (B8)

Tests for MarketDataFetcher with caching, validation, and rate limiting.
"""

import time
import pytest
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import Mock, patch, MagicMock

from alphalive.data.market_data import MarketDataFetcher, DataStaleError
from alpaca.common.exceptions import APIError as AlpacaAPIError

ET = ZoneInfo("America/New_York")


@pytest.fixture
def mock_alpaca_client():
    """Create a mock Alpaca client."""
    with patch('alphalive.data.market_data.StockHistoricalDataClient') as mock_client_class:
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        yield mock_client


@pytest.fixture
def sample_bars_data():
    """Generate sample bar data for testing."""
    dates = pd.date_range(
        start=datetime.now(ET) - timedelta(days=100),
        end=datetime.now(ET),
        freq='D'
    )

    df = pd.DataFrame({
        'open': [150.0 + i * 0.5 for i in range(len(dates))],
        'high': [151.0 + i * 0.5 for i in range(len(dates))],
        'low': [149.0 + i * 0.5 for i in range(len(dates))],
        'close': [150.5 + i * 0.5 for i in range(len(dates))],
        'volume': [1000000 + i * 1000 for i in range(len(dates))],
    }, index=dates)

    # Create MultiIndex (symbol, timestamp) as Alpaca returns
    df.index = pd.MultiIndex.from_product([['AAPL'], df.index], names=['symbol', 'timestamp'])

    return df


@pytest.fixture
def market_data_fetcher(mock_alpaca_client):
    """Create MarketDataFetcher instance with mocked client."""
    fetcher = MarketDataFetcher(api_key="test_key", secret_key="test_secret")
    fetcher.client = mock_alpaca_client
    return fetcher


def test_market_data_fetcher_initialization():
    """Test MarketDataFetcher initialization."""
    with patch('alphalive.data.market_data.StockHistoricalDataClient'):
        fetcher = MarketDataFetcher(api_key="test_key", secret_key="test_secret")

        assert fetcher.cache == {}
        assert fetcher.cache_ttl_seconds == 300


def test_get_latest_bars_success(market_data_fetcher, mock_alpaca_client, sample_bars_data):
    """Test successful bar fetching."""
    # Mock the bars response
    mock_bars = Mock()
    mock_bars.df = sample_bars_data
    mock_alpaca_client.get_stock_bars.return_value = mock_bars

    result = market_data_fetcher.get_latest_bars("AAPL", "1Day", lookback_bars=200)

    assert isinstance(result, pd.DataFrame)
    assert len(result) <= 200  # Should be truncated to lookback_bars
    assert list(result.columns) == ['open', 'high', 'low', 'close', 'volume']
    assert result.index.tz is not None  # Timezone-aware

    # Verify API was called
    mock_alpaca_client.get_stock_bars.assert_called_once()

    # Verify caching
    assert "AAPL" in market_data_fetcher.cache
    assert market_data_fetcher.cache["AAPL"]["timeframe"] == "1Day"


def test_get_latest_bars_from_cache(market_data_fetcher, sample_bars_data):
    """Test cache hit on second request."""
    # Manually populate cache
    df = sample_bars_data.copy()
    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index(level=0, drop=True)
    df = df.rename(columns=str.lower)

    market_data_fetcher.cache["AAPL"] = {
        "bars": df.tail(200),
        "timestamp": datetime.now(ET),
        "timeframe": "1Day"
    }

    # Request should hit cache
    result = market_data_fetcher.get_latest_bars("AAPL", "1Day", lookback_bars=200)

    assert isinstance(result, pd.DataFrame)
    # Client should not be called (cache hit)
    market_data_fetcher.client.get_stock_bars.assert_not_called()


def test_get_latest_bars_cache_miss_timeframe_mismatch(market_data_fetcher, mock_alpaca_client, sample_bars_data):
    """Test cache miss when timeframe doesn't match."""
    # Cache daily data
    df = sample_bars_data.copy()
    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index(level=0, drop=True)
    df = df.rename(columns=str.lower)

    market_data_fetcher.cache["AAPL"] = {
        "bars": df.tail(200),
        "timestamp": datetime.now(ET),
        "timeframe": "1Day"
    }

    # Mock the bars response
    mock_bars = Mock()
    mock_bars.df = sample_bars_data
    mock_alpaca_client.get_stock_bars.return_value = mock_bars

    # Request hourly data - should miss cache
    result = market_data_fetcher.get_latest_bars("AAPL", "1Hour", lookback_bars=200)

    # Client should be called (cache miss)
    mock_alpaca_client.get_stock_bars.assert_called_once()


def test_get_latest_bars_cache_expired(market_data_fetcher, mock_alpaca_client, sample_bars_data):
    """Test cache expiration after TTL."""
    # Cache data with old timestamp
    df = sample_bars_data.copy()
    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index(level=0, drop=True)
    df = df.rename(columns=str.lower)

    market_data_fetcher.cache["AAPL"] = {
        "bars": df.tail(200),
        "timestamp": datetime.now(ET) - timedelta(seconds=400),  # Older than TTL (300s)
        "timeframe": "1Day"
    }

    # Mock the bars response
    mock_bars = Mock()
    mock_bars.df = sample_bars_data
    mock_alpaca_client.get_stock_bars.return_value = mock_bars

    # Request should miss cache (expired)
    result = market_data_fetcher.get_latest_bars("AAPL", "1Day", lookback_bars=200)

    # Client should be called
    mock_alpaca_client.get_stock_bars.assert_called_once()


def test_get_latest_bars_empty_data(market_data_fetcher, mock_alpaca_client):
    """Test error handling for empty data."""
    # Mock empty response
    mock_bars = Mock()
    mock_bars.df = pd.DataFrame()
    mock_alpaca_client.get_stock_bars.return_value = mock_bars

    with pytest.raises(ValueError, match="No data returned"):
        market_data_fetcher.get_latest_bars("AAPL", "1Day")


def test_get_latest_bars_insufficient_bars(market_data_fetcher, mock_alpaca_client):
    """Test error when insufficient bars returned."""
    # Mock response with only 10 bars (less than minimum 20)
    dates = pd.date_range(
        start=datetime.now(ET) - timedelta(days=10),
        end=datetime.now(ET),
        freq='D'
    )

    df = pd.DataFrame({
        'open': [150.0] * len(dates),
        'high': [151.0] * len(dates),
        'low': [149.0] * len(dates),
        'close': [150.5] * len(dates),
        'volume': [1000000] * len(dates),
    }, index=dates)

    df.index = pd.MultiIndex.from_product([['AAPL'], df.index], names=['symbol', 'timestamp'])

    mock_bars = Mock()
    mock_bars.df = df
    mock_alpaca_client.get_stock_bars.return_value = mock_bars

    with pytest.raises(ValueError, match="only 11 bars"):  # 11 days in range
        market_data_fetcher.get_latest_bars("AAPL", "1Day", lookback_bars=200)


def test_data_stale_error_15min(market_data_fetcher, mock_alpaca_client):
    """Test DataStaleError for 15Min timeframe."""
    # Create stale data (10 minutes old)
    stale_time = datetime.now(ET) - timedelta(minutes=10)
    dates = pd.date_range(
        start=stale_time - timedelta(hours=10),
        end=stale_time,
        freq='15min'
    )

    df = pd.DataFrame({
        'open': [150.0] * len(dates),
        'high': [151.0] * len(dates),
        'low': [149.0] * len(dates),
        'close': [150.5] * len(dates),
        'volume': [1000000] * len(dates),
    }, index=dates)

    df.index = pd.MultiIndex.from_product([['AAPL'], df.index], names=['symbol', 'timestamp'])

    mock_bars = Mock()
    mock_bars.df = df
    mock_alpaca_client.get_stock_bars.return_value = mock_bars

    with pytest.raises(DataStaleError, match="10.*minutes old.*limit: 5 min"):
        market_data_fetcher.get_latest_bars("AAPL", "15Min", lookback_bars=30)


def test_data_stale_error_1hour(market_data_fetcher, mock_alpaca_client):
    """Test DataStaleError for 1Hour timeframe."""
    # Create stale data (20 minutes old)
    stale_time = datetime.now(ET) - timedelta(minutes=20)
    dates = pd.date_range(
        start=stale_time - timedelta(hours=50),
        end=stale_time,
        freq='1h'
    )

    df = pd.DataFrame({
        'open': [150.0] * len(dates),
        'high': [151.0] * len(dates),
        'low': [149.0] * len(dates),
        'close': [150.5] * len(dates),
        'volume': [1000000] * len(dates),
    }, index=dates)

    df.index = pd.MultiIndex.from_product([['AAPL'], df.index], names=['symbol', 'timestamp'])

    mock_bars = Mock()
    mock_bars.df = df
    mock_alpaca_client.get_stock_bars.return_value = mock_bars

    with pytest.raises(DataStaleError, match="20.*minutes old.*limit: 15 min"):
        market_data_fetcher.get_latest_bars("AAPL", "1Hour", lookback_bars=30)


def test_get_current_price_success(market_data_fetcher, mock_alpaca_client):
    """Test successful current price fetching."""
    mock_trade = Mock()
    mock_trade.price = 150.75
    mock_alpaca_client.get_stock_latest_trade.return_value = {"AAPL": mock_trade}

    price = market_data_fetcher.get_current_price("AAPL")

    assert price == 150.75
    mock_alpaca_client.get_stock_latest_trade.assert_called_once()


def test_get_current_price_fallback_to_cache(market_data_fetcher, mock_alpaca_client, sample_bars_data):
    """Test fallback to cached close price when API fails."""
    # Populate cache
    df = sample_bars_data.copy()
    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index(level=0, drop=True)
    df = df.rename(columns=str.lower)

    market_data_fetcher.cache["AAPL"] = {
        "bars": df.tail(200),
        "timestamp": datetime.now(ET),
        "timeframe": "1Day"
    }

    # Mock API failure
    mock_alpaca_client.get_stock_latest_trade.side_effect = Exception("API timeout")

    price = market_data_fetcher.get_current_price("AAPL")

    # Should use cached close price
    assert isinstance(price, float)
    assert price > 0


def test_get_current_price_no_fallback(market_data_fetcher, mock_alpaca_client):
    """Test exception when API fails and no cache."""
    mock_alpaca_client.get_stock_latest_trade.side_effect = Exception("API timeout")

    with pytest.raises(Exception, match="Unable to get current price"):
        market_data_fetcher.get_current_price("AAPL")


def test_fetch_with_retry_rate_limit(market_data_fetcher):
    """Test retry logic for 429 rate limit."""
    # Create a custom exception class that behaves like AlpacaAPIError
    class MockAlpacaError(Exception):
        def __init__(self, status_code, headers=None):
            super().__init__("Rate limited")
            self.status_code = status_code
            self.response = Mock()
            self.response.headers = headers or {}

    mock_func = Mock()

    # First two calls fail with 429, third succeeds
    error1 = MockAlpacaError(429, {"Retry-After": "1"})
    error2 = MockAlpacaError(429, {"Retry-After": "1"})

    mock_func.side_effect = [error1, error2, "success"]

    with patch('time.sleep') as mock_sleep:
        # Patch AlpacaAPIError to catch our mock exception
        with patch('alphalive.data.market_data.AlpacaAPIError', MockAlpacaError):
            result = market_data_fetcher._fetch_with_retry(mock_func, max_retries=3)

            assert result == "success"
            assert mock_func.call_count == 3
            assert mock_sleep.call_count == 2  # Slept twice before third attempt
            # Should have slept for 1 second each time (from Retry-After header)
            assert mock_sleep.call_args_list[0][0][0] == 1
            assert mock_sleep.call_args_list[1][0][0] == 1


def test_fetch_with_retry_server_error(market_data_fetcher):
    """Test retry logic for 5xx server errors."""
    # Create a custom exception class that behaves like AlpacaAPIError
    class MockAlpacaError(Exception):
        def __init__(self, status_code):
            super().__init__("Server error")
            self.status_code = status_code

    mock_func = Mock()

    # First two calls fail with 500, third succeeds
    error1 = MockAlpacaError(500)
    error2 = MockAlpacaError(500)

    mock_func.side_effect = [error1, error2, "success"]

    with patch('time.sleep') as mock_sleep:
        # Patch AlpacaAPIError to catch our mock exception
        with patch('alphalive.data.market_data.AlpacaAPIError', MockAlpacaError):
            result = market_data_fetcher._fetch_with_retry(mock_func, max_retries=3)

            assert result == "success"
            assert mock_func.call_count == 3
            # Exponential backoff: 2s, 4s
            assert mock_sleep.call_count == 2
            assert mock_sleep.call_args_list[0][0][0] == 2
            assert mock_sleep.call_args_list[1][0][0] == 4


def test_fetch_with_retry_non_retryable_error(market_data_fetcher):
    """Test that 4xx errors (except 429) are not retried."""
    # Create a custom exception class that behaves like AlpacaAPIError
    class MockAlpacaError(Exception):
        def __init__(self, status_code):
            super().__init__("Bad request")
            self.status_code = status_code

    mock_func = Mock()

    error = MockAlpacaError(400)
    mock_func.side_effect = error

    with patch('alphalive.data.market_data.AlpacaAPIError', MockAlpacaError):
        # Should raise the error immediately
        with pytest.raises(MockAlpacaError):
            market_data_fetcher._fetch_with_retry(mock_func, max_retries=3)

        # Should fail immediately, no retries
        assert mock_func.call_count == 1


def test_fetch_with_retry_max_retries_exhausted(market_data_fetcher):
    """Test that retries are exhausted after max_retries."""
    # Create a custom exception class that behaves like AlpacaAPIError
    class MockAlpacaError(Exception):
        def __init__(self, status_code, headers=None):
            super().__init__("Rate limited")
            self.status_code = status_code
            self.response = Mock()
            self.response.headers = headers or {}

    mock_func = Mock()

    # Make it fail every time
    error1 = MockAlpacaError(429, {"Retry-After": "1"})
    error2 = MockAlpacaError(429, {"Retry-After": "1"})
    error3 = MockAlpacaError(429, {"Retry-After": "1"})

    mock_func.side_effect = [error1, error2, error3]

    with patch('time.sleep'):
        with patch('alphalive.data.market_data.AlpacaAPIError', MockAlpacaError):
            # Should raise after exhausting retries
            with pytest.raises(MockAlpacaError):
                market_data_fetcher._fetch_with_retry(mock_func, max_retries=3)

            # Should try 3 times then give up
            assert mock_func.call_count == 3


def test_clear_cache_specific_ticker(market_data_fetcher):
    """Test clearing cache for specific ticker."""
    market_data_fetcher.cache = {
        "AAPL": {"bars": pd.DataFrame(), "timestamp": datetime.now(ET)},
        "MSFT": {"bars": pd.DataFrame(), "timestamp": datetime.now(ET)}
    }

    market_data_fetcher.clear_cache("AAPL")

    assert "AAPL" not in market_data_fetcher.cache
    assert "MSFT" in market_data_fetcher.cache


def test_clear_cache_all(market_data_fetcher):
    """Test clearing all cache."""
    market_data_fetcher.cache = {
        "AAPL": {"bars": pd.DataFrame(), "timestamp": datetime.now(ET)},
        "MSFT": {"bars": pd.DataFrame(), "timestamp": datetime.now(ET)}
    }

    market_data_fetcher.clear_cache()

    assert market_data_fetcher.cache == {}


def test_map_timeframe(market_data_fetcher):
    """Test timeframe mapping."""
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    # Test 1Day
    tf = market_data_fetcher._map_timeframe("1Day")
    assert tf.amount == 1
    assert tf.unit == TimeFrameUnit.Day

    # Test 1Hour
    tf = market_data_fetcher._map_timeframe("1Hour")
    assert tf.amount == 1
    assert tf.unit == TimeFrameUnit.Hour

    # Test 15Min
    tf = market_data_fetcher._map_timeframe("15Min")
    assert tf.amount == 15
    assert tf.unit == TimeFrameUnit.Minute


def test_map_timeframe_invalid(market_data_fetcher):
    """Test invalid timeframe raises error."""
    with pytest.raises(ValueError, match="Unsupported timeframe"):
        market_data_fetcher._map_timeframe("5Min")
