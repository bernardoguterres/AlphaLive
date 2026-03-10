"""
Test Configuration Loading and Validation
"""

import json
import os
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch

from alphalive.config import load_config, load_strategy, load_strategies, load_config_path, load_env, validate_all
from alphalive.migrations import migrate_schema
from alphalive.strategy_schema import StrategySchema


def test_load_example_config():
    """Test loading example strategy config."""
    config_path = Path(__file__).parent.parent / "configs" / "example_strategy.json"
    config = load_config(str(config_path))

    assert config.schema_version == "1.0"
    assert config.strategy.name == "ma_crossover"
    assert config.ticker == "AAPL"
    assert config.timeframe == "1Day"


def test_schema_validation_fails_on_invalid_version():
    """Test that invalid schema versions are rejected."""
    from alphalive.strategy_schema import StrategySchema
    from pydantic import ValidationError

    invalid_config = {
        "schema_version": "2.0",  # Invalid version
        "strategy": {"name": "ma_crossover", "parameters": {}},
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
            "trailing_stop_pct": None,
            "commission_per_trade": 0.0
        },
        "execution": {"order_type": "market", "cooldown_bars": 1},
        "safety_limits": {
            "max_trades_per_day": 20,
            "max_api_calls_per_hour": 500,
            "signal_generation_timeout_seconds": 5.0,
            "broker_degraded_mode_threshold_failures": 3
        },
        "metadata": {
            "exported_from": "AlphaLab",
            "exported_at": "2026-03-08T10:00:00Z",
            "alphalab_version": "0.2.0",
            "backtest_id": "bt_001",
            "backtest_period": {"start": "2020-01-01", "end": "2024-12-31"},
            "performance": {
                "sharpe_ratio": 1.45,
                "sortino_ratio": 1.82,
                "total_return_pct": 32.5,
                "max_drawdown_pct": -12.3,
                "win_rate_pct": 58.2,
                "profit_factor": 1.75,
                "total_trades": 47,
                "calmar_ratio": 2.64
            }
        }
    }

    with pytest.raises(ValidationError):
        StrategySchema(**invalid_config)


def test_backward_compatibility_safety_limits():
    """Test that missing safety_limits gets defaults applied."""
    config_dict = {
        "schema_version": "1.0",
        "strategy": {"name": "ma_crossover", "parameters": {}},
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
            "trailing_stop_pct": None,
            "commission_per_trade": 0.0
        },
        "execution": {"order_type": "market", "cooldown_bars": 1},
        "metadata": {
            "exported_from": "AlphaLab",
            "exported_at": "2026-03-08T10:00:00Z",
            "alphalab_version": "0.2.0",
            "backtest_id": "bt_001",
            "backtest_period": {"start": "2020-01-01", "end": "2024-12-31"},
            "performance": {
                "sharpe_ratio": 1.45,
                "sortino_ratio": 1.82,
                "total_return_pct": 32.5,
                "max_drawdown_pct": -12.3,
                "win_rate_pct": 58.2,
                "profit_factor": 1.75,
                "total_trades": 47,
                "calmar_ratio": 2.64
            }
        }
        # safety_limits is missing
    }

    # Apply migration
    migrated = migrate_schema(config_dict)

    # Verify defaults were added
    assert "safety_limits" in migrated
    assert migrated["safety_limits"]["max_trades_per_day"] == 20
    assert migrated["safety_limits"]["max_api_calls_per_hour"] == 500


def test_load_multiple_strategies_from_directory(sample_strategy_dict):
    """Test loading multiple strategy JSON files from a directory."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create two strategy files
        strategy1 = sample_strategy_dict.copy()
        strategy1["ticker"] = "AAPL"

        strategy2 = sample_strategy_dict.copy()
        strategy2["ticker"] = "TSLA"
        strategy2["strategy"]["name"] = "rsi_mean_reversion"

        with open(Path(temp_dir) / "strategy1.json", 'w') as f:
            json.dump(strategy1, f)

        with open(Path(temp_dir) / "strategy2.json", 'w') as f:
            json.dump(strategy2, f)

        strategies = load_strategies(temp_dir)

        assert len(strategies) == 2
        tickers = {s.ticker for s in strategies}
        assert "AAPL" in tickers
        assert "TSLA" in tickers


def test_load_config_path_single_file(sample_strategy_dict):
    """Test load_config_path with a single JSON file."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create one strategy file
        strategy_path = Path(temp_dir) / "strategy.json"
        with open(strategy_path, 'w') as f:
            json.dump(sample_strategy_dict, f)

        # Load using load_config_path (file mode)
        strategies = load_config_path(str(strategy_path))

        # Should return list with 1 strategy
        assert len(strategies) == 1
        assert strategies[0].ticker == "AAPL"


