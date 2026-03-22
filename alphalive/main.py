"""
AlphaLive Main Entry Point (B9a)

24/7 trading loop that runs on Railway.
This is NOT a cron job. It's a persistent Python process that:
- Sleeps when market is closed
- Wakes up to trade when market is open
- Handles Railway restarts gracefully via SIGTERM
"""

import time
import os
import sys
import signal
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from alphalive.config import load_config_path, load_env, validate_all
from alphalive.broker.alpaca_broker import AlpacaBroker
from alphalive.data.market_data import MarketDataFetcher, DataStaleError
from alphalive.strategy.signal_engine import SignalEngine
from alphalive.execution.risk_manager import RiskManager
from alphalive.execution.order_manager import OrderManager
from alphalive.notifications.telegram_bot import TelegramNotifier
from alphalive.notifications.telegram_commands import TelegramCommandListener

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Timeframe-aware signal check intervals (B9b)
TIMEFRAME_CHECK_INTERVALS = {
    "1Day": None,  # Handled by morning_check_done flag
    "1Hour": 60,   # Check every 60 minutes
    "15Min": 15    # Check every 15 minutes
}


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


def main(
    config_path: str,
    dry_run: bool = False,
    paper: bool = True,
    replay_mode: bool = False,
    replay_start: str = "2015-01-01",
    replay_end: str = "2019-12-31",
    replay_speed: int = 0
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
        logger.info(f"Multi-strategy mode: {len(all_strategy_configs)} strategies loaded")
        for i, cfg in enumerate(all_strategy_configs, 1):
            logger.info(f"  [{i}] {cfg.strategy.name} on {cfg.ticker} @ {cfg.timeframe}")

    # Override with command-line args
    app_config.dry_run = dry_run or app_config.dry_run
    app_config.broker.paper = paper

    if not validate_all(all_strategy_configs, app_config):
        logger.critical("Configuration validation failed. Exiting.")
        sys.exit(1)

    # 2. Initialize components
    logger.info("Initializing subsystems...")

    broker = AlpacaBroker(
        api_key=app_config.broker.api_key,
        secret_key=app_config.broker.secret_key,
        paper=app_config.broker.paper,
        base_url=app_config.broker.base_url
    )

    if not broker.connect():
        logger.critical("Failed to connect to Alpaca. Exiting.")
        sys.exit(1)  # Railway will restart the process

    market_data = MarketDataFetcher(
        api_key=app_config.broker.api_key,
        secret_key=app_config.broker.secret_key
    )

    # Multi-strategy support: Create maps for signal engines, risk managers, and order managers
    # For simplicity in this implementation, each strategy has its own risk manager
    # (portfolio-level limits are checked by summing across all strategies)
    signal_engine_map = {}
    risk_manager_map = {}
    order_manager_map = {}

    notifier = TelegramNotifier(
        bot_token=app_config.telegram.bot_token,
        chat_id=app_config.telegram.chat_id,
        enabled=app_config.telegram.enabled
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
            safety_limits=strategy_config.safety_limits
        )

        # Create order manager for this strategy
        order_manager_map[ticker] = OrderManager(
            broker=broker,
            risk_manager=risk_manager_map[ticker],
            config=strategy_config,
            notifier=notifier,
            dry_run=app_config.dry_run
        )

        logger.info(f"  Initialized components for {strategy_name} ({ticker})")

    # 5. Initialize Telegram command listener (B14)
    # Polls for inbound commands (/status, /pause, /resume, etc.) on background thread
    # NOTE: For multi-strategy mode, uses first strategy's components
    # (command listener doesn't yet fully support multi-strategy)
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
            config=first_strategy
        )
        cmd_listener.start()
        logger.info("Telegram command listener started (polling every 5s)")
    else:
        logger.info("Telegram command listener disabled (Telegram not configured)")

    logger.info("All subsystems initialized successfully")

    # 2.5. Startup data backfill + warmup validation (B9b + B15 multi-strategy)
    # On any restart (including mid-day), fetch enough bars to warm up
    # all indicators before the first signal check. This prevents
    # the bot from generating garbage signals after a Railway restart.
    logger.info("Running startup data backfill and warmup validation...")
    for strategy_config in all_strategy_configs:
        ticker = strategy_config.ticker
        logger.info(f"  Warming up {strategy_config.strategy.name} ({ticker})...")

        try:
            df = market_data.get_latest_bars(
                ticker, strategy_config.timeframe, lookback_bars=250
            )
            # Validate data quality (will raise DataStaleError if too old)
            # This is critical on restart — if data is stale, the bot should
            # exit and let Railway restart it (which triggers a fresh data fetch)

            # Run a test signal to verify indicators are fully warmed
            test_signal = signal_engine_map[ticker].generate_signal(df)
            warmup_complete = test_signal.get("warmup_complete", True)

            if not warmup_complete:
                logger.warning(f"  Indicator warmup incomplete for {ticker} — signals may be unreliable")
                notifier.send_alert(
                    f"⚠️ Indicator warmup incomplete for {ticker} on startup. Some indicators have NaN values."
                )
            else:
                logger.info(
                    f"  Warmup complete for {ticker}: {len(df)} bars loaded, "
                    f"test signal: {test_signal['signal']}"
                )
                if len(all_strategy_configs) == 1:  # Only send detailed message for single strategy
                    notifier.send_message(
                        f"✅ <b>Startup warmup OK</b>\n"
                        f"Strategy: {strategy_config.strategy.name}\n"
                        f"Ticker: {ticker}\n"
                        f"Bars loaded: {len(df)}\n"
                        f"Test signal: {test_signal['signal']}\n"
                        f"Confidence: {test_signal['confidence']:.2%}",
                        parse_mode="HTML"
                    )

        except DataStaleError as e:
            logger.critical(f"Startup data staleness for {ticker}: {e}")
            notifier.send_error_alert(f"❌ Startup failed for {ticker}: {str(e)}")
            sys.exit(1)  # Exit and let Railway restart
        except Exception as e:
            logger.error(f"Startup warmup failed for {ticker}: {e}", exc_info=True)
            notifier.send_error_alert(f"⚠️ Startup warmup error for {ticker}: {str(e)}")
            # Don't exit — try to continue, but flag it

    # Send multi-strategy warmup summary
    if len(all_strategy_configs) > 1:
        notifier.send_message(
            f"✅ <b>Multi-strategy warmup complete</b>\n\n"
            f"Strategies: {len(all_strategy_configs)}\n"
            f"Tickers: {', '.join([cfg.ticker for cfg in all_strategy_configs])}",
            parse_mode="HTML"
        )

    # 3. Send startup message
    mode = "DRY RUN" if app_config.dry_run else ("PAPER" if paper else "🔴 LIVE")

    if len(all_strategy_configs) == 1:
        # Single strategy mode
        strategy_config = all_strategy_configs[0]
        startup_msg = (
            f"🚀 <b>AlphaLive Started</b>\n\n"
            f"<b>Mode:</b> {mode}\n"
            f"<b>Strategy:</b> {strategy_config.strategy.name}\n"
            f"<b>Ticker:</b> {strategy_config.ticker}\n"
            f"<b>Timeframe:</b> {strategy_config.timeframe}\n"
            f"<b>Risk:</b> SL {strategy_config.risk.stop_loss_pct}% / "
            f"TP {strategy_config.risk.take_profit_pct}%\n"
            f"<b>Backtest Sharpe:</b> {strategy_config.metadata.performance.sharpe_ratio:.2f}\n"
            f"<b>Platform:</b> Railway (24/7)\n\n"
            f"Bot is now monitoring the market."
        )
    else:
        # Multi-strategy mode
        strategy_list = "\n".join([
            f"  • {cfg.strategy.name} on {cfg.ticker} @ {cfg.timeframe}"
            for cfg in all_strategy_configs
        ])
        startup_msg = (
            f"🚀 <b>AlphaLive Started</b> (Multi-Strategy)\n\n"
            f"<b>Mode:</b> {mode}\n"
            f"<b>Strategies:</b> {len(all_strategy_configs)}\n\n"
            f"{strategy_list}\n\n"
            f"<b>Platform:</b> Railway (24/7)\n\n"
            f"Bot is now monitoring the market."
        )

    notifier.send_message(startup_msg, parse_mode="HTML")

    # 4. State tracking
    today_str = None           # Track current trading day
    eod_summary_sent = False   # Has end-of-day summary been sent?
    eod_summary_retry = False  # Did EOD summary fail? Retry once on next loop
    last_exit_check = 0        # Timestamp of last exit condition check
    last_position_reconciliation = 0  # Timestamp of last position reconciliation (B9b)

    # State tracking for multi-strategy (B9b + B15)
    morning_checks_done = set()  # Set of tickers that have had morning check today
    last_signal_check_map = {}   # {ticker: timestamp} for 1Hour/15Min strategies

    # TIMEFRAME-AWARE SIGNAL CHECKS (B9b):
    # For 1Day: use morning_checks_done set
    # For 1Hour/15Min: use should_run_signal_check() + last_signal_check_map

    # 5. SIGTERM handler for graceful Railway shutdown
    def handle_sigterm(signum, frame):
        logger.info("SIGTERM received — Railway is restarting/stopping")

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
            summary = {
                "trades": len(all_orders),
                "pnl": 0.0,  # TODO: Calculate actual P&L
                "win_rate": 0.0,
                "portfolio_value": account.equity
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
        logger.info("🎬 REPLAY MODE")
        logger.info(f"Period: {replay_start} to {replay_end}")
        logger.info(f"Speed: {'instant' if replay_speed == 0 else f'{replay_speed}s per day'}")
        logger.info("=" * 80)

        from alphalive.replay import ReplaySimulator

        # Create replay simulator
        simulator = ReplaySimulator(
            broker=broker,
            start_date=replay_start,
            end_date=replay_end,
            tickers=[cfg.ticker for cfg in all_strategy_configs],
            speed_multiplier=replay_speed
        )

        # Run simulation
        simulator.run(
            strategy_configs=all_strategy_configs,
            signal_engines=signal_engine_map,
            risk_managers=risk_manager_map,
            order_managers=order_manager_map,
            notifier=notifier
        )

        # Exit after replay completes
        logger.info("Replay mode complete — exiting")
        sys.exit(0)

    # === LIVE MODE ===
    # Main loop — runs FOREVER
    logger.info(f"AlphaLive running 24/7. Mode: {mode}.")
    logger.info("Press Ctrl+C to stop (or wait for Railway SIGTERM)")

    while True:
        try:
            now_et = datetime.now(ET)
            current_day = now_et.strftime("%Y-%m-%d")

            # --- Check command listener thread health (B14) ---
            if cmd_listener is not None and not cmd_listener.thread.is_alive():
                logger.error("⚠️ Telegram command listener thread died")
                notifier.send_error_alert(
                    "⚠️ Command listener offline — /pause and /resume unavailable. "
                    "Restart service to restore."
                )
                # Set to None to avoid spamming alerts every loop iteration
                cmd_listener = None

            # --- New day reset ---
            if current_day != today_str:
                today_str = current_day
                morning_checks_done = set()  # Reset to empty set (B9b multi-strategy)
                last_signal_check_map = {}   # Reset signal check timestamps (B9b)
                eod_summary_sent = False
                eod_summary_retry = False

                # Reset daily for all strategies
                for ticker in risk_manager_map:
                    risk_manager_map[ticker].reset_daily()
                    order_manager_map[ticker].reset_daily()

                logger.info(f"=== New trading day: {current_day} ({now_et.strftime('%A')}) ===")

            # --- Market closed? Sleep longer ---
            if not broker.is_market_open():
                # Weekend: sleep 30 minutes
                if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
                    logger.debug("Weekend. Sleeping 30 min.")
                    time.sleep(1800)
                    continue

                # Before 9:30 AM ET: sleep until closer to open
                if now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 30):
                    logger.debug(f"Pre-market ({now_et.strftime('%H:%M %Z')}). Sleeping 5 min.")
                    time.sleep(300)
                    continue

                # After 4 PM ET: send EOD summary, then sleep until midnight
                if now_et.hour >= 16:
                    if not eod_summary_sent:
                        # Send EOD summary before sleeping
                        # Set flag first — prevents infinite retry loop if send fails
                        eod_summary_sent = True
                        try:
                            # Aggregate order history across all strategies
                            all_orders = []
                            for ticker in order_manager_map:
                                all_orders.extend(order_manager_map[ticker].get_order_history())

                            account = broker.get_account()

                            summary = {
                                "trades": len(all_orders),
                                "pnl": 0.0,  # TODO: Calculate actual P&L from order_history
                                "win_rate": 0.0,  # TODO: Calculate from closed positions
                                "start_equity": 100000.0,  # TODO: Track from morning
                                "end_equity": account.equity
                            }

                            notifier.send_daily_summary(summary)
                            logger.info("EOD summary sent.")
                        except Exception as e:
                            logger.error(f"EOD summary error: {e}", exc_info=True)

                    logger.debug(f"After hours ({now_et.strftime('%H:%M %Z')}). Sleeping 30 min.")
                    time.sleep(1800)
                    continue

                # Market holiday or other closure
                logger.debug("Market closed (holiday?). Sleeping 5 min.")
                time.sleep(300)
                continue

            # === MARKET IS OPEN ===

            # --- Signal checks (multi-strategy + timeframe-aware, B9b) ---
            # For 1Day: check after 9:35 AM ET (once per day)
            # For 1Hour/15Min: check at bar boundaries (every hour/15min)
            if now_et.hour == 9 and now_et.minute >= 35:
                for strat_cfg in all_strategy_configs:
                    # Determine if this strategy should check signals now
                    should_check = False

                    if strat_cfg.timeframe == "1Day":
                        # For daily: use morning_checks_done gate
                        should_check = strat_cfg.ticker not in morning_checks_done
                    else:
                        # For intraday (1Hour/15Min): use timeframe-aware check
                        should_check = should_run_signal_check(
                            strat_cfg.timeframe,
                            last_signal_check_map.get(strat_cfg.ticker, 0)
                        )

                    if should_check:
                        logger.info("=" * 80)
                        logger.info(f"SIGNAL CHECK: {strat_cfg.strategy.name}/{strat_cfg.ticker}")
                        logger.info("=" * 80)

                        try:
                            # DATA STALENESS CHECK (B9b): verify data is fresh on EVERY signal check
                            # This catches cases where the market data feed is delayed or broken
                            # (Alpaca API issue, network issue, etc.). Don't generate signals on stale data.
                            df = market_data.get_latest_bars(
                                ticker=strat_cfg.ticker,
                                timeframe=strat_cfg.timeframe,
                                lookback_bars=200
                            )

                            # CRITICAL: Corporate action detection (splits, special dividends)
                            # If price jumped >20% overnight without corresponding volume,
                            # it's likely a stock split or reverse split. Skip signal generation
                            # to avoid false breakout/crash signals.
                            if len(df) >= 2:
                                yesterday_close = df['close'].iloc[-2]
                                today_open = df['open'].iloc[-1]
                                pct_change = abs((today_open - yesterday_close) / yesterday_close)

                                if pct_change > 0.20:  # 20% overnight move
                                    logger.critical(
                                        f"⚠️ SPLIT DETECTED: Price jumped {pct_change*100:.1f}% overnight "
                                        f"(${yesterday_close:.2f} → ${today_open:.2f}). "
                                        f"Skipping signal check to prevent false signals."
                                    )
                                    notifier.send_alert(
                                        f"⚠️ CORPORATE ACTION DETECTED\n"
                                        f"{strat_cfg.ticker} moved {pct_change*100:.1f}% overnight.\n"
                                        f"Likely stock split/reverse split.\n"
                                        f"Signal check skipped today for safety."
                                    )
                                    morning_checks_done.add(strat_cfg.ticker)
                                    continue

                            # Generate signal
                            signal_result = signal_engine_map[strat_cfg.ticker].generate_signal(df)
                            logger.info(f"Signal: {signal_result['signal']} | Confidence: {signal_result['confidence']:.2%}")
                            logger.info(f"Reason: {signal_result['reason']}")

                            if signal_result["signal"] in ("BUY", "SELL"):
                                # Get current price
                                price = market_data.get_current_price(strat_cfg.ticker)

                                # Get account info
                                account = broker.get_account()

                                # Get position counts
                                all_positions = broker.get_all_positions()
                                strategy_positions = [p for p in all_positions if p.symbol == strat_cfg.ticker]
                                current_positions_count = len(strategy_positions)
                                total_portfolio_positions = len(all_positions)

                                # Execute signal
                                result = order_manager_map[strat_cfg.ticker].execute_signal(
                                    ticker=strat_cfg.ticker,
                                    signal=signal_result,
                                    current_price=price,
                                    account_equity=account.equity,
                                    current_positions_count=current_positions_count,
                                    total_portfolio_positions=total_portfolio_positions,
                                    current_bar=len(df)
                                )

                                if result["status"] == "success":
                                    logger.info(f"✅ Order placed: {result['order_id']}")
                                    notifier.send_trade_notification(
                                        ticker=strat_cfg.ticker,
                                        side=signal_result["signal"],
                                        qty=result["filled_qty"],
                                        price=result["filled_price"],
                                        reason=signal_result["reason"]
                                    )
                                elif result["status"] == "blocked":
                                    logger.warning(f"❌ Trade blocked: {result['reason']}")
                                else:
                                    logger.error(f"❌ Trade error: {result['reason']}")

                        except DataStaleError as e:
                            logger.warning(f"Data staleness during signal check: {e}")
                            notifier.send_error_alert(
                                f"⚠️ Data staleness on {strat_cfg.ticker}: {str(e)}"
                            )
                            # Skip this signal check — don't generate signals on stale data
                            morning_checks_done.add(strat_cfg.ticker)  # Mark as done to avoid infinite retry
                            continue

                        except Exception as e:
                            logger.error(
                                f"Signal check error [{strat_cfg.strategy.name}/{strat_cfg.ticker}]: {e}",
                                exc_info=True
                            )
                            notifier.send_error_alert(
                                f"Signal check failed: {strat_cfg.strategy.name}/{strat_cfg.ticker}"
                            )
                            morning_checks_done.add(strat_cfg.ticker)  # Mark as done
                            continue

                        # Mark as checked
                        morning_checks_done.add(strat_cfg.ticker)
                        last_signal_check_map[strat_cfg.ticker] = time.time()

            # --- Exit condition checks (every 5 minutes during market hours) ---
            if time.time() - last_exit_check >= 300:  # 5 minutes
                try:
                    positions = broker.get_all_positions()

                    if positions:
                        # Get current prices for all positions
                        current_prices = {}
                        for pos in positions:
                            try:
                                current_prices[pos.symbol] = market_data.get_current_price(pos.symbol)
                            except Exception as e:
                                logger.warning(f"Failed to get price for {pos.symbol}: {e}")
                                # Use position's current_price as fallback
                                current_prices[pos.symbol] = pos.current_price

                        # Check exit conditions for each position (multi-strategy)
                        for pos in positions:
                            ticker = pos.symbol

                            # Skip if no order manager for this ticker (shouldn't happen)
                            if ticker not in order_manager_map:
                                logger.warning(f"No order manager for {ticker}, skipping exit check")
                                continue

                            # Convert position to dict format
                            pos_dict = [{
                                "ticker": ticker,
                                "avg_entry": pos.avg_entry_price,
                                "side": pos.side,
                                "qty": pos.qty,
                                "highest_since_entry": pos.current_price  # TODO: Track actual high
                            }]

                            # Check exit conditions using the correct order manager
                            exits = order_manager_map[ticker].check_exits(pos_dict, current_prices)

                            for exit_signal in exits:
                                logger.info(f"EXIT: {exit_signal['ticker']} - {exit_signal['reason']}")

                                if app_config.dry_run:
                                    logger.info(f"[DRY RUN] Would SELL {exit_signal['ticker']}")
                                else:
                                    # Close position using the correct order manager
                                    result = order_manager_map[ticker].close_position(
                                        ticker=exit_signal['ticker'],
                                        reason=exit_signal['reason']
                                    )

                                if result["status"] == "success":
                                    # Find the position to get details
                                    pos = next((p for p in positions if p.symbol == exit_signal['ticker']), None)
                                    if pos:
                                        pnl = (exit_signal['current_price'] - pos.avg_entry_price) * pos.qty
                                        pnl_pct = ((exit_signal['current_price'] - pos.avg_entry_price) / pos.avg_entry_price) * 100

                                        notifier.send_position_closed_notification(
                                            ticker=exit_signal['ticker'],
                                            qty=pos.qty,
                                            entry_price=pos.avg_entry_price,
                                            exit_price=exit_signal['current_price'],
                                            pnl=pnl,
                                            pnl_pct=pnl_pct,
                                            reason=exit_signal['reason']
                                        )

                except Exception as e:
                    logger.error(f"Exit check error: {e}", exc_info=True)
                    notifier.send_error_alert(f"Exit check failed: {str(e)}")

                last_exit_check = time.time()

            # --- Hourly position reconciliation (every 30 minutes during market hours, B9b) ---
            # If Alpaca fills an order that AlphaLive loses track of (network blip
            # during order placement, Railway restart mid-order), the bot won't know
            # until we reconcile. Check Alpaca's positions against our internal tracking
            # every 30 minutes and sync any drift.
            # CRITICAL: If drift detected, AUTO-HALT trading to prevent compounding errors.
            if time.time() - last_position_reconciliation >= 1800:  # 30 minutes
                try:
                    alpaca_positions = broker.get_all_positions()

                    # Convert to dict format for comparison
                    alpaca_tickers = {pos.symbol: {
                        "symbol": pos.symbol,
                        "qty": pos.qty,
                        "avg_entry_price": pos.avg_entry_price,
                        "side": pos.side
                    } for pos in alpaca_positions}

                    # Get internal tracking from order manager
                    # NOTE: This requires implementing get_tracked_positions() in OrderManager
                    # For now, we can compare against what we expect from order history
                    internal_tickers = set()
                    for ticker in order_manager_map:
                        for order in order_manager_map[ticker].get_order_history():
                            if order.get("status") == "filled":
                                internal_tickers.add(order["ticker"])

                    drift_detected = False

                    # Check: Alpaca has positions we don't track
                    for ticker, alpaca_pos in alpaca_tickers.items():
                        if ticker not in internal_tickers:
                            drift_detected = True
                            logger.critical(
                                f"🚨 POSITION DRIFT: Alpaca has {ticker} ({alpaca_pos['qty']} shares) "
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

                    # Reverse check: We track positions that Alpaca doesn't have
                    for ticker in internal_tickers:
                        if ticker not in alpaca_tickers:
                            drift_detected = True
                            logger.critical(
                                f"🚨 POSITION DRIFT: Bot tracks {ticker} but Alpaca doesn't. "
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

                    # AUTO-HALT if any drift detected
                    if drift_detected:
                        logger.critical(
                            "⛔ AUTO-HALTING TRADING due to position drift. "
                            "Manual intervention required. Set TRADING_PAUSED=false to resume."
                        )
                        # Set environment variable to pause trading
                        # Note: This only affects the current process. For permanent halt,
                        # user must set TRADING_PAUSED=true in Railway dashboard.
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

                last_position_reconciliation = time.time()

            # --- End of day summary (3:55 PM ET) ---
            # EOD summary retry logic: set flag first, attempt send, if it fails,
            # set retry flag and try ONCE more on the next loop. This prevents
            # infinite retry loops while still catching transient failures.
            if not eod_summary_sent and now_et.hour == 15 and now_et.minute >= 55:
                eod_summary_sent = True  # Set flag before attempting
                try:
                    # Aggregate order history across all strategies
                    all_orders = []
                    for ticker in order_manager_map:
                        all_orders.extend(order_manager_map[ticker].get_order_history())

                    account = broker.get_account()

                    summary = {
                        "trades": len(all_orders),
                        "pnl": 0.0,  # TODO: Calculate actual P&L
                        "win_rate": 0.0,  # TODO: Calculate from closed positions
                        "start_equity": 100000.0,  # TODO: Track from morning
                        "end_equity": account.equity
                    }

                    notifier.send_daily_summary(summary)
                    logger.info("=== End of Day Summary ===")
                except Exception as e:
                    logger.error(f"EOD summary error: {e}", exc_info=True)
                    if not eod_summary_retry:
                        # First failure — queue one retry on next loop
                        eod_summary_retry = True
                        eod_summary_sent = False
                        logger.warning("EOD summary failed — will retry once on next loop")

            # Retry EOD summary once if it failed earlier
            if eod_summary_retry and not eod_summary_sent:
                eod_summary_sent = True  # Set flag to prevent further retries
                try:
                    # Aggregate order history across all strategies
                    all_orders = []
                    for ticker in order_manager_map:
                        all_orders.extend(order_manager_map[ticker].get_order_history())

                    account = broker.get_account()

                    summary = {
                        "trades": len(all_orders),
                        "pnl": 0.0,
                        "win_rate": 0.0,
                        "start_equity": 100000.0,
                        "end_equity": account.equity
                    }

                    notifier.send_daily_summary(summary)
                    logger.info("EOD summary sent (retry succeeded)")
                except Exception as e:
                    logger.error(f"EOD summary retry failed: {e}", exc_info=True)
                    # Give up after one retry — don't spam

            # Sleep 30 seconds between checks during market hours.
            # Why 30s and not 5 minutes (the exit check interval)?
            # 1. Responsive to SIGTERM — Railway sends SIGTERM on deploy,
            #    and we want to catch it within 30s, not wait 5 minutes.
            # 2. The morning check and EOD summary are time-sensitive
            #    (9:35 AM, 3:55 PM) and need ~30s precision.
            # 3. The 5-minute exit check interval is enforced by the
            #    last_exit_check guard above, not by the sleep duration.
            time.sleep(30)

        except KeyboardInterrupt:
            # Local testing: Ctrl+C
            logger.info("KeyboardInterrupt received — shutting down")
            break
        except Exception as e:
            # Catch-all: log error, notify, sleep, and continue.
            # NEVER let the loop die — Railway will restart but we lose state.
            logger.error(f"Main loop error: {e}", exc_info=True)
            try:
                notifier.send_error_alert(f"Main loop error: {str(e)}")
            except:
                pass  # Don't let notification failure crash the loop
            time.sleep(60)  # Wait a minute before retrying


if __name__ == "__main__":
    import argparse
    from alphalive.utils.logger import setup_logger

    # Setup logging
    setup_logger()

    # Parse arguments
    parser = argparse.ArgumentParser(description="AlphaLive 24/7 Trading Bot")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to strategy config JSON"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log trades without executing"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use live trading (default: paper)"
    )

    args = parser.parse_args()

    # Run main loop
    main(
        config_path=args.config,
        dry_run=args.dry_run,
        paper=not args.live
    )
