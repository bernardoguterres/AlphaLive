"""
Tests for alphalive.migrations.schema_migrations.

test_config.py's test_backward_compatibility_safety_limits already covers
the v1.0 safety_limits backfill via load_strategy(); this covers
migrate_schema() directly, including the unknown-version error.
"""

import pytest

from alphalive.migrations.schema_migrations import migrate_schema


def test_migrate_schema_v1_adds_default_safety_limits():
    config = {"schema_version": "1.0", "ticker": "AAPL"}
    result = migrate_schema(config)

    assert result["safety_limits"]["max_trades_per_day"] == 20
    assert result["safety_limits"]["max_api_calls_per_hour"] == 500


def test_migrate_schema_v1_preserves_existing_safety_limits():
    config = {
        "schema_version": "1.0",
        "safety_limits": {"max_trades_per_day": 5},
    }
    result = migrate_schema(config)

    assert result["safety_limits"] == {"max_trades_per_day": 5}


def test_migrate_schema_defaults_to_v1_when_missing():
    config = {"ticker": "AAPL"}
    result = migrate_schema(config)

    assert "safety_limits" in result


def test_migrate_schema_unknown_version_raises():
    config = {"schema_version": "99.0"}

    with pytest.raises(ValueError, match="Unknown schema version"):
        migrate_schema(config)
