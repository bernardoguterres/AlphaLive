"""
Pytest Configuration and Shared Fixtures

Provides reusable test fixtures for AlphaLive tests.
"""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest

from alphalive.broker.base_broker import Account, Position, Order
from alphalive.strategy_schema import StrategySchema


@pytest.fixture
def sample_strategy_config():
    """
    Load example strategy configuration.

    Returns:
        StrategySchema instance
    """
    config_path = Path(__file__).parent.parent / "configs" / "example_strategy.json"

    with open(config_path) as f:
        config_dict = json.load(f)

    return StrategySchema(**config_dict)


@pytest.fixture
def mock_broker():
    """
    Create a mock broker with standard responses.

    Returns:
        Mock broker instance
    """
    broker = Mock()

    # Mock account
    broker.get_account.return_value = Account(
        equity=100000.0,
        cash=50000.0,
        buying_power=200000.0,
        portfolio_value=100000.0,
        long_market_value=50000.0,
        short_market_value=0.0,
        daytrade_count=0,
        pattern_day_trader=False
    )

    # Mock positions (empty by default)
    broker.get_positions.return_value = []

    # Mock get_position (None by default)
    broker.get_position.return_value = None

    # Mock market open
    broker.is_market_open.return_value = True

    # Mock place_order
    broker.place_order.return_value = Order(
        id="order_123",
        symbol="AAPL",
        qty=10.0,
        side="buy",
        order_type="market",
        limit_price=None,
        status="filled",
        filled_qty=10.0,
        filled_avg_price=150.0,
        submitted_at=datetime.now(),
        filled_at=datetime.now()
    )

    # Mock get_bars
    broker.get_bars.return_value = [
        {
            "timestamp": datetime.now(),
            "open": 150.0,
            "high": 152.0,
            "low": 149.0,
            "close": 151.0,
            "volume": 1000000
        }
    ]

    return broker


@pytest.fixture
def mock_account_state():
    """
    Mock account state for testing.

    Returns:
        Dictionary with account state
    """
    return {
        "equity": 100000.0,
        "cash": 50000.0,
        "buying_power": 200000.0,
        "portfolio_value": 100000.0,
        "positions": []
    }


@pytest.fixture
def sample_bars():
    """
    Sample OHLCV bars for testing indicators/signals.

    Returns:
        DataFrame with 50 bars
    """
    data = []
    base_price = 100.0

    for i in range(50):
        price = base_price + (i * 0.5)
        data.append({
            "timestamp": datetime.now(),
            "open": price,
            "high": price + 1.0,
            "low": price - 1.0,
            "close": price + 0.5,
            "volume": 1000000
        })

    return pd.DataFrame(data)


@pytest.fixture
def sample_position():
    """
    Sample position for testing.

    Returns:
        Position instance
    """
    return Position(
        symbol="AAPL",
        qty=10.0,
        side="long",
        avg_entry_price=150.0,
        current_price=155.0,
        unrealized_pl=50.0,
        unrealized_plpc=3.33,
        market_value=1550.0
    )


@pytest.fixture
def mock_telegram():
    """
    Create a mock Telegram notifier.

    Returns:
        Mock telegram notifier
    """
    notifier = Mock()
    notifier.send_message.return_value = True
    notifier.send_startup_notification.return_value = True
    notifier.send_trade_notification.return_value = True
    notifier.send_position_closed_notification.return_value = True
    notifier.send_error_alert.return_value = True
    notifier.send_alert.return_value = True
    notifier.send_daily_summary.return_value = True
    notifier.send_shutdown_notification.return_value = True
    notifier.is_offline.return_value = False
    notifier.enabled = True
    return notifier


@pytest.fixture
def mock_market_data():
    """
    Create a mock market data fetcher.

    Returns:
        Mock market data fetcher
    """
    fetcher = Mock()

    # Return sample bars by default
    data = []
    base_price = 100.0
    for i in range(200):
        price = base_price + (i * 0.5)
        data.append({
            "open": price,
            "high": price + 1.0,
            "low": price - 1.0,
            "close": price + 0.5,
            "volume": 1000000
        })

    df = pd.DataFrame(data)
    df.index = pd.date_range(start="2024-01-01", periods=200, freq="D", tz="America/New_York")

    fetcher.get_latest_bars.return_value = df
    fetcher.get_current_price.return_value = 150.0

    return fetcher


@pytest.fixture
def ma_crossover_bars():
    """
    Create bars with known MA crossover pattern.

    Returns:
        DataFrame with golden cross pattern
    """
    data = []
    # Create bars where fast SMA(10) crosses above slow SMA(20)
    for i in range(50):
        if i < 30:
            # Fast below slow
            price = 100.0 - (i * 0.1)
        else:
            # Fast crosses above slow
            price = 100.0 + ((i - 30) * 0.5)

        data.append({
            "open": price,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price,
            "volume": 1000000
        })

    df = pd.DataFrame(data)
    df.index = pd.date_range(start="2024-01-01", periods=50, freq="D", tz="America/New_York")
    return df


