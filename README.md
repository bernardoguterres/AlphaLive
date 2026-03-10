# AlphaLive

**24/7 live trading execution engine** for strategies exported from AlphaLab.

Export a backtested strategy from AlphaLab → deploy to Railway → it trades automatically. Get Telegram alerts for every trade and daily summaries.

---

## How It Works

1. **Backtest strategies in AlphaLab** until you find ones you like
2. **Click "Export to AlphaLive"** → saves a JSON config with your strategy parameters
3. **Commit the JSON** to `configs/` in this repo
4. **Push to GitHub** → Railway auto-deploys
5. **AlphaLive runs 24/7**: sleeps when market is closed, trades when open
6. **Get Telegram alerts** for every trade, exit, and daily summary

---

## Architecture

AlphaLive is a production-grade trading bot with:

- **Signal Generation**: Replicates AlphaLab strategy logic exactly (5 strategies supported)
- **Risk Management**: Stop loss, take profit, trailing stop, position sizing, daily limits
- **Order Execution**: Alpaca Markets API with retry logic, slippage checks, partial fill handling
- **Market Data**: Real-time bars from Alpaca with caching and staleness detection
- **Notifications**: Telegram alerts for trades, exits, errors, daily summaries
- **Resilience**: Auto-restart on Railway, position reconciliation, corporate action detection

### Components

```
┌─────────────────────────────────────────────────────────────┐
│                     AlphaLive (Railway)                     │
├─────────────────────────────────────────────────────────────┤
│  Main Loop (24/7)                                           │
│    ↓                                                        │
│  Market Data Fetcher (Alpaca) → Signal Engine → Risk Mgr   │
│    ↓                                                        │
│  Order Manager → Alpaca Broker → Positions                 │
│    ↓                                                        │
│  Telegram Notifier → Your Phone                            │
└─────────────────────────────────────────────────────────────┘
```

**Market Closed Behavior**:
- Checks if market is open every 30 seconds
- Sleeps efficiently when closed (no wasted API calls)
- Wakes up at 9:30 AM ET and starts trading

**Signal Timing**:
- **1Day strategies**: Check once per day at 9:35 AM ET
- **1Hour strategies**: Check every hour at :00 minutes
- **15Min strategies**: Check every 15 minutes (:00, :15, :30, :45)

**Exit Monitoring**:
- Checks stop loss / take profit every 5 minutes during market hours
- Corporate action detection (skips trading on 20% overnight moves)
- End-of-day summary sent at 3:55 PM ET

---

## Local Development

### Prerequisites

- Python 3.11+
- Alpaca Markets account (free paper trading account)
- Telegram bot (optional, for notifications)

### Setup

1. **Clone the repo**:
   ```bash
   git clone https://github.com/yourusername/AlphaLive.git
   cd AlphaLive
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment**:
   ```bash
   cp .env.example .env
   # Edit .env with your API keys
   ```

4. **Create a strategy config** or use the example:
   ```bash
   # configs/example_strategy.json already exists
   # Or export from AlphaLab to configs/
   ```

5. **Validate configuration** (recommended first step):
   ```bash
   python run.py --validate-only
   ```

   This tests:
   - ✅ Strategy JSON is valid
   - ✅ Alpaca connection works
   - ✅ Market data fetch works
   - ✅ Signal generation works

6. **Run in dry-run mode** (recommended for testing):
   ```bash
   python run.py --dry-run
   ```

   This logs trades without executing them. Perfect for testing signal logic.

7. **Run with paper trading**:
   ```bash
   python run.py
   ```

   Default is paper trading (`ALPACA_PAPER=true`). Safe for testing with fake money.

### CLI Options

```bash
python run.py [OPTIONS]

Options:
  --config PATH         Path to strategy JSON (default: STRATEGY_CONFIG env var)
  --dry-run             Log trades without executing (for testing)
  --validate-only       Test config and connections, then exit
```

---

## Deploy to Railway

**See [SETUP.md](SETUP.md) for detailed deployment guide.**

Quick steps:

1. **Create Railway account**: [railway.app](https://railway.app)
2. **Create new project** → Deploy from GitHub repo
3. **Set environment variables**:
   - `ALPACA_API_KEY`
   - `ALPACA_SECRET_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `STRATEGY_CONFIG=configs/your_strategy.json`
   - `ALPACA_PAPER=true` (start with paper trading!)
4. **Deploy** → Railway auto-builds and runs 24/7

**Cost**: ~$5/month on Railway Starter plan (500 hours included).

---

## Environment Variables

### Required

