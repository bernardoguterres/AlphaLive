# AlphaLive — The Math

AlphaLive doesn't invent new math — its signal-generation logic is an independently-written mirror of AlphaLab's (see `CLAUDE.md`'s "Signal parity is critical"; the two are diffed against each other by `tests/test_signal_parity.py`, neither imports the other). For the indicator formulas (SMA/RSI/Bollinger/ATR/ADX/VWAP) and strategy entry/exit logic, see **`AlphaLab/docs/MATH_EXPLAINER.md`** — it's the same math here. This document covers what's actually unique to AlphaLive: the math of turning a signal into a real position, sized and protected, and the portfolio/risk-management layer around it. Verified against the real source (`alphalive/execution/risk_manager.py`, `alphalive/strategy/signal_engine.py`, `alphalive/portfolio/target_weights.py`), not written from memory.

---

## 1. Position Sizing

$$\text{Max Dollars} = \text{Account Equity} \times \frac{\text{max\_position\_size\_pct}}{100}$$
$$\text{Shares} = \frac{\text{Max Dollars}}{\text{Current Price}}$$

For market orders, this is floored to 3 decimal places (Alpaca supports fractional shares on market day orders) rather than a whole share — the actual reason: on a $1,000 account with 10% position sizing, that's $100 per position; whole-share rounding would floor that to 0 shares for anything priced above $100, and the strategy would simply never trade. Below Alpaca's $1 minimum notional, the order is skipped entirely (returns 0 shares) rather than sent and rejected.

Limit orders are floored to whole shares — Alpaca doesn't support fractional limit orders at all, a hard broker constraint, not a design choice.

---

## 2. Exit Protection — Three Independent Mechanisms

### Fixed stop-loss / take-profit
$$\text{Stop-Loss Trigger: } \text{Price} \leq \text{Entry Price} \times \Big(1 - \frac{\text{stop\_loss\_pct}}{100}\Big)$$
$$\text{Take-Profit Trigger: } \text{Price} \geq \text{Entry Price} \times \Big(1 + \frac{\text{take\_profit\_pct}}{100}\Big)$$
Signs flip for short positions. Both are fixed, anchored to the entry price for the life of the position — they don't move.

### Generic trailing stop (any strategy, opt-in via config)
$$\text{Trigger: } \text{Price} \leq \text{Highest Price Since Entry} \times \Big(1 - \frac{\text{trailing\_stop\_pct}}{100}\Big)$$
Unlike the fixed stop, this one *moves up* with the position — the trigger level ratchets to follow the highest price seen since entry, locking in gains as the position rises while still giving it room to breathe on pullbacks. Requires `PERSISTENT_STORAGE=true`; without state surviving a Railway restart, "highest price since entry" would silently reset to whatever price the bot restarts at, defeating the whole mechanism.

### Greenblatt-specific peak-trailing stop (always active for `greenblatt_weekly`, not opt-in)
$$\text{Trailing Stop Level} = \text{Peak Price} \times (1 - 0.20)$$
$$\text{Exit if: } \text{Price}_{\text{now}} \leq \text{Trailing Stop Level}$$
Same shape as the generic trailing stop above, but implemented directly in the signal engine rather than the general risk manager, and it's the *only* exit mechanism active by default for this strategy (RSI/SMA exits exist but are opt-in, off by default). `_peak_price` updates every bar the position is open and is persisted across restarts as part of engine state — without it, a restart mid-position would reset the peak to the post-restart price and the trailing stop would silently stop protecting against the drawdown that already happened.

**Minimum hold override**: for `greenblatt_weekly`, the trailing stop above *always* bypasses the 52-week minimum hold — a real drawdown gets exited regardless of how recently the position was opened. Only the *optional* RSI/SMA exits respect the minimum hold. This is a deliberate asymmetry: a stop-loss protecting capital shouldn't be blocked by a holding-period rule designed to stop a strategy from getting shaken out of a winning position too early — those are different concerns and the code treats them differently on purpose.

---

## 3. The Bear Market Filter

$$\text{Bear Market} = \big(\text{Price} < SMA_{200}\big) \ \text{AND}\ \big(SMA_{200,\ \text{today}} < SMA_{200,\ 20\ \text{bars ago}}\big)$$

Both conditions must hold — price merely being below the 200-day average isn't enough (that happens in normal pullbacks within an uptrend too); the average itself has to be declining. When true, new BUY signals are blocked; SELL/exit signals always pass through regardless — the filter only ever prevents *entering*, never prevents *leaving*, on the logic that a risk filter should never trap you in a position. For `greenblatt_weekly` (weekly bars), the same logic uses the strategy's own slow SMA (default 50-week ≈ 200 trading days) as the trend reference instead of a literal `SMA_200` column, since daily and weekly bar counts aren't directly comparable.

---

## 4. Risk Management — the check-order matters

`RiskManager.can_trade()` runs a strict, ordered gauntlet before any BUY is allowed through — the first failing check wins, no partial overrides:

1. `TRADING_PAUSED` kill switch (env var)
2. Manual Telegram `/pause`
3. Trade frequency limit (`max_trades_per_day`)
4. API call budget (`max_api_calls_per_hour`)
5. Broker degraded mode (3+ consecutive failures → auto-pause)
6. Daily loss limit (per-strategy)
7. Consecutive-loss circuit breaker (3 losses in a row → pause for the day)
8. Max open positions (per-strategy)
9. Portfolio max positions (global, across all strategies)
10. Cooldown period since the last trade

SELL/exit signals skip checks 8-10 deliberately — blocking a position-*reducing* trade on a position-count cap would trap the bot fully invested exactly when it's trying to de-risk, which defeats the point of a risk limit.

### Daily loss limit
$$\text{Max Daily Loss (\$)} = \text{Account Equity} \times \frac{\text{max\_daily\_loss\_pct}}{100}$$
$$\text{Halt if: } |\text{Daily P\&L}| \geq \text{Max Daily Loss (\$)}$$
Tracked at two levels: per-strategy (each strategy has its own limit and its own halt) and globally (`GlobalRiskManager` sums `daily_pnl` across every registered strategy and halts *all* of them if the combined loss breaches the global cap) — a single strategy blowing past its own limit doesn't automatically halt the others, but the portfolio-wide sum can.

---

## 5. Portfolio Construction (built, not yet live)

`alphalive/portfolio/target_weights.py` mirrors AlphaLab's `PortfolioConstructor` exactly — rank candidates by `combined_rank` (the Greenblatt combined rank, ascending = better), take the top N, weight each equally:
$$\text{Target Weight}_i = \frac{1}{N}, \quad \text{Target Shares}_i = \frac{\text{Target Weight}_i \times \text{Portfolio Value}}{\text{Price}_i}$$
Deliberately a second, independent implementation (not importing AlphaLab's), diffed against a fixture AlphaLab exports via `tests/test_portfolio_parity.py` — same "two independent implementations checked against each other" philosophy as the signal-parity testing.

**Important, and worth being precise about with an employer**: this module computes correct target positions for a given snapshot, but it is **not wired into the live 24/7 trading loop**. It doesn't decide when to rebalance, doesn't place orders, and a portfolio-shaped strategy config cannot currently be deployed. That's a deliberate, separate next step — not an oversight, and not something to imply is "live" if asked about it.

---

## The one-paragraph version, for an interview

*"AlphaLive turns a validated AlphaLab strategy into a real, running position — not just a signal. Position sizing is capital-percentage-based with fractional-share support so it works at small account sizes. Every position is protected by up to three independent, differently-shaped exit mechanisms (fixed stop/take-profit, a generic trailing stop, and a strategy-specific peak-trailing stop that deliberately overrides the minimum-hold rule when protecting capital). A ten-check risk gauntlet gates every entry, with sells exempted from position-count limits on purpose — a risk system that can trap you in a position defeats itself. And the portfolio-construction layer is built and parity-tested against an independent implementation, but I know exactly where the line is between 'built and tested' and 'actually running in production' — it's not wired into the live loop yet, and I wouldn't claim it is."*
