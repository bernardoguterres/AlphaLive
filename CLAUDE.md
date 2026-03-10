# AlphaLive - CLAUDE.md

## Project Overview

AlphaLive is a **24/7 live trading execution engine** designed to run continuously on Railway (or any cloud platform). It executes strategies exported from AlphaLab with real-time risk management, order execution via Alpaca Markets, and Telegram notifications.

**Key Characteristics**:
- **Deployment**: Cloud-native, runs on Railway as a worker process
- **Configuration**: 100% environment variables (no local .env in production)
- **Logging**: Structured logs to STDOUT (Railway captures automatically)
- **Notifications**: Direct Telegram Bot API calls via httpx (no python-telegram-bot library)
- **Architecture**: Modular components with clear separation of concerns

## Architecture Overview

### Module Hierarchy

```
alphalive/
├── main.py                    # Entry point, 24/7 trading loop
├── config.py                  # Load/validate strategy JSON + env vars
├── strategy_schema.py         # Pydantic v2 models for strategy config
│
├── broker/                    # Broker interface and implementations
│   ├── base_broker.py         # Abstract broker interface
│   └── alpaca_broker.py       # Alpaca API implementation (alpaca-py)
│
├── strategy/                  # Signal generation
│   ├── signal_engine.py       # Generate buy/sell signals from market data
│   └── indicators.py          # Technical indicators (SMA, RSI, Bollinger, etc.)
│
├── execution/                 # Order execution and risk
│   ├── order_manager.py       # Place, track, cancel orders
│   └── risk_manager.py        # Position sizing, stop loss, portfolio limits
│
├── notifications/             # Alerts
│   └── telegram_bot.py        # Telegram notifications via httpx
│
├── data/                      # Market data
│   └── market_data.py         # Fetch live/historical price data from broker
│
├── utils/                     # Utilities
│   └── logger.py              # Structured logging to STDOUT
│
└── migrations/                # Schema versioning
    └── schema_migrations.py   # Migrate old strategy JSONs to current schema
```

### Core Components

#### 1. Main Loop (`main.py`)

**Purpose**: Coordinates all subsystems in a 24/7 loop on Railway.

**Implementation**: Simple function-based approach with while-True loop. NOT a cron job. This is a persistent Python process that sleeps when market is closed and wakes up to trade when open.

**Responsibilities**:
- Initialize broker, data fetcher, signal engine, risk manager, order manager, notifier
- Verify timezone (US/Eastern) on startup
- Check market status continuously
- Generate signals at appropriate times (9:35 AM ET for daily strategies)
- Execute trades with risk checks
- Monitor positions for exit conditions every 5 minutes
- Send EOD summary at 3:55 PM ET
- Handle Railway restarts gracefully via SIGTERM

**Main Function Signature**:
```python
def main(config_path: str, dry_run: bool = False, paper: bool = True):
    """
    Main entry point for AlphaLive.
    Runs forever on Railway.
    """
```

**State Tracking Variables**:
```python
today_str = None           # Track current trading day (YYYY-MM-DD)
morning_check_done = False # Has morning signal check run today?
eod_summary_sent = False   # Has end-of-day summary been sent?
eod_summary_retry = False  # Did EOD summary fail? Retry once
last_exit_check = 0        # Timestamp of last exit condition check (every 5 min)
```

**Main Loop Flow**:
```
1. Timezone Verification
   - Log: "Timezone: EST (verified)" or "Timezone: EDT (verified)"
   - Confirms Railway environment is using US/Eastern

2. Load and Validate Config
   - Load strategy JSON
   - Load environment variables
   - Override with command-line args (--dry-run, --live)
   - Validate all configs
   - Exit with code 1 if validation fails (Railway will restart)

3. Initialize Subsystems
   - Broker (AlpacaBroker)
   - Market data (MarketDataFetcher)
   - Signal engine (SignalEngine)
   - Risk manager (RiskManager)
   - Order manager (OrderManager)
   - Notifier (TelegramNotifier)
   - Exit with code 1 if broker connection fails (Railway will restart)

4. Send Startup Message
   - Mode (DRY RUN / PAPER / 🔴 LIVE)
   - Strategy name and ticker
   - Risk parameters (SL, TP)
   - Backtest Sharpe ratio
   - Platform (Railway 24/7)

5. Register SIGTERM Handler
   - Railway sends SIGTERM on restart/stop
   - Handler sends shutdown notification with final stats
   - Exits with code 0 (graceful shutdown)

6. Main Loop (while True):
   a. New Day Reset (if date changed)
      - Reset state flags (morning_check_done, eod_summary_sent, etc.)
      - Call risk_manager.reset_daily()
      - Call order_manager.reset_daily()
      - Log: "=== New trading day: YYYY-MM-DD (Day Name) ==="

   b. Market Closed Checks (sleep longer)
      - Weekend (Sat/Sun): Sleep 30 minutes
      - Pre-market (<9:30 AM ET): Sleep 5 minutes
      - After hours (≥4:00 PM ET): Send EOD summary, sleep 30 minutes
      - Holiday or other: Sleep 5 minutes

   c. Market Open — Morning Signal Check (9:35 AM ET, once per day)
      - Corporate Action Detection:
        * If price moved >20% overnight → Skip signal check
        * Log CRITICAL: "⚠️ SPLIT DETECTED"
        * Send Telegram alert
        * Prevents false breakout/crash signals
      - Fetch 200 bars of historical data
      - Generate signal via signal_engine
      - If BUY/SELL:
        * Get current price
        * Get account equity
        * Count positions (strategy + portfolio totals)
        * Execute via order_manager
        * Send Telegram notification if successful
      - Set morning_check_done = True

   d. Exit Condition Checks (every 5 minutes during market hours)
      - Get all open positions from broker
      - Get current prices for each position
      - Call order_manager.check_exits(positions, prices)
      - For each exit signal:
        * Log: "EXIT: {ticker} - {reason}"
        * Close position via order_manager
        * Send Telegram notification with P&L

   e. End of Day Summary (3:55 PM ET)
      - Calculate daily stats (trades, P&L, win rate)
      - Get account equity
      - Send via notifier.send_daily_summary()
      - Retry Logic:
        * Set eod_summary_sent = True BEFORE attempting
        * If fails and not eod_summary_retry: set retry flag, clear sent flag
        * On next loop: retry once, then give up
        * Prevents infinite retry loops

   f. Sleep 30 seconds
      - Why 30s? (not 5 min like exit checks):
        1. SIGTERM responsiveness: Railway deploy triggers SIGTERM,
           want to catch within 30s (not wait 5 min)
        2. Time-sensitive checks: morning (9:35 AM), EOD (3:55 PM)
           need ~30s precision
        3. Exit checks use last_exit_check guard (enforced independently)

   g. Exception Handling (catch-all)
      - Log error with traceback
      - Send Telegram alert
      - Sleep 60 seconds
      - CONTINUE (never let loop die)
      - Self-healing: any error is caught and loop continues

7. On SIGTERM/Ctrl+C
   - Log: "SIGTERM received — Railway is restarting/stopping"
   - Send shutdown notification with daily stats
   - Exit with code 0
```

**Timing (when each check runs in ET)**:
- **9:35 AM**: Morning signal check (once per day for daily strategies)
- **9:30 AM - 4:00 PM**: Exit checks every 5 minutes (market hours)
- **3:55 PM**: End-of-day summary (once per day)
- **4:00 PM+**: Market closed, send EOD if not sent, sleep 30 min
- **Weekends**: Sleep 30 minutes between checks
- **Pre-market**: Sleep 5 minutes between checks

**Sleep Behavior**:
| Condition | Sleep Duration | Reason |
|-----------|----------------|--------|
| Market hours | 30 seconds | SIGTERM responsiveness + time-sensitive checks |
| Weekend | 30 minutes | Market closed, no trading |
| Pre-market (<9:30 AM) | 5 minutes | Close to open, check more frequently |
| After hours (≥4:00 PM) | 30 minutes | Market closed for the day |
| Holiday/other closure | 5 minutes | May reopen during day |
| Exception caught | 60 seconds | Wait before retry (backoff) |

**SIGTERM Handling (Railway Restarts)**:
```python
def handle_sigterm(signum, frame):
    logger.info("SIGTERM received — Railway is restarting/stopping")
    # Get final stats
    account = broker.get_account()
    summary = {...}
    notifier.send_shutdown_notification(summary)
    sys.exit(0)  # Graceful shutdown

signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)  # Also Ctrl+C for local testing
```

**Railway Restart Behavior**:
- Railway sends SIGTERM ~10 seconds before killing process
- Handler catches SIGTERM within 30 seconds (loop sleep)
- Sends shutdown notification
- Exits gracefully with code 0
- Railway starts new process
- New process loads fresh config, reconnects to broker, continues trading
- **State is NOT persisted across restarts** (future: state file)

**Kill Switch (TRADING_PAUSED env var)**:
- Set `TRADING_PAUSED=true` in Railway Variables tab
- Railway restarts process (~15-30 seconds)
- On restart, `risk_manager.can_trade()` checks env var at top of every call
- Blocks all new entries while TRADING_PAUSED=true
- To resume: Set `TRADING_PAUSED=false` → Railway restarts → trading resumes
- **Not instant (~15-30s)** but reliable emergency brake

**Corporate Action Detection (20% Overnight Move)**:
```python
if len(df) >= 2:
    yesterday_close = df['close'].iloc[-2]
    today_open = df['open'].iloc[-1]
    pct_change = abs((today_open - yesterday_close) / yesterday_close)

    if pct_change > 0.20:  # 20% overnight move
        logger.critical("⚠️ SPLIT DETECTED")
        notifier.send_alert("Corporate action detected - skipping signal")
        morning_check_done = True
        continue  # Skip signal generation
```

**Why This Matters**:
- Stock splits/reverse splits cause large overnight price jumps
- Without detection: strategy sees "massive breakout" or "crash" → false signal
- Detection prevents false entries on split days
- Example: 2-for-1 split → price drops 50% overnight → looks like crash → strategy wants to short → WRONG
- Detection: Skip signal check on split days, wait for next day

**EOD Summary Retry Logic**:
```
First attempt (3:55 PM):
  eod_summary_sent = True  # Set BEFORE attempting
  try:
    send_daily_summary()
  except:
    if not eod_summary_retry:
      eod_summary_retry = True      # Queue one retry
      eod_summary_sent = False      # Allow retry

Next loop iteration:
  if eod_summary_retry and not eod_summary_sent:
    eod_summary_sent = True  # Prevent further retries
    try:
      send_daily_summary()  # Retry once
    except:
      pass  # Give up, don't spam
```

**Why**: Transient network failures shouldn't block EOD summary, but infinite retries would spam Telegram. One retry is enough.

**30-Second Sleep Rationale**:
1. **SIGTERM Responsiveness**: Railway sends SIGTERM on deploy. If we sleep 5 minutes, we wouldn't catch it for up to 5 minutes. With 30s, we catch it within 30s max.
2. **Time-Sensitive Checks**: Morning check (9:35 AM) and EOD summary (3:55 PM) need ~30-second precision. Can't wait 5 minutes and miss the window.
3. **Exit Check Interval**: Enforced by `last_exit_check` timestamp guard, not by sleep duration. Loop runs every 30s, but exit check only runs every 5 minutes.

**Self-Healing Architecture**:
- Catch-all exception handler in main loop
- Log error + traceback
- Send Telegram alert
- Sleep 60 seconds
- **CONTINUE** (never exit)
- Loop keeps running even if subsystems fail
- Railway only restarts on fatal errors (sys.exit(1))

**Fatal Errors (Railway Will Restart)**:
- Config validation failure
- Broker connection failure
- Other errors that make trading impossible

**Non-Fatal Errors (Loop Continues)**:
- Market data fetch failure
- Signal generation error
- Order placement error
- Telegram notification failure
- Exit check error

**Command-Line Interface**:
```bash
# Paper trading (default)
python -m alphalive.main --config configs/ma_crossover.json

# Dry run (log only, no orders)
python -m alphalive.main --config configs/ma_crossover.json --dry-run

# Live trading (DANGEROUS)
python -m alphalive.main --config configs/ma_crossover.json --live
```

**Railway Deployment**:
```bash
# Set in Railway Variables
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
STRATEGY_CONFIG=configs/ma_crossover.json

# Optional
ALPACA_PAPER=true  # Paper trading (default)
DRY_RUN=false      # Actually execute trades (default)
TRADING_PAUSED=false  # Allow trading (default)

# Procfile
worker: python -m alphalive.main --config $STRATEGY_CONFIG
```

**Resilience Features (B9b)**:

AlphaLive includes production-grade resilience features for 24/7 cloud deployment:

1. **Startup Data Backfill + Warmup Validation**:
   - On any restart (including mid-day Railway restarts), fetch 250 bars before first signal check
   - Run test signal to verify indicators are fully warmed up
   - Check `warmup_complete` flag — if False, log warning and send Telegram alert
   - If data is stale on startup (raises `DataStaleError`): exit with code 1, let Railway restart
   - Prevents garbage signals from incomplete indicators after restart

2. **Data Staleness Check on Every Signal Check**:
   - Wrap `market_data.get_latest_bars()` with `try/except DataStaleError`
   - If data is stale: log warning, send Telegram alert, skip signal check (mark as done)
   - Prevents trading on delayed/broken market data feed
   - Staleness thresholds (from B8):
     - 15Min: data older than 5 minutes = STALE
     - 1Hour: data older than 15 minutes = STALE
     - 1Day: data older than 1440 minutes = STALE

3. **Position Reconciliation with AUTO-HALT (every 30 minutes)**:
   - Compare Alpaca positions vs bot's internal tracking (from order_history)
   - If mismatch detected (drift):
     - Log CRITICAL: "🚨 POSITION DRIFT"
     - Send Telegram alert with details
     - **AUTO-HALT trading**: `os.environ["TRADING_PAUSED"] = "true"`
     - Continue exit monitoring for existing positions
   - Drift scenarios:
     - Alpaca has position bot doesn't track (order filled but bot didn't record it)
     - Bot tracks position Alpaca doesn't have (position closed externally or never filled)
   - **Recovery**: User reviews positions in Alpaca dashboard, verifies correctness, sets `TRADING_PAUSED=false` in Railway

4. **Multi-Strategy Coordination**:
   - State tracking uses `set()` instead of `bool`: `morning_checks_done = set()`
   - Iterate `all_strategy_configs` in signal check section
   - Each strategy tracked independently: `morning_checks_done.add(ticker)`
   - Signal engine map: `signal_engine_map = {strategy_name: signal_engine}`
   - Default: single strategy mode (`all_strategy_configs = [strategy_config]`)
   - Future: load multiple strategies from directory

5. **Timeframe-Aware Signal Check Gating**:
   - Helper function: `should_run_signal_check(timeframe, last_check_time)`
   - **1Day**: Use `morning_checks_done` set (once per day after 9:35 AM)
   - **1Hour**: Check every hour at bar boundaries (10:00, 11:00, 12:00...)
   - **15Min**: Check every 15 minutes at bar boundaries (9:30, 9:45, 10:00, 10:15...)
   - Alignment to bar boundaries: `now.minute % interval_minutes != 0` → skip
   - Timing slop: -35 seconds to account for clock drift
   - Tracking: `last_signal_check_map = {ticker: timestamp}`

6. **DST Awareness**:
   - Uses `zoneinfo.ZoneInfo("America/New_York")` for ET timezone
   - DST transitions (March, November) handled automatically by OS timezone database
   - On startup, log: `datetime.now(ET).tzname()` → "EST" or "EDT" (verified)
   - **Edge case**: If Railway container started before DST transition, 9:35 AM check could fire at wrong wall-clock time until restart
   - **Mitigation**: Restart Railway service manually on DST transition days (2x/year)

**Position Reconciliation Example**:
```
[12:30 PM] Position reconciliation check:
- Alpaca: AAPL 100 shares @ $150.00
- Bot internal tracking: No record of AAPL position

[CRITICAL] 🚨 POSITION DRIFT: Alpaca has AAPL but bot has no record
[ACTION] AUTO-HALT trading: TRADING_PAUSED=true (current process)
[TELEGRAM] Alert sent: "Position drift detected - trading paused"

[USER ACTION REQUIRED]
1. Check Alpaca dashboard: verify AAPL position exists
2. Check bot logs: look for failed order placement or network error
3. Determine cause: order filled but bot didn't record? Or external fill?
4. If position is valid: Set TRADING_PAUSED=false in Railway
5. If position is invalid: Close position manually in Alpaca, then TRADING_PAUSED=false
```

**Timeframe-Aware Signal Gating Example**:
```python
# 1Day strategy (AAPL)
if timeframe == "1Day":
    should_check = "AAPL" not in morning_checks_done
    # Checks once per day after 9:35 AM

# 1Hour strategy (SPY)
if timeframe == "1Hour":
    should_check = should_run_signal_check("1Hour", last_signal_check_map.get("SPY", 0))
    # Checks at 10:00, 11:00, 12:00, 1:00, 2:00, 3:00
    # (every hour on the hour, if 60 minutes have passed since last check)

# 15Min strategy (QQQ)
if timeframe == "15Min":
    should_check = should_run_signal_check("15Min", last_signal_check_map.get("QQQ", 0))
    # Checks at 9:30, 9:45, 10:00, 10:15, ... 3:45
    # (every 15 minutes, aligned to bar boundaries)
```

**Important**: These resilience features handle edge cases in 24/7 deployment:
- **Mid-day restarts**: Warmup validation prevents garbage signals
- **Data feed delays**: Staleness checks prevent trading on old data
- **Position drift**: Reconciliation catches tracking failures, auto-halts to prevent compounding
- **Multi-strategy**: Coordination prevents signal conflicts
- **Intraday strategies**: Timeframe-aware gating ensures correct signal timing
- **DST transitions**: Timezone awareness handles spring/fall clock changes

#### 2. Configuration (`config.py`)

**Purpose**: Load and validate strategy configurations.

**Key Functions**:
- `load_config(path)`: Load strategy JSON, apply migrations, validate with Pydantic
- `validate_environment_variables()`: Ensure required env vars are set
- `get_config_from_env()`: Get strategy path from STRATEGY_CONFIG env var

**Usage**:
```python
config = load_config("configs/ma_crossover.json")
# Returns validated StrategySchema instance
```

#### 3. Broker Interface (`broker/`)

**Purpose**: Abstract broker operations for easy swapping (Alpaca, Interactive Brokers, etc.).

**Location**: `alphalive/broker/`

##### BaseBroker Abstract Class (`base_broker.py`)

**Dataclasses**:
```python
@dataclass
class Position:
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
    id: str
    symbol: str
    qty: float
    side: str  # "buy" or "sell"
    order_type: str  # "market" or "limit"
    limit_price: Optional[float]
    status: str  # "new", "filled", "canceled", etc.
    filled_qty: float
    filled_avg_price: Optional[float]
    submitted_at: datetime
    filled_at: Optional[datetime]

@dataclass
class Account:
    equity: float
    cash: float
    buying_power: float
    portfolio_value: float
    long_market_value: float
    short_market_value: float
    daytrade_count: int
    pattern_day_trader: bool
    account_status: str  # "ACTIVE", "CLOSED", etc.
```

**Custom Exceptions**:
- `BrokerError`: Base exception for broker errors
- `AuthenticationError`: Invalid credentials (401/403)
- `RateLimitError`: API rate limit exceeded (429)
- `OrderError`: Order placement/management failure

**Abstract Methods** (all implementations must provide):

```python
connect() -> bool
    """Authenticate and verify credentials."""
    - Test API credentials
    - Print account status
    - Return True if connected

get_account() -> Account
    """Get current account information."""
    - Returns equity, buying_power, cash, day_trade_count, etc.

get_position(symbol: str) -> Optional[Position]
    """Get position for specific symbol."""
    - Returns Position if exists, None otherwise

get_all_positions() -> List[Position]
    """Get all open positions."""
    - Returns list of Position (empty list if none)

place_market_order(symbol: str, qty: int, side: str) -> Order
    """Place a market order."""
    - side: "buy" or "sell"
    - Returns Order with order details

place_limit_order(symbol: str, qty: int, side: str, limit_price: float) -> Order
    """Place a limit order."""
    - Returns Order with order details

cancel_order(order_id: str) -> bool
    """Cancel a pending order."""
    - Returns True if canceled, False if not found/already filled

get_order_status(order_id: str) -> Optional[Order]
    """Get current status of an order."""
    - Returns Order with current status, None if not found

close_position(symbol: str) -> Order
    """Close entire position using market order."""
    - Returns Order for the closing order

is_market_open() -> bool
    """Check if US stock market is currently open."""
    - Returns True if market open, False otherwise

get_market_hours() -> Dict[str, datetime]
    """Get market hours information."""
    - Returns: is_open, next_open, next_close

get_bars(symbol, timeframe, start, end, limit) -> List[Dict[str, Any]]
    """Get historical OHLCV bars."""
    - Returns list of bars with timestamp, open, high, low, close, volume
```

##### AlpacaBroker Implementation (`alpaca_broker.py`)

**Library**: `alpaca-py` v0.28.0

**Clients**:
- `TradingClient`: Order placement, account management
- `StockHistoricalDataClient`: Market data (bars)

**Initialization**:
```python
broker = AlpacaBroker(
    api_key="...",
    secret_key="...",
    paper=True,  # Paper trading mode
    base_url=None  # Auto-set based on paper flag
)

# Connect and verify
broker.connect()  # Prints account status
```

**Error Handling Strategy**:

1. **Fatal Errors (No Retry)**:
   - `401/403` (Authentication): Raise `AuthenticationError` immediately
   - Invalid parameters: Raise `ValueError` immediately
   - Other API errors: Raise `BrokerError` immediately

2. **Retryable Errors (Exponential Backoff)**:
   - `429` (Rate Limit): Retry with 1s, 2s, 4s delays
   - `5xx` (Server Error): Retry with backoff
   - `ConnectionError`: Retry with backoff
   - Max 3 retries total

3. **Retry Configuration**:
   ```python
   MAX_RETRIES = 3
   INITIAL_RETRY_DELAY = 1.0  # seconds
   # Delay doubles each retry: 1s → 2s → 4s
   ```

4. **Retry Logic** (`_retry_with_backoff`):
   ```python
   for attempt in range(1, MAX_RETRIES + 1):
       try:
           return func(*args, **kwargs)
       except APIError as e:
           if e.status_code in (401, 403):
               raise AuthenticationError(...)  # Don't retry
           elif e.status_code == 429:
               logger.warning(f"Rate limited. Retry {attempt}/{MAX_RETRIES} in {delay}s...")
               time.sleep(delay)
               delay *= 2
           elif e.status_code >= 500:
               logger.warning(f"Server error. Retry {attempt}/{MAX_RETRIES} in {delay}s...")
               time.sleep(delay)
               delay *= 2
   ```