| Variable | Description | Example |
|----------|-------------|---------|
| `ALPACA_API_KEY` | Alpaca API key (get from alpaca.markets) | `PK...` |
| `ALPACA_SECRET_KEY` | Alpaca secret key | `xxx...` |
| `STRATEGY_CONFIG` | Path to strategy JSON file | `configs/ma_crossover.json` |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `ALPACA_PAPER` | `true` | Use paper trading (recommended for testing) |
| `TELEGRAM_BOT_TOKEN` | `None` | Telegram bot token (for notifications) |
| `TELEGRAM_CHAT_ID` | `None` | Your Telegram chat ID |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `DRY_RUN` | `false` | Log trades without executing |
| `TRADING_PAUSED` | `false` | Pause trading (kill switch) |

### Getting API Keys

**Alpaca Markets**:
1. Sign up at [alpaca.markets](https://alpaca.markets)
2. Go to **Your API Keys** in dashboard
3. Generate new paper trading keys
4. Copy API Key and Secret Key

**Telegram Bot**:
1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow prompts
3. Copy bot token (looks like `123456:ABC-DEF...`)
4. Start a chat with your bot
5. Get your chat ID by visiting: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
6. Send a message to your bot, then refresh the URL above — your chat ID is in the response

---

## Safety Features

AlphaLive has multiple layers of protection:

### Risk Management

- **Stop Loss**: Automatically close positions at configured loss threshold
- **Take Profit**: Lock in gains at target price
- **Trailing Stop**: Follow price up, exit on pullback (optional)
- **Position Sizing**: Max % of account per position (prevents overexposure)
- **Daily Loss Limit**: Halt all trading if daily loss exceeds threshold
- **Max Positions**: Limit simultaneous open positions

### Circuit Breakers

- **Consecutive Loss Breaker**: Pause trading after 3 stop-outs in a row
- **Kill Switch**: Set `TRADING_PAUSED=true` in Railway to halt immediately
- **Corporate Action Detection**: Skip trading on 20% overnight moves (stock splits, etc.)
- **Position Drift Auto-Halt**: Halts if Alpaca positions don't match bot's internal tracking

### Operational Safety

- **Data Staleness Checks**: Won't trade on old data (market may be closed)
- **Startup Warmup Validation**: Ensures indicators are ready before first trade
- **Rate Limiting**: Exponential backoff prevents API bans
- **Graceful Degradation**: Telegram failures don't crash trading
- **SIGTERM Handling**: Clean shutdown on Railway restarts

### Live Trading Warnings

When you switch to live trading (`ALPACA_PAPER=false`), you'll see:

```
⚠️  ⚠️  ⚠️  WARNING ⚠️  ⚠️  ⚠️
⚠️  LIVE TRADING MODE — REAL MONEY AT RISK  ⚠️
⚠️  ⚠️  ⚠️  WARNING ⚠️  ⚠️  ⚠️
```

**Recommendation**: Run on paper for at least 1 week before switching to live.

---

## Strategies Supported

AlphaLive supports 5 strategies exported from AlphaLab:

### 1. MA Crossover
**Description**: Buy when fast SMA crosses above slow SMA, sell when it crosses below.

**Parameters**:
- `fast_period`: Fast SMA period (default: 10)
- `slow_period`: Slow SMA period (default: 20)

**Best For**: Trending markets, daily timeframes

---

### 2. RSI Mean Reversion
**Description**: Buy when RSI is oversold, sell when overbought.

**Parameters**:
- `period`: RSI period (default: 14)
- `oversold`: Oversold threshold (default: 30)
- `overbought`: Overbought threshold (default: 70)

**Best For**: Range-bound markets, intraday

---

### 3. Momentum Breakout
**Description**: Buy on new high with volume surge.

**Parameters**:
- `lookback`: Lookback period for rolling high (default: 20)
- `surge_pct`: Volume surge multiplier (default: 1.5)
- `atr_period`: ATR period for trailing stop (default: 14)

**Best For**: Volatile stocks, breakout plays

---

### 4. Bollinger Breakout
**Description**: Buy on consecutive closes above upper band with volume confirmation.

**Parameters**:
- `period`: Bollinger Bands period (default: 20)
- `std_dev`: Standard deviation multiplier (default: 2.0)
- `confirmation_bars`: Consecutive bars above/below band (default: 2)

**Best For**: Trend continuation, daily/hourly

---

### 5. VWAP Reversion
**Description**: Buy when price is far below VWAP and RSI is oversold, sell when far above and RSI is overbought.

**Parameters**:
- `deviation_threshold`: Deviation in standard deviations (default: 2.0)
- `rsi_period`: RSI period (default: 14)
- `oversold`: RSI oversold threshold (default: 30)
- `overbought`: RSI overbought threshold (default: 70)

**Best For**: Intraday mean reversion

---

## Multi-Strategy Mode

AlphaLive can run **multiple strategies simultaneously** by loading all JSONs from a directory:

1. **Export multiple strategies** from AlphaLab
2. **Place all JSONs** in `configs/` directory
3. **Set environment variable**:
   ```bash
   STRATEGY_CONFIG_DIR=configs/
   ```
4. **Deploy** → AlphaLive runs all strategies in parallel

**Risk Scope**:
- **Per-Strategy Limits**: `max_open_positions`, `stop_loss_pct`, `take_profit_pct`
- **Global Limits**: `max_daily_loss_pct` (halts ALL strategies), `portfolio_max_positions` (total positions across all)

**Example**: 3 strategies with `max_open_positions=[5,3,2]` → total potential = 10 positions, but `portfolio_max_positions=8` caps it at 8.

---

## Telegram Notifications

When configured, you'll receive:

- **Bot Started**: "🚀 AlphaLive Started" with strategy details
- **Trade Executed**: "🟢 BUY 66 AAPL @ $150.00"
- **Position Closed**: "💰 Position Closed — P&L: $495.00 (+5.00%)"
- **Stop Loss Hit**: "🛑 Stop loss triggered — AAPL -$300.00"
- **Daily Summary**: "📈 Daily Summary — 5 trades, $450 profit, 60% win rate"
- **Error Alerts**: "⚠️ Alpaca API timeout"
- **Circuit Breaker**: "⚠️ 3 consecutive losses — trading paused"

**Graceful Degradation**: If Telegram fails, trading continues (alerts are lost but trades still execute).

---

## Logs

AlphaLive logs to STDOUT in structured format:

```
2026-03-09 09:35:00 [INFO] alphalive.main: Market is open — running signal check
2026-03-09 09:35:01 [INFO] alphalive.data.market_data: Fetched 200 bars for AAPL (latest: 2026-03-09 09:34:00 EST)
2026-03-09 09:35:02 [INFO] alphalive.strategy.signal_engine: BUY signal: MA crossover (fast SMA crossed above slow SMA)
2026-03-09 09:35:03 [INFO] alphalive.execution.order_manager: MARKET BUY 66 AAPL @ market | Order ID: abc123-def456
2026-03-09 09:35:05 [INFO] alphalive.broker.alpaca_broker: Order filled: 66 shares @ $150.25
```

**Railway**: Logs are captured automatically and viewable in dashboard.

**Local**: Logs print to terminal.

---

## Troubleshooting

### "Invalid API key"
- Check that `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are correct
- Verify you're using **paper trading keys** (not live keys) if `ALPACA_PAPER=true`

### "Data is stale"
- Market may be closed (bot sleeps automatically)
- Check Alpaca status: [status.alpaca.markets](https://status.alpaca.markets)

### "Telegram offline — trading continues but alerts lost"
- Check `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are correct
- Verify bot is not blocked
- Bot will auto-retry every 10 minutes

### "Trade blocked: Daily loss limit exceeded"
- Bot has hit `max_daily_loss_pct` for the day
- Trading resumes next trading day automatically

### "Position drift detected — TRADING HALTED"
- Alpaca positions don't match bot's internal tracking
- Manually reconcile positions in Alpaca dashboard
- Set `TRADING_PAUSED=false` to resume

---

## Contributing

AlphaLive is part of the Alpha trading suite:

- **AlphaLab**: Backtest strategies, export to AlphaLive
- **AlphaLive**: Execute strategies 24/7 on Railway (this repo)

For questions, issues, or contributions, open an issue on GitHub.

---

## Known Limitations

### Pattern Day Trader (PDT) Rule

**What is it**: SEC regulation requiring $25,000 minimum account balance for accounts that execute 4+ day trades within 5 business days.

**How it affects AlphaLive**:
- **Paper Trading**: No PDT restrictions (unlimited day trades)
- **Live Trading with <$25k**:
  - Limited to 3 day trades per 5 business days
  - AlphaLive does NOT track day trade count
  - You must manually monitor via Alpaca dashboard
  - Exceeding limit results in 90-day trading restriction by your broker
- **Live Trading with ≥$25k**: No restrictions

**Recommended Strategies**:
- Use **1Day timeframe** strategies (no day trades)
- Monitor `daytrade_count` in Alpaca dashboard daily
- Set `max_trades_per_day` conservatively in strategy JSON
- Consider swing trading strategies (hold overnight)

**References**:
- [Alpaca PDT Guide](https://alpaca.markets/learn/pattern-day-trading/)
- [SEC PDT Rule](https://www.sec.gov/investor/pubs/daytrade.htm)

---

## License

MIT License — see LICENSE file for details.

---

## Disclaimer

**Trading involves substantial risk of loss. Past performance does not guarantee future results.**

- AlphaLive is provided "as is" without warranty
- You are responsible for your own trading decisions
- Always test on paper trading before using live funds
- Monitor your bot regularly
- Use appropriate position sizing and risk limits

**Use at your own risk.**
