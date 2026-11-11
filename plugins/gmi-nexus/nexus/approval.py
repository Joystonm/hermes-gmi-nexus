"""Human approval for spend that reaches the configured threshold.

The rule this module exists to enforce: **the model may ask for money; only a
person may approve it.**

So there is no model-settable ``approve`` argument anywhere in NEXUS' tool
schemas, and the approval threshold is enforced by a ``pre_tool_call`` hook
that escalates the call to Hermes' own approval prompt. The person who
answers that prompt is the only thing that can unlock the spend; the per-run
and daily ceilings stay hard limits underneath it, and nothing the model says
can lift them.

How the two halves meet:

* the hook works out an upper bound on what the call may cost, using the
  catalogue NEXUS has **already read**. It never makes a network call: the
  hook runs on a bounded worker, and a hook that is slow blocks the tool.
* when that bound reaches ``approval_threshold_usd`` — or cannot be known at
  all, which is the case for a model whose price the catalogue does not
  publish — the hook returns Hermes' ``approve`` directive, and Hermes asks
  the human.
* the hook also returns a ``modify`` directive carrying a single-use token
  for that call. The tool consumes the token when it runs. A call that
  reaches the threshold without a token is refused, and the model cannot
  mint one: the nonce is random, it lives in a ledger inside the plugin, it
  is tied to the arguments it was issued for, and it is spent the moment it
  is used.

The estimate is an *estimate at list price*, read from the catalogue's own
published rates — never a claim about what anything was billed.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import Settings

#: The argument the hook adds to a call a human has to approve.
APPROVAL_TOKEN_KEY = "_gmi_nexus_approval"

#: The prefix the approval is remembered under. A stable key means Hermes'
#: "always allow" choice keeps working across calls.
APPROVAL_RULE_KEY = "gmi-nexus-spend"

#: How long an issued token stays usable. The human answers the prompt
#: straight away; the token is only good for the call that follows it.
TOKEN_TTL_SECONDS = 900.0

#: How long a worked-out estimate is reused for. The hook asks the same
#: question twice — once to add the token, once to raise the approval — and
#: the two answers must agree.
ESTIMATE_TTL_SECONDS = 5.0

#: The tools that can spend money. The others never need an approval.
SPENDING_TOOLS: frozenset[str] = frozenset(
    {
        "nexus_run",
        "nexus_benchmark",
        "nexus_vision",
        "nexus_media",
        "nexus_dev",
    }
)


def rule_key_for(tool_name: str) -> str:
    """Return the key Hermes remembers an "always allow" choice under.

    One key per tool: allowing ``nexus_run`` for the session must not also
    allow ``nexus_media``, which spends a different amount of money and takes
    a different kind of approval. Hermes namespaces plugin rules itself, so
    this key cannot collide with a shell command pattern.
    """
    name = str(tool_name or "").strip()
    return f"{APPROVAL_RULE_KEY}:{name}" if name else APPROVAL_RULE_KEY


@dataclass(slots=True)
class Estimate:
    """What a call is expected to cost, and what it costs to be told.

    ``amount`` is ``None`` when NEXUS cannot put a figure on it — a model
    whose price the catalogue does not publish, or a catalogue NEXUS has not
    read yet. An unknown amount is never treated as free.

    ``bound`` is the most the call could spend under the limits this plugin
    is configured with: the smaller of the per-run cap and what is left of
    today's budget. It is what the human is told they are authorising when
    the exact amount is not known.
    """

    amount: float | None
    bound: float
    label: str
    threshold: float
    ceiling: float
    #: Why the amount is unknown, when it is.
    unknown_reason: str = ""

    @property
    def known(self) -> bool:
        """Whether the amount is known."""
        return self.amount is not None

    @property
    def needs_approval(self) -> bool:
        """Whether a person has to say yes before this call is submitted.

        A known amount reaches the threshold, so it is held. An unknown
        amount is always held: $0.00 is what an unpublished price *looks*
        like, and treating it as free is how an uncapped spend happens. The
        budget guard applies the same rule inside the tool, so the two agree
        and a held call is never one the tool would have let through anyway.
        """
        if self.amount is None:
            return True
        return self.amount >= self.threshold

    def message(self, tool_name: str) -> str:
        """The text Hermes shows the human when it asks for approval."""
        limit = (
            f"at most ${max(0.0, self.bound):.2f}, the limit this plugin is "
            "configured with"
            if self.bound > 0
            else "the limit this plugin is configured with could not be worked out"
        )
        if self.amount is None:
            detail = self.unknown_reason or "the amount could not be worked out"
            return (
                f"{tool_name} may spend money on GMI Cloud and {detail}, so it "
                f"needs your approval before it is submitted ({limit})."
            )
        return (
            f"{tool_name} may spend about ${self.amount:.4f} on GMI Cloud "
            f"({limit})."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "amount": None if self.amount is None else round(self.amount, 8),
            "bound": round(max(0.0, self.bound), 8),
            "label": self.label,
            "threshold": self.threshold,
            "ceiling": self.ceiling,
            "known": self.known,
        }


class SpendApprover:
    """Keeps the approvals Hermes' approval gate has granted.

    The ledger is in memory and inside the plugin: a token is worthless
    anywhere else, and it is spent the first time it is used.
    """

    def __init__(self, *, ttl_seconds: float = TOKEN_TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._issued: dict[str, dict[str, Any]] = {}
        self._estimates: dict[tuple[str, str], tuple[float, Estimate]] = {}
        self._ttl = float(ttl_seconds)

    # -- issuing ------------------------------------------------------------ #

    def issue(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        estimated_usd: float | None,
        ceiling_usd: float,
    ) -> str:
        """Issue a single-use token for one call."""
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._sweep()
            self._issued[token] = {
                "tool_name": tool_name,
                "fingerprint": fingerprint(args),
                "estimated_usd": estimated_usd,
                "ceiling_usd": float(ceiling_usd),
                "issued_at": time.time(),
            }
        return token

    # -- spending ----------------------------------------------------------- #

    def consume(self, tool_name: str, args: dict[str, Any]) -> Approval | None:
        """Spend a token, if there is one for this call.

        Returns ``None`` when there is no token, when it was issued for
        another tool or another set of arguments, or when it has expired.
        """
        token = (args or {}).get(APPROVAL_TOKEN_KEY)
        if not isinstance(token, str) or not token:
            return None
        with self._lock:
            record = self._issued.pop(token, None)
        if record is None:
            return None
        if record["tool_name"] != tool_name:
            return None
        if record["fingerprint"] != fingerprint(args):
            return None
        if time.time() - float(record["issued_at"]) > self._ttl:
            return None
        return Approval(
            tool_name=tool_name,
            estimated_usd=record["estimated_usd"],
            ceiling_usd=float(record["ceiling_usd"]),
            issued_at=float(record["issued_at"]),
        )

    # -- estimates ---------------------------------------------------------- #

    def estimate(
        self,
        tool_name: str,
        args: dict[str, Any],
        compute: Callable[[], Estimate],
    ) -> Estimate:
        """Return the estimate for a call, working it out at most once.

        The hook asks the same question twice — once to add the token and
        once to raise the approval — and the two answers have to agree, so
        the answer is remembered for a few seconds.
        """
        key = (tool_name, fingerprint(args))
        now = time.time()
        with self._lock:
            hit = self._estimates.get(key)
            if hit is not None and now - hit[0] < ESTIMATE_TTL_SECONDS:
                return hit[1]
        value = compute()
        with self._lock:
            if len(self._estimates) > 64:
                self._estimates.clear()
            self._estimates[key] = (time.time(), value)
        return value

    # -- housekeeping ------------------------------------------------------- #

    def _sweep(self) -> None:
        """Drop tokens that have expired."""
        now = time.time()
        stale = [
            token
            for token, record in self._issued.items()
            if now - float(record["issued_at"]) > self._ttl
        ]
        for token in stale:
            self._issued.pop(token, None)


@dataclass(slots=True)
class Approval:
    """One approval, granted by a person through Hermes' approval gate."""

    tool_name: str
    estimated_usd: float | None
    ceiling_usd: float
    issued_at: float


