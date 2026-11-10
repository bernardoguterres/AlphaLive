"""Property-based tests (Hypothesis) for the top_n limit rule and quarantine release.

Offline only: no broker, no network.
"""

import json
from pathlib import Path

import pytest

# Hypothesis is not in requirements.txt (CI installs only that file), so skip
# rather than fail collection where it is absent.
pytest.importorskip("hypothesis")

from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from alphalive.portfolio_schema import PortfolioStrategySchema
from alphalive.state import INTENT_UNCERTAIN, BotState

_BASE = json.loads(
    (Path(__file__).parent.parent / "configs" / "example_strategy.json").read_text()
)
_TICKERS = [f"T{i}" for i in range(1, 51)]


def _cfg(top_n, universe, max_open, portfolio_max):
    risk = dict(_BASE["risk"], max_open_positions=max_open, portfolio_max_positions=portfolio_max)
    return {
        "schema_version": "1.0",
        "strategy": {"name": "greenblatt_portfolio", "top_n": top_n, "rebalance_weeks": 52,
                     "weighting": "equal_weight", "parameters": {}},
        "tickers": _TICKERS[:universe],
        "timeframe": "1Week",
        "risk": risk,
        "execution": _BASE["execution"],
        "safety_limits": _BASE["safety_limits"],
        "metadata": _BASE["metadata"],
    }


@st.composite
def _limits(draw):
    """Limits plus a top_n biased to straddle min(limit): ~half fit, half don't."""
    universe = draw(st.integers(2, 50))
    max_open = draw(st.integers(1, 50))
    portfolio_max = draw(st.integers(max_open, 50))  # Risk requires >= max_open
    top_n = draw(st.integers(2, min(50, min(universe, max_open, portfolio_max) + 3)))
    return top_n, universe, max_open, portfolio_max


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])
@given(_limits())
def test_top_n_accepted_iff_it_fits_every_limit(args):
    top_n, universe, max_open, portfolio_max = args
    fits = top_n <= min(universe, max_open, portfolio_max)
    try:
        PortfolioStrategySchema(**_cfg(top_n, universe, max_open, portfolio_max))
        accepted = True
    except ValidationError:
        accepted = False
    # Both directions: a fitting config is never refused, a non-fitting one never accepted.
    assert accepted is fits


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    quarantine=st.booleans(),
    legacy=st.booleans(),
    status_uncertain=st.booleans(),
    operator=st.text(max_size=6),
    note=st.text(max_size=6),
)
def test_release_only_when_quarantined_uncertain_and_attributed(
    tmp_path_factory, quarantine, legacy, status_uncertain, operator, note
):
    st_ = BotState(state_file=str(tmp_path_factory.mktemp("s") / "s.json"))
    it = st_.create_submission_intent(
        ticker="AAPL", side="CLOSE", qty=1.0, order_type="market", strategy_name="ma_crossover"
    )
    fields = {}
    if status_uncertain:
        fields["status"] = INTENT_UNCERTAIN
    if quarantine:
        fields["is_quarantine"] = True
    if legacy:
        fields["last_error"] = "post-cancellation position (1.0) inconsistent with expected remainder (0.0)"
    if fields:
        st_.update_submission_intent(it["intent_id"], **fields)
    expected = status_uncertain and (quarantine or legacy) and operator.strip() != "" and note.strip() != ""
    assert st_.release_quarantine_intent(it["intent_id"], operator, note) is expected
    # an intent that was not released must still block its ticker/side
    if not expected:
        assert st_.get_open_intent("AAPL", "CLOSE") is not None


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(attempts=st.integers(2, 5))
def test_release_is_idempotent(tmp_path_factory, attempts):
    """Only the first release succeeds; later calls change nothing."""
    state = BotState(state_file=str(tmp_path_factory.mktemp("i") / "s.json"))
    it = state.create_submission_intent(
        ticker="AAPL", side="CLOSE", qty=1.0, order_type="market", strategy_name="ma_crossover"
    )
    state.update_submission_intent(it["intent_id"], status=INTENT_UNCERTAIN, is_quarantine=True)
    results = [state.release_quarantine_intent(it["intent_id"], f"op{i}", "checked") for i in range(attempts)]
    assert results == [True] + [False] * (attempts - 1)
    saved = state.get_submission_intent(it["intent_id"])
    assert saved["released_by"] == "op0" and saved["status"] == "reconciled"
