"""Shared retry-with-exponential-backoff helper for outbound network calls.

Audit finding (E2E_Simulation_CodeQuality_Audit_2026-07-13, Part 14 #5):
order_manager, alpaca_broker, market_data, and telegram_bot each hand-rolled
their own attempt-loop/sleep/backoff-doubling boilerplate. The *decision* of
which errors are retryable is genuinely different per call site (status-code
routing, exception types), so this does not try to unify that; it only
extracts the shared loop mechanics via a caller-supplied classifier.
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryDecision(Enum):
    """What to do after `func()` raised an exception."""

    RETRY = auto()
    FATAL = auto()


@dataclass
class RetryOutcome:
    """Result of classifying an exception raised by the retried call.

    Attributes:
        decision: RETRY to sleep and try again (if attempts remain), FATAL
            to re-raise immediately without retrying.
        delay_override: If set, use this delay instead of the computed
            exponential backoff for this attempt (e.g. to honor a server's
            Retry-After header).
        log_message: If set, used instead of the default warning message.
    """

    decision: RetryDecision
    delay_override: float | None = None
    log_message: str | None = None


def retry_with_backoff(
    func: Callable[[], T],
    *,
    classify: Callable[[Exception], RetryOutcome],
    max_retries: int = 3,
    base_delay: float = 1.0,
    multiplier: float = 2.0,
) -> T:
    """Call func(), retrying RETRY-classified exceptions with exponential backoff.

    Args:
        func: Zero-argument callable to invoke.
        classify: Given an exception raised by func(), returns a
            RetryOutcome deciding whether to retry.
        max_retries: Maximum number of attempts (including the first).
        base_delay: Delay in seconds before the second attempt; doubles
            (times `multiplier`) after each subsequent retry.
        multiplier: Backoff multiplier applied to base_delay each retry.

    Returns:
        The return value of func() on success.

    Raises:
        Whatever func() raised, once classified FATAL or once max_retries
        is exhausted on a RETRY-classified exception.
    """
    delay = base_delay

    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except Exception as e:
            outcome = classify(e)

            if outcome.decision is RetryDecision.FATAL or attempt >= max_retries:
                raise

            wait = (
                outcome.delay_override if outcome.delay_override is not None else delay
            )
            logger.warning(
                outcome.log_message
                or f"Retryable error (attempt {attempt}/{max_retries}): {e}. "
                f"Retrying in {wait:.1f}s..."
            )
            time.sleep(wait)
            delay *= multiplier

    # Unreachable: the loop above always returns or raises.
    raise AssertionError("retry_with_backoff: exhausted loop without raising")