**Logging Examples**:
```
INFO: Connecting to Alpaca...
INFO: ============================================================
INFO: ALPACA CONNECTION SUCCESSFUL
INFO: ============================================================
INFO: Account Status: ACTIVE
INFO: Equity: $100,000.00
INFO: Cash: $50,000.00
INFO: Buying Power: $200,000.00
INFO: Portfolio Value: $100,000.00
INFO: Day Trade Count: 0
INFO: Pattern Day Trader: False
INFO: ============================================================

INFO: MARKET BUY 10 AAPL @ market | Order ID: abc123-def456

WARNING: Alpaca rate limited (429). Retry 2/3 in 2.0s...

ERROR: Authentication error (fatal): Invalid API key
```

**Timeframe Mapping**:
```python
TIMEFRAME_MAP = {
    "1Min": TimeFrame.Minute,
    "5Min": TimeFrame(5, "Min"),
    "15Min": TimeFrame(15, "Min"),
    "1Hour": TimeFrame.Hour,
    "1Day": TimeFrame.Day
}
```

**Usage Examples**:

```python
from alphalive.broker.alpaca_broker import AlpacaBroker

# Initialize and connect
broker = AlpacaBroker(api_key=..., secret_key=..., paper=True)
broker.connect()

# Get account
account = broker.get_account()
print(f"Equity: ${account.equity:,.2f}")

# Place market order
order = broker.place_market_order("AAPL", 10, "buy")
print(f"Order {order.id}: {order.status}")

# Place limit order
order = broker.place_limit_order("AAPL", 10, "sell", 155.50)
print(f"Limit order @ ${order.limit_price}")

# Get position
position = broker.get_position("AAPL")
if position:
    print(f"P&L: ${position.unrealized_pl:.2f} ({position.unrealized_plpc:.2f}%)")

# Check market status
if broker.is_market_open():
    print("Market is open")

# Get market hours
hours = broker.get_market_hours()
print(f"Next open: {hours['next_open']}")

# Get bars
bars = broker.get_bars("AAPL", "1Day", limit=30)
for bar in bars[-5:]:  # Last 5 days
    print(f"{bar['timestamp']}: Close ${bar['close']:.2f}")
```

**Parameter Validation**:
- Symbol must be non-empty string
- Quantity must be positive integer
- Side must be "buy" or "sell"
- Limit price must be positive number
- All validated before API call

**Connection State**:
- `_ensure_connected()` checks before every API call
- Raises `BrokerError` if not connected
- Set `self.connected = True` after successful `connect()`

#### 4. Signal Engine (`strategy/signal_engine.py`)

**Purpose**: Generate buy/sell signals from market data with exact AlphaLab parity.

**Location**: `alphalive/strategy/signal_engine.py`

**CRITICAL**: Signal logic must match AlphaLab backtest exactly. Any divergence means live results won't match backtest expectations.

##### Signal Return Format

```python
{
    "signal": "BUY" | "SELL" | "HOLD",
    "confidence": 0.0-1.0,  # float
    "reason": "Short SMA(20) crossed above Long SMA(50)",  # str
    "indicators": {  # dict of indicator values at last bar
        "sma_20": 187.5,
        "sma_50": 183.2,
        "price": 189.3
    },
    "warmup_complete": True,  # bool - False if any required indicator is NaN
    "generation_time_ms": 324  # int - time taken in milliseconds
}
```

**warmup_complete**: Critical for Railway mid-day restarts. If False, not enough historical bars to calculate indicators yet. DO NOT trade on incomplete signals.

##### Performance Budget

**Target**: <5 seconds per strategy (all indicators + signal logic)
**Expected**: <0.5s for 200 bars on Railway's shared vCPU

**Timing Instrumentation**: Every signal generation is timed. Warning logged if >5s:
```
WARNING: ⚠️ Signal generation SLOW: ma_crossover took 6.2s (budget: 5s). Optimize indicators.
```

**Optimization Tips** if you exceed 5s:
- Use vectorized pandas/numpy operations (avoid loops)
- Reduce lookback_bars if >500 (200 is usually sufficient)
- Cache indicators that don't change
- Avoid `apply()`, `iterrows()` - use vectorized ops

##### Supported Strategies (AlphaLab Parity)

**1. MA Crossover** (`ma_crossover`)

**Parameters**:
- `fast_period`: Fast SMA period (default: 10)
- `slow_period`: Slow SMA period (default: 20)

**Logic**:
- **BUY**: Fast SMA crosses above Slow SMA (fast_prev ≤ slow_prev AND fast_curr > slow_curr)
- **SELL**: Fast SMA crosses below Slow SMA (fast_prev ≥ slow_prev AND fast_curr < slow_curr)
- **Confidence**: Based on spread between SMAs as % of price (2% spread = 100% confidence)

**Example Signal**:
```python
{
    "signal": "BUY",
    "confidence": 0.85,
    "reason": "Bullish MA crossover: Fast SMA(10)=187.5 crossed above Slow SMA(20)=183.2",
    "indicators": {"sma_10": 187.5, "sma_20": 183.2, "price": 189.3}
}
```

---

**2. RSI Mean Reversion** (`rsi_mean_reversion`)

**Parameters**:
- `period`: RSI period (default: 14)
- `oversold`: Oversold threshold (default: 30)
- `overbought`: Overbought threshold (default: 70)

**Logic**:
- **BUY**: RSI < oversold threshold
- **SELL**: RSI > overbought threshold
- **Confidence**: How far RSI is from threshold (distance / threshold)

**Example Signal**:
```python
{
    "signal": "BUY",
    "confidence": 0.73,
    "reason": "RSI oversold: RSI(14)=22.0 < 30 (distance: 8.0)",
    "indicators": {"rsi_14": 22.0, "oversold": 30, "overbought": 70}
}
```

---

**3. Momentum Breakout** (`momentum_breakout`)

**Parameters**:
- `lookback`: Lookback period for rolling high (default: 20)
- `surge_pct`: Volume surge multiplier (default: 1.5)
- `atr_period`: ATR period for trailing stop (default: 14)
- `volume_ma_period`: Volume MA period (default: 20)

**Logic**:
- **BUY**: Close > rolling_high(lookback) AND volume > volume_ma * surge_pct
- **SELL**: Trailing stop hit (3x ATR below recent high) - NOT implemented in signal engine, handled by risk manager
- **Confidence**: Based on volume surge magnitude ((surge - surge_pct) / surge_pct)

**Example Signal**:
```python
{
    "signal": "BUY",
    "confidence": 0.67,
    "reason": "Momentum breakout: Price 189.3 > High(20)=185.2 (+2.21%), Volume surge 2.1x (>1.5x)",
    "indicators": {"price": 189.3, "rolling_high": 185.2, "volume_surge": 2.1}
}
```

---

**4. Bollinger Breakout** (`bollinger_breakout`)

**Parameters**:
- `period`: Bollinger Bands period (default: 20)
- `std_dev`: Standard deviation multiplier (default: 2.0)
- `confirmation_bars`: Number of consecutive bars above/below band (default: 2) - **EXACT KEY NAME**
- `volume_ma_period`: Volume MA period (default: 20)

**Logic**:
- **BUY**: Close > BB_upper for `confirmation_bars` consecutive bars AND volume > volume_ma * 1.5
- **SELL**: Close < BB_lower for `confirmation_bars` consecutive bars
- **Confidence**: Based on distance from band (distance % / 2.0)

**PARITY WARNING**: `confirmation_bars` logic must match AlphaLab exactly. AlphaLab evaluates vectorized across DataFrame, AlphaLive evaluates on rolling window. Test case: Verify both agree on exact bar where signal fires after 2 consecutive closes above upper band.

**PARAMETER NAME CONTRACT**: Must use `confirmation_bars` (not `confirm_bars`, not `confirmBars`). This exact key must match in A5 (AlphaLab), B4 (AlphaLive), and all test fixtures.

**Example Signal**:
```python
{
    "signal": "BUY",
    "confidence": 0.92,
    "reason": "Bollinger upper breakout: Price 189.3 > BB_upper=185.0 for 2 bars, Volume surge 1.8x",
    "indicators": {"price": 189.3, "bb_upper": 185.0, "bb_middle": 180.0, "bb_lower": 175.0}
}
```

---

**5. VWAP Reversion** (`vwap_reversion`)

**Parameters**:
- `deviation_threshold`: Deviation in standard deviations (default: 2.0)
- `rsi_period`: RSI period (default: 14)
- `oversold`: RSI oversold threshold (default: 30)
- `overbought`: RSI overbought threshold (default: 70)
- `vwap_std_period`: Period for VWAP std dev calculation (default: 20)

**Logic**:
- **BUY**: Price < VWAP - (deviation_threshold * vwap_std) AND RSI < oversold
- **SELL**: Price > VWAP + (deviation_threshold * vwap_std) AND RSI > overbought
- **Confidence**: Based on deviation magnitude (abs(deviation) / (threshold * 2))

**Example Signal**:
```python
{
    "signal": "BUY",
    "confidence": 0.88,
    "reason": "VWAP oversold reversion: Price 175.0 < VWAP-2σ=180.0, RSI=25.0 < 30, Deviation=-2.5σ",
    "indicators": {"price": 175.0, "vwap": 180.0, "vwap_std": 2.0, "rsi_14": 25.0}
}
```

---

##### Usage Example

```python
from alphalive.strategy.signal_engine import SignalEngine

# Initialize with strategy config
engine = SignalEngine(strategy_config)

# Generate signal from OHLCV DataFrame
signal = engine.generate_signal(bars_df)

# Check warmup
if not signal["warmup_complete"]:
    logger.warning("Indicators not ready yet - need more historical bars")
    return

# Check performance
if signal["generation_time_ms"] > 5000:
    logger.warning("Signal generation too slow!")

# Act on signal
if signal["signal"] == "BUY":
    logger.info(f"BUY signal: {signal['reason']} (confidence: {signal['confidence']:.2%})")
    order_manager.execute_signal(signal)
elif signal["signal"] == "SELL":
    logger.info(f"SELL signal: {signal['reason']}")
    order_manager.close_position(ticker)
```

#### 5. Indicators (`strategy/indicators.py`)

**Purpose**: Calculate technical indicators using `ta` library.

**Location**: `alphalive/strategy/indicators.py`

**Library**: `ta` (Technical Analysis Library in Python)

**Input**: pandas DataFrame with columns: `open`, `high`, `low`, `close`, `volume` (lowercase)

**Output**: Same DataFrame with indicator columns added

**NaN Handling**: Graceful - first N rows will be NaN where insufficient data. Never errors.

##### Available Indicator Functions

**Moving Averages**:
```python
add_sma(df, period) -> DataFrame
    """Adds f"sma_{period}" column"""
    # First (period - 1) rows will be NaN

add_ema(df, period) -> DataFrame
    """Adds f"ema_{period}" column"""
    # First (period - 1) rows will be NaN
```

**Momentum**:
```python
add_rsi(df, period=14) -> DataFrame
    """Adds f"rsi_{period}" column (0-100 range)"""
    # First (period) rows will be NaN

add_macd(df, fast=12, slow=26, signal=9) -> DataFrame
    """Adds "macd", "macd_signal", "macd_hist" columns"""
    # First (slow + signal - 1) rows will be NaN
```

**Volatility**:
```python
add_bollinger(df, period=20, std_dev=2.0) -> DataFrame
    """Adds "bb_upper", "bb_middle", "bb_lower" columns"""
    # First (period - 1) rows will be NaN

add_atr(df, period=14) -> DataFrame
    """Adds f"atr_{period}" column"""
    # First (period) rows will be NaN
```

**Trend**:
```python
add_adx(df, period=14) -> DataFrame
    """Adds f"adx_{period}" column (0-100 range)"""
    # First (period * 2) rows will be NaN
```

**Volume**:
```python
add_vwap(df) -> DataFrame
    """Adds "vwap" column"""
    # VWAP = cumsum(typical_price * volume) / cumsum(volume)
    # Cumulative from start, first row may be NaN if volume=0

add_obv(df) -> DataFrame
    """Adds "obv" (On-Balance Volume) column"""
    # Cumulative from start
```

**Strategy-Specific** (Main Function):
```python
add_all_for_strategy(df, strategy_name, params) -> DataFrame
    """
    Adds only indicators needed for specific strategy.
    This is the main function to call - minimizes computation.

    Supported strategies:
    - "ma_crossover": SMA(fast), SMA(slow)
    - "rsi_mean_reversion": RSI(period)
    - "momentum_breakout": ATR, rolling_high, volume_ma
    - "bollinger_breakout": BB, volume_ma
    - "vwap_reversion": VWAP, RSI, vwap_std

    Raises ValueError if strategy_name unknown.
    """
```

##### Usage Examples

**Single Indicator**:
```python
import pandas as pd
from alphalive.strategy.indicators import add_sma, add_rsi

# Add SMA
df = add_sma(df, period=20)
print(df['sma_20'].tail())

# Add RSI
df = add_rsi(df, period=14)
print(df['rsi_14'].tail())
```

**Strategy-Specific Indicators** (Recommended):
```python
from alphalive.strategy.indicators import add_all_for_strategy

# Add only what MA crossover needs
df = add_all_for_strategy(df, "ma_crossover", {"fast_period": 10, "slow_period": 20})
# Now df has: sma_10, sma_20

# Add only what RSI mean reversion needs
df = add_all_for_strategy(df, "rsi_mean_reversion", {"period": 14})
# Now df has: rsi_14
```

**Performance**: Each function uses vectorized pandas/numpy operations. Expected <0.3s for 200 bars.

#### 6. Risk Manager (`execution/risk_manager.py`)

**Purpose**: The gatekeeper — nothing trades without its approval. Enforces risk limits, stop loss/take profit, daily limits, and circuit breakers.

**Architecture**: Two classes:
- `RiskManager`: Per-strategy risk management
- `GlobalRiskManager`: Multi-strategy risk aggregation

#### RiskManager (Per-Strategy)

**Initialization**:
```python
risk_manager = RiskManager(
    risk_config=strategy.risk,
    execution_config=strategy.execution,
    strategy_name="ma_crossover",
    safety_limits=strategy.safety_limits,  # NEW in B17
    notifier=notifier  # Optional, for alerts
)
```

**Key Methods**:

1. **`can_trade(ticker, signal, account_equity, current_positions_count, total_portfolio_positions, current_bar=None) -> Tuple[bool, str]`**
   - **Main gatekeeper** - checks ALL limits before allowing a trade
   - Check order:
     1. `TRADING_PAUSED` env var (kill switch — checked first, always)
     2. Manual pause via Telegram /pause command (in-memory flag)
     3. **Trade frequency limit (max trades per day) - B17**
     4. **API call budget limit (max calls per hour) - B17**
     5. **Degraded mode (broker connection unstable) - B17**
     6. Daily loss limit (per-strategy)
     7. Consecutive loss circuit breaker (3 stop-outs = pause for day)
     8. Max positions limit (per-strategy)
     9. Portfolio max positions limit (across ALL strategies)
     10. Cooldown period (if current_bar provided)
   - Returns: `(True, "OK")` or `(False, "reason")`

2. **`calculate_position_size(ticker, signal, current_price, account_equity) -> int`**
   - Formula: `max_dollars = account_equity * max_position_size_pct / 100`
   - `shares = floor(max_dollars / current_price)`
   - Returns: Number of shares (0 if invalid)

3. **`check_stop_loss(entry_price, current_price, side) -> bool`**
   - Long: Trigger if `current_price <= entry_price * (1 - stop_loss_pct/100)`
   - Short: Trigger if `current_price >= entry_price * (1 + stop_loss_pct/100)`

4. **`check_take_profit(entry_price, current_price, side) -> bool`**
   - Long: Trigger if `current_price >= entry_price * (1 + take_profit_pct/100)`
   - Short: Trigger if `current_price <= entry_price * (1 - take_profit_pct/100)`

5. **`check_trailing_stop(entry_price, highest_since_entry, current_price, side) -> bool`**
   - Only active if `trailing_stop_enabled=True` in config
   - Long: Trigger if `current_price <= highest_since_entry * (1 - trailing_stop_pct/100)`
   - Short: Trigger if `current_price >= lowest_since_entry * (1 + trailing_stop_pct/100)`

6. **`record_trade(ticker, pnl, current_bar=None) -> None`**
   - Records completed trade's P&L
   - Updates `daily_pnl` (cumulative)
   - Tracks `consecutive_losses` counter:
     - Loss: increment counter
     - Win: reset counter to 0
   - Circuit breaker: If `consecutive_losses >= 3`, set `trading_paused_by_circuit_breaker = True`
   - Updates `last_trade_bar[ticker]` for cooldown tracking

7. **`reset_daily() -> None`**
   - Call at start of each trading day
   - Resets: `daily_pnl`, `daily_trades`, `consecutive_losses`, `trading_paused_by_circuit_breaker`

#### GlobalRiskManager (Multi-Strategy)

**Purpose**: Tracks cross-strategy metrics and enforces portfolio-level limits.

**Initialization**:
```python
global_risk = GlobalRiskManager()
global_risk.register_strategy("ma_crossover", risk_manager_1)
global_risk.register_strategy("rsi_reversion", risk_manager_2)
```

**Key Methods**:

1. **`check_global_daily_loss(account_equity, max_daily_loss_pct) -> Tuple[bool, str]`**
   - Sums `daily_pnl` across ALL registered strategies
   - Compares total P&L against global limit
   - If exceeded: ALL strategies halt for the day
   - Returns: `(True, "OK")` or `(False, "reason")`

2. **`register_strategy(strategy_name, risk_manager) -> None`**
   - Register a strategy's RiskManager for global tracking

3. **`record_trade(strategy_name, pnl) -> None`**
   - Record trade for global statistics

4. **`is_trading_halted() -> bool`**
   - Check if global halt is active

#### Kill Switch (TRADING_PAUSED)

**Purpose**: Emergency brake to halt all new entries from Railway dashboard.

**Implementation**:
- Checked at the **TOP** of `can_trade()` (before all other checks)
- Reads `os.environ.get("TRADING_PAUSED", "false")`
- Triggers if value is `"true"`, `"1"`, or `"yes"`

**How It Works**:
1. Set `TRADING_PAUSED=true` in Railway Variables tab
2. Railway **restarts the process** (~15-30 seconds)
3. On restart, bot reads `TRADING_PAUSED=true` and blocks all new entries
4. To resume: Set `TRADING_PAUSED=false` → Railway restarts again → trading resumes

**Important**: Not instant (requires process restart), but reliable.

#### Consecutive Loss Circuit Breaker

**Purpose**: Automatically pause trading after streak of losses.

**Behavior**:
- Tracks `consecutive_losses` counter per strategy
- Loss: increment counter
- Win: reset counter to 0
- If `consecutive_losses >= 3`:
  - Set `trading_paused_by_circuit_breaker = True`
  - Block all new entries for rest of the day
  - Log CRITICAL warning
  - Caller should send Telegram alert: "⚠️ 3 consecutive losses — trading paused for the rest of the day"

#### Daily Reset

**Trigger**: Automatic at start of each new trading day.

**Resets**:
- `daily_pnl = 0.0`
- `daily_trades = []`
- `consecutive_losses = 0`
- `trading_paused_by_circuit_breaker = False`
- `trades_today = 0` **(NEW in B17)**

**Usage**:
```python
# Initialize
risk_manager = RiskManager(strategy.risk, strategy.execution, "ma_crossover")

# Check if trade is allowed
can_trade, reason = risk_manager.can_trade(
    ticker="AAPL",
    signal="BUY",
    account_equity=100000.0,
    current_positions_count=2,
    total_portfolio_positions=5,
    current_bar=150
)

if not can_trade:
    logger.warning(f"Trade blocked: {reason}")
    return

# Calculate position size
shares = risk_manager.calculate_position_size("AAPL", "BUY", 150.0, 100000.0)

# Execute trade...

# Record completed trade
risk_manager.record_trade("AAPL", pnl=-125.50, current_bar=151)

# Check exit conditions
if risk_manager.check_stop_loss(150.0, 147.0, "long"):
    # Close position
    pass
```

#### Cost Safety Limits (B17)

**Purpose**: Prevent runaway trading costs from bugs, infinite loops, or market anomalies.

**Configuration**: All limits are configurable per-strategy via the `safety_limits` block in the strategy JSON (added in schema v1.0).

**Default Values**:
```python
safety_limits = {
    "max_trades_per_day": 20,
    "max_api_calls_per_hour": 500,
    "signal_generation_timeout_seconds": 5.0,
    "broker_degraded_mode_threshold_failures": 3
}
```

**Key Safety Features**:

1. **Trade Frequency Limit** (`max_trades_per_day`):
   - Hard limit on trades per day (default: 20)
   - Prevents runaway signal generation bugs
   - **Auto-pause behavior**: When limit hit:
     - Sets `trading_paused_manual = True` (instant halt)
     - Logs CRITICAL warning
     - Sends Telegram alert: "🚨 EMERGENCY HALT - Max trades per day reached"
     - Requires manual resume via `/resume` command or `TRADING_PAUSED=false`

2. **API Call Budget** (`max_api_calls_per_hour`):
   - Hard limit on broker API calls per hour (default: 500)
   - Protects against Alpaca rate limits (200 req/min)
   - Hourly counter resets at top of each hour
   - **Soft warning at 80%**: Logs warning when 80% of budget consumed
   - **Auto-pause at 100%**: Same behavior as trade frequency limit
   - Usage:
     ```python
     risk_manager.record_api_call("get_latest_bars")  # Call before each API request
     ```

3. **Signal Generation Timeout** (`signal_generation_timeout_seconds`):
   - Timeout for strategy signal generation (default: 5.0s)
   - Prevents blocking main loop with slow indicators
   - Implementation in main.py:
     ```python
     import signal as signal_module

     def timeout_handler(signum, frame):
         raise TimeoutError("Signal generation exceeded timeout")

     signal_module.signal(signal_module.SIGALRM, timeout_handler)
     signal_module.alarm(int(risk_manager.signal_timeout_seconds))

     try:
         signal_result = signal_engine.generate_signal(df)
     except TimeoutError:
         logger.error(f"Signal timeout ({risk_manager.signal_timeout_seconds}s)")
         # Skip this cycle, try again next time
     finally:
         signal_module.alarm(0)  # Cancel alarm
     ```

4. **Broker Degraded Mode** (`broker_degraded_mode_threshold_failures`):
   - Enters degraded mode after N consecutive broker failures (default: 3)
   - **Degraded mode behavior**:
     - Blocks all new entries
     - Allows exits on best effort (using cached data)
     - Logs CRITICAL warning
     - Sends Telegram alert: "⚠️ DEGRADED MODE - Broker connection unstable"
   - **Exit degraded mode**: Automatic when broker call succeeds
   - Usage:
     ```python
     try:
         risk_manager.record_api_call("get_account")
         account = broker.get_account()
         risk_manager.record_broker_success()
         return account
     except Exception as e:
         risk_manager.record_broker_failure(e)
         raise
     ```

**Key Methods** (added in B17):

- **`record_api_call(endpoint: str) -> None`**
  - Track API calls for rate limit protection
  - Auto-resets hourly counter at top of each hour

- **`record_broker_failure(error: Exception) -> None`**
  - Track consecutive broker failures
  - Enters degraded mode at threshold

- **`record_broker_success() -> None`**
  - Reset failure counter on successful call
  - Exit degraded mode if currently degraded

