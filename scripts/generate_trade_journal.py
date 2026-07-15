#!/usr/bin/env python3
"""
Trade Journal Generator - Export trades to CSV for analysis

Fetches all trades from Alpaca and generates a comprehensive trade journal
in CSV format for analysis in Excel/Google Sheets.

Usage:
    python scripts/generate_trade_journal.py --days 30
    python scripts/generate_trade_journal.py --start 2026-03-01 --end 2026-04-25
    python scripts/generate_trade_journal.py --all
"""

import csv
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus


def check_environment():
    """Check required environment variables are set"""
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        print("Error: Missing required environment variables")
        print("")
        print("Please set:")
        print("  export ALPACA_API_KEY='your_key'")
        print("  export ALPACA_SECRET_KEY='your_secret'")
        print("")
        print("Get keys from: https://app.alpaca.markets/paper/dashboard/overview")
        sys.exit(1)


class TradeJournalGenerator:
    """Generate comprehensive trade journal from Alpaca orders"""

    def __init__(self):
        # Initialize Alpaca client
        api_key = os.getenv("ALPACA_API_KEY")
        secret_key = os.getenv("ALPACA_SECRET_KEY")
        paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"

        self.client = TradingClient(api_key, secret_key, paper=paper)
        self.account = self.client.get_account()
        self.is_paper = paper

    def fetch_orders(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        days: Optional[int] = None,
    ) -> List[Dict]:
        """Fetch orders from Alpaca with retry logic"""

        if days:
            start_date = datetime.now() - timedelta(days=days)
            end_date = datetime.now()

        request = GetOrdersRequest(
            status=QueryOrderStatus.FILLED, after=start_date, until=end_date, limit=500
        )

        # Retry logic for API calls
        max_retries = 3
        for attempt in range(max_retries):
            try:
                print("Fetching orders from Alpaca...", end="", flush=True)
                orders = self.client.get_orders(filter=request)
                print(f"Found {len(orders)} orders")

                return [
                    {
                        "id": order.id,
                        "symbol": order.symbol,
                        "side": order.side.value,
                        "qty": float(order.filled_qty),
                        "fill_price": float(order.filled_avg_price),
                        "order_type": order.order_type.value,
                        "time_in_force": order.time_in_force.value,
                        "submitted_at": order.submitted_at,
                        "filled_at": order.filled_at,
                        "status": order.status.value,
                    }
                    for order in orders
                ]

            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                wait_time = 2**attempt
                print(
                    f"\n API call failed, retrying in {wait_time}s... ({attempt+1}/{max_retries})"
                )
                time.sleep(wait_time)

    def match_trades(self, orders: List[Dict]) -> List[Dict]:
        """Match buy/sell orders into complete trades"""

        # Group orders by symbol
        by_symbol = {}
        for order in orders:
            symbol = order["symbol"]
            if symbol not in by_symbol:
                by_symbol[symbol] = []
            by_symbol[symbol].append(order)

        # Sort by filled time
        for symbol in by_symbol:
            by_symbol[symbol].sort(key=lambda x: x["filled_at"])

        # Match trades (FIFO)
        trades = []
        trade_id = 1

        for symbol, symbol_orders in by_symbol.items():
            open_positions = []

            for order in symbol_orders:
                if order["side"] == "buy":
                    open_positions.append(order)
                elif order["side"] == "sell" and open_positions:
                    # Match with oldest buy (FIFO)
                    buy_order = open_positions.pop(0)

                    entry_price = buy_order["fill_price"]
                    exit_price = order["fill_price"]
                    qty = buy_order["qty"]

                    pnl = (exit_price - entry_price) * qty
                    pnl_pct = ((exit_price / entry_price) - 1) * 100

                    hold_time = order["filled_at"] - buy_order["filled_at"]
                    hold_hours = hold_time.total_seconds() / 3600
                    hold_days = hold_time.days

                    trades.append(
                        {
                            "trade_id": trade_id,
                            "symbol": symbol,
                            "entry_date": buy_order["filled_at"].strftime("%Y-%m-%d"),
                            "entry_time": buy_order["filled_at"].strftime("%H:%M:%S"),
                            "entry_price": entry_price,
                            "exit_date": order["filled_at"].strftime("%Y-%m-%d"),
                            "exit_time": order["filled_at"].strftime("%H:%M:%S"),
                            "exit_price": exit_price,
                            "qty": qty,
                            "pnl_usd": pnl,
                            "pnl_pct": pnl_pct,
                            "hold_hours": hold_hours,
                            "hold_days": hold_days,
                            "outcome": (
                                "WIN"
                                if pnl > 0
                                else "LOSS" if pnl < 0 else "BREAK_EVEN"
                            ),
                            "buy_order_id": buy_order["id"],
                            "sell_order_id": order["id"],
                        }
                    )

                    trade_id += 1

        return trades

    def calculate_statistics(self, trades: List[Dict]) -> Dict:
        """Calculate trading statistics"""
        if not trades:
            return {}

        winners = [t for t in trades if t["outcome"] == "WIN"]
        losers = [t for t in trades if t["outcome"] == "LOSS"]

        total_pnl = sum(t["pnl_usd"] for t in trades)
        total_pnl_pct = sum(t["pnl_pct"] for t in trades)

        return {
            "total_trades": len(trades),
            "winners": len(winners),
            "losers": len(losers),
            "break_even": len([t for t in trades if t["outcome"] == "BREAK_EVEN"]),
            "win_rate": (len(winners) / len(trades)) * 100,
            "total_pnl_usd": total_pnl,
            "total_pnl_pct": total_pnl_pct,
            "avg_pnl_usd": total_pnl / len(trades),
            "avg_pnl_pct": total_pnl_pct / len(trades),
            "avg_win_pct": (
                sum(t["pnl_pct"] for t in winners) / len(winners) if winners else 0
            ),
            "avg_loss_pct": (
                sum(t["pnl_pct"] for t in losers) / len(losers) if losers else 0
            ),
            "largest_win_pct": max((t["pnl_pct"] for t in winners), default=0),
            "largest_loss_pct": min((t["pnl_pct"] for t in losers), default=0),
            "avg_hold_hours": sum(t["hold_hours"] for t in trades) / len(trades),
            "profit_factor": (
                abs(
                    sum(t["pnl_usd"] for t in winners)
                    / sum(t["pnl_usd"] for t in losers)
                )
                if losers and sum(t["pnl_usd"] for t in losers) != 0
                else 0
            ),
        }

    def export_to_csv(self, trades: List[Dict], output_file: str):
        """Export trades to CSV file"""

        if not trades:
            print("No trades to export")
            return

        # Create output directory
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)

        # Write CSV
        fieldnames = [
            "trade_id",
            "symbol",
            "entry_date",
            "entry_time",
            "entry_price",
            "exit_date",
            "exit_time",
            "exit_price",
            "qty",
            "pnl_usd",
            "pnl_pct",
            "hold_hours",
            "hold_days",
            "outcome",
            "buy_order_id",
            "sell_order_id",
        ]

        with open(output_file, "w", newline="") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(trades)

            print(f"Exported {len(trades)} trades to: {output_file}")

    def export_statistics_csv(self, stats: Dict, output_file: str):
        """Export statistics to CSV file"""

        if not stats:
            return

        with open(output_file, "w", newline="") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["Metric", "Value"])

            writer.writerow(["Total Trades", stats["total_trades"]])
            writer.writerow(["Winners", stats["winners"]])
            writer.writerow(["Losers", stats["losers"]])
            writer.writerow(["Break Even", stats["break_even"]])
            writer.writerow(["Win Rate (%)", f"{stats['win_rate']:.2f}"])
            writer.writerow(["Total P&L (USD)", f"${stats['total_pnl_usd']:.2f}"])
            writer.writerow(["Total P&L (%)", f"{stats['total_pnl_pct']:.2f}"])
            writer.writerow(["Avg P&L (USD)", f"${stats['avg_pnl_usd']:.2f}"])
            writer.writerow(["Avg P&L (%)", f"{stats['avg_pnl_pct']:.2f}"])
            writer.writerow(["Avg Win (%)", f"{stats['avg_win_pct']:.2f}"])
            writer.writerow(["Avg Loss (%)", f"{stats['avg_loss_pct']:.2f}"])
            writer.writerow(["Largest Win (%)", f"{stats['largest_win_pct']:.2f}"])
            writer.writerow(["Largest Loss (%)", f"{stats['largest_loss_pct']:.2f}"])
            writer.writerow(["Avg Hold Time (hours)", f"{stats['avg_hold_hours']:.2f}"])
            writer.writerow(["Profit Factor", f"{stats['profit_factor']:.2f}"])

            print(f"Exported statistics to: {output_file}")

    def print_summary(self, trades: List[Dict], stats: Dict):
        """Print summary to console"""

        print("\n" + "=" * 80)
        print("TRADE JOURNAL SUMMARY")
        print("=" * 80)
        print(f"Account Type: {'PAPER' if self.is_paper else 'LIVE'}")
        print(f"Total Trades: {len(trades)}")

        if not stats:
            print("No completed trades found")
            return

        print()
        print("-" * 80)
        print("PERFORMANCE STATISTICS")
        print("-" * 80)
        print(f"Win Rate: {stats['win_rate']:.1f}%")
        print(
            f"Winners: {stats['winners']} | Losers: {stats['losers']} | Break Even: {stats['break_even']}"
        )
        print(
            f"Total P&L: ${stats['total_pnl_usd']:.2f} ({stats['total_pnl_pct']:.2f}%)"
        )
        print(
            f"Avg P&L per Trade: ${stats['avg_pnl_usd']:.2f} ({stats['avg_pnl_pct']:.2f}%)"
        )
        print(
            f"Avg Win: {stats['avg_win_pct']:.2f}% | Avg Loss: {stats['avg_loss_pct']:.2f}%"
        )
        print(
            f"Largest Win: {stats['largest_win_pct']:.2f}% | Largest Loss: {stats['largest_loss_pct']:.2f}%"
        )
        print(
            f"Avg Hold Time: {stats['avg_hold_hours']:.1f} hours ({stats['avg_hold_hours']/24:.1f} days)"
        )
        print(f"Profit Factor: {stats['profit_factor']:.2f}")
        print()

        # Recent trades
        print("-" * 80)
        print(f"RECENT TRADES (Last {min(10, len(trades))})")
        print("-" * 80)

        for trade in trades[-10:]:
            outcome_icon = (
                ""
                if trade["outcome"] == "WIN"
                else "" if trade["outcome"] == "LOSS" else ""
            )
            print(
                f"#{trade['trade_id']:3d} | {trade['entry_date']} | {trade['symbol']:6s} | "
                f"${trade['entry_price']:.2f} → ${trade['exit_price']:.2f} | "
                f"{trade['pnl_pct']:+6.2f}% | {outcome_icon} {trade['outcome']}"
            )

        print("=" * 80)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate trade journal from Alpaca orders"
    )
    parser.add_argument("--days", type=int, help="Number of days to fetch (e.g., 30)")
    parser.add_argument("--start", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", help="End date (YYYY-MM-DD)")
    parser.add_argument("--all", action="store_true", help="Fetch all trades")
    parser.add_argument("--output", default="trade_journal.csv", help="Output CSV file")
    parser.add_argument("--version", action="version", version="AlphaLive Tools v1.0.0")

    args = parser.parse_args()

    # Check environment first
    check_environment()

    try:
        generator = TradeJournalGenerator()

        # Parse dates
        start_date = None
        end_date = None

        if args.start:
            start_date = datetime.strptime(args.start, "%Y-%m-%d")
        if args.end:
            end_date = datetime.strptime(args.end, "%Y-%m-%d")

        # Fetch orders (progress indicator inside fetch_orders now)
        if args.all:
            orders = generator.fetch_orders()
        elif args.days:
            orders = generator.fetch_orders(days=args.days)
        elif start_date or end_date:
            orders = generator.fetch_orders(start_date=start_date, end_date=end_date)
        else:
            # Default to last 30 days
            orders = generator.fetch_orders(days=30)

        # Match trades
        print("Matching buy/sell orders...", end="", flush=True)
        trades = generator.match_trades(orders)
        print(f"Matched {len(trades)} complete trades")

        # Calculate statistics
        stats = generator.calculate_statistics(trades)

        # Export
        generator.export_to_csv(trades, args.output)

        # Export statistics
        stats_file = args.output.replace(".csv", "_stats.csv")
        generator.export_statistics_csv(stats, stats_file)

        # Print summary
        generator.print_summary(trades, stats)

    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
