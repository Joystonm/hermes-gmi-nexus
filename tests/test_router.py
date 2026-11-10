"""Model selection, spend limits, evaluation and recovery."""

from __future__ import annotations

import pytest

from nexus.budget import BudgetGuard
from nexus.config import Settings
from nexus.errors import (
    ApprovalRequiredError,
    AuthenticationError,
    BadRequestError,
    BudgetExceededError,
    ModelUnavailableError,
    RateLimitError,
    TransientError,
)
from nexus.evaluator import Evaluator
from nexus.models import Capability, ModelInfo, Pricing, Step, StepResult, TaskType
from nexus.recovery import ACTION_RETRY, ACTION_STOP, ACTION_SWITCH, classify_error, decide
from nexus.router import Router, RoutingSignals


def _model(model_id: str, **kwargs) -> ModelInfo:
    defaults = dict(
        id=model_id,
        name=model_id,
        owned_by=model_id.split("/")[0],
        context_length=8000,
        pricing=Pricing(prompt=0.000001, completion=0.000002),
        capabilities=(Capability.TEXT,),
    )
    defaults.update(kwargs)
    return ModelInfo(**defaults)


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #


def test_a_model_without_the_required_capability_is_rejected_with_a_reason():
    router = Router(Settings())
    decision = router.route(
        TaskType.IMAGE_UNDERSTANDING,
        (Capability.TEXT, Capability.VISION),
        [_model("acme/text-only")],
    )
    assert decision.candidates == []
    assert "vision" in decision.rejected["acme/text-only"]


def test_a_blocked_model_is_never_routed_to():
    router = Router(Settings(blocked_models=["acme/never"]))
    decision = router.route(TaskType.GENERAL_REASONING, (Capability.TEXT,), [_model("acme/never")])
    assert decision.candidates == []
    assert "blocked" in decision.rejected["acme/never"]


def test_a_model_that_is_not_ready_is_rejected():
    router = Router(Settings())
    decision = router.route(
        TaskType.GENERAL_REASONING, (Capability.TEXT,), [_model("acme/warming", is_ready=False)]
    )
    assert decision.candidates == []
    assert "not ready" in decision.rejected["acme/warming"]


def test_the_cheaper_model_wins_under_the_cheap_strategy():
    cheap = _model("acme/cheap", pricing=Pricing(prompt=0.0000001, completion=0.0000001))
    dear = _model("acme/dear", pricing=Pricing(prompt=0.0001, completion=0.0001))
    router = Router(Settings())
    decision = router.route(
        TaskType.GENERAL_REASONING, (Capability.TEXT,), [dear, cheap], strategy="cheap"
    )
    assert decision.primary.model.id == "acme/cheap"


def test_a_preferred_model_is_promoted():
    router = Router(Settings(preferred_models=["acme/preferred"]))
    decision = router.route(
        TaskType.GENERAL_REASONING,
        (Capability.TEXT,),
        [_model("acme/other"), _model("acme/preferred")],
    )
    assert decision.primary.model.id == "acme/preferred"
    assert any("preferred" in reason for reason in decision.primary.reasons)


def test_a_named_model_wins_even_when_it_scores_lower():
    router = Router(Settings())
    decision = router.route(
        TaskType.GENERAL_REASONING,
        (Capability.TEXT,),
        [_model("acme/other"), _model("acme/named")],
        requested_model="acme/named",
    )
    assert decision.primary.model.id == "acme/named"


def test_a_named_model_matches_a_provider_qualified_catalogue_id():
    router = Router(Settings())
    decision = router.route(
        TaskType.GENERAL_REASONING,
        (Capability.TEXT,),
        [_model("openai/gpt-4o-mini")],
        requested_model="gpt-4o-mini",
    )
    assert decision.primary.model.id == "openai/gpt-4o-mini"


def test_a_named_model_that_cannot_do_the_task_is_reported_not_used():
    router = Router(Settings())
    decision = router.route(
        TaskType.IMAGE_UNDERSTANDING,
        (Capability.TEXT, Capability.VISION),
        [_model("acme/text-only")],
        requested_model="acme/text-only",
    )
    assert decision.primary is None
    assert "acme/text-only" in decision.rejected


def test_candidates_always_say_why_they_scored_what_they_scored():
    router = Router(Settings())
    decision = router.route(
        TaskType.GENERAL_REASONING, (Capability.TEXT,), [_model("acme/one")]
    )
    reasons = decision.primary.reasons
    assert any(r.startswith("quality:") for r in reasons)
    assert any(r.startswith("cost:") for r in reasons)


def test_measured_history_outweighs_the_prior():
    signals = RoutingSignals(
        benchmark={},
        success={"acme/proven": 1.0},
        samples={"acme/proven": 10},
        latency={"acme/proven": 500.0},
        cost={"acme/proven": 0.0001},
    )
    router = Router(Settings(), signals)
    candidate = router.score_candidate(_model("acme/proven"), (Capability.TEXT,))
    assert candidate.historical_success == 1.0
    assert any("over 10 run" in reason for reason in candidate.reasons)


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #


