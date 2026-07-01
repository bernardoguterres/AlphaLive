#!/usr/bin/env python3
"""
Performance Tracker - Real-time trading performance analysis

Monitors live/paper trading performance and generates daily/weekly reports.
Run this periodically to track strategy performance vs backtest expectations.

Usage:
    python scripts/performance_tracker.py --config configs/production/rsi_simple_SPY_15Min.json
    python scripts/performance_tracker.py --all  # Track all configs
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import OrderSide, QueryOrderStatus


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


def resolve_config_path(config_path: str) -> str:
    """Resolve config path relative to AlphaLive directory"""
    if os.path.isabs(config_path):
        if os.path.exists(config_path):
            return config_path

    # Try current directory first
    if os.path.exists(config_path):
        return os.path.abspath(config_path)

    # Try relative to AlphaLive directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    alphalive_dir = os.path.join(script_dir, '..')
    config_full_path = os.path.join(alphalive_dir, config_path)

    if os.path.exists(config_full_path):
        return config_full_path

        print(f"Error: Config file not found: {config_path}")
    print(f"   Looked in:")
    print(f"   - {os.path.abspath(config_path)}")
    print(f"   - {config_full_path}")
    sys.exit(1)


class PerformanceTracker:
    """Track and analyze live trading performance"""

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = resolve_config_path(config_path) if config_path else None
        self.config = self._load_config() if config_path else None

        # Initialize Alpaca client
        api_key = os.getenv("ALPACA_API_KEY")
        secret_key = os.getenv("ALPACA_SECRET_KEY")
        paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"

        self.client = TradingClient(api_key, secret_key, paper=paper)
        self.account = self.client.get_account()
        self.is_paper = paper

    def _load_config(self) -> Dict:
        """Load strategy configuration with validation"""
        try:
            with open(self.config_path, 'r') as f:
                config = json.load(f)

            # Basic validation
            required = ['strategy', 'ticker', 'timeframe']
            missing = [k for k in required if k not in config]
            if missing:
                raise ValueError(f"Config missing required fields: {missing}")

            return config
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in config file: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"Error loading config: {e}")
            sys.exit(1)

    def get_trades_since(self, days: int = 7) -> List[Dict]:
        """Get all filled trades from last N days with retry logic"""
        after = datetime.now() - timedelta(days=days)

        request = GetOrdersRequest(
            status=QueryOrderStatus.FILLED,
            after=after,
            limit=500
        )

        # Retry logic for API calls
        max_retries = 3
        for attempt in range(max_retries):
            try:
                print(f"Fetching orders from Alpaca (last {days} days)...", end='', flush=True)
                orders = self.client.get_orders(filter=request)
                print(f"Found {len(orders)} orders")

                trades = []
                for order in orders:
                    trades.append({
                        'symbol': order.symbol,
                        'side': order.side.value,
                        'qty': float(order.filled_qty),
                        'fill_price': float(order.filled_avg_price),
                        'filled_at': order.filled_at,
                        'order_id': order.id
                    })

                return trades

            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                wait_time = 2 ** attempt
                print(f"\n API call failed, retrying in {wait_time}s... ({attempt+1}/{max_retries})")
                time.sleep(wait_time)

    def calculate_pnl(self, trades: List[Dict]) -> Dict:
        """Calculate P&L from trades"""
        if not trades:
            return {
                'total_pnl': 0,
                'win_rate': 0,
                'total_trades': 0,
                'winners': 0,
                'losers': 0,
                'avg_win': 0,
                'avg_loss': 0,
                'largest_win': 0,
                'largest_loss': 0
            }

        # Group by symbol and match buy/sell pairs
        df = pd.DataFrame(trades)
        df['filled_at'] = pd.to_datetime(df['filled_at'])
        df = df.sort_values('filled_at')

        positions = {}
        closed_trades = []

        for _, trade in df.iterrows():
            symbol = trade['symbol']

            if symbol not in positions:
                positions[symbol] = []

            if trade['side'] == 'buy':
                positions[symbol].append({
                    'qty': trade['qty'],
                    'entry_price': trade['fill_price'],
                    'entry_time': trade['filled_at']
                })
            elif trade['side'] == 'sell' and positions[symbol]:
                # Match with oldest position (FIFO)
                position = positions[symbol].pop(0)
                pnl = (trade['fill_price'] - position['entry_price']) * position['qty']
                pnl_pct = ((trade['fill_price'] / position['entry_price']) - 1) * 100

                closed_trades.append({
                    'symbol': symbol,
                    'entry_price': position['entry_price'],
                    'exit_price': trade['fill_price'],
                    'qty': position['qty'],
                    'pnl': pnl,
                    'pnl_pct': pnl_pct,
                    'entry_time': position['entry_time'],
                    'exit_time': trade['filled_at'],
                    'hold_time': (trade['filled_at'] - position['entry_time']).total_seconds() / 3600  # hours
                })

        if not closed_trades:
            return {
                'total_pnl': 0,
                'win_rate': 0,
                'total_trades': 0,
                'winners': 0,
                'losers': 0,
                'avg_win': 0,
                'avg_loss': 0,
                'largest_win': 0,
                'largest_loss': 0,
                'open_positions': sum(len(pos) for pos in positions.values())
            }

        closed_df = pd.DataFrame(closed_trades)

        winners = closed_df[closed_df['pnl'] > 0]
        losers = closed_df[closed_df['pnl'] <= 0]

        return {
            'total_pnl': closed_df['pnl'].sum(),
            'total_pnl_pct': closed_df['pnl_pct'].sum(),
            'win_rate': (len(winners) / len(closed_df)) * 100 if len(closed_df) > 0 else 0,
            'total_trades': len(closed_df),
            'winners': len(winners),
            'losers': len(losers),
            'avg_win': winners['pnl_pct'].mean() if len(winners) > 0 else 0,
            'avg_loss': losers['pnl_pct'].mean() if len(losers) > 0 else 0,
            'largest_win': winners['pnl_pct'].max() if len(winners) > 0 else 0,
            'largest_loss': losers['pnl_pct'].min() if len(losers) > 0 else 0,
            'avg_hold_time_hours': closed_df['hold_time'].mean(),
            'open_positions': sum(len(pos) for pos in positions.values()),
            'trades': closed_trades
        }

    def compare_to_backtest(self, live_stats: Dict) -> Dict:
        """Compare live performance to backtest expectations"""
        if not self.config:
            return {}

        backtest = self.config['metadata']['performance']

        return {
            'backtest_win_rate': backtest['win_rate_pct'],
            'live_win_rate': live_stats['win_rate'],
            'win_rate_diff': live_stats['win_rate'] - backtest['win_rate_pct'],
            'backtest_sharpe': backtest['sharpe_ratio'],
            'backtest_avg_win': backtest.get('avg_win_pct', 0),
            'live_avg_win': live_stats['avg_win'],
            'backtest_avg_loss': backtest.get('avg_loss_pct', 0),
            'live_avg_loss': live_stats['avg_loss'],
            'performance_aligned': abs(live_stats['win_rate'] - backtest['win_rate_pct']) < 15
        }

    def generate_report(self, days: int = 7) -> str:
        """Generate performance report"""
        trades = self.get_trades_since(days)
        stats = self.calculate_pnl(trades)

        report = []
        report.append("=" * 80)
        report.append(f"PERFORMANCE REPORT - Last {days} Days")
        report.append("=" * 80)
        report.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append(f"Account Type: {'PAPER' if self.client.paper else 'LIVE'}")
        report.append(f"Account Equity: ${float(self.account.equity):,.2f}")
        report.append(f"Buying Power: ${float(self.account.buying_power):,.2f}")
        report.append("")

        if self.config:
            report.append(f"Strategy: {self.config['strategy']['name']}")
            report.append(f"Ticker: {self.config['ticker']}")
            report.append(f"Timeframe: {self.config['timeframe']}")
            report.append("")

        report.append("-" * 80)
        report.append("TRADING STATISTICS")
        report.append("-" * 80)
        report.append(f"Total Trades (Closed): {stats['total_trades']}")
        report.append(f"Open Positions: {stats.get('open_positions', 0)}")
        report.append(f"Winners: {stats['winners']} | Losers: {stats['losers']}")
        report.append(f"Win Rate: {stats['win_rate']:.1f}%")
        report.append("")

        report.append("-" * 80)
        report.append("PROFIT & LOSS")
        report.append("-" * 80)
        report.append(f"Total P&L: ${stats['total_pnl']:,.2f} ({stats.get('total_pnl_pct', 0):.2f}%)")
        report.append(f"Average Win: {stats['avg_win']:.2f}%")
        report.append(f"Average Loss: {stats['avg_loss']:.2f}%")
        report.append(f"Largest Win: {stats['largest_win']:.2f}%")
        report.append(f"Largest Loss: {stats['largest_loss']:.2f}%")

        if stats.get('avg_hold_time_hours'):
            report.append(f"Avg Hold Time: {stats['avg_hold_time_hours']:.1f} hours")

        report.append("")

        # Compare to backtest if config available
        if self.config and stats['total_trades'] > 0:
            comparison = self.compare_to_backtest(stats)
            report.append("-" * 80)
            report.append("BACKTEST COMPARISON")
            report.append("-" * 80)
            report.append(f"Backtest Win Rate: {comparison['backtest_win_rate']:.1f}%")
            report.append(f"Live Win Rate: {comparison['live_win_rate']:.1f}%")
            report.append(f"Difference: {comparison['win_rate_diff']:+.1f}%")
            report.append("")
            report.append(f"Backtest Avg Win: {comparison['backtest_avg_win']:.2f}%")
            report.append(f"Live Avg Win: {comparison['live_avg_win']:.2f}%")
            report.append(f"Backtest Avg Loss: {comparison['backtest_avg_loss']:.2f}%")
            report.append(f"Live Avg Loss: {comparison['live_avg_loss']:.2f}%")
            report.append("")

            if comparison['performance_aligned']:
                report.append("Performance ALIGNED with backtest expectations")
            else:
                report.append("Performance DIVERGED from backtest (review strategy)")

        # Recent trades
        if stats['total_trades'] > 0:
            report.append("")
            report.append("-" * 80)
            report.append(f"RECENT TRADES (Last {min(10, stats['total_trades'])})")
            report.append("-" * 80)

            for trade in stats['trades'][-10:]:
                outcome = "WIN" if trade['pnl'] > 0 else "LOSS"
                report.append(
                    f"{trade['entry_time'].strftime('%m-%d %H:%M')} | "
                    f"{trade['symbol']:6s} | "
                    f"${trade['entry_price']:.2f} → ${trade['exit_price']:.2f} | "
                    f"{trade['pnl_pct']:+.2f}% | "
                    f"{outcome}"
                )

        report.append("")
        report.append("=" * 80)

        return "\n".join(report)

    def save_report(self, days: int = 7, output_dir: str = "reports"):
        """Generate and save report to file"""
        report = self.generate_report(days)

        # Create reports directory
        Path(output_dir).mkdir(exist_ok=True)

        # Save report
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{output_dir}/performance_{timestamp}.txt"

        with open(filename, 'w') as f:
            f.write(report)

        print(report)
        print(f"\n Report saved to: {filename}")

        return filename


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Track live trading performance")
    parser.add_argument("--config", help="Path to strategy config file")
    parser.add_argument("--days", type=int, default=7, help="Number of days to analyze")
    parser.add_argument("--save", action="store_true", help="Save report to file")
    parser.add_argument('--version', action='version', version='AlphaLive Tools v1.0.0')

    args = parser.parse_args()

    # Check environment first
    check_environment()

    try:
        tracker = PerformanceTracker(args.config)

        if args.save:
            tracker.save_report(days=args.days)
        else:
            print(tracker.generate_report(days=args.days))

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
