"""
AlphaLive Main Entry Point

24/7 trading loop that runs on Railway.
This is NOT a cron job. It's a persistent Python process that:
- Sleeps when market is closed
- Wakes up to trade when market is open
- Handles Railway restarts gracefully via SIGTERM
"""

import asyncio
import time
import os
import sys
import signal
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime
from zoneinfo import ZoneInfo

from alphalive.config import load_config_path, load_env, validate_all
from alphalive.services.alphasignal_client import (
    IDENTITY_UNCHECKED,
    IDENTITY_UNREACHABLE,
    IDENTITY_WRONG_SERVICE,
    AlphaSignalClient,
    run_pre_execution_checks,
)
from alphalive.utils.env_bool import read_bool_env
from alphalive.state import (
    BotState,
    StateLockError,
    check_trailing_stop_requirements,
)
from alphalive.broker.alpaca_broker import AlpacaBroker
from alphalive.broker.transport import READ_ONLY_ENV
from alphalive.data.market_data import MarketDataFetcher, DataStaleError
from alphalive.strategy.signal_engine import SignalEngine
from alphalive.execution.risk_manager import RiskManager, GlobalRiskManager
from alphalive.execution.order_manager import OrderManager
from alphalive.notifications.telegram_bot import TelegramNotifier
from alphalive.notifications.telegram_commands import TelegramCommandListener

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Timeframe-aware signal check intervals
TIMEFRAME_CHECK_INTERVALS = {
    "1Day": None,  # Handled by morning_check_done flag
    "1Hour": 60,  # Check every 60 minutes
    "15Min": 15,  # Check every 15 minutes
}


def _compute_daily_stats(
    all_orders: list, start_equity: float, end_equity: float
) -> dict:
    """
    Compute P&L and win rate from today's order history.

    P&L is derived from equity change (start → end), which is the authoritative
    source - it captures unrealised moves on positions held overnight and is
    unaffected by FIFO matching edge-cases.

    Win rate counts only completed round-trips (BUY followed by a SELL for the
    same ticker) using a simple FIFO queue per ticker.

    Args:
        all_orders: List of order dicts from OrderManager.get_order_history().
                    Each entry has keys: ticker, side, qty, price, timestamp.
        start_equity: Portfolio equity at market open (morning_equity).
        end_equity:   Portfolio equity now (account.equity).

    Returns:
        Dict with keys: pnl (float, dollars), win_rate (float, 0–100 %).
    """
    # start_equity <= 0 means morning equity was never captured (restart
    # after 4 PM with no persisted value) - reporting end_equity - 0 would
    # claim the whole account as today's P&L.
    pnl = end_equity - start_equity if start_equity > 0 else 0.0

    # Win-rate: qty-aware FIFO matching of BUYs to SELLs per ticker.
    # Each SELL consumes shares from the buy queue oldest-first; the sell is
    # a win if its price beats the weighted-average cost of the shares it
    # matched. Matters with fractional sizing, where quantities rarely align
    # one order to one order.
    buy_queues: dict[str, list] = {}  # ticker -> list of [qty, price]
    wins = 0
    total_sells = 0

    for order in sorted(all_orders, key=lambda o: o["timestamp"]):
        ticker = order["ticker"]
        side = order.get("side", "").upper()
        qty = float(order.get("qty", 1) or 1)
        if side == "BUY":
            buy_queues.setdefault(ticker, []).append([qty, order["price"]])
        elif side == "SELL":
            queue = buy_queues.get(ticker, [])
            matched_qty = 0.0
            matched_cost = 0.0
            remaining = qty
            while queue and remaining > 0:
                lot_qty, lot_price = queue[0]
                take = min(lot_qty, remaining)
                matched_qty += take
                matched_cost += take * lot_price
                remaining -= take
                if take >= lot_qty:
                    queue.pop(0)
                else:
                    queue[0][0] = lot_qty - take
            if matched_qty > 0:
                total_sells += 1
                avg_buy_price = matched_cost / matched_qty
                if order["price"] > avg_buy_price:
                    wins += 1

    win_rate = (wins / total_sells * 100) if total_sells > 0 else 0.0
    return {"pnl": pnl, "win_rate": win_rate}


def _record_broker_call(
    order_manager_map: dict, success: bool, error: Exception = None
) -> None:
    """Propagate a broker API call's outcome to every strategy's RiskManager.

    Needed so the degraded-mode circuit breaker (auto-pause after
    broker_degraded_mode_threshold_failures consecutive failures) can
    actually trip, and so it can self-heal once the connection recovers -
    record_broker_success() alone only resets the failure counter, it
    doesn't clear an already-tripped degraded_mode flag.
    """
    for om in order_manager_map.values():
        if success:
            om.risk.record_broker_success()
            om.risk.exit_degraded_mode()
        else:
            om.risk.record_broker_failure(error)


def _send_eod_summary(
    order_manager_map: dict, broker, morning_equity: float, notifier
) -> None:
    """Aggregate today's trades across all strategies and push the daily summary."""
    all_orders = []
    for ticker in order_manager_map:
        all_orders.extend(order_manager_map[ticker].get_order_history())
    try:
        account = broker.get_account()
    except Exception as e:
        _record_broker_call(order_manager_map, success=False, error=e)
        raise
    _record_broker_call(order_manager_map, success=True)
    daily_stats = _compute_daily_stats(all_orders, morning_equity, account.equity)
    summary = {
        "trades": len(all_orders),
        "pnl": daily_stats["pnl"],
        "win_rate": daily_stats["win_rate"],
        "start_equity": morning_equity,
        "end_equity": account.equity,
    }
    notifier.send_daily_summary(summary)


def should_run_signal_check(timeframe: str, last_check_time: float) -> bool:
    """
    Determine if a signal check should run based on timeframe and last check time.

    For 1Day: returns False (handled by morning_check_done flag)
    For 1Hour: returns True if 60 minutes have passed since last check
    For 15Min: returns True if 15 minutes have passed since last check

    Also aligns to bar boundaries: 9:30, 9:45, 10:00 for 15Min.

    Args:
        timeframe: Strategy timeframe ("1Day", "1Hour", "15Min")
        last_check_time: Unix timestamp of last signal check

    Returns:
        True if signal check should run now
    """
    if timeframe == "1Day":
        return False  # Use morning_check_done flag instead

    interval_minutes = TIMEFRAME_CHECK_INTERVALS[timeframe]
    now = datetime.now(ET)

    # Check if we're at a bar boundary (9:30, 9:45, 10:00 for 15Min)
    if now.minute % interval_minutes != 0:
        return False

    # Check if enough time has passed since last check
    time_since_last = time.time() - last_check_time
    return time_since_last >= (interval_minutes * 60 - 35)  # -35s for timing slop


def _build_screener():
    """Build the monthly Greenblatt screener from env vars, or None if unset.

    SCREENER_UNIVERSE: comma-separated tickers (required to enable)
    SCREENER_OUTPUT_PATH: output JSON path (default screener_output.json,
        same env var the dashboard reads)
    SCREENER_TOP_N: candidates to keep (default 15)
    """
    universe_env = os.getenv("SCREENER_UNIVERSE", "").strip()
    if not universe_env:
        logger.info("Monthly screener disabled (SCREENER_UNIVERSE not set)")
        return None
    universe = [t.strip().upper() for t in universe_env.split(",") if t.strip()]
    if not universe:
        return None

    from alphalive.screener.fundamental_screener import FundamentalScreener

    screener = FundamentalScreener(
        universe=universe,
        output_path=os.getenv("SCREENER_OUTPUT_PATH", "screener_output.json"),
        top_n=int(os.getenv("SCREENER_TOP_N", "15")),
    )
    logger.info(
        f"Monthly screener enabled | universe: {len(universe)} tickers | "
        f"top_n: {screener.top_n} | output: {screener.output_path}"
    )
    return screener


def _run_monthly_screener(screener, bot_state, notifier, now_et: datetime) -> None:
    """Run the Greenblatt screen once per calendar month.

    Month-keyed via BotState rather than day-of-month: the 30s main loop
    would otherwise re-run the full yfinance screen every iteration on the
    1st, and skip the month entirely if the bot was down that day. The
    screener output is informational (dashboard panel + candidate list for
    AlphaLab backtesting) - a failure alerts and skips to next month rather
    than retrying every 30 seconds.
    """
    if screener is None:
        return
    month_key = now_et.strftime("%Y-%m")
    if bot_state.get_last_screener_month() == month_key:
        return

    logger.info(f"Running monthly Greenblatt screener for {month_key}...")
    bot_state.set_last_screener_month(month_key)
    try:
        candidates = screener.run()
    except Exception as e:
        logger.error(f"Monthly screener failed: {e}", exc_info=True)
        notifier.send_error_alert(
            f"Monthly Greenblatt screener failed: {e}. "
            f"Will retry next month; run screener.run() manually if needed."
        )
        return

    top = candidates[:5]
    lines = "\n".join(
        f"  {i}. {c.ticker} (rank {c.combined_rank}, EY {c.earnings_yield:.1%}, "
        f"ROC {c.return_on_capital:.1%})"
        for i, c in enumerate(top, 1)
    )
    notifier.send_message(
        f"📋 <b>Monthly Greenblatt Screen</b> ({month_key})\n\n"
        f"{len(candidates)} candidates selected. Top {len(top)}:\n{lines}\n\n"
        f"Backtest candidates in AlphaLab before deploying.",
        parse_mode="HTML",
    )