#: The one ledger the plugin uses. The hook and the tools have to agree on
#: it, and a second instance would mean a token the tool cannot see.
_APPROVER = SpendApprover()


def approver() -> SpendApprover:
    """Return the plugin's spend-approval ledger."""
    return _APPROVER


def fingerprint(args: dict[str, Any]) -> str:
    """A stable fingerprint of a call's arguments, ignoring the token."""
    payload = {k: v for k, v in (args or {}).items() if k != APPROVAL_TOKEN_KEY}
    try:
        return json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return repr(sorted(payload.items(), key=lambda item: item[0]))


# --------------------------------------------------------------------------- #
# Working out what a call will cost
# --------------------------------------------------------------------------- #


def estimate_call(
    settings: Settings,
    engine: Any,
    tool_name: str,
    args: dict[str, Any],
) -> Estimate:
    """Estimate what a tool call will cost, from the catalogue already read.

    The estimate is an upper bound over the models the call could land on, so
    the answer the human is asked for is never lower than the answer the
    budget guard reaches. ``amount`` is ``None`` when the catalogue has not
    been read, or when a model the call could use publishes no price — an
    unknown amount is never treated as free.

    ``engine`` may be ``None``: the hook must never build one, because a
    hook that blocks on the network blocks the tool call behind it.
    """
    threshold = float(settings.approval_threshold_usd)
    ceiling = float(settings.max_cost_per_run_usd)
    amount, label, unknown = _estimate(engine, tool_name, args)
    return Estimate(
        amount=amount,
        bound=_bound(settings, engine),
        label=label,
        threshold=threshold,
        ceiling=ceiling,
        unknown_reason=unknown,
    )


