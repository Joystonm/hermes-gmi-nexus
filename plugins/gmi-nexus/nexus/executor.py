"""Executing one planned step.

The executor owns the attempt loop for a step: it asks the router for an
ordered list of models, tries them in turn, and gives up only when the
recovery policy says to. Every attempt is recorded — which model ran, how
long it took, what it cost — so a report can state what happened rather than
imply it.

Two rules the executor never breaks:

* A failed call is never reported as a success. If the provider refused the
  request, the step is ``failed`` and the error is carried into the report.
* A call is never made without a budget check first. The estimate comes from
  the model's published price, so it is an estimate at list price and is
  labelled as one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from .budget import BudgetGuard
from .config import Settings
from .errors import NexusError
from .http import HttpClient
from .models import (
    Capability,
    MediaModelInfo,
    Modality,
    RouteCandidate,
    RouteDecision,
    Step,
    StepResult,
    TaskType,
    Usage,
)
from .providers.gmi_llm import GmiLLMClient
from .providers.gmi_media import GmiMediaClient
from .registry import catalogue_error_suffix

#: The system prompt NEXUS gives its own steps.
SYSTEM_PROMPT = (
    "You are a component of GMI NEXUS, an adaptive model-orchestration system "
    "running inside the Hermes Agent. Answer the task directly and concretely. "
    "Prefer specifics over generalities. If the task asks for code, return "
    "complete runnable code with no placeholders. If the task asks for a "
    "critique, name each problem and say how to fix it."
)

#: How much of the previous steps' output is carried into the next step.
MAX_CONTEXT_CHARS = 12000

#: The largest single model reply NEXUS will accept into the run context.
MAX_OUTPUT_CHARS = 20000

#: The task types whose answer can be a whole file rather than a sentence.
#: A reply that runs out of budget is cut off mid-JSON or mid-sentence, so
#: these steps ask for the configured ``code_max_tokens`` instead.
_CODE_BEARING_TASKS = frozenset(
    {TaskType.CODING, TaskType.DEBUGGING, TaskType.CODE_REVIEW}
)


@dataclass(slots=True)
class ExecutionContext:
    """What a step knows about the run so far."""

    goal: str = ""
    outputs: list[str] = field(default_factory=list)
    media_urls: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add_output(self, text: str) -> None:
        if text:
            self.outputs.append(text[:MAX_OUTPUT_CHARS])

    def context_text(self) -> str:
        """Return the previous steps' output, bounded in size."""
        if not self.outputs:
            return ""
        joined = "\n\n".join(self.outputs)
        if len(joined) <= MAX_CONTEXT_CHARS:
            return joined
        return "...[earlier output trimmed]...\n" + joined[-MAX_CONTEXT_CHARS:]


