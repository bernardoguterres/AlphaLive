"""
Configuration Loading and Validation

Loads strategy JSON files exported from AlphaLab and validates against
the StrategySchema using migrations for backward compatibility.

Supports multi-strategy mode with global risk management.
"""

import json
import logging
import os
from pathlib import Path
from typing import Dict, Any, List, Optional

from pydantic import BaseModel, Field, field_validator, ValidationError
from dotenv import load_dotenv

from alphalive.migrations import migrate_schema
from alphalive.strategy_schema import StrategySchema

logger = logging.getLogger(__name__)


# =============================================================================
# Pydantic Configuration Models
# =============================================================================

class BrokerConfig(BaseModel):
    """Broker API configuration."""
    api_key: str = Field(..., description="Alpaca API key")
    secret_key: str = Field(..., description="Alpaca secret key")
    paper: bool = Field(default=True, description="Use paper trading")
    base_url: Optional[str] = Field(
        default=None,
        description="Custom base URL (auto-set based on paper flag if None)"
    )

    def model_post_init(self, __context):
        """Set base_url based on paper flag if not provided."""
        if self.base_url is None:
            self.base_url = (
                "https://paper-api.alpaca.markets" if self.paper
                else "https://api.alpaca.markets"
            )

    @field_validator('api_key', 'secret_key')
    @classmethod
    def validate_not_empty(cls, v: str) -> str:
        """Ensure API keys are not empty."""
        if not v or v.strip() == "":
            raise ValueError("API key cannot be empty")
        return v

    def mask_api_key(self) -> str:
        """Return masked API key for logging."""
        if len(self.api_key) <= 4:
            return "****"
        return f"****{self.api_key[-4:]}"

    def mask_secret_key(self) -> str:
        """Return masked secret key for logging."""
        if len(self.secret_key) <= 4:
            return "****"
        return f"****{self.secret_key[-4:]}"


class TelegramConfig(BaseModel):
    """Telegram notification configuration."""
    bot_token: Optional[str] = Field(default=None, description="Telegram bot token")
    chat_id: Optional[str] = Field(default=None, description="Telegram chat ID")
    enabled: bool = Field(default=False, description="Enable notifications")

    def model_post_init(self, __context):
        """Auto-enable if both token and chat_id are provided."""
        if self.bot_token and self.chat_id:
            self.enabled = True
        else:
            self.enabled = False


class AppConfig(BaseModel):
    """Application-level configuration."""
    broker: BrokerConfig = Field(..., description="Broker configuration")
    telegram: TelegramConfig = Field(..., description="Telegram configuration")
    log_level: str = Field(default="INFO", description="Logging level")
    dry_run: bool = Field(default=False, description="Dry run mode (no real trades)")
    trading_paused: bool = Field(default=False, description="Pause all trading")
    state_file: str = Field(
        default="/tmp/alphalive_state.json",
        description="State persistence file path"
    )
    health_port: int = Field(default=8080, description="Health check HTTP port")
    health_secret: str = Field(default="", description="Health check secret token")
    persistent_storage: bool = Field(
        default=False,
        description="Enable persistent storage (Railway volumes)"
    )

    @field_validator('log_level')
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        v = v.upper()
        if v not in valid_levels:
            raise ValueError(f"Invalid log level: {v}. Must be one of {valid_levels}")
        return v


# =============================================================================
# Strategy Loading Functions
# =============================================================================

def load_strategy(path: str) -> StrategySchema:
    """
    Load and validate a single strategy configuration from JSON file.

    Applies schema migrations for backward compatibility before validation.

    Args:
        path: Path to strategy JSON file

    Returns:
        Validated StrategySchema instance

    Raises:
        FileNotFoundError: If config file doesn't exist
        ValueError: If JSON is invalid
        ValidationError: If schema validation fails

    Example:
        >>> config = load_strategy("configs/ma_crossover.json")
        >>> print(config.strategy.name)
        "ma_crossover"
    """
    config_path = Path(path)

    if not config_path.exists():
        raise FileNotFoundError(f"Strategy config not found: {config_path}")

    logger.info(f"Loading strategy configuration from {config_path}")

    try:
        # Load JSON
        with open(config_path, "r") as f:
            config_dict = json.load(f)

        logger.debug(f"Loaded JSON with schema version: {config_dict.get('schema_version', 'unknown')}")

        # Apply migrations (backward compatibility)
        migrated_config = migrate_schema(config_dict)

        # Validate with Pydantic
        strategy_config = StrategySchema(**migrated_config)

        logger.info(
            f"✓ Strategy loaded: {strategy_config.strategy.name} on "
            f"{strategy_config.ticker} @ {strategy_config.timeframe} | "
            f"Sharpe: {strategy_config.metadata.performance.sharpe_ratio:.2f}"
        )

        return strategy_config

    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in {config_path}: {e}")
        raise ValueError(f"Invalid JSON in {config_path}: {e}")
    except ValidationError as e:
        logger.error(f"Schema validation failed for {config_path}")
        # Log detailed validation errors
        for error in e.errors():
            field = " -> ".join(str(loc) for loc in error['loc'])
            logger.error(f"  ✗ {field}: {error['msg']}")
        raise
    except Exception as e:
        logger.error(f"Failed to load config from {config_path}: {e}")
        raise