- **`enter_degraded_mode(reason: str) -> None`**
  - Enter degraded mode (no new entries)
  - Send Telegram alert

- **`exit_degraded_mode() -> None`**
  - Exit degraded mode (resume normal trading)
  - Send Telegram alert

- **`get_safety_stats() -> Dict`**
  - Get current safety statistics for monitoring
  - Returns: trades_today, api_calls_this_hour, degraded_mode, etc.

**Monitoring Integration**:

Health endpoint (B13b) includes safety stats:
```json
{
  "safety_limits": {
    "trades_today": 8,
    "max_trades_per_day": 20,
    "api_calls_this_hour": 67,
    "max_api_calls_per_hour": 500,
    "degraded_mode": false,
    "broker_consecutive_failures": 0
  }
}
```

**Updated can_trade() Check Order** (with B17 additions):

1. `TRADING_PAUSED` env var (kill switch)
2. Manual pause via Telegram /pause command
3. **Trade frequency limit (NEW)**
4. **API call budget limit (NEW)**
5. **Degraded mode check (NEW)**
6. Daily loss limit
7. Consecutive loss circuit breaker
8. Max positions (per-strategy)
9. Portfolio max positions (global)
10. Cooldown period

**Example Configuration**:

```json
{
  "schema_version": "1.0",
  "strategy": {...},
  "ticker": "AAPL",
  "risk": {...},
  "safety_limits": {
    "max_trades_per_day": 15,
    "max_api_calls_per_hour": 400,
    "signal_generation_timeout_seconds": 5.0,
    "broker_degraded_mode_threshold_failures": 3
  }
}
```

If `safety_limits` block is missing, defaults are applied automatically via Pydantic.

#### 7. Order Manager (`execution/order_manager.py`)

**Purpose**: Wraps the broker and adds robust order execution with retry logic, duplicate prevention, slippage checks, and partial fill handling.

**Key Features**:
- Order placement with exponential backoff retry
- Duplicate order prevention (60s window)
- Idempotency keys (via client_order_id)
- Slippage detection and alerts
- Partial fill handling
- Specific Alpaca error handling
- Exit condition checking (stop loss, take profit, trailing stop)

#### Key Methods

**1. `execute_signal(ticker, signal, current_price, account_equity, current_positions_count, total_portfolio_positions, current_bar=None) -> Dict`**

Main entry point for executing trading signals.

**Flow**:
1. **Risk Check**: Call `risk_manager.can_trade()` with all limits
2. **Duplicate Check**: Prevent duplicate orders within 60s
3. **Idempotency Key**: Generate unique client_order_id
4. **Position Sizing**: Calculate shares via `risk_manager.calculate_position_size()`
5. **Order Placement**: Place market or limit order with retry logic
6. **Slippage Check**: Compare expected vs actual cost (warn if >1%)
7. **Partial Fill Handling**: Log and alert if filled < requested
8. **Recording**: Add to order_history for tracking

**Returns**:
```python
{
    "status": "success" | "blocked" | "error",
    "reason": "...",
    "order_id": "..." (if success),
    "filled_qty": int,
    "filled_price": float,
    "slippage_pct": float (if success)
}
```

**Example**:
```python
result = order_manager.execute_signal(
    ticker="AAPL",
    signal={"signal": "BUY", "confidence": 0.8, "reason": "MA crossover"},
    current_price=150.0,
    account_equity=100000.0,
    current_positions_count=2,
    total_portfolio_positions=5,
    current_bar=100
)

if result["status"] == "success":
    logger.info(f"Order placed: {result['order_id']}")
elif result["status"] == "blocked":
    logger.warning(f"Trade blocked: {result['reason']}")
```

**2. `_place_with_retry(order_func, ticker, max_retries=3)`**

Places order with exponential backoff retry and specific error handling.

**Error Handling**:
- **403 "insufficient buying power"**: No retry, alert user
- **422 "market is closed"**: No retry, critical bug alert
- **403 "symbol not found"**: No retry, halt bot, set TRADING_PAUSED=true
- **429 "rate limited"**: Retry with 4s, 8s, 16s backoff
- **Network/timeout errors**: Retry with 2s, 4s, 8s backoff
- **400 "client_order_id already exists"**: Idempotency success (not an error)
- **Unknown errors**: Retry with exponential backoff

**Example**:
```python
result = self._place_with_retry(
    lambda: self.broker.place_market_order(ticker, qty, "buy"),
    ticker="AAPL",
    max_retries=3
)
```

**3. `_check_recent_order(ticker, side) -> Optional[Dict]`**

Duplicate order prevention - checks if order was placed in last 60s.

**Purpose**:
- Prevent duplicate orders from rapid signal fires
- Protect against bot restart mid-execution
- Guard against network double-submit

**Returns**: `{"order_id": "...", "timestamp": datetime, "age_seconds": float}` or `None`

**4. `_generate_idempotency_key(ticker, side, signal_timestamp) -> str`**

Generates unique idempotency key for Alpaca client_order_id.

**Format**: `{ticker}_{side}_{YYYYMMDD}_{HHMMSS}`
**Example**: `AAPL_buy_20260305_093500`

**Behavior**: Alpaca rejects duplicate client_order_id within same trading day, making orders idempotent across bot restarts.

**5. `check_exits(positions, current_prices) -> List[Dict]`**

Check all open positions for exit conditions.

**Checks**:
- Stop loss (via `risk_manager.check_stop_loss()`)
- Take profit (via `risk_manager.check_take_profit()`)
- Trailing stop (via `risk_manager.check_trailing_stop()` if enabled)

**Returns**: List of exit signals
```python
[
    {"ticker": "AAPL", "reason": "Stop loss hit (entry $150.00, now $147.00)", "current_price": 147.0},
    ...
]
```

**Example**:
```python
positions = [
    {"ticker": "AAPL", "avg_entry": 150.0, "side": "long", "highest_since_entry": 155.0},
    {"ticker": "MSFT", "avg_entry": 300.0, "side": "long"}
]

current_prices = {"AAPL": 147.0, "MSFT": 315.0}

exits = order_manager.check_exits(positions, current_prices)

for exit in exits:
    logger.info(f"Exit signal: {exit['ticker']} - {exit['reason']}")
    order_manager.close_position(exit['ticker'], exit['reason'])
```

**6. `close_position(ticker, reason) -> Dict`**

Close entire position via broker.

**Returns**: `{"status": "success" | "error", "order_id": "...", "reason": "..."}`

**7. `_calculate_limit_price(current_price, side, offset_pct) -> float`**

Calculate limit price with offset.

- **BUY**: `current_price * (1 + offset_pct / 100)` (willing to pay slightly more)
- **SELL**: `current_price * (1 - offset_pct / 100)` (willing to accept slightly less)

#### Slippage Detection

**Threshold**: >1% slippage triggers warning and Telegram alert

**Calculation**:
```python
expected_cost = current_price * qty
actual_cost = filled_price * filled_qty
slippage_pct = abs(actual_cost - expected_cost) / expected_cost * 100
```

**Alert**: If `slippage_pct > 1.0%`:
- Log WARNING
- Send Telegram: "⚠️ High slippage: AAPL BUY (1.5% slippage)"

#### Partial Fill Handling

**Policy**: Accept partial fills, do not retry to fill the rest

**Rationale**: Partial fills are rare on liquid stocks. Retrying could result in overexposure if the first order completes late.

**Behavior**:
- Log WARNING: "PARTIAL FILL: AAPL BUY — requested 100, filled 95"
- Send Telegram: "📊 Partial fill: AAPL 95/100 shares filled"
- Record filled quantity in order_history

#### Duplicate Prevention

**Window**: 60 seconds

**Check**: Before placing order, scan `order_history` in reverse (most recent first) for same ticker+side

**If found**:
- Log WARNING: "Duplicate order prevented: AAPL BUY — already placed order 123 at 2026-03-05 09:35:00"
- Return: `{"status": "blocked", "reason": "Duplicate prevention: order placed 30s ago"}`

#### Idempotency Keys

**Purpose**: Prevent duplicate orders across bot restarts

**Implementation**:
1. Generate key: `_generate_idempotency_key(ticker, side, timestamp)`
2. Pass as `client_order_id` to broker
3. Alpaca rejects duplicate client_order_id → idempotent

**Format**: `AAPL_buy_20260305_093500`

**Behavior**: If bot restarts mid-signal and tries to place same order again, Alpaca returns error "client_order_id already exists" → interpreted as success

#### Dry Run Mode

**Purpose**: Test strategies without real trades

**Usage**:
```python
order_manager = OrderManager(broker, risk_manager, config, dry_run=True)
```

**Behavior**:
- All signals are logged: `[DRY RUN] Would execute: BUY 66 AAPL @ $150.00`
- No actual orders placed
- Returns success with fake order_id: `DRY_RUN_{idempotency_key}`

#### Daily Reset

**Method**: `reset_daily()`

**Call**: At start of each trading day

**Resets**:
- `order_history = []`
- `pending_orders = {}`

**Usage**:
```python
order_manager = OrderManager(broker, risk_manager, config, notifier)

# Execute signal
result = order_manager.execute_signal(...)

# Check exits
exits = order_manager.check_exits(positions, current_prices)

# Close positions
for exit in exits:
    order_manager.close_position(exit['ticker'], exit['reason'])

# Daily reset
order_manager.reset_daily()
```

#### 8. Market Data (`data/market_data.py`)

**Purpose**: Fetch historical and real-time market data from Alpaca with caching, validation, and rate limit handling.

**Implementation**: Uses `alpaca-py` library (`StockHistoricalDataClient`) to fetch OHLCV bars directly from Alpaca's data API.

**Key Features**:
- **Caching**: 5-minute TTL for intraday data to reduce API calls
- **Data Validation**: Checks for staleness, missing bars, NaN values, zero volume
- **Rate Limiting**: Automatic retry with exponential backoff for 429 and 5xx errors
- **Timeframe Support**: 1Day, 1Hour, 15Min

**Class: MarketDataFetcher**

```python
from alphalive.data.market_data import MarketDataFetcher

fetcher = MarketDataFetcher(api_key="...", secret_key="...")
```

**Methods**:

1. **`get_latest_bars(ticker, timeframe, lookback_bars=200)`**
   - Fetches the most recent N bars for a ticker
   - **Args**:
     - `ticker`: Stock symbol (e.g., "AAPL")
     - `timeframe`: "1Day" | "1Hour" | "15Min"
     - `lookback_bars`: Number of bars to fetch (default 200)
   - **Returns**: pandas DataFrame with timezone-aware index (US/Eastern)
     - Columns: `open`, `high`, `low`, `close`, `volume`
     - Index: datetime (timezone-aware)
   - **Raises**:
     - `DataStaleError`: If data is too old for the timeframe
     - `ValueError`: If insufficient bars or missing data

2. **`get_current_price(ticker)`**
   - Fetches the most recent trade price for a ticker
   - **Fallback**: Uses cached close price if API fails
   - **Returns**: float (price)
   - **Raises**: Exception if unable to get price from API or cache

3. **`clear_cache(ticker=None)`**
   - Clears cache for a specific ticker or all tickers
   - **Args**: `ticker` (optional) - if None, clears all

**Caching Strategy**:

```python
# Cache structure
cache = {
    "AAPL": {
        "bars": df,              # pandas DataFrame
        "timestamp": datetime,   # When cached
        "timeframe": "1Day"      # Timeframe string
    }
}

# TTL: 5 minutes (300 seconds)
# Cache hit conditions:
# - Ticker exists
# - Timeframe matches
# - Age < 300 seconds
```

**Data Validation Rules**:

1. **Freshness Thresholds** (raises `DataStaleError` if exceeded):
   - `15Min` strategies: Data older than **5 minutes** = STALE
   - `1Hour` strategies: Data older than **15 minutes** = STALE
   - `1Day` strategies: Data older than **1440 minutes (1 day)** = STALE
   - For daily data during market hours (9 AM - 4 PM ET, weekdays), data must be from today

2. **Minimum Bars**:
   - **Raises error** if fewer than 20 bars (insufficient for indicator warmup)
   - **Logs warning** if fewer than 200 bars (recommended minimum)

3. **Data Quality Checks** (logs warnings):
   - NaN values in OHLCV columns
   - Zero volume bars (suspicious for liquid stocks)
   - Missing required columns

**Rate Limiting**:

Alpaca has rate limits: **200 requests/min** for data endpoints.

**Retry Logic** (`_fetch_with_retry`):
- **429 (Rate Limited)**:
  - Reads `Retry-After` header (default 5s)
  - Waits specified time before retry
  - Max 3 attempts
- **5xx (Server Error)**:
  - Exponential backoff: 2s, 4s, 8s
  - Max 3 attempts
- **4xx (Client Error, except 429)**:
  - No retry, raises immediately
  - Examples: Invalid symbol, malformed request
- **Other Exceptions**:
  - Exponential backoff: 2s, 4s, 8s
  - Max 3 attempts

**Usage Examples**:

```python
from alphalive.data.market_data import MarketDataFetcher, DataStaleError

# Initialize
fetcher = MarketDataFetcher(
    api_key=os.environ["ALPACA_API_KEY"],
    secret_key=os.environ["ALPACA_SECRET_KEY"]
)

# Fetch daily bars
try:
    bars = fetcher.get_latest_bars("AAPL", "1Day", lookback_bars=200)
    print(f"Fetched {len(bars)} bars")
    print(f"Latest close: ${bars['close'].iloc[-1]:.2f}")
    print(f"Latest bar time: {bars.index[-1]}")
except DataStaleError as e:
    logger.error(f"Data is stale: {e}")
    # Market may be closed, pause trading
except ValueError as e:
    logger.error(f"Insufficient data: {e}")
    # Not enough bars for indicator warmup

# Get current price
try:
    price = fetcher.get_current_price("AAPL")
    print(f"Current price: ${price:.2f}")
except Exception as e:
    logger.error(f"Failed to get price: {e}")

# Clear cache (force fresh fetch)
fetcher.clear_cache("AAPL")  # Clear specific ticker
fetcher.clear_cache()        # Clear all
```

**Timeframe Mapping**:

Strategy timeframes are mapped to Alpaca's `TimeFrame`:
- `"1Day"` → `TimeFrame.Day`
- `"1Hour"` → `TimeFrame.Hour`
- `"15Min"` → `TimeFrame(15, TimeFrameUnit.Minute)`

**Data Format**:

All DataFrames returned have:
- **Index**: `pd.DatetimeIndex` (timezone-aware, US/Eastern)
- **Columns**: `open`, `high`, `low`, `close`, `volume` (lowercase)
- **Order**: Sorted by time (oldest to newest)
- **Size**: Exactly `lookback_bars` rows (or fewer if insufficient data available)

**Error Handling**:

```python
# DataStaleError - data too old
try:
    bars = fetcher.get_latest_bars("AAPL", "15Min")
except DataStaleError as e:
    # Market closed or data feed delayed
    logger.warning(f"Data stale: {e}")
    # Option 1: Skip trading this cycle
    # Option 2: Wait and retry
    # Option 3: Use last known good data (risky!)

# ValueError - insufficient bars
try:
    bars = fetcher.get_latest_bars("NEWIPO", "1Day", lookback_bars=200)
except ValueError as e:
    # New stock, not enough history
    logger.error(f"Insufficient bars: {e}")
    # Option 1: Reduce lookback_bars requirement
    # Option 2: Skip this ticker

# AlpacaAPIError - API errors
from alpaca.common.exceptions import APIError as AlpacaAPIError
try:
    bars = fetcher.get_latest_bars("INVALID", "1Day")
except AlpacaAPIError as e:
    if e.status_code == 404:
        logger.error(f"Symbol not found: INVALID")
    elif e.status_code == 429:
        logger.warning("Rate limited, retry later")
    else:
        logger.error(f"Alpaca API error: {e}")
```

#### 9. Telegram Notifier (`notifications/telegram_bot.py`)

**Purpose**: Send real-time alerts via Telegram with graceful degradation.

**Implementation**: Direct HTTP calls to Telegram Bot API using `httpx`.

**CRITICAL**: Does NOT use `python-telegram-bot` (any version). Calls Telegram Bot API directly via HTTPS POST. This is a deliberate design choice: no library version drift, no sync/async confusion, works forever.

**If you see** `from telegram import Bot` or `from telegram.ext import Application` or `import telegram` **ANYWHERE in this codebase, it is WRONG. Delete it.**

#### Key Features

**1. Retry Logic**
- Max 3 retries with exponential backoff: 1s, 2s, 4s
- Per-message retry (not per-send)
- Continues trading loop even if all retries fail

