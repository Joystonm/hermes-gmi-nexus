"""Backend routes for the GMI NEXUS dashboard tab.

Hermes mounts this file at ``/api/plugins/gmi-nexus/``. It serves the numbers
the dashboard shows, and it serves **only real numbers**:

* the model catalogue is read live from GMI Cloud (a free read);
* the run history, the arena results and the routing statistics come from
  the plugin's own database;
* where no data exists, the answer says so instead of filling the gap with a
  placeholder. Every payload carries a ``basis`` field — ``measured``,
  ``estimated`` or ``none`` — so the UI can label an estimate as one.

Two rules shape the code below.

**The credential never appears.** It is read through Hermes' secret scope
only to answer "is one configured?", and only that boolean crosses this
boundary.

**The event loop is never blocked.** The catalogue reads are HTTP requests
and the history reads are SQLite queries, so every handler hands the work to
a worker thread with :func:`asyncio.to_thread`. A handler that blocked the
loop would delay every other dashboard request behind it.

Nothing here spends money. Reading the catalogue and reading the recorded
history are both free; generation is initiated by the agent through the
plugin's tools, where Hermes' approval policy applies.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException

router = APIRouter()

#: The plugin's name, as declared in ``plugin.yaml``. It is also the
#: namespace the routes are mounted under.
PLUGIN_NAME = "gmi-nexus"

#: The root of the plugin package — the folder that holds ``nexus/``.
PLUGIN_ROOT = Path(__file__).resolve().parents[1]

#: The private module name used when the agent half is not already loaded.
_PRIVATE_PACKAGE = "gmi_nexus_dashboard_package"

#: How many media models get their published description fetched. Each
#: description is one free GET; the whole catalogue would be a slow page.
MEDIA_SPEC_LIMIT = 12

_ENGINE: Any | None = None
_ENGINE_SETTINGS: dict[str, Any] | None = None
_ENGINE_LOCK = threading.RLock()


# --------------------------------------------------------------------------- #
# Importing the NEXUS package
# --------------------------------------------------------------------------- #


def _is_ours(module: Any) -> bool:
    """True when ``module`` was loaded from this plugin's folder."""
    try:
        origin = Path(getattr(module, "__file__", "") or "").resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    try:
        origin.relative_to(PLUGIN_ROOT)
    except (ValueError, RuntimeError):
        return False
    return True