def load_strategies(config_dir: str) -> List[StrategySchema]:
    """
    Load all strategy configurations from a directory.

    Supports multi-strategy mode where multiple strategies run simultaneously.

    Args:
        config_dir: Directory containing strategy JSON files

    Returns:
        List of validated StrategySchema instances

    Raises:
        ValueError: If no valid strategy files found

    Example:
        >>> strategies = load_strategies("configs/")
        >>> print(f"Loaded {len(strategies)} strategies")
    """
    config_path = Path(config_dir)

    if not config_path.exists():
        raise FileNotFoundError(f"Config directory not found: {config_dir}")

    if not config_path.is_dir():
        raise ValueError(f"Path is not a directory: {config_dir}")

    # Find all JSON files
    json_files = list(config_path.glob("*.json"))

    if not json_files:
        raise ValueError(f"No JSON files found in {config_dir}")

    logger.info(f"Found {len(json_files)} strategy files in {config_dir}")

    strategies = []
    errors = []

    for json_file in json_files:
        try:
            strategy = load_strategy(str(json_file))
            strategies.append(strategy)
        except Exception as e:
            error_msg = f"{json_file.name}: {str(e)}"
            errors.append(error_msg)
            logger.warning(f"Skipping {json_file.name}: {e}")

    if not strategies:
        logger.error("No valid strategies loaded")
        if errors:
            logger.error("Errors encountered:")
            for error in errors:
                logger.error(f"  - {error}")
        raise ValueError("No valid strategies could be loaded from directory")

    logger.info(f"Successfully loaded {len(strategies)} strategies")

    if errors:
        logger.warning(f"Failed to load {len(errors)} strategies:")
        for error in errors:
            logger.warning(f"  - {error}")

    return strategies


def load_config_path(path: str) -> List[StrategySchema]:
    """
    Load strategy configuration(s) from a file or directory.

    Supports both single-strategy and multi-strategy modes:
    - If path is a file: Load single strategy, return as list with 1 element
    - If path is a directory: Load all JSON files, return list of strategies

    Args:
        path: Path to strategy JSON file or directory

    Returns:
        List of validated StrategySchema instances

    Raises:
        FileNotFoundError: If path doesn't exist
        ValueError: If invalid file/directory or no strategies found

    Example:
        >>> # Single strategy
        >>> strategies = load_config_path("configs/ma_crossover.json")
        >>> print(len(strategies))  # 1

        >>> # Multi-strategy
        >>> strategies = load_config_path("configs/strategies/")
        >>> print(len(strategies))  # 3
    """
    config_path = Path(path)

    if not config_path.exists():
        raise FileNotFoundError(f"Config path not found: {path}")

    if config_path.is_dir():
        # Multi-strategy mode: load all JSON files from directory
        logger.info(f"Loading strategies from directory: {path}")
        return load_strategies(path)
    elif config_path.is_file():
        # Single strategy mode: load one file, return as list
        logger.info(f"Loading single strategy from file: {path}")
        strategy = load_strategy(path)
        return [strategy]
    else:
        raise ValueError(f"Path is neither file nor directory: {path}")


# =============================================================================
# Environment Variable Loading
# =============================================================================