class Executor:
    """Runs planned steps against the GMI Cloud models."""

    def __init__(
        self,
        settings: Settings,
        registry: Any,
        router: Any,
        budget: BudgetGuard,
        llm_client: GmiLLMClient,
        media_client: GmiMediaClient,
        media_router: Any,
        *,
        max_attempts: int = 3,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.router = router
        self.budget = budget
        self.llm_client = llm_client
        self.media_client = media_client
        self.media_router = media_router
        self.max_attempts = max(1, int(max_attempts))

    # -- routing ------------------------------------------------------------ #

    @staticmethod
    def _media_modality(step: Step) -> Modality | None:
        """Return the modality a step generates, or ``None`` for a text step."""
        if step.task_type in (TaskType.IMAGE_GENERATION, TaskType.IMAGE_EDITING):
            return Modality.IMAGE
        if step.task_type is TaskType.VIDEO:
            return Modality.VIDEO
        if step.task_type is TaskType.AUDIO:
            return Modality.AUDIO
        return None

    @staticmethod
    def media_modality(step: Step) -> Modality | None:
        """Return the modality a step generates, or ``None`` for a text step.

        The public face of :meth:`_media_modality`, so the approval hook can
        price a planned step the same way the executor will run it.
        """
        return Executor._media_modality(step)

    def token_budget(self, step: Step) -> int:
        """The token budget for this step's reply."""
        return self._token_budget(step)

    @staticmethod
    def required_capabilities(step: Step) -> tuple[Capability, ...]:
        """The capabilities a step needs from a model."""
        return Executor._required(step)

    def estimate_step_cost(
        self, step: Step, model: Any, *, completion_tokens: int | None = None
    ) -> float:
        """Estimate what running this step on this model would cost.

        The same estimate the executor's own budget check makes, so the
        figure a person is shown before approving a call is the figure the
        guard will check afterwards.
        """
        messages = self._build_messages(step, ExecutionContext(goal=step.description))
        budget = self._token_budget(step) if completion_tokens is None else int(completion_tokens)
        return self._estimate_llm_cost(model, messages, budget)

    def preview_route(
        self,
        step: Step,
        *,
        strategy: str = "balanced",
        prompt: str = "",
        requested_model: str | None = None,
    ) -> RouteDecision:
        """Describe how this step would be routed, without calling anything.

        The answer comes from the same router the executor will use, so the
        ROUTE phase of a report and the EXECUTE phase of the same report
        cannot disagree. A generation step is routed against the *generation*
        catalogue — asking the language-model router for a model that can
        produce images would always answer "no eligible model", and publish
        that answer next to a step that then ran perfectly well.
        """
        modality = self._media_modality(step)
        if modality is not None:
            return self._preview_media_route(step, modality)
        return self.router.route(
            step.task_type,
            self._required(step),
            self.registry.list_models(),
            strategy=strategy,
            prompt=prompt or step.prompt,
            requested_model=requested_model,
        )

    def _preview_media_route(self, step: Step, modality: Modality) -> RouteDecision:
        """Route a generation step against the request-queue catalogue."""
        options = dict(step.metadata.get("options") or {})
        prompt = step.prompt or step.description
        available = self.registry.list_media(modality)
        candidate, considered = self.media_router.select(
            [info.id for info in available],
            modality,
            prompt,
            options,
            preferred=self.settings.preferred_models,
        )
        by_id: dict[str, MediaModelInfo] = {info.id: info for info in available}

        candidates: list[RouteCandidate] = []
        rejected: dict[str, str] = {}
        for item in considered:
            if item.rejected:
                rejected[item.model_id] = item.rejected
                continue
            info = by_id.get(item.model_id)
            if info is None:
                continue
            candidates.append(
                RouteCandidate(
                    model=info,
                    score=item.score,
                    reasons=list(item.reasons),
                    estimated_cost_usd=item.estimated_cost_usd,
                )
            )
        candidates.sort(key=lambda c: c.score, reverse=True)

        return RouteDecision(
            task_type=step.task_type,
            required_capabilities=self._required(step),
            candidates=candidates,
            rejected=rejected,
            strategy="balanced",
            kind="media",
        )

    # -- the attempt loop --------------------------------------------------- #

    def execute(
        self,
        step: Step,
        context: ExecutionContext,
        decision: RouteDecision | None = None,
        *,
        approved: bool = False,
        max_attempts: int | None = None,
    ) -> StepResult:
        """Run one step and return its result."""
        started = time.perf_counter()
        result = StepResult(
            step_index=step.index,
            description=step.description,
            task_type=step.task_type,
            started_at=started,
        )

        try:
            if step.task_type in (TaskType.IMAGE_GENERATION, TaskType.IMAGE_EDITING):
                self._execute_media(step, context, result, approved=approved, decision=decision)
            elif step.task_type is TaskType.VIDEO:
                self._execute_media(
                    step, context, result, approved=approved, modality=Modality.VIDEO, decision=decision
                )
            elif step.task_type is TaskType.AUDIO:
                self._execute_media(
                    step, context, result, approved=approved, modality=Modality.AUDIO, decision=decision
                )
            else:
                self._execute_llm(
                    step,
                    context,
                    result,
                    decision=decision,
                    approved=approved,
                    max_attempts=max_attempts,
                )
        except NexusError as exc:
            result.status = "failed"
            result.error = exc.to_dict()
            result.output = str(exc)
        except Exception as exc:  # pragma: no cover - defensive
            result.status = "failed"
            result.error = {"kind": "UnexpectedError", "message": str(exc)}
            result.output = ""

        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    # -- language models ---------------------------------------------------- #

    def _execute_llm(
        self,
        step: Step,
        context: ExecutionContext,
        result: StepResult,
        *,
        decision: RouteDecision | None,
        approved: bool,
        max_attempts: int | None = None,
    ) -> None:
        """Try the routed models in order until one succeeds."""
        if decision is None or not decision.candidates:
            decision = self.router.route(
                step.task_type,
                self._required(step),
                self.registry.list_models(),
                strategy="balanced",
                prompt=step.prompt,
            )
        if not decision.candidates:
            result.status = "failed"
            result.error = {
                "kind": "NoEligibleModel",
                "message": (
                    f"no model in the live catalogue satisfies {step.task_type.value}: "
                    + ", ".join(sorted(c.value for c in self._required(step)))
                    + catalogue_error_suffix(self.registry)
                ),
            }
            return

        messages = self._build_messages(step, context)
        attempts = 0
        last_error: dict[str, Any] | None = None
        attempt_cap = self.max_attempts if max_attempts is None else max(1, int(max_attempts))
        token_budget = self._token_budget(step)

        for candidate in decision.candidates:
            if attempts >= attempt_cap:
                break
            attempts += 1
            model = candidate.model
            result.attempted.append(model.id)
            estimate = self._estimate_llm_cost(model, messages, token_budget)
            try:
                self.budget.enforce(
                    estimate,
                    approved=approved,
                    description=f"a call to {model.id}",
                    price_known=model.price_known,
                )
            except NexusError as exc:
                last_error = exc.to_dict()
                result.attempts = attempts
                result.fallbacks.append(f"{model.id}: {exc}")
                break

            try:
                response = self.llm_client.chat(
                    model.id,
                    messages,
                    model_info=model,
                    max_tokens=token_budget,
                )
            except NexusError as exc:
                last_error = exc.to_dict()
                result.fallbacks.append(f"{model.id}: {exc}")
                continue

            # The step's cost, priced from the provider's own reported usage.
            # Without this the run's total would leave out every language
            # model call and under-report what was spent. It is recorded
            # *before* the answer is looked at, because a call that came back
            # empty was still billed: the provider spent the tokens whether
            # or not the model said anything with them.
            self.budget.record(
                "llm",
                model.id,
                response.usage.cost_usd,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                requests=response.usage.requests,
                detail=step.description[:200],
            )
            result.estimated_cost_usd += response.usage.cost_usd

            # A call that came back with no text is not an answer. A
            # reasoning model can spend its whole token budget thinking and
            # be cut off before it says anything, and the provider then sends
            # ``content: null``. Recording that as a successful step would
            # claim work that never happened, so it counts as a failed
            # attempt and the next routed model gets its turn.
            if not (response.text or "").strip():
                last_error = {
                    "kind": "EmptyResponse",
                    "message": (
                        f"{model.id} returned no text"
                        + (
                            f"; it was cut off at the token limit "
                            f"({token_budget} tokens)"
                            if response.finish_reason == "length"
                            else ""
                        )
                    ),
                }
                result.fallbacks.append(f"{model.id}: {last_error['message']}")
                result.attempts = attempts
                result.usage = response.usage
                continue

            result.status = "ok"
            result.model_id = model.id
            result.output = response.text
            result.attempts = attempts
            result.usage = response.usage
            if response.finish_reason == "length":
                # The answer was cut off by the token limit. It is still the
                # model's answer, but it is not the whole one, and the report
                # has to say so rather than presenting half an answer as a
                # finished one.
                result.fallbacks.append(
                    f"the reply was cut off at the model's token limit "
                    f"({token_budget} tokens); the answer above is incomplete"
                )
            result.latency_ms = response.latency_ms or result.latency_ms
            return

        result.status = "failed"
        result.attempts = attempts
        result.error = last_error or {
            "kind": "NoEligibleModel",
            "message": "every candidate model failed",
        }

    def _build_messages(self, step: Step, context: ExecutionContext) -> list[dict[str, Any]]:
        """Build the message list for one step."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]
        previous = context.context_text()
        if previous:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Context produced by the earlier steps of this run:\n\n"
                        f"{previous}\n\n---\n\nContinue with this step: {step.description}"
                    ),
                }
            )
        messages.append({"role": "user", "content": step.prompt or step.description})
        return messages

    def _token_budget(self, step: Step) -> int:
        """The token budget for this step's reply.

        A step that produces code can produce a whole file, and a reply that
        runs out of budget is cut off mid-sentence. Code-bearing steps
        therefore get the configured ``code_max_tokens``; a step that answers
        in prose gets the configured ``max_tokens``.

        The prose budget is not as small as it looks. A reasoning model
        spends part of it thinking before it answers, and a live run showed a
        model using all 2048 of its completion tokens on reasoning, being
        cut off mid-thought and returning no text at all — a step that
        looked successful and was not. The budget now leaves room for the
        reasoning and the answer.
        """
        if step.task_type in _CODE_BEARING_TASKS:
            return max(1024, int(self.settings.code_max_tokens))
        return int(self.settings.max_tokens)

    def _estimate_llm_cost(
        self,
        model: Any,
        messages: Sequence[dict[str, Any]],
        completion_tokens: int = 1024,
    ) -> float:
        """Estimate what one call will cost, at list price.

        The estimate prices the budget the reply is allowed, so the gate is
        given an upper bound on the call rather than a guess at it.
        """
        from .router import estimate_tokens

        prompt_chars = 0
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                prompt_chars += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        prompt_chars += len(str(part.get("text") or ""))
        return model.pricing.cost(
            prompt_tokens=estimate_tokens(" " * prompt_chars),
            completion_tokens=completion_tokens,
            requests=1,
        )

    # -- media models ------------------------------------------------------- #

    def _execute_media(
        self,
        step: Step,
        context: ExecutionContext,
        result: StepResult,
        *,
        approved: bool,
        modality: Modality = Modality.IMAGE,
        decision: RouteDecision | None = None,
    ) -> None:
        """Generate an asset with the request-queue models."""
        options = dict(step.metadata.get("options") or {})
        prompt = step.prompt or step.description
        if step.metadata.get("image"):
            options.setdefault("image", step.metadata["image"])
        if step.metadata.get("mask"):
            options.setdefault("mask", step.metadata["mask"])

        media_models = [info.id for info in self.registry.list_media(modality)]
        candidate, considered = self.media_router.select(
            media_models,
            modality,
            prompt,
            options,
            preferred=self.settings.preferred_models,
        )
        # A route that was already published is honoured, so the model named
        # in the report is the model that runs.
        if decision is not None and decision.kind == "media" and decision.candidates:
            chosen = decision.candidates[0].model.id
            match = next((c for c in considered if c.model_id == chosen), None)
            if match is not None:
                candidate = match
        if candidate is None or candidate.spec is None:
            reasons = "; ".join(
                f"{c.model_id}: {c.rejected}" for c in considered if c.rejected
            ) or "no candidate model was available"
            result.status = "failed"
            result.error = {"kind": "NoEligibleModel", "message": reasons}
            result.validation = {"candidates": [c.to_dict() for c in considered]}
            return

        estimate = candidate.estimated_cost_usd
        try:
            self.budget.enforce(
                estimate,
                approved=approved,
                description=f"a {modality.value} generation with {candidate.model_id}",
                price_known=candidate.spec.has_published_price(),
            )
        except NexusError as exc:
            result.status = "failed"
            result.error = exc.to_dict()
            result.output = str(exc)
            result.model_id = candidate.model_id
            return

        generation = self.media_client.generate(
            candidate.model_id,
            prompt,
            options,
            modality=modality,
        )

        result.model_id = candidate.model_id
        result.attempts = 1
        result.attempted.append(candidate.model_id)
        result.estimated_cost_usd = estimate
        result.media_urls = list(generation.media_urls)
        result.validation = {
            "candidates": [c.to_dict() for c in considered],
            "dropped_options": list(generation.dropped_options),
            "estimated_cost_usd": round(generation.estimated_cost_usd, 8),
        }

        if not generation.ok:
            # The provider did not produce media: this is a failure, not a
            # success with an empty result.
            result.status = "failed"
            result.error = {
                "kind": "GenerationFailed",
                "message": generation.error or f"generation ended with status {generation.status}",
            }
            result.output = ""
            return

        result.status = "ok"
        result.output = (
            f"Generated {len(generation.media_urls)} asset(s) with {candidate.model_id}."
            if len(generation.media_urls) != 1
            else f"Generated 1 asset with {candidate.model_id}."
        )
        self.budget.record(
            "media",
            candidate.model_id,
            generation.estimated_cost_usd,
            requests=1,
            detail=step.description[:200],
        )

    # -- helpers ------------------------------------------------------------ #

    @staticmethod
    def _required(step: Step) -> tuple[Capability, ...]:
        from .models import TASK_CAPABILITIES

        return TASK_CAPABILITIES.get(step.task_type, (Capability.TEXT,))


__all__ = ["Executor", "ExecutionContext", "MAX_CONTEXT_CHARS", "SYSTEM_PROMPT"]
