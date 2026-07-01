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
from datetime import datetime
from zoneinfo import ZoneInfo

from alphalive.config import load_config_path, load_env, validate_all
from alphalive.services.alphasignal_client import (
    AlphaSignalClient,
    run_pre_execution_checks,
)
from alphalive.services.deeplob_client import DeepLOBClient
from alphalive.state import BotState
from alphalive.broker.alpaca_broker import AlpacaBroker
from alphalive.data.market_data import MarketDataFetcher, DataStaleError
from alphalive.strategy.signal_engine import SignalEngine
from alphalive.execution.risk_manager import RiskManager
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
    source — it captures unrealised moves on positions held overnight and is
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
    pnl = end_equity - start_equity

    # Win-rate: FIFO match BUYs to SELLs per ticker
    buy_queues: dict[str, list] = {}
    wins = 0
    total_sells = 0

    for order in sorted(all_orders, key=lambda o: o["timestamp"]):
        ticker = order["ticker"]
        side = order.get("side", "").upper()
        if side == "BUY":
            buy_queues.setdefault(ticker, []).append(order["price"])
        elif side == "SELL":
            queue = buy_queues.get(ticker, [])
            if queue:
                buy_price = queue.pop(0)
                total_sells += 1
                if order["price"] > buy_price:
                    wins += 1

    win_rate = (wins / total_sells * 100) if total_sells > 0 else 0.0
    return {"pnl": pnl, "win_rate": win_rate}


