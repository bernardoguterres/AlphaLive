# AlphaLive Railway Deployment Guide

Complete guide for deploying AlphaLive to Railway for 24/7 automated trading.

---

## Prerequisites

Before deploying, ensure you have:

1. ✅ **Railway Account**: Sign up at [railway.app](https://railway.app)
2. ✅ **Alpaca Markets Account**: Get API keys from [alpaca.markets](https://alpaca.markets)
3. ✅ **Telegram Bot** (optional): Create via [@BotFather](https://t.me/BotFather)
4. ✅ **GitHub Repository**: AlphaLive code pushed to GitHub
5. ✅ **Strategy JSON**: At least one strategy exported from AlphaLab

**Recommended**: Start with **paper trading** (`ALPACA_PAPER=true`) to test before risking real funds.

---

## Step 1: Get Your API Keys

### Alpaca API Keys

1. **Sign up** at [alpaca.markets](https://alpaca.markets)
2. Go to **Paper Trading** dashboard
3. Navigate to **"Your API Keys"**
4. Click **"Generate New Keys"**
5. Copy:
   - **API Key ID** (starts with `PK...`)
   - **Secret Key** (long alphanumeric string)

**Important**: Start with **paper trading keys** (not live). Paper trading is free and uses fake money.

### Telegram Bot Setup (Optional)

Telegram notifications are optional but highly recommended for real-time trade alerts.

1. **Create bot**:
   - Open Telegram
   - Search for **@BotFather**
   - Send `/newbot`
   - Follow prompts (choose a name and username)
   - Copy the **bot token** (looks like `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`)

2. **Get your chat ID**:
   - Start a chat with your bot (search for it in Telegram)
   - Send any message (e.g., "hello")
   - Visit this URL in browser (replace `<YOUR_BOT_TOKEN>`):
     ```
     https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
     ```
   - Look for `"chat":{"id":123456789}` in the JSON response
   - Copy the **chat ID** (the number, e.g., `123456789`)

---

## Step 2: Prepare Your Strategy

1. **Export strategy from AlphaLab** as JSON
2. **Save to `configs/` directory** in this repository:
   ```bash
   configs/
   ├── ma_crossover.json       # Your first strategy
   └── rsi_reversion.json      # Optional: second strategy
   ```

3. **Validate locally** (optional but recommended):
   ```bash
   python run.py --validate-only --config configs/ma_crossover.json
   ```

4. **Commit and push** to GitHub:
   ```bash
   git add configs/ma_crossover.json
   git commit -m "Add strategy config"
   git push
   ```

---

## Step 3: Deploy to Railway

### 3.1 Create Railway Project

1. Go to [railway.app/new](https://railway.app/new)
2. Click **"New Project"**
3. Select **"Deploy from GitHub repo"**
4. Choose your **AlphaLive** repository
5. Railway auto-detects the `Dockerfile` and builds the image

### 3.2 Configure Environment Variables

In Railway dashboard:

1. Click your project
2. Go to **"Variables"** tab
3. Click **"Add Variable"** and add the following:

#### Required Variables

```bash
ALPACA_API_KEY=PK...                          # From Alpaca dashboard
ALPACA_SECRET_KEY=your_secret_key             # From Alpaca dashboard
STRATEGY_CONFIG=configs/ma_crossover.json     # Path to your strategy JSON
```

#### Recommended Variables

```bash
ALPACA_PAPER=true                             # Start with paper trading!
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...          # From @BotFather
TELEGRAM_CHAT_ID=123456789                    # From getUpdates API
LOG_LEVEL=INFO                                # DEBUG for more detail
DRY_RUN=false                                 # Set to true for testing signal logic only
TRADING_PAUSED=false                          # Kill switch (set to true to halt)
```

#### Optional Variables

```bash
ALPACA_BASE_URL=https://paper-api.alpaca.markets    # Auto-set based on ALPACA_PAPER
STATE_FILE=/tmp/alphalive_state.json                # Default is fine
HEALTH_PORT=8080                                    # Port for health check endpoint
HEALTH_SECRET=<generate_random_32_char_string>      # Required for health checks (use openssl rand -hex 16)
PERSISTENT_STORAGE=false                            # Set to true if using Railway volumes
```

**Generating HEALTH_SECRET**:
```bash
# Generate a random 32-character hex string
openssl rand -hex 16
# Example output: a3f7b2c9d4e5f6g7h8i9j0k1l2m3n4o5

# Use this value for HEALTH_SECRET
HEALTH_SECRET=a3f7b2c9d4e5f6g7h8i9j0k1l2m3n4o5
```

### 3.3 Deploy

1. Railway **auto-deploys** when you push to GitHub
2. Go to **"Deployments"** tab to watch build progress
3. Click **"View Logs"** to see real-time output

**Expected logs**:
```
=================================================================
  █████╗ ██╗     ██████╗ ██╗  ██╗ █████╗ ██╗     ██╗██╗   ██╗███████╗
 ██╔══██╗██║     ██╔══██╗██║  ██║██╔══██╗██║     ██║██║   ██║██╔════╝
 ███████║██║     ██████╔╝███████║███████║██║     ██║██║   ██║█████╗
 ██╔══██║██║     ██╔═══╝ ██╔══██║██╔══██║██║     ██║╚██╗ ██╔╝██╔══╝
 ██║  ██║███████╗██║     ██║  ██║██║  ██║███████╗██║ ╚████╔╝ ███████╗
 ╚═╝  ╚═╝╚══════╝╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝  ╚══════╝
=================================================================
  Config: configs/ma_crossover.json
  Mode: PAPER TRADING
  Platform: Railway
=================================================================
```

### 3.4 Verify Deployment

**Check Telegram**:
- You should receive: **"🚀 AlphaLive Started"** with strategy details

**Check Railway Logs**:
- Look for: `✅ ALL VALIDATIONS PASSED`
- Look for: `Market is closed — sleeping until 9:30 AM ET` (if outside market hours)
- Look for: `Market is open — running signal check` (if during market hours)

---

## Railway Resource Limits & Cost

AlphaLive is optimized for Railway's shared infrastructure. Here's what to expect:

### Resource Usage

**Memory**:
- Expected: **200-450 MB**
- Breakdown:
  - Python runtime: ~150 MB
  - Pandas/NumPy: ~50-100 MB
  - Market data cache: ~50-100 MB
  - Indicators (200 bars): ~50-100 MB
- Peak usage during signal generation: **400-450 MB**
- Railway Starter plan limit: **512 MB** (you'll stay under this)

**CPU**:
- **Minimal** — sleeps when market is closed
- Active during market hours: **1-5% of vCPU**
- Signal generation spikes: **10-20% for <1 second**
- Railway allocation: Shared vCPU (sufficient for all strategies)

**Disk**:
- Code + dependencies: **~100 MB**
- Logs (rolled daily): **<50 MB**
- State file (if persistent storage): **<1 MB**
- Total: **~150 MB** (well under Railway's limits)

### Network/API Usage

**Typical API calls per day** (1 strategy on 1Day timeframe):
- Get bars: ~6/day (once at market open + 5 checks)
- Get account: ~80/day (every 5 min during market hours)
- Get positions: ~80/day (every 5 min for exit checks)
- Place orders: ~1-5/day (actual trades)
- **Total**: ~170-180 API calls/day

**Bandwidth**: <1 MB/day (JSON responses from Alpaca)

### Cost Estimate

**Railway Starter Plan** ($5/month):
- Includes: 500 execution hours/month
- AlphaLive uses: **~720 hours/month** (24/7 runtime)
- **Overage cost**: $0.000231/hour × 220 hours = **~$0.05/month**
- **Total**: **~$5.05/month**

**Railway Hobby Plan** ($20/month):
- Includes: Unlimited execution hours
- Best for: Running 24/7 without worrying about limits
- **Total**: **$20/month flat**

**Recommended**: Start with Starter plan ($5/month), upgrade to Hobby if needed.

### Performance Expectations

**Signal Generation**:
- MA Crossover: **<0.1 seconds**
- RSI Mean Reversion: **<0.2 seconds**
- Bollinger Breakout: **<0.3 seconds**
- Momentum Breakout: **<0.3 seconds**
- VWAP Reversion: **<0.4 seconds**

All well under the 5-second timeout budget.

**Order Execution**:
- Market orders: **<1 second** (Alpaca API response)
- Retry on failure: Up to 3 attempts with exponential backoff

**Reliability**:
- Railway uptime: **99.9%**
- Auto-restart on crash: **~15-30 seconds**
- Position reconciliation on restart: **Automatic**

### Multi-Strategy Resource Scaling

Running multiple strategies increases resource usage linearly:

| Strategies | Memory | CPU (Active) | API Calls/Day |
|-----------|---------|--------------|---------------|
| 1         | 250 MB  | 1-5%         | ~180          |
| 2         | 350 MB  | 2-8%         | ~360          |
| 3         | 450 MB  | 3-12%        | ~540          |
| 4-5       | ⚠️ 512 MB+ | 4-15%        | ~720-900      |

**Railway Starter Plan Limit**: 512 MB memory

**Recommendation**:
- **1-3 strategies**: Starter plan ($5/month)
- **4+ strategies**: Hobby plan ($20/month) or split across multiple deployments

---

## Multi-Strategy Mode

Run **multiple strategies simultaneously** from a single Railway deployment.

### Setup

1. **Export multiple strategies** from AlphaLab
2. **Place all JSONs** in `configs/` directory:
   ```bash
   configs/
   ├── ma_crossover_aapl.json
   ├── rsi_reversion_tsla.json
   └── momentum_breakout_spy.json
   ```

3. **Set environment variable** in Railway:
   ```bash
   STRATEGY_CONFIG_DIR=configs/
   ```
   (Remove or leave empty `STRATEGY_CONFIG` if using `STRATEGY_CONFIG_DIR`)

4. **Deploy** → Railway auto-detects all JSONs and runs strategies in parallel

### Risk Scope

**Per-Strategy Limits** (independent per strategy):
- `max_open_positions`: Each strategy can have up to N positions
- `stop_loss_pct`, `take_profit_pct`: Per-position limits
- `max_position_size_pct`: % of **total account equity** per position

**Global Limits** (enforced across ALL strategies):
- `max_daily_loss_pct`: If **total account** loses this %, **ALL strategies halt**
- `portfolio_max_positions`: Max **total** positions across all strategies

**Example**:
- 3 strategies with `max_open_positions=[5,3,2]` → potential 10 positions
- But `portfolio_max_positions=8` → caps at 8 total positions
- If account starts at $100k and `max_daily_loss_pct=3%` → if equity drops to $97k, **all strategies halt**

---

## Telegram Commands

**Plan**: Starter plan ($5/month, 500 hours included) is sufficient.

**Resources**:
- **CPU**: Shared vCPU (sufficient for 1-5 strategies)
- **Memory**: 512MB-1GB (AlphaLive uses ~200MB)
- **Storage**: Minimal (state file is <1MB)

**Scaling**:
- 1-3 strategies: Starter plan
- 4-10 strategies: Pro plan ($20/month, more resources)

### Alpaca API

**Rate Limits**:
- **Data API**: 200 requests/minute
- **Trading API**: 200 requests/minute

**AlphaLive API Usage** (per strategy):
- ~10 requests/hour during market hours (market data, position checks)
- ~2 requests/trade (order placement, status check)
- Well within Alpaca's limits for up to 5 strategies

---

## Cost Breakdown

### Paper Trading (Testing)

| Service | Cost |
|---------|------|
| Railway Starter | $5/month |
| Alpaca Paper Trading | Free |
| **Total** | **$5/month** |

### Live Trading (Production)

| Service | Cost |
|---------|------|
| Railway Starter (1-3 strategies) | $5/month |
| Railway Pro (4-10 strategies) | $20/month |
| Alpaca Live Trading | Free (commission-free trading) |
| **Total** | **$5-20/month** |

**Additional Costs**:
- **Margin**: If trading on margin, Alpaca charges interest (~8-12% annually)
- **Pattern Day Trader**: $25k minimum if making 4+ day trades in 5 days

---

## Production Checklist

Before switching to **live trading**, complete this checklist:

- [ ] ✅ Backtested strategy in AlphaLab with Sharpe > 1.0
- [ ] ✅ Ran in **DRY_RUN=true** mode for 24 hours (verify signal logic)
- [ ] ✅ Ran in **ALPACA_PAPER=true** mode for 1+ weeks (verify execution)
- [ ] ✅ Verified stop loss triggers correctly (check logs)
- [ ] ✅ Verified take profit triggers correctly (check logs)
- [ ] ✅ Confirmed Telegram notifications working
- [ ] ✅ No errors in Railway logs
- [ ] ✅ Reviewed all risk parameters (`stop_loss_pct`, `max_daily_loss_pct`, etc.)
- [ ] ✅ Started with small position sizes (1-5% of account per position)
- [ ] ✅ Set `max_daily_loss_pct` to conservative limit (2-3%)
- [ ] ✅ Monitored paper trading performance matches backtest expectations

**Only proceed to live trading if ALL boxes are checked.**

---

## Enable Live Trading

**⚠️ WARNING**: Live trading uses real money. Proceed only after completing production checklist.

### Steps

1. **Generate Live API Keys** in Alpaca:
   - Go to Alpaca **Live Trading** dashboard (separate from paper)
   - Generate new API keys (live keys are different from paper keys)
   - Copy new `API Key ID` and `Secret Key`

2. **Update Railway Variables**:
   ```bash
   ALPACA_PAPER=false
   ALPACA_API_KEY=<new_live_api_key>
   ALPACA_SECRET_KEY=<new_live_secret_key>
   DRY_RUN=false
   ```

3. **Railway Auto-Redeploys**:
   - Changes to variables trigger automatic redeploy (~30-60 seconds)

4. **Monitor Closely**:
   - Watch Railway logs for first 1 hour
   - Confirm Telegram notifications for first trade
   - Check Alpaca dashboard for positions
   - **Be ready to set `TRADING_PAUSED=true` if anything looks wrong**

---

## Monitoring

### Daily

- ✅ Check Railway logs for errors
- ✅ Review Telegram end-of-day summary (sent at 3:55 PM ET)
- ✅ Verify positions in Alpaca dashboard match bot's tracking
- ✅ Confirm no position drift alerts

### Weekly

- ✅ Analyze trading performance (P&L, win rate, Sharpe)
- ✅ Compare live results to backtest expectations
- ✅ Adjust risk parameters if needed (e.g., tighter stop loss)
- ✅ Review any error alerts or circuit breaker triggers

### Monthly

- ✅ Evaluate strategy performance (is it still working?)
- ✅ Re-backtest in AlphaLab with updated data
- ✅ Consider parameter optimization
- ✅ Review Railway costs and optimize if needed

---

## Security Best Practices

### Security Audit Script

**IMPORTANT**: Run security audit before EVERY deployment to production.

```bash
./scripts/security_audit.sh
```

**Exit codes**:
- `0` — All checks passed, safe to deploy
- `1` — Security issues found, fix before deploying

**What it checks**:
1. No API keys in git history
2. No hardcoded secrets in config files
3. `.env` file properly gitignored
4. `HEALTH_SECRET` configured and strong
5. Telegram commands check `chat_id`
6. Rate limiting implemented
7. `.env` not tracked by git

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

### Rotating Alpaca API Keys (Zero Downtime)

**When to rotate**:
- Every 90 days (recommended)
- If you suspect compromise
- When offboarding team members with access

**Steps**:
1. Log in to [Alpaca dashboard](https://alpaca.markets) → API Keys
2. Generate **NEW** paper trading key pair
   - ⚠️ **Keep old keys active** during transition
3. Railway dashboard → Variables → Update:
   ```
   ALPACA_API_KEY = <new key>
   ALPACA_SECRET_KEY = <new secret>
   ```
4. Railway auto-deploys with new keys (~30s downtime)
5. Verify health endpoint responds:
   ```bash
   curl -H "X-Health-Secret: $HEALTH_SECRET" https://your-app.railway.app/
   ```
6. Check Railway logs for "Alpaca connection successful"
7. **Delete old keys** from Alpaca dashboard
8. Document rotation in incident log with date

**Rollback procedure** (if new keys don't work):
1. Railway dashboard → Variables → Restore old values
2. Railway redeploys automatically
3. Investigate why new keys failed

### Rotating Telegram Bot Token

**IMPORTANT**: Telegram bot tokens **cannot be rotated**. You must create a new bot.

**When to rotate**:
- If bot token is leaked/compromised
- When offboarding team members with access

**Steps**:
1. Open Telegram → search for [@BotFather](https://t.me/botfather)
2. Send `/newbot` → follow prompts → get new token
3. Send `/start` to new bot to get chat_id:
   ```bash
   # Visit this URL to get chat_id:
   https://api.telegram.org/bot<NEW_BOT_TOKEN>/getUpdates
   ```
4. Railway dashboard → Variables → Update:
   ```
   TELEGRAM_BOT_TOKEN = <new token>
   TELEGRAM_CHAT_ID = <your chat id>
   ```
5. Railway restarts (~30s)
6. Verify new bot responds to `/status` command
7. **Revoke old bot** via @BotFather:
   - Send `/deletebot` → select old bot

**Note**: Old bot stops receiving commands immediately after token change.

### Generating Strong HEALTH_SECRET

**Generate a 32-character hex string**:
```bash
openssl rand -hex 32
```

**Example output**:
```
a3f7b2c9d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1
```

**Set in Railway**:
```
HEALTH_SECRET=a3f7b2c9d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1
```

**Security requirements**:
- Minimum 16 characters (32 recommended)
- Use only hex characters (0-9, a-f)
- Generate fresh secret (don't reuse from examples)
- Store in Railway Variables (never commit to git)

### Pre-Commit Hook (Optional but Recommended)

Prevents accidentally committing secrets to git.

**Create `.git/hooks/pre-commit`**:
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

**Test it**:
```bash
# Try to commit a file with a fake API key
echo "APCA-API-KEY-ID: PKABCDEF12345" > test.txt
git add test.txt
git commit -m "test"  # Should be blocked
```

### Security Checklist Before Production

- [ ] Run `./scripts/security_audit.sh` → exits 0
- [ ] All API keys stored in Railway Variables (not in code)
- [ ] `.env` file in `.gitignore` and not tracked
- [ ] `HEALTH_SECRET` set and strong (32+ chars)
- [ ] Telegram `chat_id` verified (only you can send commands)
- [ ] All tests pass: `pytest tests/test_security.py`
- [ ] No print() statements in production code (use logger)
- [ ] Review Railway logs for any credential leaks
- [ ] Pre-commit hook installed (optional)

### What to Do If Credentials Are Leaked

**If committed to git**:
1. **Immediately** revoke compromised keys (Alpaca dashboard / @BotFather)
2. Generate new keys
3. Update Railway Variables with new keys
4. Remove secrets from git history:
   ```bash
   # Option 1: git-filter-repo
   pip install git-filter-repo
   git filter-repo --path-match <file-with-secrets> --invert-paths

   # Option 2: BFG Repo Cleaner
   java -jar bfg.jar --delete-files <file-with-secrets>
   git reflog expire --expire=now --all
   git gc --prune=now --aggressive
   ```
5. Force push to remote (notify team)
6. Rotate all API keys (Alpaca + Telegram)
7. Document incident and lessons learned

**If exposed publicly**:
1. Revoke keys **immediately**
2. Generate new keys
3. Update Railway Variables
4. Monitor account for unauthorized activity
5. Enable 2FA on all accounts (Alpaca, Railway, GitHub)

### Rate Limiting

**Telegram commands**: Max 10 commands per minute per chat
- Prevents abuse if bot token is leaked
- User sees: "⚠️ Rate limit exceeded. Max 10 commands per minute."

**Broker API**: Alpaca has built-in rate limiting (200 requests/min)
- AlphaLive retries with exponential backoff
- No action needed from you

---

## Cost Safety Limits

AlphaLive includes configurable safety limits to prevent runaway trading costs from bugs, infinite loops, or market anomalies. These limits are configured per-strategy in the `safety_limits` block of your strategy JSON.

### Default Safety Limits

If not specified in your strategy JSON, these defaults are applied:

```json
{
  "safety_limits": {
    "max_trades_per_day": 20,
    "max_api_calls_per_hour": 500,
    "signal_generation_timeout_seconds": 5.0,
    "broker_degraded_mode_threshold_failures": 3
  }
}
```

### Configuring Safety Limits

Add or modify the `safety_limits` block in your strategy JSON:

```json
{
  "schema_version": "1.0",
  "strategy": {...},
  "ticker": "AAPL",
  "timeframe": "1Day",
  "risk": {...},
  "execution": {...},
  "safety_limits": {
    "max_trades_per_day": 15,
    "max_api_calls_per_hour": 400,
    "signal_generation_timeout_seconds": 5.0,
    "broker_degraded_mode_threshold_failures": 3
  }
}
```

### What Each Limit Does

#### 1. Max Trades Per Day

**Purpose**: Prevents runaway signal generation bugs

**Default**: 20 trades/day

**What happens when limit is hit**:
- Trading **auto-pauses** immediately (sets `trading_paused_manual = True`)
- Logs CRITICAL warning
- Sends Telegram alert: "🚨 EMERGENCY HALT - Max trades per day reached"
- Requires manual intervention to resume

**How to resume**:
- Option 1: Send `/resume` command via Telegram (after verifying no bug)
- Option 2: Set `TRADING_PAUSED=false` in Railway Variables (triggers restart)

**Recommended values by account size**:
- **Small account (<$10k)**: 10-15 trades/day
- **Medium account ($10k-$100k)**: 15-25 trades/day
- **Large account (>$100k)**: 20-40 trades/day

#### 2. Max API Calls Per Hour

**Purpose**: Protects against Alpaca rate limits (200 req/min = 12,000 req/hour)

**Default**: 500 calls/hour

**What happens**:
- **80% threshold**: Warning logged (no pause)
- **100% threshold**: Trading auto-pauses
- Counter **resets automatically** at top of each hour

**Typical API call usage per strategy**:
- Get account: ~1 call/min = 60/hour
- Get bars: ~6 calls/min (1Day timeframe) = 360/hour
- Place order: ~1 call per trade
- Get positions: ~1 call/min = 60/hour
- **Total for 1 strategy on 1Day timeframe**: ~400-500 calls/hour

**Recommended values**:
- **Single strategy**: 400-600 calls/hour
- **Multi-strategy (2-3 strategies)**: 800-1200 calls/hour total (distribute across strategies)

#### 3. Signal Generation Timeout

**Purpose**: Prevents blocking main loop with slow indicator calculations

**Default**: 5.0 seconds

**What happens when exceeded**:
- Signal generation is interrupted
- Logs ERROR warning
- Skips this cycle, tries again next time (doesn't crash)

**Recommended values**:
- **Simple strategies (MA crossover, RSI)**: 3-5 seconds
- **Complex strategies (multiple indicators)**: 5-10 seconds

**If you frequently hit timeout**:
1. Optimize indicator calculations (use vectorized pandas operations)
2. Reduce `lookback_bars` in signal engine (default: 200)
3. Increase timeout in strategy JSON

#### 4. Broker Degraded Mode Threshold

**Purpose**: Handles unstable broker connection gracefully

**Default**: 3 consecutive failures

**What happens**:
- After N consecutive broker API failures, enters "degraded mode"
- **In degraded mode**:
  - Blocks all **new entries**
  - Allows **exits** on best effort (using last known prices)
  - Logs CRITICAL warning
  - Sends Telegram alert: "⚠️ DEGRADED MODE - Broker connection unstable"
- **Exit degraded mode**: Automatic when broker call succeeds
- Retries connection every 5 minutes

**Recommended values**:
- **Conservative**: 2-3 failures (enter degraded mode quickly)
- **Aggressive**: 4-5 failures (tolerate more failures before pausing)

### Monitoring Safety Limits

Check current safety status via health endpoint:

```bash
curl -H "X-Health-Secret: your_secret" https://your-app.railway.app/health
```

Response includes:
```json
{
  "status": "healthy",
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

### What To Do When Limits Are Hit

#### Trade Frequency Limit Hit

**Symptoms**:
- Telegram alert: "🚨 EMERGENCY HALT - Max trades per day reached"
- Logs show: "COST SAFETY LIMIT: X trades today (limit: Y)"

**Investigation steps**:
1. Check Railway logs for repeated signal fires
2. Verify signal logic isn't stuck in a loop
3. Check if market conditions are triggering excessive signals
4. Review executed trades in Alpaca dashboard

**Resolution**:
- **If bug detected**: Fix code, deploy, resume trading
- **If legitimate market activity**: Consider increasing limit or pausing for today
- **Resume**: `/resume` via Telegram or `TRADING_PAUSED=false` in Railway

#### API Call Limit Hit

**Symptoms**:
- Telegram alert: "🚨 EMERGENCY HALT - Max API calls/hour exceeded"
- Logs show: "COST SAFETY LIMIT: X API calls this hour (limit: Y)"

**Investigation steps**:
1. Check Railway logs for excessive `record_api_call()` entries
2. Verify data fetching frequency
3. Check if multiple strategies are sharing the same broker instance

**Resolution**:
- **Wait for hourly reset**: Counter resets at top of each hour
- **Increase limit**: Update `max_api_calls_per_hour` in strategy JSON
- **Optimize**: Reduce data fetching frequency or cache more aggressively

#### Degraded Mode

**Symptoms**:
- Telegram alert: "⚠️ DEGRADED MODE - Broker connection unstable"
- Logs show: "ENTERING DEGRADED MODE: Broker connection failed X consecutive times"

**Investigation steps**:
1. Check Alpaca API status: [status.alpaca.markets](https://status.alpaca.markets)
2. Verify Railway has internet connectivity
3. Check Railway logs for specific broker error messages

**Resolution**:
- **Automatic**: AlphaLive retries connection every 5 minutes
- **Manual**: Restart Railway service if connection doesn't restore
- **Exits automatically** when broker call succeeds (sends "✅ DEGRADED MODE CLEARED" alert)

### Best Practices

1. **Start conservative**: Use default limits for first week, adjust based on actual usage
2. **Monitor daily**: Check health endpoint at end of each trading day
3. **Set up alerts**: Configure Railway to notify you on service restarts
4. **Review logs weekly**: Look for patterns of hitting soft limits (80% API budget)
5. **Test in paper trading**: Verify limits work as expected before going live
6. **Document changes**: Note why you adjusted limits in git commit messages

---

## Troubleshooting

### "Invalid API key"
**Cause**: Wrong API key or using paper keys with `ALPACA_PAPER=false`

**Fix**:
- Verify `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are correct
- If `ALPACA_PAPER=true`, use **paper trading keys**
- If `ALPACA_PAPER=false`, use **live trading keys**

---

### "Data is stale — market may be closed"
**Cause**: Market is closed or data feed delayed

**Fix**:
- Check if market is open (9:30 AM - 4:00 PM ET, weekdays)
- Check Alpaca status: [status.alpaca.markets](https://status.alpaca.markets)
- Bot will auto-resume when market opens (no action needed)

---

### "Telegram offline — trading continues but alerts lost"
**Cause**: Telegram API failure or wrong credentials

**Fix**:
- Check `TELEGRAM_BOT_TOKEN` is correct (no extra spaces)
- Check `TELEGRAM_CHAT_ID` is correct (number, not username)
- Verify bot is not blocked by Telegram
- Bot will auto-retry every 10 minutes (trading continues)

---

### "Trade blocked: Daily loss limit exceeded"
**Cause**: Account hit `max_daily_loss_pct` for the day

**Fix**:
- This is **working as intended** (protecting your account)
- Trading resumes automatically next trading day
- Review why losses occurred (strategy not working? Market conditions changed?)

---

### "Position drift detected — TRADING HALTED"
**Cause**: Alpaca positions don't match bot's internal tracking

**Fix**:
1. Check Alpaca dashboard for positions
2. Manually close any unexpected positions
3. Check Railway logs for errors during order placement
4. Set `TRADING_PAUSED=false` to resume (only after reconciling positions)

---

### No trades executing
**Possible causes**:
- Market is closed (bot sleeps automatically)
- `TRADING_PAUSED=true` (kill switch active)
- `DRY_RUN=true` (logs trades but doesn't execute)
- Strategy not generating signals (check logs for signal checks)
- Risk manager blocking trades (check logs for rejection reasons)

**Fix**:
- Check Railway logs for signal generation
- Verify `TRADING_PAUSED=false`
- Verify `DRY_RUN=false`
- Check risk limits (e.g., `max_open_positions` reached)

---

## Pausing & Resuming

### Pause Trading (Kill Switch)

To **immediately halt all new entries** without stopping the bot:

1. Go to Railway dashboard → **Variables**
2. Set `TRADING_PAUSED=true`
3. Railway restarts (~15-30 seconds)
4. Bot blocks all new entries (existing positions remain open)

### Resume Trading

1. Go to Railway dashboard → **Variables**
2. Set `TRADING_PAUSED=false`
3. Railway restarts
4. Trading resumes

**Note**: Existing open positions are NOT closed when paused. To close positions, manually close via Alpaca dashboard or set `TRADING_PAUSED=false` and let exit conditions trigger.

---

## Updating Your Strategy

To update strategy parameters after deployment:

1. **Update JSON** in AlphaLab and export new version
2. **Replace file** in repository:
   ```bash
   cp ~/Downloads/ma_crossover_v2.json configs/ma_crossover.json
   ```
3. **Commit and push**:
   ```bash
   git add configs/ma_crossover.json
   git commit -m "Update stop loss to 2.5%"
   git push
   ```
4. **Railway auto-deploys** on push
5. **Bot restarts** with new strategy (~30-60 seconds)

**Important**: Strategy changes trigger a restart. Open positions remain open (bot tracks them across restarts), but check logs to confirm positions are reconciled correctly.

---

## Stopping the Bot

To **completely stop** AlphaLive:

### Option 1: Delete Service (Permanent)

1. Go to Railway dashboard
2. Click your project
3. Go to **Settings** → **Delete Service**
4. Confirm deletion

You'll receive a **Telegram shutdown notification** with daily stats.

### Option 2: Pause Deployment (Temporary)

1. Go to Railway dashboard
2. Click your project
3. Click **"Pause Deployment"** button
4. Bot stops but service remains (can resume later)

---

## Logs & Debugging

### Viewing Logs

**Railway Dashboard**:
1. Click your project
2. Go to **"Deployments"**
3. Click **"View Logs"**
4. Real-time logs stream in browser

**Example logs**:
```
2026-03-09 09:35:00 [INFO] alphalive.main: Market is open — running signal check
2026-03-09 09:35:01 [INFO] alphalive.data.market_data: Fetched 200 bars for AAPL
2026-03-09 09:35:02 [INFO] alphalive.strategy.signal_engine: BUY signal: MA crossover
2026-03-09 09:35:03 [INFO] alphalive.execution.order_manager: MARKET BUY 66 AAPL @ market
2026-03-09 09:35:05 [INFO] alphalive.broker.alpaca_broker: Order filled: 66 shares @ $150.25
```

### Log Levels

- `DEBUG`: Detailed diagnostic info (set `LOG_LEVEL=DEBUG` for troubleshooting)
- `INFO`: Normal operation (trades, signal checks, position updates)
- `WARNING`: Unexpected but recoverable (risk limit rejections, Telegram failures)
- `ERROR`: Serious issues (API failures, order placement failures)
- `CRITICAL`: System-level failures (position drift, circuit breaker triggers)

### Downloading Logs

Railway doesn't offer log download directly, but you can:
1. Use Railway CLI: `railway logs` (streams logs to terminal)
2. Copy/paste from browser
3. Set up external log aggregation (Datadog, Papertrail) via Railway integrations

---

## Advanced: Persistent Storage

By default, AlphaLive uses `/tmp/alphalive_state.json` for state persistence. This is lost on restarts.

To **persist state across Railway restarts**:

1. **Create Railway Volume**:
   - Go to Railway project → **Settings**
   - Add a volume at mount path `/data`

2. **Update environment variable**:
   ```bash
   STATE_FILE=/data/alphalive_state.json
   PERSISTENT_STORAGE=true
   ```

3. **Redeploy**

State file now survives Railway restarts (position tracking, daily stats, etc.).

---

## Using Trailing Stops

**⚠️ IMPORTANT**: Trailing stops require persistent storage across Railway restarts to prevent real money risk.

### Why Persistent Storage is Required

Trailing stops track the **highest price** seen for each position. If Railway redeploys mid-day and this tracking is lost, the bot will:
1. Reset `position_high` to current price (wrong!)
2. Miscalculate trailing stop trigger
3. Exit too early or too late (real money loss)

**To prevent this, AlphaLive refuses to start if trailing stops are enabled without persistent storage.**

### Setup Steps

If your strategy has `trailing_stop_enabled: true`, you **must** complete these steps:

#### 1. Create Railway Volume

1. Go to Railway dashboard → Your project
2. Click **"Settings"** → **"Volumes"**
3. Click **"New Volume"**
4. Set mount path: `/mnt/data`
5. Click **"Add"**

#### 2. Set Environment Variables

Add these variables in Railway **"Variables"** tab:

```bash
STATE_FILE=/mnt/data/alphalive_state.json
PERSISTENT_STORAGE=true
```

**Why `/mnt/data`?**
- `/tmp/` is ephemeral (lost on redeploy) ❌
- `/mnt/data/` is persistent (survives redeploys) ✅

#### 3. Redeploy

Railway will auto-restart with new configuration.

**Verify in logs**:
```
[INFO] BotState initialized from /mnt/data/alphalive_state.json
[INFO] Trailing stop configuration check passed
```

### What Happens Without Persistent Storage

If you deploy with `trailing_stop_enabled: true` but don't set up persistent storage:

**Bot will refuse to start** with this error:

```
[CRITICAL] STARTUP ABORTED: trailing_stop_enabled=true requires persistent
storage, but PERSISTENT_STORAGE is not set to true. A Railway redeploy
mid-day will reset position_highs and miscalculate trailing stops, which
is a real money risk. Either:
(A) Set trailing_stop_enabled=false in your strategy config, or
(B) Mount a Railway Volume, set STATE_FILE=/mnt/data/alphalive_state.json,
and set PERSISTENT_STORAGE=true
```

You'll also receive a **Telegram alert**:
```
⛔ AlphaLive refused to start: trailing stops require persistent storage.
See Railway logs for fix instructions.
```

### Solutions

**Option A**: Disable trailing stops (simpler)
- Edit your strategy JSON: `"trailing_stop_enabled": false`
- Re-export from AlphaLab
- Push to GitHub → Railway redeploys

**Option B**: Enable persistent storage (recommended if you want trailing stops)
- Follow setup steps above (create volume, set env vars)
- Deploy → Bot starts successfully

### Verification Checklist

Before deploying with trailing stops:

- [ ] ✅ Created Railway Volume at `/mnt/data`
- [ ] ✅ Set `STATE_FILE=/mnt/data/alphalive_state.json`
- [ ] ✅ Set `PERSISTENT_STORAGE=true`
- [ ] ✅ Redeployed to Railway
- [ ] ✅ Checked logs: "Trailing stop configuration check passed"
- [ ] ✅ Verified state file persists across restarts

**Test persistence**:
1. Wait for bot to open a position
2. Trigger a Railway restart (push new commit)
3. Check logs: bot should load position_highs from state file
4. Verify trailing stop still works correctly

---

## Support

- **Railway Docs**: [docs.railway.app](https://docs.railway.app)
- **Alpaca API Docs**: [alpaca.markets/docs](https://alpaca.markets/docs)
- **AlphaLive Dev Guide**: See [CLAUDE.md](CLAUDE.md) for architecture and development
- **Issues**: Open issue on GitHub repository

---

## Next Steps

1. ✅ **Deploy to Railway** with paper trading
2. ✅ **Monitor for 1 week** → verify signals match AlphaLab backtest
3. ✅ **Review performance** → ensure Sharpe > 1.0, win rate > 50%
4. ✅ **Enable live trading** → start with small position sizes (1-5% account)
5. ✅ **Scale up** → increase position sizes as confidence grows

**Good luck trading!** 🚀
