"""Spend limits and approval gates.

NEXUS never spends money without a limit in place, and never spends past a
limit without a human saying so. Three gates apply, in order:

1. **Per-run cap** — one run may not cost more than ``max_cost_per_run_usd``.
2. **Daily cap** — the organisation's tracked spend for the day may not exceed
   ``daily_budget_usd``.
3. **Approval** — a single operation whose estimated cost is at or above
   ``approval_threshold_usd`` is held until it is approved.

The approval gate raises :class:`ApprovalRequiredError` rather than blocking
forever: the estimate travels with the error, so Hermes' own approval flow (or
a human) can approve it and the call can be retried with ``approved=True``.

All three figures are *estimates at list price*, adjusted for the discount the
live catalogue publishes for the model. They are estimates of what the call is
expected to cost, never a claim about what was billed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .errors import ApprovalRequiredError, BudgetExceededError

#: Start of the current UTC day, used as the daily-budget window.
_SECONDS_PER_DAY = 86400.0


def _start_of_day() -> float:
    """Return the timestamp at the start of the current UTC day."""
    now = time.time()
    return now - (now % _SECONDS_PER_DAY)


@dataclass(slots=True)
class BudgetDecision:
    """The outcome of a budget check."""

    allowed: bool
    estimated_cost_usd: float
    reason: str = ""
    requires_approval: bool = False
    daily_spend_usd: float = 0.0
    daily_remaining_usd: float = 0.0
    run_remaining_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "estimated_cost_usd": round(self.estimated_cost_usd, 8),
            "reason": self.reason,
            "requires_approval": self.requires_approval,
            "daily_spend_usd": round(self.daily_spend_usd, 8),
            "daily_remaining_usd": round(self.daily_remaining_usd, 8),
            "run_remaining_usd": round(self.run_remaining_usd, 8),
        }


class BudgetGuard:
    """Enforces the configured spend limits."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._run_spend = 0.0

    # -- accounting --------------------------------------------------------- #

    def record(self, kind: str, model_id: str | None, cost_usd: float, *,
               prompt_tokens: int = 0, completion_tokens: int = 0,
               requests: int = 1, detail: str | None = None) -> None:
        """Record spend that has already happened."""
        from . import storage

        self._run_spend += max(0.0, float(cost_usd))
        storage.record_usage(
            kind,
            model_id,
            float(cost_usd),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            requests=requests,
            detail=detail,
        )

    def daily_spend(self) -> float:
        """Return the estimated USD spent since the start of the current day."""
        from . import storage

        try:
            return storage.spend_since(_start_of_day())
        except Exception:
            return 0.0

    @property
    def run_spend(self) -> float:
        """The estimated USD spent by the current run."""
        return self._run_spend

    def reset_run(self) -> None:
        """Start a new run's accounting."""
        self._run_spend = 0.0

    # -- the gate ----------------------------------------------------------- #

    def check(
        self,
        estimated_cost_usd: float,
        *,
        approved: bool = False,
        description: str = "",
    ) -> BudgetDecision:
        """Check whether a spend of this size is allowed right now."""
        estimate = max(0.0, float(estimated_cost_usd))
        daily = self.daily_spend()
        daily_remaining = max(0.0, self.settings.daily_budget_usd - daily)
        run_remaining = max(0.0, self.settings.max_cost_per_run_usd - self._run_spend)

        decision = BudgetDecision(
            allowed=True,
            estimated_cost_usd=estimate,
            daily_spend_usd=daily,
            daily_remaining_usd=daily_remaining,
            run_remaining_usd=run_remaining,
        )

        if estimate > run_remaining:
            decision.allowed = False
            decision.reason = (
                f"this call would cost about ${estimate:.6f}, which is more than the "
                f"${run_remaining:.6f} left in this run's ${self.settings.max_cost_per_run_usd:.2f} cap"
            )
            return decision

        if estimate > daily_remaining:
            decision.allowed = False
            decision.reason = (
                f"this call would cost about ${estimate:.6f}, which exceeds the "
                f"${daily_remaining:.6f} left of today's ${self.settings.daily_budget_usd:.2f} budget"
            )
            return decision

        if estimate >= self.settings.approval_threshold_usd and not approved:
            decision.requires_approval = True
            decision.allowed = False
            label = description or "this operation"
            decision.reason = (
                f"{label} is estimated to cost about ${estimate:.6f}, which is at or above the "
                f"${self.settings.approval_threshold_usd:.2f} approval threshold. "
                "Re-run with approval to proceed."
            )
            return decision

        return decision

    def enforce(
        self,
        estimated_cost_usd: float,
        *,
        approved: bool = False,
        description: str = "",
    ) -> BudgetDecision:
        """Check the budget and raise when the spend is not allowed."""
        decision = self.check(estimated_cost_usd, approved=approved, description=description)
        if decision.allowed:
            return decision
        if decision.requires_approval:
            raise ApprovalRequiredError(decision.reason, detail=decision.to_dict())
        raise BudgetExceededError(decision.reason, detail=decision.to_dict())

    def snapshot(self) -> dict[str, Any]:
        """Return the current budget state for display."""
        daily = self.daily_spend()
        return {
            "daily_budget_usd": self.settings.daily_budget_usd,
            "daily_spend_usd": round(daily, 8),
            "daily_remaining_usd": round(max(0.0, self.settings.daily_budget_usd - daily), 8),
            "max_cost_per_run_usd": self.settings.max_cost_per_run_usd,
            "run_spend_usd": round(self._run_spend, 8),
            "run_remaining_usd": round(
                max(0.0, self.settings.max_cost_per_run_usd - self._run_spend), 8
            ),
            "approval_threshold_usd": self.settings.approval_threshold_usd,
            "note": "figures are estimates at list price with the model's published discount applied",
        }


__all__ = ["BudgetDecision", "BudgetGuard"]