def test_load_config_path_directory(sample_strategy_dict):
    """Test load_config_path with a directory of JSON files."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create two strategy files
        strategy1 = sample_strategy_dict.copy()
        strategy1["ticker"] = "AAPL"

        strategy2 = sample_strategy_dict.copy()
        strategy2["ticker"] = "TSLA"

        with open(Path(temp_dir) / "strategy1.json", 'w') as f:
            json.dump(strategy1, f)

        with open(Path(temp_dir) / "strategy2.json", 'w') as f:
            json.dump(strategy2, f)

        # Load using load_config_path (directory mode)
        strategies = load_config_path(temp_dir)

        # Should return list with 2 strategies
        assert len(strategies) == 2
        tickers = {s.ticker for s in strategies}
        assert "AAPL" in tickers
        assert "TSLA" in tickers


def test_load_env_correctly(sample_app_config_dict):
    """Test that load_env() reads environment variables correctly."""
    with patch.dict(os.environ, sample_app_config_dict, clear=False):
        app_config = load_env()

        assert app_config.broker.api_key == "test_api_key_12345"
        assert app_config.broker.secret_key == "test_secret_key_67890"
        assert app_config.broker.paper is True
        assert app_config.telegram.bot_token == "123456:ABC-DEF"
        assert app_config.telegram.chat_id == "123456789"
        assert app_config.log_level == "INFO"
        assert app_config.dry_run is False
        assert app_config.trading_paused is False


def test_mask_api_keys_in_logs(sample_app_config_dict):
    """Test that API keys are masked in logs."""
    with patch.dict(os.environ, sample_app_config_dict, clear=False):
        app_config = load_env()

        masked_api_key = app_config.broker.mask_api_key()
        masked_secret_key = app_config.broker.mask_secret_key()

        # Should show last 4 characters only
        assert masked_api_key == "****2345"
        assert masked_secret_key == "****7890"

        # Should NOT contain full keys
        assert "test_api_key_12345" not in masked_api_key
        assert "test_secret_key_67890" not in masked_secret_key


def test_validate_sane_ranges(sample_strategy_dict):
    """Test that validation rejects insane parameter ranges."""
    from pydantic import ValidationError

    # Test negative stop loss
    invalid_config = sample_strategy_dict.copy()
    invalid_config["risk"]["stop_loss_pct"] = -5.0

    with pytest.raises(ValidationError):
        StrategySchema(**invalid_config)

    # Test stop loss below minimum (0.1)
    invalid_config = sample_strategy_dict.copy()
    invalid_config["risk"]["stop_loss_pct"] = 0.05

    with pytest.raises(ValidationError):
        StrategySchema(**invalid_config)

    # Test stop loss above maximum (50.0)
    invalid_config = sample_strategy_dict.copy()
    invalid_config["risk"]["stop_loss_pct"] = 100.0

    with pytest.raises(ValidationError):
        StrategySchema(**invalid_config)


def test_validate_all_prints_summary_and_returns_true(
    sample_strategy_dict, sample_app_config_dict, caplog
):
    """Test that validate_all() logs summary and returns True for valid config."""
    import logging
    caplog.set_level(logging.INFO)

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(sample_strategy_dict, f)
        temp_path = f.name

    try:
        strategy = load_strategy(temp_path)

        with patch.dict(os.environ, sample_app_config_dict, clear=False):
            app_config = load_env()

            # validate_all should return True and log summary
            result = validate_all([strategy], app_config)

            assert result is True

            # Check that summary was logged
            log_output = caplog.text
            assert "ALPHALIVE CONFIGURATION SUMMARY" in log_output
            assert "STRATEGIES" in log_output
            assert "BROKER" in log_output
            assert "ALL CONFIGURATIONS VALID" in log_output
    finally:
        os.unlink(temp_path)
