"""The tools NEXUS registers with Hermes.

Eight tools, each a thin, honest wrapper over the engine:

``nexus_models``     what GMI Cloud offers right now, and what NEXUS verified
``nexus_analyze``    understand + plan + route preview, spending nothing
``nexus_run``        the full eight-step pipeline
``nexus_benchmark``  real side-by-side measurements across models
``nexus_vision``     read images, find visual defects, compare, screenshot
``nexus_media``      generate and edit images, video and audio
``nexus_dev``        implement, test and fix code through Hermes' own tools
``nexus_report``     what actually happened, at what measured cost

Every handler returns a JSON string, because that is what Hermes' tool
registry accepts. Errors are returned as ``{"error": ...}`` with a message
written for the person reading it, not a stack trace.
"""

from __future__ import annotations

import functools
import json
from typing import Any, Callable

from .config import Settings, resolve_settings
from .errors import NexusError
from .models import Capability, Modality, probe_verdict
from .orchestrator import Nexus

#: The modalities the media catalogue is searched by.
_MEDIA_MODALITIES = {
    "image": Modality.IMAGE,
    "video": Modality.VIDEO,
    "audio": Modality.AUDIO,
}


def _dumps(payload: Any) -> str:
    """Encode a tool result."""
    return json.dumps(payload, ensure_ascii=False, default=str)


def _error(message: str, **extra: Any) -> str:
    """Encode a tool error."""
    return _dumps({"error": message, **extra})


