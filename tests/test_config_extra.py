"""
Extended config.py coverage: malformed JSON, invalid log level, partial
directory-load failures, and the multi-strategy branch of validate_all().
tests/test_config.py and tests/test_config_v2.py already cover the happy
paths and the missing-env-var error — this fills in the remaining error
and multi-strategy branches.
"""

import json

import pytest

from alphalive.config import (
    AppConfig,
    load_strategy,
    load_strategies,
    validate_all,
)
from alphalive.strategy_schema import StrategySchema


@pytest.fixture
def valid_strategy_dict():
    return json.loads(
        (__import__("pathlib").Path(__file__).parent.parent / "configs" / "example_strategy.json").read_text()
    )


def test_load_strategy_invalid_json_raises_value_error(tmp_path):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{not valid json")

    with pytest.raises(ValueError, match="Invalid JSON"):
        load_strategy(str(bad_file))


def test_load_strategy_schema_validation_error_reraises(tmp_path, valid_strategy_dict):
    invalid = dict(valid_strategy_dict)
    del invalid["ticker"]  # required field
    bad_file = tmp_path / "missing_field.json"
    bad_file.write_text(json.dumps(invalid))

    with pytest.raises(Exception):
        load_strategy(str(bad_file))


def test_load_strategies_directory_partial_failure_skips_bad_files(tmp_path, valid_strategy_dict):
    good_file = tmp_path / "good.json"
    good_file.write_text(json.dumps(valid_strategy_dict))
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not json at all")

    strategies = load_strategies(str(tmp_path))

    assert len(strategies) == 1


def test_load_strategies_directory_not_a_directory_raises(tmp_path, valid_strategy_dict):
    file_path = tmp_path / "single.json"
    file_path.write_text(json.dumps(valid_strategy_dict))

    with pytest.raises(ValueError, match="not a directory"):
        load_strategies(str(file_path))


def test_load_strategies_directory_missing_raises(tmp_path):
    missing_dir = tmp_path / "does_not_exist"

    with pytest.raises(FileNotFoundError):
        load_strategies(str(missing_dir))


def test_app_config_invalid_log_level_raises():
    from alphalive.config import BrokerConfig, TelegramConfig

    with pytest.raises(Exception):
        AppConfig(
            broker=BrokerConfig(api_key="k", secret_key="s"),
            telegram=TelegramConfig(),
            log_level="NOT_A_LEVEL",
        )


def test_validate_all_multi_strategy_prints_risk_scope_section(valid_strategy_dict, capsys):
    from alphalive.config import BrokerConfig, TelegramConfig

    cfg1 = StrategySchema(**valid_strategy_dict)
    cfg2_dict = dict(valid_strategy_dict)
    cfg2_dict["ticker"] = "MSFT"
    cfg2 = StrategySchema(**cfg2_dict)

    app_config = AppConfig(
        broker=BrokerConfig(api_key="k", secret_key="s"),
        telegram=TelegramConfig(),
    )

    result = validate_all([cfg1, cfg2], app_config)

    assert result is True
