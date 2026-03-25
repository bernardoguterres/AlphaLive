#!/usr/bin/env python3
"""
Log Analysis Helper

Extracts and summarizes trade decisions from AlphaLive logs.
Useful for understanding why trades were/weren't made.

Usage:
    python scripts/analyze_logs.py logs/alphalive.log
    python scripts/analyze_logs.py logs/alphalive.log.2026-03-25
"""

import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List


def parse_log_file(log_path: Path) -> Dict[str, List[Dict]]:
    """
    Parse log file and extract trade decisions.

    Returns:
        Dictionary with keys: signals, trades, blocks, errors
    """
    results = {
        "signals": [],
        "trades": [],
        "blocks": [],
        "errors": [],
        "indicators": []
    }

    with open(log_path, 'r') as f:
        for line in f:
            # Extract timestamp
            timestamp_match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
            timestamp = timestamp_match.group(1) if timestamp_match else "Unknown"

            # Signal checks
            if "Signal:" in line and "Confidence:" in line:
                signal_match = re.search(r'Signal: (\w+) \| Confidence: ([\d.]+)%', line)
                if signal_match:
                    results["signals"].append({
                        "timestamp": timestamp,
                        "signal": signal_match.group(1),
                        "confidence": float(signal_match.group(2)),
                        "line": line.strip()
                    })

            # Indicator values
            elif "Indicators:" in line:
                results["indicators"].append({
                    "timestamp": timestamp,
                    "line": line.strip()
                })

            # Trade decisions
            elif "Trade decision |" in line:
                decision_match = re.search(r'Signal: (\w+) \| Action: (\w+)(?: \w+)? \| Reason: (.+)', line)
                if decision_match:
                    signal, action, reason = decision_match.groups()
                    if action == "BLOCKED":
                        results["blocks"].append({
                            "timestamp": timestamp,
                            "signal": signal,
                            "reason": reason,
                            "line": line.strip()
                        })
                    else:
                        results["signals"].append({
                            "timestamp": timestamp,
                            "signal": signal,
                            "action": action,
                            "reason": reason,
                            "line": line.strip()
                        })

            # Trade executions
            elif "Trade executed |" in line:
                trade_match = re.search(r'(BUY|SELL) ([\d.]+) (\w+) @ \$([\d.]+)', line)
                if trade_match:
                    side, qty, ticker, price = trade_match.groups()
                    results["trades"].append({
                        "timestamp": timestamp,
                        "side": side,
                        "qty": float(qty),
                        "ticker": ticker,
                        "price": float(price),
                        "line": line.strip()
                    })

            # Errors
            elif "[ERROR]" in line or "Trade blocked:" in line:
                results["errors"].append({
                    "timestamp": timestamp,
                    "line": line.strip()
                })

    return results


def print_summary(results: Dict[str, List[Dict]]):
    """Print human-readable summary of log analysis."""
    print("=" * 80)
    print("ALPHALIVE LOG ANALYSIS")
    print("=" * 80)

    # Signal summary
    print(f"\n📊 SIGNAL SUMMARY")
    print(f"{'─' * 80}")
    signal_counts = defaultdict(int)
    for sig in results["signals"]:
        signal_counts[sig["signal"]] += 1

    for signal, count in sorted(signal_counts.items()):
        print(f"  {signal}: {count}")

    # Trade summary
    print(f"\n💰 TRADE EXECUTIONS: {len(results['trades'])}")
    print(f"{'─' * 80}")
    if results["trades"]:
        for trade in results["trades"]:
            total = trade["qty"] * trade["price"]
            print(f"  [{trade['timestamp']}] {trade['side']} {trade['qty']} {trade['ticker']} "
                  f"@ ${trade['price']:.2f} (Total: ${total:.2f})")
    else:
        print("  No trades executed")

    # Blocked trades
    print(f"\n🚫 BLOCKED TRADES: {len(results['blocks'])}")
    print(f"{'─' * 80}")
    if results["blocks"]:
        block_reasons = defaultdict(int)
        for block in results["blocks"]:
            # Extract key part of reason for grouping
            reason_short = block["reason"].split(":")[0] if ":" in block["reason"] else block["reason"]
            block_reasons[reason_short] += 1

        for reason, count in sorted(block_reasons.items(), key=lambda x: -x[1]):
            print(f"  {reason}: {count}")

        print(f"\n  Recent blocks:")
        for block in results["blocks"][-5:]:  # Show last 5
            print(f"  [{block['timestamp']}] {block['signal']} blocked: {block['reason'][:60]}...")
    else:
        print("  No trades blocked")

    # Errors
    if results["errors"]:
        print(f"\n⚠️  ERRORS: {len(results['errors'])}")
        print(f"{'─' * 80}")
        for err in results["errors"][-10:]:  # Show last 10
            print(f"  [{err['timestamp']}] {err['line'][:100]}")

    # Recent indicator snapshots
    if results["indicators"]:
        print(f"\n📈 RECENT INDICATOR SNAPSHOTS")
        print(f"{'─' * 80}")
        for ind in results["indicators"][-5:]:  # Show last 5
            print(f"  [{ind['timestamp']}]")
            # Extract indicator values
            ind_line = ind['line'].split("Indicators: ")[1] if "Indicators: " in ind['line'] else ""
            print(f"    {ind_line}")

    print("\n" + "=" * 80)


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/analyze_logs.py <log_file>")
        print("Example: python scripts/analyze_logs.py logs/alphalive.log")
        sys.exit(1)

    log_path = Path(sys.argv[1])
    if not log_path.exists():
        print(f"Error: Log file not found: {log_path}")
        sys.exit(1)

    print(f"Analyzing log file: {log_path}")
    results = parse_log_file(log_path)
    print_summary(results)


if __name__ == "__main__":
    main()
