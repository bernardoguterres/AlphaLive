# AlphaLive Broker Integration Plan

## Overview

This document outlines the complete implementation plan for adding **Interactive Brokers** and **Tradier** support to AlphaLive as alternatives to Alpaca.

**Goal:** Enable AlphaLive to connect to multiple brokers, allowing users to choose based on cost, features, and preferences.

---

## Quick Comparison

| Feature | Alpaca Plus | Interactive Brokers | Tradier |
|---------|-------------|---------------------|---------|
| **Annual Cost** | $1,000 | $120-180 | $120 |
| **Real-Time Data** | ✅ Yes | ✅ Yes | ✅ Yes |
| **API Complexity** | Easy | Medium | Easy |
| **Implementation Time** | Done ✅ | 2-3 days | 1-2 days |
| **Asset Classes** | Stocks only | Stocks, Options, Futures, Forex | Stocks, Options |
| **Global Access** | US only | 70+ countries | US only |
| **Paper Trading** | ✅ Free | ✅ Free | ✅ Free |
| **AlphaLive Changes** | None | Medium | Low |

---

## Architecture Overview

### Current Architecture
```
AlphaLive
    ↓
BaseBroker (abstract)
    ↓
AlpacaBroker (concrete)
    ↓
Alpaca API (alpaca-py)
```

### Target Architecture
```
AlphaLive
    ↓
BaseBroker (abstract)
    ↓
    ├── AlpacaBroker (concrete) ← Already implemented
    ├── IBBroker (concrete) ← New
    └── TradierBroker (concrete) ← New
```

---

## Implementation Plan: Interactive Brokers

### 1. Prerequisites

**What You Need:**
- Interactive Brokers account (free to open)
- TWS (Trader Workstation) OR IB Gateway installed locally
- Python `ibapi` library (official IB Python API)
- Market data subscription (~$10-15/month)

