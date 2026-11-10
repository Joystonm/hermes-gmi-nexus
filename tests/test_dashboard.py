"""Tests for the dashboard backend.

The dashboard is a reader over the same data the agent half writes, so these
tests check three things:

* every route answers, and answers with the shape the UI expects;
* where there is no data, the answer says so instead of inventing a value;
* the credential never crosses the boundary — the payload reports whether a
  key is configured, never the key itself.

Nothing here reaches the network. The catalogue is stubbed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "gmi-nexus"))

from nexus.config import Settings  # noqa: E402
from nexus.models import Capability, MediaModelInfo, Modality, ModelInfo, Pricing  # noqa: E402

#: A credential that is obviously not real. The tests use it to check that the
#: key is read and then never disclosed.
FAKE_KEY = "gmi-test-key-not-a-real-credential"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


class _StubRoute:
    """One registered route."""

    def __init__(self, path: str, endpoint: Any) -> None:
        self.path = path
        self.endpoint = endpoint


class _StubRouter:
    """A stand-in for ``fastapi.APIRouter``.

    The plugin's backend only ever *registers* routes, so a minimal
    substitute exercises the same code. It is needed because a fastapi and a
    starlette that disagree with each other cannot construct a real router —
    an environment fault that breaks Hermes' own dashboard too, and one that
    this plugin cannot fix from the inside. ``test_the_environment_can_build_a_fastapi_router``
    reports on it.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.routes: list[_StubRoute] = []

    def get(self, path: str, **kwargs: Any):
        def decorator(func):
            self.routes.append(_StubRoute(path, func))
            return func

        return decorator