def _send_eod_summary(
    order_manager_map: dict, broker, morning_equity: float, notifier
) -> None:
    """Aggregate today's trades across all strategies and push the daily summary."""
    all_orders = []
    for ticker in order_manager_map:
        all_orders.extend(order_manager_map[ticker].get_order_history())
    account = broker.get_account()
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
            test_signal = signal_engine_map[ticker].generate_signal(df)
            warmup_complete = test_signal.get("warmup_complete", True)

            if not warmup_complete:
                logger.warning(
                    f"  Indicator warmup incomplete for {ticker} — signals may be unreliable"
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


def _check_signal_for_strategy(
    strat_cfg,
    now_et: datetime,
    morning_checks_done: set,
    last_signal_check_map: dict,
    market_data,
    signal_engine_map: dict,
    order_manager_map: dict,
    deeplob_client,
    alphasignal_client,
    broker,
    notifier,
) -> None:
    """Run the signal check for one strategy. Mutates morning_checks_done and last_signal_check_map."""
    if strat_cfg.timeframe in ("1Day", "1Week"):
        if not (now_et.hour == 9 and now_et.minute >= 35):
            return
        if strat_cfg.ticker in morning_checks_done:
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
                morning_checks_done.add(strat_cfg.ticker)
                return

        signal_result = signal_engine_map[strat_cfg.ticker].generate_signal(df)
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
            _signal_direction = 2 if signal_result["signal"] == "BUY" else 0
            _lob_allowed = True
            _lob_pred: dict = {}
            _sentiment_allowed = True
            _sentiment_pred: dict = {}

            if deeplob_client is not None or alphasignal_client is not None:
                # Run DeepLOB and AlphaSignal concurrently via asyncio.gather.
                # lob_snapshot=None: Alpaca free tier has no L2 feed;
                # DeepLOBClient.is_execution_allowed fails open for None.
                (
                    _lob_allowed,
                    _lob_pred,
                    _sentiment_allowed,
                    _sentiment_pred,
                ) = asyncio.run(
                    run_pre_execution_checks(
                        deeplob_client=deeplob_client,
                        alphasignal_client=alphasignal_client,
                        lob_snapshot=None,
                        ticker=strat_cfg.ticker,
                        signal_direction=_signal_direction,
                    )
                )

            if not _lob_allowed or not _sentiment_allowed:
                logger.info(
                    "Execution blocked — "
                    f"lob_allowed={_lob_allowed}, "
                    f"sentiment_allowed={_sentiment_allowed}, "
                    f"sentiment_score={_sentiment_pred.get('sentiment_score', 'N/A')}, "
                    f"lob_prediction={_lob_pred}"
                )
            else:
                price = market_data.get_current_price(strat_cfg.ticker)
                account = broker.get_account()
                all_positions = broker.get_all_positions()
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
                    current_bar=len(df),
                )

                if result["status"] == "success":
                    logger.info(f"Order placed: {result['order_id']}")
                    logger.info(
                        f"Trade executed | {signal_result['signal']} "
                        f"{result['filled_qty']} {strat_cfg.ticker} "
                        f"@ ${result['filled_price']:.2f} | "
                        f"Total: ${result['filled_qty'] * result['filled_price']:.2f}"
                    )
                    notifier.send_trade_notification(
                        ticker=strat_cfg.ticker,
                        side=signal_result["signal"],
                        qty=result["filled_qty"],
                        price=result["filled_price"],
                        reason=signal_result["reason"],
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
        logger.warning(f"Data staleness during signal check: {e}")
        notifier.send_error_alert(f"⚠️ Data staleness on {strat_cfg.ticker}: {str(e)}")
        morning_checks_done.add(strat_cfg.ticker)
        return

    except Exception as e:
        logger.error(
            f"Signal check error [{strat_cfg.strategy.name}/{strat_cfg.ticker}]: {e}",
            exc_info=True,
        )
        notifier.send_error_alert(
            f"Signal check failed: {strat_cfg.strategy.name}/{strat_cfg.ticker}"
        )
        morning_checks_done.add(strat_cfg.ticker)
        return

    morning_checks_done.add(strat_cfg.ticker)
    last_signal_check_map[strat_cfg.ticker] = time.time()


def _run_exit_checks(
    broker,
    market_data,
    order_manager_map: dict,
    bot_state,
    app_config,
    notifier,
) -> None:
    """Check stop loss, take profit, and trailing stop for all open positions."""
    try:
        positions = broker.get_all_positions()

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
                        bot_state.clear_position_high(ticker)

                        closed_pos = next(
                            (p for p in positions if p.symbol == exit_signal["ticker"]),
                            None,
                        )
                        if closed_pos:
                            pnl = (
                                exit_signal["current_price"]
                                - closed_pos.avg_entry_price
                            ) * closed_pos.qty
                            pnl_pct = (
                                (
                                    exit_signal["current_price"]
                                    - closed_pos.avg_entry_price
                                )
                                / closed_pos.avg_entry_price
                            ) * 100
                            notifier.send_position_closed_notification(
                                ticker=exit_signal["ticker"],
                                qty=closed_pos.qty,
                                entry_price=closed_pos.avg_entry_price,
                                exit_price=exit_signal["current_price"],
                                pnl=pnl,
                                pnl_pct=pnl_pct,
                                reason=exit_signal["reason"],
                            )

    except Exception as e:
        logger.error(f"Exit check error: {e}", exc_info=True)
        notifier.send_error_alert(f"Exit check failed: {str(e)}")


def _run_position_reconciliation(
    broker,
    order_manager_map: dict,
    app_config,
    notifier,
) -> None:
    """Compare Alpaca positions against internal order history; auto-halt on drift."""
    try:
        alpaca_positions = broker.get_all_positions()

        alpaca_tickers = {
            pos.symbol: {
                "symbol": pos.symbol,
                "qty": pos.qty,
                "avg_entry_price": pos.avg_entry_price,
                "side": pos.side,
            }
            for pos in alpaca_positions
        }

        internal_tickers: set = set()
        for ticker in order_manager_map:
            for order in order_manager_map[ticker].get_order_history():
                if order.get("status") == "filled":
                    internal_tickers.add(order["ticker"])

        drift_detected = False

        for ticker, alpaca_pos in alpaca_tickers.items():
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

        for ticker in internal_tickers:
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

    # State file — used for dashboard kill switch and trailing stop persistence
    bot_state = BotState(app_config.state_file)

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
        )
        logger.info(
            f"AlphaSignal sentiment filter enabled | "
            f"URL: {app_config.alphasignal.url} | "
            f"Threshold: {app_config.alphasignal.sentiment_threshold}"
        )
    else:
        logger.info("AlphaSignal sentiment filter disabled (ALPHASIGNAL_ENABLED=false)")

    # DeepLOB LOB-prediction client (Integration D → AL)
    deeplob_client: DeepLOBClient | None = None
    if app_config.deeplob.enabled:
        deeplob_client = DeepLOBClient(
            base_url=app_config.deeplob.url,
            confidence_threshold=app_config.deeplob.confidence_threshold,
            timeout_seconds=app_config.deeplob.timeout_seconds,
        )
        logger.info(
            f"DeepLOB LOB-prediction filter enabled | "
            f"URL: {app_config.deeplob.url} | "
            f"Confidence threshold: {app_config.deeplob.confidence_threshold}"
        )
    else:
        logger.info("DeepLOB LOB-prediction filter disabled (DEEPLOB_ENABLED=false)")

    # Multi-strategy support: Create maps for signal engines, risk managers, and order managers
    # For simplicity in this implementation, each strategy has its own risk manager
    # (portfolio-level limits are checked by summing across all strategies)
    signal_engine_map = {}
    risk_manager_map = {}
    order_manager_map = {}

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
        )

        # Create order manager for this strategy
        order_manager_map[ticker] = OrderManager(
            broker=broker,
            risk_manager=risk_manager_map[ticker],
            config=strategy_config,
            notifier=notifier,
            dry_run=app_config.dry_run,
        )

        logger.info(f"  Initialized components for {strategy_name} ({ticker})")

    # 5. Initialize Telegram command listener
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
            config=first_strategy,
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
            order_managers=order_manager_map,
            notifier=notifier,
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

            # --- Check command listener thread health ---
            if cmd_listener is not None and not cmd_listener.thread.is_alive():
                logger.error("Telegram command listener thread died")
                notifier.send_error_alert(
                    "⚠️ Command listener offline — /pause and /resume unavailable. "
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
                morning_equity = 0.0
                drawdown_alert_sent = False

                # Reset daily for all strategies
                for ticker in risk_manager_map:
                    risk_manager_map[ticker].reset_daily()
                    order_manager_map[ticker].reset_daily()

                logger.info(
                    f"=== New trading day: {current_day} ({now_et.strftime('%A')}) ==="
                )

            # --- Dashboard kill switch (checked every loop iteration) ---
            if bot_state.check_dashboard_paused():
                logger.info("Trading paused via dashboard kill switch — sleeping 30s")
                time.sleep(30)
                continue

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
                        # Set flag first — prevents infinite retry loop if send fails
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

            # --- Intraday drawdown monitoring ---
            try:
                account = broker.get_account()
                current_equity = account.equity

                # Capture morning equity once per day (first time market is open)
                if morning_equity == 0.0 and current_equity > 0:
                    morning_equity = current_equity
                    peak_equity_today = current_equity
                    logger.info(f"Morning equity captured: ${morning_equity:,.2f}")

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
                    deeplob_client,
                    alphasignal_client,
                    broker,
                    notifier,
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
                )
                last_exit_check = time.time()

            # --- Position reconciliation (every 30 minutes during market hours) ---
            # Auto-halts trading if Alpaca positions diverge from internal order history.
            if time.time() - last_position_reconciliation >= 1800:  # 30 minutes
                _run_position_reconciliation(
                    broker, order_manager_map, app_config, notifier
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
                        # First failure — queue one retry on next loop
                        eod_summary_retry = True
                        eod_summary_sent = False
                        logger.warning(
                            "EOD summary failed — will retry once on next loop"
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
