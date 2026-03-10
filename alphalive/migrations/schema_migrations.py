"""
Schema Migration System for AlphaLive

Handles versioned schema migrations to ensure backward compatibility
with strategy JSON files exported from AlphaLab.

Migration Policy:
- Minor versions (1.0, 1.1, 1.2): Backward compatible changes
  * Add new optional fields
  * Add new strategies to enum
  * Existing JSON files work without modification

- Major versions (2.0, 3.0): Breaking changes
  * Rename or remove fields
  * Change field types
  * Add required fields
  * Requires migrate_X_to_Y() function

Usage:
    from alphalive.migrations import migrate_schema

    # Load JSON and apply migrations
    config_dict = json.load(f)
    migrated_config = migrate_schema(config_dict)

    # Validate with Pydantic
    strategy = StrategySchema(**migrated_config)
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def migrate_schema(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Migrate strategy config to latest schema version.

    Applies migrations in sequence: 1.0 → 1.1 → 2.0 → ...
    Each migration function is responsible for updating the schema_version field.

    Args:
        config: Raw strategy configuration dictionary

    Returns:
        Migrated configuration dictionary at the latest schema version

    Raises:
        ValueError: If schema version is unknown or invalid

    Example:
        >>> config = {"schema_version": "1.0", ...}
        >>> migrated = migrate_schema(config)
        >>> migrated["schema_version"]
        "1.0"  # Current version (no migration needed)
    """
    version = config.get("schema_version", "1.0")

    if version == "1.0":
        # Current version — apply v13 backward compatibility enhancements
        if "safety_limits" not in config:
            logger.warning(
                "Adding default safety_limits to v1.0 schema (v13 backward compatibility). "
                "Consider re-exporting from AlphaLab to get explicit values."
            )
            config["safety_limits"] = {
                "max_trades_per_day": 20,
                "max_api_calls_per_hour": 500,
                "signal_generation_timeout_seconds": 5.0,
                "broker_degraded_mode_threshold_failures": 3
            }
        return config

    # Future migrations would go here:
    # elif version == "1.1":
    #     config = migrate_1_1_to_2_0(config)
    #     return migrate_schema(config)  # Recursive for chaining

    else:
        raise ValueError(
            f"Unknown schema version: {version}. "
            f"This AlphaLive version supports schema v1.0 only. "
            f"Please upgrade AlphaLive or downgrade the strategy export."
        )


def migrate_1_0_to_2_0(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Example future migration (not implemented yet).

    This placeholder shows how breaking changes would be handled:
    - Renaming fields
    - Changing field types
    - Adding required fields with computed defaults
    - Removing deprecated fields

    Args:
        config: Strategy config at schema version 1.0

    Returns:
        Strategy config upgraded to schema version 2.0

    Example Breaking Changes (hypothetical):
        # Rename field
        config["risk"]["stop_loss_percent"] = config["risk"].pop("stop_loss_pct")

        # Change type
        config["execution"]["cooldown_seconds"] = config["execution"]["cooldown_bars"] * 60

        # Add required field with default
        config["risk"]["position_scaling_enabled"] = False

        # Remove deprecated field
        config["metadata"].pop("alphalab_version", None)
    """
    logger.info("Migrating schema from 1.0 to 2.0")

    # Example transformations (commented out as this is a placeholder)
    # config["schema_version"] = "2.0"
    # config["risk"]["stop_loss_percent"] = config["risk"].pop("stop_loss_pct")
    # config["risk"]["take_profit_percent"] = config["risk"].pop("take_profit_pct")

    # Placeholder: Just update version
    config["schema_version"] = "2.0"

    return config


def migrate_2_0_to_2_1(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Example minor version migration (backward compatible).

    Minor version migrations add optional fields or extend enums
    without breaking existing configurations.

    Args:
        config: Strategy config at schema version 2.0

    Returns:
        Strategy config upgraded to schema version 2.1

    Example Minor Changes (hypothetical):
        # Add optional field with default
        if "execution" not in config:
            config["execution"] = {}
        config["execution"].setdefault("retry_failed_orders", False)

        # No changes needed for enum extension (backward compatible)
        # strategy.name can accept new values without migration
    """
    logger.info("Migrating schema from 2.0 to 2.1 (backward compatible)")

    # Example: Add optional field with default
    # config["execution"].setdefault("retry_failed_orders", False)

    config["schema_version"] = "2.1"

    return config
