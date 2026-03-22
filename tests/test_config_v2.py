"""
Test Configuration Loading and Validation (v2)

Tests for the new Pydantic-based config system with multi-strategy support.
"""

import json
import os
import pytest
from pathlib import Path
from unittest.mock import Mock, patch

from alphalive.config import (
    BrokerConfig,
    TelegramConfig,
    AppConfig,
    load_strategy,
    load_strategies,
    load_env,
    validate_all
)
from alphalive.execution.risk_manager import GlobalRiskManager, RiskManager
from alphalive.broker.base_broker import Account


def test_broker_config_validation():
    """Test BrokerConfig validation."""
    # Valid config
    config = BrokerConfig(
        api_key="test_key_123",
        secret_key="test_secret_456",
        paper=True
    )

    assert config.api_key == "test_key_123"
    assert config.secret_key == "test_secret_456"
    assert config.paper is True
    assert config.base_url == "https://paper-api.alpaca.markets"

    # Test live mode
    live_config = BrokerConfig(
        api_key="test_key",
        secret_key="test_secret",
        paper=False
    )
    assert live_config.base_url == "https://api.alpaca.markets"


def test_broker_config_masking():
    """Test API key masking."""
    config = BrokerConfig(
        api_key="test_key_12345",
        secret_key="test_secret_67890",
        paper=True
    )

    assert config.mask_api_key() == "****2345"
    assert config.mask_secret_key() == "****7890"


def test_telegram_config_auto_enable():
    """Test TelegramConfig auto-enablement."""
    # With both token and chat_id
    config = TelegramConfig(
        bot_token="123456:ABC-DEF",
        chat_id="987654321"
    )
    assert config.enabled is True

    # With only token
    config = TelegramConfig(bot_token="123456:ABC-DEF")
    assert config.enabled is False

    # With neither
    config = TelegramConfig()
    assert config.enabled is False


def test_app_config_validation():
    """Test AppConfig validation."""
    broker = BrokerConfig(
        api_key="test_key",
        secret_key="test_secret",
        paper=True
    )
    telegram = TelegramConfig()

    config = AppConfig(
        broker=broker,
        telegram=telegram,
        log_level="DEBUG",
        dry_run=True
    )

    assert config.log_level == "DEBUG"
    assert config.dry_run is True
    assert config.trading_paused is False


