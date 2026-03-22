"""
Replay Simulator for Testing Strategies with Historical Data

Simulates trading by stepping through historical data day-by-day.
Uses Alpaca's FREE historical data (no subscription needed).

Usage:
    simulator = ReplaySimulator(
        broker=broker,
        start_date="2015-01-01",
        end_date="2019-12-31",
        tickers=["AAPL"],
        speed_multiplier=0  # 0=instant
    )

    simulator.run(
        strategy_configs=all_strategy_configs,
        signal_engines=signal_engine_map,
        risk_managers=risk_manager_map,
        order_managers=order_manager_map,
        notifier=notifier
    )
"""

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List

import pandas as pd

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


class ReplaySimulator:
    """
    Simulates trading by stepping through historical data day-by-day.

    How it works:
    1. Fetches historical data once (from Alpaca free tier)
    2. Iterates through trading days
    3. For each day:
       - Sets "current time" to that day's 9:35 AM
       - Provides historical bars up to that point
       - Runs signal check
       - Executes trades (dry run)
       - Checks exits
       - Sends EOD summary
    4. Reports final results
    """

    def __init__(
        self,
        broker,
        start_date: str,
        end_date: str,
        tickers: List[str],
        speed_multiplier: int = 0  # 0=instant, 1=1sec per day
    ):
        """
        Initialize ReplaySimulator.

        Args:
            broker: Broker instance (AlpacaBroker)
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            tickers: List of tickers to simulate
            speed_multiplier: Speed (0=instant, 1=1sec/day, etc.)
        """
        self.broker = broker
        self.start_date = pd.Timestamp(start_date, tz=ET)
        self.end_date = pd.Timestamp(end_date, tz=ET)
        self.tickers = tickers
        self.speed_multiplier = speed_multiplier

        # Will store all historical data
        self.historical_data = {}  # {ticker: DataFrame}

        # Trading days
        self.trading_days = []

        # Results tracking
        self.results = {
            "trades": [],
            "daily_pnl": {},
            "total_pnl": 0.0,
            "total_trades": 0,
            "wins": 0,
            "losses": 0
        }

        # Position tracking
        self.positions = {}  # {ticker: {"qty": int, "entry_price": float, "side": str}}

        logger.info(f"ReplaySimulator initialized for {start_date} to {end_date}")

    def _load_historical_data(self):
        """
        Load all historical data once at the start.
        Uses Alpaca's FREE historical data API (no subscription needed).
        """
        logger.info("=" * 80)
        logger.info("Loading historical data from Alpaca (free tier)...")
        logger.info("=" * 80)

        for ticker in self.tickers:
            logger.info(f"Fetching {ticker}...")

            # Use the new get_historical_bars method
            df = self.broker.get_historical_bars(
                symbol=ticker,
                timeframe="1Day",
                start=self.start_date,
                end=self.end_date
            )

            if df.empty:
                raise ValueError(f"No historical data for {ticker}")

            # Store for this ticker
            self.historical_data[ticker] = df

            logger.info(f"  ✓ Loaded {len(df)} bars for {ticker}")

        # Extract trading days (days when market was open)
        first_ticker = self.tickers[0]
        self.trading_days = self.historical_data[first_ticker].index.tolist()

        logger.info("=" * 80)
        logger.info(f"✓ Loaded {len(self.trading_days)} trading days")
        logger.info(f"  First: {self.trading_days[0].strftime('%Y-%m-%d')}")
        logger.info(f"  Last: {self.trading_days[-1].strftime('%Y-%m-%d')}")
        logger.info("=" * 80)

    def _get_bars_up_to_date(
        self, ticker: str, current_date: pd.Timestamp, lookback_bars: int = 200
    ) -> pd.DataFrame:
        """
        Get historical bars up to (but not including) the current date.
        This simulates what the bot would see on that trading day.
        """
        all_bars = self.historical_data[ticker]

        # Get bars before current date
        mask = all_bars.index < current_date
        available_bars = all_bars[mask]

        # Return last N bars
        return available_bars.tail(lookback_bars)

    def _simulate_trading_day(
        self,
        current_date: pd.Timestamp,
        strategy_configs,
        signal_engines: Dict,
        risk_managers: Dict,
        order_managers: Dict,
        notifier
    ):
        """
        Simulate one trading day.

        Flow:
        1. Set current time to 9:35 AM on this date
        2. Generate signal (using bars up to yesterday)
        3. Execute trade if signal is BUY/SELL
        4. Check exits for existing positions
        5. Track P&L
        """
        date_str = current_date.strftime("%Y-%m-%d")
        day_name = current_date.strftime("%A")

        logger.info("")
        logger.info("=" * 80)
        logger.info(f"📅 {date_str} ({day_name})")
        logger.info("=" * 80)

        # For each strategy
        for config in strategy_configs:
            ticker = config.ticker

            # Get historical bars UP TO this date (not including today)
            bars = self._get_bars_up_to_date(ticker, current_date, lookback_bars=250)

            if len(bars) < 50:
                logger.warning(f"  ⚠️  Insufficient data for {ticker} (need warmup period)")
                continue

            # Generate signal
            signal_engine = signal_engines[ticker]
            signal = signal_engine.generate_signal(bars)

            if not signal.get("warmup_complete", True):
                logger.warning(f"  ⚠️  Warmup not complete for {ticker}")
                continue

            logger.info(f"  {ticker} | Signal: {signal['signal']} | Confidence: {signal['confidence']:.2%}")
            logger.info(f"  Reason: {signal['reason']}")

            # Get "current" price (today's open)
            current_price = self.historical_data[ticker].loc[current_date]['open']

            # Check exits first (if we have a position)
            if ticker in self.positions:
                self._check_exit(
                    ticker=ticker,
                    current_date=current_date,
                    current_price=current_price,
                    risk_manager=risk_managers[ticker],
                    notifier=notifier
                )

            # Execute entry signal if BUY and no position
            if signal["signal"] == "BUY" and ticker not in self.positions:
                self._execute_entry(
                    ticker=ticker,
                    signal=signal,
                    current_date=current_date,
                    current_price=current_price,
                    config=config,
                    risk_manager=risk_managers[ticker],
                    notifier=notifier
                )

            # Execute exit signal if SELL and we have a position
            elif signal["signal"] == "SELL" and ticker in self.positions:
                position = self.positions[ticker]
                self._close_position(
                    ticker=ticker,
                    current_date=current_date,
                    exit_price=current_price,
                    qty=position["qty"],
                    entry_price=position["entry_price"],
                    reason="Strategy SELL signal",
                    notifier=notifier
                )

        # Sleep if speed multiplier set
        if self.speed_multiplier > 0:
            time.sleep(self.speed_multiplier)

    def _execute_entry(
        self,
        ticker: str,
        signal: dict,
        current_date: pd.Timestamp,
        current_price: float,
        config,
        risk_manager,
        notifier
    ):
        """Execute entry trade (BUY/SELL)."""

        # Calculate position size
        account_equity = 100000.0  # Simulated starting equity
        shares = risk_manager.calculate_position_size(
            ticker, signal["signal"], current_price, account_equity
        )

        if shares == 0:
            logger.info(f"  ✗ Position size = 0 (blocked)")
            return

        # Check risk limits
        can_trade, reason = risk_manager.can_trade(
            ticker=ticker,
            signal=signal["signal"],
            account_equity=account_equity,
            current_positions_count=len(self.positions),
            total_portfolio_positions=len(self.positions)
        )

        if not can_trade:
            logger.info(f"  ✗ Trade blocked: {reason}")
            return

        # Execute (simulated)
        side = signal["signal"]
        cost = shares * current_price

        logger.info(f"  ✓ [REPLAY] {side} {shares} {ticker} @ ${current_price:.2f} (${cost:,.2f})")

        # Track position
        self.positions[ticker] = {
            "qty": shares,
            "entry_price": current_price,
            "entry_date": current_date,
            "side": side
        }

        # Record trade
        self.results["trades"].append({
            "date": current_date.strftime("%Y-%m-%d"),
            "ticker": ticker,
            "action": "ENTRY",
            "side": side,
            "qty": shares,
            "price": current_price,
            "cost": cost
        })

        self.results["total_trades"] += 1

        # Send notification
        notifier.send_message(
            f"🎬 <b>REPLAY: {side} Signal</b>\n\n"
            f"<b>Date:</b> {current_date.strftime('%Y-%m-%d')}\n"
            f"<b>Ticker:</b> {ticker}\n"
            f"<b>Qty:</b> {shares}\n"
            f"<b>Price:</b> ${current_price:.2f}\n"
            f"<b>Cost:</b> ${cost:,.2f}\n"
            f"<b>Reason:</b> {signal['reason']}"
        )

    def _check_exit(
        self,
        ticker: str,
        current_date: pd.Timestamp,
        current_price: float,
        risk_manager,
        notifier
    ):
        """Check if we should exit the position."""

        position = self.positions[ticker]
        entry_price = position["entry_price"]
        qty = position["qty"]
        side = position["side"]

        # Get price high since entry (for trailing stop)
        entry_date = position["entry_date"]
        bars_since_entry = self.historical_data[ticker].loc[entry_date:current_date]
        highest_since_entry = bars_since_entry['high'].max()

        # Check stop loss
        if risk_manager.check_stop_loss(entry_price, current_price, side.lower()):
            self._close_position(
                ticker, current_date, current_price, qty, entry_price, "Stop Loss", notifier
            )
            return

        # Check take profit
        if risk_manager.check_take_profit(entry_price, current_price, side.lower()):
            self._close_position(
                ticker, current_date, current_price, qty, entry_price, "Take Profit", notifier
            )
            return

        # Check trailing stop
        if risk_manager.check_trailing_stop(
            entry_price, highest_since_entry, current_price, side.lower()
        ):
            self._close_position(
                ticker, current_date, current_price, qty, entry_price, "Trailing Stop", notifier
            )
            return

    def _close_position(
        self,
        ticker: str,
        current_date: pd.Timestamp,
        exit_price: float,
        qty: int,
        entry_price: float,
        reason: str,
        notifier
    ):
        """Close a position and record P&L."""

        # Calculate P&L
        pnl = (exit_price - entry_price) * qty
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100

        logger.info(
            f"  💰 EXIT: {ticker} @ ${exit_price:.2f} | "
            f"P&L: ${pnl:.2f} ({pnl_pct:+.2f}%) | Reason: {reason}"
        )

        # Track results
        self.results["total_pnl"] += pnl
        if pnl > 0:
            self.results["wins"] += 1
        else:
            self.results["losses"] += 1

        # Record trade
        self.results["trades"].append({
            "date": current_date.strftime("%Y-%m-%d"),
            "ticker": ticker,
            "action": "EXIT",
            "side": "SELL",
            "qty": qty,
            "price": exit_price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "reason": reason
        })

        # Remove position
        del self.positions[ticker]

        # Send notification
        notifier.send_message(
            f"💰 <b>REPLAY: Position Closed</b>\n\n"
            f"<b>Date:</b> {current_date.strftime('%Y-%m-%d')}\n"
            f"<b>Ticker:</b> {ticker}\n"
            f"<b>Qty:</b> {qty}\n"
            f"<b>Entry:</b> ${entry_price:.2f}\n"
            f"<b>Exit:</b> ${exit_price:.2f}\n"
            f"<b>P&L:</b> ${pnl:.2f} ({pnl_pct:+.2f}%)\n"
            f"<b>Reason:</b> {reason}"
        )

    def run(
        self,
        strategy_configs,
        signal_engines: Dict,
        risk_managers: Dict,
        order_managers: Dict,
        notifier
    ):
        """
        Main replay loop.

        Steps through each trading day and simulates trading.
        """
        # Load all historical data first
        self._load_historical_data()

        # Send startup notification
        logger.info("")
        logger.info("🎬 Starting replay simulation...")
        notifier.send_message(
            f"🎬 <b>Replay Mode Started</b>\n\n"
            f"<b>Period:</b> {self.start_date.strftime('%Y-%m-%d')} to {self.end_date.strftime('%Y-%m-%d')}\n"
            f"<b>Trading Days:</b> {len(self.trading_days)}\n"
            f"<b>Strategies:</b> {', '.join(self.tickers)}\n\n"
            f"Simulating trading day-by-day..."
        )

        # Iterate through each trading day
        for i, trading_day in enumerate(self.trading_days, 1):
            if i % 50 == 0:  # Progress update every 50 days
                logger.info(f"\n[{i}/{len(self.trading_days)}] Progress: {(i/len(self.trading_days)*100):.1f}%")

            self._simulate_trading_day(
                current_date=trading_day,
                strategy_configs=strategy_configs,
                signal_engines=signal_engines,
                risk_managers=risk_managers,
                order_managers=order_managers,
                notifier=notifier
            )

        # Final summary
        self._send_final_summary(notifier)

    def _send_final_summary(self, notifier):
        """Send final results summary."""

        total_trades = self.results["total_trades"]
        total_pnl = self.results["total_pnl"]
        wins = self.results["wins"]
        losses = self.results["losses"]

        if total_trades > 0:
            win_rate = (wins / total_trades) * 100
        else:
            win_rate = 0.0

        logger.info("")
        logger.info("=" * 80)
        logger.info("🏁 REPLAY COMPLETE")
        logger.info("=" * 80)
        logger.info(f"Period: {self.start_date.strftime('%Y-%m-%d')} to {self.end_date.strftime('%Y-%m-%d')}")
        logger.info(f"Trading Days: {len(self.trading_days)}")
        logger.info(f"Total Trades: {total_trades}")
        logger.info(f"Wins: {wins} | Losses: {losses}")
        logger.info(f"Win Rate: {win_rate:.1f}%")
        logger.info(f"Total P&L: ${total_pnl:,.2f}")
        logger.info("=" * 80)

        summary_text = (
            f"🏁 <b>Replay Complete</b>\n\n"
            f"<b>Period:</b> {self.start_date.strftime('%Y-%m-%d')} to {self.end_date.strftime('%Y-%m-%d')}\n"
            f"<b>Trading Days:</b> {len(self.trading_days)}\n"
            f"<b>Total Trades:</b> {total_trades}\n"
            f"<b>Wins:</b> {wins} | <b>Losses:</b> {losses}\n"
            f"<b>Win Rate:</b> {win_rate:.1f}%\n"
            f"<b>Total P&L:</b> ${total_pnl:,.2f}\n\n"
        )

        if len(self.results["trades"]) > 0:
            summary_text += "<b>Sample Trades:</b>\n"
            for trade in self.results["trades"][:5]:
                if trade["action"] == "ENTRY":
                    summary_text += f"  • {trade['date']}: {trade['side']} {trade['qty']} {trade['ticker']} @ ${trade['price']:.2f}\n"
                else:
                    summary_text += f"  • {trade['date']}: EXIT {trade['ticker']} - P&L ${trade['pnl']:.2f} ({trade['reason']})\n"

            if total_trades > 5:
                summary_text += f"  ... and {total_trades - 5} more\n"

        notifier.send_message(summary_text)
