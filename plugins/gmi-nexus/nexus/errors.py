"""Error taxonomy shared by every NEXUS component.

The recovery policy keys off :class:`NexusError.kind`, so every failure raised
inside the engine is classified exactly once, at the boundary, and never
classified twice.
"""

from __future__ import annotations

from typing import Any


class NexusError(Exception):
    """Base class for every NEXUS failure.

    ``kind`` is a stable, machine-readable slug. The recovery policy maps it to
    a decision (retry, switch model, give up). ``retryable`` is the default
    hint; the recovery policy may override it per-situation.
    """

    kind = "unknown"
    retryable = False

    def __init__(self, message: str, *, detail: Any = None, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "status": self.status,
            "retryable": self.retryable,
            "detail": self.detail,
        }


class NotConfiguredError(NexusError):
    """The plugin is missing a credential it needs to reach GMI Cloud."""

    kind = "not_configured"
    retryable = False


class ConfigError(NexusError):
    """A setting was present but unusable (bad URL, wrong type, ...)."""

    kind = "config_error"
    retryable = False


class AuthenticationError(NexusError):
    """GMI Cloud rejected the API key."""

    kind = "authentication"
    retryable = False


class PermissionDeniedError(NexusError):
    """The credential is valid but not allowed to use the requested model."""

    kind = "permission_denied"
    retryable = False


class RateLimitError(NexusError):
    """GMI Cloud asked us to slow down."""

    kind = "rate_limit"
    retryable = True


class TimeoutError_(NexusError):
    """The request exceeded the configured timeout."""

    kind = "timeout"
    retryable = True


class TransientError(NexusError):
    """A 5xx or a broken connection: worth retrying the same request."""

    kind = "transient"
    retryable = True


class BadRequestError(NexusError):
    """The request was malformed or the model rejected the payload."""

    kind = "bad_request"
    retryable = False


class ModelUnavailableError(NexusError):
    """The model id is unknown, retired or not currently routable."""

    kind = "model_unavailable"
    retryable = False


class ContentFilteredError(NexusError):
    """The provider refused to produce output for this request."""

    kind = "content_filtered"
    retryable = False


class BudgetExceededError(NexusError):
    """The run would exceed a configured spend limit."""

    kind = "budget_exceeded"
    retryable = False


class ApprovalRequiredError(NexusError):
    """The operation is above the approval threshold and was not approved."""

    kind = "approval_required"
    retryable = False


class ValidationError(NexusError):
    """A response failed schema/contract validation."""

    kind = "validation_error"
    retryable = False


class NexusCancelledError(NexusError):
    """The operator cancelled the run."""

    kind = "cancelled"
    retryable = False


#: HTTP status codes that map onto a specific error class.
_STATUS_ERRORS: dict[int, type[NexusError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: ModelUnavailableError,
    408: TimeoutError_,
    422: BadRequestError,
    429: RateLimitError,
}


def classify_status(status: int, body: str = "") -> type[NexusError]:
    """Map an HTTP status onto the error class that matches it."""
    if status in _STATUS_ERRORS:
        return _STATUS_ERRORS[status]
    if status == 402:
        return BudgetExceededError
    if 500 <= status < 600:
        return TransientError
    return NexusError


def error_from_response(status: int, body: str) -> NexusError:
    """Build a typed error from a non-2xx GMI response."""
    cls = classify_status(status, body)
    message = _extract_error_message(body) or f"GMI Cloud returned HTTP {status}"
    return cls(message, detail=body[:2000], status=status)


#: The phrases a provider uses when it will not serve a model any more.
#:
#: GMI's catalogue is published independently of the models that can actually
#: be served, so a retired model can keep its place in the catalogue after the
#: provider has dropped it. A call to one of those comes back as a generic
#: failure with the real reason nested inside it, and this is where that
#: reason is recognised.
RETIREMENT_SIGNALS: tuple[str, ...] = (
    "deprecated",
    "no longer available",
    "unavailablemodel",
    "model not found",
    "does not exist",
    "has been retired",
    "is retired",
    "not available on this",
)


def is_retirement_message(message: str) -> bool:
    """Whether an error message says the model itself is gone.

    A retired model does not come back on a retry, so this is worth
    remembering: the plugin records it and stops routing to it.
    """
    lowered = (message or "").lower()
    return any(signal in lowered for signal in RETIREMENT_SIGNALS)


def _message_from_details(details: Any) -> str:
    """Pull the real reason out of a wrapped error's ``details``."""
    if isinstance(details, str):
        text = details.strip()
        if not text:
            return ""
        try:
            import json

            parsed = json.loads(text)
        except (ValueError, TypeError):
            return text[:300]
        return _message_from_details(parsed)
    if isinstance(details, dict):
        err = details.get("error")
        if isinstance(err, dict):
            for key in ("message", "detail"):
                value = err.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:300]
        for key in ("message", "detail"):
            value = details.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:300]
    return ""


def _extract_error_message(body: str) -> str:
    """Pull a human-readable message out of a GMI/OpenAI-style error body.

    A wrapped error keeps the reason in ``error.details`` — usually a JSON
    string — while ``error.message`` says only that the backend failed. The
    reason is the part an operator needs, so it is reported first and the
    wrapper's own wording is kept alongside it.
    """
    if not body:
        return ""
    try:
        import json

        parsed = json.loads(body)
    except (ValueError, TypeError):
        return body.strip()[:400]
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict):
            primary = ""
            for key in ("message", "detail", "type"):
                value = err.get(key)
                if isinstance(value, str) and value.strip():
                    primary = value.strip()
                    break
            specific = _message_from_details(err.get("details"))
            if specific:
                if not primary or specific.lower() in primary.lower():
                    return specific[:400]
                return f"{specific} ({primary})"[:400]
            if primary:
                return primary[:400]
        if isinstance(err, str) and err.strip():
            return err.strip()[:400]
        for key in ("message", "detail"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:400]
    return ""