def _wire_min_hold_checkers(
    all_strategy_configs: list, signal_engine_map: dict, bot_state
) -> None:
    """Connect greenblatt_weekly engines to timestamp-based min-hold checks.

    Bar-counting (AlphaLab's approach) is unreliable live - restarts lose the
    bar index - so AlphaLive enforces min hold from the entry timestamps that
    main.py records in BotState on every real BUY fill.
    """
    for strategy_config in all_strategy_configs:
        if strategy_config.strategy.name != "greenblatt_weekly":
            continue
        ticker = strategy_config.ticker
        min_hold_weeks = strategy_config.strategy.parameters.get("min_hold_bars", 52)
        signal_engine_map[ticker].min_hold_checker = (
            lambda t=ticker, w=min_hold_weeks: bot_state.is_min_hold_met(t, w)
        )
        logger.info(
            f"Min-hold gate wired for greenblatt_weekly ({ticker}): "
            f"{min_hold_weeks} weeks"
        )


def _restore_engine_states(
    all_strategy_configs: list, signal_engine_map: dict, bot_state
) -> None:
    """Restore SignalEngine state from BotState, reconciled against the ledger.

    The persisted engine state is only trusted when the position ledger (which
    _sync_position_ledger just reconciled with the broker) agrees:
    - ledger has no position  -> engine must be flat, whatever was saved
    - ledger has a position   -> engine must be in-position; if the saved state
      is missing/flat (e.g. state written by an older version), rebuild it from
      the ledger entry price and the tracked position high.
    """
    for strategy_config in all_strategy_configs:
        ticker = strategy_config.ticker
        engine = signal_engine_map[ticker]
        saved = bot_state.get_engine_state(ticker)
        ledger_entry = bot_state.get_open_positions().get(ticker)

        if ledger_entry is None:
            # No broker position. Long-implying state must be cleared, but
            # position-free bookkeeping (vwap short/cooldown counters) is
            # legitimate while flat and must survive the restart.
            if saved:
                sanitized = dict(saved)
                if sanitized.get("in_position") or sanitized.get("vwap_position") == 1:
                    logger.warning(
                        f"Engine state for {ticker} implied a long position but "
                        f"the ledger has none - resetting position fields."
                    )
                    sanitized.update(
                        {"in_position": False, "entry_price": 0.0, "peak_price": 0.0}
                    )
                    if sanitized.get("vwap_position") == 1:
                        sanitized["vwap_position"] = 0
                engine.restore_state(sanitized)
                bot_state.save_engine_state(ticker, engine.get_state())
            continue

        if saved and (saved.get("in_position") or saved.get("vwap_position") == 1):
            engine.restore_state(saved)
        else:
            entry_price = ledger_entry.get("entry_price", 0.0)
            peak = max(bot_state.get_position_high(ticker) or 0.0, entry_price)
            logger.warning(
                f"Ledger holds {ticker} but no saved engine state - rebuilding "
                f"from ledger (entry ${entry_price:.2f}, peak ${peak:.2f})."
            )
            rebuilt = {
                "in_position": True,
                "entry_price": entry_price,
                "peak_price": peak,
            }
            if strategy_config.strategy.name == "vwap_reversion":
                rebuilt["vwap_position"] = 1
            engine.restore_state(rebuilt)
        bot_state.save_engine_state(ticker, engine.get_state())


def _apply_corporate_actions(positions, bot_state, notifier) -> None:
    """Detect broker-side splits on held positions and rescale our RAW
    bookkeeping (ledger qty/entry, trailing-stop position high).

    The broker rescales quantity and average entry on a split while keeping
    cost basis. Our ledger and the raw position_high would otherwise stay in
    pre-split units: the trailing stop would see a ~-50%/-75% "drop" and the
    reconciler a qty mismatch. Heuristic: qty changed, no order in flight
    for the ticker, and cost basis (qty x avg entry) unchanged within 1%.
    Anything else stays a genuine drift for _run_position_reconciliation.
    """
    ledger = bot_state.get_open_positions()
    busy = {i["ticker"] for i in bot_state.list_open_intents()}
    for pos in positions:
        entry = ledger.get(pos.symbol)
        if entry is None or pos.symbol in busy:
            continue
        lq, bq = float(entry.get("qty") or 0), float(pos.qty or 0)
        if lq <= 0 or bq <= 0 or abs(bq - lq) <= 1e-6:
            continue
        l_cost = lq * float(entry.get("entry_price") or 0)
        b_cost = bq * float(pos.avg_entry_price or 0)
        if l_cost <= 0 or abs(b_cost / l_cost - 1) > 0.01:
            continue
        ratio = bq / lq
        bot_state.record_position_open(pos.symbol, bq, float(pos.avg_entry_price))
        high = bot_state.get_position_high(pos.symbol)
        if high:
            bot_state.state["position_highs"][pos.symbol] = high / ratio
            bot_state.save()
        msg = (
            f"Corporate action on {pos.symbol}: broker qty {lq:g} -> {bq:g} "
            f"(x{ratio:.4g}) with unchanged cost basis - ledger and trailing-stop "
            f"high rescaled."
        )
        logger.warning(msg)
        notifier.send_alert(f"ℹ️ {msg}")


def _sync_engines_with_ledger(signal_engine_map: dict, bot_state) -> None:
    """Ledger wins, continuously: align each engine's long/flat flag with
    the broker-confirmed position ledger whenever no order for that ticker
    is still in flight (an unfilled BUY must not leave the engine "long",
    a risk-exit close must not leave it "long" either)."""
    ledger = bot_state.get_open_positions()
    busy = {i["ticker"] for i in bot_state.list_open_intents()}
    for ticker, engine in signal_engine_map.items():
        if ticker in busy:
            continue
        before = engine.get_state()
        entry = ledger.get(ticker)
        engine.sync_position(
            entry is not None,
            entry_price=(entry or {}).get("entry_price", 0.0),
            peak_price=bot_state.get_position_high(ticker) or 0.0,
        )
        if engine.get_state() != before:
            bot_state.save_engine_state(ticker, engine.get_state())


def _run_startup_warmup(
    all_strategy_configs: list,
    market_data,
    signal_engine_map: dict,
    notifier,
) -> None:
    """Fetch and validate startup bars for every strategy before the first signal check."""
    logger.info("Running startup data backfill and warmup validation...")
    for strategy_config in all_strategy_configs:
        ticker = strategy_config.ticker
        logger.info(f"  Warming up {strategy_config.strategy.name} ({ticker})...")
        try:
            df = market_data.get_latest_bars(
                ticker, strategy_config.timeframe, lookback_bars=250
            )
            # The warmup signal is a throwaway - but generate_signal() mutates
            # stateful strategies (an entry here would flip _in_position without
            # any order, silently consuming the real 9:35 AM entry signal).
            # Snapshot and restore around it.
            _pre_warmup_state = signal_engine_map[ticker].get_state()
            test_signal = signal_engine_map[ticker].generate_signal(df)
            signal_engine_map[ticker].restore_state(_pre_warmup_state)
            warmup_complete = test_signal.get("warmup_complete", True)

            if not warmup_complete:
                logger.warning(
                    f"  Indicator warmup incomplete for {ticker} - signals may be unreliable"
                )
                notifier.send_alert(
                    f"⚠️ Indicator warmup incomplete for {ticker} on startup. "
                    f"Some indicators have NaN values."
                )
            else:
                logger.info(
                    f"  Warmup complete for {ticker}: {len(df)} bars loaded, "
                    f"test signal: {test_signal['signal']}"
                )
                if len(all_strategy_configs) == 1:
                    notifier.send_message(
                        f"✅ <b>Startup warmup OK</b>\n"
                        f"Strategy: {strategy_config.strategy.name}\n"
                        f"Ticker: {ticker}\n"
                        f"Bars loaded: {len(df)}\n"
                        f"Test signal: {test_signal['signal']}\n"
                        f"Confidence: {test_signal['confidence']:.2%}",
                        parse_mode="HTML",
                    )
        except DataStaleError as e:
            logger.critical(f"Startup data staleness for {ticker}: {e}")
            notifier.send_error_alert(f"❌ Startup failed for {ticker}: {str(e)}")
            sys.exit(1)
        except Exception as e:
            logger.error(f"Startup warmup failed for {ticker}: {e}", exc_info=True)
            notifier.send_error_alert(f"⚠️ Startup warmup error for {ticker}: {str(e)}")

    if len(all_strategy_configs) > 1:
        notifier.send_message(
            f"✅ <b>Multi-strategy warmup complete</b>\n\n"
            f"Strategies: {len(all_strategy_configs)}\n"
            f"Tickers: {', '.join([cfg.ticker for cfg in all_strategy_configs])}",
            parse_mode="HTML",
        )


