"""Model selection.

The router turns a task into an ordered list of eligible models. Two things
happen in this order and must not be swapped:

1. **Hard filters.** A model that cannot do what the task needs — no vision for
   an image task, blocked by the operator, not ready, or missing from the live
   catalogue — is removed outright and the reason is recorded.
2. **Scoring.** The survivors are ranked by weighted signals: benchmark
   history, observed reliability, latency, cost and operator preference.

The signals come from three places, in descending order of authority: real
benchmark results measured by the Arena, NEXUS' own recorded execution
outcomes, and finally the catalogue's own published pricing. Where a signal is
missing, the router falls back to a neutral prior and *says so* in the
candidate's reasons, so a report never implies a measurement that did not
happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .config import Settings
from .models import (
    ESSENTIAL_CAPABILITIES,
    SPECIALISATION_CAPABILITIES,
    Capability,
    ModelInfo,
    RouteCandidate,
    RouteDecision,
    TaskType,
)

#: Default weights per routing strategy.
STRATEGY_WEIGHTS: dict[str, dict[str, float]] = {
    "balanced": {"quality": 0.35, "reliability": 0.30, "latency": 0.15, "cost": 0.20},
    "quality": {"quality": 0.60, "reliability": 0.25, "latency": 0.05, "cost": 0.10},
    "fast": {"quality": 0.20, "reliability": 0.20, "latency": 0.45, "cost": 0.15},
    "cheap": {"quality": 0.20, "reliability": 0.20, "latency": 0.15, "cost": 0.45},
}

#: Neutral prior used when a signal has never been observed.
PRIOR_QUALITY = 0.55
PRIOR_RELIABILITY = 0.60

#: How many observations are needed before history outweighs the prior.
PRIOR_STRENGTH = 4

#: Bonus applied to a model the operator explicitly prefers.
PREFERENCE_BONUS = 0.12

#: Rough tokens-per-character ratio used for cost pre-estimates.
CHARS_PER_TOKEN = 4.0

#: Latency that scores 0.5 on the latency signal.
LATENCY_HALF_LIFE_MS = 2000.0

#: Cost (USD) that scores 0.5 on the cost signal.
COST_HALF_LIFE_USD = 0.01

#: Score penalty for a model that lacks a required *specialisation*. The
#: model stays eligible — a general model can attempt the work — but it is
#: ranked below the specialists and the reason is recorded.
SPECIALISATION_PENALTY = 0.15


@dataclass(slots=True)
class RoutingSignals:
    """Everything the router knows about the models it may choose."""

    #: Benchmark score per model id, in the range 0..1.
    benchmark: dict[str, float]
    #: Observed success rate per model id, in the range 0..1.
    success: dict[str, float]
    #: Observed sample count per model id.
    samples: dict[str, int]
    #: Exponentially weighted latency per model id, in milliseconds.
    latency: dict[str, float]
    #: Exponentially weighted cost per model id, in USD.
    cost: dict[str, float]

    @classmethod
    def empty(cls) -> "RoutingSignals":
        return cls({}, {}, {}, {}, {})

    @classmethod
    def from_storage(cls, task_type: str | None = None) -> "RoutingSignals":
        """Load the recorded signals from the plugin's database."""
        from . import storage

        benchmark: dict[str, float] = {}
        rows = storage.model_stats(task_type)
        success: dict[str, float] = {}
        samples: dict[str, int] = {}
        latency: dict[str, float] = {}
        cost: dict[str, float] = {}
        for row in rows:
            model_id = str(row.get("model_id") or "")
            if not model_id:
                continue
            runs = int(row.get("runs") or 0)
            successes = int(row.get("successes") or 0)
            samples[model_id] = samples.get(model_id, 0) + runs
            success[model_id] = (successes / runs) if runs else PRIOR_RELIABILITY
            latency[model_id] = float(row.get("ewma_latency_ms") or 0.0)
            cost[model_id] = float(row.get("ewma_cost_usd") or 0.0)

        # Benchmark scores come from the Arena's recorded results.
        for benchmark_row in storage.list_benchmarks(limit=50):
            summary = benchmark_row.get("summary") or {}
            if not isinstance(summary, dict):
                continue
            scores = summary.get("scores") or {}
            if not isinstance(scores, dict):
                continue
            for model_id, value in scores.items():
                if isinstance(value, (int, float)):
                    benchmark[str(model_id)] = float(value)
        return cls(benchmark, success, samples, latency, cost)


