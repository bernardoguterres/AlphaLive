"""Tests for PortfolioStrategySchema (multi-ticker portfolio config shape)."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from alphalive.portfolio_schema import PortfolioStrategySchema, all_portfolio_tickers


@pytest.fixture
def valid_strategy_dict():
    return json.loads(
        (Path(__file__).parent.parent / "configs" / "example_strategy.json").read_text()
    )


def _portfolio_dict(valid_strategy_dict, **overrides):
    d = {
        "schema_version": "1.0",
        "strategy": {
            "name": "greenblatt_portfolio",
            "top_n": 3,
            "rebalance_weeks": 52,
            "weighting": "equal_weight",
            "parameters": {},
        },
        "tickers": ["AAPL", "MSFT", "GOOGL", "META", "JNJ"],
        "timeframe": "1Week",
        "risk": valid_strategy_dict["risk"],
        "execution": valid_strategy_dict["execution"],
        "safety_limits": valid_strategy_dict["safety_limits"],
        "metadata": valid_strategy_dict["metadata"],
    }
    d.update(overrides)
    return d


class TestPortfolioStrategySchema:
    def test_valid_portfolio_config_parses(self, valid_strategy_dict):
        cfg = PortfolioStrategySchema(**_portfolio_dict(valid_strategy_dict))
        assert cfg.strategy.name == "greenblatt_portfolio"
        assert cfg.strategy.top_n == 3
        assert len(cfg.tickers) == 5

    def test_tickers_normalized_to_uppercase(self, valid_strategy_dict):
        d = _portfolio_dict(valid_strategy_dict)
        d["tickers"] = ["aapl", "msft", "googl"]
        d["strategy"]["top_n"] = 2
        cfg = PortfolioStrategySchema(**d)
        assert cfg.tickers == ["AAPL", "MSFT", "GOOGL"]

    def test_duplicate_tickers_within_universe_rejected(self, valid_strategy_dict):
        d = _portfolio_dict(valid_strategy_dict)
        d["tickers"] = ["AAPL", "MSFT", "AAPL"]
        with pytest.raises(ValidationError, match="Duplicate tickers"):
            PortfolioStrategySchema(**d)

    def test_top_n_exceeding_universe_size_rejected(self, valid_strategy_dict):
        d = _portfolio_dict(valid_strategy_dict)
        d["strategy"]["top_n"] = 10  # only 5 tickers
        with pytest.raises(ValidationError, match="cannot exceed"):
            PortfolioStrategySchema(**d)

    def test_fewer_than_two_tickers_rejected(self, valid_strategy_dict):
        d = _portfolio_dict(valid_strategy_dict)
        d["tickers"] = ["AAPL"]
        d["strategy"]["top_n"] = 1
        with pytest.raises(ValidationError):
            PortfolioStrategySchema(**d)

    def test_invalid_ticker_format_rejected(self, valid_strategy_dict):
        d = _portfolio_dict(valid_strategy_dict)
        d["tickers"] = ["AAPL", "!!!bad"]
        d["strategy"]["top_n"] = 2
        with pytest.raises(ValidationError, match="Invalid ticker symbol"):
            PortfolioStrategySchema(**d)

    def test_only_equal_weight_literal_accepted(self, valid_strategy_dict):
        d = _portfolio_dict(valid_strategy_dict)
        d["strategy"]["weighting"] = "rank_weighted"
        with pytest.raises(ValidationError):
            PortfolioStrategySchema(**d)

    def test_wrong_schema_version_rejected(self, valid_strategy_dict):
        # Pydantic's Literal["1.0"] type check rejects "2.0" before the
        # custom validate_schema_version validator ever runs (same
        # pre-existing pattern as StrategySchema in strategy_schema.py) -
        # the validator is defensive/documentation, not the actual gate.
        d = _portfolio_dict(valid_strategy_dict)
        d["schema_version"] = "2.0"
        with pytest.raises(ValidationError):
            PortfolioStrategySchema(**d)

    def test_single_ticker_strategy_name_not_a_valid_portfolio_name(
        self, valid_strategy_dict
    ):
        d = _portfolio_dict(valid_strategy_dict)
        d["strategy"][
            "name"
        ] = "ma_crossover"  # a single-ticker StrategyName, not portfolio
        with pytest.raises(ValidationError):
            PortfolioStrategySchema(**d)


class TestAllPortfolioTickers:
    def test_claims_the_whole_universe_not_just_top_n(self, valid_strategy_dict):
        cfg = PortfolioStrategySchema(**_portfolio_dict(valid_strategy_dict))
        claimed = all_portfolio_tickers([cfg])
        assert set(claimed.keys()) == set(cfg.tickers)
        assert all(v == "greenblatt_portfolio" for v in claimed.values())

    def test_empty_list_returns_empty_dict(self):
        assert all_portfolio_tickers([]) == {}