class NexusToolBox:
    """Holds the engine and implements the tool handlers.

    The engine is built once per plugin load so the live catalogue is fetched
    once, not once per call.
    """

    def __init__(self, ctx: Any | None = None) -> None:
        self.ctx = ctx
        self._engine: Nexus | None = None
        self._settings: Settings | None = None

    # -- engine ------------------------------------------------------------- #

    def settings(self) -> Settings:
        """Return the resolved settings, re-read when the config changes."""
        return resolve_settings(self.ctx)

    def engine(self) -> Nexus:
        """Return the engine, building it on first use."""
        if self._engine is None:
            self._settings = self.settings()
            self._engine = Nexus(self._settings, ctx=self.ctx)
        return self._engine

    def engine_if_ready(self) -> Nexus | None:
        """Return the engine only when it has already been built.

        The spend-approval hook needs a priced catalogue, and it runs on a
        bounded worker thread. Building the engine here could read the
        catalogue over the network, and a hook that blocks on the network
        blocks the tool call behind it — so the hook is given the engine
        that exists, and never one it has to go and get.
        """
        return self._engine

    def approval_context(self) -> tuple[Settings, Nexus | None]:
        """Return the settings and the engine the approval hook should read.

        The engine's own settings are used when it exists, so the threshold
        the hook applies is the same threshold the budget guard applies
        inside the tool. A tool that reads the limits the hook used is the
        only kind where the two can agree.
        """
        engine = self._engine
        if engine is not None:
            return engine.settings, engine
        return self.settings(), None

    def refresh_settings(self) -> None:
        """Drop the cached engine so the next call picks up new settings."""
        if self._engine is not None:
            self._engine.close()
        self._engine = None
        self._settings = None

    # -- helpers ------------------------------------------------------------ #

    @staticmethod
    def _str(args: dict[str, Any], key: str, default: str = "") -> str:
        value = args.get(key, default)
        return str(value).strip() if value is not None else default

    @staticmethod
    def _bool(args: dict[str, Any], key: str, default: bool = False) -> bool:
        value = args.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    @staticmethod
    def _list(args: dict[str, Any], key: str) -> list[str]:
        value = args.get(key) or []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    @staticmethod
    def _strategy(args: dict[str, Any]) -> str:
        strategy = str(args.get("strategy") or "balanced").strip().lower()
        return strategy if strategy in ("balanced", "quality", "fast", "cheap") else "balanced"

    @staticmethod
    def _approved(tool_name: str, args: dict[str, Any]) -> bool:
        """Whether a person has approved this call.

        The answer comes from a token the ``pre_tool_call`` hook adds once
        Hermes' approval prompt has been answered. There is no argument the
        model can set to get it: the token is random, single-use, tied to the
        arguments it was issued for, and lives in a ledger inside the plugin.
        A call without one is simply not approved, and the spend limits
        apply as they are configured.
        """
        from .approval import approver

        try:
            return approver().consume(tool_name, args or {}) is not None
        except Exception:  # pragma: no cover - defensive
            return False

    # -- nexus_models ------------------------------------------------------- #

    def nexus_models(self, args: dict[str, Any]) -> str:
        """List the live catalogue with verified capabilities and prices."""
        engine = self.engine()
        modality = self._str(args, "modality", "llm").lower() or "llm"
        query = self._str(args, "query").lower()
        capability = self._str(args, "capability").lower()
        verified_only = self._bool(args, "verified_only")
        refresh = self._bool(args, "refresh")
        try:
            limit = int(args.get("limit") or 40)
        except (TypeError, ValueError):
            limit = 40
        limit = max(1, min(limit, 200))

        if modality == "llm":
            models = engine.registry.list_models(refresh=refresh)
            if query:
                models = [
                    m for m in models
                    if query in m.id.lower() or query in m.name.lower()
                ]
            if capability:
                try:
                    wanted = Capability(capability)
                except ValueError:
                    return _error(
                        f"'{capability}' is not a capability NEXUS knows about. "
                        f"Known capabilities: {', '.join(c.value for c in Capability)}."
                    )
                models = [m for m in models if m.supports(wanted)]
            if verified_only:
                models = [
                    m for m in models
                    if any(
                        probe_verdict(evidence) is True
                        for evidence in m.capability_evidence.values()
                    )
                ]
            payload = {
                "catalogue": "gmi-llm",
                "count": len(models),
                "source": engine.registry.source_description(),
                "models": [m.to_dict() for m in models[:limit]],
                "truncated": len(models) > limit,
            }
            return _dumps(payload)

        if modality not in _MEDIA_MODALITIES:
            return _error(
                f"'{modality}' is not a catalogue NEXUS can search. "
                "Use one of: llm, image, video, audio."
            )

        wanted = _MEDIA_MODALITIES[modality]
        media = engine.registry.list_media(wanted, refresh=refresh)
        if query:
            media = [m for m in media if query in m.id.lower()]
        return _dumps(
            {
                "catalogue": f"gmi-{modality}",
                "count": len(media),
                "source": engine.registry.source_description(),
                "models": [m.to_dict() for m in media[:limit]],
                "truncated": len(media) > limit,
                "note": (
                    "Each model's own published description, price and parameter "
                    "schema are read before a request is built for it."
                ),
            }
        )

    # -- nexus_analyze ------------------------------------------------------ #

    def nexus_analyze(self, args: dict[str, Any]) -> str:
        """Understand and plan a goal without spending anything."""
        goal = self._str(args, "goal")
        if not goal:
            return _error("nexus_analyze needs a 'goal' to work on.")
        engine = self.engine()
        analysis = engine.analyze(goal, strategy=self._strategy(args))
        analysis["cost"] = {
            "spent_usd": 0.0,
            "note": "Analysis reads the free catalogue; it calls no model.",
        }
        return _dumps(analysis)

    # -- nexus_run ---------------------------------------------------------- #

    def nexus_run(self, args: dict[str, Any]) -> str:
        """Run the eight-step pipeline."""
        goal = self._str(args, "goal")
        if not goal:
            return _error("nexus_run needs a 'goal' to work on.")
        engine = self.engine()
        report = engine.run(
            goal,
            strategy=self._strategy(args),
            approved=self._approved("nexus_run", args),
        )
        return _dumps(report.to_dict())

    # -- nexus_benchmark ---------------------------------------------------- #

    def nexus_benchmark(self, args: dict[str, Any]) -> str:
        """Benchmark models against each other on identical test cases."""
        engine = self.engine()
        requested = self._list(args, "models")
        suite_name = self._str(args, "suite", "default") or "default"

        models = []
        if requested:
            for model_id in requested:
                model = engine.registry.get(model_id)
                if model is None:
                    engine.registry.list_models()
                    model = engine.registry.get(model_id)
                if model is None:
                    return _error(
                        f"'{model_id}' is not in the live GMI Cloud catalogue, so "
                        "there is nothing to benchmark."
                    )
                models.append(model)
        else:
            models = _default_benchmark_set(engine)

        if not models:
            return _error(
                "No model could be selected for the benchmark. Refresh the "
                "catalogue with nexus_models and try again."
            )

        result = engine.arena.run(
            models,
            budget=engine.budget,
            approved=self._approved("nexus_benchmark", args),
            suite_name=suite_name,
        )
        result["cost_note"] = (
            "Costs below are measured from the provider's own usage blocks, "
            "priced at the catalogue's published rates."
        )
        return _dumps(result)

    # -- nexus_vision ------------------------------------------------------- #

    def nexus_vision(self, args: dict[str, Any]) -> str:
        """Analyse images, find defects, compare, or capture the screen."""
        engine = self.engine()
        mode = self._str(args, "mode", "analyze").lower() or "analyze"
        approved = self._approved("nexus_vision", args)

        if mode == "screenshot":
            capture = engine.vision.capture_screenshot(app=self._str(args, "app", "screen") or "screen")
            if not capture.get("ok"):
                return _error(str(capture.get("error") or "the screenshot failed"))
            payload: dict[str, Any] = {"mode": "screenshot", **capture}
            images = self._list(args, "images")
            if images:
                question = self._str(args, "question") or "Describe this screen."
                outcome = engine.vision.analyse(
                    [capture["image"], *images], question, approved=approved
                )
                payload["analysis"] = outcome.to_dict()
            return _dumps(payload)

        if mode == "compare":
            before = self._str(args, "before")
            after = self._str(args, "after")
            if not before or not after:
                return _error("A comparison needs both a 'before' and an 'after' image.")
            outcome = engine.vision.compare(
                before, after, question=self._str(args, "question"), approved=approved
            )
            return _dumps({"mode": "compare", **outcome.to_dict()})

        images = self._list(args, "images")
        if not images:
            return _error(
                "Give at least one image in 'images', or use mode 'screenshot' "
                "to capture the screen first."
            )

        if mode == "issues":
            outcome = engine.vision.detect_issues(
                images, self._str(args, "criteria"), approved=approved
            )
            return _dumps({"mode": "issues", **outcome.to_dict()})

        question = self._str(args, "question") or "Describe this image in detail."
        outcome = engine.vision.analyse(images, question, approved=approved)
        return _dumps({"mode": "analyze", **outcome.to_dict()})

    # -- nexus_media -------------------------------------------------------- #

    def nexus_media(self, args: dict[str, Any]) -> str:
        """Generate or edit images, video and audio."""
        engine = self.engine()
        prompt = self._str(args, "prompt")
        if not prompt:
            return _error("nexus_media needs a 'prompt' describing what to generate.")

        modality_name = self._str(args, "modality", "image").lower() or "image"
        if modality_name not in _MEDIA_MODALITIES:
            return _error(
                f"'{modality_name}' is not a modality NEXUS can generate. "
                "Use one of: image, video, audio."
            )
        modality = _MEDIA_MODALITIES[modality_name]

        options = args.get("options")
        options = dict(options) if isinstance(options, dict) else {}
        image = self._str(args, "image")
        mask = self._str(args, "mask")

        if image:
            options["image"] = image
        if mask:
            options["mask"] = mask

        result = engine.creative.generate(
            prompt,
            modality=modality,
            options=options,
            approved=self._approved("nexus_media", args),
        )
        payload = result.to_dict()
        payload["cost_note"] = (
            "estimated_cost_usd is priced from the model's published rate before "
            "the request; it is not a billed amount."
        )
        if not result.ok and result.status == "blocked":
            payload["hint"] = (
                "The request was refused by the configured spend limits, or it "
                "needs a person to approve it. Raise the limit in the plugin's "
                "configuration, or ask the person running this session to "
                "approve the request when Hermes asks."
            )
        return _dumps(payload)

    # -- nexus_dev ---------------------------------------------------------- #

    def nexus_dev(self, args: dict[str, Any]) -> str:
        """Implement, test and fix code through Hermes' own tools."""
        engine = self.engine()
        mode = self._str(args, "mode", "implement").lower() or "implement"
        task = self._str(args, "task")
        if not task:
            return _error("nexus_dev needs a 'task' describing the change to make.")
        files = self._list(args, "files")
        approved = self._approved("nexus_dev", args)

        if mode == "review":
            if not files:
                return _error("A review needs the 'files' to read.")
            outcome = engine.dev.review(files, approved=approved)
            return _dumps({"mode": "review", **outcome.to_dict()})

        try:
            max_attempts = int(args.get("max_attempts") or 2)
        except (TypeError, ValueError):
            max_attempts = 2
        outcome = engine.dev.implement(
            task,
            context_files=files,
            test_command=self._str(args, "test_command") or None,
            max_attempts=max(1, min(max_attempts, 5)),
            approved=approved,
        )
        payload = {"mode": "implement", **outcome.to_dict()}
        payload["execution_note"] = (
            "Files were written and commands were run through Hermes' own "
            "write_file and terminal tools, so Hermes' approval policy applied."
        )
        return _dumps(payload)

    # -- nexus_report ------------------------------------------------------- #

    def nexus_report(self, args: dict[str, Any]) -> str:
        """Report the recorded history and the routing statistics."""
        from . import storage

        run_id = self._str(args, "run_id")
        if run_id:
            detail = storage.run_detail(run_id)
            if detail is None:
                return _error(f"No run with id '{run_id}' is recorded.")
            return _dumps(detail)

        try:
            days = int(args.get("days") or 30)
        except (TypeError, ValueError):
            days = 30
        days = max(1, min(days, 365))

        engine = self.engine()
        status = engine.status()
        status["days"] = days
        status["cost_note"] = (
            "Costs are estimates priced at the catalogue's published rates; "
            "the token counts are the provider's own reported usage."
        )
        if not status["recent_runs"]:
            status["recent_runs_note"] = (
                "No run has been recorded yet — run nexus_run to create one."
            )
        return _dumps(status)