def _build_startup_message(all_strategy_configs: list, mode: str) -> str:
    """Build the Telegram startup notification message."""
    if len(all_strategy_configs) == 1:
        cfg = all_strategy_configs[0]
        return (
            f"🚀 <b>AlphaLive Started</b>\n\n"
            f"<b>Mode:</b> {mode}\n"
            f"<b>Strategy:</b> {cfg.strategy.name}\n"
            f"<b>Ticker:</b> {cfg.ticker}\n"
            f"<b>Timeframe:</b> {cfg.timeframe}\n"
            f"<b>Risk:</b> SL {cfg.risk.stop_loss_pct}% / "
            f"TP {cfg.risk.take_profit_pct}%\n"
            f"<b>Backtest Sharpe:</b> {cfg.metadata.performance.sharpe_ratio:.2f}\n"
            f"<b>Platform:</b> Railway (24/7)\n\n"
            f"Bot is now monitoring the market."
        )
    strategy_list = "\n".join(
        [
            f"  • {cfg.strategy.name} on {cfg.ticker} @ {cfg.timeframe}"
            for cfg in all_strategy_configs
        ]
    )
    return (
        f"🚀 <b>AlphaLive Started</b> (Multi-Strategy)\n\n"
        f"<b>Mode:</b> {mode}\n"
        f"<b>Strategies:</b> {len(all_strategy_configs)}\n\n"
        f"{strategy_list}\n\n"
        f"<b>Platform:</b> Railway (24/7)\n\n"
        f"Bot is now monitoring the market."
    )


def _iso_week_key(now_et: datetime) -> str:
    """ISO-8601 calendar-week key ("2026-W03"), Monday-start and
    year-boundary safe (unlike naive strftime("%Y-%W")). Used to gate a
    1Week strategy to evaluating once per calendar week. Pure calendar-date
    arithmetic - never looks at time-of-day, so DST transitions cannot
    affect it.
    """
    iso_year, iso_week, _ = now_et.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _mark_periodic_check_done(
    strat_cfg, now_et: datetime, morning_checks_done: set, bot_state, notifier=None
) -> None:
    """Mark a 1Day/1Week strategy's periodic check as DEFINITIVELY completed
    for this pass - the evaluation actually reached a decision (HOLD, a
    blocked signal, an order attempt, or a deliberate safety skip like
    split-detection). Use _mark_periodic_check_attempted instead for a data-
    fetch failure, evaluation exception, or timeout - those must NOT mark
    the week complete, so a later retry (same day, or after a restart) can
    still happen. See "Weekly-key persistence timing" in the 2026-09-11
    pass-3 report for the exact per-outcome policy this encodes.

    For 1Week, releases the in-flight claim from try_begin_weekly_eval and
    durably persists the ISO calendar-week key. If that persistence write
    itself fails, further automatic evaluation is halted (TRADING_PAUSED)
    rather than silently continuing on in-memory-only protection - see
    BotState.mark_weekly_eval_done's docstring.
    """
    morning_checks_done.add(strat_cfg.ticker)
    if strat_cfg.timeframe == "1Day":
        # Not halting on a failed write: a duplicate daily entry is still
        # blocked by the persisted cooldown, open-intent guard and cap.
        if not bot_state.mark_daily_eval_done(
            strat_cfg.ticker, now_et.strftime("%Y-%m-%d")
        ):
            logger.warning(
                f"Could not persist the daily evaluation marker for "
                f"{strat_cfg.ticker} - a restart today may re-evaluate it."
            )
    if strat_cfg.timeframe == "1Week":
        week_key = _iso_week_key(now_et)
        bot_state.end_weekly_eval(strat_cfg.ticker, week_key)
        persisted = bot_state.mark_weekly_eval_done(strat_cfg.ticker, week_key)
        if not persisted:
            msg = (
                f"Failed to durably persist the weekly evaluation marker for "
                f"{strat_cfg.ticker} ({week_key}) - halting trading "
                f"(TRADING_PAUSED) to avoid an undetected duplicate weekly "
                f"evaluation after a restart."
            )
            logger.critical(msg)
            os.environ["TRADING_PAUSED"] = "true"
            if notifier:
                notifier.send_error_alert(f"⛔ {msg}")


def _mark_periodic_check_attempted(
    strat_cfg, now_et: datetime, morning_checks_done: set, bot_state
) -> None:
    """Mark only the in-memory same-day/same-process guard - used when a
    1Day/1Week check did NOT reach a decision (data-fetch failure,
    strategy-evaluation exception, signal-generation timeout). The
    persisted weekly key is deliberately left untouched so the week can
    still be retried later today, after a restart, or on a later trading
    day within the same ISO week - only _mark_periodic_check_done (a
    genuine completion) earns the durable "done" marker.
    """
    morning_checks_done.add(strat_cfg.ticker)
    if strat_cfg.timeframe == "1Week":
        bot_state.end_weekly_eval(strat_cfg.ticker, _iso_week_key(now_et))


