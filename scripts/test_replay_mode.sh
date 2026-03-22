#!/bin/bash
#
# Test Replay Mode with Historical Data (FREE)
#
# This script demonstrates how to test your trading strategies
# on historical data without paying for a premium Alpaca subscription.
#
# Historical data is FREE on Alpaca (9+ years available).
# Only real-time data requires a subscription.

echo "================================================================"
echo "AlphaLive Replay Mode - Test Your Strategy on Historical Data"
echo "================================================================"
echo ""
echo "Testing periods (avoiding COVID-19 anomaly):"
echo "  • Pre-COVID:  2015-2019 (5 years of normal markets)"
echo "  • Post-COVID: 2022-2024 (3 years of recovery)"
echo ""
echo "================================================================"
echo ""

# Check if strategy config exists
if [ ! -f "configs/ma_crossover_AAPL_2020-2024.json" ]; then
    echo "❌ Error: Strategy config not found"
    echo "   Please create configs/ma_crossover_AAPL_2020-2024.json first"
    exit 1
fi

# Check if environment variables are set
if [ -z "$ALPACA_API_KEY" ] || [ -z "$ALPACA_SECRET_KEY" ]; then
    echo "❌ Error: Alpaca API credentials not set"
    echo ""
    echo "Please set environment variables:"
    echo "  export ALPACA_API_KEY='your_key_here'"
    echo "  export ALPACA_SECRET_KEY='your_secret_here'"
    echo ""
    exit 1
fi

echo "✓ Environment variables set"
echo "✓ Strategy config found"
echo ""

# Ask user which period to test
echo "Which period would you like to test?"
echo "  1) Pre-COVID (2015-2019) - 5 years"
echo "  2) Post-COVID (2022-2024) - 3 years"
echo "  3) Combined (2015-2019 + 2022-2024) - 8 years"
echo "  4) Custom date range"
echo ""
read -p "Enter choice (1-4): " choice

case $choice in
    1)
        START="2015-01-01"
        END="2019-12-31"
        PERIOD="Pre-COVID (2015-2019)"
        ;;
    2)
        START="2022-01-01"
        END="2024-12-31"
        PERIOD="Post-COVID (2022-2024)"
        ;;
    3)
        echo ""
        echo "Note: Combined testing will run two separate simulations:"
        echo "  1. Pre-COVID (2015-2019)"
        echo "  2. Post-COVID (2022-2024)"
        echo ""
        read -p "Press Enter to continue..."

        # Run Pre-COVID first
        echo ""
        echo "Running Pre-COVID simulation (2015-2019)..."
        python run.py \
            --config configs/ma_crossover_AAPL_2020-2024.json \
            --replay-mode \
            --replay-start 2015-01-01 \
            --replay-end 2019-12-31 \
            --dry-run

        echo ""
        echo "================================================================"
        echo "Pre-COVID simulation complete!"
        echo "================================================================"
        echo ""
        read -p "Press Enter to continue to Post-COVID simulation..."

        # Run Post-COVID second
        START="2022-01-01"
        END="2024-12-31"
        PERIOD="Post-COVID (2022-2024)"
        ;;
    4)
        echo ""
        read -p "Enter start date (YYYY-MM-DD): " START
        read -p "Enter end date (YYYY-MM-DD): " END
        PERIOD="Custom ($START to $END)"
        ;;
    *)
        echo "Invalid choice"
        exit 1
        ;;
esac

echo ""
echo "================================================================"
echo "Starting Replay Simulation"
echo "================================================================"
echo "  Period: $PERIOD"
echo "  Strategy: MA Crossover (AAPL)"
echo "  Mode: DRY RUN (no real orders)"
echo "================================================================"
echo ""
read -p "Press Enter to start simulation..."

# Run replay mode
python run.py \
    --config configs/ma_crossover_AAPL_2020-2024.json \
    --replay-mode \
    --replay-start "$START" \
    --replay-end "$END" \
    --dry-run

echo ""
echo "================================================================"
echo "Replay simulation complete!"
echo "================================================================"
echo ""
echo "Check your Telegram for the final results summary."
echo ""
echo "If the strategy performed well, consider upgrading to Alpaca"
echo "premium (\$9-99/month) to enable live trading."
echo ""