**2. Graceful Degradation**
- Tracks `consecutive_failures` counter
- After 3 consecutive failures:
  - Set `telegram_offline = True`
  - Log CRITICAL: "🚨 Telegram offline — trading continues but alerts lost"
  - Continue returning False (don't crash)
- Background retry every 10 minutes:
  - Check `time.time() - last_retry_attempt > 600`
  - Attempt one send
  - If success: restore connection, reset failures

**3. Never Crashes Trading Loop**
- All exceptions caught and logged
- Returns `False` on failure, not exception
- Trading continues even if Telegram is completely down

#### Methods

**`send_message(text, parse_mode="HTML") -> bool`**

Core method for sending messages.

**Flow**:
1. Check if enabled (skip if disabled)
2. Check if offline and background retry not due (skip if too soon)
3. Retry loop (3 attempts):
   - POST to `https://api.telegram.org/bot{token}/sendMessage`
   - Payload: `{"chat_id": chat_id, "text": text, "parse_mode": parse_mode}`
   - Timeout: 10 seconds
   - If success: reset failures, restore if offline, return True
   - If fail: sleep with exponential backoff, increment failures
4. If all retries fail: increment consecutive_failures, mark offline if ≥3

**Example**:
```python
success = notifier.send_message("🚀 Bot started", parse_mode="HTML")
if not success:
    logger.warning("Telegram send failed, but trading continues")
```

**`send_startup_notification(strategy_name, ticker, config)`**

Bot started notification with strategy details.

**Example Message**:
```
🚀 AlphaLive Started

Strategy: ma_crossover
Ticker: AAPL
Timeframe: 1Day
Stop Loss: 2.0%
Take Profit: 5.0%
Max Position Size: 10.0%
Max Daily Loss: 3.0%

Bot is now monitoring the market 24/7.
```

**`send_shutdown_notification(daily_stats)`**

Bot stopped notification with daily summary.

**Args**: `daily_stats` dict with keys: `trades`, `pnl`, `win_rate`

**`send_trade_notification(ticker, side, qty, price, reason)`**

Trade executed notification.

**Example Message**:
```
🟢 BUY Signal Executed

Ticker: AAPL
Qty: 66
Price: $150.00
Total: $9900.00
Reason: MA crossover (fast SMA crossed above slow SMA)
```

**`send_position_closed_notification(ticker, qty, entry_price, exit_price, pnl, pnl_pct, reason)`**

Position closed notification (stop loss, take profit, trailing stop).

**Example Message**:
```
💰 Position Closed

Ticker: AAPL
Qty: 66
Entry: $150.00
Exit: $157.50
P&L: $495.00 (+5.00%)
Reason: Take profit hit
```

**`send_error_alert(error_msg)`**

Error alert notification.

**Example Message**:
```
⚠️ AlphaLive Error

Connection timeout to Alpaca API

Check logs for details.
```

**`send_alert(message)`**

Generic alert message.

**Example**:
```python
notifier.send_alert("⚠️ High slippage: AAPL BUY (1.5% slippage)")
notifier.send_alert("📊 Partial fill: AAPL 95/100 shares filled")
notifier.send_alert("⚠️ 3 consecutive losses — trading paused for the day")
```

**`send_daily_summary(stats)`**

End-of-day performance summary.

**Args**: `stats` dict with keys: `trades`, `pnl`, `win_rate`, `start_equity`, `end_equity`

**Example Message**:
```
📈 Daily Summary

Trades: 5
P&L: $450.00
Win Rate: 60.0%
Start Equity: $100000.00
End Equity: $100450.00
```

**`is_offline() -> bool`**

Check if Telegram is currently offline (3+ consecutive failures).

#### Graceful Degradation Behavior

**Normal Operation**:
```
consecutive_failures = 0
telegram_offline = False
→ All messages sent normally
```

**After 1-2 Failures**:
```
consecutive_failures = 1 or 2
telegram_offline = False
→ Log warnings, continue trying
```

**After 3 Failures**:
```
consecutive_failures = 3
telegram_offline = True
→ Log CRITICAL: "🚨 Telegram offline — trading continues but alerts lost"
→ Skip message sends (return False immediately)
→ Start background retry timer
```

**Background Retry (Every 10 Minutes)**:
```
if telegram_offline and (time.time() - last_retry_attempt > 600):
    → Attempt one send
    → If success:
      * telegram_offline = False
      * consecutive_failures = 0
      * Log INFO: "✅ Telegram connection restored"
```

#### Usage Examples

**Initialization**:
```python
notifier = TelegramNotifier(
    bot_token="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz",
    chat_id="123456789",
    enabled=True
)

# Or disabled:
notifier = TelegramNotifier(bot_token=None, chat_id=None)  # enabled = False
```

**Bot Lifecycle**:
```python
# Startup
notifier.send_startup_notification(
    strategy_name="ma_crossover",
    ticker="AAPL",
    config={"timeframe": "1Day", "stop_loss_pct": 2.0, ...}
)

# Trade executed
notifier.send_trade_notification(
    ticker="AAPL",
    side="BUY",
    qty=66,
    price=150.0,
    reason="MA crossover"
)

# Position closed
notifier.send_position_closed_notification(
    ticker="AAPL",
    qty=66,
    entry_price=150.0,
    exit_price=157.5,
    pnl=495.0,
    pnl_pct=5.0,
    reason="Take profit hit"
)

# Error
notifier.send_error_alert("Alpaca API timeout")

# Generic alert
notifier.send_alert("High slippage detected")

# Daily summary
notifier.send_daily_summary({
    "trades": 5,
    "pnl": 450.0,
    "win_rate": 60.0,
    "start_equity": 100000.0,
    "end_equity": 100450.0
})

# Shutdown
notifier.send_shutdown_notification({
    "trades": 5,
    "pnl": 450.0,
    "win_rate": 60.0
})
```

**Checking Status**:
```python
if notifier.is_offline():
    logger.warning("Telegram is offline — no alerts being sent")
```

#### Error Handling

**Never Crashes**:
```python
try:
    response = httpx.post(url, json=payload, timeout=10.0)
    # ... handle response
except Exception as e:
    logger.error(f"Telegram send failed: {e}")
    # Increment failures, return False
    # Trading continues
```

**HTTP Status Codes**:
- `200 OK`: Success, reset failures
- `400 Bad Request`: Invalid chat_id or token (log error, don't retry)
- `401 Unauthorized`: Invalid bot token (log error, don't retry)
- `429 Too Many Requests`: Rate limited (retry with backoff)
- `5xx Server Error`: Telegram server issue (retry with backoff)

#### HTML Formatting

**Supported Tags** (parse_mode="HTML"):
- `<b>bold</b>` - Bold text
- `<i>italic</i>` - Italic text
- `<code>code</code>` - Monospace font
- `<pre>pre</pre>` - Preformatted text block

**Example**:
```python
text = (
    f"🟢 <b>BUY Signal Executed</b>\n\n"
    f"<b>Ticker:</b> AAPL\n"
    f"<code>Price: $150.00</code>"
)
notifier.send_message(text, parse_mode="HTML")
```

#### 10. Telegram Command Listener (`notifications/telegram_commands.py`)

**Purpose**: Poll Telegram for incoming commands on a background thread, allowing remote control of AlphaLive from your phone.

**Location**: `alphalive/notifications/telegram_commands.py`

**Architecture**: Uses Telegram's `getUpdates` polling API (NOT webhooks). Polls every 5 seconds on a daemon thread. Simple and works on Railway without needing a public URL.

**Security**: Only responds to messages from the configured `chat_id`. All other chats are ignored and logged as warnings.

#### TelegramCommandListener Class

**Initialization**:
```python
from alphalive.notifications.telegram_commands import TelegramCommandListener

listener = TelegramCommandListener(
    bot_token="your_bot_token",
    chat_id="123456789",
    order_manager=order_manager,
    risk_manager=risk_manager,
    broker=broker,
    notifier=notifier,
    config=strategy_config
)

listener.start()  # Starts polling in background thread
```

**Key Methods**:

1. **`start()`**
   - Starts polling in background daemon thread
   - Thread name: "TelegramCommandListener"
   - Non-blocking: won't prevent shutdown

2. **`stop()`**
   - Stops polling gracefully
   - Called on SIGTERM

3. **`_poll_loop()`** (internal)
   - Polls `getUpdates` every 5 seconds
   - Uses long polling with 5-second timeout
   - Handles rate limiting, auth errors, network errors

4. **`_handle_command(text)`** (internal)
   - Routes commands to handlers
   - Catches exceptions and sends error messages
   - Suggests `/help` for unknown commands

#### Supported Commands

**1. `/status` — Current Bot State**

**Purpose**: Get real-time status of the bot and positions.

**Response**:
```
📊 AlphaLive Status

Mode: Paper Trading
Strategy: ma_crossover on AAPL
Timeframe: 1Day

Open Positions:
  • AAPL: 10 shares, +1.20% ($+18.00)

Daily P&L: +$145.30
Account Equity: $100,000.00
Buying Power: $200,000.00

Trading Paused: No ▶️
Uptime: 4h 23m
Last Signal: BUY at 09:35 AM
```

**Implementation**: Queries broker for account and positions, formats with HTML.

---

**2. `/pause` — Pause Trading**

**Purpose**: Stop all new entries immediately (in-memory flag).

**Response**:
```
⏸ Trading Paused

No new entries will be placed.
Open positions will still be monitored for exits.

Use /resume to re-enable trading.
```

**Implementation**:
- Sets `risk_manager.trading_paused_manual = True`
- `RiskManager.can_trade()` checks this flag before allowing trades
- Does NOT close existing positions
- Does NOT require Railway restart (instant)

---

**3. `/resume` — Resume Trading**

**Purpose**: Re-enable trading after `/pause`.

**Response**:
```
▶️ Trading Resumed

New signals will be executed.
Circuit breaker and other limits still active.
```

**Implementation**:
- Sets `risk_manager.trading_paused_manual = False`
- Trading resumes immediately

---

**4. `/close_all` — Close All Positions**

**Purpose**: Close all open positions at market (with confirmation).

**Flow**:
1. User sends `/close_all`
2. Bot asks for confirmation:
   ```
   ⚠️ Close ALL Positions?

   This will close:
     • AAPL: 10 shares

   Reply /confirm_close to proceed.
   ```
3. User sends `/confirm_close`
4. Bot closes all positions and reports results:
   ```
   🔴 Positions Closed

   ✅ AAPL: Closed
   ```

**Implementation**:
- Sets `_pending_close_all` flag on first command
- Waits for `/confirm_close` before executing
- Calls `order_manager.close_position()` for each position
- Reports success/failure for each

**Safety**: Two-step confirmation prevents accidental closes.

---

**5. `/config` — View Strategy Configuration**

**Purpose**: View current strategy settings (sanitized, no API keys).

**Response**:
```
⚙️ Strategy Configuration

Strategy: ma_crossover
Ticker: AAPL
Timeframe: 1Day

Risk Management:
  • Stop Loss: 2.0%
  • Take Profit: 5.0%
  • Max Position: 10.0%
  • Max Daily Loss: 3.0%
  • Max Positions: 5
  • Trailing Stop: Off

Execution:
  • Order Type: MARKET
  • Cooldown: 1 bars
```

**Implementation**: Formats `strategy_config` fields with HTML.

---

**6. `/performance` — Performance Stats**

**Purpose**: View performance since bot started.

**Response**:
```
📈 Performance (since Mar 5)

Total Trades: 12 (8W / 4L)
Total P&L: +$892.40 (+0.89%)
Win Rate: 66.7%

Best Trade: AAPL +$340.00 (+2.80%)
Worst Trade: GOOGL -$120.00 (-1.10%)

Consecutive Losses: 0
```

**Implementation**:
- Reads `risk_manager.daily_trades`
- Calculates win/loss stats
- Formats with HTML

---

**7. `/help` — List All Commands**

**Purpose**: Show available commands.

**Response**:
```
🤖 AlphaLive Commands

/status — Current bot state and positions
/pause — Pause trading (no new entries)
/resume — Resume trading
/close_all — Close all positions (asks for confirmation)
/config — View strategy configuration
/performance — Performance stats since bot started
/help — Show this help message
```

---

#### Security: Chat ID Check

**CRITICAL**: The listener only responds to messages from the configured `chat_id`.

**Implementation**:
```python
def _poll_loop(self):
    for update in updates:
        msg = update.get("message", {})
        msg_chat_id = str(msg.get("chat", {}).get("id", ""))

        # Security: only respond to configured chat
        if msg_chat_id == self.chat_id:
            self._handle_command(msg.get("text", ""))
        else:
            logger.warning(f"Ignored command from unauthorized chat: {msg_chat_id}")
```

**Why**: Prevents anyone who finds your bot token from controlling your bot.

**How to Get Chat ID**:
1. Send `/start` to your bot
2. Visit: `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
3. Look for `"chat":{"id":123456789}`
4. Use that ID in `TELEGRAM_CHAT_ID` env var

---

#### Polling Mechanism

**Method**: Telegram `getUpdates` API (NOT webhooks)

**Why getUpdates?**
- No public URL needed (works on Railway workers without HTTP endpoint)
- Simpler to implement (just HTTP GET polling)
- More reliable than webhooks (no missed messages if server restarts)

**How it works**:
```python
GET https://api.telegram.org/bot{token}/getUpdates
  ?offset={last_update_id + 1}
  &timeout=5
```

**Parameters**:
- `offset`: Only fetch updates newer than this ID
- `timeout`: Long polling (waits up to 5s for new messages)

**Polling interval**: 5 seconds

**Error handling**:
- **401 Unauthorized**: Invalid bot token → stop polling (no retry)
- **Network errors**: Log and retry after 5s
- **Timeout**: Normal (expected), continue polling

**Update ID tracking**: `self.last_update_id` stores the last processed update ID to avoid re-processing messages.

---

#### Integration with main.py

**Initialization** (after all components):
```python
# 5. Initialize Telegram command listener (B14)
cmd_listener = None
if app_config.telegram.enabled:
    cmd_listener = TelegramCommandListener(
        bot_token=app_config.telegram.bot_token,
        chat_id=app_config.telegram.chat_id,
        order_manager=order_manager,
        risk_manager=risk_manager,
        broker=broker,
        notifier=notifier,
        config=strategy_config
    )
    cmd_listener.start()
    logger.info("Telegram command listener started (polling every 5s)")
else:
    logger.info("Telegram command listener disabled (Telegram not configured)")
```

**Thread Liveness Check** (in main loop):
```python
while True:
    # Check command listener thread health (B14)
    if cmd_listener is not None and not cmd_listener.thread.is_alive():
        logger.error("⚠️ Telegram command listener thread died")
        notifier.send_error_alert(
            "⚠️ Command listener offline — /pause and /resume unavailable. "
            "Restart service to restore."
        )
        # Set to None to avoid spamming alerts every loop iteration
        cmd_listener = None
```

**SIGTERM Handler**:
```python
def handle_sigterm(signum, frame):
    logger.info("SIGTERM received — Railway is restarting/stopping")

    # Stop command listener
    if cmd_listener is not None:
        cmd_listener.stop()
        logger.info("Telegram command listener stopped")

    # ... rest of shutdown logic
```

**Important**: Trading continues even if command listener dies. The liveness check alerts you, but does NOT crash the bot.

---

#### Daemon Thread Behavior

**Why daemon thread?**
- Main trading loop runs in main thread
- Command listener runs in background daemon thread
- Daemon threads don't prevent process shutdown
- When main thread exits (SIGTERM), daemon terminates automatically

**Thread properties**:
```python
self.thread = threading.Thread(
    target=self._poll_loop,
    daemon=True,
    name="TelegramCommandListener"
)
```

**Non-blocking**: Polling happens in background, never blocks trading loop.

---

#### Adding New Commands

**1. Add handler method**:
```python
def _cmd_my_command(self):
    """Handle /my_command."""
    # Get data
    data = self.broker.get_something()

    # Format message
    message = f"<b>My Command Result</b>\n\n{data}"

    # Send response
    self.notifier.send_message(message, parse_mode="HTML")
```

**2. Route in `_handle_command()`**:
```python
def _handle_command(self, text: str):
    command = text.lower().strip()

    if command == "/status":
        self._cmd_status()
    elif command == "/my_command":  # Add here
        self._cmd_my_command()
    # ... rest of routes
```

**3. Update `/help` response**:
```python
def _cmd_help(self):
    message = (
        "🤖 <b>AlphaLive Commands</b>\n\n"
        "/status — Current bot state\n"
        "/my_command — My new command\n"  # Add here
        # ... rest of commands
    )
```

**4. Add test**:
```python
def test_my_command(mock_components):
    listener = TelegramCommandListener(...)
    listener._handle_command("/my_command")

    # Verify response
    assert mock_components['notifier'].send_message.called
    call_args = mock_components['notifier'].send_message.call_args
    message = call_args[0][0]
    assert "My Command Result" in message
```

---

#### Testing

**Test files**: `tests/test_telegram_commands.py` (9 tests)

**Tests**:
1. `test_status_command`: Verify /status returns formatted status
2. `test_pause_resume`: Verify /pause and /resume toggle flag
3. `test_unknown_command`: Verify /help suggestion for unknown commands
4. `test_wrong_chat_ignored`: Verify messages from other chats ignored
5. `test_close_all_confirmation_flow`: Verify /close_all requires confirmation
6. `test_config_command`: Verify /config returns strategy config
7. `test_performance_command`: Verify /performance returns stats
8. `test_help_command`: Verify /help lists all commands
9. `test_start_stop_thread`: Verify thread starts and stops correctly

**Running tests**:
```bash
# All command listener tests
pytest tests/test_telegram_commands.py -v

# Specific test
pytest tests/test_telegram_commands.py::test_status_command -v
```

**Manual testing** (local development):
1. Start bot with Telegram configured
2. Send commands to your bot from Telegram app
3. Verify responses and behavior
4. Check logs for command processing

---

#### Error Handling

**Command execution errors**:
```python
try:
    if command == "/status":
        self._cmd_status()
    # ... other commands
except Exception as e:
    logger.error(f"Command handler error: {e}", exc_info=True)
    self.notifier.send_message(
        f"⚠️ Error executing command: {e}\n\nCheck logs for details."
    )
```

**Polling errors**:
- **401 Unauthorized**: Stop polling (invalid token)
- **Timeout**: Normal, continue polling
- **Network errors**: Log and retry after 5s
- **Telegram API errors**: Log and retry after 5s

**Thread death**: Detected by liveness check in main loop → alert sent → trading continues.

---

#### Best Practices

1. **Always set chat_id** — prevents unauthorized access
2. **Test commands locally** before deploying to Railway
3. **Monitor command listener logs** — warnings indicate auth failures or thread crashes
4. **Use /pause, not TRADING_PAUSED** — faster (instant vs 15-30s restart)
5. **Confirm /close_all** — two-step prevents accidents
6. **Check /status regularly** — verify bot is trading as expected
7. **Use /performance** — track win rate and P&L
8. **Keep bot token secret** — don't commit to git, use Railway env vars

---

#### Troubleshooting

**Problem**: Commands not responding

**Solution**:
- Check Telegram configured: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` set
- Check logs for "Telegram command listener started"
- Verify you're sending from the correct chat (check chat_id matches)
- Check logs for "Ignored command from unauthorized chat" warnings

**Problem**: Thread keeps dying

**Solution**:
- Check logs for exceptions in `_poll_loop()`
- Verify bot token is valid (test with `curl https://api.telegram.org/bot<TOKEN>/getMe`)
- Check network connectivity from Railway
- Look for 401 Unauthorized errors (invalid token)

**Problem**: /pause not working (trades still executing)

**Solution**:
- Verify `risk_manager.trading_paused_manual` is True
- Check logs for "Trading paused via Telegram /pause command"
- Verify `can_trade()` checks `trading_paused_manual` flag
- Check for race conditions (signal generated before pause)

**Problem**: /close_all confirmation timeout

**Solution**:
- `/close_all` and `/confirm_close` must be sent in sequence
- Confirmation expires if you send another command first
- Just send `/close_all` again to restart flow

#### 11. Logging (`utils/logger.py`)

**Purpose**: Configure structured logging for Railway.

**Configuration**:
- Logs to STDOUT (Railway captures automatically)
- Format: `%(asctime)s [%(levelname)s] %(name)s: %(message)s`
- Level: Configured via `LOG_LEVEL` env var (default: INFO)
- Third-party libraries (httpx, alpaca) set to WARNING to reduce noise

**Usage**:
```python
from alphalive.utils.logger import setup_logger
setup_logger()  # Call once at startup in run.py
```

#### 12. State Persistence (`state.py`)

**Purpose**: Lightweight state persistence to handle Railway restarts gracefully.

**Location**: `alphalive/state.py`

**Problem**: Railway restarts happen frequently (deploys, crashes, maintenance). Without state persistence, the bot would:
- Re-run morning signal check (duplicate trades)
- Re-send EOD summary (spam)
- Forget position highs (miscalculate trailing stops)
- Reset daily P&L (circuit breaker reset)

**Solution**: Store critical state in JSON file that survives restarts.

**State File Locations**:

| Environment | Path | Persistent? | Use Case |
|-------------|------|-------------|----------|
| Default | `/tmp/alphalive_state.json` | ❌ No | Development, non-trailing-stop strategies |
| Production (trailing stops) | `/mnt/data/alphalive_state.json` | ✅ Yes | Railway Volume, survives restarts |

**BotState Class**:

```python
from alphalive.state import BotState

# Initialize (loads from STATE_FILE env var)
state = BotState()

# Check if morning check already ran today
if not state.already_ran_morning_check("2024-03-11"):
    # Run signal check
    generate_signal(...)
    state.mark_morning_check_done("2024-03-11")

# Check if EOD summary already sent today
if not state.already_sent_eod("2024-03-11"):
    send_eod_summary(...)
    state.mark_eod_sent("2024-03-11")

# Track position highs for trailing stops
state.set_position_high("AAPL", 155.0)
high = state.get_position_high("AAPL")  # Returns 155.0

# When position closed
state.clear_position_high("AAPL")

# Daily reset
state.reset_daily("2024-03-12")
```

**State Structure**:

```json
{
  "last_morning_check_date": "2024-03-11",
  "last_eod_summary_date": "2024-03-11",
  "daily_pnl": 450.0,
  "trades_today": [
    {"ticker": "AAPL", "pnl": 450.0}
  ],
  "position_highs": {
    "AAPL": 155.0,
    "TSLA": 200.0
  },
  "last_startup": "2024-03-11T09:30:00-05:00",
  "last_saved": "2024-03-11T15:55:00-05:00",
  "version": "1.0"
}
```

**Key Methods**:

1. **`already_ran_morning_check(today: str) -> bool`**
   - Check if morning signal check already ran today
   - Prevents duplicate trades on restart

2. **`mark_morning_check_done(today: str)`**
   - Mark morning check as complete
   - Saves state to file

3. **`already_sent_eod(today: str) -> bool`**
   - Check if EOD summary already sent
   - Prevents spam on restart

4. **`mark_eod_sent(today: str)`**
   - Mark EOD summary as sent
   - Saves state to file

5. **`get_position_high(ticker: str) -> Optional[float]`**
   - Get highest price seen for position (trailing stops)
   - Returns None if not tracking

6. **`set_position_high(ticker: str, price: float)`**
   - Update position high (only increases, never decreases)
   - Saves state to file

7. **`clear_position_high(ticker: str)`**
   - Clear position high when position closed
   - Saves state to file

8. **`reset_daily(today: str)`**
   - Reset daily counters at start of new trading day
   - Clears: daily_pnl, trades_today, morning_check_date, eod_summary_date
   - Keeps: position_highs (persist across days)

**Trailing Stop Enforcement**:

**CRITICAL**: If `trailing_stop_enabled=true` in strategy config, the bot **refuses to start** unless `PERSISTENT_STORAGE=true`.

**Rationale**: A Railway redeploy mid-day would reset `position_highs`, causing the bot to miscalculate trailing stops (real money risk).

**Enforcement Code** (in `main.py`):

```python
from alphalive.state import check_trailing_stop_requirements

# Immediately after loading config, before broker connection
check_trailing_stop_requirements(strategy_config, notifier)
```

**Startup Behavior**:

```python
if strategy_config.risk.trailing_stop_enabled:
    if os.environ.get("PERSISTENT_STORAGE", "false").lower() != "true":
        logger.critical(
            "STARTUP ABORTED: trailing_stop_enabled=true requires persistent "
            "storage, but PERSISTENT_STORAGE is not set to true. A Railway "
            "redeploy mid-day will reset position_highs and miscalculate "
            "trailing stops, which is a real money risk. Either: "
            "(A) Set trailing_stop_enabled=false in your strategy config, or "
            "(B) Mount a Railway Volume, set STATE_FILE=/mnt/data/alphalive_state.json, "
            "and set PERSISTENT_STORAGE=true"
        )
        notifier.send_error_alert(
            "⛔ AlphaLive refused to start: trailing stops require "
            "persistent storage. See Railway logs for fix instructions."
        )
        sys.exit(1)
```

**Daily P&L Reconstruction**:

On startup, the bot reconstructs `daily_pnl` from broker's today's fills to restore circuit breaker state after restart.

```python
from alphalive.state import reconstruct_daily_pnl

# On startup, after broker connection
daily_pnl = reconstruct_daily_pnl(broker, risk_manager)
```

**How it works**:
1. Call `broker.get_todays_fills()` → list of fills from today
2. Sum `pnl` from all fills
3. Set `risk_manager.daily_pnl = sum`

**Failure Handling**: If reconstruction fails (API error, no fills yet):
- Default to `0.0`
- Log WARNING: "Daily P&L reconstruction failed — circuit breaker reset. Monitor manually today."
- **This is acceptable risk** — better than crashing on startup

**Error Recovery**:

```python
try:
    fills = broker.get_todays_fills()
    daily_pnl = sum(fill.get("pnl", 0.0) for fill in fills)
    risk_manager.daily_pnl = daily_pnl
    logger.info(f"Daily P&L reconstructed: ${daily_pnl:.2f} ({len(fills)} fills)")
except Exception as e:
    logger.warning(
        f"Daily P&L reconstruction failed: {e}. "
        f"Defaulting to 0.0. Circuit breaker reset. Monitor manually today."
    )
    risk_manager.daily_pnl = 0.0
```

**Corrupted State File Handling**:

If state file is corrupted (invalid JSON), the bot **does not crash**:
- Logs WARNING
- Returns default state (all counters reset)
- Continues running

**SIGTERM State Flush**:

On SIGTERM (Railway restart), the bot:
1. Sends Telegram shutdown notification with daily stats
2. State is already saved (happens on every update)
3. Exits cleanly with code 0

**No explicit flush needed** — state is saved after every update.

**Environment Variables**:

| Variable | Default | Description |
|----------|---------|-------------|
| `STATE_FILE` | `/tmp/alphalive_state.json` | Path to state file |
| `PERSISTENT_STORAGE` | `false` | Required for trailing stops |

**Railway Volume Setup** (for trailing stops):

See [SETUP.md](SETUP.md) section "Using Trailing Stops" for detailed instructions:
1. Create Railway Volume at `/mnt/data`
2. Set `STATE_FILE=/mnt/data/alphalive_state.json`
3. Set `PERSISTENT_STORAGE=true`

**Testing State Persistence**:

```bash
# Run state persistence tests
pytest tests/test_state.py -v

# Test specific scenario
pytest tests/test_state.py::test_state_survives_restart -v
```

**State Persistence Tests** (`tests/test_state.py`):
- `test_state_survives_restart`: Verify state loads from file
- `test_morning_check_not_duplicated`: Prevent duplicate signal checks
- `test_eod_not_duplicated`: Prevent duplicate EOD summaries
- `test_position_high_persists`: Trailing stop state persists
- `test_position_high_cleared`: Position highs cleared on exit
- `test_corrupted_state_file_returns_defaults`: Graceful error handling
- `test_daily_pnl_reconstruction_success`: P&L restored from fills
- `test_daily_pnl_reconstruction_failure`: Defaults to 0.0 on error
- `test_trailing_stop_enforcement_blocks_startup`: Blocks without persistent storage
- `test_trailing_stop_enforcement_allows_with_persistent_storage`: Allows with persistent storage

**Best Practices**:

1. **Development**: Use default `/tmp/` location (ephemeral)
2. **Production (no trailing stops)**: Use default `/tmp/` location (simpler)
3. **Production (with trailing stops)**: Use Railway Volume at `/mnt/data` (required)
4. **Daily Reset**: Call `state.reset_daily(today)` at start of new trading day
5. **SIGTERM**: State is auto-saved, no explicit flush needed
6. **Testing**: Always test restart behavior in staging before production

#### 13. Health Check Endpoint (`health.py`)

**Purpose**: Minimal HTTP server for Railway healthcheck monitoring. Runs on daemon thread to not block main trading loop.

**Location**: `alphalive/health.py`

**Why**: Railway needs a way to verify the bot is alive and healthy. A simple HTTP endpoint that responds with 200 OK when healthy allows Railway to restart the bot if it becomes unresponsive.

**Architecture**:
- HTTP server runs on daemon thread (non-blocking, won't prevent shutdown)
- Listens on configurable port (default: 8080)
- Authenticates via `X-Health-Secret` header
- Returns JSON payload with health status, uptime, and custom metrics
- Disabled by default if `HEALTH_SECRET` not set

#### HealthCheckHandler

**Purpose**: HTTP request handler for health check endpoint.

**Authentication**: Requires `X-Health-Secret` header matching `HEALTH_SECRET` env var. If `HEALTH_SECRET` not set, endpoint is disabled (returns 503).

**Class Variables** (set by HealthServer):
```python
class HealthCheckHandler(BaseHTTPRequestHandler):
    health_data = {}    # Custom health metrics
    start_time = None   # Bot start time
    secret = None       # HEALTH_SECRET value
```

**Response Codes**:

| Code | Condition | Response Body |
|------|-----------|---------------|
| 200 | Valid secret, healthy | `{"status": "ok", "uptime": "2h 15m", "last_check": "2024-03-11T15:30:00-05:00", ...}` |
| 401 | Wrong secret | `{"error": "Unauthorized"}` |
| 404 | Path != "/" | `{"error": "Not found"}` |
| 503 | HEALTH_SECRET not set | `{"error": "Health endpoint disabled"}` |

**Example Request**:
```bash
curl -H "X-Health-Secret: your_secret_here" http://localhost:8080/
```

**Example Response** (200 OK):
```json
{
  "status": "ok",
  "uptime": "2h 15m",
  "last_check": "2024-03-11T15:30:00-05:00",
  "warmup_complete": true,
  "bars_loaded": 252,
  "trading_paused": false,
  "dry_run": false,
  "paper": true,
  "strategy": "ma_crossover",
  "ticker": "AAPL",
  "timeframe": "1Day"
}
```

**Implementation**:
```python
def do_GET(self):
    """Handle GET requests to / endpoint."""
    if self.path != "/":
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b'{"error": "Not found"}')
        return

    # Check if health endpoint is enabled
    if not self.secret:
        logger.debug("Health check disabled: HEALTH_SECRET not set")
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error": "Health endpoint disabled"}')
        return

    # Verify authentication
    request_secret = self.headers.get("X-Health-Secret")
    if request_secret != self.secret:
        logger.warning(
            f"Health check unauthorized: wrong secret from {self.client_address[0]}"
        )
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error": "Unauthorized"}')
        return

    # Calculate uptime
    if self.start_time:
        uptime_seconds = (datetime.now() - self.start_time).total_seconds()
        hours = int(uptime_seconds // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        uptime = f"{hours}h {minutes}m"
    else:
        uptime = "unknown"

    # Build response payload
    payload = {
        "status": "ok",
        "uptime": uptime,
        "last_check": datetime.now(ET).isoformat(),
        **self.health_data
    }

    # Send response
    self.send_response(200)
    self.send_header("Content-Type", "application/json")
    self.end_headers()
    self.wfile.write(json.dumps(payload).encode('utf-8'))

    logger.debug(f"Health check successful from {self.client_address[0]}")
```

#### HealthServer

**Purpose**: Health check HTTP server running on daemon thread.

**Initialization**:
```python
from alphalive.health import HealthServer

health = HealthServer(
    port=8080,
    health_data={
        "warmup_complete": True,
        "bars_loaded": 252,
        "trading_paused": False,
        "dry_run": False,
        "paper": True
    }
)
health.start()
```

**Key Methods**:

1. **`__init__(port: int = 8080, health_data: dict = None)`**
   - Reads `HEALTH_SECRET` from environment
   - Sets class variables for handler
   - Logs warning if HEALTH_SECRET not set (endpoint disabled)

2. **`start()`**
   - Creates HTTPServer listening on `0.0.0.0:{port}`
   - Starts server in daemon thread (non-blocking)
   - Thread name: "HealthCheckServer"

3. **`update_health_data(data: dict)`**
   - Update health metrics dynamically
   - Example: Update `warmup_complete` after first signal check

4. **`stop()`**
   - Shutdown server gracefully
   - Called on SIGTERM before exit

**Helper Function**:
```python
def create_health_server(config, dry_run: bool = False, paper: bool = True) -> HealthServer:
    """
    Create and start health check server.

    Args:
        config: Strategy configuration
        dry_run: Whether running in dry run mode
        paper: Whether using paper trading

    Returns:
        HealthServer instance
    """
    port = int(os.environ.get("HEALTH_PORT", "8080"))

    health_data = {
        "warmup_complete": True,  # Updated after first signal check
        "bars_loaded": 0,         # Updated after market data fetch
        "trading_paused": os.environ.get("TRADING_PAUSED", "false").lower() == "true",
        "dry_run": dry_run,
        "paper": paper,
        "strategy": config.strategy.name,
        "ticker": config.ticker,
        "timeframe": config.timeframe
    }

    health = HealthServer(port=port, health_data=health_data)
    health.start()

    return health
```

#### Usage in main.py

**Initialization** (after loading config, before main loop):
```python
from alphalive.health import create_health_server

# Create health server
health = create_health_server(config, dry_run=dry_run, paper=paper_trading)

# Main loop starts...
```

**Update Health Data** (during trading):
```python
# After fetching bars
health.update_health_data({"bars_loaded": len(bars)})

# After warmup complete
health.update_health_data({"warmup_complete": True})

# Before shutdown
health.stop()
```

#### Railway Healthcheck Configuration

**railway.toml**:
```toml
[build]
builder = "dockerfile"

[deploy]
restartPolicyType = "always"
healthcheckPath = "/"
healthcheckTimeout = 30
```

**Railway Dashboard**:
1. Go to Settings → Healthcheck
2. Enable HTTP healthcheck
3. Set path: `/`
4. Set port: `8080` (or `${{HEALTH_PORT}}`)
5. Set timeout: 30 seconds
6. Add header: `X-Health-Secret: ${{HEALTH_SECRET}}`

**How it works**:
- Railway sends GET request to `http://<service>:8080/` every 30 seconds
- Includes `X-Health-Secret` header with value from environment variable
- If response is 200 OK, service is healthy
- If response is non-200 or timeout (>30s), service is unhealthy
- With `restartPolicyType = "always"`, Railway restarts the service on failures

#### Environment Variables

| Variable | Default | Description | Required |
|----------|---------|-------------|----------|
| `HEALTH_PORT` | `8080` | Port for health check endpoint | No |
| `HEALTH_SECRET` | `""` | Secret token for authentication | **Yes** (to enable endpoint) |

**Generating HEALTH_SECRET**:
```bash
# Generate a random 32-character hex string
openssl rand -hex 16

# Example output:
a3f7b2c9d4e5f6g7h8i9j0k1l2m3n4o5

# Set in Railway:
HEALTH_SECRET=a3f7b2c9d4e5f6g7h8i9j0k1l2m3n4o5
```

**Security**: Keep `HEALTH_SECRET` private. Don't commit to git. Railway injects it at runtime.

#### Logging Behavior

**When HEALTH_SECRET Set**:
```
INFO: Health endpoint enabled on port 8080
INFO: Health check server listening on port 8080
DEBUG: Health check successful from 10.0.1.5
```

**When HEALTH_SECRET Not Set**:
```
WARNING: Health endpoint disabled: HEALTH_SECRET env var not set. To enable, set HEALTH_SECRET=<random_string>
INFO: Health check server listening on port 8080 (disabled - no HEALTH_SECRET)
DEBUG: Health check disabled: HEALTH_SECRET not set
```

**Unauthorized Requests**:
```
WARNING: Health check unauthorized: wrong secret from 10.0.1.5
```

#### Testing

**Test Files**: `tests/test_state.py` (health endpoint tests at bottom)

**Tests**:
1. `test_health_returns_200_with_correct_secret`: Valid secret returns 200 OK
2. `test_health_returns_401_with_wrong_secret`: Wrong secret returns 401 Unauthorized
3. `test_health_returns_503_when_secret_not_configured`: No secret returns 503 Service Unavailable

**Running Tests**:
```bash
# All health endpoint tests
pytest tests/test_state.py::test_health_returns_200_with_correct_secret -v
pytest tests/test_state.py::test_health_returns_401_with_wrong_secret -v
pytest tests/test_state.py::test_health_returns_503_when_secret_not_configured -v
```

**Manual Testing** (local development):
```bash
# Start bot with HEALTH_SECRET set
export HEALTH_SECRET=test_secret_123
python run.py --config configs/example_strategy.json --dry-run

# In another terminal:
# Valid secret (should return 200)
curl -H "X-Health-Secret: test_secret_123" http://localhost:8080/

# Wrong secret (should return 401)
curl -H "X-Health-Secret: wrong_secret" http://localhost:8080/

# No secret header (should return 401)
curl http://localhost:8080/

# Check response payload
curl -H "X-Health-Secret: test_secret_123" http://localhost:8080/ | jq
```

#### Best Practices

1. **Always set HEALTH_SECRET in production** — required for Railway healthchecks
2. **Use openssl rand -hex 16** to generate strong secrets (32 characters)
3. **Don't commit HEALTH_SECRET to git** — use Railway environment variables
4. **Monitor health check logs** — warnings indicate authentication failures or timeouts
5. **Test healthcheck locally** before deploying to Railway
6. **Keep timeout reasonable** — Railway default is 30s, health endpoint responds <50ms
7. **Disable endpoint in development** if not needed (omit HEALTH_SECRET)

#### Daemon Thread Behavior

**Why daemon thread**:
- Main trading loop is in the main thread
- Health server runs in background daemon thread
- Daemon threads don't prevent process shutdown
- When main thread exits (SIGTERM), daemon thread terminates automatically

**Non-blocking**:
```python
# Start server in daemon thread (won't block main loop)
self.thread = threading.Thread(
    target=self.server.serve_forever,
    daemon=True,
    name="HealthCheckServer"
)
self.thread.start()
```

**SIGTERM Handling**:
```python
# On SIGTERM, stop health server gracefully
def shutdown_handler(signum, frame):
    logger.info("SIGTERM received, shutting down gracefully...")
    health.stop()  # Shutdown HTTP server
    notifier.send_shutdown_notification(daily_stats)
    sys.exit(0)
```

#### Troubleshooting

**Problem**: Railway healthcheck failing (502 Bad Gateway)

**Solution**:
- Check HEALTH_SECRET is set in Railway variables
- Check HEALTH_PORT matches Railway healthcheck config (default: 8080)
- Check logs for "Health endpoint disabled" warning
- Verify `X-Health-Secret` header is configured in Railway healthcheck

**Problem**: Health endpoint returns 401 Unauthorized

**Solution**:
- Verify HEALTH_SECRET value matches in Railway variables and healthcheck header
- Check for typos or whitespace in secret value
- Confirm request includes `X-Health-Secret` header (not `Authorization`)

**Problem**: Health endpoint returns 503 Service Unavailable

**Solution**:
- HEALTH_SECRET not set in environment
- Set HEALTH_SECRET in Railway variables and redeploy

**Problem**: Health server blocking main loop

**Solution**:
- This should never happen (daemon thread)
- Check logs for thread errors
- Verify `daemon=True` in thread initialization

## Configuration Management (`config.py`)

### Overview

AlphaLive uses a comprehensive Pydantic v2-based configuration system that loads and validates:
1. **Strategy configurations** from JSON files (exported from AlphaLab)
2. **Application settings** from environment variables (Railway or .env)
3. **Multi-strategy support** for running multiple strategies simultaneously

### Pydantic Configuration Models

#### BrokerConfig

```python
class BrokerConfig(BaseModel):
    api_key: str              # Alpaca API key
    secret_key: str           # Alpaca secret key
    paper: bool = True        # Use paper trading (default)
    base_url: Optional[str]   # Auto-set based on paper flag

    def mask_api_key() -> str:        # Returns "****2345"
    def mask_secret_key() -> str:     # Returns "****7890"
```

**Validation**:
- API keys cannot be empty
- `base_url` auto-set to `https://paper-api.alpaca.markets` (paper) or `https://api.alpaca.markets` (live)

#### TelegramConfig

```python
class TelegramConfig(BaseModel):
    bot_token: Optional[str] = None    # Telegram bot token
    chat_id: Optional[str] = None      # Telegram chat ID
    enabled: bool = False              # Auto-enabled if both token and chat_id provided
```

**Auto-enablement**: If both `bot_token` and `chat_id` are provided, `enabled` is automatically set to `True`.

#### AppConfig

```python
class AppConfig(BaseModel):
    broker: BrokerConfig                           # Broker configuration
    telegram: TelegramConfig                       # Telegram configuration
    log_level: str = "INFO"                        # DEBUG, INFO, WARNING, ERROR, CRITICAL
    dry_run: bool = False                          # Log trades without executing
    trading_paused: bool = False                   # Pause all trading
    state_file: str = "/tmp/alphalive_state.json"  # State persistence path
    health_port: int = 8080                        # Health check HTTP port
    health_secret: str = ""                        # Health check secret token
    persistent_storage: bool = False               # Enable persistent storage
```

**Validation**:
- `log_level` must be one of: DEBUG, INFO, WARNING, ERROR, CRITICAL

### Configuration Loading Flow

#### 1. Environment Variables → AppConfig

```python
from alphalive.config import load_env

app_config = load_env()
# Reads from:
# 1. OS environment variables (Railway sets these in production)
# 2. .env file (local development fallback)
```

**Priority**:
1. **OS environment variables** (Railway production)
2. **.env file** (local development only)

**Required Environment Variables**:
- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`

**Optional Environment Variables**:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `ALPACA_PAPER` (default: `true`)
- `ALPACA_BASE_URL` (auto-set if not provided)
- `LOG_LEVEL` (default: `INFO`)
- `DRY_RUN` (default: `false`)
- `TRADING_PAUSED` (default: `false`)
- `STATE_FILE` (default: `/tmp/alphalive_state.json`)
- `HEALTH_PORT` (default: `8080`)
- `HEALTH_SECRET` (default: `""`)
- `PERSISTENT_STORAGE` (default: `false`)

**Environment Variable Mapping**:

| Environment Variable | AppConfig Field | Type | Default |
|---------------------|-----------------|------|---------|
| `ALPACA_API_KEY` | `broker.api_key` | str | *required* |
| `ALPACA_SECRET_KEY` | `broker.secret_key` | str | *required* |
| `ALPACA_PAPER` | `broker.paper` | bool | `true` |
| `ALPACA_BASE_URL` | `broker.base_url` | str | *auto* |
| `TELEGRAM_BOT_TOKEN` | `telegram.bot_token` | str | `None` |
| `TELEGRAM_CHAT_ID` | `telegram.chat_id` | str | `None` |
| `LOG_LEVEL` | `log_level` | str | `INFO` |
| `DRY_RUN` | `dry_run` | bool | `false` |
| `TRADING_PAUSED` | `trading_paused` | bool | `false` |
| `STATE_FILE` | `state_file` | str | `/tmp/alphalive_state.json` |
| `HEALTH_PORT` | `health_port` | int | `8080` |
| `HEALTH_SECRET` | `health_secret` | str | `""` |
| `PERSISTENT_STORAGE` | `persistent_storage` | bool | `false` |

**Boolean Parsing**: Strings `"true"`, `"1"`, `"yes"`, `"on"` are parsed as `True` (case-insensitive).

**API Key Masking**: API keys are masked in logs for security:
```python
logger.info(f"API Key: {app_config.broker.mask_api_key()}")  # "****2345"
```

#### 2. JSON Files → StrategySchema

**Single Strategy**:
```python
from alphalive.config import load_strategy

strategy = load_strategy("configs/ma_crossover.json")
# Returns: StrategySchema instance
```

**Multiple Strategies** (Multi-Strategy Mode):
```python
from alphalive.config import load_strategies

strategies = load_strategies("configs/")
# Loads all .json files from directory
# Returns: List[StrategySchema]
```

**Automatic Mode Detection** (Recommended):
```python
from alphalive.config import load_config_path

# Single file: returns [StrategySchema]
strategies = load_config_path("configs/ma_crossover.json")

# Directory: returns List[StrategySchema]
strategies = load_config_path("configs/strategies/")

# Same function works for both modes!
```

**Error Handling**:
- Invalid JSON: Raises `ValueError` with clear error message
- Schema validation failure: Logs detailed field-level errors with Pydantic's error messages
- Missing files: Raises `FileNotFoundError`

**Example Error Output**:
```
ERROR: Schema validation failed for configs/bad_strategy.json
  ✗ risk -> stop_loss_pct: Input should be greater than or equal to 0.1
  ✗ strategy -> name: Input should be one of ['ma_crossover', 'rsi_mean_reversion', ...]
```

#### 3. Validation Summary

```python
from alphalive.config import validate_all

valid = validate_all(strategies, app_config)
# Returns: True if all valid, False otherwise
# Prints comprehensive summary table
```

**Summary Output Example**:
```
================================================================================
ALPHALIVE CONFIGURATION SUMMARY
================================================================================

STRATEGIES (2 loaded):
  ✅ [1] ma_crossover on AAPL @ 1Day | Sharpe: 1.45 | Return: 32.5%
  ✅ [2] rsi_mean_reversion on TSLA @ 1Hour | Sharpe: 1.82 | Return: 28.3%

BROKER:
  ✅ Alpaca Paper Trading
     API Key: ****2345
     Base URL: https://paper-api.alpaca.markets

NOTIFICATIONS:
  ✅ Telegram: Configured (chat: ...1234)

RISK MANAGEMENT (example from first strategy):
  ✅ Stop Loss: 2.0%
  ✅ Take Profit: 5.0%
  ✅ Max Position Size: 10.0%
  ✅ Max Daily Loss: 3.0% (GLOBAL across all strategies)
  ✅ Max Open Positions: 5 (PER STRATEGY)
  ✅ Portfolio Max Positions: 10 (GLOBAL)

APPLICATION SETTINGS:
  Log Level: INFO
  Dry Run: NO
  Trading Paused: NO
  State File: /tmp/alphalive_state.json

MULTI-STRATEGY RISK SCOPE:
  • max_open_positions: PER STRATEGY (each strategy can have up to N positions)
  • max_daily_loss_pct: GLOBAL (all strategies halted if total account loss exceeds limit)
  • max_position_size_pct: PER STRATEGY (% of total account equity)
  • portfolio_max_positions: GLOBAL (total positions across all strategies)
  Total potential positions: 10 (capped by portfolio limit: 10)

================================================================================
✅ ALL CONFIGURATIONS VALID - Ready to trade!
================================================================================
```

### Multi-Strategy Mode

#### Risk Scope Clarification

When multiple strategies are loaded via `load_strategies()`, risk limits are applied as follows:

**PER-STRATEGY Limits**:
- **`max_open_positions`**: Each strategy can have up to N positions open independently
  - Example: 3 strategies with `max_open_positions=2` each → up to 6 total positions (if portfolio limit allows)
- **`max_position_size_pct`**: Applied to **total account equity** per strategy
  - Example: 3 strategies each at 10% → total potential exposure is 30% of account
- **Consecutive loss circuit breaker**: Strategy A stopping out doesn't pause Strategy B

**GLOBAL Limits** (enforced by `GlobalRiskManager`):
- **`max_daily_loss_pct`**: Calculated against **total account equity**
  - If ANY combination of strategies pushes account equity down by this %, **ALL strategies are halted** for the day
  - Example: Account starts at $100k, max_daily_loss_pct=3% → if equity drops to $97k, all strategies stop
- **`portfolio_max_positions`**: Total positions across **ALL strategies**
  - Example: 3 strategies with `max_open_positions=[5,3,2]` → total potential = 10, but `portfolio_max_positions=8` caps it at 8

#### GlobalRiskManager

**Location**: `alphalive/execution/risk_manager.py`

**Purpose**: Enforce portfolio-level limits across all strategies.

**Key Methods**:
```python
global_rm = GlobalRiskManager(broker)

# Check global daily loss (halts ALL strategies if exceeded)
can_continue, reason = global_rm.check_global_daily_loss(max_daily_loss_pct=3.0)

# Check portfolio position limit
can_trade, reason = global_rm.check_portfolio_positions(portfolio_max_positions=10)

# Record trade for global tracking
global_rm.record_trade(strategy_name="ma_crossover", pnl=150.50)

# Check if globally halted
if global_rm.is_trading_halted():
    logger.warning(f"All strategies halted: {global_rm.get_halt_reason()}")
```

**Daily Reset**:
- Global stats reset at midnight (date change)
- `start_equity` is captured on first check of the day
- P&L is calculated as: `(current_equity - start_equity) / start_equity * 100`

**Halt Behavior**:
- Once global daily loss limit is hit, `strategies_halted` flag is set to `True`
- All subsequent `can_trade()` checks return `False` with halt reason
- Resets automatically on next trading day

#### Multi-Strategy Workflow

```python
from alphalive.config import load_strategies, load_env, validate_all
from alphalive.execution.risk_manager import GlobalRiskManager

# 1. Load configurations
strategies = load_strategies("configs/")
app_config = load_env()

# 2. Validate all
if not validate_all(strategies, app_config):
    raise ValueError("Configuration validation failed")

# 3. Initialize global risk manager
global_rm = GlobalRiskManager(broker)

# 4. Main loop
for strategy in strategies:
    # Check global limits first
    can_continue, reason = global_rm.check_global_daily_loss(
        strategy.risk.max_daily_loss_pct
    )

    if not can_continue:
        logger.warning(f"Strategy {strategy.strategy.name} skipped: {reason}")
        continue

    # Check portfolio positions
    can_trade, reason = global_rm.check_portfolio_positions(
        strategy.risk.portfolio_max_positions
    )

    if not can_trade:
        logger.warning(f"Strategy {strategy.strategy.name} skipped: {reason}")
        continue

    # Proceed with per-strategy risk checks and trading
    # ...
```

#### Setting Up Multi-Strategy Mode (B15)

**When to use**:
- ✅ After successfully running 1 strategy for 1+ month
- ✅ When strategies are uncorrelated (different tickers or timeframes)
- ✅ When you understand the risk implications (positions can add up fast)

**When NOT to use**:
- ❌ During initial deployment/testing
- ❌ If strategies trade the same ticker (correlated risk)
- ❌ If you haven't tested portfolio-level limits

**Setup Steps**:

1. **Create strategy configs directory**:
   ```bash
   mkdir -p configs/strategies/
   ```

2. **Export multiple strategies from AlphaLab**:
   - Export 2-5 strategies (click "Export to AlphaLive" for each)
   - Place JSON files in `configs/strategies/`:
     ```
     configs/strategies/aapl_ma_crossover_1day.json
     configs/strategies/msft_rsi_reversion_1day.json
     configs/strategies/googl_bollinger_1hour.json
     ```

3. **Set Railway environment variable**:
   - In Railway dashboard → Variables:
     - Remove: `STRATEGY_CONFIG=configs/strategy.json`
     - Add: `STRATEGY_CONFIG_DIR=configs/strategies/`
   - Railway will restart and load all `.json` files from that directory

4. **Verify multi-strategy startup**:
   Check Railway logs for:
   ```
   Loaded 3 strategies from configs/strategies/
   Multi-strategy mode: 3 strategies loaded
     [1] ma_crossover on AAPL @ 1Day
     [2] rsi_mean_reversion on MSFT @ 1Day
     [3] bollinger_breakout on GOOGL @ 1Hour
   ```

**Implementation Details** (B15):

In main.py, multi-strategy mode works as follows:

```python
# 1. Load all strategies
all_strategy_configs = load_config_path(config_path)  # File or directory

# 2. Create maps for each strategy
signal_engine_map = {}
risk_manager_map = {}
order_manager_map = {}

for strategy_config in all_strategy_configs:
    ticker = strategy_config.ticker

    # Each strategy gets its own signal engine
    signal_engine_map[ticker] = SignalEngine(strategy_config)

    # Each strategy gets its own risk manager
    risk_manager_map[ticker] = RiskManager(
        risk_config=strategy_config.risk,
        execution_config=strategy_config.execution,
        strategy_name=strategy_config.strategy.name
    )

    # Each strategy gets its own order manager
    order_manager_map[ticker] = OrderManager(
        broker=broker,
        risk_manager=risk_manager_map[ticker],
        config=strategy_config,
        notifier=notifier,
        dry_run=app_config.dry_run
    )

# 3. In main loop: iterate through all strategies
for strategy_config in all_strategy_configs:
    ticker = strategy_config.ticker

    # Generate signal for this strategy
    signal = signal_engine_map[ticker].generate_signal(bars)

    # Execute trade with this strategy's risk manager
    order_manager_map[ticker].execute_signal(...)
```

**Portfolio-Level Position Tracking**:

Each strategy's RiskManager tracks its own positions. Portfolio-level limits are enforced by:
1. **Per-strategy check**: `current_positions_count` (positions for this strategy)
2. **Portfolio check**: `total_portfolio_positions` (sum across all strategies)

Example in `can_trade()`:
```python
# 5. Check max positions (per-strategy)
if not self.check_max_positions(current_positions_count):
    return (False, f"Max positions reached for strategy: {current_positions_count}/{max_open_positions}")

# 6. Check portfolio max positions (across ALL strategies)
if total_portfolio_positions >= self.risk_config.portfolio_max_positions:
    return (False, f"Portfolio max positions reached: {total_portfolio_positions}/{portfolio_max_positions}")
```

**Risk Scenario Example**:

3 strategies with these configs:
- Strategy A: `max_open_positions=5`
- Strategy B: `max_open_positions=5`
- Strategy C: `max_open_positions=5`
- Portfolio: `portfolio_max_positions=10`

**Theoretical max**: 5+5+5 = 15 positions
**Actual max**: 10 positions (portfolio limit kicks in first)

**Correlation Risk Warning**:

If all 3 strategies trade tech stocks (AAPL, MSFT, GOOGL), a tech sector crash will hit all positions simultaneously. Your 10-position limit won't help if they're all correlated. **Diversify across sectors or asset classes**.

**Testing Multi-Strategy Mode**:

1. Start with `DRY_RUN=true`, 2 strategies only
2. Verify both strategies generate signals independently
3. Verify `portfolio_max_positions` limit works
4. Verify daily loss limit applies to combined P&L
5. Run C1 signal parity test for EACH strategy
6. Paper trade for 2 weeks before going live

### Backward Compatibility Functions

For legacy code compatibility:

```python
# Old function name → New implementation
load_config(path)                    # → load_strategy(path)
validate_environment_variables()     # → load_env() (returns dict instead of AppConfig)
get_config_from_env()                # → os.getenv("STRATEGY_CONFIG")
```

## Key Architecture Components

### Strategy Schema (`alphalive/strategy_schema.py`)

**Location**: `/alphalive/strategy_schema.py`

**Purpose**: Canonical Pydantic v2 model for strategy configurations exported from AlphaLab.

**Schema Version**: 1.0

**Main Models**:
- `StrategySchema`: Top-level model containing all strategy configuration
- `Strategy`: Strategy name and parameters
- `Risk`: Risk management parameters (stop loss, take profit, position sizing, etc.)
- `Execution`: Order execution settings (market/limit, cooldown, etc.)
- `SafetyLimits`: Safety thresholds to prevent runaway behavior (NEW in v13)
- `Metadata`: Export metadata and backtest performance from AlphaLab

**How It's Used**:
1. AlphaLab exports strategies as JSON files matching this schema
2. `config.py` loads JSON files and validates against `StrategySchema`
3. Pydantic v2 validators enforce ranges and cross-field constraints
4. Invalid configurations are rejected before any trading occurs

**Backward Compatibility**:
- If `safety_limits` block is missing from JSON (pre-v13 exports), defaults are applied:
  ```python
  {
    "max_trades_per_day": 20,
    "max_api_calls_per_hour": 500,
    "signal_generation_timeout_seconds": 5.0,
    "broker_degraded_mode_threshold_failures": 3
  }
  ```
- Use `load_strategy_with_defaults(data)` helper for automatic default injection

### Supported Strategies

Currently supported strategy names (defined in `StrategyName` Literal):
1. `ma_crossover` - Moving average crossover
2. `rsi_mean_reversion` - RSI-based mean reversion
3. `momentum_breakout` - Momentum breakout
4. `bollinger_breakout` - Bollinger Band breakout
5. `vwap_reversion` - VWAP reversion

**How to Add a New Strategy**:
1. Add the strategy name to the `StrategyName` Literal in `strategy_schema.py`:
   ```python
   StrategyName = Literal[
       "ma_crossover",
       "rsi_mean_reversion",
       "momentum_breakout",
       "bollinger_breakout",
       "vwap_reversion",
       "your_new_strategy"  # Add here
   ]
   ```
2. Implement the strategy in AlphaLab
3. Export from AlphaLab with the new strategy name
4. AlphaLive will automatically accept it after updating the schema

### Risk Management Hierarchy

**Per-Strategy Limits** (enforced per strategy instance):
- `max_open_positions`: Max positions for this specific strategy (1-50)
- `stop_loss_pct`: Stop loss for each position (0.1-50.0%)
- `take_profit_pct`: Take profit for each position (0.5-100.0%)
- `max_position_size_pct`: Max size of any single position (1.0-100.0%)

**Portfolio-Level Limits** (enforced across all strategies):
- `portfolio_max_positions`: Max total positions across all running strategies (1-100)
  - **Must be >= max_open_positions for any strategy**
  - Example: Strategy A has max_open_positions=15, portfolio_max_positions=10
    - Portfolio limit (10) caps the total, even if Strategy A could open 15
- `max_daily_loss_pct`: Max daily loss for entire portfolio (0.5-20.0%)

**Safety Limits** (system-wide protections):
- `max_trades_per_day`: Circuit breaker for total trades (1-200, default 20)
- `max_api_calls_per_hour`: Rate limit for broker API (100-2000, default 500)
- `signal_generation_timeout_seconds`: Timeout for strategy signal generation (1.0-30.0s, default 5.0s)
- `broker_degraded_mode_threshold_failures`: Failures before entering degraded mode (1-10, default 3)

**Enforcement Location**:
- Enforced in `risk_manager.can_trade()` before placing any order
- Violations log warnings and reject trade entry

### Field Validation Warnings

The schema logs warnings for potentially risky parameters:
- `stop_loss_pct > 15.0%`: "Very wide stop loss — verify intentional"
- `take_profit_pct > 50.0%`: "Aggressive take profit target"
- `trailing_stop_pct > 10.0%`: "Wide trailing stop — may give back significant gains"
- `max_trades_per_day > 50`: "High trade frequency — verify strategy logic"

### Schema Version Policy

**Current Version**: 1.0

**Version Numbering**:
- **Minor versions** (1.0, 1.1, 1.2): Backward compatible changes
  - Add new optional fields
  - Add new strategies to `StrategyName` enum
  - Existing JSON files work without modification

- **Major versions** (2.0, 3.0): Breaking changes
  - Rename or remove fields
  - Change field types
  - Add new required fields
  - Requires migration function and re-export of all strategies

**When to Bump Version**:
- Adding optional field: Keep 1.0, document in changelog
- Adding `safety_limits` (v13): Kept as 1.0 with backward compat via defaults
- Making `safety_limits` required: Would bump to 2.0, write migration

**Validation Strategy**:
- Schema version is validated as `Literal["1.0"]` in Pydantic model
- Future versions will require schema migration before validation
- Unsupported versions are rejected with clear error messages

### Schema Migrations (`alphalive/migrations/`)

**Location**: `/alphalive/migrations/schema_migrations.py`

**Purpose**: Automatic migration of strategy configurations across schema versions for backward compatibility.

**How It Works**:
1. `config.py` calls `migrate_schema(config_dict)` before Pydantic validation
2. Migration system detects schema version and applies transformations
3. Migrations are chained: v1.0 → v1.1 → v2.0 → ... (recursive)
4. Final migrated config is validated against current schema

**Usage Example**:
```python
from alphalive.migrations import migrate_schema
from alphalive.strategy_schema import StrategySchema
import json

# Load JSON file
with open("strategy.json") as f:
    config_dict = json.load(f)

# Apply migrations (if needed)
migrated_config = migrate_schema(config_dict)

# Validate with Pydantic
strategy = StrategySchema(**migrated_config)
```

**Current Migrations**:
- **v1.0 → v1.0** (backward compat): Adds default `safety_limits` if missing (v13 enhancement)
- **v1.0 → v2.0**: Placeholder for future breaking changes (not implemented)

**How to Write a New Migration**:

1. **Minor Version Migration** (backward compatible):
   ```python
   def migrate_1_0_to_1_1(config: Dict[str, Any]) -> Dict[str, Any]:
       """Add optional fields with defaults"""
       logger.info("Migrating schema from 1.0 to 1.1")

       # Add optional field
       config["execution"].setdefault("retry_failed_orders", False)

       # Update version
       config["schema_version"] = "1.1"
       return config
   ```

2. **Major Version Migration** (breaking changes):
   ```python
   def migrate_1_0_to_2_0(config: Dict[str, Any]) -> Dict[str, Any]:
       """Handle breaking changes"""
       logger.info("Migrating schema from 1.0 to 2.0")

       # Rename field
       config["risk"]["stop_loss_percent"] = config["risk"].pop("stop_loss_pct")

       # Change type (example: bars to seconds)
       cooldown_bars = config["execution"]["cooldown_bars"]
       config["execution"]["cooldown_seconds"] = cooldown_bars * 60
       del config["execution"]["cooldown_bars"]

       # Add required field with computed default
       config["risk"]["position_scaling_enabled"] = False

       # Update version
       config["schema_version"] = "2.0"
       return config
   ```

3. **Update `migrate_schema()` to call the new migration**:
   ```python
   def migrate_schema(config: Dict[str, Any]) -> Dict[str, Any]:
       version = config.get("schema_version", "1.0")

       if version == "1.0":
           config = migrate_1_0_to_1_1(config)
           return migrate_schema(config)  # Recursive chaining

       elif version == "1.1":
           config = migrate_1_1_to_2_0(config)
           return migrate_schema(config)  # Continue chain

       elif version == "2.0":
           # Current version
           return config

       else:
           raise ValueError(f"Unknown schema version: {version}")
   ```

**Testing Requirements for Migrations**:

Create tests in `tests/test_schema_migrations.py`:

```python
def test_v1_0_loads_without_safety_limits():
    """Old JSON files without safety_limits should load successfully"""
    config = {
        "schema_version": "1.0",
        "strategy": {"name": "ma_crossover", "parameters": {}},
        "ticker": "AAPL",
        "timeframe": "1Day",
        "risk": {...},
        "execution": {...},
        "metadata": {...}
        # safety_limits is missing
    }

    migrated = migrate_schema(config)
    assert "safety_limits" in migrated
    assert migrated["safety_limits"]["max_trades_per_day"] == 20

def test_migration_chain():
    """Migrations should chain correctly: 1.0 → 1.1 → 2.0"""
    config = {"schema_version": "1.0", ...}

    migrated = migrate_schema(config)
    assert migrated["schema_version"] == "2.0"  # Final version
    # Verify transformations from all migrations
    assert "stop_loss_percent" in migrated["risk"]  # From 1.0→2.0
    assert "retry_failed_orders" in migrated["execution"]  # From 1.0→1.1

def test_unknown_version_raises_error():
    """Unknown schema versions should raise clear error"""
    config = {"schema_version": "99.0", ...}

    with pytest.raises(ValueError, match="Unknown schema version: 99.0"):
        migrate_schema(config)
```

**Migration Release Checklist**:

When releasing a breaking schema change (major version bump):

1. ✅ Write `migrate_X_to_Y()` function in `schema_migrations.py`
2. ✅ Add migration tests in `tests/test_schema_migrations.py`
3. ✅ Update `schema_version` in `strategy_schema.py` Literal
4. ✅ Update Pydantic model validators for new schema
5. ✅ Update this CLAUDE.md documentation
6. ✅ Deploy AlphaLab with matching migration first (export side)
7. ✅ Re-export all strategies from AlphaLab
8. ✅ Test C1 signal parity after migration (backtest vs live)
9. ✅ Deploy AlphaLive (import side)
10. ✅ Verify old JSON files auto-upgrade successfully

**IMPORTANT**: Always deploy AlphaLab before AlphaLive when introducing schema changes. This ensures exported strategies use the new schema before AlphaLive expects it.

**Safety_Limits Defaults (v13 Backward Compatibility)**:

Current default values applied when `safety_limits` block is missing:
```python
{
    "max_trades_per_day": 20,
    "max_api_calls_per_hour": 500,
    "signal_generation_timeout_seconds": 5.0,
    "broker_degraded_mode_threshold_failures": 3
}
```

This allows pre-v13 strategy exports to continue working without re-export.

## Timeframes

Supported timeframes (defined in `Timeframe` Literal):
- `1Day`: Daily bars
- `1Hour`: Hourly bars
- `15Min`: 15-minute bars

## Order Types

Supported order types (defined in `OrderType` Literal):
- `market`: Market orders (immediate execution at current price)
- `limit`: Limit orders (execution at specified price or better)
  - Uses `limit_offset_pct` to calculate limit price from signal price

## Example Strategy Configuration

```json
{
  "schema_version": "1.0",
  "strategy": {
    "name": "ma_crossover",
    "parameters": {
      "fast_period": 10,
      "slow_period": 20
    },
    "description": "Fast/slow MA crossover with trend confirmation"
  },
  "ticker": "AAPL",
  "timeframe": "1Day",
  "risk": {
    "stop_loss_pct": 2.0,
    "take_profit_pct": 5.0,
    "max_position_size_pct": 10.0,
    "max_daily_loss_pct": 3.0,
    "max_open_positions": 5,
    "portfolio_max_positions": 10,
    "trailing_stop_enabled": false,
    "trailing_stop_pct": 3.0,
    "commission_per_trade": 0.0
  },
  "execution": {
    "order_type": "market",
    "limit_offset_pct": 0.1,
    "cooldown_bars": 1
  },
  "safety_limits": {
    "max_trades_per_day": 20,
    "max_api_calls_per_hour": 500,
    "signal_generation_timeout_seconds": 5.0,
    "broker_degraded_mode_threshold_failures": 3
  },
  "metadata": {
    "exported_from": "AlphaLab",
    "exported_at": "2026-03-05T12:00:00Z",
    "alphalab_version": "0.2.0",
    "backtest_id": "bt_abc123",
    "backtest_period": {
      "start": "2020-01-01",
      "end": "2024-12-31"
    },
    "performance": {
      "sharpe_ratio": 1.45,
      "sortino_ratio": 1.82,
      "total_return_pct": 32.5,
      "max_drawdown_pct": -12.3,
      "win_rate_pct": 58.2,
      "profit_factor": 1.75,
      "total_trades": 47,
      "calmar_ratio": 2.64
    }
  }
}
```

## Development Guidelines

### Schema Modifications

**DO**:
- Add new optional fields with sensible defaults
- Add new strategies to the `StrategyName` enum
- Add validation warnings for edge cases
- Test backward compatibility with old JSON files

**DON'T**:
- Remove or rename existing fields without a major version bump
- Change field types without a migration path
- Add required fields without defaults and migration
- Modify validation ranges without consulting backtest results

### Testing Strategy Imports

1. Create a test JSON file matching the schema
2. Load it using `load_strategy_with_defaults(json_data)`
3. Verify all validators pass
4. Check that warnings are logged for risky parameters
5. Verify missing `safety_limits` gets defaults applied

## How to Run Locally

### Setup

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment** (copy .env.example to .env):
   ```bash
   cp .env.example .env
   # Edit .env with your API keys
   ```

3. **Validate strategy JSON**:
   ```bash
   python -c "from alphalive.config import load_config; load_config('configs/example_strategy.json')"
   ```

### Run in Dry Run Mode (Recommended First)

```bash
export DRY_RUN=true
export STRATEGY_CONFIG=configs/example_strategy.json
python run.py --config configs/example_strategy.json
```

This logs trades without executing them — perfect for testing signal generation.

### Run with Paper Trading

```bash
export DRY_RUN=false
export ALPACA_PAPER=true
export STRATEGY_CONFIG=configs/example_strategy.json
python run.py --config configs/example_strategy.json
```

### Run Tests

```bash
# All tests
pytest tests/ -v

# Specific test file
pytest tests/test_risk_manager.py -v

# With coverage
pytest tests/ --cov=alphalive --cov-report=html
```

## How to Deploy to Railway

**See [SETUP.md](SETUP.md) for detailed deployment guide.**

### Quick Deploy

1. **Push to GitHub**:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git push -u origin main
   ```

2. **Create Railway project**:
   - Go to railway.app/new
   - Select "Deploy from GitHub repo"
   - Choose this repository

3. **Set environment variables** in Railway dashboard:
   - `ALPACA_API_KEY`
   - `ALPACA_SECRET_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `STRATEGY_CONFIG=configs/your_strategy.json`
   - `ALPACA_PAPER=true` (start with paper trading!)
   - `DRY_RUN=false`

4. **Deploy**:
   - Railway auto-builds from Dockerfile
   - Check logs for "AlphaLive initialized"
   - Verify Telegram startup notification

### Railway Configuration Files

- **Dockerfile**: Builds Python 3.11 image with dependencies
- **railway.toml**: Deploy config (restart policy, health checks)
- **Procfile**: Defines worker process (`python run.py --config configs/strategy.json`)

## Environment Variables Reference

### Required

| Variable | Description | Example |
|----------|-------------|---------|
| `ALPACA_API_KEY` | Alpaca API key | `PK...` |
| `ALPACA_SECRET_KEY` | Alpaca secret key | `xxx...` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | `123456:ABC-DEF...` |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID | `123456789` |
| `STRATEGY_CONFIG` | Path to strategy JSON | `configs/ma_crossover.json` |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `ALPACA_PAPER` | `true` | Use paper trading |
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets` | Alpaca API base URL |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `DRY_RUN` | `false` | Log trades without executing |
| `TRADING_PAUSED` | `false` | Pause trading temporarily |
| `STATE_FILE` | `/tmp/alphalive_state.json` | State persistence file |
| `HEALTH_PORT` | `8080` | Health check HTTP port |
| `HEALTH_SECRET` | `""` | Health check secret token |
| `PERSISTENT_STORAGE` | `false` | Enable persistent storage (Railway volumes) |

### Local Development Only

In production (Railway), **do NOT use .env files**. Set all variables in Railway dashboard.

For local development, use `.env` file (loaded by `python-dotenv`):

```bash
# .env (local only)
ALPACA_API_KEY=your_key
ALPACA_SECRET_KEY=your_secret
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
STRATEGY_CONFIG=configs/example_strategy.json
LOG_LEVEL=DEBUG
DRY_RUN=true
```

## Code Conventions

### Style

- **Formatting**: Follow PEP 8
- **Line Length**: 100 characters max
- **Imports**: Group into stdlib, third-party, local (use `isort`)
- **Type Hints**: Use type hints for all function signatures
- **Docstrings**: Google-style docstrings for all modules, classes, functions

### Logging

- **Never use print()**: Always use `logging` module
- **Log Levels**:
  - `DEBUG`: Detailed diagnostic info (signal checks, bar fetches)
  - `INFO`: Normal operation (trade executions, position updates)
  - `WARNING`: Unexpected but recoverable (risk limit rejections)
  - `ERROR`: Serious issues (API failures, order placement failures)
  - `CRITICAL`: System-level failures (unrecoverable errors)

**Example**:
```python
import logging
logger = logging.getLogger(__name__)

logger.debug("Fetching 200 bars for AAPL")
logger.info(f"Order placed: BUY 10 AAPL @ market")
logger.warning(f"Trade blocked: {reason}")
logger.error(f"API call failed: {e}", exc_info=True)
```

### Error Handling

- **Catch specific exceptions**: Avoid bare `except:`
- **Log exceptions with traceback**: Use `exc_info=True`
- **Graceful degradation**: Don't crash the main loop on recoverable errors
- **Send Telegram notifications**: Alert on critical errors

**Example**:
```python
try:
    order = broker.place_order(...)
except BrokerAPIError as e:
    logger.error(f"Order placement failed: {e}", exc_info=True)
    notifier.send_error_notification(str(e))
    return False
except Exception as e:
    logger.critical(f"Unexpected error: {e}", exc_info=True)
    notifier.send_error_notification(f"CRITICAL: {e}")
    raise
```

### Testing

AlphaLive has a comprehensive test suite with **100+ tests** covering all core functionality. All tests run without any API keys or network access by mocking external dependencies.

#### Test Suite Overview

| Test File | Tests | Purpose |
|-----------|-------|---------|
| `test_config.py` | 8 | Configuration loading, validation, env vars |
| `test_indicators.py` | 10 | Technical indicator calculations |
| `test_signal_engine.py` | 12 | Signal generation for all 5 strategies |
| `test_risk_manager.py` | 27 | Risk limits, position sizing, circuit breakers |
| `test_order_manager.py` | 19 | Order execution, duplicate prevention, slippage |
| `test_telegram_notifier.py` | 19 | Telegram notifications, retry logic, graceful degradation |
| `test_integration.py` | 5 | End-to-end workflows (signal → risk → order) |
| **Total** | **100** | **Complete coverage** |

#### Running Tests

**Run all tests**:
```bash
pytest tests/ -v
```

**Run specific test file**:
```bash
pytest tests/test_risk_manager.py -v
```

**Run with coverage**:
```bash
pytest tests/ --cov=alphalive --cov-report=html
open htmlcov/index.html  # View coverage report
```

**Run specific test**:
```bash
pytest tests/test_config.py::test_load_valid_strategy_json -v
```

**Run tests matching pattern**:
```bash
pytest tests/ -k "risk_manager" -v
```

#### Shared Fixtures (`conftest.py`)

Shared fixtures provide reusable test data and mocks:

**Configuration Fixtures**:
- `sample_strategy_config`: Loaded example strategy (StrategySchema instance)
- `sample_strategy_dict`: Strategy config as dictionary (for manipulation)
- `sample_app_config_dict`: Application config dictionary (env vars)

**Mock Fixtures**:
- `mock_broker`: Mocked broker with standard responses (orders, positions, account)
- `mock_telegram`: Mocked Telegram notifier (all methods return True)
- `mock_market_data`: Mocked market data fetcher (returns sample bars)

**Data Fixtures**:
- `sample_bars`: Generic OHLCV DataFrame (50 bars, uptrend)
- `ma_crossover_bars`: Bars with golden cross pattern (fast SMA crosses above slow)
- `rsi_oversold_bars`: Bars with RSI < 30 (strong downtrend)
- `rsi_overbought_bars`: Bars with RSI > 70 (strong uptrend)
- `momentum_breakout_bars`: Bars with breakout + volume surge

**Other Fixtures**:
- `sample_position`: Mock Position instance
- `sample_account_state`: Account state dictionary

#### Mocking Patterns

**External Dependencies Mocked**:
1. **Broker API** (`mock_broker`):
   - `get_account()` → Returns Account with $100k equity
   - `get_position()` → Returns None (no position)
   - `get_all_positions()` → Returns empty list
   - `place_market_order()` → Returns filled Order
   - `is_market_open()` → Returns True

2. **Telegram API** (`mock_telegram`):
   - All `send_*` methods return True
   - `is_offline()` returns False

3. **Market Data** (`mock_market_data`):
   - `get_latest_bars()` → Returns 200-bar DataFrame
   - `get_current_price()` → Returns $150.0

**Mocking Example**:
```python
def test_execute_signal_dry_run(sample_strategy_dict, mock_broker, mock_telegram):
    """Test signal execution in dry run mode."""
    from alphalive.strategy_schema import StrategySchema
    from alphalive.execution.risk_manager import RiskManager
    from alphalive.execution.order_manager import OrderManager

    config = StrategySchema(**sample_strategy_dict)
    rm = RiskManager(config.risk, config.execution, "test_strategy")
    om = OrderManager(
        broker=mock_broker,
        risk_manager=rm,
        execution_config=config.execution,
        notifier=mock_telegram,
        dry_run=True  # Dry run mode
    )

    result = om.execute_signal(
        ticker="AAPL",
        signal={"signal": "BUY", "confidence": 0.8, "reason": "Test"},
        current_price=150.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0
    )

    # Should succeed but NOT call broker
    assert result["status"] == "success"
    assert not mock_broker.place_market_order.called  # Dry run shouldn't place orders
```

#### Test Isolation

- **No shared state**: Each test runs in isolation
- **No API keys required**: All external APIs mocked
- **No network calls**: Tests run offline
- **Deterministic**: Same input → same output (no randomness)

#### Test Coverage Goals

- **Unit Tests**: >80% coverage on core modules
- **Integration Tests**: Full workflows tested end-to-end
- **Edge Cases**: Errors, exceptions, boundary conditions
- **Regression Tests**: Prevent bugs from reappearing

#### Continuous Integration

Tests are designed to run in CI/CD pipelines (GitHub Actions, Railway, etc.):

```yaml
# .github/workflows/test.yml
name: Test Suite
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - uses: actions/setup-python@v2
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: pytest tests/ --cov=alphalive --cov-report=xml
      - uses: codecov/codecov-action@v2  # Upload coverage
```

#### Test Data Strategy

**Deterministic Fixtures**: All test data is generated programmatically with known patterns:
- **MA Crossover**: 30 bars below, then crosses above (golden cross at bar 31)
- **RSI Oversold**: Strong downtrend (price drops 1.0/bar) → RSI < 30
- **RSI Overbought**: Strong uptrend (price rises 1.0/bar) → RSI > 70
- **Momentum Breakout**: 40 bars flat, then breakout with 2x volume surge
- **Empty DataFrame**: Edge case testing (doesn't crash)

**Known Values**: Tests verify calculations match expected values:
```python
# SMA example: [100, 101, 102] with period=3
# Expected SMA: (100+101+102)/3 = 101.0
assert df['sma_3'].iloc[2] == pytest.approx(101.0)
```

This ensures indicators produce correct results, not just "some value".

#### Testing Philosophy

1. **Fast**: All tests run in <10 seconds (no network, no sleep)
2. **Isolated**: No dependencies on external services
3. **Comprehensive**: Every function has tests
4. **Maintainable**: Clear names, simple logic, good fixtures
5. **Reliable**: Same result every time (no flaky tests)

#### Simulated Trading Day Tests (`test_simulated_day.py`)

**Purpose**: The most critical tests for confidence before deploying to Railway. These tests simulate a full trading day minute-by-minute WITHOUT any real API calls.

**Location**: `tests/test_simulated_day.py`

**Test Count**: 10 comprehensive scenario tests

**Key Features**:
- **Mock Clock**: Advance time minute-by-minute to simulate entire trading day
- **Zero API Calls**: All external dependencies mocked (broker, market data, Telegram)
- **End-to-End Validation**: Verify bot behavior at every stage of the trading day

**Scenarios Covered**:

1. **`test_full_trading_day`** — Main simulation from 6 AM to 4:05 PM:
   - 6:00 AM: Market closed, bot sleeping
   - 9:30 AM: Market opens, bot detects open
   - 9:35 AM: Morning signal check fires, BUY signal generated, order placed
   - 9:40 AM: Exit check runs, no exits needed
   - 10:00 AM: Price drops, stop loss hit, sell order placed
   - 10:05 AM: Exit check, no positions
   - 2:00 PM: Exit check, all quiet
   - 3:55 PM: EOD summary sent via Telegram
   - 4:00 PM: Market closes
   - 4:05 PM: Bot sleeping, no API calls

2. **`test_weekend_behavior`** — Saturday 10 AM:
   - Verify bot sleeps 30 minutes, makes zero API calls
   - Market closed detection works correctly

3. **`test_holiday_behavior`** — Weekday holiday:
   - Market closed all day (e.g., Christmas)
   - Bot handles gracefully (no crashes, no trades)
   - No error alerts sent (this is normal)

4. **`test_morning_signal_error_recovery`** — Error handling:
   - `market_data.get_latest_bars()` throws exception
   - Error caught, Telegram error alert sent
   - Bot continues running (doesn't crash)
   - Exit checks still run later

5. **`test_broker_connection_loss`** — Connection error recovery:
   - `broker.get_account()` throws `ConnectionError`
   - Error caught, logged, Telegram notified
   - Bot retries on next cycle
   - Connection restored, trading resumes

6. **`test_daily_loss_limit_halt`** — Daily loss limit enforcement:
   - Morning BUY, price tanks, stop loss exit
   - Daily P&L exceeds `max_daily_loss_pct`
   - Next signal blocked with "Daily loss limit hit"
   - Telegram notified of halt

7. **`test_max_positions_limit`** — Max positions enforcement:
   - 5 open positions (at max limit)
   - BUY signal generated
   - Trade blocked with reason logged
   - Telegram notified

8. **`test_dry_run_no_orders`** — Dry run mode verification:
   - Run full day simulation with `dry_run=True`
   - `broker.place_market_order()` is NEVER called
   - All signals still logged
   - Telegram still gets alerts

9. **`test_sigterm_handling`** — Graceful shutdown:
   - Send SIGTERM signal to process
   - Shutdown message sent to Telegram with daily stats
   - Process exits cleanly with code 0

10. **`test_consecutive_loss_circuit_breaker`** — Circuit breaker:
    - 3 consecutive losses recorded
    - Trading paused automatically
    - Next signal blocked with "Circuit breaker triggered"
    - Telegram alert sent

**Mock Patterns Used**:

```python
# Mock Clock
class MockClock:
    def __init__(self, start_time):
        self.current_time = start_time

    def advance(self, minutes=1):
        """Advance clock by N minutes."""
        self.current_time += timedelta(minutes=minutes)

    def now(self, tz=None):
        """Return current mock time."""
        if tz:
            return self.current_time.replace(tzinfo=tz)
        return self.current_time

# Usage
mock_clock = MockClock(datetime(2024, 3, 11, 6, 0, 0, tzinfo=ET))
mock_clock.advance(210)  # Jump to 9:30 AM
```

**Mocked Components**:

1. **Broker**:
   - `is_market_open()` → Returns True/False based on mock clock
   - `get_account()` → Returns mock Account
   - `get_position()` → Returns mock Position or None
   - `place_market_order()` → Returns mock Order

2. **Market Data**:
   - `get_latest_bars()` → Returns pre-built DataFrame with golden cross pattern
   - `get_current_price()` → Returns controlled price (150.0, 146.0, etc.)

3. **Telegram**:
   - All `send_*` methods → Return True
   - Verify correct alerts sent at right times

4. **Clock** (`datetime.now()`):
   - Patched with `mock_clock.now(ET)`
   - Allows stepping through time minute-by-minute

**Running Simulated Day Tests**:

```bash
# Run all simulated day tests
pytest tests/test_simulated_day.py -v

# Run specific scenario
pytest tests/test_simulated_day.py::test_full_trading_day -v

# Run with verbose output
pytest tests/test_simulated_day.py -v -s
```

**Why These Tests Matter**:

1. **Confidence**: If these tests pass, the bot will work correctly on Railway
2. **Coverage**: Tests every stage of the trading day (open, signal, exit, close, weekend)
3. **Error Handling**: Verifies bot recovers from errors gracefully (doesn't crash)
4. **Risk Limits**: Validates all circuit breakers and limits work as expected
5. **Dry Run**: Confirms dry run mode logs but never places orders
6. **Shutdown**: Ensures clean shutdown on SIGTERM (Railway restarts)

**Before Deploying to Railway**:

```bash
# Run all tests
pytest tests/ -v

# Run simulated day tests specifically
pytest tests/test_simulated_day.py -v

# If all tests pass → SAFE TO DEPLOY
```

These tests simulate days, weeks, and months of trading in seconds, giving you confidence the bot will behave correctly in production.

### Signal Parity Verification (Mini-Checkpoint)

**CRITICAL**: Run this checkpoint after completing B4 (indicators + signal_engine) and BEFORE proceeding to B5.

**Purpose**: Verify that AlphaLive generates the EXACT same signals as AlphaLab backtest on identical historical data. Signal parity is critical - if live signals don't match backtest signals, your live results won't match backtest expectations.

**Location**: `scripts/mini_checkpoint.py`

**Prerequisites**:
1. Complete B4 implementation (indicators.py + signal_engine.py)
2. Export 500-bar AAPL fixture from AlphaLab: `tests/fixtures/aapl_fixture_500bars.csv`
3. Run backtests in AlphaLab for all 5 strategies on the fixture
4. Export expected signals to `tests/fixtures/expected_signals_{strategy_name}.csv`

**Running the checkpoint**:
```bash
python scripts/mini_checkpoint.py
```

**Expected output**:
```
==============================================================
Mini-Checkpoint: Signal Parity Verification
==============================================================

Loaded fixture: 500 bars (AAPL 2022-2023)

Testing ma_crossover... ✓ 47 signals, 0 mismatches
Testing rsi_mean_reversion... ✓ 23 signals, 0 mismatches
Testing momentum_breakout... ✓ 31 signals, 0 mismatches
Testing bollinger_breakout... ✓ 19 signals, 0 mismatches
Testing vwap_reversion... ✓ 28 signals, 0 mismatches

==============================================================
✅ PASS: All strategies match. Proceed to B5.
==============================================================
```

**Pass Criteria**:
- Exit code 0
- All 5 strategies show "0 mismatches"
- No warnings about missing fixtures or expected signals

**If Failed**:
1. **Check parameter names**: AlphaLab vs AlphaLive keys must match EXACTLY
   - Example: `confirmation_bars` (not `confirm_bars`)

2. **Verify indicator calculations**: Compare intermediate values at mismatch bars
   - Add debug logging to both AlphaLab and AlphaLive
   - Check SMA, RSI, Bollinger values

3. **Audit signal logic**: Line-by-line comparison of strategy logic
   - Bollinger breakout: confirmation_bars rolling window logic
   - VWAP reversion: deviation calculation
   - MA crossover: crossover detection (prev vs curr)

4. **Check warmup handling**: Both systems must skip the same warmup bars

**Do NOT proceed to B5** until mini-checkpoint passes with 0 mismatches. Signal parity is non-negotiable.

See `tests/fixtures/README.md` for detailed instructions on generating fixture data and expected signals.

### Git Workflow

1. **Feature Branches**: Create branch for each feature/fix
   ```bash
   git checkout -b feature/trailing-stop-loss
   ```

2. **Commit Messages**: Use conventional commits
   ```
   feat: Add trailing stop loss support
   fix: Correct position size calculation for fractional shares
   docs: Update CLAUDE.md with new architecture
   test: Add tests for risk manager daily limits
   ```

3. **Pull Requests**: Always PR to main, include:
   - Description of changes
   - Test results
   - Updated CLAUDE.md if architecture changed

### Adding a New Strategy

1. **Update `StrategyName` Literal** in `strategy_schema.py`:
   ```python
   StrategyName = Literal[
       "ma_crossover",
       "rsi_mean_reversion",
       "momentum_breakout",
       "bollinger_breakout",
       "vwap_reversion",
       "volume_breakout"  # New strategy
   ]
   ```

2. **Implement signal logic** in `signal_engine.py`:
   ```python
   def _volume_breakout_signal(self, bars: pd.DataFrame) -> Optional[Signal]:
       """Volume breakout strategy."""
       threshold = self.params.get("volume_threshold", 2.0)
       avg_volume = bars["volume"].rolling(20).mean()
       current_volume = bars["volume"].iloc[-1]

       if current_volume > avg_volume.iloc[-1] * threshold:
           return Signal(action="buy", price=bars["close"].iloc[-1], ...)

       return None
   ```

3. **Route in `generate_signal()`**:
   ```python
   elif self.strategy_name == "volume_breakout":
       return self._volume_breakout_signal(bars)
   ```

4. **Add tests** in `tests/test_signal_engine.py`

5. **Update CLAUDE.md** with new strategy description

### Modifying Risk Limits

**DO NOT** modify risk limits without careful consideration:

1. **Backtest first**: Validate new limits in AlphaLab
2. **Update schema**: Modify validators in `strategy_schema.py`
3. **Test thoroughly**: Ensure limits are enforced in `risk_manager.py`
4. **Document warnings**: Update CLAUDE.md with rationale
5. **Coordinate with AlphaLab**: Keep export/import schemas in sync

## Security Hardening (B16)

### Overview

AlphaLive includes comprehensive security measures to prevent credential leaks and unauthorized access before production deployment.

**Security layers**:
1. Security audit script (`scripts/security_audit.sh`)
2. Telegram command rate limiting (10 commands/min)
3. Telegram chat_id authentication
4. Health endpoint secret authentication
5. Security test suite (`tests/test_security.py`)
6. API key rotation procedures
7. Pre-commit hook (optional)

### Security Audit Script

**Location**: `scripts/security_audit.sh`

**Purpose**: Automated security checks before every deployment.

**Usage**:
```bash
./scripts/security_audit.sh
```

**Exit codes**:
- `0` — All checks passed, safe to deploy
- `1` — Security issues found, fix before deploying

**Checks performed**:
1. **Git history scan**: No API keys in git history
2. **Config file scan**: No hardcoded secrets in `configs/`
3. **Gitignore check**: `.env` file properly ignored
4. **HEALTH_SECRET validation**: Set and strong (≥16 chars)
5. **Telegram authentication**: Commands check `chat_id`
6. **Rate limiting**: Telegram commands have rate limits
7. **Git tracking check**: `.env` not tracked by git

**Example output**:
```
==================================
AlphaLive Security Audit
==================================

[1] Checking git history for leaked credentials...
✓ No credentials in git history

[2] Checking config files for hardcoded secrets...
✓ No hardcoded secrets in configs/

[3] Checking .env is gitignored...
✓ .env properly gitignored

[4] Checking HEALTH_SECRET is configured...
✓ HEALTH_SECRET configured and strong

[5] Checking Telegram command authentication...
✓ Telegram commands check chat_id

[6] Checking Telegram command rate limiting...
✓ Telegram command rate limiting implemented

[7] Checking .env is not tracked by git...
✓ .env not tracked by git

==================================
✅ Security audit passed
Safe to deploy to production
```

**Integration with deployment workflow**:
```bash
# Before every Railway deployment:
./scripts/security_audit.sh
if [ $? -eq 0 ]; then
    git push origin main  # Railway auto-deploys
else
    echo "Fix security issues before deploying"
    exit 1
fi
```

### Telegram Command Rate Limiting

**Location**: `alphalive/notifications/telegram_commands.py`

**Purpose**: Prevent command spam/abuse if bot token is leaked.

**Implementation**:
```python
class TelegramCommandListener:
    def __init__(self, ...):
        # Rate limiting (prevent command spam/abuse)
        self.command_timestamps = defaultdict(list)
        self.rate_limit_window = 60  # seconds
        self.rate_limit_max = 10  # commands per window

    def _handle_command(self, text: str):
        # Rate limiting check
        now = time.time()
        self.command_timestamps[self.chat_id] = [
            ts for ts in self.command_timestamps[self.chat_id]
            if now - ts < self.rate_limit_window
        ]

        if len(self.command_timestamps[self.chat_id]) >= self.rate_limit_max:
            logger.warning(f"Rate limit exceeded for chat_id {self.chat_id}")
            self.notifier.send_message(
                "⚠️ Rate limit exceeded. Max 10 commands per minute."
            )
            return

        self.command_timestamps[self.chat_id].append(now)

        # ... handle command ...
```

**Rate limit**: 10 commands per 60-second window

**User experience**: If exceeded, user sees:
```
⚠️ Rate limit exceeded

Maximum 10 commands per minute.
Please wait before sending more commands.
```

**Why**: Even if bot token is leaked, attacker can only send 10 commands/minute, limiting damage.

### API Key Rotation Procedures

#### Rotating Alpaca API Keys (Zero Downtime)

**Frequency**: Every 90 days (recommended)

**Steps**:
1. Alpaca dashboard → API Keys → Generate NEW paper trading keys
2. **Keep old keys active** during transition
3. Railway Variables → Update `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`
4. Railway auto-deploys (~30s downtime)
5. Verify health endpoint responds
6. Delete old keys from Alpaca dashboard
7. Document rotation with date

**Rollback**: Restore old values in Railway Variables if new keys fail.

#### Rotating Telegram Bot Token

**Important**: Telegram tokens cannot be rotated — you must create a new bot.

**Steps**:
1. @BotFather → `/newbot` → get new token
2. Get new `chat_id`: `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Railway Variables → Update `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
4. Railway restarts (~30s)
5. Verify new bot responds to `/status`
6. @BotFather → `/deletebot` → revoke old bot

### Security Test Suite

**Location**: `tests/test_security.py`

**Tests** (6 total):

1. **`test_no_secrets_in_configs()`**
   - Scans `configs/` for hardcoded API keys
   - Patterns: Alpaca API keys, Telegram tokens
   - Fails if secrets found in JSON files

2. **`test_telegram_commands_check_chat_id()`**
   - Verifies chat_id check exists in source code
   - Verifies unauthorized commands are logged
   - Ensures only configured chat can send commands

3. **`test_telegram_rate_limiting()`**
   - Sends 10 commands (should succeed)
   - Sends 11th command (should be rate limited)
   - Verifies rate limit message sent

4. **`test_env_file_not_in_git()`**
   - Checks `.env` is in `.gitignore`
   - Verifies `.env` not tracked by git
   - Prevents credential leaks via git

5. **`test_health_endpoint_requires_secret()`**
   - Verifies `X-Health-Secret` header check exists
   - Verifies 401 Unauthorized response for wrong secret
   - Verifies 503 response when `HEALTH_SECRET` not set

6. **`test_no_print_statements_with_secrets()`**
   - Scans source code for `print()` statements
   - Enforces use of `logger` instead
   - Prevents accidental credential logging

**Running security tests**:
```bash
pytest tests/test_security.py -v
```

**All tests must pass before production deployment.**

### Pre-Commit Hook (Optional)

**Location**: `.git/hooks/pre-commit`

**Purpose**: Block commits containing API keys.

**Setup**:
```bash
#!/bin/bash
# Prevent committing secrets

if git diff --cached | grep -iE "(APCA-API|sk_[a-zA-Z0-9]{32}|[0-9]{10}:[A-Za-z0-9_-]{35})"; then
    echo "❌ COMMIT BLOCKED: Detected API key in staged changes"
    echo "   Remove secrets and use environment variables"
    exit 1
fi

exit 0
```

**Make executable**:
```bash
chmod +x .git/hooks/pre-commit
```

**Test**:
```bash
echo "APCA-API-KEY-ID: PKTEST123" > test.txt
git add test.txt
git commit -m "test"  # Should be blocked
```

### Credential Leak Response

**If credentials committed to git**:
1. **Immediately** revoke keys (Alpaca dashboard / @BotFather)
2. Generate new keys
3. Update Railway Variables
4. Remove from git history:
   ```bash
   pip install git-filter-repo
   git filter-repo --path-match <file> --invert-paths
   git reflog expire --expire=now --all
   git gc --prune=now --aggressive
   ```
5. Force push to remote
6. Rotate ALL API keys
7. Document incident

**If exposed publicly**:
1. Revoke keys **immediately**
2. Generate new keys
3. Monitor account for unauthorized activity
4. Enable 2FA on all accounts

### Security Best Practices

**Development**:
- Never commit API keys to git
- Use `.env` file locally (gitignored)
- Use Railway Variables in production
- Run security audit before deploying

**Production**:
- Rotate API keys every 90 days
- Enable 2FA on Alpaca, Railway, GitHub
- Monitor Railway logs for suspicious activity
- Keep HEALTH_SECRET strong (32+ chars)
- Review Telegram command logs regularly

**Deployment checklist**:
- [ ] `./scripts/security_audit.sh` exits 0
- [ ] `pytest tests/test_security.py` passes
- [ ] All API keys in Railway Variables
- [ ] `.env` not tracked by git
- [ ] `HEALTH_SECRET` set and strong
- [ ] Telegram `chat_id` verified

## File Structure Reference

```
alphalive/
├── alphalive/                      # Main package
│   ├── __init__.py                 # Package init (version info)
│   ├── main.py                     # 24/7 trading loop (AlphaLive class)
│   ├── config.py                   # Load/validate configs + env vars
│   ├── strategy_schema.py          # Pydantic v2 models
│   │
│   ├── broker/                     # Broker integrations
│   │   ├── __init__.py
│   │   ├── base_broker.py          # Abstract interface (Position, Order, Account)
│   │   └── alpaca_broker.py        # Alpaca implementation (alpaca-py)
│   │
│   ├── strategy/                   # Signal generation
│   │   ├── __init__.py
│   │   ├── signal_engine.py        # 5 strategy implementations
│   │   └── indicators.py           # Technical indicators (SMA, RSI, etc.)
│   │
│   ├── execution/                  # Orders and risk
│   │   ├── __init__.py
│   │   ├── order_manager.py        # Place/track/cancel orders
│   │   └── risk_manager.py         # Position sizing, limits, stop loss
│   │
│   ├── notifications/              # Alerts
│   │   ├── __init__.py
│   │   └── telegram_bot.py         # httpx → Telegram Bot API
│   │
│   ├── data/                       # Market data
│   │   ├── __init__.py
│   │   └── market_data.py          # Fetch bars from broker
│   │
│   ├── utils/                      # Utilities
│   │   ├── __init__.py
│   │   └── logger.py               # STDOUT logging config
│   │
│   └── migrations/                 # Schema versioning
│       ├── __init__.py
│       └── schema_migrations.py    # migrate_schema() function
│
├── configs/                        # Strategy JSON files
│   └── example_strategy.json       # MA crossover example
│
├── tests/                          # Test suite
│   ├── __init__.py
│   ├── conftest.py                 # Shared fixtures (mock_broker, sample_bars, etc.)
│   ├── test_config.py              # Config loading tests
│   ├── test_indicators.py          # Indicator calculation tests
│   ├── test_signal_engine.py       # Signal generation tests
│   ├── test_risk_manager.py        # Risk limit enforcement tests
│   ├── test_order_manager.py       # Order execution tests
│   └── test_integration.py         # End-to-end workflow tests
│
├── run.py                          # Entry point (loads .env, calls main())
├── requirements.txt                # Pinned dependencies
├── Dockerfile                      # Railway Docker build
├── Procfile                        # Railway worker definition
├── railway.toml                    # Railway deploy config
├── .env.example                    # Env var template (for local dev)
│
├── README.md                       # User-facing documentation
├── SETUP.md                        # Railway deployment guide
└── CLAUDE.md                       # THIS FILE - Dev guide
```

## Entry Point (run.py)

**Location**: `/run.py`

**Purpose**: Command-line entry point for AlphaLive with argument parsing, validation mode, and banner display.

### CLI Arguments

```python
python run.py [OPTIONS]

Options:
  --config PATH         Path to strategy JSON (default: STRATEGY_CONFIG env var)
  --dry-run             Log trades without executing (default: DRY_RUN env var)
  --validate-only       Test config and connections, then exit
```

### Validation Mode (`--validate-only`)

**Purpose**: Test everything before running live. Recommended first step after deployment.

**Flow**:
```
1. Load strategy JSON → validate schema
2. Load environment variables → validate required keys
3. Connect to Alpaca broker → print account status
4. Fetch market data → verify API access
5. Generate test signal → verify signal engine works
6. Exit with code 0 if all pass, code 1 if any fail
```

**Example**:
```bash
$ python run.py --validate-only
Validating configuration and connections...

✅ Configuration valid

Testing Alpaca connection...
✅ Broker connection successful
   Account equity: $100,000.00

Testing market data...
✅ Market data OK (200 bars fetched for AAPL)

Testing signal generation...
✅ Signal generation OK
   Test signal: BUY
   Warmup complete: True

=============================================================================
✅ ALL VALIDATIONS PASSED
=============================================================================

Ready to run:
  python run.py --config configs/example_strategy.json
```

### Startup Banner

**Purpose**: Visual confirmation of configuration and mode.

**Example Output**:
```
================================================================================
  █████╗ ██╗     ██████╗ ██╗  ██╗ █████╗ ██╗     ██╗██╗   ██╗███████╗
 ██╔══██╗██║     ██╔══██╗██║  ██║██╔══██╗██║     ██║██║   ██║██╔════╝
 ███████║██║     ██████╔╝███████║███████║██║     ██║██║   ██║█████╗
 ██╔══██║██║     ██╔═══╝ ██╔══██║██╔══██║██║     ██║╚██╗ ██╔╝██╔══╝
 ██║  ██║███████╗██║     ██║  ██║██║  ██║███████╗██║ ╚████╔╝ ███████╗
 ╚═╝  ╚═╝╚══════╝╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝  ╚══════╝
================================================================================
  Config: configs/ma_crossover.json
  Mode: PAPER TRADING
  Platform: Railway
================================================================================
```

**Live Trading Warning**:
```
⚠️  ⚠️  ⚠️  WARNING ⚠️  ⚠️  ⚠️
⚠️  LIVE TRADING MODE — REAL MONEY AT RISK  ⚠️
⚠️  ⚠️  ⚠️  WARNING ⚠️  ⚠️  ⚠️
```

### Paper vs Live Control

**IMPORTANT**: Paper/live is controlled by `ALPACA_PAPER` environment variable, **NOT** a CLI flag.

**Rationale**: On Railway, you can't accidentally pass `--live` flag. You must explicitly set `ALPACA_PAPER=false` in dashboard, which requires intentional action and triggers a process restart.

**Usage**:
```bash
# Paper trading (safe)
export ALPACA_PAPER=true
python run.py

# Live trading (real money)
export ALPACA_PAPER=false
python run.py
```

### Error Handling

- **Config validation failure**: Exit code 1 (Railway will restart)
- **Broker connection failure**: Exit code 1
- **Market data failure**: Exit code 1
- **Keyboard interrupt**: Exit code 0 (clean shutdown)
- **Unexpected exception**: Exit code 1, traceback printed

### Integration with main.py

```python
from alphalive.main import main as run_main
from alphalive.utils.logger import setup_logger

# Setup logging
setup_logger()

# Run main loop
try:
    run_main(
        config_path=args.config,
        dry_run=args.dry_run,
        paper=paper
    )
except KeyboardInterrupt:
    print("Shutting down...")
    sys.exit(0)
except Exception as e:
    print(f"Fatal error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
```

---

## Documentation Files

AlphaLive has **EXACTLY 3 markdown files**:
1. **CLAUDE.md** (this file) — Developer guide
2. **README.md** — User-facing documentation
3. **SETUP.md** — Railway deployment guide

**No `docs/` folder** — keeps repo simple and files discoverable.

### README.md Structure

**Location**: `/README.md`

**Purpose**: User-facing documentation for getting started with AlphaLive.

**Sections**:
1. **How It Works**: AlphaLab → Railway workflow (5 steps)
2. **Architecture**: High-level component diagram, signal timing, exit monitoring
3. **Local Development**: Prerequisites, setup steps, CLI options
4. **Deploy to Railway**: Quick steps, link to SETUP.md
5. **Environment Variables**: Required and optional vars with descriptions
6. **Safety Features**: Risk management, circuit breakers, operational safety
7. **Strategies Supported**: 5 strategies with descriptions and parameters
8. **Multi-Strategy Mode**: How to run multiple strategies simultaneously
9. **Telegram Notifications**: Example messages, graceful degradation
10. **Logs**: Example log output, Railway vs local
11. **Troubleshooting**: Common errors and fixes
12. **Contributing**: Link to AlphaLab, how to contribute
13. **Disclaimer**: Trading risk warning

**Target Audience**: End users who want to deploy and use AlphaLive.

### SETUP.md Structure

**Location**: `/SETUP.md`

**Purpose**: Step-by-step Railway deployment guide.

**Sections**:
1. **Prerequisites**: What you need before starting
2. **Step 1: Get Your API Keys**: Alpaca and Telegram setup
3. **Step 2: Prepare Your Strategy**: Export from AlphaLab, validate locally
4. **Step 3: Deploy to Railway**: GitHub deployment flow, environment variables
5. **Multi-Strategy Mode**: How to run multiple strategies from one deployment
6. **Resource Requirements**: Railway plans, CPU/memory usage
7. **Cost Breakdown**: Paper vs live trading costs (~$5-20/month)
8. **Production Checklist**: What to verify before live trading
9. **Enable Live Trading**: How to switch from paper to live
10. **Monitoring**: Daily, weekly, monthly review checklists
11. **Troubleshooting**: Common deployment issues and fixes
12. **Pausing & Resuming**: Kill switch usage
13. **Updating Your Strategy**: How to deploy config changes
14. **Stopping the Bot**: How to shut down
15. **Logs & Debugging**: Viewing logs, log levels, downloading logs
16. **Advanced: Persistent Storage**: Using Railway volumes

**Target Audience**: Users deploying to Railway for the first time.

---

## Environment Variables Reference

AlphaLive is configured **100% via environment variables** (no .env files in production).

### Environment Variable → AppConfig Mapping

| Environment Variable | AppConfig Field | Type | Default | Required | Description |
|---------------------|-----------------|------|---------|----------|-------------|
| `ALPACA_API_KEY` | `broker.api_key` | str | — | ✅ | Alpaca API key (from alpaca.markets) |
| `ALPACA_SECRET_KEY` | `broker.secret_key` | str | — | ✅ | Alpaca secret key |
| `ALPACA_PAPER` | `broker.paper` | bool | `true` | ❌ | Use paper trading (recommended for testing) |
| `ALPACA_BASE_URL` | `broker.base_url` | str | *auto* | ❌ | Auto-set based on `ALPACA_PAPER` flag |
| `TELEGRAM_BOT_TOKEN` | `telegram.bot_token` | str | `None` | ❌ | Telegram bot token (from @BotFather) |
| `TELEGRAM_CHAT_ID` | `telegram.chat_id` | str | `None` | ❌ | Your Telegram chat ID |
| `STRATEGY_CONFIG` | — | str | `configs/strategy.json` | ❌ | Path to strategy JSON file |
| `STRATEGY_CONFIG_DIR` | — | str | `None` | ❌ | Path to directory of strategy JSONs (multi-strategy mode) |
| `LOG_LEVEL` | `log_level` | str | `INFO` | ❌ | Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL) |
| `DRY_RUN` | `dry_run` | bool | `false` | ❌ | Log trades without executing |
| `TRADING_PAUSED` | `trading_paused` | bool | `false` | ❌ | Pause all trading (kill switch) |
| `STATE_FILE` | `state_file` | str | `/tmp/alphalive_state.json` | ❌ | State persistence file path |
| `HEALTH_PORT` | `health_port` | int | `8080` | ❌ | Health check HTTP port (Railway uses this) |
| `HEALTH_SECRET` | `health_secret` | str | `""` | ❌ | Health check secret token |
| `PERSISTENT_STORAGE` | `persistent_storage` | bool | `false` | ❌ | Enable persistent storage (Railway volumes) |

### Boolean Parsing

Strings are parsed as `True` if they match (case-insensitive):
- `"true"`
- `"1"`
- `"yes"`
- `"on"`

All other values are parsed as `False`.

**Examples**:
```python
ALPACA_PAPER=true     → True
ALPACA_PAPER=TRUE     → True
ALPACA_PAPER=1        → True
ALPACA_PAPER=yes      → True
ALPACA_PAPER=false    → False
ALPACA_PAPER=0        → False
ALPACA_PAPER=         → False (empty string)
```

### Environment Variable Sources (Priority Order)

1. **OS environment variables** (Railway production) — highest priority
2. **.env file** (local development only) — fallback
3. **CLI arguments** (`--dry-run`, `--config`) — override for specific invocation

**Railway**: Set variables in **Variables** tab → automatic restart on change

**Local**: Use `.env` file (loaded by `python-dotenv`) or export manually

### API Key Masking

API keys are masked in logs for security:
```python
app_config.broker.mask_api_key()      # Returns "****2345"
app_config.broker.mask_secret_key()   # Returns "****7890"
```

**Example log output**:
```
2026-03-09 09:30:00 [INFO] alphalive.config: Loaded environment config
2026-03-09 09:30:00 [INFO] alphalive.config:   Broker: Alpaca (paper=true)
2026-03-09 09:30:00 [INFO] alphalive.config:   API Key: ****2345
2026-03-09 09:30:00 [INFO] alphalive.config:   Telegram: Enabled (chat_id=****1234)
```

### Single vs Multi-Strategy Mode

**Single Strategy Mode**:
```bash
STRATEGY_CONFIG=configs/ma_crossover.json
```
→ Loads one strategy JSON

**Multi-Strategy Mode**:
```bash
STRATEGY_CONFIG_DIR=configs/
```
→ Loads all `.json` files from directory

**Priority**: If both are set, `STRATEGY_CONFIG_DIR` takes precedence.

---

## Railway Deployment

### Deployment Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Railway Deployment Flow                      │
└─────────────────────────────────────────────────────────────────────┘

1. Push to GitHub
   ├─ git add .
   ├─ git commit -m "Deploy strategy"
   └─ git push origin main

2. Railway Auto-Detects Changes
   ├─ Webhook from GitHub triggers build
   └─ Railway reads Dockerfile

3. Build Docker Image
   ├─ FROM python:3.11-slim
   ├─ COPY requirements.txt .
   ├─ RUN pip install -r requirements.txt
   ├─ COPY alphalive/ ./alphalive/
   ├─ COPY configs/ ./configs/
   ├─ COPY run.py .
   └─ CMD ["python", "run.py"]

4. Inject Environment Variables
   ├─ Railway injects variables from dashboard
   └─ AlphaLive reads via os.environ.get()

5. Start Worker Process
   ├─ Railway runs: python run.py
   ├─ run.py calls main() from alphalive/main.py
   └─ Bot enters 24/7 loop

6. Health Checks (optional)
   ├─ Railway pings http://localhost:8080/health every 60s
   └─ If unhealthy for 5 minutes, restart

7. Logs Streaming
   ├─ All stdout/stderr captured by Railway
   ├─ Viewable in dashboard → Deployments → Logs
   └─ Logs retained for 7 days (Starter plan)

8. On Git Push
   ├─ Railway detects new commit
   ├─ Starts new build
   ├─ Graceful shutdown of old process (SIGTERM)
   ├─ Start new process
   └─ Zero-downtime deploy (~30-60 seconds)
```

### Dockerfile

**Location**: `/Dockerfile`

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY alphalive/ ./alphalive/
COPY configs/ ./configs/
COPY run.py .

# Railway will inject environment variables
# CMD will be overridden by Procfile if present
CMD ["python", "run.py"]
```

**Key Points**:
- Uses official Python 3.11 slim image (minimal size)
- Installs dependencies from requirements.txt
- Copies only necessary files (no tests, no .git)
- Runs `python run.py` by default
- Environment variables injected by Railway at runtime

### Procfile

**Location**: `/Procfile`

```
worker: python run.py
```

**Purpose**: Tells Railway this is a **worker process** (not a web service).

**Difference from Web Service**:
- **Web service**: Exposes HTTP port, receives traffic, has URL
- **Worker process**: Background job, no public HTTP endpoint, runs 24/7

**Railway Detection**:
- If `Procfile` exists and contains `worker:` → Railway runs as worker
- If `Procfile` missing → Railway uses `CMD` from Dockerfile

### railway.toml

**Location**: `/railway.toml`

```toml
[build]
builder = "dockerfile"
dockerfilePath = "Dockerfile"

[deploy]
restartPolicyType = "on_failure"
restartPolicyMaxRetries = 3
healthcheckPath = "/health"
healthcheckTimeout = 300

[env]
PYTHONUNBUFFERED = "1"
```

**Key Settings**:
- **builder**: Use Dockerfile (not Nixpacks)
- **restartPolicyType**: Restart on failure, not on success exit
- **restartPolicyMaxRetries**: Retry up to 3 times, then give up
- **healthcheckPath**: Ping `/health` endpoint (optional)
- **healthcheckTimeout**: Wait 300s for healthy response
- **PYTHONUNBUFFERED**: Force Python to write logs immediately (don't buffer)

### Railway Restart Behavior

**Triggers**:
1. **Code push** → Graceful restart (SIGTERM sent, waits 30s, SIGKILL)
2. **Environment variable change** → Graceful restart
3. **Manual restart** via dashboard → Graceful restart
4. **Crash** (exit code 1) → Automatic restart (up to 3 times)
5. **Health check failure** → Automatic restart

**SIGTERM Handling** (in main.py):
```python
import signal

def sigterm_handler(signum, frame):
    logger.info("Received SIGTERM — shutting down gracefully...")
    notifier.send_shutdown_notification(daily_stats)
    sys.exit(0)

signal.signal(signal.SIGTERM, sigterm_handler)
```

**Graceful Shutdown Flow**:
```
1. Railway sends SIGTERM to process
2. Python catches signal via signal.signal()
3. sigterm_handler() logs shutdown
4. Send Telegram shutdown notification with daily stats
5. sys.exit(0) terminates process
6. Railway starts new process with updated code/config
```

### Health Checks (Optional)

AlphaLive can expose a simple HTTP health check endpoint for Railway monitoring.

**Implementation** (in main.py):
```python
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

# Start health check server in background thread
health_server = HTTPServer(("0.0.0.0", app_config.health_port), HealthCheckHandler)
health_thread = threading.Thread(target=health_server.serve_forever, daemon=True)
health_thread.start()
logger.info(f"Health check server listening on port {app_config.health_port}")
```

**Usage**: Railway pings `http://localhost:8080/health` every 60 seconds. If unhealthy for 5 minutes, Railway restarts the service.

### Persistent Storage (Railway Volumes)

By default, AlphaLive uses `/tmp/alphalive_state.json` for state persistence. This is lost on restarts.

**To persist state across restarts**:
1. Create Railway volume at `/data`
2. Set `STATE_FILE=/data/alphalive_state.json`
3. Set `PERSISTENT_STORAGE=true`

**State file contents**:
```json
{
  "today_str": "2026-03-09",
  "morning_check_done": true,
  "eod_summary_sent": false,
  "last_position_reconciliation": 1710000000,
  "daily_stats": {
    "trades": 5,
    "pnl": 450.0,
    "win_rate": 60.0
  }
}
```

**Benefit**: Bot remembers state across Railway restarts (e.g., "did I already send EOD summary today?").

### Multi-Strategy Deployment

**Single Railway Service** can run **multiple strategies simultaneously**.

**Setup**:
1. Export multiple strategies from AlphaLab as JSON files
2. Place all in `configs/` directory
3. Set `STRATEGY_CONFIG_DIR=configs/` in Railway
4. Push to GitHub → Railway auto-deploys

**Execution**:
- Bot iterates through all strategies on each loop
- Each strategy has independent signal checks, risk limits, positions
- Global limits enforced across all strategies (`max_daily_loss_pct`, `portfolio_max_positions`)

**Resource Usage**:
- 1-3 strategies: Starter plan (shared vCPU, 512MB RAM)
- 4-10 strategies: Pro plan (dedicated vCPU, 2GB RAM)

---

## Related Files

- `alphalive/strategy_schema.py`: Pydantic v2 models for strategy configuration
- `alphalive/migrations/schema_migrations.py`: Schema migration system for backward compatibility
- `alphalive/config.py`: Loads and validates strategy JSON files (calls migrate_schema before validation)
- `alphalive/execution/risk_manager.py`: Enforces risk limits from the schema during live trading
- `alphalive/main.py`: 24/7 trading loop coordinator
- `run.py`: Entry point that loads env vars and starts main loop
- `README.md`: User-facing documentation (getting started, strategies, safety)
- `SETUP.md`: Railway deployment guide (step-by-step, troubleshooting)
- AlphaLab `backend/strategy_schema.py`: Mirror of this schema (export side)
- AlphaLab `backend/migrations/schema_migrations.py`: Mirror of migration system (export side)

---

## Production-Ready Status

**Status**: ✅ **PRODUCTION READY** (as of March 10, 2026)

**Audit Results**:
- 221 tests passing (176% of 80+ requirement)
- 95% pass rate (57/60 audit items)
- Security audit passing
- All critical safety features implemented

**Deployment Target**: Railway (Starter or Hobby plan)

**Expected Resource Usage**:
- Memory: 200-450 MB (under 512 MB limit)
- CPU: 1-5% active, spikes to 10-20% during signal generation
- Cost: ~$5.05/month (Starter) or $20/month (Hobby)

---

## Known Limitations

The following limitations are acceptable for MVP and documented for users:

### 1. Pattern Day Trader (PDT) Rule

**Limitation**: AlphaLive does NOT track day trade count.

**Impact**:
- Users with <$25k account must manually monitor day trades via Alpaca dashboard
- Exceeding 3 day trades in 5 business days triggers 90-day restriction
- No impact on paper trading or accounts ≥$25k

**Mitigation**:
- Use 1Day timeframe strategies (no day trades)
- Set `max_trades_per_day` conservatively
- Monitor `daytrade_count` in account status

**Documentation**: Explained in README.md "Known Limitations" section

### 2. No Metrics JSON File

**Limitation**: No persistent metrics file written to disk.

**Impact**:
- External monitoring tools cannot consume metrics
- Monitoring relies on Telegram alerts + Railway logs

**Current Monitoring**:
- Telegram: Trade alerts, position updates, daily summaries
- Railway Logs: All INFO+ level logs
- Health Endpoint: Basic status only (no memory usage)

**Mitigation**: Adequate for MVP; metrics file can be added if external monitoring needed

### 3. No Automatic Position Reconciliation Halt

**Limitation**: Bot does not auto-halt on position drift detection.

**Impact**:
- If manual trades placed outside bot, positions may drift
- Bot continues trading based on broker API (source of truth)

**Mitigation**:
- Don't place manual trades while bot is running
- Positions fetched from broker API on every check (always in sync)

**Future**: Can add drift detection + auto-halt if needed

### 4. Signal Checks Not Strictly Bar-Aligned

**Limitation**: Signal checks run every 30 seconds, not strictly aligned to bar close times.

**Impact**:
- 15Min strategy may check at :07, :37 instead of :00, :15, :30, :45
- Signal only fires after warmup complete, so no false signals

**Mitigation**: Warmup prevents trading on incomplete bars

**Current Behavior**: Adequate for all timeframes; strict alignment can be added if needed

### 5. No Advanced Order Types

**Limitation**: Market and limit orders only.

**Impact**:
- No stop-limit, trailing-stop-limit, OCO, or bracket orders
- Trailing stop implemented via polling (check_exits every 5 min)

**Mitigation**: Polling-based trailing stop works adequately for intraday timeframes

### 6. Railway Memory Limit (Starter Plan)

**Limitation**: Railway Starter plan has 512 MB memory limit.

**Impact**:
- Running 4+ strategies may exceed limit
- Each strategy adds ~100-150 MB

**Mitigation**:
- Starter plan: Run 1-3 strategies
- Hobby plan ($20/month): Run 4+ strategies
- Or split across multiple Railway deployments

---

## Deployment Pre-Flight Checklist

Before deploying to production:

**Configuration**:
- [ ] Strategy JSON validated locally
- [ ] `scripts/verify_deployment.py` passes
- [ ] `scripts/security_audit.sh` passes
- [ ] All tests passing: `pytest tests/ -v`

**Railway Setup**:
- [ ] ALPACA_API_KEY set
- [ ] ALPACA_SECRET_KEY set
- [ ] ALPACA_PAPER=true (start with paper trading!)
- [ ] TELEGRAM_BOT_TOKEN set
- [ ] TELEGRAM_CHAT_ID set
- [ ] HEALTH_SECRET generated (min 16 chars)
- [ ] STRATEGY_CONFIG or STRATEGY_CONFIG_DIR set

**Safety**:
- [ ] Start with paper trading (ALPACA_PAPER=true)
- [ ] Monitor for 1 week before going live
- [ ] Test Telegram commands (/status, /pause, /resume)
- [ ] Verify health endpoint responds
- [ ] Check Railway logs for errors

**Going Live**:
- [ ] Set ALPACA_PAPER=false in Railway
- [ ] Use live API keys (not paper keys)
- [ ] Start with small position sizes (e.g., max_position_size_pct=5%)
- [ ] Monitor closely for first 24 hours
- [ ] Keep Telegram notifications enabled

---

## Support & Maintenance

**For Issues**:
- Check SETUP.md "Troubleshooting" section
- Review Railway logs
- Run `scripts/verify_deployment.py` locally
- Open GitHub issue with error logs

**For Updates**:
- Pull latest code from main branch
- Run `pytest tests/` to verify
- Re-run `scripts/security_audit.sh`
- Deploy via `git push` (Railway auto-deploys)

**For Questions**:
- Review CLAUDE.md (this file) for architecture
- Review README.md for high-level overview
- Review SETUP.md for deployment guide
