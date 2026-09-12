"""
Market Data Fetcher

Fetches historical and real-time market data from Alpaca using alpaca-py.
Includes caching, data validation, and rate limit handling.
"""

import logging
from datetime import datetime, time as _time, timedelta
from typing import Optional, Callable, Any
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.common.exceptions import APIError as AlpacaAPIError

from alphalive.utils.retry import RetryDecision, RetryOutcome, retry_with_backoff

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Regular US equity market close (ET) - the session-completion boundary
# _resample_to_weekly uses when an explicit as_of falls on the trailing
# week's own Friday (see that method's docstring). Ordinary session close
# only; no early-close-calendar awareness exists in this codebase.
_MARKET_CLOSE_TIME = _time(16, 0)


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
        self.cache = (
            {}
        )  # {ticker: {"bars": df, "timestamp": datetime, "timeframe": str}}
        self.cache_ttl_seconds = 300  # 5 minutes for intraday data
        # Weekly bars change at most once per day; cache for a full trading day
        self._weekly_cache_ttl_seconds = 24 * 3600

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
            if timeframe == "1Week":
                # Fetch daily bars and resample: need lookback_bars weeks of daily data
                # Add 20% buffer for weekends/holidays
                days_back = int(lookback_bars * 7 * 1.2)
            elif timeframe == "1Day":
                days_back = lookback_bars * 2
            elif timeframe == "1Hour":
                days_back = max(lookback_bars // 6 + 5, 30)  # ~6 hours/day, add buffer
            else:  # 15Min
                days_back = max(lookback_bars // 26 + 5, 14)  # ~26 bars/day, add buffer

            start_date = datetime.now(ET) - timedelta(days=days_back)
            end_date = datetime.now(ET)

            # For 1Week: always fetch daily bars from Alpaca, then resample
            fetch_tf = TimeFrame.Day if timeframe == "1Week" else tf

            request = StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=fetch_tf,
                start=start_date,
                end=end_date,
                feed="iex",
            )

            # Fetch with retry logic
            bars = self._fetch_with_retry(lambda: self.client.get_stock_bars(request))

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
                df.index = df.index.tz_localize("UTC").tz_convert(ET)
            elif str(df.index.tz) != "America/New_York":
                df.index = df.index.tz_convert(ET)

            # Resample daily → weekly (week ending Friday) for 1Week strategies.
            # `as_of` makes the wall-clock read explicit here at the call
            # site (not hidden inside the resampler, which touches no
            # clock) - "now" is what makes today's still-forming week
            # incomplete for a live fetch.
            if timeframe == "1Week":
                df = self._resample_to_weekly(df, as_of=datetime.now(ET))

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
                "timeframe": timeframe,
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

    def _get_from_cache(self, ticker: str, timeframe: str) -> Optional[pd.DataFrame]:
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

        ttl = (
            self._weekly_cache_ttl_seconds
            if timeframe == "1Week"
            else self.cache_ttl_seconds
        )
        age_seconds = (datetime.now(ET) - cached["timestamp"]).total_seconds()
        if age_seconds < ttl:
            logger.debug(f"Using cached data for {ticker} (age: {age_seconds:.0f}s)")
            return cached["bars"]
        else:
            logger.debug(f"Cache expired for {ticker} (age: {age_seconds:.0f}s)")
            return None

    def _validate_data_quality(self, df: pd.DataFrame, ticker: str, timeframe: str):
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
            last_bar_time = last_bar_time.tz_localize("US/Eastern")

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
        elif timeframe == "1Week" and age_minutes > 10080:  # More than 7 days old
            raise DataStaleError(
                f"{ticker} weekly data is {age_minutes / 1440:.1f} days old (limit: 7 days). "
                f"Last bar: {last_bar_time.strftime('%Y-%m-%d %H:%M:%S %Z')}."
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
        elif timeframe == "1Week":
            # 1Week fetches daily bars and resamples; this path is not used directly
            return TimeFrame.Day
        elif timeframe == "1Hour":
            return TimeFrame.Hour
        elif timeframe == "15Min":
            return TimeFrame(15, TimeFrameUnit.Minute)
        else:
            raise ValueError(
                f"Unsupported timeframe: {timeframe}. "
                f"Must be one of: 1Day, 1Hour, 15Min, 1Week"
            )

    def _resample_to_weekly(
        self, df: pd.DataFrame, as_of: Optional[pd.Timestamp] = None
    ) -> pd.DataFrame:
        """Resample a daily OHLCV DataFrame to weekly bars (week ending Friday).

        Uses standard OHLCV aggregation:
          open  = first bar of the week
          high  = max of the week
          low   = min of the week
          close = last bar of the week
          volume = sum of the week

        Excludes an incomplete trailing week (weekly scheduling policy):
        pandas' `resample("W-FRI")` labels each bucket by that week's Friday
        even when the underlying daily data only covers Mon-Wed so far -
        that bucket's "close" would be a still-forming price, not the
        week's true close. A weekly strategy must never evaluate against
        that partial bar.

        Completeness reference (2026-09-11 pass 4 - session-completion
        semantics on the Friday boundary itself): this function touches no
        wall clock directly - the caller (get_latest_bars) passes its own
        fetch time as `as_of`. Two independent checks can each mark the
        trailing week incomplete:

          1. Data-driven: the week's Friday is later than the last daily
             bar actually present in `df` (date-only comparison - if the
             data doesn't reach that Friday at all, there's nothing
             session-completion could rescue).
          2. as_of-driven: comparing `as_of` against the week's Friday
             calendar date is not sufficient by itself - a `df` that (per
             upstream API behavior) already contains a same-day, still-
             forming daily bar FOR that Friday would otherwise let a
             midnight-normalized "Friday <= Friday" comparison pass while
             the trading session that day hasn't closed yet. So: if
             `as_of`'s date is strictly before the Friday, incomplete; if
             strictly after, complete (the week has fully elapsed
             regardless of intraday clock time); if `as_of` falls ON the
             Friday itself, complete only once `as_of`'s time-of-day is at
             or after the market-close boundary (_MARKET_CLOSE_TIME) - the
             calendar date arriving is not the same as the session closing.

        The week is dropped if EITHER check calls it incomplete - so this
        never extends past the data's own last bar even if `as_of` is
        later (no future information beyond what's fetched), and never
        extends past `as_of` even if the data (unexpectedly) contains
        later bars (no future information beyond the evaluation time).
        When `as_of` is omitted, only the data-driven check applies (no
        clock to reference - matches the historical/backtest-fixture use
        case, where "session completion" isn't a meaningful concept
        without a wall-clock evaluation time). No early-close-calendar
        awareness exists in this codebase (no market-calendar abstraction
        is available here beyond broker.is_market_open() at the main-loop
        level) - a genuine early close is not specially handled and is a
        documented limitation, not silently guessed at.
        """
        weekly = (
            df.resample("W-FRI")
            .agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                volume=("volume", "sum"),
            )
            .dropna(subset=["close"])
        )

        if len(weekly) > 0 and len(df) > 0:
            last_daily_date = df.index[-1].normalize()
            last_weekly_label = weekly.index[-1].normalize()

            incomplete_per_data = last_weekly_label > last_daily_date

            incomplete_per_as_of = False
            if as_of is not None:
                as_of_ts = pd.Timestamp(as_of)
                if df.index.tz is not None:
                    if as_of_ts.tz is None:
                        as_of_ts = as_of_ts.tz_localize(df.index.tz)
                    else:
                        as_of_ts = as_of_ts.tz_convert(df.index.tz)
                elif as_of_ts.tz is not None:
                    # df is tz-naive but as_of is tz-aware - drop tz so the
                    # two remain comparable (normalize() on a bare
                    # Timestamp vs. a tz-aware one raises otherwise).
                    as_of_ts = as_of_ts.tz_localize(None)

                as_of_date = as_of_ts.normalize()
                if as_of_date < last_weekly_label:
                    incomplete_per_as_of = True
                elif as_of_date == last_weekly_label:
                    # as_of falls on the Friday itself - the calendar date
                    # arriving is not the same as the session closing.
                    incomplete_per_as_of = as_of_ts.time() < _MARKET_CLOSE_TIME
                # else as_of_date > last_weekly_label: the week has fully
                # elapsed regardless of intraday clock time - complete.

            if incomplete_per_data or incomplete_per_as_of:
                logger.debug(
                    f"Dropping incomplete trailing week (label "
                    f"{last_weekly_label.date()}, last daily bar "
                    f"{last_daily_date.date()}, as_of "
                    f"{as_of if as_of is not None else 'n/a'}) - week hasn't "
                    f"closed yet as of the evaluation time."
                )
                weekly = weekly.iloc[:-1]

        logger.debug(f"Resampled {len(df)} daily bars → {len(weekly)} weekly bars")
        return weekly

    @staticmethod
    def _classify_fetch_retry_error(e: Exception) -> RetryOutcome:
        """Classify an exception raised while fetching market data.

        Loop mechanics live in the shared `retry_with_backoff()` helper;
        this only decides retryability and, for 429s, the Retry-After delay.
        """
        if isinstance(e, AlpacaAPIError):
            if e.status_code == 429:
                retry_after = 5  # Default
                if hasattr(e, "response") and e.response is not None:
                    retry_after = int(e.response.headers.get("Retry-After", 5))
                return RetryOutcome(
                    RetryDecision.RETRY,
                    delay_override=retry_after,
                    log_message=f"Alpaca rate limited (429). Retrying after {retry_after}s...",
                )

            if e.status_code >= 500:
                return RetryOutcome(
                    RetryDecision.RETRY,
                    log_message=f"Alpaca server error ({e.status_code}). Retrying...",
                )

            # Other 4xx errors are not retryable
            logger.error(f"Alpaca API error ({e.status_code}): {e}. Not retryable.")
            return RetryOutcome(RetryDecision.FATAL)

        return RetryOutcome(
            RetryDecision.RETRY, log_message=f"Data fetch failed: {e}. Retrying..."
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
        return retry_with_backoff(
            fetch_func,
            classify=self._classify_fetch_retry_error,
            max_retries=max_retries,
            base_delay=2.0,
            multiplier=2.0,
        )

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
