"""
Market Data Fetcher (B8)

Fetches historical and real-time market data from Alpaca using alpaca-py.
Includes caching, data validation, and rate limit handling.
"""

import time
import logging
from datetime import datetime, timedelta
from typing import Optional, Callable, Any
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.common.exceptions import APIError as AlpacaAPIError

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


class DataStaleError(Exception):
    """Raised when data is too old for the configured timeframe."""
    pass


class MarketDataFetcher:
    """
    Fetch historical and real-time market data from Alpaca.

    Features:
    - Caching with 5-minute TTL
    - Data quality validation
    - Staleness detection by timeframe
    - Rate limit handling with retry logic
    """

    def __init__(self, api_key: str, secret_key: str):
        """
        Initialize MarketDataFetcher.

        Args:
            api_key: Alpaca API key
            secret_key: Alpaca secret key
        """
        self.client = StockHistoricalDataClient(api_key, secret_key)
        self.cache = {}  # {ticker: {"bars": df, "timestamp": datetime, "timeframe": str}}
        self.cache_ttl_seconds = 300  # 5 minutes for intraday data

        logger.info("MarketDataFetcher initialized")

    def get_latest_bars(
        self, ticker: str, timeframe: str, lookback_bars: int = 200
    ) -> pd.DataFrame:
        """
        Fetch the most recent N bars for a ticker.

        Args:
            ticker: Stock ticker symbol
            timeframe: "1Day" | "1Hour" | "15Min"
            lookback_bars: Number of bars to fetch (default 200)

        Returns:
            DataFrame with columns: timestamp, open, high, low, close, volume
            Index is timezone-aware datetime (US/Eastern)

        Raises:
            DataStaleError: If data is too old for the timeframe
            ValueError: If data is missing or insufficient
        """
        # Check cache first
        cached = self._get_from_cache(ticker, timeframe)
        if cached is not None:
            return cached

        # Fetch from Alpaca with retry logic
        try:
            logger.info(f"Fetching {lookback_bars} bars of {ticker} @ {timeframe}")

            # Map timeframe to Alpaca format
            tf = self._map_timeframe(timeframe)

            # Calculate start date (fetch extra to ensure we have enough)
            # For daily: lookback_bars * 2 days (accounts for weekends/holidays)
            # For intraday: more conservative multiplier
            if timeframe == "1Day":
                days_back = lookback_bars * 2
            elif timeframe == "1Hour":
                days_back = max(lookback_bars // 6 + 5, 30)  # ~6 hours/day, add buffer
            else:  # 15Min
                days_back = max(lookback_bars // 26 + 5, 14)  # ~26 bars/day, add buffer

            start_date = datetime.now(ET) - timedelta(days=days_back)
            end_date = datetime.now(ET)

            request = StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=tf,
                start=start_date,
                end=end_date
            )

            # Fetch with retry logic
            bars = self._fetch_with_retry(
                lambda: self.client.get_stock_bars(request)
            )

            # Convert to DataFrame
            df = bars.df
            if df.empty:
                raise ValueError(f"No data returned for {ticker}")

            # Alpaca returns MultiIndex (symbol, timestamp), flatten it
            if isinstance(df.index, pd.MultiIndex):
                df = df.reset_index(level=0, drop=True)

            # Rename columns to lowercase
            df = df.rename(columns=str.lower)

            # Ensure timezone-aware index
            if df.index.tz is None:
                df.index = df.index.tz_localize('UTC').tz_convert(ET)
            elif str(df.index.tz) != 'America/New_York':
                df.index = df.index.tz_convert(ET)

            # Keep only the last N bars
            df = df.tail(lookback_bars)

            logger.info(
                f"Fetched {len(df)} bars for {ticker} "
                f"(latest: {df.index[-1].strftime('%Y-%m-%d %H:%M:%S %Z')})"
            )

            # Validate data quality
            self._validate_data_quality(df, ticker, timeframe)

            # Cache it
            self.cache[ticker] = {
                "bars": df,
                "timestamp": datetime.now(ET),
                "timeframe": timeframe
            }

            return df

        except DataStaleError:
            # Re-raise staleness errors
            raise
        except Exception as e:
            logger.error(f"Failed to fetch data for {ticker}: {e}", exc_info=True)
            raise

    def get_current_price(self, ticker: str) -> float:
        """
        Get the most recent price for a ticker.

        Args:
            ticker: Stock ticker symbol

        Returns:
            Current price (from latest trade)

        Raises:
            Exception: If unable to fetch price from API or cache
        """
        try:
            request = StockLatestTradeRequest(symbol_or_symbols=ticker)
            latest_trade = self._fetch_with_retry(
                lambda: self.client.get_stock_latest_trade(request)
            )
            price = latest_trade[ticker].price
            logger.debug(f"Current price for {ticker}: ${price:.2f}")
            return price

        except Exception as e:
            logger.warning(f"Failed to get current price for {ticker}: {e}")

            # Fallback: use last close from cached bars
            cached = self.cache.get(ticker)
            if cached:
                fallback_price = float(cached["bars"]["close"].iloc[-1])
                logger.info(
                    f"Using cached close price for {ticker}: ${fallback_price:.2f}"
                )
                return fallback_price

            # No fallback available
            raise Exception(
                f"Unable to get current price for {ticker}: API failed and no cached data"
            )

    def _get_from_cache(
        self, ticker: str, timeframe: str
    ) -> Optional[pd.DataFrame]:
        """
        Return cached data if still fresh.

        Args:
            ticker: Stock ticker
            timeframe: Timeframe string

        Returns:
            Cached DataFrame if valid, None otherwise
        """
        cached = self.cache.get(ticker)
        if cached is None:
            return None

        # Check if timeframe matches
        if cached.get("timeframe") != timeframe:
            logger.debug(
                f"Cache miss for {ticker}: timeframe mismatch "
                f"({cached.get('timeframe')} != {timeframe})"
            )
            return None

        age_seconds = (datetime.now(ET) - cached["timestamp"]).total_seconds()
        if age_seconds < self.cache_ttl_seconds:
            logger.debug(f"Using cached data for {ticker} (age: {age_seconds:.0f}s)")
            return cached["bars"]
        else:
            logger.debug(f"Cache expired for {ticker} (age: {age_seconds:.0f}s)")
            return None

    def _validate_data_quality(
        self, df: pd.DataFrame, ticker: str, timeframe: str
    ):
        """
        Check data quality and raise errors/warnings as needed.

        DATA FRESHNESS THRESHOLDS BY TIMEFRAME:
        - 15Min strategies: data older than 5 minutes = STALE
        - 1Hour strategies: data older than 15 minutes = STALE
        - 1Day strategies: data older than 1440 minutes (1 day) = STALE

        MINIMUM BARS:
        - At least 20 bars required (indicator warmup)
        - Warn if fewer than 200 bars (recommended)

        Args:
            df: DataFrame to validate
            ticker: Stock ticker
            timeframe: Timeframe string

        Raises:
            DataStaleError: If data is too old for the timeframe
            ValueError: If data is empty or insufficient bars
        """
        if df.empty:
            raise ValueError(f"Empty DataFrame for {ticker}")

        # Check data freshness
        last_bar_time = df.index[-1]
        if not isinstance(last_bar_time, pd.Timestamp):
            last_bar_time = pd.Timestamp(last_bar_time)

        # Make timezone-aware if needed
        if last_bar_time.tz is None:
            last_bar_time = last_bar_time.tz_localize('US/Eastern')

        now = datetime.now(ET)
        age_minutes = (now - last_bar_time).total_seconds() / 60

        # Check staleness thresholds
        if timeframe == "15Min" and age_minutes > 5:
            raise DataStaleError(
                f"{ticker} data is {age_minutes:.1f} minutes old (limit: 5 min for 15Min timeframe). "
                f"Last bar: {last_bar_time.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
                f"Market may be closed or data feed delayed."
            )
        elif timeframe == "1Hour" and age_minutes > 15:
            raise DataStaleError(
                f"{ticker} data is {age_minutes:.1f} minutes old (limit: 15 min for 1Hour timeframe). "
                f"Last bar: {last_bar_time.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
                f"Market may be closed or data feed delayed."
            )
        elif timeframe == "1Day" and age_minutes > 1440:  # More than 1 day old
            # For daily data, check if we're in market hours and the data is from yesterday
            # Market hours: 9:30 AM - 4:00 PM ET
            if 9 <= now.hour < 16 and now.weekday() < 5:  # Weekday during market hours
                raise DataStaleError(
                    f"{ticker} daily data is from {last_bar_time.date()}, but market is open today. "
                    f"Expected today's data. Last bar: {last_bar_time.strftime('%Y-%m-%d %H:%M:%S %Z')}"
                )

        # Check minimum bars
        if len(df) < 20:
            raise ValueError(
                f"{ticker} has only {len(df)} bars (minimum 20 required for indicator warmup)"
            )

        if len(df) < 200:
            logger.warning(
                f"{ticker} has only {len(df)} bars (recommended: 200+). "
                f"Some indicators may not be fully warmed up."
            )

        # Check for NaN in price columns
        critical_cols = ["open", "high", "low", "close", "volume"]
        for col in critical_cols:
            if col not in df.columns:
                raise ValueError(f"{ticker} missing required column: {col}")

            nan_count = df[col].isna().sum()
            if nan_count > 0:
                logger.warning(
                    f"{ticker} has {nan_count} NaN values in '{col}' column. "
                    f"This may affect indicator calculations."
                )

        # Check for zero volume bars (suspicious)
        zero_volume_bars = (df["volume"] == 0).sum()
        if zero_volume_bars > 0:
            logger.warning(
                f"{ticker} has {zero_volume_bars} bars with zero volume. "
                f"This may indicate data quality issues or a thinly traded stock."
            )

    def _map_timeframe(self, timeframe: str) -> TimeFrame:
        """
        Map strategy timeframe string to Alpaca TimeFrame.

        Args:
            timeframe: "1Day" | "1Hour" | "15Min"

        Returns:
            Alpaca TimeFrame object

        Raises:
            ValueError: If timeframe is not supported
        """
        if timeframe == "1Day":
            return TimeFrame.Day
        elif timeframe == "1Hour":
            return TimeFrame.Hour
        elif timeframe == "15Min":
            return TimeFrame(15, TimeFrameUnit.Minute)
        else:
            raise ValueError(
                f"Unsupported timeframe: {timeframe}. "
                f"Must be one of: 1Day, 1Hour, 15Min"
            )

    def _fetch_with_retry(
        self, fetch_func: Callable[[], Any], max_retries: int = 3
    ) -> Any:
        """
        Execute a fetch function with retry logic for rate limits and server errors.

        Alpaca rate limits: 200 req/min for data endpoints.

        Args:
            fetch_func: Function to call (should return data)
            max_retries: Maximum number of retry attempts

        Returns:
            Result from fetch_func

        Raises:
            AlpacaAPIError: If non-retryable error (4xx other than 429)
            Exception: If all retries exhausted
        """
        for attempt in range(1, max_retries + 1):
            try:
                return fetch_func()

            except AlpacaAPIError as e:
                if e.status_code == 429:
                    # Rate limited
                    # Try to get Retry-After header from response
                    retry_after = 5  # Default
                    if hasattr(e, 'response') and e.response is not None:
                        retry_after = int(e.response.headers.get("Retry-After", 5))
                    logger.warning(
                        f"Alpaca rate limited (429). Retry {attempt}/{max_retries} "
                        f"after {retry_after}s..."
                    )
                    if attempt < max_retries:
                        time.sleep(retry_after)
                    else:
                        raise

                elif e.status_code >= 500:
                    # Server error - exponential backoff
                    wait_time = 2 ** attempt
                    logger.warning(
                        f"Alpaca server error ({e.status_code}). Retry {attempt}/{max_retries} "
                        f"after {wait_time}s..."
                    )
                    if attempt < max_retries:
                        time.sleep(wait_time)
                    else:
                        raise

                else:
                    # Other 4xx errors are not retryable
                    logger.error(
                        f"Alpaca API error ({e.status_code}): {e}. Not retryable."
                    )
                    raise

            except Exception as e:
                if attempt < max_retries:
                    wait_time = 2 ** attempt
                    logger.warning(
                        f"Data fetch failed (attempt {attempt}/{max_retries}): {e}. "
                        f"Retrying in {wait_time}s..."
                    )
                    time.sleep(wait_time)
                else:
                    logger.error(f"Data fetch failed after {max_retries} attempts: {e}")
                    raise

    def clear_cache(self, ticker: Optional[str] = None):
        """
        Clear cache for a specific ticker or all tickers.

        Args:
            ticker: Ticker to clear (None = clear all)
        """
        if ticker:
            if ticker in self.cache:
                del self.cache[ticker]
                logger.debug(f"Cleared cache for {ticker}")
        else:
            self.cache.clear()
            logger.debug("Cleared all cache")
