#!/bin/bash
# Test paper trading connection with ONE strategy

echo "Testing AlphaLive paper trading connection..."
echo "================================================"

# Load environment from .env.paper_test
export $(cat .env.paper_test | grep -v '^#' | xargs)

# Run validation-only mode first
# --validate-only is offline (config only); the broker connection is exercised by the paper run itself
python3 run.py --validate-only --config configs/production/ma_crossover_SPY.json