def _bound(settings: Settings, engine: Any) -> float:
    """The most a single call could spend under the configured limits.

    The per-run cap and the daily budget are hard limits: a call cannot cost
    more than the smaller of them, whatever a person approves. That makes
    the bound the honest answer to "how much is this at worst", and it is
    what the human is told when the exact amount is not known.
    """
    ceiling = max(0.0, float(settings.max_cost_per_run_usd))
    remaining = max(0.0, float(settings.daily_budget_usd))
    if engine is not None:
        try:
            snapshot = engine.budget.snapshot()
            remaining = max(0.0, float(snapshot.get("daily_remaining_usd", remaining)))
        except Exception:
            # A storage fault must not turn a priced call into an unpriced
            # one; the configured daily budget is the safe stand-in.
            remaining = max(0.0, float(settings.daily_budget_usd))
    return min(ceiling, remaining)


def _estimate(
    engine: Any, tool_name: str, args: dict[str, Any]
) -> tuple[float | None, str, str]:
    """Return ``(amount, label, unknown_reason)`` for one tool."""
    if tool_name not in SPENDING_TOOLS:
        return None, "this call does not spend money", ""
    if engine is None:
        return (
            None,
            "a call that spends money",
            "the catalogue has not been read yet, so the cost cannot be worked out",
        )
    try:
        if tool_name == "nexus_run":
            return _estimate_run(engine, args)
        if tool_name == "nexus_benchmark":
            return _estimate_benchmark(engine, args)
        if tool_name == "nexus_vision":
            return _estimate_vision(engine, args)
        if tool_name == "nexus_media":
            return _estimate_media(engine, args)
        if tool_name == "nexus_dev":
            return _estimate_dev(engine, args)
    except Exception:
        # A failure to price a call is not a failure of the call: it means
        # the amount is unknown, which is exactly what the human is asked
        # about.
        return None, "the cost could not be worked out", "its cost could not be worked out"
    return None, "this call does not spend money", ""


def _estimate_run(engine: Any, args: dict[str, Any]) -> tuple[float | None, str, str]:
    """Price a whole pipeline run, step by step."""
    from .classifier import classify
    from .planner import Planner

    goal = str((args or {}).get("goal") or "").strip()
    if not goal:
        return None, "the goal is empty", "the goal is empty"
    models = engine.registry.cached_models()
    if not models:
        return (
            None,
            "a run of the goal",
            "the catalogue has not been read yet",
        )

    classification = classify(goal)
    plan = Planner().plan(goal, classification)
    strategy = _strategy(args)
    total = 0.0
    for step in plan.steps:
        bound, unknown = _step_bound(
            engine,
            step,
            models,
            strategy=strategy,
            requested_model=classification.requested_model,
        )
        if bound is None:
            return (
                None,
                f"a run of {len(plan.steps)} step(s)",
                unknown or "the cost of one of its steps could not be worked out",
            )
        total += bound
    return total, f"a run of {len(plan.steps)} step(s)", ""


def _strategy(args: dict[str, Any]) -> str:
    """The routing strategy the run will use.

    The estimate has to be made on the same routing the run will do: a
    ``quality`` run picks dearer models than a ``balanced`` one, and a
    figure worked out on the wrong routing is a figure about a different
    call.
    """
    value = str((args or {}).get("strategy") or "balanced").strip().lower()
    return value if value in ("balanced", "quality", "fast", "cheap") else "balanced"


def _step_bound(
    engine: Any,
    step: Any,
    models: list[Any],
    *,
    strategy: str = "balanced",
    requested_model: str | None = None,
) -> tuple[float | None, str]:
    """Price one planned step. Returns ``(cost, unknown_reason)``."""
    modality = engine.executor.media_modality(step)
    if modality is not None:
        options = dict(step.metadata.get("options") or {})
        return _media_bound(engine, modality, options)

    decision = engine.router.route(
        step.task_type,
        engine.executor.required_capabilities(step),
        models,
        strategy=strategy,
        prompt=step.prompt or "",
        requested_model=requested_model,
    )
    if not decision.candidates:
        return None, "no model in the catalogue can serve one of its steps"
    return _worst_cost(engine, step, decision.candidates)