def test_load_env_with_required_vars(monkeypatch):
    """Test load_env with required environment variables."""
    from unittest.mock import Mock

    # Mock load_dotenv to prevent loading from .env file
    mock_load_dotenv = Mock(return_value=None)
    monkeypatch.setattr("alphalive.config.load_dotenv", mock_load_dotenv)

    # Clear all env vars first
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("ALPACA_PAPER", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    # Set required env vars
    monkeypatch.setenv("ALPACA_API_KEY", "test_key_123")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test_secret_456")

    app_config = load_env()

    assert app_config.broker.api_key == "test_key_123"
    assert app_config.broker.secret_key == "test_secret_456"
    assert app_config.broker.paper is True  # Default
    assert app_config.telegram.enabled is False  # No telegram vars


def test_load_env_with_all_vars(monkeypatch):
    """Test load_env with all environment variables."""
    monkeypatch.setenv("ALPACA_API_KEY", "test_key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test_secret")
    monkeypatch.setenv("ALPACA_PAPER", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat_id")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("TRADING_PAUSED", "false")

    app_config = load_env()

    assert app_config.broker.paper is False
    assert app_config.telegram.enabled is True
    assert app_config.log_level == "WARNING"
    assert app_config.dry_run is True
    assert app_config.trading_paused is False


def test_load_env_missing_required_vars(monkeypatch):
    """Test load_env fails without required variables."""
    from unittest.mock import Mock

    # Mock load_dotenv to prevent loading from .env file
    mock_load_dotenv = Mock(return_value=None)
    monkeypatch.setattr("alphalive.config.load_dotenv", mock_load_dotenv)

    # Clear all env vars (including from .env file)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    with pytest.raises(ValueError, match="Missing required environment variables"):
        load_env()


def test_load_strategy_valid():
    """Test loading a valid strategy."""
    config_path = Path(__file__).parent.parent / "configs" / "example_strategy.json"

    strategy = load_strategy(str(config_path))

    assert strategy.schema_version == "1.0"
    assert strategy.strategy.name == "ma_crossover"
    assert strategy.ticker == "AAPL"
    assert strategy.timeframe == "1Day"


def test_load_strategy_nonexistent():
    """Test loading non-existent strategy file."""
    with pytest.raises(FileNotFoundError):
        load_strategy("nonexistent.json")


def test_load_strategies_directory():
    """Test loading all strategies from directory."""
    config_dir = Path(__file__).parent.parent / "configs"

    strategies = load_strategies(str(config_dir))

    assert len(strategies) >= 1
    assert all(s.schema_version == "1.0" for s in strategies)


def test_validate_all_success(sample_strategy_config, monkeypatch):
    """Test validate_all with valid configurations."""
    # Set env vars
    monkeypatch.setenv("ALPACA_API_KEY", "test_key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test_secret")

    app_config = load_env()

    valid = validate_all([sample_strategy_config], app_config)

    assert valid is True


def test_global_risk_manager_daily_loss(sample_strategy_config):
    """Test GlobalRiskManager daily loss checking."""
    from alphalive.strategy_schema import SafetyLimits

    grm = GlobalRiskManager()

    # Register a strategy
    rm1 = RiskManager(
        risk_config=sample_strategy_config.risk,
        execution_config=sample_strategy_config.execution,
        strategy_name="strategy1",
        safety_limits=SafetyLimits(),
        notifier=None
    )
    rm1.daily_pnl = -2000.0  # $2k loss
    grm.register_strategy("strategy1", rm1)

    # Should pass initially (2% loss on 100k = 2k < 3% limit)
    can_continue, reason = grm.check_global_daily_loss(100000.0, 3.0)
    assert can_continue is True

    # Add more loss
    rm1.daily_pnl = -3500.0  # $3.5k loss = 3.5% of 100k

    # Should fail (3.5% loss > 3% limit)
    can_continue, reason = grm.check_global_daily_loss(100000.0, 3.0)
    assert can_continue is False
    assert "GLOBAL daily loss limit exceeded" in reason


def test_global_risk_manager_portfolio_positions():
    """Test GlobalRiskManager portfolio position tracking."""
    grm = GlobalRiskManager()

    # Just verify it initializes correctly
    assert grm.is_trading_halted() is False
    stats = grm.get_global_stats()
    assert stats["total_trades"] == 0
    assert stats["total_pnl"] == 0.0


def test_global_risk_manager_record_trade():
    """Test GlobalRiskManager trade recording."""
    grm = GlobalRiskManager()

    grm.record_trade("ma_crossover", 150.50)

    stats = grm.get_global_stats()
    assert stats["total_trades"] == 1


def test_global_risk_manager_halt_state(sample_strategy_config):
    """Test GlobalRiskManager halt state."""
    from alphalive.strategy_schema import SafetyLimits

    grm = GlobalRiskManager()

    # Register a strategy with loss
    rm1 = RiskManager(
        risk_config=sample_strategy_config.risk,
        execution_config=sample_strategy_config.execution,
        strategy_name="strategy1",
        safety_limits=SafetyLimits(),
        notifier=None
    )
    rm1.daily_pnl = -4000.0  # 4% loss on 100k
    grm.register_strategy("strategy1", rm1)

    # Not halted initially
    assert grm.is_trading_halted() is False

    # Check global daily loss (should trigger halt)
    can_continue, reason = grm.check_global_daily_loss(100000.0, 3.0)
    assert can_continue is False

    # Should be halted
    assert grm.is_trading_halted() is True
