"""
Abstract Broker Interface

Defines the contract for broker implementations.
Allows swapping brokers (Alpaca, Interactive Brokers, etc.) without
changing the rest of the codebase.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Dict, Any


@dataclass
class Position:
    """Represents an open position."""
    symbol: str
    qty: float
    side: str  # "long" or "short"
    avg_entry_price: float
    current_price: float
    unrealized_pl: float
    unrealized_plpc: float  # Percent
    market_value: float


@dataclass
class Order:
    """Represents an order."""
    id: str
    symbol: str
    qty: float
    side: str  # "buy" or "sell"
    order_type: str  # "market" or "limit"
    limit_price: Optional[float]
    status: str  # "new", "filled", "canceled", "rejected", etc.
    filled_qty: float
    filled_avg_price: Optional[float]
    submitted_at: datetime
    filled_at: Optional[datetime]


@dataclass
class Account:
    """Represents account information."""
    equity: float
    cash: float
    buying_power: float
    portfolio_value: float
    long_market_value: float
    short_market_value: float
    daytrade_count: int
    pattern_day_trader: bool
    account_status: str = "ACTIVE"  # "ACTIVE", "ACCOUNT_CLOSED", etc.


class BrokerError(Exception):
    """Base exception for broker errors."""
    pass


class AuthenticationError(BrokerError):
    """Raised when authentication fails."""
    pass


class RateLimitError(BrokerError):
    """Raised when API rate limit is exceeded."""
    pass


class OrderError(BrokerError):
    """Raised when order placement/management fails."""
    pass


class BaseBroker(ABC):
    """
    Abstract base class for broker implementations.

    All broker integrations must implement these methods.
    This interface allows easy swapping between brokers (Alpaca, IBKR, etc.)
    without changing the rest of the codebase.
    """

    @abstractmethod
    def connect(self) -> bool:
        """
        Authenticate with the broker and verify credentials.

        Should:
        - Test API credentials
        - Verify account is active
        - Print account status (equity, buying_power, etc.)
        - Log connection success/failure

        Returns:
            True if connected successfully, False otherwise

        Raises:
            AuthenticationError: If credentials are invalid
            BrokerError: If connection fails for other reasons
        """
        pass

    @abstractmethod
    def get_account(self) -> Account:
        """
        Get current account information.

        Returns:
            Account instance with current account state including:
            - equity: Total account value
            - cash: Available cash
            - buying_power: Available buying power (includes margin)
            - portfolio_value: Current portfolio value
            - long_market_value: Value of long positions
            - short_market_value: Value of short positions
            - daytrade_count: Number of day trades in rolling 5-day period
            - pattern_day_trader: Whether account is flagged as PDT
            - account_status: Account status (ACTIVE, CLOSED, etc.)

        Raises:
            BrokerError: If account fetch fails
        """
        pass

    @abstractmethod
    def get_position(self, symbol: str) -> Optional[Position]:
        """
        Get position for a specific symbol.

        Args:
            symbol: Ticker symbol (e.g., "AAPL")

        Returns:
            Position instance if position exists, None otherwise

        Raises:
            BrokerError: If position fetch fails
        """
        pass

    @abstractmethod
    def get_all_positions(self) -> List[Position]:
        """
        Get all open positions.

        Returns:
            List of Position instances (empty list if no positions)

        Raises:
            BrokerError: If positions fetch fails
        """
        pass

    @abstractmethod
    def place_market_order(
        self,
        symbol: str,
        qty: int,
        side: str
    ) -> Order:
        """
        Place a market order.

        Args:
            symbol: Ticker symbol (e.g., "AAPL")
            qty: Quantity of shares (positive integer)
            side: "buy" or "sell"

        Returns:
            Order instance with order details

        Raises:
            OrderError: If order placement fails
            ValueError: If parameters are invalid

        Example:
            >>> order = broker.place_market_order("AAPL", 10, "buy")
            >>> print(f"Order {order.id} placed: {order.status}")
        """
        pass

    @abstractmethod
    def place_limit_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        limit_price: float
    ) -> Order:
        """
        Place a limit order.

        Args:
            symbol: Ticker symbol (e.g., "AAPL")
            qty: Quantity of shares (positive integer)
            side: "buy" or "sell"
            limit_price: Limit price per share

        Returns:
            Order instance with order details

        Raises:
            OrderError: If order placement fails
            ValueError: If parameters are invalid

        Example:
            >>> order = broker.place_limit_order("AAPL", 10, "buy", 150.50)
            >>> print(f"Limit order {order.id} placed @ ${limit_price}")
        """
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel a pending order.

        Args:
            order_id: Order ID to cancel

        Returns:
            True if canceled successfully, False if order not found or already filled

        Raises:
            BrokerError: If cancellation request fails

        Note:
            Orders that are already filled or canceled cannot be canceled.
        """
        pass

    @abstractmethod
    def get_order_status(self, order_id: str) -> Optional[Order]:
        """
        Get current status of an order.

        Args:
            order_id: Order ID

        Returns:
            Order instance with current status, None if not found

        Raises:
            BrokerError: If order status fetch fails

        Note:
            Order status can be: "new", "filled", "partially_filled",
            "canceled", "rejected", "expired", etc.
        """
        pass

    @abstractmethod
    def close_position(self, symbol: str) -> Order:
        """
        Close an entire position using a market order.

        Args:
            symbol: Ticker symbol

        Returns:
            Order instance for the closing order

        Raises:
            OrderError: If position close fails
            ValueError: If no position exists for symbol

        Note:
            This will place a market order to close the entire position.
            For long positions, sells all shares. For short positions, buys to cover.
        """
        pass

    @abstractmethod
    def is_market_open(self) -> bool:
        """
        Check if the US stock market is currently open.

        Returns:
            True if market is open for trading, False otherwise

        Raises:
            BrokerError: If market status check fails

        Note:
            Regular market hours: 9:30 AM - 4:00 PM ET, Mon-Fri
            (excluding US market holidays)
        """
        pass

    @abstractmethod
    def get_market_hours(self) -> Dict[str, datetime]:
        """
        Get market hours information.

        Returns:
            Dictionary with:
            - is_open: bool - Currently open
            - next_open: datetime - Next market open time
            - next_close: datetime - Next market close time

        Raises:
            BrokerError: If market hours fetch fails
        """
        pass

    @abstractmethod
    def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get historical bars (OHLCV data).

        Args:
            symbol: Ticker symbol
            timeframe: "1Min", "5Min", "15Min", "1Hour", "1Day"
            start: Start datetime (optional)
            end: End datetime (optional)
            limit: Max number of bars (optional)

        Returns:
            List of bar dictionaries with keys:
            - timestamp: datetime
            - open: float
            - high: float
            - low: float
            - close: float
            - volume: int

        Raises:
            BrokerError: If bars fetch fails
            ValueError: If timeframe is invalid

        Note:
            If both limit and start/end are specified, limit takes precedence.
        """
        pass

    @abstractmethod
    def get_historical_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime
    ):
        """
        Get historical bars for replay mode (returns pandas DataFrame).

        Optimized for fetching large date ranges for backtesting and replay
        simulation. Uses broker's free historical data API where available.

        Args:
            symbol: Stock ticker symbol (e.g., "AAPL")
            timeframe: "1Min", "5Min", "15Min", "1Hour", "1Day"
            start: Start datetime (timezone-aware recommended)
            end: End datetime (timezone-aware recommended)

        Returns:
            pandas DataFrame with:
            - Index: timezone-aware datetime
            - Columns: open, high, low, close, volume (lowercase)

        Raises:
            BrokerError: If bars fetch fails
            ValueError: If timeframe is invalid or no data returned

        Note:
            This method is specifically designed for replay mode and returns
            data as a DataFrame (unlike get_bars which returns list of dicts).
            The data is free on most brokers (only real-time data requires subscription).
        """
        pass