@pytest.fixture
def rsi_oversold_bars():
    """
    Create bars with RSI in oversold territory (<30).

    Returns:
        DataFrame with oversold RSI pattern
    """
    data = []
    # Create declining prices to push RSI below 30
    for i in range(50):
        price = 100.0 - (i * 1.0)  # Strong downtrend
        data.append({
            "open": price + 0.5,
            "high": price + 1.0,
            "low": price - 0.5,
            "close": price,
            "volume": 1000000
        })

    df = pd.DataFrame(data)
    df.index = pd.date_range(start="2024-01-01", periods=50, freq="D", tz="America/New_York")
    return df


@pytest.fixture
def rsi_overbought_bars():
    """
    Create bars with RSI in overbought territory (>70).

    Returns:
        DataFrame with overbought RSI pattern
    """
    data = []
    # Create rising prices to push RSI above 70
    for i in range(50):
        price = 100.0 + (i * 1.0)  # Strong uptrend
        data.append({
            "open": price - 0.5,
            "high": price + 0.5,
            "low": price - 1.0,
            "close": price,
            "volume": 1000000
        })

    df = pd.DataFrame(data)
    df.index = pd.date_range(start="2024-01-01", periods=50, freq="D", tz="America/New_York")
    return df


@pytest.fixture
def momentum_breakout_bars():
    """
    Create bars with momentum breakout pattern.

    Returns:
        DataFrame with breakout above recent high with volume surge
    """
    data = []
    for i in range(50):
        if i < 40:
            # Consolidation
            price = 100.0 + (i % 5) * 0.2
            volume = 1000000
        else:
            # Breakout
            price = 105.0 + (i - 40) * 0.5
            volume = 2000000  # Volume surge

        data.append({
            "open": price - 0.2,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price,
            "volume": volume
        })

    df = pd.DataFrame(data)
    df.index = pd.date_range(start="2024-01-01", periods=50, freq="D", tz="America/New_York")
    return df


@pytest.fixture
def sample_strategy_dict():
    """
    Sample strategy configuration as dictionary.

    Returns:
        Dictionary matching StrategySchema
    """
    return {
        "schema_version": "1.0",
        "strategy": {
            "name": "ma_crossover",
            "parameters": {
                "fast_period": 10,
                "slow_period": 20
            },
            "description": "Test strategy"
        },
        "ticker": "AAPL",
        "timeframe": "1Day",
        "risk": {
            "stop_loss_pct": 2.0,
            "take_profit_pct": 5.0,
            "max_position_size_pct": 10.0,
            "max_daily_loss_pct": 3.0,
            "max_open_positions": 5,
            "portfolio_max_positions": 10,
            "trailing_stop_enabled": False,
            "trailing_stop_pct": 3.0,
            "commission_per_trade": 0.0
        },
        "execution": {
            "order_type": "market",
            "limit_offset_pct": 0.1,
            "cooldown_bars": 1
        },
        "safety_limits": {
            "max_trades_per_day": 20,
            "max_api_calls_per_hour": 500,
            "signal_generation_timeout_seconds": 5.0,
            "broker_degraded_mode_threshold_failures": 3
        },
        "metadata": {
            "exported_from": "AlphaLab",
            "exported_at": "2024-01-01T00:00:00Z",
            "alphalab_version": "0.1.0",
            "backtest_id": "test_123",
            "backtest_period": {
                "start": "2020-01-01",
                "end": "2023-12-31"
            },
            "performance": {
                "sharpe_ratio": 1.5,
                "sortino_ratio": 2.0,
                "total_return_pct": 25.0,
                "max_drawdown_pct": -10.0,
                "win_rate_pct": 55.0,
                "profit_factor": 1.8,
                "total_trades": 50,
                "calmar_ratio": 2.5
            }
        }
    }


@pytest.fixture
def sample_app_config_dict():
    """
    Sample application configuration as dictionary.

    Returns:
        Dictionary for AppConfig
    """
    return {
        "ALPACA_API_KEY": "test_api_key_12345",
        "ALPACA_SECRET_KEY": "test_secret_key_67890",
        "ALPACA_PAPER": "true",
        "TELEGRAM_BOT_TOKEN": "123456:ABC-DEF",
        "TELEGRAM_CHAT_ID": "123456789",
        "LOG_LEVEL": "INFO",
        "DRY_RUN": "false",
        "TRADING_PAUSED": "false"
    }
