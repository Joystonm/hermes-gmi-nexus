"""The eight-step adaptive pipeline.

``UNDERSTAND → PLAN → ROUTE → EXECUTE → EVALUATE → RECOVER → LEARN → REPORT``

Each step has one job, and the pipeline records what each one decided:

**UNDERSTAND**
    What kind of work is this? The rules classify the request; a model is
    consulted only when the rules are unsure, and the answer records which
    route it took.

**PLAN**
    What are the steps? Artefact templates give a deliverable its structure;
    otherwise the request's own clauses become the steps.

**ROUTE**
    Which model for which step? Models that cannot do the work are removed
    outright; the survivors are scored on benchmark results, observed
    reliability, latency, cost and the operator's preferences.

**EXECUTE**
    Run the steps, with the budget checked before every call.

**EVALUATE**
    Did each step produce what it was asked for? Deterministic checks always
    run; independent verification is optional and never asks the model that
    produced the answer to judge itself.

**RECOVER**
    When a step did not succeed, the recovery policy decides: retry the same
    model for a transient failure, switch to another candidate for a
    model-specific one, or stop.

**LEARN**
    Every outcome — the model's latency, its cost, whether it worked — is
    folded into the routing statistics. This is the *only* thing that adapts:
    NEXUS learns which model to pick, and it never modifies a model.

**REPORT**
    What happened, at what cost, with which models, and what was uncertain.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Sequence

from .arena import Arena
from .budget import BudgetGuard
from .classifier import Classification, classify
from .config import Settings
from .errors import ApprovalRequiredError, BudgetExceededError, NexusError
from .evaluator import Evaluator
from .executor import ExecutionContext, Executor
from .http import HttpClient
from .media_router import MediaRouter
from .models import (
    ModelInfo,
    Plan,
    RouteDecision,
    RunReport,
    Step,
    StepResult,
    TaskType,
)
from .planner import Planner
from .providers.gmi_llm import GmiLLMClient
from .providers.gmi_media import GmiMediaClient
from .registry import ModelRegistry
from .router import Router, RoutingSignals
from .modules.coding import DevAgent
from .modules.creative import CreativeEngine
from .modules.vision import VisionModule

#: The pipeline step names, in order, for the dashboard and the report.
PIPELINE_STEPS: tuple[str, ...] = (
    "understand",
    "plan",
    "route",
    "execute",
    "evaluate",
    "recover",
    "learn",
    "report",
)

#: How many extra candidates a failed step may be given.
MAX_RECOVERIES = 2

#: The most provider calls a single step may make, recovery attempts
#: included. The bound is per step, so a failing step can never spend the
#: whole run's budget on its own.
MAX_STEP_CALLS = 4


class Nexus:
    """The NEXUS engine."""

    def __init__(self, settings: Settings, ctx: Any | None = None) -> None:
        self.settings = settings
        self.ctx = ctx
        self.http = HttpClient(
            timeout=float(settings.request_timeout_s), max_retries=settings.max_retries
        )
        self.registry = ModelRegistry(settings, self.http)
        self.router = Router(settings, RoutingSignals.from_storage())
        self.budget = BudgetGuard(settings)
        self.llm = GmiLLMClient(settings.llm_base_url, self.http)
        self.media = GmiMediaClient(settings.media_base_url, self.http)
        self.media_router = MediaRouter(self.media.catalog)
        self.planner = Planner()
        self.executor = Executor(
            settings,
            self.registry,
            self.router,
            self.budget,
            self.llm,
            self.media,
            self.media_router,
        )
        self.evaluator = Evaluator()
        self.vision = VisionModule(
            settings, self.registry, self.router, self.llm, self.budget, ctx
        )
        self.dev = DevAgent(settings, self.registry, self.router, self.llm, self.budget, ctx)
        self.creative = CreativeEngine(
            settings, self.registry, self.media, self.media_router, self.budget
        )
        self.arena = Arena(settings, self.llm)

    # -- helpers ------------------------------------------------------------ #

    def reload_signals(self) -> None:
        """Re-read the routing statistics from storage."""
        self.router.reload_signals()

    def close(self) -> None:
        """Release the HTTP client."""
        self.http.close()

    # -- UNDERSTAND, PLAN, ROUTE -------------------------------------------- #

    def analyze(
        self,
        goal: str,
        *,
        media_mimes: Sequence[str] = (),
        strategy: str = "balanced",
    ) -> dict[str, Any]:
        """Understand and plan a goal without executing anything.

        Nothing is spent here: the catalogue is read (free), the plan is built
        locally, and the routing preview is a scoring pass over the live
        catalogue.
        """
        classification = classify(goal, media_mimes=tuple(media_mimes))
        plan = self.planner.plan(goal, classification)
        preview = self._preview_routes(plan, strategy)
        return {
            "goal": goal,
            "understanding": classification.to_dict(),
            "plan": plan.to_dict(),
            "routing_preview": preview,
            "catalogue": self.registry.summary(),
            "budget": self.budget.snapshot(),
        }

    def _preview_routes(self, plan: Plan, strategy: str) -> list[dict[str, Any]]:
        """Score the models for each planned step without calling anything.

        The preview comes from the executor, for the same reason the ROUTE
        phase does: a generation step is routed against the *generation*
        catalogue, so previewing it against the language-model catalogue
        would answer "no eligible model" for a step that then runs perfectly
        well. An analysis that disagreed with the run it previews would be
        worse than no analysis at all.
        """
        preview: list[dict[str, Any]] = []
        for step in plan.steps:
            decision = self.executor.preview_route(
                step, strategy=strategy, prompt=step.prompt
            )
            preview.append(
                {
                    "step_index": step.index,
                    "description": step.description,
                    "task_type": step.task_type.value,
                    "decision": decision.to_dict(),
                }
            )
        return preview

    @staticmethod
    def _required(step: Step) -> tuple[Any, ...]:
        from .models import TASK_CAPABILITIES

        return TASK_CAPABILITIES.get(step.task_type, ())

    # -- the full pipeline -------------------------------------------------- #

    def run(
        self,
        goal: str,
        *,
        media_mimes: Sequence[str] = (),
        strategy: str = "balanced",
        approved: bool = False,
        verify: bool = False,
        run_id: str | None = None,
    ) -> RunReport:
        """Run the eight-step pipeline over a goal."""
        from . import storage

        run_id = run_id or uuid.uuid4().hex
        self.budget.reset_run()
        started = time.perf_counter()

        # 1. UNDERSTAND
        classification = classify(goal, media_mimes=tuple(media_mimes))
        report = RunReport(run_id=run_id, goal=goal, task_type=classification.task_type)
        report.messages.append(
            f"Understood as {classification.task_type.value} "
            f"(confidence {classification.confidence:.2f}): {'; '.join(classification.evidence)}"
        )

        # 2. PLAN
        plan = self.planner.plan(goal, classification)
        report.plan = plan
        report.messages.append(
            f"Planned {len(plan.steps)} step(s): " + "; ".join(s.description for s in plan.steps)
        )

        storage.record_run(run_id, goal, classification.task_type.value)

        if not plan.steps:
            report.status = "failed"
            report.messages.append("The goal produced no executable steps.")
            storage.finish_run(run_id, report.status, 0, 0, [])
            return report

        # 3. ROUTE
        #
        # The route is previewed by the executor, which is the component that
        # will carry it out — and which knows that a generation step is routed
        # against the generation catalogue rather than the language-model
        # one. Routing everything through the language-model router here
        # would publish "no eligible model" for every image step, next to a
        # step that then ran perfectly well.
        decisions: dict[int, RouteDecision] = {}
        for step in plan.steps:
            decision = self.executor.preview_route(
                step,
                strategy=strategy,
                prompt=step.prompt,
                requested_model=classification.requested_model,
            )
            decisions[step.index] = decision
            decision.step_index = step.index
            report.routes.append(decision)
            primary = decision.primary
            if primary is not None:
                report.messages.append(
                    f"Step {step.index + 1} → {primary.model.id} "
                    f"(score {primary.score:.3f}, ~${primary.estimated_cost_usd:.6f})"
                )
            else:
                report.messages.append(
                    f"Step {step.index + 1} → no eligible model: "
                    + "; ".join(f"{k}: {v}" for k, v in decision.rejected.items())
                )

        # 4. EXECUTE, 5. EVALUATE, 6. RECOVER
        context = ExecutionContext(goal=goal)
        results: list[StepResult] = []
        evaluations: list[Any] = []
        for step in plan.steps:
            result = self.executor.execute(
                step,
                context,
                decisions.get(step.index),
                approved=approved,
                max_attempts=MAX_STEP_CALLS,
            )
            evaluation = self.evaluator.check(step, result)

            # 6. RECOVER — a step that did not produce a usable result is
            # retried on a *different* model before the run gives up on it.
            if not evaluation.passed:
                result = self._recover(step, result, context, decisions, approved=approved)
                evaluation = self.evaluator.check(step, result)

            result.validation = dict(result.validation)
            result.validation["evaluation"] = evaluation.to_dict()
            results.append(result)
            evaluations.append(evaluation)

            if result.status == "ok" and evaluation.passed:
                context.add_output(result.output)
                context.media_urls.extend(result.media_urls)
            else:
                reason = (result.error or {}).get("message") or "; ".join(
                    evaluation.failures
                ) or "the step did not produce a usable result"
                context.notes.append(f"Step {step.index + 1} did not succeed: {reason}")
            storage.record_step(run_id, result.to_dict())

        report.steps = results

        # 7. LEARN
        self._learn(results)

        # 8. REPORT
        report.total_cost_usd = sum(r.usage.cost_usd for r in results) + sum(
            r.estimated_cost_usd for r in results
        )
        report.total_latency_ms = int((time.perf_counter() - started) * 1000)
        report.models_used = [r.model_id for r in results if r.model_id]

        succeeded = [
            result
            for result, evaluation in zip(results, evaluations)
            if result.status == "ok" and evaluation.passed
        ]
        if len(succeeded) == len(results):
            report.status = "completed"
        elif succeeded:
            report.status = "partial"
            report.messages.append(
                f"{len(results) - len(succeeded)} of {len(results)} step(s) did not succeed."
            )
        else:
            report.status = "failed"
            report.messages.append("No step of the plan succeeded.")

        storage.finish_run(
            run_id,
            report.status,
            report.total_cost_usd,
            report.total_latency_ms,
            report.models_used,
            report.to_dict(),
        )
        return report

    def _recover(
        self,
        step: Step,
        result: StepResult,
        context: ExecutionContext,
        decisions: dict[int, RouteDecision],
        *,
        approved: bool,
    ) -> StepResult:
        """Try other models for a step that did not pass its checks.

        The retry is handed the candidates that have *not* been tried yet, so
        a step that failed on one model genuinely moves to another rather
        than calling the same model again. The step's call budget is shared
        between the first attempt and the recovery attempts.
        """
        tried = set(result.attempted)
        decision = decisions.get(step.index)
        if decision is None:
            return result

        calls_used = max(result.attempts, len(result.attempted))
        for _ in range(MAX_RECOVERIES):
            if calls_used >= MAX_STEP_CALLS:
                result.fallbacks = list(result.fallbacks)
                result.fallbacks.append(
                    f"recovery: the step's budget of {MAX_STEP_CALLS} call(s) was reached"
                )
                break
            alternatives = [c for c in decision.candidates if c.model.id not in tried]
            if not alternatives:
                break
            candidate = alternatives[0]
            tried.add(candidate.model.id)
            retry_decision = RouteDecision(
                task_type=decision.task_type,
                required_capabilities=decision.required_capabilities,
                candidates=[candidate],
                rejected=decision.rejected,
                strategy=decision.strategy,
                # The kind travels with the decision: a generation step that
                # is retried must be retried against the generation
                # catalogue, with the candidate this recovery chose.
                kind=decision.kind,
            )
            retry = self.executor.execute(
                step, context, retry_decision, approved=approved, max_attempts=1
            )
            calls_used += max(retry.attempts, len(retry.attempted))
            retry.fallbacks = list(result.fallbacks) + list(retry.fallbacks)
            retry.fallbacks.append(
                f"recovery: retried with {retry.model_id or candidate.model.id}"
            )
            if retry.status == "ok":
                retry.attempts = calls_used
                retry.attempted = list(result.attempted) + list(retry.attempted)
                return retry
            result = retry

        result.fallbacks = list(result.fallbacks)
        result.fallbacks.append(
            f"recovery: {len(tried)} model(s) tried, none produced a usable result"
        )
        return result

    def _learn(self, results: Sequence[StepResult]) -> None:
        """Fold the measured outcomes into the routing statistics."""
        from . import storage

        if not self.settings.enable_learning:
            return
        for result in results:
            if not result.model_id:
                continue
            try:
                storage.update_model_stats(
                    result.model_id,
                    result.task_type.value,
                    success=result.status == "ok",
                    latency_ms=result.latency_ms,
                    cost_usd=result.usage.cost_usd or result.estimated_cost_usd,
                )
            except Exception:
                continue
        self.reload_signals()

    # -- reporting ---------------------------------------------------------- #

    def status(self) -> dict[str, Any]:
        """Return the engine's current state."""
        from . import storage

        return {
            "catalogue": self.registry.summary(),
            "budget": self.budget.snapshot(),
            "routing": self.router.to_dict(),
            "usage": storage.usage_summary(days=self.settings.retention_days),
            "recent_runs": storage.recent_runs(limit=10),
            "benchmarks": storage.list_benchmarks(limit=10),
            "model_stats": storage.model_stats(),
            "capability_probes": storage.capability_probes(),
        }


__all__ = ["MAX_RECOVERIES", "MAX_STEP_CALLS", "PIPELINE_STEPS", "Nexus"]