def _shrink(observed: float | None, samples: int, prior: float) -> float:
    """Blend an observed rate with the prior, weighted by sample count."""
    if observed is None or samples <= 0:
        return prior
    weight = min(1.0, samples / PRIOR_STRENGTH)
    return prior * (1.0 - weight) + observed * weight


def _latency_score(latency_ms: float) -> float:
    """Map a latency to 0..1, where faster is better."""
    if latency_ms <= 0:
        return 0.75  # never observed: assume decent rather than perfect
    return 1.0 / (1.0 + latency_ms / LATENCY_HALF_LIFE_MS)


def _cost_score(cost_usd: float) -> float:
    """Map a cost to 0..1, where cheaper is better."""
    if cost_usd <= 0:
        return 0.9
    return 1.0 / (1.0 + cost_usd / COST_HALF_LIFE_USD)


def estimate_tokens(text: str) -> int:
    """Estimate the prompt token count of ``text``."""
    if not text:
        return 0
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def _matches_request(model_id: str, requested: str) -> bool:
    """Whether a catalogue id satisfies a model named in the request.

    A request may name "gpt-4o-mini" where the catalogue says
    "openai/gpt-4o-mini", so a trailing segment match counts.
    """
    if model_id == requested:
        return True
    return model_id.lower().endswith("/" + requested.lower())


