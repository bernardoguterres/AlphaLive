#!/usr/bin/env python3
"""
Automated Weekly Report Generator

Generates comprehensive weekly performance report and optionally sends via email.
Run this as a cron job every Sunday night to get automated weekly summaries.

Usage:
    python scripts/automated_weekly_report.py
    python scripts/automated_weekly_report.py --email your@email.com
    python scripts/automated_weekly_report.py --telegram
    python scripts/automated_weekly_report.py --save-only
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus


def check_environment():
    """Check required environment variables are set"""
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        print("❌ Error: Missing required environment variables")
        print("")
        print("Please set:")
        print("  export ALPACA_API_KEY='your_key'")
        print("  export ALPACA_SECRET_KEY='your_secret'")
        print("")
        print("Get keys from: https://app.alpaca.markets/paper/dashboard/overview")
        sys.exit(1)


class WeeklyReportGenerator:
    """Generate comprehensive weekly trading report"""

    def __init__(self):
        # Initialize Alpaca client
        api_key = os.getenv("ALPACA_API_KEY")
        secret_key = os.getenv("ALPACA_SECRET_KEY")
        paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"

        self.client = TradingClient(api_key, secret_key, paper=paper)
        self.account = self.client.get_account()
        self.is_paper = paper

        # Telegram config
        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")

    def fetch_week_trades(self) -> List[Dict]:
        """Fetch trades from past week"""
        start_date = datetime.now() - timedelta(days=7)

        request = GetOrdersRequest(
            status=QueryOrderStatus.FILLED,
            after=start_date,
            limit=500
        )

        orders = self.client.get_orders(filter=request)

        return [
            {
                'symbol': order.symbol,
                'side': order.side.value,
                'qty': float(order.filled_qty),
                'fill_price': float(order.filled_avg_price),
                'filled_at': order.filled_at,
            }
            for order in orders
        ]

    def match_trades(self, orders: List[Dict]) -> List[Dict]:
        """Match buy/sell orders into complete trades"""
        by_symbol = {}
        for order in orders:
            symbol = order['symbol']
            if symbol not in by_symbol:
                by_symbol[symbol] = []
            by_symbol[symbol].append(order)

        for symbol in by_symbol:
            by_symbol[symbol].sort(key=lambda x: x['filled_at'])

        trades = []
        for symbol, symbol_orders in by_symbol.items():
            open_positions = []

            for order in symbol_orders:
                if order['side'] == 'buy':
                    open_positions.append(order)
                elif order['side'] == 'sell' and open_positions:
                    buy_order = open_positions.pop(0)

                    pnl = (order['fill_price'] - buy_order['fill_price']) * buy_order['qty']
                    pnl_pct = ((order['fill_price'] / buy_order['fill_price']) - 1) * 100

                    trades.append({
                        'symbol': symbol,
                        'entry_price': buy_order['fill_price'],
                        'exit_price': order['fill_price'],
                        'qty': buy_order['qty'],
                        'pnl': pnl,
                        'pnl_pct': pnl_pct,
                        'outcome': 'WIN' if pnl > 0 else 'LOSS',
                        'entry_time': buy_order['filled_at'],
                        'exit_time': order['filled_at'],
                    })

        return trades

    def calculate_stats(self, trades: List[Dict]) -> Dict:
        """Calculate weekly statistics"""
        if not trades:
            return {
                'total_trades': 0,
                'win_rate': 0,
                'total_pnl': 0,
                'avg_win': 0,
                'avg_loss': 0,
                'winners': 0,
                'losers': 0,
            }

        winners = [t for t in trades if t['outcome'] == 'WIN']
        losers = [t for t in trades if t['outcome'] == 'LOSS']

        return {
            'total_trades': len(trades),
            'winners': len(winners),
            'losers': len(losers),
            'win_rate': (len(winners) / len(trades)) * 100,
            'total_pnl': sum(t['pnl'] for t in trades),
            'total_pnl_pct': sum(t['pnl_pct'] for t in trades),
            'avg_win': sum(t['pnl_pct'] for t in winners) / len(winners) if winners else 0,
            'avg_loss': sum(t['pnl_pct'] for t in losers) / len(losers) if losers else 0,
            'largest_win': max((t['pnl_pct'] for t in winners), default=0),
            'largest_loss': min((t['pnl_pct'] for t in losers), default=0),
        }

    def generate_report(self, trades: List[Dict], stats: Dict) -> str:
        """Generate weekly report text"""
        week_start = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        week_end = datetime.now().strftime('%Y-%m-%d')

        lines = []
        lines.append("=" * 80)
        lines.append(f"📊 WEEKLY TRADING REPORT")
        lines.append("=" * 80)
        lines.append(f"Period: {week_start} to {week_end}")
        lines.append(f"Account: {'PAPER' if self.is_paper else 'LIVE'}")
        lines.append(f"Current Equity: ${float(self.account.equity):,.2f}")
        lines.append(f"Buying Power: ${float(self.account.buying_power):,.2f}")
        lines.append("")

        if stats['total_trades'] == 0:
            lines.append("⚠️  No trades executed this week")
            lines.append("")
            lines.append("Possible reasons:")
            lines.append("  - Market conditions didn't meet strategy criteria")
            lines.append("  - Trading paused manually")
            lines.append("  - Max trades per day limit reached")
            lines.append("")
            lines.append("Action: Review strategy parameters if this persists")
        else:
            lines.append("-" * 80)
            lines.append("PERFORMANCE SUMMARY")
            lines.append("-" * 80)
            lines.append(f"Total Trades: {stats['total_trades']}")
            lines.append(f"Winners: {stats['winners']} | Losers: {stats['losers']}")
            lines.append(f"Win Rate: {stats['win_rate']:.1f}%")
            lines.append(f"Total P&L: ${stats['total_pnl']:.2f} ({stats['total_pnl_pct']:.2f}%)")
            lines.append(f"Avg Win: {stats['avg_win']:.2f}%")
            lines.append(f"Avg Loss: {stats['avg_loss']:.2f}%")
            lines.append(f"Best Trade: {stats['largest_win']:.2f}%")
            lines.append(f"Worst Trade: {stats['largest_loss']:.2f}%")
            lines.append("")

            # Performance assessment
            lines.append("-" * 80)
            lines.append("PERFORMANCE ASSESSMENT")
            lines.append("-" * 80)

            if stats['win_rate'] >= 65 and stats['total_pnl'] > 0:
                lines.append("✅ EXCELLENT - Performing above expectations")
            elif stats['win_rate'] >= 55 and stats['total_pnl'] > 0:
                lines.append("✅ GOOD - On track")
            elif stats['total_pnl'] > 0:
                lines.append("⚠️  ACCEPTABLE - Profitable but below target win rate")
            else:
                lines.append("🔴 NEEDS ATTENTION - Review strategy parameters")

            lines.append("")

            # Trade log
            lines.append("-" * 80)
            lines.append(f"TRADE LOG (All {stats['total_trades']} trades)")
            lines.append("-" * 80)

            for i, trade in enumerate(trades, 1):
                outcome_icon = "✅" if trade['outcome'] == 'WIN' else "❌"
                lines.append(
                    f"{i:2d}. {trade['entry_time'].strftime('%m-%d %H:%M')} | "
                    f"{trade['symbol']:6s} | ${trade['entry_price']:.2f} → ${trade['exit_price']:.2f} | "
                    f"{trade['pnl_pct']:+6.2f}% | {outcome_icon}"
                )

        lines.append("")
        lines.append("-" * 80)
        lines.append("NEXT WEEK ACTION ITEMS")
        lines.append("-" * 80)

        if not self.is_paper and stats['total_trades'] > 0:
            if stats['win_rate'] >= 60:
                lines.append("  ✅ Continue current strategy")
                if stats['total_trades'] < 10:
                    lines.append("  📈 Consider adding 2nd strategy to increase frequency")
            else:
                lines.append("  ⚠️  Review and adjust parameters")
                lines.append("  📊 Compare to backtest expectations")

        if self.is_paper:
            lines.append("  🔄 Continue paper trading")
            if stats['total_trades'] >= 20 and stats['win_rate'] >= 60:
                lines.append("  ✅ Ready to consider live deployment")

        lines.append("")
        lines.append("=" * 80)
        lines.append(f"Report generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("=" * 80)

        return "\n".join(lines)

    def send_telegram(self, report: str) -> bool:
        """Send report via Telegram"""
        if not self.telegram_token or not self.telegram_chat_id:
            print("⚠️  Telegram credentials not configured")
            return False

        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"

            # Telegram has 4096 char limit, split if needed
            if len(report) > 4000:
                parts = [report[i:i+4000] for i in range(0, len(report), 4000)]
                for part in parts:
                    payload = {
                        "chat_id": self.telegram_chat_id,
                        "text": part,
                        "parse_mode": "HTML"
                    }
                    httpx.post(url, json=payload, timeout=10)
            else:
                payload = {
                    "chat_id": self.telegram_chat_id,
                    "text": report,
                    "parse_mode": "HTML"
                }
                httpx.post(url, json=payload, timeout=10)

            print("✅ Report sent via Telegram")
            return True

        except Exception as e:
            print(f"❌ Failed to send Telegram message: {e}")
            return False

    def save_report(self, report: str, output_dir: str = "reports"):
        """Save report to file"""
        Path(output_dir).mkdir(exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d")
        filename = f"{output_dir}/weekly_report_{timestamp}.txt"

        with open(filename, 'w') as f:
            f.write(report)

        print(f"✅ Report saved to: {filename}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Generate automated weekly report")
    parser.add_argument("--telegram", action="store_true", help="Send via Telegram")
    parser.add_argument("--save-only", action="store_true", help="Only save to file, don't print")
    parser.add_argument("--output-dir", default="reports", help="Output directory")
    parser.add_argument('--version', action='version', version='AlphaLive Tools v1.0.0')

    args = parser.parse_args()

    # Check environment first
    check_environment()

    try:
        generator = WeeklyReportGenerator()

        print("Fetching trades from past week...", end='', flush=True)
        orders = generator.fetch_week_trades()
        print(f" ✅")

        print("Matching trades...", end='', flush=True)
        trades = generator.match_trades(orders)
        stats = generator.calculate_stats(trades)
        print(f" ✅ Found {len(trades)} completed trades")

        report = generator.generate_report(trades, stats)

        if not args.save_only:
            print("\n" + report)

        generator.save_report(report, args.output_dir)

        if args.telegram:
            generator.send_telegram(report)

    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