def _worst_cost(engine: Any, step: Any, candidates: list[Any]) -> tuple[float | None, str]:
    """The most a call could cost among the models it might land on.

    A candidate whose price the catalogue does not publish makes the amount
    unknown: NEXUS cannot promise a figure it cannot compute, and an
    unknown figure is not a zero figure.
    """
    worst = 0.0
    for candidate in candidates:
        model = getattr(candidate, "model", None)
        if model is None:
            continue
        if not getattr(model, "price_known", True):
            return None, (
                "the catalogue publishes no price for one of the models that "
                "could serve it"
            )
        worst = max(
            worst,
            engine.executor.estimate_step_cost(step, model),
        )
    return worst, ""


def _estimate_benchmark(engine: Any, args: dict[str, Any]) -> tuple[float | None, str, str]:
    """Price a benchmark: every case against every model."""
    from .arena import DEFAULT_SUITE

    models = _benchmark_models(engine, args)
    if not models:
        return None, "a benchmark", "the catalogue has not been read yet"
    total = 0.0
    for model in models:
        if not model.price_known:
            return (
                None,
                f"{len(models)} model(s) against {len(DEFAULT_SUITE)} case(s)",
                "the catalogue publishes no price for one of the models to be tested",
            )
        for case in DEFAULT_SUITE:
            total += model.pricing.cost(
                prompt_tokens=max(1, len(case.prompt) // 4),
                completion_tokens=case.max_tokens,
                requests=1,
            )
    return total, f"{len(models)} model(s) against {len(DEFAULT_SUITE)} case(s)", ""


def _benchmark_models(engine: Any, args: dict[str, Any]) -> list[Any]:
    """The models a benchmark would run, read from the cached catalogue."""
    from .models import Capability

    cached = engine.registry.cached_models()
    requested = [str(item) for item in ((args or {}).get("models") or []) if str(item).strip()]
    if requested:
        wanted = set(requested)
        return [model for model in cached if model.id in wanted]
    chosen: list[Any] = []
    owners: set[str] = set()
    for model in cached:
        if not model.is_ready or not model.supports(Capability.TEXT):
            continue
        owner = (model.owned_by or model.id).split("/")[0]
        if owner in owners:
            continue
        owners.add(owner)
        chosen.append(model)
        if len(chosen) >= 3:
            break
    return chosen


def _estimate_vision(engine: Any, args: dict[str, Any]) -> tuple[float | None, str, str]:
    """Price a vision call."""
    from .models import Capability

    models = [
        model
        for model in engine.registry.cached_models()
        if model.supports(Capability.VISION) and model.is_ready
    ]
    if not models:
        return None, "a vision call", "the catalogue has not been read yet"
    worst = 0.0
    for model in models:
        if not model.price_known:
            return (
                None,
                "a vision call",
                "the catalogue publishes no price for one of the models that could read the image",
            )
        worst = max(
            worst,
            model.pricing.cost(prompt_tokens=512, completion_tokens=1024, requests=1),
        )
    return worst, "a vision call", ""


def _estimate_dev(engine: Any, args: dict[str, Any]) -> tuple[float | None, str, str]:
    """Price a development call."""
    from .models import Capability
    from .router import estimate_tokens

    models = [
        model
        for model in engine.registry.cached_models()
        if model.supports(Capability.CODING)
    ]
    if not models:
        return None, "a development call", "the catalogue has not been read yet"
    prompt = str((args or {}).get("task") or "")
    prompt_tokens = estimate_tokens(prompt)
    budget = max(1024, int(engine.settings.code_max_tokens))
    worst = 0.0
    for model in models:
        if not model.price_known:
            return (
                None,
                "a development call",
                "the catalogue publishes no price for one of the models that could do the work",
            )
        worst = max(
            worst,
            model.pricing.cost(
                prompt_tokens=prompt_tokens,
                completion_tokens=budget,
                requests=1,
            ),
        )
    return worst, "a development call", ""


def _estimate_media(engine: Any, args: dict[str, Any]) -> tuple[float | None, str, str]:
    """Price a generation."""
    from .models import Modality

    name = str((args or {}).get("modality") or "image").strip().lower()
    modality = {
        "image": Modality.IMAGE,
        "video": Modality.VIDEO,
        "audio": Modality.AUDIO,
    }.get(name)
    if modality is None:
        return None, f"a {name} generation", f"{name} is not a modality NEXUS can generate"
    options = dict((args or {}).get("options") or {})
    for key in ("image", "mask"):
        value = (args or {}).get(key)
        if value:
            options[key] = value
    bound, unknown = _media_bound(engine, modality, options)
    if bound is None:
        return None, f"a {modality.value} generation", unknown
    return bound, f"a {modality.value} generation", ""


def _media_bound(engine: Any, modality: Any, options: dict[str, Any]) -> tuple[float | None, str]:
    """The most a generation could cost among the models already described.

    Only models that publish a price are considered: an unpriced model is
    refused by the media router rather than treated as free, so including it
    would price a call NEXUS would never make.
    """
    specs = [
        spec
        for spec in engine.media.catalog.cached()
        if spec.modality is modality and spec.has_published_price()
    ]
    if not specs:
        return None, (
            f"no {modality.value} model with a published price has been described yet"
        )
    try:
        count = int(options.get("n") or 1)
    except (TypeError, ValueError):
        count = 1
    duration = _as_float(options.get("duration"))
    text = str(options.get("text") or "")
    worst = 0.0
    for spec in specs:
        worst = max(worst, spec.estimate_cost(count=count, duration=duration, text=text))
    return worst, ""


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# The hook
# --------------------------------------------------------------------------- #


def make_hooks(
    approver: SpendApprover,
    context_provider: Callable[[], tuple[Settings, Any]],
) -> tuple[Callable[..., Any], Callable[..., Any]]:
    """Build the two ``pre_tool_call`` callbacks NEXUS registers.

    The first adds the token; the second asks the human. They are separate
    callbacks because a Hermes hook directive carries one action, and both
    are needed: the token proves the human said yes, and the directive is
    what makes Hermes ask.

    ``context_provider`` returns ``(settings, engine)``. The engine is the
    one that is **already built** — the hook must never build it, because
    building it may read the catalogue over the network and a hook that
    blocks on the network blocks the tool call behind it.

    Nothing in either callback may raise: Hermes treats a raising
    ``pre_tool_call`` callback as a veto, so an error in the estimate would
    silently block every spending tool.
    """

    def _estimate(tool_name: str, args: dict[str, Any]) -> Estimate:
        def compute() -> Estimate:
            try:
                settings, engine = context_provider()
            except Exception:
                settings, engine = None, None
            if settings is None:
                return Estimate(
                    amount=None,
                    bound=0.0,
                    label="a call that spends money",
                    threshold=0.0,
                    ceiling=0.0,
                    unknown_reason="the plugin's configuration could not be read",
                )
            try:
                return estimate_call(settings, engine, tool_name, args)
            except Exception:
                return Estimate(
                    amount=None,
                    bound=0.0,
                    label="a call that spends money",
                    threshold=0.0,
                    ceiling=0.0,
                    unknown_reason="its cost could not be worked out",
                )

        return approver.estimate(tool_name, args, compute)

    def on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> dict[str, Any] | None:
        """Escalate a call that needs a person's approval."""
        name = str(tool_name or "")
        if name not in SPENDING_TOOLS:
            return None
        call_args = dict(args or {})
        try:
            estimate = _estimate(name, call_args)
        except Exception:
            # No figure could be worked out, so no figure can be trusted to
            # be under the threshold. Hold the call.
            return {
                "action": "approve",
                "message": (
                    f"{name} may spend money on GMI Cloud and its cost could "
                    "not be worked out, so it needs your approval before it is "
                    "submitted."
                ),
                "rule_key": rule_key_for(name),
            }
        if not estimate.needs_approval:
            return None
        return {
            "action": "approve",
            "message": estimate.message(name),
            "rule_key": rule_key_for(name),
        }

    def on_pre_tool_call_modify(
        tool_name: str = "", args: Any = None, **_: Any
    ) -> dict[str, Any] | None:
        """Add the single-use token that proves the human approved the call."""
        name = str(tool_name or "")
        if name not in SPENDING_TOOLS:
            return None
        call_args = dict(args or {})
        try:
            estimate = _estimate(name, call_args)
            if not estimate.needs_approval:
                return None
            token = approver.issue(
                name,
                call_args,
                estimated_usd=estimate.amount,
                ceiling_usd=estimate.bound,
            )
        except Exception:
            return None
        return {"action": "modify", "args": {APPROVAL_TOKEN_KEY: token}}

    return on_pre_tool_call_modify, on_pre_tool_call


__all__ = [
    "APPROVAL_RULE_KEY",
    "APPROVAL_TOKEN_KEY",
    "Approval",
    "Estimate",
    "SPENDING_TOOLS",
    "SpendApprover",
    "approver",
    "estimate_call",
    "fingerprint",
    "make_hooks",
    "rule_key_for",
]
