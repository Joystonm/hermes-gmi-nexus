"""Core value objects for NEXUS.

Everything the engine produces is one of these dataclasses, so the dashboard,
the tool handlers and the tests all speak the same vocabulary.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

# --------------------------------------------------------------------------- #
# Capabilities
# --------------------------------------------------------------------------- #


class Capability(str, Enum):
    """A capability a model can be asked for.

    Routing treats the *essential* ones as hard filters: a model that cannot
    read an image is never selected for an image-understanding task. The
    *specialisations* are preferences — a general model writes worse code
    than a code model, but it writes code.
    """

    TEXT = "text"
    VISION = "vision"
    TOOLS = "tools"
    REASONING = "reasoning"
    LONG_CONTEXT = "long_context"
    CODE = "code"
    AUDIO = "audio"
    VIDEO = "video"
    IMAGE_OUT = "image_out"


# --------------------------------------------------------------------------- #
# Probe evidence
# --------------------------------------------------------------------------- #

#: The version of the probe protocol. A probe is only as good as the
#: question it asks, so the protocol is versioned: when the question
#: changes, the verdicts recorded under the old version are void and the
#: affected models are probed again.
#:
#: Version 1 asked a model to name the colours of two squares, which was
#: easy to pass by accident — "red" and "blue" are the two most guessable
#: colour words there are. Version 2 shows a randomly drawn three-digit
#: number and asks the model to read it back.
PROBE_PROTOCOL_VERSION = "v2"

#: The prefix of evidence that records a *successful* probe. A catalogue
#: entry may claim a capability; only a probe decides whether it is real.
PROBE_VERIFIED_PREFIX = f"probe:verified:{PROBE_PROTOCOL_VERSION}"

#: The prefix of evidence that records a *failed* probe. A model that has
#: failed one must not be offered the capability again.
PROBE_FAILED_PREFIX = f"probe:failed:{PROBE_PROTOCOL_VERSION}"


def probe_verdict(evidence: str) -> bool | None:
    """Return the verdict carried by a probe's evidence.

    ``True`` when the probe verified the capability, ``False`` when it
    refuted it, and ``None`` when the evidence carries no verdict — either
    because the capability came from the catalogue's own metadata rather
    than from a probe, or because the evidence was recorded under an older
    probe protocol, which no longer counts.
    """
    if evidence.startswith(PROBE_VERIFIED_PREFIX):
        return True
    if evidence.startswith(PROBE_FAILED_PREFIX):
        return False
    return None


class Modality(str, Enum):
    """Media modality handled by the request-queue API."""

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"


# --------------------------------------------------------------------------- #
# Task classification
# --------------------------------------------------------------------------- #


class TaskType(str, Enum):
    """The kind of work NEXUS was asked to do."""

    GENERAL_REASONING = "general_reasoning"
    COMPLEX_REASONING = "complex_reasoning"
    CODING = "coding"
    DEBUGGING = "debugging"
    CODE_REVIEW = "code_review"
    IMAGE_UNDERSTANDING = "image_understanding"
    DOCUMENT_ANALYSIS = "document_analysis"
    IMAGE_GENERATION = "image_generation"
    IMAGE_EDITING = "image_editing"
    AUDIO = "audio"
    VIDEO = "video"
    MULTIMODAL = "multimodal"
    PLANNING = "planning"


#: Capabilities each task type requires.
TASK_CAPABILITIES: dict[TaskType, tuple[Capability, ...]] = {
    TaskType.GENERAL_REASONING: (Capability.TEXT,),
    TaskType.COMPLEX_REASONING: (Capability.TEXT, Capability.REASONING),
    TaskType.CODING: (Capability.TEXT, Capability.CODE),
    TaskType.DEBUGGING: (Capability.TEXT, Capability.CODE, Capability.REASONING),
    TaskType.CODE_REVIEW: (Capability.TEXT, Capability.CODE),
    TaskType.IMAGE_UNDERSTANDING: (Capability.TEXT, Capability.VISION),
    TaskType.DOCUMENT_ANALYSIS: (Capability.TEXT, Capability.VISION),
    TaskType.IMAGE_GENERATION: (Capability.IMAGE_OUT,),
    TaskType.IMAGE_EDITING: (Capability.IMAGE_OUT,),
    TaskType.AUDIO: (Capability.AUDIO,),
    TaskType.VIDEO: (Capability.VIDEO,),
    TaskType.MULTIMODAL: (Capability.TEXT, Capability.VISION),
    TaskType.PLANNING: (Capability.TEXT, Capability.REASONING),
}

#: Capabilities a task genuinely cannot be done without. A model that lacks
#: one of these is rejected outright: sending an image to a text-only model
#: produces a confident wrong answer rather than an error, so the safest
#: thing is to refuse the route and say why.
ESSENTIAL_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.TEXT,
        Capability.VISION,
        Capability.IMAGE_OUT,
        Capability.AUDIO,
        Capability.VIDEO,
    }
)

#: Capabilities that describe a *specialisation* rather than a hard
#: requirement. Any text model can attempt the work, so a model that lacks
#: the specialisation is not rejected — it is ranked below the specialists
#: and the reason is recorded. This matters because the live catalogue
#: publishes no capability metadata for most models: without this, a coding
#: task would be unroutable on any catalogue that happens to publish no
#: model whose id mentions "code".
SPECIALISATION_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.CODE,
        Capability.REASONING,
        Capability.TOOLS,
        Capability.LONG_CONTEXT,
    }
)

#: Human-readable labels used in reports and in the dashboard.
TASK_LABELS: dict[TaskType, str] = {
    TaskType.GENERAL_REASONING: "General reasoning",
    TaskType.COMPLEX_REASONING: "Complex reasoning",
    TaskType.CODING: "Coding",
    TaskType.DEBUGGING: "Debugging",
    TaskType.CODE_REVIEW: "Code review",
    TaskType.IMAGE_UNDERSTANDING: "Image understanding",
    TaskType.DOCUMENT_ANALYSIS: "Document analysis",
    TaskType.IMAGE_GENERATION: "Image generation",
    TaskType.IMAGE_EDITING: "Image editing",
    TaskType.AUDIO: "Audio",
    TaskType.VIDEO: "Video",
    TaskType.MULTIMODAL: "Multimodal",
    TaskType.PLANNING: "Planning",
}


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Pricing:
    """List prices for one model, in USD per token, plus tiered overrides."""

    prompt: float = 0.0
    completion: float = 0.0
    request: float = 0.0
    image: float = 0.0
    input_cache_read: float = 0.0
    input_cache_write: float = 0.0
    overrides: list[dict[str, Any]] = field(default_factory=list)
    discount_to_user: float = 0.0

    def tier_for(self, prompt_tokens: int) -> dict[str, Any]:
        """Return the pricing tier that applies at ``prompt_tokens``.

        GMI publishes long-context tiers via ``pricing.overrides``. The last
        override whose ``min_prompt_tokens`` the request meets wins, because the
        list is ordered from the smallest threshold upwards.
        """
        chosen: dict[str, Any] = {}
        for override in self.overrides:
            threshold = int(override.get("min_prompt_tokens") or 0)
            if prompt_tokens >= threshold:
                chosen = override
        return chosen

    def effective(self, prompt_tokens: int = 0) -> "Pricing":
        """Return the pricing that applies at ``prompt_tokens``, tier included."""
        tier = self.tier_for(prompt_tokens)
        if not tier:
            return self
        return Pricing(
            prompt=float(tier.get("prompt", self.prompt) or 0.0),
            completion=float(tier.get("completion", self.completion) or 0.0),
            request=float(tier.get("request", self.request) or 0.0),
            image=float(tier.get("image", self.image) or 0.0),
            input_cache_read=float(tier.get("input_cache_read", self.input_cache_read) or 0.0),
            input_cache_write=float(tier.get("input_cache_write", self.input_cache_write) or 0.0),
            overrides=self.overrides,
            discount_to_user=self.discount_to_user,
        )

    def cost(
        self,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        requests: int = 0,
    ) -> float:
        """Estimate the USD cost of a request at the given token counts.

        The result is an *estimate at list price*. The organisation's discount
        is applied here because the live catalogue publishes it per model, so
        the figure tracks what the account is actually charged more closely
        than an undiscounted list price would.
        """
        tier = self.effective(prompt_tokens)
        total = (
            tier.prompt * prompt_tokens
            + tier.completion * completion_tokens
            + tier.request * requests
            + tier.image * requests
        )
        return round(total * (1.0 - self.discount_to_user), 10)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ModelInfo:
    """One routable model from the live GMI Cloud catalogue."""

    id: str
    name: str = ""
    owned_by: str = ""
    context_length: int | None = None
    quantization: str = ""
    created: int | None = None
    pricing: Pricing = field(default_factory=Pricing)
    description: str = ""
    capabilities: tuple[Capability, ...] = ()
    #: How each capability was determined — "metadata", "heuristic" or "probe".
    capability_evidence: dict[str, str] = field(default_factory=dict)
    is_ready: bool = True
    is_free: bool = False

    @property
    def capability_set(self) -> set[Capability]:
        return set(self.capabilities)

    def supports(self, capability: Capability) -> bool:
        return capability in self.capability_set

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name or self.id,
            "owned_by": self.owned_by,
            "context_length": self.context_length,
            "quantization": self.quantization,
            "pricing": self.pricing.to_dict(),
            "capabilities": [c.value for c in self.capabilities],
            "capability_evidence": dict(self.capability_evidence),
            "is_ready": self.is_ready,
            "is_free": self.is_free,
            "description": self.description,
        }


@dataclass(slots=True)
class MediaModelInfo:
    """One model from the GMI request-queue catalogue (image / video / audio)."""

    id: str
    modality: Modality
    description: str = ""
    #: False for catalogue entries that are private workflows or test entries
    #: rather than usable product models. They are listed, but never selected.
    product: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "modality": self.modality.value,
            "description": self.description,
            "product": self.product,
        }


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Step:
    """One planned unit of work."""

    index: int
    description: str
    task_type: TaskType
    prompt: str = ""
    modalities: tuple[Modality, ...] = ()
    depends_on: tuple[int, ...] = ()
    media_model: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "description": self.description,
            "task_type": self.task_type.value,
            "prompt": self.prompt,
            "modalities": [m.value for m in self.modalities],
            "depends_on": list(self.depends_on),
            "media_model": self.media_model,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class Plan:
    """An ordered set of steps produced by the planner."""

    goal: str
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "steps": [s.to_dict() for s in self.steps],
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RouteCandidate:
    """A scored, eligible model for a task.

    The model is a language model for a text step and a generation model for
    a media step. Both carry an ``id``, which is what the report shows.
    """

    model: ModelInfo | MediaModelInfo
    score: float
    reasons: list[str] = field(default_factory=list)
    estimated_cost_usd: float = 0.0
    benchmark_score: float | None = None
    historical_success: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model.id,
            "kind": "media" if isinstance(self.model, MediaModelInfo) else "llm",
            "score": round(self.score, 4),
            "reasons": list(self.reasons),
            "estimated_cost_usd": round(self.estimated_cost_usd, 8),
            "benchmark_score": self.benchmark_score,
            "historical_success": self.historical_success,
        }


@dataclass(slots=True)
class RouteDecision:
    """The router's answer for one task."""

    task_type: TaskType
    required_capabilities: tuple[Capability, ...]
    candidates: list[RouteCandidate] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    strategy: str = "balanced"
    #: ``"llm"`` or ``"media"``. A generation step is routed against the
    #: generation catalogue, so the answer comes from a different router.
    kind: str = "llm"
    #: The step this decision was made for. It is what lets a report line
    #: the published route up with the route that was actually taken.
    step_index: int = -1

    @property
    def primary(self) -> RouteCandidate | None:
        return self.candidates[0] if self.candidates else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type.value,
            "task_label": TASK_LABELS.get(self.task_type, self.task_type.value),
            "required_capabilities": [c.value for c in self.required_capabilities],
            "strategy": self.strategy,
            "kind": self.kind,
            "step_index": self.step_index,
            "candidates": [c.to_dict() for c in self.candidates],
            "rejected": dict(self.rejected),
        }


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Usage:
    """Token and cost accounting for one model call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    requests: int = 1
    cost_usd: float = 0.0
    #: True when the figure came from the provider's own usage block.
    measured: bool = True

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "requests": self.requests,
            "cost_usd": round(self.cost_usd, 8),
            "measured": self.measured,
        }


@dataclass(slots=True)
class StepResult:
    """The outcome of executing one step."""

    step_index: int
    description: str
    task_type: TaskType
    status: str = "pending"  # ok | failed | skipped
    model_id: str | None = None
    output: str = ""
    media_urls: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    attempts: int = 0
    #: The models that were actually called for this step, in order. A step
    #: that failed on one model must not be retried on the same one.
    attempted: list[str] = field(default_factory=list)
    fallbacks: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    validation: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    #: The estimated USD cost of this step at list price.
    estimated_cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_index": self.step_index,
            "description": self.description,
            "task_type": self.task_type.value,
            "status": self.status,
            "model_id": self.model_id,
            "output": self.output,
            "media_urls": list(self.media_urls),
            "usage": self.usage.to_dict(),
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
            "attempted": list(self.attempted),
            "fallbacks": list(self.fallbacks),
            "error": self.error,
            "validation": dict(self.validation),
            "estimated_cost_usd": round(self.estimated_cost_usd, 8),
        }


@dataclass(slots=True)
class RunReport:
    """The final report returned to Hermes."""

    run_id: str
    goal: str
    status: str = "completed"  # completed | failed | partial
    task_type: TaskType = TaskType.GENERAL_REASONING
    plan: Plan | None = None
    steps: list[StepResult] = field(default_factory=list)
    total_cost_usd: float = 0.0
    total_latency_ms: int = 0
    models_used: list[str] = field(default_factory=list)
    routes: list[RouteDecision] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "status": self.status,
            "task_type": self.task_type.value,
            "task_label": TASK_LABELS.get(self.task_type, self.task_type.value),
            "plan": self.plan.to_dict() if self.plan else None,
            "steps": [s.to_dict() for s in self.steps],
            "total_cost_usd": round(self.total_cost_usd, 8),
            "total_latency_ms": self.total_latency_ms,
            "models_used": list(self.models_used),
            "routes": [r.to_dict() for r in self.routes],
            "messages": list(self.messages),
            "created_at": self.created_at,
        }
