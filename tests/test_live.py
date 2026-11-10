"""Live integration tests against the real GMI Cloud API.

These tests are the only ones that talk to GMI Cloud, and they are **skipped
unless both** conditions hold:

* ``GMI_API_KEY`` is set to a real credential, and
* ``GMI_LIVE_TESTS=1`` is set — an explicit opt-in, so a normal ``pytest``
  run never spends money.

Run them with::

    GMI_LIVE_TESTS=1 GMI_API_KEY=... python -m pytest tests/test_live.py -v

The catalogue reads are free. The inference test makes one real request and
reports the provider's own usage; it is the only test here that costs
anything. No result in this file is simulated: if the API fails, the test
fails.

These are *live API* tests. They are not *Hermes plugin* tests: the plugin
is exercised end to end inside a real Hermes install by
``docs/HERMES_VALIDATION.md``, and the plugin's registration contract is
covered by ``tests/test_plugin.py``.
"""

from __future__ import annotations

import os

import pytest

from nexus.config import Settings, read_api_key
from nexus.http import HttpClient
from nexus.orchestrator import Nexus
from nexus.providers.gmi_llm import GmiLLMClient
from nexus.registry import ModelRegistry

#: The opt-in switch. Without it, nothing in this file reaches the network.
LIVE_FLAG = "GMI_LIVE_TESTS"

#: The smallest sensible request: one short completion.
PROBE_PROMPT = "Reply with exactly the word: NEXUS"


def _live_enabled() -> bool:
    return os.environ.get(LIVE_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def _key() -> str | None:
    return read_api_key()


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not _live_enabled(),
        reason=f"set {LIVE_FLAG}=1 (and GMI_API_KEY) to run the live GMI Cloud tests",
    ),
    pytest.mark.skipif(
        _key() is None,
        reason="GMI_API_KEY is not configured",
    ),
]


@pytest.fixture
def live_settings() -> Settings:
    """Settings pointed at the real GMI Cloud endpoints."""
    return Settings(
        llm_base_url=os.environ.get(
            "GMI_BASE_URL", "https://api.gmi-serving.com/v1"
        ).rstrip("/"),
        media_base_url=os.environ.get(
            "GMI_MEDIA_BASE_URL",
            "https://console.gmicloud.ai/api/v1/ie/requestqueue/apikey",
        ).rstrip("/"),
        request_timeout_s=60,
        max_retries=1,
        max_cost_per_run_usd=0.05,
        daily_budget_usd=0.50,
        approval_threshold_usd=0.05,
    )


@pytest.fixture
def live_registry(live_settings) -> ModelRegistry:
    client = HttpClient(timeout=live_settings.request_timeout_s, max_retries=1)
    return ModelRegistry(live_settings, client)


# --------------------------------------------------------------------------- #
# Free: catalogue reads
# --------------------------------------------------------------------------- #


def test_live_the_llm_catalogue_can_be_read(live_registry):
    """The catalogue is read live; no model ids are hard-coded anywhere."""
    models = live_registry.list_models()

    assert models, "the live catalogue returned no models"
    for model in models:
        assert model.id
        assert model.pricing is not None


def test_live_the_media_catalogue_can_be_read(live_registry):
    from nexus.models import Modality

    media = live_registry.list_media()

    assert media, "the live media catalogue returned no models"
    assert all(m.id for m in media)
    assert all(m.modality in (Modality.IMAGE, Modality.VIDEO, Modality.AUDIO) for m in media)


def test_live_every_generation_model_publishes_its_own_parameters(live_settings, live_registry):
    """Each model documents its own schema; NEXUS never reuses one model's
    parameters for another.

    The scope is the *product* models — the ones NEXUS can actually route
    to. The catalogue also carries account-private workflow entries, which
    are not selectable and would only add one HTTP request each.
    """
    from nexus.providers.gmi_media import GmiMediaClient

    client = HttpClient(timeout=live_settings.request_timeout_s, max_retries=1)
    media = GmiMediaClient(live_settings.media_base_url, client)

    specs = []
    for info in live_registry.list_media():
        if not info.product:
            continue
        spec = media.catalog.get_spec(info.id)
        if spec is not None:
            specs.append(spec)

    assert specs, "no generation model published a parameter schema"
    for spec in specs:
        assert spec.id
        # A schema with no parameters at all would mean the request could
        # not be built from the model's own documentation.
        assert spec.parameters or spec.modality, spec.id

    # The property that matters: two models of the same modality do not
    # share a parameter schema. If they did, NEXUS would be sending one
    # model's parameters to another.
    by_modality: dict[str, set[tuple[str, ...]]] = {}
    for spec in specs:
        by_modality.setdefault(spec.modality.value, set()).add(tuple(spec.parameter_names()))
    for modality, schemas in by_modality.items():
        assert len(schemas) > 1, (
            f"every {modality} model published the same parameter schema: "
            f"{schemas} — parameters are not being read per model"
        )


# --------------------------------------------------------------------------- #
# Costs money: one real inference call
# --------------------------------------------------------------------------- #


def test_live_a_real_model_answers(live_settings, live_registry):
    """One real chat completion, with the provider's own usage reported."""
    models = [m for m in live_registry.list_models() if m.is_ready]
    if not models:
        pytest.skip("the live catalogue returned no ready model")

    client = HttpClient(timeout=live_settings.request_timeout_s, max_retries=1)
    llm = GmiLLMClient(live_settings.llm_base_url, client)

    response = llm.chat(models[0].id, [{"role": "user", "content": PROBE_PROMPT}])

    assert response.text
    assert response.usage.prompt_tokens > 0
    assert response.usage.requests == 1


def test_live_the_pipeline_routes_and_reports(live_settings):
    """The whole eight-step pipeline against the real catalogue."""
    engine = Nexus(live_settings)
    try:
        analysis = engine.analyze("Summarise what a router does in one sentence.")
        assert analysis["plan"]["steps"]
        assert analysis["routing_preview"]
        # Analysis calls no model, so it spends nothing — and says so.
        assert analysis["budget"]["run_spend_usd"] == 0.0
        assert analysis["budget"]["daily_spend_usd"] == 0.0
    finally:
        engine.close()
