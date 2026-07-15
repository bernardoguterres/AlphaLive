"""Regression test for audit bug 2.4 (2026-07-13): check_trailing_stop_requirements()
was fully implemented and unit-tested (tests/test_state.py) but never called
anywhere in the codebase - a trailing-stop strategy could start today with
no persistent storage configured and silently lose position_highs on the
next Railway redeploy, the exact real-money risk the guard exists to catch.

alphalive.main.main() now calls it for every loaded strategy immediately
after validate_all() passes, before any broker connection is attempted.
This test proves the wiring, not just the function in isolation (which
test_state.py already covers thoroughly) - it drives main() itself and
asserts startup is refused before the broker is ever touched.
"""

import copy
from unittest.mock import Mock, patch

import pytest

from alphalive.strategy_schema import StrategySchema


@pytest.fixture
def trailing_stop_strategy_config(sample_strategy_dict):
    config_dict = copy.deepcopy(sample_strategy_dict)
    config_dict["risk"]["trailing_stop_enabled"] = True
    config_dict["risk"]["trailing_stop_pct"] = 3.0
    return StrategySchema(**config_dict)


@pytest.fixture
def mock_app_config():
    from alphalive.config import AppConfig, BrokerConfig, TelegramConfig

    return AppConfig(
        broker=BrokerConfig(api_key="k", secret_key="s", paper=True),
        telegram=TelegramConfig(),
        state_file="/tmp/test_alphalive_state.json",
    )


def test_main_refuses_to_start_trailing_stop_strategy_without_persistent_storage(
    trailing_stop_strategy_config, mock_app_config, monkeypatch
):
    monkeypatch.delenv("PERSISTENT_STORAGE", raising=False)

    with (
        patch(
            "alphalive.main.load_config_path",
            return_value=[trailing_stop_strategy_config],
        ),
        patch("alphalive.main.load_env", return_value=mock_app_config),
        patch("alphalive.main.validate_all", return_value=True),
        patch("alphalive.main.AlpacaBroker") as mock_broker_cls,
    ):
        from alphalive.main import main

        with pytest.raises(SystemExit) as exc_info:
            main(config_path="unused.json")

        assert exc_info.value.code == 1
        # The whole point: startup must be refused BEFORE the broker is
        # ever instantiated/connected, not just eventually logged as unsafe.
        mock_broker_cls.assert_not_called()


def test_main_starts_trailing_stop_strategy_with_persistent_storage(
    trailing_stop_strategy_config, mock_app_config, monkeypatch
):
    """Sanity check the wiring doesn't block legitimate configs - startup
    proceeds past the trailing-stop gate (reaches broker connection) when
    PERSISTENT_STORAGE=true."""
    monkeypatch.setenv("PERSISTENT_STORAGE", "true")

    mock_broker = Mock()
    mock_broker.connect.return_value = False  # stop the test right after the gate

    with (
        patch(
            "alphalive.main.load_config_path",
            return_value=[trailing_stop_strategy_config],
        ),
        patch("alphalive.main.load_env", return_value=mock_app_config),
        patch("alphalive.main.validate_all", return_value=True),
        patch("alphalive.main.AlpacaBroker", return_value=mock_broker),
        patch("alphalive.health.create_health_server"),
    ):
        from alphalive.main import main

        with pytest.raises(SystemExit):
            main(config_path="unused.json")

        mock_broker.connect.assert_called_once()