def load_env() -> AppConfig:
    """
    Load application configuration from environment variables.

    Priority:
    1. OS environment variables (set by Railway in production)
    2. .env file (for local development only)

    Returns:
        Validated AppConfig instance

    Raises:
        ValueError: If required variables are missing or invalid

    Environment Variables:
        Required:
        - ALPACA_API_KEY: Alpaca API key
        - ALPACA_SECRET_KEY: Alpaca secret key

        Optional:
        - TELEGRAM_BOT_TOKEN: Telegram bot token
        - TELEGRAM_CHAT_ID: Telegram chat ID
        - ALPACA_PAPER: Use paper trading (default: true)
        - ALPACA_BASE_URL: Custom base URL (optional)
        - LOG_LEVEL: Logging level (default: INFO)
        - DRY_RUN: Dry run mode (default: false)
        - TRADING_PAUSED: Pause trading (default: false)
        - STATE_FILE: State file path (default: /tmp/alphalive_state.json)
        - HEALTH_PORT: Health check port (default: 8080)
        - HEALTH_SECRET: Health check secret (default: "")
        - PERSISTENT_STORAGE: Enable persistent storage (default: false)

    Example:
        >>> app_config = load_env()
        >>> print(f"Broker: Alpaca {'Paper' if app_config.broker.paper else 'Live'}")
    """
    # Try to load .env file (for local development)
    # In production (Railway), env vars are already set in the environment
    dotenv_path = Path(".env")
    if dotenv_path.exists():
        logger.debug("Loading .env file for local development")
        load_dotenv(dotenv_path)
    else:
        logger.debug("No .env file found, using environment variables only")

    # Validate required variables
    required_vars = ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]

    if missing_vars:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing_vars)}. "
            f"Set these in Railway dashboard or .env file for local dev."
        )

    # Parse boolean values
    def parse_bool(value: str) -> bool:
        return value.lower() in ("true", "1", "yes", "on")

    # Build broker config
    broker_config = BrokerConfig(
        api_key=os.getenv("ALPACA_API_KEY"),
        secret_key=os.getenv("ALPACA_SECRET_KEY"),
        paper=parse_bool(os.getenv("ALPACA_PAPER", "true")),
        base_url=os.getenv("ALPACA_BASE_URL")  # None if not set
    )

    # Build telegram config
    telegram_config = TelegramConfig(
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        chat_id=os.getenv("TELEGRAM_CHAT_ID")
    )

    # Build app config
    app_config = AppConfig(
        broker=broker_config,
        telegram=telegram_config,
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        dry_run=parse_bool(os.getenv("DRY_RUN", "false")),
        trading_paused=parse_bool(os.getenv("TRADING_PAUSED", "false")),
        state_file=os.getenv("STATE_FILE", "/tmp/alphalive_state.json"),
        health_port=int(os.getenv("HEALTH_PORT", "8080")),
        health_secret=os.getenv("HEALTH_SECRET", ""),
        persistent_storage=parse_bool(os.getenv("PERSISTENT_STORAGE", "false"))
    )

    # Log configuration (with masked API keys)
    logger.info("Application configuration loaded")
    logger.debug(f"  Broker API Key: {broker_config.mask_api_key()}")
    logger.debug(f"  Broker Secret Key: {broker_config.mask_secret_key()}")
    logger.debug(f"  Broker Mode: {'Paper Trading' if broker_config.paper else 'Live Trading'}")
    logger.debug(f"  Telegram: {'Enabled' if telegram_config.enabled else 'Disabled'}")
    logger.debug(f"  Log Level: {app_config.log_level}")
    logger.debug(f"  Dry Run: {app_config.dry_run}")
    logger.debug(f"  Trading Paused: {app_config.trading_paused}")

    return app_config


# =============================================================================
# Validation and Summary
# =============================================================================