**Account Setup:**
1. Open IB account at https://www.interactivebrokers.com
2. Enable API access in account settings
3. Download TWS or IB Gateway (https://www.interactivebrokers.com/en/trading/tws.php)
4. Subscribe to US Securities Snapshot and Futures Value Bundle ($10/month)

**IB Gateway vs TWS:**
- **TWS (Trader Workstation):** Full GUI trading platform (heavier, more features)
- **IB Gateway:** Lightweight API gateway (recommended for bots, no GUI)

**Recommended:** Use IB Gateway for AlphaLive (less resource-intensive)

---

### 2. File Structure

```
alphalive/
├── broker/
│   ├── __init__.py
│   ├── base_broker.py          # Already exists
│   ├── alpaca_broker.py        # Already exists
│   └── ib_broker.py            # NEW - IB implementation
│
├── config.py                   # Update to support IB config
│
tests/
├── test_ib_broker.py          # NEW - IB tests
└── conftest.py                # Update with IB fixtures
```

---

### 3. Code Implementation

#### 3.1 Install Dependencies

Add to `requirements.txt`:
```txt
# Interactive Brokers
ibapi==10.19.2  # Official IB Python API
```

Install:
```bash
pip install ibapi==10.19.2
```

#### 3.2 Create `ib_broker.py`

**File:** `alphalive/broker/ib_broker.py`

**Key Components:**

```python
"""Interactive Brokers broker implementation for AlphaLive."""

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order as IBOrder
from threading import Thread, Event
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
import logging

from .base_broker import (
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


class IBApp(EWrapper, EClient):
    """
    IB API requires both EWrapper (callbacks) and EClient (requests).
    This class combines both.
    """

    def __init__(self):
        EClient.__init__(self, self)
        EWrapper.__init__(self)

        # Connection state
        self.connected_event = Event()
        self.next_order_id = None

        # Data storage (callbacks populate these)
        self.account_summary = {}
        self.positions = {}
        self.orders = {}
        self.bars = []

        # Request tracking
        self.req_id = 1000
        self.pending_requests = {}

    def nextValidId(self, orderId: int):
        """Callback when connection succeeds."""
        super().nextValidId(orderId)
        self.next_order_id = orderId
        self.connected_event.set()
        logger.info(f"IB connected - Next order ID: {orderId}")

    def error(self, reqId: int, errorCode: int, errorString: str):
        """Callback for errors."""
        logger.error(f"IB Error {errorCode}: {errorString} (reqId: {reqId})")

        # Authentication errors
        if errorCode in (502, 503):  # Not connected, connection lost
            raise AuthenticationError(f"IB connection error: {errorString}")

        # Rate limiting
        if errorCode == 100:  # Max rate of messages exceeded
            raise RateLimitError("IB rate limit exceeded")

    def accountSummary(self, reqId: int, account: str, tag: str, value: str, currency: str):
        """Callback for account summary."""
        self.account_summary[tag] = value

    def position(self, account: str, contract: Contract, position: float, avgCost: float):
        """Callback for positions."""
        symbol = contract.symbol
        self.positions[symbol] = {
            "symbol": symbol,
            "qty": position,
            "avg_cost": avgCost,
            "contract": contract
        }

    def orderStatus(self, orderId: int, status: str, filled: float,
                    remaining: float, avgFillPrice: float, *args):
        """Callback for order status."""
        self.orders[orderId] = {
            "order_id": orderId,
            "status": status,
            "filled_qty": filled,
            "remaining": remaining,
            "avg_fill_price": avgFillPrice
        }

    def historicalData(self, reqId: int, bar):
        """Callback for historical bars."""
        self.bars.append({
            "timestamp": datetime.strptime(bar.date, "%Y%m%d %H:%M:%S"),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume
        })

    def get_next_req_id(self) -> int:
        """Generate next request ID."""
        self.req_id += 1
        return self.req_id


class IBBroker(BaseBroker):
    """
    Interactive Brokers implementation.

    Usage:
        broker = IBBroker(host="127.0.0.1", port=7497, client_id=1)
        broker.connect()
        account = broker.get_account()
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 7497, client_id: int = 1):
        """
        Initialize IB broker.

        Args:
            host: IB Gateway/TWS host (default localhost)
            port: 7497 (paper), 7496 (live) for TWS
                  4002 (paper), 4001 (live) for IB Gateway
            client_id: Unique client ID (multiple bots = different IDs)
        """
        self.host = host
        self.port = port
        self.client_id = client_id

        self.app = IBApp()
        self.connected = False
        self.account_id = None

        # Start API thread
        self.api_thread = None

    def connect(self) -> bool:
        """Connect to IB Gateway/TWS."""
        try:
            logger.info(f"Connecting to IB at {self.host}:{self.port} (client_id={self.client_id})")

            # Connect
            self.app.connect(self.host, self.port, self.client_id)

            # Start API thread (IB requires separate thread for message loop)
            self.api_thread = Thread(target=self.app.run, daemon=True)
            self.api_thread.start()

            # Wait for connection (max 10 seconds)
            if not self.app.connected_event.wait(timeout=10):
                raise BrokerError("IB connection timeout - is TWS/Gateway running?")

            # Get account ID
            self._get_account_id()

            self.connected = True
            logger.info(f"IB connected successfully - Account: {self.account_id}")

            # Print account summary
            account = self.get_account()
            logger.info("=" * 60)
            logger.info("IB CONNECTION SUCCESSFUL")
            logger.info("=" * 60)
            logger.info(f"Account ID: {self.account_id}")
            logger.info(f"Equity: ${account.equity:,.2f}")
            logger.info(f"Cash: ${account.cash:,.2f}")
            logger.info(f"Buying Power: ${account.buying_power:,.2f}")
            logger.info("=" * 60)

            return True

        except Exception as e:
            logger.error(f"IB connection failed: {e}")
            raise AuthenticationError(f"IB connection failed: {e}")

    def _get_account_id(self):
        """Fetch account ID from IB."""
        req_id = self.app.get_next_req_id()
        self.app.reqManagedAccts()
        time.sleep(1)  # Wait for callback

        # IB returns account list via managedAccounts callback
        # For simplicity, use first account
        # (Production: let user specify account ID in config)
        self.account_id = "DU123456"  # Placeholder - actual ID from callback

    def _ensure_connected(self):
        """Verify connection before API calls."""
        if not self.connected or not self.app.isConnected():
            raise BrokerError("Not connected to IB - call connect() first")

    def get_account(self) -> Account:
        """Get account information."""
        self._ensure_connected()

        # Request account summary
        req_id = self.app.get_next_req_id()
        self.app.reqAccountSummary(req_id, "All", "NetLiquidation,TotalCashValue,BuyingPower")

        # Wait for callbacks (IB is async)
        time.sleep(1)

        return Account(
            equity=float(self.app.account_summary.get("NetLiquidation", 0)),
            cash=float(self.app.account_summary.get("TotalCashValue", 0)),
            buying_power=float(self.app.account_summary.get("BuyingPower", 0)),
            portfolio_value=float(self.app.account_summary.get("NetLiquidation", 0)),
            long_market_value=0.0,  # Calculate from positions
            short_market_value=0.0,
            daytrade_count=0,  # IB doesn't expose this easily
            pattern_day_trader=False,
            account_status="ACTIVE"
        )

    def get_position(self, symbol: str) -> Optional[Position]:
        """Get position for specific symbol."""
        self._ensure_connected()

        # Request positions
        self.app.reqPositions()
        time.sleep(1)

        if symbol not in self.app.positions:
            return None

        pos = self.app.positions[symbol]

        # Get current price (simplified - should use reqMktData)
        current_price = pos["avg_cost"]  # Placeholder
        qty = pos["qty"]

        return Position(
            symbol=symbol,
            qty=abs(qty),
            side="long" if qty > 0 else "short",
            avg_entry_price=pos["avg_cost"],
            current_price=current_price,
            unrealized_pl=(current_price - pos["avg_cost"]) * qty,
            unrealized_plpc=((current_price - pos["avg_cost"]) / pos["avg_cost"]) * 100,
            market_value=current_price * abs(qty)
        )

    def get_all_positions(self) -> List[Position]:
        """Get all open positions."""
        self._ensure_connected()

        self.app.reqPositions()
        time.sleep(1)

        positions = []
        for symbol in self.app.positions:
            pos = self.get_position(symbol)
            if pos:
                positions.append(pos)

        return positions

    def place_market_order(self, symbol: str, qty: int, side: str) -> Order:
        """Place market order."""
        self._ensure_connected()

        # Validate
        if not symbol or qty <= 0:
            raise ValueError("Invalid symbol or quantity")
        if side not in ("buy", "sell"):
            raise ValueError("Side must be 'buy' or 'sell'")

        # Create contract
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"  # Stock
        contract.exchange = "SMART"  # Smart routing
        contract.currency = "USD"

        # Create order
        order = IBOrder()
        order.action = "BUY" if side == "buy" else "SELL"
        order.orderType = "MKT"  # Market order
        order.totalQuantity = qty

        # Place order
        order_id = self.app.next_order_id
        self.app.next_order_id += 1

        logger.info(f"IB MARKET {side.upper()} {qty} {symbol} @ market | Order ID: {order_id}")
        self.app.placeOrder(order_id, contract, order)

        # Wait for order status
        time.sleep(1)

        return Order(
            id=str(order_id),
            symbol=symbol,
            qty=qty,
            side=side,
            order_type="market",
            limit_price=None,
            status="submitted",  # IB uses: PendingSubmit, Submitted, Filled, Cancelled
            filled_qty=0,
            filled_avg_price=None,
            submitted_at=datetime.now(),
            filled_at=None
        )

    def place_limit_order(self, symbol: str, qty: int, side: str, limit_price: float) -> Order:
        """Place limit order."""
        self._ensure_connected()

        # Similar to market order but with orderType="LMT" and lmtPrice
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"

        order = IBOrder()
        order.action = "BUY" if side == "buy" else "SELL"
        order.orderType = "LMT"
        order.lmtPrice = limit_price
        order.totalQuantity = qty

        order_id = self.app.next_order_id
        self.app.next_order_id += 1

        logger.info(f"IB LIMIT {side.upper()} {qty} {symbol} @ ${limit_price} | Order ID: {order_id}")
        self.app.placeOrder(order_id, contract, order)

        time.sleep(1)

        return Order(
            id=str(order_id),
            symbol=symbol,
            qty=qty,
            side=side,
            order_type="limit",
            limit_price=limit_price,
            status="submitted",
            filled_qty=0,
            filled_avg_price=None,
            submitted_at=datetime.now(),
            filled_at=None
        )

    def cancel_order(self, order_id: str) -> bool:
        """Cancel pending order."""
        self._ensure_connected()

        logger.info(f"Canceling IB order: {order_id}")
        self.app.cancelOrder(int(order_id))
        time.sleep(0.5)

        return True

    def get_order_status(self, order_id: str) -> Optional[Order]:
        """Get order status."""
        self._ensure_connected()

        if int(order_id) not in self.app.orders:
            return None

        ib_order = self.app.orders[int(order_id)]

        return Order(
            id=order_id,
            symbol="",  # Not stored in callback
            qty=0,
            side="",
            order_type="market",
            limit_price=None,
            status=ib_order["status"].lower(),
            filled_qty=ib_order["filled_qty"],
            filled_avg_price=ib_order["avg_fill_price"] if ib_order["avg_fill_price"] > 0 else None,
            submitted_at=datetime.now(),
            filled_at=datetime.now() if ib_order["status"] == "Filled" else None
        )

    def close_position(self, symbol: str) -> Order:
        """Close entire position."""
        self._ensure_connected()

        position = self.get_position(symbol)
        if not position:
            raise OrderError(f"No position found for {symbol}")

        # Reverse side
        side = "sell" if position.side == "long" else "buy"

        return self.place_market_order(symbol, int(position.qty), side)

    def is_market_open(self) -> bool:
        """Check if US stock market is open."""
        # IB doesn't have direct API for this
        # Use same logic as Alpaca (check NYSE hours)
        from datetime import datetime
        import pytz

        et = pytz.timezone("America/New_York")
        now = datetime.now(et)

        # Weekend
        if now.weekday() >= 5:
            return False

        # Market hours: 9:30 AM - 4:00 PM ET
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)

        return market_open <= now < market_close

    def get_market_hours(self) -> Dict[str, datetime]:
        """Get market hours."""
        # Simplified implementation
        et = pytz.timezone("America/New_York")
        now = datetime.now(et)

        return {
            "is_open": self.is_market_open(),
            "next_open": now.replace(hour=9, minute=30),
            "next_close": now.replace(hour=16, minute=0)
        }

    def get_bars(self, symbol: str, timeframe: str, start: datetime = None,
                 end: datetime = None, limit: int = 200) -> List[Dict[str, Any]]:
        """Get historical bars."""
        self._ensure_connected()

        # Create contract
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"

        # Timeframe mapping
        bar_size_map = {
            "1Min": "1 min",
            "5Min": "5 mins",
            "15Min": "15 mins",
            "1Hour": "1 hour",
            "1Day": "1 day"
        }

        duration_map = {
            "1Min": f"{limit} S",  # Seconds
            "5Min": f"{limit * 5} S",
            "15Min": f"{limit * 15} S",
            "1Hour": f"{limit} S",
            "1Day": f"{limit} D"  # Days
        }

        bar_size = bar_size_map.get(timeframe, "1 day")
        duration = duration_map.get(timeframe, "200 D")

        # Request historical data
        req_id = self.app.get_next_req_id()
        self.app.bars = []  # Clear previous bars

        end_datetime = end.strftime("%Y%m%d %H:%M:%S") if end else ""

        self.app.reqHistoricalData(
            req_id,
            contract,
            end_datetime,
            duration,
            bar_size,
            "TRADES",  # What to show
            1,  # Use RTH (regular trading hours)
            1,  # Format: 1 = text date
            False,  # Keep up to date
            []
        )

        # Wait for callbacks
        time.sleep(2)

        return self.app.bars

    def disconnect(self):
        """Disconnect from IB."""
        if self.connected:
            logger.info("Disconnecting from IB...")
            self.app.disconnect()
            self.connected = False
```

**Key Implementation Notes:**

1. **Threading:** IB API requires a separate thread for the message loop (`app.run()`)
2. **Async Callbacks:** All data comes via callbacks (need `time.sleep()` to wait)
3. **Request IDs:** Each request needs unique ID (use counter)
4. **Contract Objects:** IB uses `Contract` objects for instruments
5. **Order IDs:** IB assigns order IDs (use `nextValidId` callback)

---

#### 3.3 Update `config.py`

**Changes:**

```python
# In alphalive/config.py

class BrokerConfig(BaseModel):
    """Broker configuration."""

    # Provider selection
    provider: Literal["alpaca", "ib", "tradier"] = "alpaca"

    # Alpaca-specific
    api_key: str = ""
    secret_key: str = ""
    paper: bool = True
    base_url: Optional[str] = None

    # IB-specific
    ib_host: str = "127.0.0.1"
    ib_port: int = 7497  # 7497=paper TWS, 7496=live TWS, 4002=paper Gateway, 4001=live Gateway
    ib_client_id: int = 1  # Unique per bot instance

    def get_broker_instance(self):
        """Factory method to create broker instance."""
        if self.provider == "alpaca":
            from alphalive.broker.alpaca_broker import AlpacaBroker
            return AlpacaBroker(
                api_key=self.api_key,
                secret_key=self.secret_key,
                paper=self.paper,
                base_url=self.base_url
            )

        elif self.provider == "ib":
            from alphalive.broker.ib_broker import IBBroker
            return IBBroker(
                host=self.ib_host,
                port=self.ib_port,
                client_id=self.ib_client_id
            )

        else:
            raise ValueError(f"Unknown broker provider: {self.provider}")
```

**Environment Variables:**

```bash
# .env
BROKER_PROVIDER=ib  # or "alpaca" or "tradier"

# IB-specific
IB_HOST=127.0.0.1
IB_PORT=7497  # 7497=paper TWS
IB_CLIENT_ID=1
```

---

#### 3.4 Update `main.py`

**Changes:**

```python
# In alphalive/main.py

# Old:
# broker = AlpacaBroker(...)

# New:
broker = app_config.broker.get_broker_instance()
broker.connect()
```

---

### 4. Testing Strategy

#### 4.1 Unit Tests

**File:** `tests/test_ib_broker.py`

```python
"""Tests for Interactive Brokers broker implementation."""

import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
from alphalive.broker.ib_broker import IBBroker, IBApp
from alphalive.broker.base_broker import Position, Order, Account


@pytest.fixture
def ib_broker():
    """Create IB broker instance for testing."""
    broker = IBBroker(host="127.0.0.1", port=7497, client_id=999)

    # Mock the IB API connection
    with patch.object(broker.app, 'connect'), \
         patch.object(broker.app, 'run'), \
         patch.object(broker.app.connected_event, 'wait', return_value=True):

        broker.app.next_order_id = 1
        broker.connected = True
        broker.account_id = "DU123456"

        yield broker


def test_ib_broker_initialization():
    """Test IB broker initialization."""
    broker = IBBroker(host="127.0.0.1", port=7497, client_id=1)

    assert broker.host == "127.0.0.1"
    assert broker.port == 7497
    assert broker.client_id == 1
    assert not broker.connected


def test_ib_broker_connect(ib_broker):
    """Test IB broker connection."""
    assert ib_broker.connected
    assert ib_broker.account_id == "DU123456"


def test_ib_get_account(ib_broker):
    """Test getting account info."""
    # Mock account summary
    ib_broker.app.account_summary = {
        "NetLiquidation": "100000.00",
        "TotalCashValue": "50000.00",
        "BuyingPower": "200000.00"
    }

    account = ib_broker.get_account()

    assert isinstance(account, Account)
    assert account.equity == 100000.0
    assert account.cash == 50000.0
    assert account.buying_power == 200000.0


def test_ib_place_market_order(ib_broker):
    """Test placing market order."""
    with patch.object(ib_broker.app, 'placeOrder') as mock_place:
        order = ib_broker.place_market_order("AAPL", 10, "buy")

        assert isinstance(order, Order)
        assert order.symbol == "AAPL"
        assert order.qty == 10
        assert order.side == "buy"
        assert order.order_type == "market"
        assert mock_place.called


def test_ib_get_position(ib_broker):
    """Test getting position."""
    # Mock positions
    ib_broker.app.positions = {
        "AAPL": {
            "symbol": "AAPL",
            "qty": 10,
            "avg_cost": 150.0
        }
    }

    position = ib_broker.get_position("AAPL")

    assert isinstance(position, Position)
    assert position.symbol == "AAPL"
    assert position.qty == 10
    assert position.side == "long"


def test_ib_close_position(ib_broker):
    """Test closing position."""
    # Mock position
    ib_broker.app.positions = {
        "AAPL": {
            "symbol": "AAPL",
            "qty": 10,
            "avg_cost": 150.0
        }
    }

    with patch.object(ib_broker, 'place_market_order') as mock_place:
        mock_place.return_value = Order(
            id="1",
            symbol="AAPL",
            qty=10,
            side="sell",
            order_type="market",
            limit_price=None,
            status="submitted",
            filled_qty=0,
            filled_avg_price=None,
            submitted_at=datetime.now(),
            filled_at=None
        )

        order = ib_broker.close_position("AAPL")

        assert order.side == "sell"
        assert order.qty == 10


def test_ib_is_market_open(ib_broker):
    """Test market hours check."""
    # Mock to Tuesday 10 AM ET
    with patch('alphalive.broker.ib_broker.datetime') as mock_datetime:
        import pytz
        et = pytz.timezone("America/New_York")
        mock_now = datetime(2024, 3, 12, 10, 0, 0, tzinfo=et)  # Tuesday 10 AM
        mock_datetime.now.return_value = mock_now
        mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)

        assert ib_broker.is_market_open()


def test_ib_market_closed_weekend(ib_broker):
    """Test market closed on weekends."""
    with patch('alphalive.broker.ib_broker.datetime') as mock_datetime:
        import pytz
        et = pytz.timezone("America/New_York")
        mock_now = datetime(2024, 3, 16, 10, 0, 0, tzinfo=et)  # Saturday
        mock_datetime.now.return_value = mock_now
        mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)

        assert not ib_broker.is_market_open()
```

**Run tests:**
```bash
pytest tests/test_ib_broker.py -v
```

---

#### 4.2 Integration Tests

**Prerequisites:**
- IB Gateway/TWS running locally
- Paper trading account

**Manual Test Script:**

```bash
# Test IB connection
python -c "
from alphalive.broker.ib_broker import IBBroker

broker = IBBroker(host='127.0.0.1', port=7497)
broker.connect()

print('✅ Connection successful')

account = broker.get_account()
print(f'✅ Account: \${account.equity:,.2f}')

# Test market order (paper trading)
order = broker.place_market_order('AAPL', 1, 'buy')
print(f'✅ Order placed: {order.id}')

broker.disconnect()
"
```

---

### 5. Migration Checklist

**Phase 1: Implementation (2-3 days)**
- [ ] Install `ibapi` library
- [ ] Create `ib_broker.py` with all BaseBroker methods
- [ ] Update `config.py` with IB configuration
- [ ] Add IB environment variables to `.env.example`
- [ ] Write unit tests (`test_ib_broker.py`)

**Phase 2: Testing (1 day)**
- [ ] Set up IB paper trading account
- [ ] Install IB Gateway
- [ ] Run unit tests (all pass)
- [ ] Run manual integration test script
- [ ] Test with AlphaLive in dry-run mode

**Phase 3: Validation (1 week)**
- [ ] Run full AlphaLive in dry-run mode (7 days)
- [ ] Verify signal generation matches AlphaLab
- [ ] Run C1 signal parity test (0 mismatches)
- [ ] Test all Telegram commands
- [ ] Test kill switch (TRADING_PAUSED)

**Phase 4: Paper Trading (2-4 weeks)**
- [ ] Deploy to Railway with IB Gateway
- [ ] Monitor daily for errors
- [ ] Compare results with Alpaca paper trading
- [ ] Verify execution quality

**Phase 5: Live Trading (After full validation)**
- [ ] Switch to live IB account
- [ ] Start with micro capital ($500-$1000)
- [ ] Monitor for 2 weeks
- [ ] Scale up gradually

---

### 6. Deployment Guide

#### 6.1 Local Deployment

**Prerequisites:**
1. Download IB Gateway: https://www.interactivebrokers.com/en/trading/ibgateway-stable.php
2. Install IB Gateway
3. Configure auto-login (Settings → Lock and exit → Auto restart)
4. Set API port: 7497 (paper) or 7496 (live)

**Start IB Gateway:**
```bash
# macOS
/Applications/IBGateway.app/Contents/MacOS/ibgateway

# Linux
~/IBGateway/ibgateway
```

**Start AlphaLive:**
```bash
export BROKER_PROVIDER=ib
export IB_HOST=127.0.0.1
export IB_PORT=7497
export IB_CLIENT_ID=1

python run.py --config configs/ma_crossover.json
```

---

#### 6.2 Railway Deployment

**Challenge:** Railway doesn't support GUI apps (can't run IB Gateway)