def _check_signal_for_strategy(
    strat_cfg,
    now_et: datetime,
    morning_checks_done: set,
    last_signal_check_map: dict,
    market_data,
    signal_engine_map: dict,
    order_manager_map: dict,
    alphasignal_client,
    broker,
    notifier,
    global_risk,
    bot_state,
) -> None:
    """Run the signal check for one strategy. Mutates morning_checks_done and last_signal_check_map."""
    if strat_cfg.timeframe in ("1Day", "1Week"):
        if not (now_et.hour == 9 and now_et.minute >= 35):
            return
        if strat_cfg.ticker in morning_checks_done:
            return
        if strat_cfg.timeframe == "1Day":
            if bot_state.get_daily_eval_date(strat_cfg.ticker) == now_et.strftime(
                "%Y-%m-%d"
            ):
                # Completed earlier today, before a restart.
                morning_checks_done.add(strat_cfg.ticker)
                logger.info(
                    f"Daily evaluation for {strat_cfg.ticker} already completed "
                    f"today ({now_et:%Y-%m-%d} ET) - not re-evaluating after restart"
                )
                return
        if strat_cfg.timeframe == "1Week":
            week_key = _iso_week_key(now_et)
            if not bot_state.try_begin_weekly_eval(strat_cfg.ticker, week_key):
                # Already evaluated this ISO calendar week (possibly before
                # a restart earlier today), or another in-flight evaluation
                # for this exact ticker/week is already claimed. Sync the
                # in-memory guard so the rest of today's ~30s loop ticks
                # short-circuit on `morning_checks_done` instead of
                # re-checking BotState.
                morning_checks_done.add(strat_cfg.ticker)
                return
    else:
        if not should_run_signal_check(
            strat_cfg.timeframe,
            last_signal_check_map.get(strat_cfg.ticker, 0),
        ):
            return

    logger.info("=" * 80)
    logger.info(f"SIGNAL CHECK: {strat_cfg.strategy.name}/{strat_cfg.ticker}")
    logger.info("=" * 80)

    try:
        df = market_data.get_latest_bars(
            ticker=strat_cfg.ticker,
            timeframe=strat_cfg.timeframe,
            lookback_bars=200,
        )

        # CRITICAL: Corporate action detection (splits, special dividends).
        # >20% overnight move without volume surge = likely split. Skip to
        # avoid false breakout/crash signals.
        if len(df) >= 2:
            yesterday_close = df["close"].iloc[-2]
            today_open = df["open"].iloc[-1]
            pct_change = abs((today_open - yesterday_close) / yesterday_close)

            if pct_change > 0.20:
                logger.critical(
                    f"SPLIT DETECTED: Price jumped {pct_change*100:.1f}% overnight "
                    f"(${yesterday_close:.2f} → ${today_open:.2f}). "
                    f"Skipping signal check to prevent false signals."
                )
                notifier.send_alert(
                    f"⚠️ CORPORATE ACTION DETECTED\n"
                    f"{strat_cfg.ticker} moved {pct_change*100:.1f}% overnight.\n"
                    f"Likely stock split/reverse split.\n"
                    f"Signal check skipped today for safety."
                )
                _mark_periodic_check_done(
                    strat_cfg, now_et, morning_checks_done, bot_state, notifier
                )
                return

        timeout = order_manager_map[strat_cfg.ticker].risk.signal_timeout_seconds
        # Deliberately NOT a `with ThreadPoolExecutor(...) as executor:` block
        # (audit bug 2.7): exiting that block calls shutdown(wait=True) by
        # default, which blocks until the hung worker thread actually
        # finishes - completely defeating future.result(timeout=...) below.
        # Measured: 1.01s configured timeout, actual resume at 3.00s.
        # shutdown(wait=False) here lets this function return as soon as the
        # timeout fires, regardless of whether the underlying call ever
        # returns - the abandoned worker thread is not forcibly killed (not
        # possible in pure Python), but it no longer blocks the caller.
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            signal_engine_map[strat_cfg.ticker].generate_signal, df
        )
        try:
            signal_result = future.result(timeout=timeout)
        except FutureTimeoutError:
            logger.critical(
                f"Signal generation timed out after {timeout}s for "
                f"{strat_cfg.strategy.name}/{strat_cfg.ticker} - skipping this check"
            )
            notifier.send_error_alert(
                f"⏱️ Signal generation timeout: {strat_cfg.ticker} "
                f"(exceeded {timeout}s)"
            )
            # A timeout means we never reached a decision - do not mark the
            # week/day complete, so a later retry is still possible.
            _mark_periodic_check_attempted(
                strat_cfg, now_et, morning_checks_done, bot_state
            )
            executor.shutdown(wait=False)
            return
        executor.shutdown(wait=False)

        logger.info(
            f"Signal: {signal_result['signal']} | "
            f"Confidence: {signal_result['confidence']:.2%}"
        )
        logger.info(f"Reason: {signal_result['reason']}")

        if "indicators" in signal_result:
            indicator_str = " | ".join(
                [
                    f"{k}={v:.2f}" if isinstance(v, (int, float)) else f"{k}={v}"
                    for k, v in signal_result["indicators"].items()
                ]
            )
            logger.info(f"Indicators: {indicator_str}")

        if signal_result["signal"] == "HOLD":
            logger.info(
                f"Trade decision | Signal: HOLD | "
                f"Action: NO TRADE | Reason: {signal_result['reason']}"
            )

        if signal_result["signal"] in ("BUY", "SELL"):
            _sentiment_allowed = True
            _sentiment_pred: dict = {"gate_status": "not_applicable_exit"}

            # The sentiment gate is an ENTRY filter. AlphaLive is long-only:
            # a SELL closes/reduces a long, so it is never sentiment-gated
            # (previously every SELL was sent as direction 0 = "short" and
            # was blocked by positive sentiment, trapping the position).
            if alphasignal_client is not None and signal_result["signal"] == "BUY":
                _signal_direction = 2
                _sentiment_allowed, _sentiment_pred = asyncio.run(
                    run_pre_execution_checks(
                        alphasignal_client=alphasignal_client,
                        ticker=strat_cfg.ticker,
                        signal_direction=_signal_direction,
                    )
                )

            _gate_status = _sentiment_pred.get("gate_status", "N/A")
            logger.debug(
                f"AlphaSignal gate status for {strat_cfg.ticker}: {_gate_status}"
            )

            if not _sentiment_allowed:
                _score = _sentiment_pred.get("sentiment_score")
                logger.info(
                    "Execution blocked by sentiment filter - "
                    + (
                        f"sentiment_score={_score:.3f} "
                        if isinstance(_score, (int, float))
                        else "sentiment_score=none (no usable evidence) "
                    )
                    + f"gate_status={_gate_status} "
                    f"fail_policy={_sentiment_pred.get('fail_policy', 'n/a')}"
                )
            else:
                price = market_data.get_current_price(strat_cfg.ticker)
                try:
                    account = broker.get_account()
                except Exception as e:
                    _record_broker_call(order_manager_map, success=False, error=e)
                    raise
                _record_broker_call(order_manager_map, success=True)

                # Entry-only, like every other risk gate: a latched global
                # daily-loss halt must never keep a strategy SELL from
                # reducing exposure.
                if signal_result["signal"] == "BUY":
                    global_can_trade, global_reason = (
                        global_risk.check_global_daily_loss(
                            account_equity=account.equity,
                            max_daily_loss_pct=strat_cfg.risk.max_daily_loss_pct,
                        )
                    )
                else:
                    global_can_trade, global_reason = True, "OK"

                if not global_can_trade:
                    logger.warning(f"Trade blocked (global): {global_reason}")
                    result = {"status": "blocked", "reason": global_reason}
                else:
                    try:
                        all_positions = broker.get_all_positions()
                    except Exception as e:
                        _record_broker_call(order_manager_map, success=False, error=e)
                        raise
                    _record_broker_call(order_manager_map, success=True)
                    strategy_positions = [
                        p for p in all_positions if p.symbol == strat_cfg.ticker
                    ]
                    result = order_manager_map[strat_cfg.ticker].execute_signal(
                        ticker=strat_cfg.ticker,
                        signal=signal_result,
                        current_price=price,
                        account_equity=account.equity,
                        current_positions_count=len(strategy_positions),
                        total_portfolio_positions=len(all_positions),
                        current_bar=df.index,
                    )

                if result["status"] == "success":
                    # Order ACCEPTED by the broker. The position ledger, P&L
                    # and risk counters are updated only from broker-confirmed
                    # fills (OrderManager._apply_order_progress), which may
                    # arrive now or on a later sync.
                    if result.get("order_status") == "dry_run":
                        logger.info(
                            f"Dry run: no order sent | {signal_result['signal']} "
                            f"{result.get('requested_qty')} {strat_cfg.ticker}"
                        )
                    else:
                        logger.info(
                            f"Order submitted | {signal_result['signal']} "
                            f"{result.get('requested_qty')} {strat_cfg.ticker} | "
                            f"order {result['order_id']} "
                            f"status={result.get('order_status')} "
                            f"| confirmed filled {result.get('filled_qty')}"
                        )
                elif result["status"] == "blocked":
                    logger.warning(f"Trade blocked: {result['reason']}")
                    logger.info(
                        f"Trade decision | Signal: {signal_result['signal']} | "
                        f"Action: BLOCKED | Reason: {result['reason']}"
                    )
                else:
                    logger.error(f"Trade error: {result['reason']}")

    except DataStaleError as e:
        # A data-fetch/staleness failure is not a completed evaluation -
        # never mark the week/day done, so a later retry can still happen.
        logger.warning(f"Data staleness during signal check: {e}")
        notifier.send_error_alert(f"⚠️ Data staleness on {strat_cfg.ticker}: {str(e)}")
        _mark_periodic_check_attempted(
            strat_cfg, now_et, morning_checks_done, bot_state
        )
        return

    except Exception as e:
        # Likewise, an evaluation exception is not a completed evaluation.
        logger.error(
            f"Signal check error [{strat_cfg.strategy.name}/{strat_cfg.ticker}]: {e}",
            exc_info=True,
        )
        notifier.send_error_alert(
            f"Signal check failed: {strat_cfg.strategy.name}/{strat_cfg.ticker}"
        )
        _mark_periodic_check_attempted(
            strat_cfg, now_et, morning_checks_done, bot_state
        )
        return

    # Persist engine state (stateful strategies must survive restarts).
    # Saved regardless of outcome - generate_signal() already mutated the
    # engine, and this must faithfully reflect what the engine believes.
    bot_state.save_engine_state(
        strat_cfg.ticker, signal_engine_map[strat_cfg.ticker].get_state()
    )

    # Reached here only via a genuine decision (HOLD, blocked, or an order
    # attempt regardless of its outcome) - a completed evaluation.
    _mark_periodic_check_done(
        strat_cfg, now_et, morning_checks_done, bot_state, notifier
    )
    last_signal_check_map[strat_cfg.ticker] = time.time()


def _run_exit_checks(
    broker,
    market_data,
    order_manager_map: dict,
    bot_state,
    app_config,
    notifier,
    global_risk,
    signal_engine_map: dict = None,
) -> None:
    """Check stop loss, take profit, and trailing stop for all open positions."""
    try:
        try:
            positions = broker.get_all_positions()
        except Exception as e:
            _record_broker_call(order_manager_map, success=False, error=e)
            raise
        _record_broker_call(order_manager_map, success=True)

        try:
            _apply_corporate_actions(positions, bot_state, notifier)
        except Exception as e:  # bookkeeping must never block exits/reconciliation
            logger.error(f"Corporate-action check failed: {e}", exc_info=True)

        if positions:
            current_prices = {}
            for pos in positions:
                try:
                    current_prices[pos.symbol] = market_data.get_current_price(
                        pos.symbol
                    )
                except Exception as e:
                    logger.warning(f"Failed to get price for {pos.symbol}: {e}")
                    current_prices[pos.symbol] = pos.current_price

            for pos in positions:
                ticker = pos.symbol

                if ticker not in order_manager_map:
                    logger.warning(
                        f"No order manager for {ticker}, skipping exit check"
                    )
                    continue

                bot_state.set_position_high(ticker, pos.current_price)
                known_high = bot_state.get_position_high(ticker) or pos.avg_entry_price

                pos_dict = [
                    {
                        "ticker": ticker,
                        "avg_entry": pos.avg_entry_price,
                        "side": pos.side,
                        "qty": pos.qty,
                        "highest_since_entry": known_high,
                    }
                ]

                exits = order_manager_map[ticker].check_exits(pos_dict, current_prices)

                for exit_signal in exits:
                    logger.info(
                        f"EXIT: {exit_signal['ticker']} - {exit_signal['reason']}"
                    )

                    if app_config.dry_run:
                        logger.info(f"[DRY RUN] Would SELL {exit_signal['ticker']}")
                        continue

                    result = order_manager_map[ticker].close_position(
                        ticker=exit_signal["ticker"],
                        reason=exit_signal["reason"],
                    )

                    if result["status"] == "success":
                        # Confirmed flat at the broker. The fill itself (P&L,
                        # ledger, risk counters) was booked from the broker's
                        # order report by OrderManager._apply_order_progress -
                        # booking it again here would double count.
                        bot_state.clear_position_high(ticker)
                        bot_state.clear_entry_timestamp(ticker)

                        # SL/TP closed the position outside the signal engine -
                        # reset the engine to flat or it thinks it's still in
                        # position and never re-enters (nor exits again).
                        if signal_engine_map and ticker in signal_engine_map:
                            signal_engine_map[ticker].restore_state(
                                {
                                    "in_position": False,
                                    "entry_price": 0.0,
                                    "peak_price": 0.0,
                                }
                            )
                            bot_state.save_engine_state(
                                ticker, signal_engine_map[ticker].get_state()
                            )
                        notifier.send_alert(
                            f"Position closed by risk exit: {ticker} - "
                            f"{exit_signal['reason']}"
                        )

    except Exception as e:
        logger.error(f"Exit check error: {e}", exc_info=True)
        notifier.send_error_alert(f"Exit check failed: {str(e)}")


