"""
Alpaca Broker Implementation

Implements BaseBroker interface using the Alpaca API (alpaca-py library).
Handles live and paper trading via Alpaca Markets with comprehensive
error handling and retry logic.
"""

import logging
import time
from datetime import datetime
from typing import List, Optional, Dict, Any

import requests
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.common.exceptions import APIError

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

logger = logging.getLogger(__name__)


class AlpacaBroker(BaseBroker):
    """
    Alpaca broker implementation.

    Uses alpaca-py library for trading and market data with comprehensive
    error handling and automatic retry logic for transient failures.
    """

    # Timeframe mapping for data API
    TIMEFRAME_MAP = {
        "1Min": TimeFrame.Minute,
        "5Min": TimeFrame(5, "Min"),
        "15Min": TimeFrame(15, "Min"),
        "1Hour": TimeFrame.Hour,
        "1Day": TimeFrame.Day
    }

    # Retry configuration
    MAX_RETRIES = 3
    INITIAL_RETRY_DELAY = 1.0  # seconds

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        paper: bool = True,
        base_url: Optional[str] = None
    ):
        """
        Initialize Alpaca broker client.

        Args:
            api_key: Alpaca API key
            secret_key: Alpaca secret key
            paper: Use paper trading (default True)
            base_url: Custom base URL (optional, auto-set if None)
        """
        self.api_key = api_key
        self.secret_key = secret_key
        self.paper = paper
        self.connected = False

        # Set base URL
        if base_url:
            self.base_url = base_url
        else:
            self.base_url = (
                "https://paper-api.alpaca.markets" if paper
                else "https://api.alpaca.markets"
            )

        # Initialize clients (will be set in connect())
        self.trading_client: Optional[TradingClient] = None
        self.data_client: Optional[StockHistoricalDataClient] = None

        logger.info(f"Alpaca broker initialized | Mode: {'Paper' if paper else 'Live'} | URL: {self.base_url}")

    def connect(self) -> bool:
        """
        Authenticate with Alpaca and verify credentials.

        Returns:
            True if connected successfully, False otherwise

        Raises:
            AuthenticationError: If credentials are invalid
        """
        try:
            logger.info("Connecting to Alpaca...")

            # Initialize trading client
            self.trading_client = TradingClient(
                api_key=self.api_key,
                secret_key=self.secret_key,
                paper=self.paper
            )

            # Initialize market data client
            self.data_client = StockHistoricalDataClient(
                api_key=self.api_key,
                secret_key=self.secret_key
            )

            # Verify credentials by fetching account
            account = self._retry_with_backoff(self.trading_client.get_account)

            # Print account status
            logger.info("=" * 60)
            logger.info("ALPACA CONNECTION SUCCESSFUL")
            logger.info("=" * 60)
            logger.info(f"Account Status: {account.status}")
            logger.info(f"Equity: ${float(account.equity):,.2f}")
            logger.info(f"Cash: ${float(account.cash):,.2f}")
            logger.info(f"Buying Power: ${float(account.buying_power):,.2f}")
            logger.info(f"Portfolio Value: ${float(account.portfolio_value):,.2f}")
            logger.info(f"Day Trade Count: {account.daytrade_count}")
            logger.info(f"Pattern Day Trader: {account.pattern_day_trader}")
            logger.info("=" * 60)

            self.connected = True
            return True

        except APIError as e:
            if e.status_code in (401, 403):
                logger.error(f"Authentication failed: {e}")
                raise AuthenticationError(f"Invalid Alpaca credentials: {e}")
            else:
                logger.error(f"Alpaca API error during connection: {e}")
                raise BrokerError(f"Failed to connect to Alpaca: {e}")

        except Exception as e:
            logger.error(f"Unexpected error during connection: {e}", exc_info=True)
            raise BrokerError(f"Failed to connect to Alpaca: {e}")

    def get_account(self) -> Account:
        """Get current account information."""
        self._ensure_connected()

        try:
            account = self._retry_with_backoff(self.trading_client.get_account)

            return Account(
                equity=float(account.equity),
                cash=float(account.cash),
                buying_power=float(account.buying_power),
                portfolio_value=float(account.portfolio_value),
                long_market_value=float(account.long_market_value or 0),
                short_market_value=float(account.short_market_value or 0),
                daytrade_count=int(account.daytrade_count),
                pattern_day_trader=account.pattern_day_trader,
                account_status=account.status
            )

        except Exception as e:
            logger.error(f"Failed to get account: {e}", exc_info=True)
            raise BrokerError(f"Failed to get account: {e}")

    def get_position(self, symbol: str) -> Optional[Position]:
        """Get position for a specific symbol."""
        self._ensure_connected()

        try:
            position = self._retry_with_backoff(
                self.trading_client.get_open_position,
                symbol
            )

            return self._convert_position(position)

        except APIError as e:
            if e.status_code == 404:
                # No position found
                logger.debug(f"No position found for {symbol}")
                return None
            else:
                logger.error(f"Failed to get position for {symbol}: {e}")
                raise BrokerError(f"Failed to get position: {e}")

        except Exception as e:
            logger.error(f"Failed to get position for {symbol}: {e}", exc_info=True)
            raise BrokerError(f"Failed to get position: {e}")

    def get_all_positions(self) -> List[Position]:
        """Get all open positions."""
        self._ensure_connected()

        try:
            positions = self._retry_with_backoff(self.trading_client.get_all_positions)

            return [self._convert_position(p) for p in positions]

        except Exception as e:
            logger.error(f"Failed to get all positions: {e}", exc_info=True)
            raise BrokerError(f"Failed to get all positions: {e}")

    def place_market_order(
        self,
        symbol: str,
        qty: int,
        side: str
    ) -> Order:
        """Place a market order."""
        self._ensure_connected()
        self._validate_order_params(symbol, qty, side)

        try:
            # Convert side to Alpaca enum
            order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL

            # Create market order request
            order_request = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce.DAY
            )

            # Submit order
            alpaca_order = self._retry_with_backoff(
                self.trading_client.submit_order,
                order_request
            )

            logger.info(f"MARKET {side.upper()} {qty} {symbol} @ market | Order ID: {alpaca_order.id}")

            return self._convert_order(alpaca_order)

        except APIError as e:
            logger.error(f"Failed to place market order: {e}")
            raise OrderError(f"Market order failed: {e}")

        except Exception as e:
            logger.error(f"Unexpected error placing market order: {e}", exc_info=True)
            raise OrderError(f"Market order failed: {e}")

    def place_limit_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        limit_price: float
    ) -> Order:
        """Place a limit order."""
        self._ensure_connected()
        self._validate_order_params(symbol, qty, side, limit_price)

        try:
            # Convert side to Alpaca enum
            order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL

            # Create limit order request
            order_request = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price
            )

            # Submit order
            alpaca_order = self._retry_with_backoff(
                self.trading_client.submit_order,
                order_request
            )

            logger.info(
                f"LIMIT {side.upper()} {qty} {symbol} @ ${limit_price:.2f} | "
                f"Order ID: {alpaca_order.id}"
            )

            return self._convert_order(alpaca_order)

        except APIError as e:
            logger.error(f"Failed to place limit order: {e}")
            raise OrderError(f"Limit order failed: {e}")

        except Exception as e:
            logger.error(f"Unexpected error placing limit order: {e}", exc_info=True)
            raise OrderError(f"Limit order failed: {e}")

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order."""
        self._ensure_connected()

        try:
            self._retry_with_backoff(self.trading_client.cancel_order_by_id, order_id)
            logger.info(f"Order canceled: {order_id}")
            return True

        except APIError as e:
            if e.status_code == 404:
                logger.warning(f"Order {order_id} not found (may already be filled/canceled)")
                return False
            else:
                logger.error(f"Failed to cancel order {order_id}: {e}")
                raise BrokerError(f"Failed to cancel order: {e}")

        except Exception as e:
            logger.error(f"Unexpected error canceling order {order_id}: {e}", exc_info=True)
            raise BrokerError(f"Failed to cancel order: {e}")

    def get_order_status(self, order_id: str) -> Optional[Order]:
        """Get current status of an order."""
        self._ensure_connected()

        try:
            alpaca_order = self._retry_with_backoff(
                self.trading_client.get_order_by_id,
                order_id
            )

            return self._convert_order(alpaca_order)

        except APIError as e:
            if e.status_code == 404:
                logger.debug(f"Order {order_id} not found")
                return None
            else:
                logger.error(f"Failed to get order status for {order_id}: {e}")
                raise BrokerError(f"Failed to get order status: {e}")

        except Exception as e:
            logger.error(f"Unexpected error getting order status for {order_id}: {e}", exc_info=True)
            raise BrokerError(f"Failed to get order status: {e}")

    def close_position(self, symbol: str) -> Order:
        """Close an entire position using a market order."""
        self._ensure_connected()

        try:
            # Get current position to verify it exists
            position = self.get_position(symbol)

            if position is None:
                raise ValueError(f"No position found for {symbol}")

            # Close position via Alpaca API
            alpaca_order = self._retry_with_backoff(
                self.trading_client.close_position,
                symbol
            )

            logger.info(f"Position closed: {symbol} | Order ID: {alpaca_order.id}")

            return self._convert_order(alpaca_order)

        except ValueError as e:
            raise  # Re-raise ValueError for no position
        except APIError as e:
            logger.error(f"Failed to close position {symbol}: {e}")
            raise OrderError(f"Failed to close position: {e}")
        except Exception as e:
            logger.error(f"Unexpected error closing position {symbol}: {e}", exc_info=True)
            raise OrderError(f"Failed to close position: {e}")

    def is_market_open(self) -> bool:
        """Check if the US stock market is currently open."""
        self._ensure_connected()

        try:
            clock = self._retry_with_backoff(self.trading_client.get_clock)
            return clock.is_open

        except Exception as e:
            logger.error(f"Failed to check market status: {e}", exc_info=True)
            raise BrokerError(f"Failed to check market status: {e}")

    def get_market_hours(self) -> Dict[str, Any]:
        """Get market hours information."""
        self._ensure_connected()

        try:
            clock = self._retry_with_backoff(self.trading_client.get_clock)

            return {
                "is_open": clock.is_open,
                "next_open": clock.next_open,
                "next_close": clock.next_close,
                "timestamp": clock.timestamp
            }

        except Exception as e:
            logger.error(f"Failed to get market hours: {e}", exc_info=True)
            raise BrokerError(f"Failed to get market hours: {e}")

    def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Get historical bars (OHLCV data)."""
        self._ensure_connected()

        try:
            # Map timeframe
            tf = self.TIMEFRAME_MAP.get(timeframe)
            if not tf:
                raise ValueError(f"Unsupported timeframe: {timeframe}")

            # Create request
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=tf,
                start=start,
                end=end,
                limit=limit
            )

            # Fetch bars with retry
            bars = self._retry_with_backoff(self.data_client.get_stock_bars, request)

            # Convert to list of dicts
            result = []
            if symbol in bars:
                for bar in bars[symbol]:
                    result.append({
                        "timestamp": bar.timestamp,
                        "open": float(bar.open),
                        "high": float(bar.high),
                        "low": float(bar.low),
                        "close": float(bar.close),
                        "volume": int(bar.volume)
                    })

            logger.debug(f"Fetched {len(result)} bars for {symbol} @ {timeframe}")
            return result

        except ValueError as e:
            raise  # Re-raise ValueError for invalid timeframe
        except Exception as e:
            logger.error(f"Failed to fetch bars for {symbol}: {e}", exc_info=True)
            raise BrokerError(f"Failed to fetch bars: {e}")

    def get_historical_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime
    ):
        """
        Get historical bars for replay mode (returns pandas DataFrame).

        This method is optimized for fetching large date ranges for backtesting
        and replay simulation. Uses Alpaca's FREE historical data API.

        Args:
            symbol: Stock ticker symbol (e.g., "AAPL")
            timeframe: "1Min", "5Min", "15Min", "1Hour", "1Day"
            start: Start datetime (timezone-aware recommended)
            end: End datetime (timezone-aware recommended)

        Returns:
            pandas DataFrame with columns: open, high, low, close, volume
            Index is timezone-aware datetime (US/Eastern)

        Raises:
            BrokerError: If bars fetch fails
            ValueError: If timeframe is invalid or no data returned

        Example:
            >>> from datetime import datetime
            >>> from zoneinfo import ZoneInfo
            >>> ET = ZoneInfo("America/New_York")
            >>> start = datetime(2024, 1, 1, tzinfo=ET)
            >>> end = datetime(2024, 12, 31, tzinfo=ET)
            >>> df = broker.get_historical_bars("AAPL", "1Day", start, end)
            >>> print(f"Loaded {len(df)} trading days")
        """
        import pandas as pd
        from zoneinfo import ZoneInfo

        self._ensure_connected()
        ET = ZoneInfo("America/New_York")

        try:
            # Map timeframe
            tf = self.TIMEFRAME_MAP.get(timeframe)
            if not tf:
                raise ValueError(f"Unsupported timeframe: {timeframe}")

            # Create request
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=tf,
                start=start,
                end=end
            )

            logger.info(
                f"Fetching historical data for {symbol} @ {timeframe} "
                f"from {start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')}"
            )

            # Fetch bars with retry
            bars = self._retry_with_backoff(self.data_client.get_stock_bars, request)

            # Convert to DataFrame
            df = bars.df

            if df.empty:
                raise ValueError(
                    f"No historical data returned for {symbol} "
                    f"({start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')})"
                )

            # Alpaca returns MultiIndex (symbol, timestamp), flatten it
            if isinstance(df.index, pd.MultiIndex):
                df = df.reset_index(level=0, drop=True)

            # Rename columns to lowercase for consistency
            df = df.rename(columns=str.lower)

            # Ensure timezone-aware index (convert to US/Eastern)
            if df.index.tz is None:
                df.index = df.index.tz_localize('UTC').tz_convert(ET)
            elif str(df.index.tz) != 'America/New_York':
                df.index = df.index.tz_convert(ET)

            logger.info(
                f"✓ Loaded {len(df)} bars for {symbol} "
                f"(first: {df.index[0].strftime('%Y-%m-%d')}, "
                f"last: {df.index[-1].strftime('%Y-%m-%d')})"
            )

            return df

        except ValueError as e:
            raise  # Re-raise ValueError
        except Exception as e:
            logger.error(
                f"Failed to fetch historical bars for {symbol}: {e}",
                exc_info=True
            )
            raise BrokerError(f"Failed to fetch historical bars: {e}")

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _ensure_connected(self):
        """Ensure broker is connected."""
        if not self.connected or self.trading_client is None:
            raise BrokerError("Not connected to Alpaca. Call connect() first.")

    def _validate_order_params(
        self,
        symbol: str,
        qty: int,
        side: str,
        limit_price: Optional[float] = None
    ):
        """Validate order parameters."""
        if not symbol or not isinstance(symbol, str):
            raise ValueError("Symbol must be a non-empty string")

        if not isinstance(qty, int) or qty <= 0:
            raise ValueError("Quantity must be a positive integer")

        if side.lower() not in ("buy", "sell"):
            raise ValueError("Side must be 'buy' or 'sell'")

        if limit_price is not None and (not isinstance(limit_price, (int, float)) or limit_price <= 0):
            raise ValueError("Limit price must be a positive number")

    def _retry_with_backoff(self, func, *args, **kwargs):
        """
        Retry a function with exponential backoff.

        Handles transient errors:
        - 429 (Rate Limit): Retry with backoff
        - 5xx (Server Error): Retry with backoff
        - ConnectionError: Retry with backoff
        - 401/403 (Auth Error): Raise immediately (no retry)

        Args:
            func: Function to call
            *args: Positional arguments to pass to func
            **kwargs: Keyword arguments to pass to func

        Returns:
            Result of func(*args, **kwargs)

        Raises:
            RateLimitError: If rate limited after all retries
            AuthenticationError: If authentication fails (401/403)
            BrokerError: If other error occurs after all retries
        """
        delay = self.INITIAL_RETRY_DELAY

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                return func(*args, **kwargs)

            except APIError as e:
                # Fatal errors - don't retry
                if e.status_code in (401, 403):
                    logger.error(f"Authentication error (fatal): {e}")
                    raise AuthenticationError(f"Authentication failed: {e}")

                # Retryable errors
                if e.status_code == 429:
                    if attempt == self.MAX_RETRIES:
                        logger.error(f"Rate limited after {self.MAX_RETRIES} retries")
                        raise RateLimitError(f"Alpaca rate limit exceeded: {e}")

                    logger.warning(
                        f"Alpaca rate limited (429). Retry {attempt}/{self.MAX_RETRIES} "
                        f"in {delay:.1f}s..."
                    )
                    time.sleep(delay)
                    delay *= 2  # Exponential backoff

                elif e.status_code >= 500:
                    if attempt == self.MAX_RETRIES:
                        logger.error(f"Server error after {self.MAX_RETRIES} retries: {e}")
                        raise BrokerError(f"Alpaca server error: {e}")

                    logger.warning(
                        f"Alpaca server error ({e.status_code}). Retry {attempt}/{self.MAX_RETRIES} "
                        f"in {delay:.1f}s..."
                    )
                    time.sleep(delay)
                    delay *= 2

                else:
                    # Other API errors - don't retry
                    raise BrokerError(f"Alpaca API error: {e}")

            except (ConnectionError, TimeoutError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                if attempt == self.MAX_RETRIES:
                    logger.error(f"Connection error after {self.MAX_RETRIES} retries: {e}")
                    raise BrokerError(f"Connection to Alpaca failed: {e}")

                logger.warning(
                    f"Connection error. Retry {attempt}/{self.MAX_RETRIES} in {delay:.1f}s..."
                )
                time.sleep(delay)
                delay *= 2

            except Exception as e:
                # Unexpected errors - don't retry
                logger.error(f"Unexpected error: {e}", exc_info=True)
                raise BrokerError(f"Unexpected error: {e}")

    def _convert_position(self, alpaca_position) -> Position:
        """Convert Alpaca position to our Position dataclass."""
        return Position(
            symbol=alpaca_position.symbol,
            qty=float(alpaca_position.qty),
            side="long" if float(alpaca_position.qty) > 0 else "short",
            avg_entry_price=float(alpaca_position.avg_entry_price),
            current_price=float(alpaca_position.current_price),
            unrealized_pl=float(alpaca_position.unrealized_pl),
            unrealized_plpc=float(alpaca_position.unrealized_plpc) * 100,  # Convert to percentage
            market_value=float(alpaca_position.market_value)
        )

    def _convert_order(self, alpaca_order) -> Order:
        """Convert Alpaca order to our Order dataclass."""
        return Order(
            id=alpaca_order.id,
            symbol=alpaca_order.symbol,
            qty=float(alpaca_order.qty),
            side=alpaca_order.side.value,
            order_type=alpaca_order.order_type.value,
            limit_price=float(alpaca_order.limit_price) if alpaca_order.limit_price else None,
            status=alpaca_order.status.value,
            filled_qty=float(alpaca_order.filled_qty) if alpaca_order.filled_qty else 0.0,
            filled_avg_price=float(alpaca_order.filled_avg_price) if alpaca_order.filled_avg_price else None,
            submitted_at=alpaca_order.submitted_at,
            filled_at=alpaca_order.filled_at
        )