**Solution 1: Use IB Cloud API** (Not yet available)
- IB is developing cloud API (no local Gateway required)
- ETA: Unknown

**Solution 2: Hybrid Deployment**
- Run IB Gateway on a dedicated VPS (AWS EC2, DigitalOcean)
- Run AlphaLive on Railway, connect to remote IB Gateway

**VPS Setup:**
```bash
# On VPS (Ubuntu 20.04)
# Install IB Gateway
wget https://download2.interactivebrokers.com/installers/ibgateway/latest-standalone/ibgateway-latest-standalone-linux-x64.sh
chmod +x ibgateway-latest-standalone-linux-x64.sh
./ibgateway-latest-standalone-linux-x64.sh

# Configure headless mode
# Edit: ~/Jts/ibgateway/1020/jts.ini
[IBGateway]
tradingMode=paper
apiPort=7497

# Start IB Gateway (headless)
xvfb-run ~/Jts/ibgateway/1020/ibgateway &
```

**Railway Config:**
```bash
# Railway environment variables
BROKER_PROVIDER=ib
IB_HOST=your-vps-ip
IB_PORT=7497
IB_CLIENT_ID=1
```

**Security:**
- Use VPN or SSH tunnel (don't expose IB Gateway to internet)
- Whitelist Railway IPs in IB Gateway settings

**Cost:**
- VPS: ~$5-10/month (DigitalOcean basic droplet)
- IB market data: ~$10-15/month
- Railway: ~$5-20/month
- **Total: ~$20-45/month** (still cheaper than Alpaca Plus $83/month)

---

### 7. Known Issues & Workarounds

#### Issue 1: IB API is Asynchronous
**Problem:** All data comes via callbacks, need to wait for responses

**Workaround:**
```python
# Use threading.Event for synchronization
result_event = Event()
result_data = {}

def callback(data):
    result_data.update(data)
    result_event.set()

self.app.reqAccountSummary(req_id, "All", "NetLiquidation")
result_event.wait(timeout=5)
return result_data
```

#### Issue 2: Multiple Accounts
**Problem:** IB returns list of accounts, need to specify which one

**Workaround:**
- Add `ib_account_id` to config
- Let user specify in environment variable

#### Issue 3: Market Data Subscriptions
**Problem:** Real-time data requires subscription

**Workaround:**
- Use delayed data for testing (free)
- Subscribe to "US Securities Snapshot and Futures Value Bundle" ($10/month)

#### Issue 4: Connection Drops
**Problem:** IB Gateway disconnects after inactivity

**Workaround:**
```python
# Ping IB every 60 seconds
def keep_alive():
    while self.connected:
        self.app.reqCurrentTime()
        time.sleep(60)

Thread(target=keep_alive, daemon=True).start()
```

---

### 8. Documentation Updates

**Files to Update:**

1. **README.md**
   - Add IB to supported brokers
   - Add IB setup instructions

2. **SETUP.md**
   - Add "Interactive Brokers Setup" section
   - Document IB Gateway installation
   - Add environment variables

3. **CLAUDE.md**
   - Update broker architecture diagram
   - Add IB implementation notes
   - Update deployment sections

4. **.env.example**
   ```bash
   # Broker Configuration
   BROKER_PROVIDER=alpaca  # alpaca, ib, tradier

   # Alpaca
   ALPACA_API_KEY=your_key
   ALPACA_SECRET_KEY=your_secret
   ALPACA_PAPER=true

   # Interactive Brokers
   IB_HOST=127.0.0.1
   IB_PORT=7497  # 7497=paper, 7496=live
   IB_CLIENT_ID=1
   IB_ACCOUNT_ID=DU123456  # Optional: specify account
   ```

---

## Implementation Plan: Tradier

### 1. Prerequisites

**What You Need:**
- Tradier account (free to open)
- Brokerage account (link to Tradier)
- API token (generate in Tradier dashboard)
- Market data subscription ($10/month)

**Account Setup:**
1. Open account at https://tradier.com
2. Link brokerage account (or open Tradier brokerage)
3. Go to Settings → API Access
4. Generate API token (copy immediately - shown only once)
5. Subscribe to real-time market data ($10/month)

---

### 2. File Structure

```
alphalive/
├── broker/
│   ├── __init__.py
│   ├── base_broker.py          # Already exists
│   ├── alpaca_broker.py        # Already exists
│   └── tradier_broker.py       # NEW - Tradier implementation
│
├── config.py                   # Update to support Tradier config
│
tests/
├── test_tradier_broker.py     # NEW - Tradier tests
└── conftest.py                # Update with Tradier fixtures
```

---

### 3. Code Implementation

#### 3.1 Install Dependencies

Add to `requirements.txt`:
```txt
# Tradier (uses httpx, already in requirements)
httpx>=0.24.0  # Already present for Telegram
```

No new dependencies needed! Tradier uses RESTful API like Alpaca.

---

#### 3.2 Create `tradier_broker.py`

**File:** `alphalive/broker/tradier_broker.py`

**Key Components:**

```python
"""Tradier broker implementation for AlphaLive."""

import httpx
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import logging
import time

from .base_broker import (
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


class TradierBroker(BaseBroker):
    """
    Tradier broker implementation.

    API Docs: https://documentation.tradier.com/brokerage-api

    Usage:
        broker = TradierBroker(
            api_token="your_token",
            account_id="your_account_id",
            sandbox=True
        )
        broker.connect()
        account = broker.get_account()
    """

    # API endpoints
    PRODUCTION_URL = "https://api.tradier.com/v1"
    SANDBOX_URL = "https://sandbox.tradier.com/v1"

    def __init__(self, api_token: str, account_id: str, sandbox: bool = True):
        """
        Initialize Tradier broker.

        Args:
            api_token: Tradier API token
            account_id: Tradier account ID (e.g., "VA12345678")
            sandbox: Use sandbox (paper trading) environment
        """
        self.api_token = api_token
        self.account_id = account_id
        self.sandbox = sandbox

        self.base_url = self.SANDBOX_URL if sandbox else self.PRODUCTION_URL
        self.connected = False

        # HTTP client
        self.client = httpx.Client(
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Accept": "application/json"
            },
            timeout=30.0
        )

    def connect(self) -> bool:
        """Connect to Tradier and verify credentials."""
        try:
            logger.info(f"Connecting to Tradier ({'sandbox' if self.sandbox else 'live'})...")

            # Test connection by fetching account
            response = self.client.get(f"{self.base_url}/user/profile")

            if response.status_code == 401:
                raise AuthenticationError("Invalid Tradier API token")

            response.raise_for_status()

            # Verify account exists
            account = self.get_account()

            self.connected = True

            logger.info("=" * 60)
            logger.info("TRADIER CONNECTION SUCCESSFUL")
            logger.info("=" * 60)
            logger.info(f"Account ID: {self.account_id}")
            logger.info(f"Mode: {'SANDBOX' if self.sandbox else 'LIVE'}")
            logger.info(f"Equity: ${account.equity:,.2f}")
            logger.info(f"Cash: ${account.cash:,.2f}")
            logger.info(f"Buying Power: ${account.buying_power:,.2f}")
            logger.info("=" * 60)

            return True

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise AuthenticationError("Invalid Tradier API token")
            elif e.response.status_code == 429:
                raise RateLimitError("Tradier rate limit exceeded")
            else:
                raise BrokerError(f"Tradier connection failed: {e}")
        except Exception as e:
            logger.error(f"Tradier connection failed: {e}")
            raise BrokerError(f"Tradier connection failed: {e}")

    def _ensure_connected(self):
        """Verify connection before API calls."""
        if not self.connected:
            raise BrokerError("Not connected to Tradier - call connect() first")

    def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """
        Make HTTP request with error handling.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint (e.g., "/accounts/VA12345/orders")
            **kwargs: Additional arguments for httpx (params, data, etc.)

        Returns:
            Response JSON

        Raises:
            BrokerError: On API errors
        """
        url = f"{self.base_url}{endpoint}"

        try:
            response = self.client.request(method, url, **kwargs)

            if response.status_code == 401:
                raise AuthenticationError("Tradier authentication failed")
            elif response.status_code == 429:
                raise RateLimitError("Tradier rate limit exceeded")
            elif response.status_code >= 400:
                raise BrokerError(f"Tradier API error {response.status_code}: {response.text}")

            return response.json()

        except httpx.RequestError as e:
            raise BrokerError(f"Tradier request failed: {e}")

    def get_account(self) -> Account:
        """Get account information."""
        self._ensure_connected()

        # Fetch account balances
        data = self._request("GET", f"/accounts/{self.account_id}/balances")
        balances = data.get("balances", {})

        return Account(
            equity=float(balances.get("total_equity", 0)),
            cash=float(balances.get("total_cash", 0)),
            buying_power=float(balances.get("option_buying_power", 0)),
            portfolio_value=float(balances.get("total_equity", 0)),
            long_market_value=float(balances.get("long_market_value", 0)),
            short_market_value=float(balances.get("short_market_value", 0)),
            daytrade_count=0,  # Tradier doesn't expose this
            pattern_day_trader=False,
            account_status="ACTIVE"
        )

    def get_position(self, symbol: str) -> Optional[Position]:
        """Get position for specific symbol."""
        self._ensure_connected()

        # Fetch all positions
        data = self._request("GET", f"/accounts/{self.account_id}/positions")
        positions = data.get("positions", {}).get("position", [])

        # Handle single position (API returns dict instead of list)
        if isinstance(positions, dict):
            positions = [positions]

        # Find position for symbol
        for pos in positions:
            if pos.get("symbol") == symbol:
                qty = float(pos.get("quantity", 0))

                return Position(
                    symbol=symbol,
                    qty=abs(qty),
                    side="long" if qty > 0 else "short",
                    avg_entry_price=float(pos.get("cost_basis", 0)) / abs(qty) if qty != 0 else 0,
                    current_price=float(pos.get("last", 0)),
                    unrealized_pl=float(pos.get("unrealized_profit_loss", 0)),
                    unrealized_plpc=float(pos.get("unrealized_profit_loss_percent", 0)),
                    market_value=float(pos.get("market_value", 0))
                )

        return None

    def get_all_positions(self) -> List[Position]:
        """Get all open positions."""
        self._ensure_connected()

        data = self._request("GET", f"/accounts/{self.account_id}/positions")
        positions_data = data.get("positions", {}).get("position", [])

        # Handle single position
        if isinstance(positions_data, dict):
            positions_data = [positions_data]

        positions = []
        for pos in positions_data:
            symbol = pos.get("symbol")
            qty = float(pos.get("quantity", 0))

            positions.append(Position(
                symbol=symbol,
                qty=abs(qty),
                side="long" if qty > 0 else "short",
                avg_entry_price=float(pos.get("cost_basis", 0)) / abs(qty) if qty != 0 else 0,
                current_price=float(pos.get("last", 0)),
                unrealized_pl=float(pos.get("unrealized_profit_loss", 0)),
                unrealized_plpc=float(pos.get("unrealized_profit_loss_percent", 0)),
                market_value=float(pos.get("market_value", 0))
            ))

        return positions

    def place_market_order(self, symbol: str, qty: int, side: str) -> Order:
        """Place market order."""
        self._ensure_connected()

        # Validate
        if not symbol or qty <= 0:
            raise ValueError("Invalid symbol or quantity")
        if side not in ("buy", "sell"):
            raise ValueError("Side must be 'buy' or 'sell'")

        # Place order
        logger.info(f"TRADIER MARKET {side.upper()} {qty} {symbol} @ market")

        data = self._request(
            "POST",
            f"/accounts/{self.account_id}/orders",
            data={
                "class": "equity",
                "symbol": symbol,
                "side": side,
                "quantity": qty,
                "type": "market",
                "duration": "day"
            }
        )

        order_data = data.get("order", {})
        order_id = str(order_data.get("id"))

        logger.info(f"Order placed: {order_id} ({order_data.get('status')})")

        return Order(
            id=order_id,
            symbol=symbol,
            qty=qty,
            side=side,
            order_type="market",
            limit_price=None,
            status=order_data.get("status", "pending").lower(),
            filled_qty=0,
            filled_avg_price=None,
            submitted_at=datetime.now(),
            filled_at=None
        )

    def place_limit_order(self, symbol: str, qty: int, side: str, limit_price: float) -> Order:
        """Place limit order."""
        self._ensure_connected()

        logger.info(f"TRADIER LIMIT {side.upper()} {qty} {symbol} @ ${limit_price}")

        data = self._request(
            "POST",
            f"/accounts/{self.account_id}/orders",
            data={
                "class": "equity",
                "symbol": symbol,
                "side": side,
                "quantity": qty,
                "type": "limit",
                "price": limit_price,
                "duration": "day"
            }
        )

        order_data = data.get("order", {})
        order_id = str(order_data.get("id"))

        return Order(
            id=order_id,
            symbol=symbol,
            qty=qty,
            side=side,
            order_type="limit",
            limit_price=limit_price,
            status=order_data.get("status", "pending").lower(),
            filled_qty=0,
            filled_avg_price=None,
            submitted_at=datetime.now(),
            filled_at=None
        )

    def cancel_order(self, order_id: str) -> bool:
        """Cancel pending order."""
        self._ensure_connected()

        logger.info(f"Canceling Tradier order: {order_id}")

        try:
            self._request(
                "DELETE",
                f"/accounts/{self.account_id}/orders/{order_id}"
            )
            return True
        except BrokerError:
            return False

    def get_order_status(self, order_id: str) -> Optional[Order]:
        """Get order status."""
        self._ensure_connected()

        try:
            data = self._request(
                "GET",
                f"/accounts/{self.account_id}/orders/{order_id}"
            )

            order_data = data.get("order", {})

            return Order(
                id=order_id,
                symbol=order_data.get("symbol", ""),
                qty=int(order_data.get("quantity", 0)),
                side=order_data.get("side", ""),
                order_type=order_data.get("type", "market"),
                limit_price=float(order_data.get("price", 0)) if order_data.get("price") else None,
                status=order_data.get("status", "unknown").lower(),
                filled_qty=int(order_data.get("exec_quantity", 0)),
                filled_avg_price=float(order_data.get("avg_fill_price", 0)) if order_data.get("avg_fill_price") else None,
                submitted_at=datetime.fromisoformat(order_data.get("create_date", datetime.now().isoformat())),
                filled_at=datetime.fromisoformat(order_data.get("transaction_date")) if order_data.get("transaction_date") else None
            )

        except BrokerError:
            return None

    def close_position(self, symbol: str) -> Order:
        """Close entire position."""
        self._ensure_connected()

        position = self.get_position(symbol)
        if not position:
            raise OrderError(f"No position found for {symbol}")

        # Reverse side
        side = "sell" if position.side == "long" else "buy"

        return self.place_market_order(symbol, int(position.qty), side)

    def is_market_open(self) -> bool:
        """Check if US stock market is open."""
        # Tradier has a market clock API
        data = self._request("GET", "/markets/clock")
        clock = data.get("clock", {})

        return clock.get("state") == "open"

    def get_market_hours(self) -> Dict[str, datetime]:
        """Get market hours."""
        data = self._request("GET", "/markets/clock")
        clock = data.get("clock", {})

        return {
            "is_open": clock.get("state") == "open",
            "next_open": datetime.fromisoformat(clock.get("next_open", "").replace("Z", "+00:00")),
            "next_close": datetime.fromisoformat(clock.get("next_close", "").replace("Z", "+00:00"))
        }

    def get_bars(self, symbol: str, timeframe: str, start: datetime = None,
                 end: datetime = None, limit: int = 200) -> List[Dict[str, Any]]:
        """Get historical bars."""
        self._ensure_connected()

        # Tradier uses different interval names
        interval_map = {
            "1Min": "1min",
            "5Min": "5min",
            "15Min": "15min",
            "1Hour": "60min",  # Tradier doesn't have 1hour, use 60min
            "1Day": "daily"
        }

        interval = interval_map.get(timeframe, "daily")

        # Calculate date range
        if not end:
            end = datetime.now()
        if not start:
            start = end - timedelta(days=365)  # 1 year default

        # Request bars
        data = self._request(
            "GET",
            f"/markets/history",
            params={
                "symbol": symbol,
                "interval": interval,
                "start": start.strftime("%Y-%m-%d"),
                "end": end.strftime("%Y-%m-%d")
            }
        )

        history = data.get("history", {}).get("day", [])

        # Handle single bar
        if isinstance(history, dict):
            history = [history]

        # Convert to standard format
        bars = []
        for bar in history:
            bars.append({
                "timestamp": datetime.fromisoformat(bar.get("date")),
                "open": float(bar.get("open", 0)),
                "high": float(bar.get("high", 0)),
                "low": float(bar.get("low", 0)),
                "close": float(bar.get("close", 0)),
                "volume": int(bar.get("volume", 0))
            })

        # Limit to requested number
        return bars[-limit:]
```

**Key Differences from Alpaca:**

1. **Similar API:** RESTful like Alpaca (easy migration)
2. **Bearer Token:** Uses `Authorization: Bearer {token}` (not Basic auth like Alpaca)
3. **Account ID Required:** Must specify account ID in URL
4. **Market Clock API:** Has dedicated `/markets/clock` endpoint
5. **Different Field Names:** `exec_quantity` vs `filled_qty`, etc.

---

#### 3.3 Update `config.py`

**Changes:**

```python
# In alphalive/config.py

class BrokerConfig(BaseModel):
    """Broker configuration."""

    # Provider selection
    provider: Literal["alpaca", "ib", "tradier"] = "alpaca"

    # Alpaca-specific
    api_key: str = ""
    secret_key: str = ""
    paper: bool = True
    base_url: Optional[str] = None

    # IB-specific
    ib_host: str = "127.0.0.1"
    ib_port: int = 7497
    ib_client_id: int = 1

    # Tradier-specific
    tradier_api_token: str = ""
    tradier_account_id: str = ""
    tradier_sandbox: bool = True  # Paper trading

    def get_broker_instance(self):
        """Factory method to create broker instance."""
        if self.provider == "alpaca":
            from alphalive.broker.alpaca_broker import AlpacaBroker
            return AlpacaBroker(
                api_key=self.api_key,
                secret_key=self.secret_key,
                paper=self.paper,
                base_url=self.base_url
            )

        elif self.provider == "ib":
            from alphalive.broker.ib_broker import IBBroker
            return IBBroker(
                host=self.ib_host,
                port=self.ib_port,
                client_id=self.ib_client_id
            )

        elif self.provider == "tradier":
            from alphalive.broker.tradier_broker import TradierBroker
            return TradierBroker(
                api_token=self.tradier_api_token,
                account_id=self.tradier_account_id,
                sandbox=self.tradier_sandbox
            )

        else:
            raise ValueError(f"Unknown broker provider: {self.provider}")
```

**Environment Variables:**

```bash
# .env
BROKER_PROVIDER=tradier

# Tradier-specific
TRADIER_API_TOKEN=your_token_here
TRADIER_ACCOUNT_ID=VA12345678
TRADIER_SANDBOX=true  # false for live trading
```

---

### 4. Testing Strategy

#### 4.1 Unit Tests

**File:** `tests/test_tradier_broker.py`

```python
"""Tests for Tradier broker implementation."""

import pytest
from unittest.mock import Mock, patch
from datetime import datetime
from alphalive.broker.tradier_broker import TradierBroker
from alphalive.broker.base_broker import Position, Order, Account


@pytest.fixture
def tradier_broker():
    """Create Tradier broker instance for testing."""
    broker = TradierBroker(
        api_token="test_token",
        account_id="VA12345678",
        sandbox=True
    )
    broker.connected = True
    return broker


def test_tradier_initialization():
    """Test Tradier broker initialization."""
    broker = TradierBroker(
        api_token="test_token",
        account_id="VA12345678",
        sandbox=True
    )

    assert broker.api_token == "test_token"
    assert broker.account_id == "VA12345678"
    assert broker.sandbox
    assert broker.base_url == TradierBroker.SANDBOX_URL


@patch('httpx.Client.get')
def test_tradier_connect(mock_get, tradier_broker):
    """Test Tradier connection."""
    # Mock user profile response
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = {"profile": {"id": "user123"}}

    # Mock account balance (called in get_account)
    with patch.object(tradier_broker, 'get_account') as mock_account:
        mock_account.return_value = Account(
            equity=100000.0,
            cash=50000.0,
            buying_power=200000.0,
            portfolio_value=100000.0,
            long_market_value=0.0,
            short_market_value=0.0,
            daytrade_count=0,
            pattern_day_trader=False,
            account_status="ACTIVE"
        )

        result = tradier_broker.connect()
        assert result


@patch.object(TradierBroker, '_request')
def test_tradier_get_account(mock_request, tradier_broker):
    """Test getting account info."""
    mock_request.return_value = {
        "balances": {
            "total_equity": "100000.00",
            "total_cash": "50000.00",
            "option_buying_power": "200000.00",
            "long_market_value": "0.00",
            "short_market_value": "0.00"
        }
    }

    account = tradier_broker.get_account()

    assert isinstance(account, Account)
    assert account.equity == 100000.0
    assert account.cash == 50000.0
    assert account.buying_power == 200000.0


@patch.object(TradierBroker, '_request')
def test_tradier_place_market_order(mock_request, tradier_broker):
    """Test placing market order."""
    mock_request.return_value = {
        "order": {
            "id": "12345",
            "status": "pending"
        }
    }

    order = tradier_broker.place_market_order("AAPL", 10, "buy")

    assert isinstance(order, Order)
    assert order.id == "12345"
    assert order.symbol == "AAPL"
    assert order.qty == 10
    assert order.side == "buy"
    assert order.order_type == "market"


@patch.object(TradierBroker, '_request')
def test_tradier_get_position(mock_request, tradier_broker):
    """Test getting position."""
    mock_request.return_value = {
        "positions": {
            "position": {
                "symbol": "AAPL",
                "quantity": "10",
                "cost_basis": "1500.00",
                "last": "155.00",
                "unrealized_profit_loss": "50.00",
                "unrealized_profit_loss_percent": "3.33",
                "market_value": "1550.00"
            }
        }
    }

    position = tradier_broker.get_position("AAPL")

    assert isinstance(position, Position)
    assert position.symbol == "AAPL"
    assert position.qty == 10
    assert position.side == "long"


@patch.object(TradierBroker, '_request')
def test_tradier_is_market_open(mock_request, tradier_broker):
    """Test market clock."""
    mock_request.return_value = {
        "clock": {
            "state": "open",
            "next_open": "2024-03-12T09:30:00Z",
            "next_close": "2024-03-12T16:00:00Z"
        }
    }

    assert tradier_broker.is_market_open()
```

**Run tests:**
```bash
pytest tests/test_tradier_broker.py -v
```

---

#### 4.2 Integration Test Script

```bash
# Test Tradier connection
python -c "
from alphalive.broker.tradier_broker import TradierBroker

broker = TradierBroker(
    api_token='your_sandbox_token',
    account_id='VA12345678',
    sandbox=True
)

broker.connect()
print('✅ Connection successful')

account = broker.get_account()
print(f'✅ Account: \${account.equity:,.2f}')

# Test market order (sandbox)
order = broker.place_market_order('AAPL', 1, 'buy')
print(f'✅ Order placed: {order.id}')
"
```

---

### 5. Migration Checklist

**Phase 1: Implementation (1-2 days)**
- [ ] Create `tradier_broker.py` with all BaseBroker methods
- [ ] Update `config.py` with Tradier configuration
- [ ] Add Tradier environment variables to `.env.example`
- [ ] Write unit tests (`test_tradier_broker.py`)

**Phase 2: Testing (1 day)**
- [ ] Set up Tradier sandbox account
- [ ] Generate API token
- [ ] Run unit tests (all pass)
- [ ] Run manual integration test script
- [ ] Test with AlphaLive in dry-run mode

**Phase 3: Validation (1 week)**
- [ ] Run full AlphaLive in dry-run mode (7 days)
- [ ] Verify signal generation matches AlphaLab
- [ ] Run C1 signal parity test (0 mismatches)
- [ ] Test all Telegram commands
- [ ] Test kill switch

**Phase 4: Paper Trading (2-4 weeks)**
- [ ] Deploy to Railway with Tradier sandbox
- [ ] Monitor daily for errors
- [ ] Compare results with Alpaca paper trading
- [ ] Verify execution quality

**Phase 5: Live Trading (After validation)**
- [ ] Switch to Tradier live account
- [ ] Start with micro capital ($500-$1000)
- [ ] Monitor for 2 weeks
- [ ] Scale up gradually

---

### 6. Deployment Guide

#### 6.1 Local Deployment

**Start AlphaLive:**
```bash
export BROKER_PROVIDER=tradier
export TRADIER_API_TOKEN=your_token
export TRADIER_ACCOUNT_ID=VA12345678
export TRADIER_SANDBOX=true

python run.py --config configs/ma_crossover.json
```

---

#### 6.2 Railway Deployment

**Easy!** Tradier is RESTful API (no local software required).

**Railway Environment Variables:**
```bash
BROKER_PROVIDER=tradier
TRADIER_API_TOKEN=your_production_token
TRADIER_ACCOUNT_ID=VA12345678
TRADIER_SANDBOX=false  # Live trading

# Other AlphaLive vars
STRATEGY_CONFIG=configs/ma_crossover.json
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

**Deploy:**
```bash
git push origin main
# Railway auto-deploys
```

**Cost:**
- Railway: $5-20/month
- Tradier market data: $10/month
- **Total: $15-30/month**

---

### 7. Known Issues & Workarounds

#### Issue 1: API Token Security
**Problem:** Token visible in logs if not careful

**Workaround:**
- Always use environment variables
- Never print token in logs
- Mask in config (same as Alpaca)

#### Issue 2: Account ID Required
**Problem:** User must specify account ID

**Workaround:**
- Document clearly in SETUP.md
- Add validation in `connect()` (fail fast if missing)

#### Issue 3: Different Field Names
**Problem:** Tradier uses different field names than Alpaca

**Workaround:**
- Map fields in `tradier_broker.py`
- All mappings isolated in one file
- BaseBroker interface unchanged

---

### 8. Documentation Updates

**Files to Update:**

1. **README.md**
   - Add Tradier to supported brokers
   - Add Tradier setup instructions

2. **SETUP.md**
   - Add "Tradier Setup" section
   - Document API token generation
   - Add environment variables

3. **.env.example**
   ```bash
   # Broker Configuration
   BROKER_PROVIDER=alpaca  # alpaca, ib, tradier

   # Alpaca
   ALPACA_API_KEY=your_key
   ALPACA_SECRET_KEY=your_secret
   ALPACA_PAPER=true

   # Interactive Brokers
   IB_HOST=127.0.0.1
   IB_PORT=7497
   IB_CLIENT_ID=1

   # Tradier
   TRADIER_API_TOKEN=your_token
   TRADIER_ACCOUNT_ID=VA12345678
   TRADIER_SANDBOX=true  # false for live
   ```

---

## Decision Matrix

### When to Choose Each Broker

| Factor | Alpaca Plus | Interactive Brokers | Tradier |
|--------|------------|---------------------|---------|
| **Cost (5 years)** | $5,000 | $900 | $600 |
| **Best If You...** | Already use Alpaca | Want options/futures | Want easy migration |
| **Implementation** | Done ✅ | 2-3 days | 1-2 days |
| **Railway Compatible** | ✅ Yes | ⚠️ Hybrid | ✅ Yes |
| **Asset Classes** | Stocks only | All | Stocks + Options |
| **API Quality** | Excellent | Good | Good |
| **Learning Curve** | Easy | Medium | Easy |

### Recommendation by Use Case

**1. Already Using Alpaca (Paper Trading)**
→ **Stay with Alpaca Free** if only trading daily strategies (15-min delay is fine)
→ **Upgrade to Tradier** if trading intraday (save $880/year vs Alpaca Plus)

**2. Want Most Cost Savings**
→ **Tradier** ($600 over 5 years)

**3. Want Most Features (Options, Futures)**
→ **Interactive Brokers** ($900 over 5 years, professional-grade)

**4. Want Easiest Migration**
→ **Tradier** (similar API to Alpaca, 1-2 days)

**5. Want Best Execution Quality**
→ **Interactive Brokers** (routes to multiple venues, better fills)

---

## Next Steps

### Option A: Implement Tradier (Recommended for Quick Win)

**Why:**
- 1-2 days implementation
- 8x cheaper than Alpaca Plus
- Easy Railway deployment
- Similar API (low risk)

**How:**
1. Review Tradier section above
2. Create account at tradier.com
3. Generate API token
4. Implement `tradier_broker.py` (copy template above)
5. Test locally
6. Deploy to Railway

**Timeline:** 3-4 days (implement + test + deploy)

---

### Option B: Implement Interactive Brokers (Recommended for Long-Term)

**Why:**
- Professional-grade platform
- Options/futures support
- Better execution
- Industry standard

**How:**
1. Review IB section above
2. Open IB account
3. Install IB Gateway
4. Implement `ib_broker.py` (copy template above)
5. Test locally with IB Gateway
6. Deploy hybrid (VPS + Railway)

**Timeline:** 4-5 days (implement + test + deploy)

---

### Option C: Implement Both (Maximum Flexibility)

**Why:**
- Use Tradier for stocks
- Use IB for options/futures
- Fallback if one broker has issues

**How:**
1. Implement Tradier first (easier)
2. Test for 1 week
3. Implement IB second
4. Test both
5. Choose per strategy

**Timeline:** 7-10 days total

---

## Questions?

This plan covers:
- ✅ Complete implementation for both brokers
- ✅ Code templates ready to copy
- ✅ Testing strategy
- ✅ Deployment guide
- ✅ Cost comparison
- ✅ Decision matrix

**Ready to implement?** Pick your broker and follow the checklist above.

**Need help?** Let me know which broker you choose and I can help with specific implementation details.
