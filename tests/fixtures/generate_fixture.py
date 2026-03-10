#!/usr/bin/env python3
"""
Generate synthetic AAPL fixture data for mini-checkpoint testing.
This is a temporary substitute until real AlphaLab data is available.
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# Set seed for reproducibility
np.random.seed(42)

# Generate 500 bars starting from 2022-01-03
start_date = datetime(2022, 1, 3)
dates = pd.date_range(start=start_date, periods=500, freq='B')  # Business days

# Generate realistic price movement (random walk with drift)
initial_price = 180.0
returns = np.random.normal(0.0005, 0.02, 500)  # Mean return ~0.05%, volatility ~2%
close_prices = initial_price * np.exp(np.cumsum(returns))

# Generate OHLC data
high_prices = close_prices * (1 + np.random.uniform(0.002, 0.015, 500))
low_prices = close_prices * (1 - np.random.uniform(0.002, 0.015, 500))

# Open is close from previous day plus some noise
open_prices = np.zeros(500)
open_prices[0] = close_prices[0] * (1 + np.random.normal(0, 0.005))
for i in range(1, 500):
    open_prices[i] = close_prices[i-1] * (1 + np.random.normal(0, 0.005))

# Ensure high is highest and low is lowest
for i in range(500):
    prices = [open_prices[i], close_prices[i]]
    high_prices[i] = max(high_prices[i], max(prices))
    low_prices[i] = min(low_prices[i], min(prices))

# Generate volume (realistic AAPL-like volume)
base_volume = 80_000_000
volume = np.random.lognormal(np.log(base_volume), 0.3, 500).astype(int)

# Create DataFrame
df = pd.DataFrame({
    'timestamp': dates,
    'open': open_prices,
    'high': high_prices,
    'low': low_prices,
    'close': close_prices,
    'volume': volume
})

# Save to CSV
output_path = 'tests/fixtures/aapl_fixture_500bars.csv'
df.to_csv(output_path, index=False)

print(f"✅ Generated {len(df)} bars of synthetic AAPL data")
print(f"   Saved to: {output_path}")
print(f"\nFirst 5 rows:")
print(df.head())
print(f"\nLast 5 rows:")
print(df.tail())
print(f"\nPrice range: ${df['close'].min():.2f} - ${df['close'].max():.2f}")
print(f"Volume range: {df['volume'].min():,} - {df['volume'].max():,}")