def _sync_position_ledger(broker, bot_state, notifier) -> None:
    """Reconcile the persisted position ledger with the broker at startup.

    A restart with a lost/stale state file is expected (ephemeral STATE_FILE,
    first boot, manual cleanup) - absorb differences here with a warning
    instead of letting the mid-session drift check auto-halt on them.
    """
    try:
        alpaca_positions = broker.get_all_positions()
    except Exception as e:
        logger.warning(f"Startup ledger sync skipped (broker error): {e}")
        return

    alpaca_by_ticker = {pos.symbol: pos for pos in alpaca_positions}
    ledger = bot_state.get_open_positions()

    for symbol, pos in alpaca_by_ticker.items():
        if symbol not in ledger:
            logger.warning(
                f"Startup sync: broker holds {symbol} ({pos.qty} @ "
                f"${pos.avg_entry_price:.2f}) not in ledger - adopting it."
            )
            bot_state.record_position_open(symbol, pos.qty, pos.avg_entry_price)
            notifier.send_alert(
                f"ℹ️ Startup sync: adopted untracked position {symbol} "
                f"({pos.qty} @ ${pos.avg_entry_price:.2f}) into the ledger."
            )

    for symbol in ledger:
        if symbol not in alpaca_by_ticker:
            logger.warning(
                f"Startup sync: ledger tracks {symbol} but broker shows no "
                f"position - removing from ledger (closed while bot was down?)."
            )
            bot_state.record_position_close(symbol)
            notifier.send_alert(
                f"ℹ️ Startup sync: {symbol} in ledger but not at broker - "
                f"removed. If unexpected, check Alpaca order history."
            )


def _reconcile_open_submission_intents(
    order_manager_map: dict, bot_state, notifier
) -> None:
    """Startup reconciliation for durable submission intents.

    Every intent a previous run left non-terminal is resolved against the
    broker through OrderManager.sync_open_intents() - the same code path the
    main loop runs every iteration: fills that happened while the bot was
    down are booked exactly once, finished orders get their terminal
    status, never-sent ("prepared") intents are abandoned. An intent that
    stays unresolved keeps blocking new orders for its ticker.
    """
    open_intents = bot_state.list_open_intents()
    if not open_intents:
        return

    logger.info(f"Startup: reconciling {len(open_intents)} open submission intent(s)")
    configured = set(order_manager_map)
    for intent in open_intents:
        if intent["ticker"] not in configured:
            logger.warning(
                f"Startup: open submission intent {intent['intent_id']} for "
                f"{intent['ticker']} but no OrderManager is configured for it - "
                f"leaving it as-is for manual review."
            )
    for ticker, om in order_manager_map.items():
        try:
            results = om.sync_open_intents()
        except Exception as e:
            logger.error(f"Startup: intent reconciliation failed for {ticker}: {e}")
            continue
        for res in results:
            action = res.get("action") or res.get("outcome")
            logger.info(
                f"Startup: intent {res['intent_id']} ({ticker}) -> {action} "
                f"({res.get('detail', '')})"
            )
            if action == "quarantine":
                notifier.send_alert(
                    f"⚠️ Startup: submission intent for {ticker} is uncertain "
                    f"({res.get('detail')}) - quarantined. No new order for this "
                    f"ticker until it resolves."
                )


def _run_position_reconciliation(
    broker,
    order_manager_map: dict,
    app_config,
    notifier,
    bot_state,
) -> None:
    """Compare Alpaca positions against the persisted ledger; auto-halt on drift.

    Uses BotState's open-position ledger (survives restarts, not reset daily)
    rather than OrderManager's order history, which only covers today - a
    multi-day hold would otherwise look like drift every morning.
    """
    try:
        # Book any fills that landed since this iteration's sync, then
        # leave tickers with an order still working out of the comparison:
        # the ledger moves only on confirmed fills, so a fill arriving
        # between the two reads would otherwise look like drift and halt.
        for om in order_manager_map.values():
            try:
                om.sync_open_intents()
            except Exception as e:
                logger.warning(f"Pre-reconciliation intent sync failed: {e}")
        # Only orders known to be working count; a quarantined (uncertain)
        # intent is an unknown outcome and drift beside it must still halt.
        busy = {
            i["ticker"]
            for i in bot_state.list_open_intents()
            if i.get("status") in ("submitting", "submitted", "partially_filled")
        }
        try:
            alpaca_positions = broker.get_all_positions()
        except Exception as e:
            _record_broker_call(order_manager_map, success=False, error=e)
            raise
        _record_broker_call(order_manager_map, success=True)
        try:
            _apply_corporate_actions(alpaca_positions, bot_state, notifier)
        except Exception as e:  # bookkeeping must never block exits/reconciliation
            logger.error(f"Corporate-action check failed: {e}", exc_info=True)

        alpaca_tickers = {
            pos.symbol: {
                "symbol": pos.symbol,
                "qty": pos.qty,
                "avg_entry_price": pos.avg_entry_price,
                "side": pos.side,
            }
            for pos in alpaca_positions
        }

        ledger = bot_state.get_open_positions()
        internal_tickers: set = set(ledger.keys())

        drift_detected = False

        if busy:
            logger.info(
                f"Reconciliation skips tickers with orders in flight: {sorted(busy)}"
            )
        for ticker, alpaca_pos in alpaca_tickers.items():
            if ticker in busy:
                continue
            if ticker not in internal_tickers:
                drift_detected = True
                logger.critical(
                    f"POSITION DRIFT: Alpaca has {ticker} ({alpaca_pos['qty']} shares) "
                    f"but bot has no record. This indicates a tracking failure."
                )
                notifier.send_alert(
                    f"🚨 <b>CRITICAL: POSITION DRIFT DETECTED</b>\n\n"
                    f"<b>Ticker:</b> {ticker}\n"
                    f"<b>Alpaca Position:</b> {alpaca_pos['qty']} shares "
                    f"@ ${alpaca_pos['avg_entry_price']:.2f}\n"
                    f"<b>Bot Position:</b> NOT TRACKED\n\n"
                    f"⛔ <b>TRADING AUTO-PAUSED</b>\n"
                    f"Fix: Set TRADING_PAUSED=false in Railway after verifying positions."
                )
                continue

            ledger_qty = ledger.get(ticker, {}).get("qty")
            if (
                ledger_qty is not None
                and abs(float(ledger_qty) - float(alpaca_pos["qty"])) > 1e-6
            ):
                drift_detected = True
                logger.critical(
                    f"POSITION DRIFT: qty mismatch for {ticker}: ledger tracks "
                    f"{ledger_qty} shares, Alpaca shows {alpaca_pos['qty']}."
                )
                notifier.send_alert(
                    f"🚨 <b>CRITICAL: POSITION DRIFT DETECTED</b>\n\n"
                    f"<b>Ticker:</b> {ticker}\n"
                    f"<b>Alpaca Position:</b> {alpaca_pos['qty']} shares\n"
                    f"<b>Bot Ledger:</b> {ledger_qty} shares (quantity mismatch)\n\n"
                    f"⛔ <b>TRADING AUTO-PAUSED</b>\n"
                    f"Fix: Set TRADING_PAUSED=false in Railway after verifying positions."
                )

        for ticker in internal_tickers:
            if ticker in busy:
                continue
            if ticker not in alpaca_tickers:
                drift_detected = True
                logger.critical(
                    f"POSITION DRIFT: Bot tracks {ticker} but Alpaca doesn't. "
                    f"Position may have been closed externally or never filled."
                )
                notifier.send_alert(
                    f"🚨 <b>CRITICAL: POSITION DRIFT DETECTED</b>\n\n"
                    f"<b>Ticker:</b> {ticker}\n"
                    f"<b>Bot Position:</b> TRACKED\n"
                    f"<b>Alpaca Position:</b> NOT FOUND\n\n"
                    f"⛔ <b>TRADING AUTO-PAUSED</b>\n"
                    f"Fix: Set TRADING_PAUSED=false in Railway after verifying positions."
                )

        if drift_detected:
            logger.critical(
                "AUTO-HALTING TRADING due to position drift. "
                "Manual intervention required. Set TRADING_PAUSED=false to resume."
            )
            os.environ["TRADING_PAUSED"] = "true"
            app_config.trading_paused = True
            notifier.send_alert(
                "⛔ <b>TRADING HALTED AUTOMATICALLY</b>\n\n"
                "Position reconciliation detected drift between bot and broker.\n"
                "All new signals will be blocked until you:\n"
                "1. Review positions in Alpaca dashboard\n"
                "2. Verify bot state is correct\n"
                "3. Set TRADING_PAUSED=false in Railway Variables\n\n"
                "Exit monitoring will continue for existing positions."
            )

    except Exception as e:
        logger.error(f"Position reconciliation error: {e}", exc_info=True)