@pytest.fixture
def dashboard(monkeypatch):
    """The dashboard backend, loaded the way Hermes loads it."""
    import fastapi

    root = Path(__file__).resolve().parents[1] / "plugins" / "gmi-nexus"
    name = "gmi_nexus_dashboard_under_test"
    spec = importlib.util.spec_from_file_location(
        name, root / "dashboard" / "plugin_api.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module

    try:
        fastapi.APIRouter()
    except TypeError:
        monkeypatch.setattr(fastapi, "APIRouter", _StubRouter)

    spec.loader.exec_module(module)
    # The engine is memoised at module level; each test starts from scratch.
    module._ENGINE = None
    module._ENGINE_SETTINGS = None
    yield module
    module._ENGINE = None
    module._ENGINE_SETTINGS = None


class _StubRegistry:
    """A catalogue that never touches the network."""

    def __init__(self, models: list[ModelInfo], media: list[MediaModelInfo]) -> None:
        self._models = models
        self._media = media

    def list_models(self, refresh: bool = False) -> list[ModelInfo]:
        return list(self._models)

    def list_media(self, modality: str | None = None, refresh: bool = False) -> list[MediaModelInfo]:
        return list(self._media)

    def summary(self) -> dict[str, Any]:
        return {
            "llm_models": len(self._models),
            "media_models": len(self._media),
            "media_by_modality": {"image": len(self._media)},
            "vision_capable": sum(
                1 for m in self._models if Capability.VISION in m.capabilities
            ),
            "tool_capable": sum(
                1 for m in self._models if Capability.TOOLS in m.capabilities
            ),
            "fetched_at": time.time(),
            "catalogue_age_seconds": 0.0,
            "error": None,
        }

    def source_description(self) -> dict[str, Any]:
        return {
            "endpoint": "https://mock.gmi/v1/models",
            "media_endpoint": "https://mock.gmi/apikey/models",
            "fetched_at": time.time(),
            "age_seconds": 0.0,
            "from_cache": False,
            "error": None,
            "note": "stubbed for the tests",
        }


class _StubCatalog:
    def get_spec(self, model_id: str, refresh: bool = False):
        return None


class _StubMedia:
    def __init__(self) -> None:
        self.catalog = _StubCatalog()


class _StubBudget:
    def snapshot(self) -> dict[str, Any]:
        return {
            "daily_budget_usd": 50.0,
            "daily_spend_usd": 0.25,
            "daily_remaining_usd": 49.75,
            "max_cost_per_run_usd": 5.0,
            "approval_threshold_usd": 10.0,
            "note": "estimates at list price",
        }


class _StubEngine:
    """The engine the dashboard reads from."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.registry = _StubRegistry(
            [
                ModelInfo(
                    id="mock/text-model",
                    name="Mock Text Model",
                    owned_by="mock",
                    context_length=8192,
                    pricing=Pricing(prompt=0.0000002, completion=0.0000006),
                    capabilities=(Capability.TEXT,),
                    capability_evidence={"text": "metadata"},
                ),
                ModelInfo(
                    id="mock/vision-model",
                    name="Mock Vision Model",
                    owned_by="mock",
                    context_length=4096,
                    pricing=Pricing(prompt=0.0000001, completion=0.0000002),
                    capabilities=(Capability.TEXT, Capability.VISION),
                    capability_evidence={"text": "metadata", "vision": "probe"},
                ),
            ],
            [MediaModelInfo(id="mock/image-model", modality=Modality.IMAGE, description="A stub")],
        )
        self.media = _StubMedia()
        self.budget = _StubBudget()

    def close(self) -> None:
        return None


@pytest.fixture
def stubbed(dashboard, settings, monkeypatch):
    """A dashboard whose catalogue is stubbed and whose engine is inert.

    The engine is a single instance, so a test can reach in and make the
    catalogue fail.
    """
    engine = _StubEngine(settings)
    monkeypatch.setattr(dashboard, "_engine", lambda: engine)
    monkeypatch.setattr(dashboard, "_settings", lambda: settings)
    return dashboard


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# The import boundary
# --------------------------------------------------------------------------- #


def test_the_environment_can_build_a_fastapi_router():
    """The host must be able to construct a router to mount this plugin's.

    A fastapi and a starlette that disagree about the ``Router`` signature
    break this — and break Hermes' own dashboard with it, since the dashboard
    server is built on the same library. Hermes pins a matching pair in its
    ``web`` extra; this test says so plainly rather than hiding it.
    """
    import fastapi

    try:
        fastapi.APIRouter()
    except TypeError as exc:
        pytest.skip(
            "this environment cannot build a fastapi router "
            f"({exc}); Hermes' own dashboard needs the same fix — install "
            'fastapi==0.133.1 and starlette==1.9.0, as Hermes pins them'
        )


def test_the_dashboard_finds_its_own_package(dashboard):
    """The dashboard loads the NEXUS package that ships beside it."""
    package = dashboard._nexus_package()

    assert package is not None
    assert hasattr(package, "__path__")


def test_the_dashboard_routes_are_mounted(dashboard):
    """Every documented route is present on the router."""
    paths = {route.path for route in dashboard.router.routes}

    assert paths == {
        "/overview",
        "/models",
        "/arena",
        "/execution",
        "/multimodal",
        "/settings",
    }


def test_every_route_is_async(dashboard):
    """A route that blocked the event loop would stall the whole dashboard."""
    for route in dashboard.router.routes:
        assert inspect.iscoroutinefunction(route.endpoint), route.path


# --------------------------------------------------------------------------- #
# Reading real data
# --------------------------------------------------------------------------- #


def test_overview_reports_the_catalogue_and_the_budget(stubbed):
    payload = run(stubbed.overview(days=30))

    assert payload["catalogue"]["llm_models"] == 2
    assert payload["catalogue"]["vision_capable"] == 1
    assert payload["budget"]["daily_budget_usd"] == 50.0
    assert payload["credential_configured"] is True
    assert payload["basis"] == "measured"


def test_overview_is_empty_before_anything_has_run(stubbed):
    """With no history, the payload says so — it does not invent a zero."""
    payload = run(stubbed.overview(days=30))

    assert payload["empty"] is True
    assert "nexus_run" in payload["empty_reason"]
    assert payload["recent_runs"] == []
    assert payload["series"] == []


def test_models_carry_their_measured_performance(stubbed, dashboard):
    """The catalogue is shown; the measurement column stays empty until there
    is a measurement to show."""
    package = dashboard._nexus_package()
    storage = dashboard._module(package, "storage")
    storage.update_model_stats(
        "mock/text-model", "general_reasoning", success=True, latency_ms=420, cost_usd=0.002
    )

    payload = run(stubbed.models())

    by_id = {model["id"]: model for model in payload["models"]}
    assert set(by_id) == {"mock/text-model", "mock/vision-model"}
    assert by_id["mock/text-model"]["measured"]["successes"] == 1
    assert by_id["mock/vision-model"]["measured"] is None


def test_models_keep_their_capability_evidence(stubbed):
    payload = run(stubbed.models())

    by_id = {model["id"]: model for model in payload["models"]}
    assert by_id["mock/vision-model"]["capability_evidence"]["vision"] == "probe"


def test_execution_lists_recorded_runs(stubbed, dashboard):
    package = dashboard._nexus_package()
    storage = dashboard._module(package, "storage")
    storage.record_run("run-1", "Do the thing", "general_reasoning")
    storage.finish_run("run-1", "completed", 0.01, 1200, ["mock/text-model"])
    storage.update_model_stats(
        "mock/text-model", "general_reasoning", success=True, latency_ms=900, cost_usd=0.01
    )

    payload = run(stubbed.execution(days=30))

    assert len(payload["runs"]) == 1
    assert payload["runs"][0]["id"] == "run-1"
    assert payload["runs"][0]["models_used"] == ["mock/text-model"]
    assert payload["model_stats"][0]["model_id"] == "mock/text-model"


def test_execution_returns_one_run_with_its_steps(stubbed, dashboard):
    package = dashboard._nexus_package()
    storage = dashboard._module(package, "storage")
    storage.record_run("run-2", "Build a page", "code_generation")
    storage.record_step(
        "run-2",
        {
            "step_index": 0,
            "description": "Write the markup",
            "task_type": "code_generation",
            "status": "ok",
            "model_id": "mock/text-model",
            "output": "<html></html>",
            "latency_ms": 800,
            "attempts": 1,
            "usage": {"prompt_tokens": 120, "completion_tokens": 60, "cost_usd": 0.004},
        },
    )

    payload = run(stubbed.execution(days=30, run_id="run-2"))

    assert payload["run"]["goal"] == "Build a page"
    assert len(payload["run"]["steps"]) == 1
    assert payload["run"]["steps"][0]["model_id"] == "mock/text-model"


def test_execution_refuses_an_unknown_run_id(stubbed):
    """A deliberate refusal keeps its status code rather than becoming a 200."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        run(stubbed.execution(days=30, run_id="does-not-exist"))

    assert raised.value.status_code == 404


def test_arena_reports_recorded_benchmarks(stubbed, dashboard):
    package = dashboard._nexus_package()
    storage = dashboard._module(package, "storage")
    storage.create_benchmark("bench-1", "reasoning", ["mock/text-model", "mock/vision-model"])
    storage.record_benchmark_result(
        "bench-1",
        {
            "model_id": "mock/text-model",
            "task_index": 0,
            "task": "Name a router",
            "latency_ms": 300,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "cost_usd": 0.001,
            "score": 0.9,
            "passed": True,
            "output": "A router.",
            "error": None,
        },
    )
    storage.record_benchmark_result(
        "bench-1",
        {
            "model_id": "mock/vision-model",
            "task_index": 0,
            "task": "Name a router",
            "latency_ms": 900,
            "prompt_tokens": 10,
            "completion_tokens": 0,
            "cost_usd": 0.002,
            "score": 0.2,
            "passed": False,
            "output": "",
            "error": "the provider rejected the request",
        },
    )
    storage.finish_benchmark("bench-1", "completed", {"models": 2})

    payload = run(stubbed.arena())

    assert len(payload["benchmarks"]) == 1
    results = payload["results"]["bench-1"]
    assert len(results) == 2
    # The stored error is JSON-encoded; the reader gets the message back.
    failed = [row for row in results if row["model_id"] == "mock/vision-model"][0]
    assert failed["error"] == "the provider rejected the request"


def test_multimodal_lists_the_generation_catalogue(stubbed):
    payload = run(stubbed.multimodal())

    assert payload["count"] == 1
    assert payload["by_modality"]["image"] == 1
    assert payload["models"][0]["id"] == "mock/image-model"
    # No published schema means NEXUS will not build a request for the model.
    assert payload["models"][0]["spec"] is None
    assert payload["models"][0]["spec_available"] is False


def test_multimodal_lists_recorded_generations(stubbed, dashboard):
    package = dashboard._nexus_package()
    storage = dashboard._module(package, "storage")
    storage.record_media_request("req-1", "mock/image-model", "hash-1")
    storage.update_media_request(
        "req-1", status="success", media_urls=["https://example.invalid/a.png"], cost_usd=0.04
    )

    payload = run(stubbed.multimodal())

    assert len(payload["requests"]) == 1
    assert payload["requests"][0]["media_urls"] == ["https://example.invalid/a.png"]
    assert payload["requests"][0]["cost_usd"] == 0.04


def test_settings_report_the_configuration_in_force(stubbed):
    payload = run(stubbed.settings())

    assert payload["settings"]["llm_base_url"] == "https://mock.gmi/v1"
    assert payload["credential_configured"] is True


# --------------------------------------------------------------------------- #
# The credential boundary
# --------------------------------------------------------------------------- #


def test_the_credential_never_appears_in_any_payload(stubbed, monkeypatch):
    """The key is read to answer "is one configured?", and nothing else."""
    monkeypatch.setenv("GMI_API_KEY", FAKE_KEY)

    payloads = [
        run(stubbed.overview(days=30)),
        run(stubbed.models()),
        run(stubbed.arena()),
        run(stubbed.execution(days=30)),
        run(stubbed.multimodal()),
        run(stubbed.settings()),
    ]

    for payload in payloads:
        assert FAKE_KEY not in json.dumps(payload, default=str)


def test_a_missing_credential_is_reported_as_missing(stubbed, monkeypatch):
    monkeypatch.delenv("GMI_API_KEY", raising=False)

    payload = run(stubbed.settings())

    assert payload["credential_configured"] is False


# --------------------------------------------------------------------------- #
# The failure path
# --------------------------------------------------------------------------- #


def test_a_broken_catalogue_is_reported_not_hidden(stubbed, monkeypatch):
    """A catalogue read that fails is surfaced as an error, not as an empty
    catalogue that looks like good news."""
    engine = stubbed._engine()
    model = engine.registry.list_models()[0]

    def _boom(refresh: bool = False):
        raise RuntimeError("the catalogue endpoint refused the connection")

    monkeypatch.setattr(engine.registry, "list_models", _boom)

    payload = run(stubbed.overview(days=30))

    assert payload["catalogue_error"]
    assert "refused the connection" in payload["catalogue_error"]
    assert model.id  # the stub catalogue itself is intact