def validate_all(strategies: List[StrategySchema], app_config: AppConfig) -> bool:
    """
    Validate all configurations and print summary.

    Args:
        strategies: List of strategy configurations
        app_config: Application configuration

    Returns:
        True if all valid, False otherwise

    Displays:
        Summary table with configuration status for each component
    """
    logger.info("=" * 80)
    logger.info("ALPHALIVE CONFIGURATION SUMMARY")
    logger.info("=" * 80)

    all_valid = True
    errors = []

    # Validate strategies
    logger.info(f"\nSTRATEGIES ({len(strategies)} loaded):")
    for i, strategy in enumerate(strategies, 1):
        try:
            sharpe = strategy.metadata.performance.sharpe_ratio
            total_return = strategy.metadata.performance.total_return_pct
            logger.info(
                f"  ✅ [{i}] {strategy.strategy.name} on {strategy.ticker} @ {strategy.timeframe} | "
                f"Sharpe: {sharpe:.2f} | Return: {total_return:.1f}%"
            )
        except Exception as e:
            logger.error(f"  ✗ [{i}] Invalid strategy: {e}")
            all_valid = False
            errors.append(f"Strategy {i}: {e}")

    # Validate broker
    logger.info(f"\nBROKER:")
    try:
        mode = "Paper Trading" if app_config.broker.paper else "Live Trading"
        logger.info(f"  ✅ Alpaca {mode}")
        logger.info(f"     API Key: {app_config.broker.mask_api_key()}")
        logger.info(f"     Base URL: {app_config.broker.base_url}")
    except Exception as e:
        logger.error(f"  ✗ Broker configuration invalid: {e}")
        all_valid = False
        errors.append(f"Broker: {e}")

    # Validate telegram
    logger.info(f"\nNOTIFICATIONS:")
    if app_config.telegram.enabled:
        chat_id_masked = f"...{app_config.telegram.chat_id[-4:]}" if app_config.telegram.chat_id else "None"
        logger.info(f"  ✅ Telegram: Configured (chat: {chat_id_masked})")
    else:
        logger.warning(f"  ⚠️  Telegram: Disabled (no bot_token or chat_id)")

    # Validate risk settings (show first strategy as example)
    if strategies:
        logger.info(f"\nRISK MANAGEMENT (example from first strategy):")
        risk = strategies[0].risk
        logger.info(f"  ✅ Stop Loss: {risk.stop_loss_pct}%")
        logger.info(f"  ✅ Take Profit: {risk.take_profit_pct}%")
        logger.info(f"  ✅ Max Position Size: {risk.max_position_size_pct}%")
        logger.info(f"  ✅ Max Daily Loss: {risk.max_daily_loss_pct}% (GLOBAL across all strategies)")
        logger.info(f"  ✅ Max Open Positions: {risk.max_open_positions} (PER STRATEGY)")
        logger.info(f"  ✅ Portfolio Max Positions: {risk.portfolio_max_positions} (GLOBAL)")

    # Application settings
    logger.info(f"\nAPPLICATION SETTINGS:")
    logger.info(f"  Log Level: {app_config.log_level}")
    logger.info(f"  Dry Run: {'YES (trades will be logged but not executed)' if app_config.dry_run else 'NO'}")
    logger.info(f"  Trading Paused: {'YES' if app_config.trading_paused else 'NO'}")
    logger.info(f"  State File: {app_config.state_file}")

    # Multi-strategy risk scope clarification
    if len(strategies) > 1:
        logger.info(f"\nMULTI-STRATEGY RISK SCOPE:")
        logger.info(f"  • max_open_positions: PER STRATEGY (each strategy can have up to N positions)")
        logger.info(f"  • max_daily_loss_pct: GLOBAL (all strategies halted if total account loss exceeds limit)")
        logger.info(f"  • max_position_size_pct: PER STRATEGY (% of total account equity)")
        logger.info(f"  • portfolio_max_positions: GLOBAL (total positions across all strategies)")

        total_max_positions = sum(s.risk.max_open_positions for s in strategies)
        portfolio_limit = strategies[0].risk.portfolio_max_positions
        logger.info(f"  Total potential positions: {total_max_positions} (capped by portfolio limit: {portfolio_limit})")

    # Summary
    logger.info("\n" + "=" * 80)
    if all_valid:
        logger.info("✅ ALL CONFIGURATIONS VALID - Ready to trade!")
    else:
        logger.error("✗ CONFIGURATION ERRORS FOUND:")
        for error in errors:
            logger.error(f"  - {error}")
        logger.error("\nPlease fix the errors above before starting AlphaLive.")
    logger.info("=" * 80 + "\n")

    return all_valid


# =============================================================================
# Helper Functions (Backward Compatibility)
# =============================================================================

def load_config(config_path: str) -> StrategySchema:
    """
    Backward compatibility wrapper for load_strategy().

    Args:
        config_path: Path to strategy JSON file

    Returns:
        Validated StrategySchema instance
    """
    return load_strategy(config_path)


def validate_environment_variables() -> Dict[str, str]:
    """
    Backward compatibility wrapper.

    Returns:
        Dictionary of environment variables

    Raises:
        ValueError: If required variables are missing
    """
    app_config = load_env()

    return {
        "alpaca_api_key": app_config.broker.api_key,
        "alpaca_secret_key": app_config.broker.secret_key,
        "alpaca_paper": str(app_config.broker.paper).lower(),
        "alpaca_base_url": app_config.broker.base_url,
        "telegram_bot_token": app_config.telegram.bot_token or "",
        "telegram_chat_id": app_config.telegram.chat_id or "",
        "log_level": app_config.log_level,
        "dry_run": str(app_config.dry_run).lower(),
        "trading_paused": str(app_config.trading_paused).lower(),
        "state_file": app_config.state_file,
        "health_port": str(app_config.health_port),
        "health_secret": app_config.health_secret,
        "persistent_storage": str(app_config.persistent_storage).lower()
    }


def get_config_from_env() -> str:
    """
    Get strategy config path from environment variable.

    Returns:
        Path to strategy configuration file

    Raises:
        ValueError: If STRATEGY_CONFIG is not set
    """
    config_path = os.getenv("STRATEGY_CONFIG")

    if not config_path:
        raise ValueError(
            "STRATEGY_CONFIG environment variable not set. "
            "Set this to the path of your strategy JSON file."
        )

    return config_path
