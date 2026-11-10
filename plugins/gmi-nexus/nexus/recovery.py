"""Deciding what to do when a step fails.

Recovery is a policy, not an improvisation. When a call fails, the error is
classified and the policy answers one of three questions:

* **Retry** the same model — only for failures that could plausibly succeed
  on a second attempt (rate limits, timeouts, transient server errors), and
  only up to a bound.
* **Switch** to the next candidate model — for failures that are specific to
  the model (its context window is too small, it does not accept the request,
  it is unavailable right now).
* **Stop** — for failures that will not be fixed by trying again (bad
  credentials, a rejected request, exhausted budget, a required approval).

Retrying a rejected request is pointless: a 400 will be a 400 next time, and
each attempt costs money and time. The policy says so explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import (
    ApprovalRequiredError,
    AuthenticationError,
    BadRequestError,
    BudgetExceededError,
    ContentFilteredError,
    ModelUnavailableError,
    NexusError,
    PermissionDeniedError,
    RateLimitError,
    TimeoutError_,
    TransientError,
    ValidationError,
)

#: The actions recovery can take.
ACTION_RETRY = "retry"
ACTION_SWITCH = "switch"
ACTION_STOP = "stop"


@dataclass(slots=True)
class RecoveryAction:
    """What recovery decided to do about a failure."""

    action: str
    reason: str
    #: How long to wait before the retry, in seconds.
    delay_seconds: float = 0.0
    #: True when the whole run should be abandoned.
    fatal: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "delay_seconds": round(self.delay_seconds, 2),
            "fatal": self.fatal,
        }


def classify_error(error: Exception) -> str:
    """Return the recovery class of an error."""
    if isinstance(error, (RateLimitError, TimeoutError_, TransientError)):
        return "transient"
    if isinstance(error, ModelUnavailableError):
        return "model_unavailable"
    if isinstance(error, BadRequestError):
        return "bad_request"
    if isinstance(error, ContentFilteredError):
        return "filtered"
    if isinstance(error, (AuthenticationError, PermissionDeniedError)):
        return "auth"
    if isinstance(error, (BudgetExceededError, ApprovalRequiredError)):
        return "budget"
    if isinstance(error, ValidationError):
        return "invalid_request"
    if isinstance(error, NexusError):
        return "transient" if error.retryable else "bad_request"
    return "unknown"


def decide(
    error: Exception,
    *,
    attempt: int,
    max_attempts: int,
    candidates_remaining: int = 0,
) -> RecoveryAction:
    """Decide what to do about a failed call.

    ``attempt`` is the attempt number just made (1-based) and
    ``candidates_remaining`` is how many other models could still be tried.
    """
    kind = classify_error(error)

    if kind == "budget":
        return RecoveryAction(
            action=ACTION_STOP,
            reason=f"the spend limit was reached: {error}",
            fatal=True,
        )

    if kind == "auth":
        return RecoveryAction(
            action=ACTION_STOP,
            reason=f"the credentials were rejected: {error}",
            fatal=True,
        )

    if kind == "bad_request":
        return RecoveryAction(
            action=ACTION_SWITCH if candidates_remaining else ACTION_STOP,
            reason=(
                "the request was rejected by the provider, so retrying the same request "
                f"would be rejected again ({error})"
            ),
            fatal=not candidates_remaining,
        )

    if kind == "invalid_request":
        return RecoveryAction(
            action=ACTION_SWITCH if candidates_remaining else ACTION_STOP,
            reason=f"the request did not match what the model accepts ({error})",
            fatal=not candidates_remaining,
        )

    if kind == "filtered":
        return RecoveryAction(
            action=ACTION_SWITCH if candidates_remaining else ACTION_STOP,
            reason=f"the provider filtered the request ({error})",
            fatal=not candidates_remaining,
        )

    if kind == "model_unavailable":
        return RecoveryAction(
            action=ACTION_SWITCH if candidates_remaining else ACTION_STOP,
            reason=f"the model is not available right now ({error})",
            fatal=not candidates_remaining,
        )

    if kind == "transient":
        if attempt < max_attempts:
            delay = min(30.0, 0.5 * (2 ** (attempt - 1)))
            return RecoveryAction(
                action=ACTION_RETRY,
                reason=f"the failure was transient ({error})",
                delay_seconds=delay,
            )
        if candidates_remaining:
            return RecoveryAction(
                action=ACTION_SWITCH,
                reason=f"the retries were exhausted ({error})",
            )
        return RecoveryAction(
            action=ACTION_STOP,
            reason=f"the failure was transient and the retries were exhausted ({error})",
            fatal=True,
        )

    return RecoveryAction(
        action=ACTION_SWITCH if candidates_remaining else ACTION_STOP,
        reason=f"an unexpected failure: {error}",
        fatal=not candidates_remaining,
    )


def apply(executor: Any, step: Any, context: Any, error: Exception, *, attempt: int,
          max_attempts: int, candidates_remaining: int = 0) -> RecoveryAction:
    """Ask for a decision and carry it out."""
    import time

    action = decide(
        error,
        attempt=attempt,
        max_attempts=max_attempts,
        candidates_remaining=candidates_remaining,
    )
    if action.action == ACTION_RETRY and action.delay_seconds > 0:
        time.sleep(action.delay_seconds)
    return action


__all__ = [
    "ACTION_RETRY",
    "ACTION_STOP",
    "ACTION_SWITCH",
    "RecoveryAction",
    "apply",
    "classify_error",
    "decide",
]