class Router:
    """Selects models for tasks."""

    def __init__(self, settings: Settings, signals: RoutingSignals | None = None) -> None:
        self.settings = settings
        self.signals = signals or RoutingSignals.empty()

    # -- eligibility -------------------------------------------------------- #

    def reject_reasons(self, model: ModelInfo, required: Iterable[Capability]) -> str | None:
        """Return why ``model`` cannot serve the task, or ``None`` if it can.

        Only the *essential* capabilities reject a model. A missing
        specialisation is a scoring matter, not an eligibility one — see
        :data:`nexus.models.SPECIALISATION_CAPABILITIES`.
        """
        if model.id in self.settings.blocked_models:
            return "blocked by operator configuration"
        if not model.is_ready:
            return "model is not ready for inference"
        missing = [
            c for c in required if c in ESSENTIAL_CAPABILITIES and not model.supports(c)
        ]
        if missing:
            names = ", ".join(c.value for c in missing)
            return f"missing required capabilities: {names}"
        return None

    @staticmethod
    def missing_specialisations(
        model: ModelInfo, required: Iterable[Capability]
    ) -> list[Capability]:
        """Return the required specialisations ``model`` does not publish."""
        return [
            c
            for c in required
            if c in SPECIALISATION_CAPABILITIES and not model.supports(c)
        ]

    # -- scoring ------------------------------------------------------------ #

    def score_candidate(
        self,
        model: ModelInfo,
        required: Iterable[Capability],
        *,
        strategy: str = "balanced",
        prompt: str = "",
        expected_output_tokens: int = 0,
    ) -> RouteCandidate:
        """Score one model for one task."""
        weights = STRATEGY_WEIGHTS.get(strategy, STRATEGY_WEIGHTS["balanced"])
        reasons: list[str] = []

        benchmark_score = self.signals.benchmark.get(model.id)
        if benchmark_score is None:
            quality = PRIOR_QUALITY
            reasons.append("quality: no benchmark data, using prior")
        else:
            quality = float(benchmark_score)
            reasons.append(f"quality: arena score {quality:.2f}")

        samples = self.signals.samples.get(model.id, 0)
        observed_success = self.signals.success.get(model.id)
        reliability = _shrink(observed_success, samples, PRIOR_RELIABILITY)
        if observed_success is None or samples == 0:
            reasons.append("reliability: no recorded runs, using prior")
        else:
            reasons.append(f"reliability: {reliability:.2f} over {samples} run(s)")

        latency_ms = self.signals.latency.get(model.id, 0.0)
        latency = _latency_score(latency_ms)
        if latency_ms > 0:
            reasons.append(f"latency: ~{int(latency_ms)}ms observed")

        prompt_tokens = estimate_tokens(prompt)
        estimated = model.pricing.cost(
            prompt_tokens=prompt_tokens,
            completion_tokens=expected_output_tokens,
            requests=1,
        )
        cost = _cost_score(estimated)
        reasons.append(f"cost: ~${estimated:.6f} estimated at list price")

        score = (
            weights["quality"] * quality
            + weights["reliability"] * reliability
            + weights["latency"] * latency
            + weights["cost"] * cost
        )

        missing = self.missing_specialisations(model, required)
        if missing:
            names = ", ".join(c.value for c in missing)
            score -= SPECIALISATION_PENALTY * len(missing)
            reasons.append(
                f"specialisation: the catalogue publishes no {names} capability "
                f"for this model, so it is ranked below the specialists"
            )

        if model.id in self.settings.preferred_models:
            score += PREFERENCE_BONUS
            reasons.append("preference: operator preferred model")

        # A model that is free is strictly better on cost.
        if model.is_free:
            reasons.append("model is free")

        # The score is a ranking value, never a probability: it is floored at
        # zero so a report never shows a negative number.
        score = max(0.0, score)

        return RouteCandidate(
            model=model,
            score=score,
            reasons=reasons,
            estimated_cost_usd=estimated,
            benchmark_score=benchmark_score,
            historical_success=observed_success,
        )

    # -- the main entry point ---------------------------------------------- #

    def route(
        self,
        task_type: TaskType,
        required: Iterable[Capability],
        models: Iterable[ModelInfo],
        *,
        strategy: str = "balanced",
        prompt: str = "",
        expected_output_tokens: int = 0,
        requested_model: str | None = None,
        limit: int = 5,
    ) -> RouteDecision:
        """Rank the eligible models for a task."""
        required = tuple(required)
        candidates: list[RouteCandidate] = []
        rejected: dict[str, str] = {}

        for model in models:
            reason = self.reject_reasons(model, required)
            if reason is not None:
                rejected[model.id] = reason
                continue
            candidates.append(
                self.score_candidate(
                    model,
                    required,
                    strategy=strategy,
                    prompt=prompt,
                    expected_output_tokens=expected_output_tokens,
                )
            )

        candidates.sort(key=lambda c: (-c.score, c.estimated_cost_usd, c.model.id))

        decision = RouteDecision(
            task_type=task_type,
            required_capabilities=required,
            candidates=candidates[:limit],
            rejected=rejected,
            strategy=strategy,
        )

        # An explicitly requested model is honoured when it is eligible, and
        # the operator is told when it was not.
        if requested_model:
            match = next(
                (c for c in candidates if _matches_request(c.model.id, requested_model)),
                None,
            )
            if match is not None:
                decision.candidates.remove(match)
                match.reasons.insert(0, "request: model was named in the request")
                match.score += 1.0
                decision.candidates.insert(0, match)
            else:
                decision.rejected[requested_model] = (
                    rejected.get(requested_model, "named model is not available for this task")
                )
        return decision

    def reload_signals(self, task_type: str | None = None) -> RoutingSignals:
        """Re-read the recorded signals from storage."""
        self.signals = RoutingSignals.from_storage(task_type)
        return self.signals

    def to_dict(self) -> dict[str, Any]:
        """Return a description of the router's current state."""
        return {
            "strategy_weights": STRATEGY_WEIGHTS,
            "preferred_models": list(self.settings.preferred_models),
            "blocked_models": list(self.settings.blocked_models),
            "models_with_benchmark_data": len(self.signals.benchmark),
            "models_with_run_history": len(self.signals.samples),
        }


__all__ = [
    "CHARS_PER_TOKEN",
    "PRIOR_QUALITY",
    "PRIOR_RELIABILITY",
    "PREFERENCE_BONUS",
    "STRATEGY_WEIGHTS",
    "Router",
    "RoutingSignals",
    "estimate_tokens",
]