def test_a_cost_above_the_approval_threshold_needs_approval():
    guard = BudgetGuard(Settings(approval_threshold_usd=0.25, max_cost_per_run_usd=10.0))
    with pytest.raises(ApprovalRequiredError):
        guard.enforce(0.5, approved=False)
    guard.enforce(0.5, approved=True)


def test_a_cost_above_the_run_ceiling_is_refused_even_when_approved():
    guard = BudgetGuard(Settings(max_cost_per_run_usd=0.10, approval_threshold_usd=0.01))
    with pytest.raises(BudgetExceededError):
        guard.enforce(0.5, approved=True)


def test_the_daily_ceiling_carries_across_runs():
    guard = BudgetGuard(Settings(daily_budget_usd=0.20, max_cost_per_run_usd=10.0))
    guard.record("llm", "acme/one", 0.15)
    guard.reset_run()
    with pytest.raises(BudgetExceededError):
        guard.enforce(0.10, approved=True)


def test_the_budget_snapshot_is_labelled_as_an_estimate():
    guard = BudgetGuard(Settings())
    snapshot = guard.snapshot()
    assert "estimate" in json_text(snapshot)


def json_text(payload) -> str:
    import json

    return json.dumps(payload)


# --------------------------------------------------------------------------- #
# Evaluator
# --------------------------------------------------------------------------- #


def _step(task_type: TaskType = TaskType.GENERAL_REASONING) -> Step:
    return Step(index=0, description="do the thing", task_type=task_type, prompt="do the thing")


def test_an_empty_output_fails_the_evaluation():
    result = StepResult(step_index=0, description="d", task_type=TaskType.GENERAL_REASONING)
    evaluation = Evaluator().check(_step(), result)
    assert not evaluation.passed
    assert evaluation.failures


def test_a_substantive_output_passes():
    result = StepResult(
        step_index=0,
        description="d",
        task_type=TaskType.GENERAL_REASONING,
        status="ok",
        output="A complete, specific answer that is long enough to be useful.",
    )
    assert Evaluator().check(_step(), result).passed


def test_a_code_step_without_code_fails():
    step = _step(TaskType.CODING)
    result = StepResult(
        step_index=0,
        description="d",
        task_type=TaskType.CODING,
        status="ok",
        output="Sure! Here is what I would do.",
    )
    assert not Evaluator().check(step, result).passed


def test_a_code_step_with_code_passes():
    step = _step(TaskType.CODING)
    result = StepResult(
        step_index=0,
        description="d",
        task_type=TaskType.CODING,
        status="ok",
        output="Here you go:\n```python\nprint('hello')\n```",
    )
    assert Evaluator().check(step, result).passed


def test_a_placeholder_is_not_a_result():
    result = StepResult(
        step_index=0,
        description="d",
        task_type=TaskType.GENERAL_REASONING,
        status="ok",
        output="TODO: write the rest of this section later.",
    )
    assert not Evaluator().check(_step(), result).passed


def test_a_media_step_without_media_fails():
    step = _step(TaskType.IMAGE_GENERATION)
    result = StepResult(
        step_index=0, description="d", task_type=TaskType.IMAGE_GENERATION, status="ok", output="ok"
    )
    assert not Evaluator().check(step, result).passed


# --------------------------------------------------------------------------- #
# Recovery
# --------------------------------------------------------------------------- #


def test_a_rate_limit_is_retried():
    action = decide(RateLimitError("slow down"), attempt=1, max_attempts=3)
    assert action.action == ACTION_RETRY


def test_a_bad_request_is_never_retried():
    action = decide(BadRequestError("malformed"), attempt=1, max_attempts=3)
    assert action.action == ACTION_STOP


def test_a_fatal_error_stops_even_on_the_first_attempt():
    action = decide(BudgetExceededError("too expensive"), attempt=1, max_attempts=3)
    assert action.action == ACTION_STOP


def test_a_model_error_switches_to_another_model():
    action = decide(
        ModelUnavailableError("retired"), attempt=1, max_attempts=3, candidates_remaining=2
    )
    assert action.action == ACTION_SWITCH


def test_the_last_attempt_stops():
    action = decide(TransientError("flaky"), attempt=3, max_attempts=3)
    assert action.action == ACTION_STOP


def test_errors_are_classified_by_what_can_be_done_about_them():
    assert classify_error(RateLimitError("x")) == "transient"
    assert classify_error(BadRequestError("x")) == "bad_request"
    assert classify_error(ModelUnavailableError("x")) == "model_unavailable"
    assert classify_error(BudgetExceededError("x")) == "budget"
    assert classify_error(AuthenticationError("x")) == "auth"