def main(
    config_path: str,
    dry_run: bool = False,
    paper: bool = True,
    replay_mode: bool = False,
    replay_start: str = "2015-01-01",
    replay_end: str = "2019-12-31",
    replay_speed: int = 0,
):
    """
    Main entry point for AlphaLive.

    Runs forever on Railway (or simulates with replay mode).

    Args:
        config_path: Path to strategy JSON config
        dry_run: Log trades without executing (default False)
        paper: Use paper trading (default True)
        replay_mode: Use replay mode with historical data (default False)
        replay_start: Replay start date (default "2015-01-01")
        replay_end: Replay end date (default "2019-12-31")
        replay_speed: Replay speed multiplier (default 0 = instant)
    """

    # Verify timezone on startup
    now_et = datetime.now(ET)
    logger.info("=" * 80)
    logger.info(f"AlphaLive Starting | {now_et.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    logger.info(f"Timezone: {now_et.tzname()} (verified)")
    logger.info("=" * 80)

    # 1. Load and validate config (supports single file or directory)
    logger.info(f"Loading strategy config(s): {config_path}")
    all_strategy_configs = load_config_path(config_path)
    app_config = load_env()

    # Log strategy count
    if len(all_strategy_configs) == 1:
        logger.info(f"Single-strategy mode: {all_strategy_configs[0].strategy.name}")
    else:
        logger.info(
            f"Multi-strategy mode: {len(all_strategy_configs)} strategies loaded"
        )
        for i, cfg in enumerate(all_strategy_configs, 1):
            logger.info(
                f"  [{i}] {cfg.strategy.name} on {cfg.ticker} @ {cfg.timeframe}"
            )

    # State file - used for dashboard kill switch and trailing stop persistence
    # exclusive=True: one execution process per state file. A second
    # process (overlapping redeploy, accidental double start) fails fast
    # here instead of overwriting the first one's intents/ledger.
    try:
        bot_state = BotState(app_config.state_file, exclusive=True)
    except StateLockError as e:
        logger.critical(str(e))
        sys.exit(1)

    # Override with command-line args
    app_config.dry_run = dry_run or app_config.dry_run
    app_config.broker.paper = paper
    # A --dry-run flag (not only the DRY_RUN env var) also arms the
    # read-only broker transport, unless that is set explicitly.
    if app_config.dry_run:
        os.environ.setdefault(READ_ONLY_ENV, "true")

    if not validate_all(all_strategy_configs, app_config):
        logger.critical("Configuration validation failed. Exiting.")
        sys.exit(1)

    # Refuse to start any strategy with trailing_stop_enabled=true unless
    # persistent storage is configured - a Railway redeploy mid-day would
    # otherwise silently reset position_highs and miscalculate trailing
    # stops (real money risk). This was previously defined but never
    # called anywhere (audit bug 2.4) - wired in here, immediately after
    # config validation and before any broker connection or notifier setup,
    # so it's checked as early as every other startup gate. No notifier is
    # available yet at this point in startup, so the Telegram alert this
    # function sends on failure is skipped (its exit + log message alone
    # are sufficient to block startup); it still exits before the bot ever
    # reaches the trading loop.
    for strategy_config in all_strategy_configs:
        check_trailing_stop_requirements(strategy_config)

    # Start the Railway healthcheck server (daemon thread; safe no-op locally
    # if HEALTH_SECRET isn't set).
    from alphalive.health import create_health_server

    create_health_server(
        all_strategy_configs[0],
        dry_run=app_config.dry_run,
        paper=app_config.broker.paper,
    )

    # AlphaSignal identity preflight, before any broker contact. A missing
    # URL or a URL answered by a different service is a configuration error
    # and stops startup; an unreachable service is an outage - logged, and
    # the fail policy decides entries until it is verified.
    alphasignal_identity = (IDENTITY_UNCHECKED, "")
    if app_config.alphasignal.enabled:
        if not app_config.alphasignal.url:
            logger.critical(
                "ALPHASIGNAL_ENABLED=true but ALPHASIGNAL_URL is not set. Set it "
                "explicitly (there is no default) or set ALPHASIGNAL_ENABLED=false. "
                "Exiting."
            )
            sys.exit(1)
        alphasignal_identity = asyncio.run(
            AlphaSignalClient(
                base_url=app_config.alphasignal.url,
                timeout_seconds=app_config.alphasignal.timeout_seconds,
            ).check_identity()
        )
        status, detail = alphasignal_identity
        if status == IDENTITY_WRONG_SERVICE:
            logger.critical(
                f"ALPHASIGNAL_URL {app_config.alphasignal.url} is not an AlphaSignal "
                f"service ({detail}). Fix ALPHASIGNAL_URL. Exiting."
            )
            sys.exit(1)
        if status == IDENTITY_UNREACHABLE:
            logger.warning(
                f"AlphaSignal at {app_config.alphasignal.url} is unreachable at "
                f"startup ({detail}); identity unverified. Entries follow "
                f"ALPHASIGNAL_FAIL_POLICY={app_config.alphasignal.fail_policy} "
                "until it answers as AlphaSignal."
            )
        else:
            logger.info(f"AlphaSignal identity verified: {detail}")

    # 2. Initialize components
    logger.info("Initializing subsystems...")

    broker = AlpacaBroker(
        api_key=app_config.broker.api_key,
        secret_key=app_config.broker.secret_key,
        paper=app_config.broker.paper,
        base_url=app_config.broker.base_url,
    )

    if not broker.connect():
        logger.critical("Failed to connect to Alpaca. Exiting.")
        sys.exit(1)  # Railway will restart the process

    market_data = MarketDataFetcher(
        api_key=app_config.broker.api_key, secret_key=app_config.broker.secret_key
    )

    # AlphaSignal sentiment client (Integration AS → AL)
    alphasignal_client: AlphaSignalClient | None = None
    if app_config.alphasignal.enabled:
        alphasignal_client = AlphaSignalClient(
            base_url=app_config.alphasignal.url,
            api_key=app_config.alphasignal.api_key,
            timeout_seconds=app_config.alphasignal.timeout_seconds,
            sentiment_threshold=app_config.alphasignal.sentiment_threshold,
            fail_policy=app_config.alphasignal.fail_policy,
            verify_identity=True,
        )
        alphasignal_client.identity, alphasignal_client.identity_detail = (
            alphasignal_identity
        )
        logger.info(
            f"AlphaSignal sentiment filter enabled | "
            f"URL: {app_config.alphasignal.url} | "
            f"Threshold: {app_config.alphasignal.sentiment_threshold} | "
            f"Timeout: {app_config.alphasignal.timeout_seconds}s | "
            f"Fail policy (entries): {app_config.alphasignal.fail_policy}"
        )
    else:
        logger.info("AlphaSignal sentiment filter disabled (ALPHASIGNAL_ENABLED=false)")

    # Multi-strategy support: Create maps for signal engines, risk managers, and order managers
    # For simplicity in this implementation, each strategy has its own risk manager
    # (portfolio-level limits are checked by summing across all strategies)
    signal_engine_map = {}
    risk_manager_map = {}
    order_manager_map = {}
    global_risk = GlobalRiskManager(bot_state=bot_state)

    notifier = TelegramNotifier(
        bot_token=app_config.telegram.bot_token,
        chat_id=app_config.telegram.chat_id,
        enabled=app_config.telegram.enabled,
    )

    for strategy_config in all_strategy_configs:
        strategy_name = strategy_config.strategy.name
        ticker = strategy_config.ticker

        # Create signal engine for this strategy
        signal_engine_map[ticker] = SignalEngine(strategy_config)

        # Create risk manager for this strategy
        risk_manager_map[ticker] = RiskManager(
            risk_config=strategy_config.risk,
            execution_config=strategy_config.execution,
            strategy_name=strategy_name,
            safety_limits=strategy_config.safety_limits,
            global_risk=global_risk,
        )
        # Persisted daily counters (trade count, realised P&L, loss streak,
        # breaker, cooldown bar) - a restart must not reset risk limits.
        risk_manager_map[ticker].attach_state(bot_state, key=ticker)

        # Create order manager for this strategy
        order_manager_map[ticker] = OrderManager(
            broker=broker,
            risk_manager=risk_manager_map[ticker],
            config=strategy_config,
            notifier=notifier,
            dry_run=app_config.dry_run,
            state=bot_state,
        )

        # Register with the global risk manager so max_daily_loss_pct is
        # enforced across all strategies combined, not just per-strategy.
        global_risk.register_strategy(ticker, risk_manager_map[ticker])

        logger.info(f"  Initialized components for {strategy_name} ({ticker})")

    # Reconcile any submission intent a crash left non-terminal, BEFORE the
    # main loop starts (objective 1) - closes the restart-duplicate-order
    # window as far as possible: a fresh process resolves what the broker
    # actually did with any in-flight order from the last run instead of
    # generating a new client_order_id and risking a duplicate.
    _reconcile_open_submission_intents(order_manager_map, bot_state, notifier)

    # Only AFTER intents are reconciled (fills that happened while the bot
    # was down are now booked once, through their intents) reconcile the
    # ledger with broker positions. Anything still unexplained is adopted
    # (or removed) with an alert; only mid-session drift auto-halts.
    _sync_position_ledger(broker, bot_state, notifier)

    # Restore stateful signal-engine state (in_position/entry/peak) from the
    # state file, reconciled against the just-synced position ledger.
    _restore_engine_states(all_strategy_configs, signal_engine_map, bot_state)

    # Gate greenblatt_weekly's opt-in exits on the minimum holding period.
    _wire_min_hold_checkers(all_strategy_configs, signal_engine_map, bot_state)

    # Monthly Greenblatt screener (None unless SCREENER_UNIVERSE is set).
    screener = _build_screener()

    # 5. Initialize Telegram command listener
    # Polls for inbound commands (/status, /pause, /resume, etc.) on background thread
    # NOTE: /pause and /resume route through the shared `global_risk` object
    # (GlobalRiskManager.set_manual_pause/clear_manual_pause) so they gate
    # every registered strategy's RiskManager.can_trade(), not just the
    # first one. /status, /config, /performance, /close_all still reflect
    # only the first configured strategy's broker/order-manager view -
    # that part of multi-strategy Telegram support remains out of scope
    # for this pass (see CLAUDE.md).
    cmd_listener = None
    if app_config.telegram.enabled:
        first_strategy = all_strategy_configs[0]
        first_ticker = first_strategy.ticker

        cmd_listener = TelegramCommandListener(
            bot_token=app_config.telegram.bot_token,
            chat_id=app_config.telegram.chat_id,
            order_manager=order_manager_map[first_ticker],
            risk_manager=risk_manager_map[first_ticker],
            broker=broker,
            notifier=notifier,
            config=first_strategy,
            global_risk=global_risk,
            order_manager_map=order_manager_map,
            strategy_configs={cfg.ticker: cfg for cfg in all_strategy_configs},
        )
        cmd_listener.start()
        logger.info("Telegram command listener started (polling every 5s)")
    else:
        logger.info("Telegram command listener disabled (Telegram not configured)")

    logger.info("All subsystems initialized successfully")

    # Startup data backfill and warmup validation
    _run_startup_warmup(all_strategy_configs, market_data, signal_engine_map, notifier)

    # 3. Send startup message
    mode = "DRY RUN" if app_config.dry_run else ("PAPER" if paper else "🔴 LIVE")
    notifier.send_message(
        _build_startup_message(all_strategy_configs, mode), parse_mode="HTML"
    )

    # 4. State tracking
    today_str = None  # Track current trading day
    eod_summary_sent = False  # Has end-of-day summary been sent?
    eod_summary_retry = False  # Did EOD summary fail? Retry once on next loop
    last_exit_check = 0  # Timestamp of last exit condition check
    last_position_reconciliation = 0  # Timestamp of last position reconciliation

    # State tracking for multi-strategy
    morning_checks_done = set()  # Set of tickers that have had morning check today
    last_signal_check_map = {}  # {ticker: timestamp} for 1Hour/15Min strategies

    # Intraday drawdown monitoring
    peak_equity_today: float = 0.0  # Highest equity seen today (set at open)
    morning_equity: float = 0.0  # Equity at market open (fixes TODO in EOD summary)
    drawdown_alert_sent: bool = False  # Avoid spamming alert on same breach
    DRAWDOWN_ALERT_PCT = float(
        os.getenv("DRAWDOWN_ALERT_PCT", "3.0")
    )  # Alert at 3% intraday DD

    # 5. SIGTERM handler for graceful Railway shutdown
    def handle_sigterm(signum, frame):
        logger.info("SIGTERM received - Railway is restarting/stopping")

        # Stop command listener
        if cmd_listener is not None:
            cmd_listener.stop()
            logger.info("Telegram command listener stopped")

        # Get final stats
        try:
            # Aggregate order history across all strategies
            all_orders = []
            for ticker in order_manager_map:
                all_orders.extend(order_manager_map[ticker].get_order_history())

            account = broker.get_account()
            daily_stats = _compute_daily_stats(
                all_orders, morning_equity, account.equity
            )
            summary = {
                "trades": len(all_orders),
                "pnl": daily_stats["pnl"],
                "win_rate": daily_stats["win_rate"],
                "portfolio_value": account.equity,
            }
            notifier.send_shutdown_notification(summary)
        except Exception as e:
            logger.error(f"Failed to send shutdown notification: {e}")

        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)  # Also handle Ctrl+C for local testing

    # 6. Replay mode OR 24/7 live loop
    if replay_mode:
        # === REPLAY MODE ===
        # Simulate trading through historical data (FREE - no subscription needed)
        logger.info("=" * 80)
        logger.info("REPLAY MODE")
        logger.info(f"Period: {replay_start} to {replay_end}")
        logger.info(
            f"Speed: {'instant' if replay_speed == 0 else f'{replay_speed}s per day'}"
        )
        logger.info("=" * 80)

        from alphalive.replay import ReplaySimulator

        # Create replay simulator
        simulator = ReplaySimulator(
            broker=broker,
            start_date=replay_start,
            end_date=replay_end,
            tickers=[cfg.ticker for cfg in all_strategy_configs],
            speed_multiplier=replay_speed,
        )

        # Run simulation
        simulator.run(
            strategy_configs=all_strategy_configs,
            signal_engines=signal_engine_map,
            risk_managers=risk_manager_map,
            notifier=notifier,
        )

        # Exit after replay completes
        logger.info("Replay mode complete - exiting")
        sys.exit(0)

    # === LIVE MODE ===
    # Main loop - runs FOREVER
    logger.info(f"AlphaLive running 24/7. Mode: {mode}.")
    logger.info("Press Ctrl+C to stop (or wait for Railway SIGTERM)")

    while True:
        try:
            now_et = datetime.now(ET)
            current_day = now_et.strftime("%Y-%m-%d")

            # --- Check command listener thread health ---
            if cmd_listener is not None and not cmd_listener.thread.is_alive():
                logger.error("Telegram command listener thread died")
                notifier.send_error_alert(
                    "⚠️ Command listener offline - /pause and /resume unavailable. "
                    "Restart service to restore."
                )
                # Set to None to avoid spamming alerts every loop iteration
                cmd_listener = None

            # --- New day reset ---
            if current_day != today_str:
                today_str = current_day
                morning_checks_done = set()
                last_signal_check_map = {}
                eod_summary_sent = False
                eod_summary_retry = False
                peak_equity_today = 0.0
                # Restore today's morning equity if this "new day" is really a
                # mid-day/post-close restart - the market-open capture path
                # never runs after 4 PM, and the EOD summary needs it.
                morning_equity = bot_state.get_morning_equity(current_day) or 0.0
                if morning_equity > 0:
                    logger.info(
                        f"Morning equity restored from state: ${morning_equity:,.2f}"
                    )
                drawdown_alert_sent = False

                # The persisted daily P&L (dashboard Daily P&L / loss gauge) is
                # date-keyed; without this it accumulated across days.
                bot_state.roll_daily_pnl(current_day)

                # Reset daily for all strategies
                for ticker in risk_manager_map:
                    risk_manager_map[ticker].reset_daily()
                    order_manager_map[ticker].reset_daily()

                logger.info(
                    f"=== New trading day: {current_day} ({now_et.strftime('%A')}) ==="
                )

            # --- Monthly Greenblatt screener (no-op unless a new month) ---
            # Runs even when trading is paused/market closed - it's
            # informational (dashboard + AlphaLab candidate list), not execution.
            _run_monthly_screener(screener, bot_state, notifier, now_et)

            # --- Pause controls (dashboard / Telegram / TRADING_PAUSED) ---
            # Same entry-only policy for all three (see
            # GlobalRiskManager.entries_paused_reason): no new entries, open
            # entry orders cancelled, exits and fill sync keep running.
            _pause_reason = global_risk.entries_paused_reason()
            if _pause_reason is None:
                try:
                    if read_bool_env("TRADING_PAUSED", default=False):
                        _pause_reason = "TRADING_PAUSED env var"
                except ValueError:
                    _pause_reason = "TRADING_PAUSED env var (malformed - failing safe)"
            if _pause_reason:
                logger.info(f"New entries paused ({_pause_reason}); exits stay active")

            # --- Market closed? Sleep longer ---
            if not broker.is_market_open():
                # Weekend: sleep 30 minutes
                if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
                    logger.debug("Weekend. Sleeping 30 min.")
                    time.sleep(1800)
                    continue

                # Before 9:30 AM ET: sleep until closer to open
                if now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 30):
                    logger.debug(
                        f"Pre-market ({now_et.strftime('%H:%M %Z')}). Sleeping 5 min."
                    )
                    time.sleep(300)
                    continue

                # After 4 PM ET: send EOD summary, then sleep until midnight
                if now_et.hour >= 16:
                    if not eod_summary_sent:
                        # Set flag first - prevents infinite retry loop if send fails
                        eod_summary_sent = True
                        try:
                            _send_eod_summary(
                                order_manager_map, broker, morning_equity, notifier
                            )
                            logger.info("EOD summary sent.")
                        except Exception as e:
                            logger.error(f"EOD summary error: {e}", exc_info=True)

                    logger.debug(
                        f"After hours ({now_et.strftime('%H:%M %Z')}). Sleeping 30 min."
                    )
                    time.sleep(1800)
                    continue

                # Market holiday or other closure
                logger.debug("Market closed (holiday?). Sleeping 5 min.")
                time.sleep(300)
                continue

            # === MARKET IS OPEN ===

            # --- Order lifecycle sync: book broker-confirmed fills and resolve
            # finished/abandoned intents BEFORE any new decision, exit check
            # or reconciliation reads the ledger. ---
            for _om in order_manager_map.values():
                try:
                    _om.sync_open_intents()
                except Exception as e:
                    logger.error(f"Order sync failed: {e}", exc_info=True)
            _sync_engines_with_ledger(signal_engine_map, bot_state)
            if _pause_reason:
                for _om in order_manager_map.values():
                    try:
                        _om.cancel_open_entry_orders(_pause_reason)
                    except Exception as e:
                        logger.error(f"Entry-order cancellation failed: {e}")

            # --- Intraday drawdown monitoring ---
            try:
                try:
                    account = broker.get_account()
                except Exception as e:
                    _record_broker_call(order_manager_map, success=False, error=e)
                    raise
                _record_broker_call(order_manager_map, success=True)
                current_equity = account.equity

                # Capture morning equity once per day (first time market is open).
                # Persisted so a mid-day restart restores it instead of
                # re-capturing (or, after 4 PM, leaving it 0 and making the
                # EOD summary report the whole account as today's P&L).
                if morning_equity == 0.0 and current_equity > 0:
                    persisted = bot_state.get_morning_equity(current_day)
                    if persisted is not None and persisted > 0:
                        morning_equity = persisted
                        logger.info(
                            f"Morning equity restored from state: ${morning_equity:,.2f}"
                        )
                    else:
                        morning_equity = current_equity
                        bot_state.set_morning_equity(current_day, morning_equity)
                        logger.info(f"Morning equity captured: ${morning_equity:,.2f}")
                    peak_equity_today = max(peak_equity_today, current_equity)

                # Update peak
                if current_equity > peak_equity_today:
                    peak_equity_today = current_equity
                    drawdown_alert_sent = False  # Reset alert if we recover to new high

                # Check drawdown from today's peak
                if peak_equity_today > 0:
                    drawdown_pct = (
                        (peak_equity_today - current_equity) / peak_equity_today * 100
                    )
                    if drawdown_pct >= DRAWDOWN_ALERT_PCT and not drawdown_alert_sent:
                        logger.warning(
                            f"INTRADAY DRAWDOWN ALERT: {drawdown_pct:.2f}% from peak "
                            f"(peak=${peak_equity_today:,.2f}, now=${current_equity:,.2f})"
                        )
                        notifier.send_alert(
                            f"⚠️ INTRADAY DRAWDOWN ALERT\n"
                            f"Portfolio down {drawdown_pct:.1f}% from today's peak\n"
                            f"Peak: ${peak_equity_today:,.2f}\n"
                            f"Now:  ${current_equity:,.2f}\n"
                            f"Loss: ${peak_equity_today - current_equity:,.2f}\n\n"
                            f"All strategies still active. Use /pause to halt trading."
                        )
                        drawdown_alert_sent = True
            except Exception as e:
                logger.warning(f"Drawdown check failed: {e}")

            # --- Signal checks (multi-strategy + timeframe-aware) ---
            # 1Day/1Week: once per day at the 9:35 AM open window.
            # 1Hour/15Min: bar-boundary checks throughout the session.
            for strat_cfg in all_strategy_configs:
                _check_signal_for_strategy(
                    strat_cfg,
                    now_et,
                    morning_checks_done,
                    last_signal_check_map,
                    market_data,
                    signal_engine_map,
                    order_manager_map,
                    alphasignal_client,
                    broker,
                    notifier,
                    global_risk,
                    bot_state,
                )

            # --- Exit condition checks (every 5 minutes during market hours) ---
            if time.time() - last_exit_check >= 300:  # 5 minutes
                _run_exit_checks(
                    broker,
                    market_data,
                    order_manager_map,
                    bot_state,
                    app_config,
                    notifier,
                    global_risk,
                    signal_engine_map,
                )
                last_exit_check = time.time()

            # --- Position reconciliation (every 30 minutes during market hours) ---
            # Auto-halts trading if Alpaca positions diverge from internal order history.
            if time.time() - last_position_reconciliation >= 1800:  # 30 minutes
                _run_position_reconciliation(
                    broker, order_manager_map, app_config, notifier, bot_state
                )
                last_position_reconciliation = time.time()

            # --- End of day summary (3:55 PM ET) ---
            # EOD summary retry logic: set flag first, attempt send, if it fails,
            # set retry flag and try ONCE more on the next loop. This prevents
            # infinite retry loops while still catching transient failures.
            if not eod_summary_sent and now_et.hour == 15 and now_et.minute >= 55:
                eod_summary_sent = True  # Set flag before attempting
                try:
                    _send_eod_summary(
                        order_manager_map, broker, morning_equity, notifier
                    )
                    logger.info("=== End of Day Summary ===")
                except Exception as e:
                    logger.error(f"EOD summary error: {e}", exc_info=True)
                    if not eod_summary_retry:
                        # First failure - queue one retry on next loop
                        eod_summary_retry = True
                        eod_summary_sent = False
                        logger.warning(
                            "EOD summary failed - will retry once on next loop"
                        )

            # Retry EOD summary once if it failed earlier
            if eod_summary_retry and not eod_summary_sent:
                eod_summary_sent = True  # Set flag to prevent further retries
                try:
                    _send_eod_summary(
                        order_manager_map, broker, morning_equity, notifier
                    )
                    logger.info("EOD summary sent (retry succeeded)")
                except Exception as e:
                    logger.error(f"EOD summary retry failed: {e}", exc_info=True)
                    # Give up after one retry - don't spam

            # Sleep 30 seconds between checks during market hours.
            # Why 30s and not 5 minutes (the exit check interval)?
            # 1. Responsive to SIGTERM - Railway sends SIGTERM on deploy,
            #    and we want to catch it within 30s, not wait 5 minutes.
            # 2. The morning check and EOD summary are time-sensitive
            #    (9:35 AM, 3:55 PM) and need ~30s precision.
            # 3. The 5-minute exit check interval is enforced by the
            #    last_exit_check guard above, not by the sleep duration.
            time.sleep(30)

        except KeyboardInterrupt:
            # Local testing: Ctrl+C
            logger.info("KeyboardInterrupt received - shutting down")
            break
        except Exception as e:
            # Catch-all: log error, notify, sleep, and continue.
            # NEVER let the loop die - Railway will restart but we lose state.
            logger.error(f"Main loop error: {e}", exc_info=True)
            try:
                notifier.send_error_alert(f"Main loop error: {str(e)}")
            except Exception:
                pass  # Don't let notification failure crash the loop
            time.sleep(60)  # Wait a minute before retrying


if __name__ == "__main__":
    import argparse
    from alphalive.utils.logger import setup_logger

    # Setup logging
    setup_logger()

    # Parse arguments
    parser = argparse.ArgumentParser(description="AlphaLive 24/7 Trading Bot")
    parser.add_argument("--config", required=True, help="Path to strategy config JSON")
    parser.add_argument(
        "--dry-run", action="store_true", help="Log trades without executing"
    )
    parser.add_argument(
        "--live", action="store_true", help="Use live trading (default: paper)"
    )

    args = parser.parse_args()

    # Run main loop
    main(config_path=args.config, dry_run=args.dry_run, paper=not args.live)