def _nexus_package() -> Any:
    """Import the NEXUS package that ships beside this file.

    The agent half is loaded by Hermes as ``hermes_plugins.<slug>``; the
    dashboard half is loaded from this file with no search path of its own.
    When the agent half is already in memory it is preferred, so both halves
    share one module instance — and therefore one database connection and one
    copy of the routing statistics. Otherwise the copy next to this file is
    loaded under a private name, so it cannot shadow anything else in the
    process.
    """
    for name in list(sys.modules):
        if not name.startswith("hermes_plugins.") or not name.endswith(".nexus"):
            continue
        module = sys.modules.get(name)
        if module is None or not _is_ours(module):
            continue
        try:
            return importlib.import_module(name)
        except Exception:  # pragma: no cover - defensive
            continue

    existing = sys.modules.get(_PRIVATE_PACKAGE)
    if existing is not None:
        return existing

    package_dir = PLUGIN_ROOT / "nexus"
    init_file = package_dir / "__init__.py"
    if not init_file.is_file():
        raise ImportError(f"NEXUS package not found next to the dashboard: {package_dir}")
    spec = importlib.util.spec_from_file_location(
        _PRIVATE_PACKAGE, init_file, submodule_search_locations=[str(package_dir)]
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"NEXUS package could not be loaded from {package_dir}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PRIVATE_PACKAGE] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_PRIVATE_PACKAGE, None)
        raise
    return module


def _module(package: Any, name: str) -> Any:
    """Import a submodule of the NEXUS package."""
    return importlib.import_module(f"{package.__name__}.{name}")


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


class _ConfigShim:
    """The slice of Hermes' plugin context that settings resolution needs.

    The agent half reads its settings through ``ctx.get_config``, which looks
    up ``plugins.entries.<plugin-id>.settings`` in ``config.yaml``. The
    dashboard half has no plugin context, so it reads the same subtree and
    hands it over as this shim — the resolution itself stays in one place
    (``nexus.config.resolve_settings``), so both halves agree on every value.
    """

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def get_config(self, key: str, default: Any = None) -> Any:
        if key in self._values:
            return self._values[key]
        # The provider publishes GMI_BASE_URL; honour it when the operator
        # has not set a base URL in config.yaml.
        if key == "llm_base_url":
            env = os.environ.get("GMI_BASE_URL", "").strip()
            if env:
                return env
        return default


def _config_values() -> dict[str, Any]:
    """Read ``plugins.entries.gmi-nexus`` from ``config.yaml``."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception:
        return {}
    try:
        entries = ((config.get("plugins") or {}).get("entries") or {})
        entry = entries.get(PLUGIN_NAME) or {}
    except AttributeError:
        return {}
    values: dict[str, Any] = {}
    settings = entry.get("settings")
    if isinstance(settings, dict):
        values.update(settings)
    legacy = entry.get("config")
    if isinstance(legacy, dict):
        for key, value in legacy.items():
            values.setdefault(key, value)
    return values


def _settings() -> Any:
    """Resolve the NEXUS settings exactly as the agent half does."""
    package = _nexus_package()
    config = _module(package, "config")
    return config.resolve_settings(_ConfigShim(_config_values()))


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #


def _engine() -> Any:
    """Return the engine, rebuilding it when the settings changed."""
    global _ENGINE, _ENGINE_SETTINGS
    settings = _settings()
    fingerprint = settings.to_dict()
    with _ENGINE_LOCK:
        if _ENGINE is not None and _ENGINE_SETTINGS == fingerprint:
            return _ENGINE
        if _ENGINE is not None:
            try:
                _ENGINE.close()
            except Exception:  # pragma: no cover - defensive
                pass
        package = _nexus_package()
        orchestrator = _module(package, "orchestrator")
        _ENGINE = orchestrator.Nexus(settings)
        _ENGINE_SETTINGS = fingerprint
        return _ENGINE


def _credential_present() -> bool:
    """True when a GMI Cloud credential is configured.

    The credential itself is never read into a value that leaves this
    module — only its presence is reported.
    """
    package = _nexus_package()
    config = _module(package, "config")
    try:
        return bool(config.read_api_key())
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _basis(payload: dict[str, Any]) -> dict[str, Any]:
    """Stamp a payload with where its numbers came from."""
    payload.setdefault("basis", "measured")
    payload.setdefault("generated_at", time.time())
    return payload


def _empty(reason: str, **extra: Any) -> dict[str, Any]:
    """An honest empty state: no data, and why there is none."""
    payload: dict[str, Any] = {
        "basis": "none",
        "empty": True,
        "empty_reason": reason,
    }
    payload.update(extra)
    return _basis(payload)


def _decode_error(value: Any) -> Any:
    """Decode a stored benchmark error back into its original form.

    The error is stored JSON-encoded so a structured failure survives; the
    reader wants the message, not the encoding.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return value
        return decoded
    return value


def _guard(func: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run a blocking reader, turning a failure into a readable error."""
    try:
        return func()
    except HTTPException:
        # A deliberate refusal (an unknown run id) keeps its status code.
        raise
    except Exception as exc:
        return _basis(
            {
                "basis": "none",
                "error": f"{type(exc).__name__}: {exc}",
                "empty": True,
                "empty_reason": "The request could not be completed.",
            }
        )


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #


def _overview(days: int) -> dict[str, Any]:
    """The landing view: catalogue, budget, spend and recent runs."""
    package = _nexus_package()
    storage = _module(package, "storage")
    engine = _engine()

    catalogue_error = None
    try:
        engine.registry.list_models()
    except Exception as exc:
        catalogue_error = f"{type(exc).__name__}: {exc}"

    summary = engine.registry.summary()
    budget = engine.budget.snapshot()
    usage = storage.usage_summary(days=days)
    series = storage.usage_series(days=days)
    runs = storage.recent_runs(limit=10)
    stats = storage.model_stats()

    return _basis(
        {
            "catalogue": summary,
            "catalogue_source": engine.registry.source_description(),
            "catalogue_error": catalogue_error,
            "budget": budget,
            "usage": usage,
            "series": series,
            "recent_runs": runs,
            "model_stats": stats,
            "credential_configured": _credential_present(),
            "spend_note": (
                "Spend is the provider's reported usage priced at the "
                "catalogue's published rates — an estimate, not a bill."
            ),
            "empty": not runs and not usage["events"],
            "empty_reason": (
                "No NEXUS run has been recorded yet. Run nexus_run to create one."
                if not runs and not usage["events"]
                else ""
            ),
        }
    )


# --------------------------------------------------------------------------- #
# Model intelligence
# --------------------------------------------------------------------------- #


def _models(limit: int = 200) -> dict[str, Any]:
    """The live LLM catalogue and the measured performance of each model."""
    package = _nexus_package()
    storage = _module(package, "storage")
    engine = _engine()

    catalogue_error = None
    try:
        models = engine.registry.list_models()
    except Exception as exc:
        catalogue_error = f"{type(exc).__name__}: {exc}"
        models = []

    stats = {row["model_id"]: row for row in storage.model_stats()}
    payload_models = []
    for model in models[:limit]:
        item = model.to_dict()
        item["measured"] = stats.get(model.id)
        payload_models.append(item)

    return _basis(
        {
            "count": len(models),
            "models": payload_models,
            "truncated": len(models) > limit,
            "source": engine.registry.source_description(),
            "catalogue_error": catalogue_error,
            "empty": not models,
            "empty_reason": (
                "The GMI Cloud catalogue could not be read."
                if not models
                else ""
            ),
            "note": (
                "Capabilities come from the catalogue's own metadata or from a "
                "probe NEXUS ran; a capability without evidence is unverified."
            ),
        }
    )


# --------------------------------------------------------------------------- #
# Arena
# --------------------------------------------------------------------------- #


def _arena(limit: int = 20) -> dict[str, Any]:
    """Recorded benchmark runs and their per-model results."""
    package = _nexus_package()
    storage = _module(package, "storage")

    benchmarks = storage.list_benchmarks(limit=limit)
    results: dict[str, list[dict[str, Any]]] = {}
    for benchmark in benchmarks:
        rows = storage.benchmark_results(benchmark["id"])
        for row in rows:
            # The error is stored as JSON so it can carry a structured
            # failure; decode it back for the reader.
            row["error"] = _decode_error(row.get("error"))
        results[benchmark["id"]] = rows

    return _basis(
        {
            "benchmarks": benchmarks,
            "results": results,
            "empty": not benchmarks,
            "empty_reason": (
                "No benchmark has been run yet. nexus_benchmark measures the "
                "same test cases across the models you name."
                if not benchmarks
                else ""
            ),
            "note": (
                "Every score here comes from a real request. Nothing is "
                "simulated and nothing is extrapolated."
            ),
        }
    )


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def _execution(days: int = 30, run_id: str | None = None) -> dict[str, Any]:
    """The recorded runs, their steps, and the routing statistics."""
    package = _nexus_package()
    storage = _module(package, "storage")

    if run_id:
        detail = storage.run_detail(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"No run with id '{run_id}' is recorded.")
        return _basis({"run": detail, "runs": storage.recent_runs(limit=25)})

    runs = storage.recent_runs(limit=25)
    stats = storage.model_stats()
    probes = storage.capability_probes()
    return _basis(
        {
            "runs": runs,
            "model_stats": stats,
            "capability_probes": probes,
            "usage": storage.usage_summary(days=days),
            "empty": not runs,
            "empty_reason": (
                "No run has been recorded yet — run nexus_run to create one."
                if not runs
                else ""
            ),
        }
    )


# --------------------------------------------------------------------------- #
# Multimodal
# --------------------------------------------------------------------------- #


def _multimodal(limit: int = MEDIA_SPEC_LIMIT) -> dict[str, Any]:
    """The image, video and audio catalogue with each model's own schema."""
    package = _nexus_package()
    storage = _module(package, "storage")
    engine = _engine()

    catalogue_error = None
    try:
        media = engine.registry.list_media()
    except Exception as exc:
        catalogue_error = f"{type(exc).__name__}: {exc}"
        media = []

    described = []
    for info in media[:limit]:
        spec = None
        try:
            spec = engine.media.catalog.get_spec(info.id)
        except Exception:
            spec = None
        item = info.to_dict()
        item["spec"] = spec.to_dict() if spec is not None else None
        item["spec_available"] = spec is not None
        described.append(item)

    by_modality: dict[str, int] = {}
    for info in media:
        by_modality[info.modality.value] = by_modality.get(info.modality.value, 0) + 1

    requests = storage.list_media_requests(limit=40)
    return _basis(
        {
            "count": len(media),
            "by_modality": by_modality,
            "models": described,
            "described": len(described),
            "truncated": len(media) > limit,
            "requests": requests,
            "source": engine.registry.source_description(),
            "catalogue_error": catalogue_error,
            "empty": not media,
            "empty_reason": (
                "The GMI Cloud generation catalogue could not be read."
                if not media
                else ""
            ),
            "note": (
                "Each model's own published description decides the request "
                "that is built for it — NEXUS never reuses one model's "
                "parameters for another."
            ),
        }
    )


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def _settings_payload() -> dict[str, Any]:
    """The resolved settings and the limits they impose."""
    engine = _engine()
    settings = engine.settings
    return _basis(
        {
            "settings": settings.to_dict(),
            "budget": engine.budget.snapshot(),
            "credential_configured": _credential_present(),
            "credential_note": (
                "The credential lives in Hermes' secret scope. NEXUS reads it "
                "from there and never writes it into the plugin, the logs or "
                "this response."
            ),
            "note": (
                "These are the settings in force right now, resolved from "
                "config.yaml, the environment and the plugin manifest."
            ),
        }
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("/overview")
async def overview(days: int = 30) -> dict[str, Any]:
    """Catalogue, budget, spend and recent runs."""
    return await asyncio.to_thread(_guard, lambda: _overview(max(1, min(days, 365))))


@router.get("/models")
async def models(limit: int = 200) -> dict[str, Any]:
    """The live language-model catalogue with measured performance."""
    return await asyncio.to_thread(_guard, lambda: _models(max(1, min(limit, 500))))


@router.get("/arena")
async def arena(limit: int = 20) -> dict[str, Any]:
    """Recorded benchmark runs and their results."""
    return await asyncio.to_thread(_guard, lambda: _arena(max(1, min(limit, 100))))


@router.get("/execution")
async def execution(days: int = 30, run_id: str | None = None) -> dict[str, Any]:
    """Recorded runs, their steps, and the routing statistics."""
    return await asyncio.to_thread(
        _guard, lambda: _execution(max(1, min(days, 365)), run_id)
    )


@router.get("/multimodal")
async def multimodal(limit: int = MEDIA_SPEC_LIMIT) -> dict[str, Any]:
    """The generation catalogue and the recorded generation history."""
    return await asyncio.to_thread(
        _guard, lambda: _multimodal(max(1, min(limit, 60)))
    )


@router.get("/settings")
async def settings() -> dict[str, Any]:
    """The settings in force, and the spend limits they impose."""
    return await asyncio.to_thread(_guard, _settings_payload)


__all__ = ["MEDIA_SPEC_LIMIT", "PLUGIN_NAME", "router"]