def _default_benchmark_set(engine: Nexus, limit: int = 3) -> list[Any]:
    """Pick a diverse set of models to benchmark when none is named.

    Diversity beats size here: three models from three different providers
    answer a different question than three checkpoints of the same family.
    """
    models = [m for m in engine.registry.list_models() if m.is_ready and m.supports(Capability.TEXT)]
    if not models:
        return []

    chosen: list[Any] = []
    owners: set[str] = set()
    for model in models:
        owner = (model.owned_by or model.id).split("/")[0]
        if owner in owners:
            continue
        owners.add(owner)
        chosen.append(model)
        if len(chosen) >= limit:
            break
    return chosen


def _guard(func: Callable[[NexusToolBox, dict[str, Any]], str]) -> Callable[..., str]:
    """Wrap a handler so any failure becomes a readable error."""

    @functools.wraps(func)
    def wrapper(self: NexusToolBox, args: dict[str, Any]) -> str:
        try:
            return func(self, args or {})
        except NexusError as exc:
            return _error(str(exc), kind=type(exc).__name__)
        except Exception as exc:  # pragma: no cover - defensive
            return _error(f"{type(exc).__name__}: {exc}")

    return wrapper


# Wrap every public handler with the guard.
nexus_models = _guard(NexusToolBox.nexus_models)
nexus_analyze = _guard(NexusToolBox.nexus_analyze)
nexus_run = _guard(NexusToolBox.nexus_run)
nexus_benchmark = _guard(NexusToolBox.nexus_benchmark)
nexus_vision = _guard(NexusToolBox.nexus_vision)
nexus_media = _guard(NexusToolBox.nexus_media)
nexus_dev = _guard(NexusToolBox.nexus_dev)
nexus_report = _guard(NexusToolBox.nexus_report)


#: The tool names NEXUS registers, mapped to their handler.
TOOLS: dict[str, Callable[[NexusToolBox, dict[str, Any]], str]] = {
    "nexus_models": nexus_models,
    "nexus_analyze": nexus_analyze,
    "nexus_run": nexus_run,
    "nexus_benchmark": nexus_benchmark,
    "nexus_vision": nexus_vision,
    "nexus_media": nexus_media,
    "nexus_dev": nexus_dev,
    "nexus_report": nexus_report,
}


__all__ = ["TOOLS", "NexusToolBox"]
