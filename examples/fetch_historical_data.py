"""
Example: Fetch Historical Data for Replay Mode

This script demonstrates how to use the new get_historical_bars() method
to fetch historical data for backtesting and replay simulation.

Usage:
    python examples/fetch_historical_data.py

Note:
    - Requires valid Alpaca API keys in environment variables
    - Uses FREE historical data (no subscription needed)
    - Works with both paper and live accounts
"""

import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from alphalive.broker.alpaca_broker import AlpacaBroker

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


def main():
    """Fetch historical data example."""

    # Initialize broker (using paper account)
    logger.info("Initializing Alpaca broker...")
    broker = AlpacaBroker(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
        paper=True
    )

    # Connect
    logger.info("Connecting to Alpaca...")
    broker.connect()

    # Fetch historical data for AAPL in 2024
    logger.info("Fetching historical data...")
    start_date = datetime(2024, 1, 1, tzinfo=ET)
    end_date = datetime(2024, 12, 31, tzinfo=ET)

    df = broker.get_historical_bars(
        symbol="AAPL",
        timeframe="1Day",
        start=start_date,
        end=end_date
    )

    # Display results
    logger.info(f"\n{'='*80}")
    logger.info("Historical Data Summary")
    logger.info(f"{'='*80}")
    logger.info(f"Symbol: AAPL")
    logger.info(f"Timeframe: 1Day")
    logger.info(f"Date Range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    logger.info(f"Total Bars: {len(df)}")
    logger.info(f"\nFirst 5 bars:")
    print(df.head())
    logger.info(f"\nLast 5 bars:")
    print(df.tail())
    logger.info(f"\nData Statistics:")
    print(df.describe())
    logger.info(f"{'='*80}\n")

    # You can now use this data for replay mode simulation
    logger.info("✓ Historical data fetched successfully!")
    logger.info("  This data can be used for replay mode testing")


if __name__ == "__main__":
    main()
