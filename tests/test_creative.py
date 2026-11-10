"""The Creative Engine: model choice, payload building, pricing and failure."""

from __future__ import annotations

import pytest

from nexus.budget import BudgetGuard
from nexus.config import Settings
from nexus.http import HttpClient
from nexus.media_catalog import build_payload, parse_spec
from nexus.media_router import MediaRouter, is_product_model, likely_modality
from nexus.models import Modality
from nexus.modules.creative import CreativeEngine
from nexus.providers.gmi_media import GmiMediaClient
from nexus.registry import ModelRegistry

from conftest import SAMPLE_IMAGE_SPEC, SAMPLE_VIDEO_SPEC, FakeTransport


def _creative(settings, transport) -> CreativeEngine:
    client = HttpClient(transport=transport, max_retries=0, timeout=5.0)
    registry = ModelRegistry(settings, client)
    media = GmiMediaClient(settings.media_base_url, client)
    return CreativeEngine(settings, registry, media, MediaRouter(media.catalog), BudgetGuard(settings))


def _submitted(transport) -> dict:
    """Return the body of the single submitted generation request."""
    posts = [
        entry
        for entry in transport.requests
        if entry["method"] == "POST" and entry["url"].endswith("/requests")
    ]
    assert len(posts) == 1, f"expected exactly one submission, got {len(posts)}"
    return posts[0]["body"]


# --------------------------------------------------------------------------- #
# Choosing a model
# --------------------------------------------------------------------------- #


def test_a_private_workflow_is_never_selected():
    assert is_product_model("550e8400-e29b-41d4-a716-446655440000") is False
    assert is_product_model("gpt-image-2") is True


def test_a_model_with_no_published_description_is_rejected_with_a_reason(settings):
    transport = FakeTransport()
    creative = _creative(settings, transport)

    result = creative.generate("a cat", modality=Modality.IMAGE)
    # seedream-4-0 has no published description in the mock, so it cannot be
    # used; gpt-image-2 can.
    assert result.ok is True
    assert result.model_id == "gpt-image-2"

    rejected = {c["model_id"]: c.get("rejected") for c in result.candidates}
    assert rejected["seedream-4-0"]
    assert "no published description" in rejected["seedream-4-0"]


def test_a_video_request_picks_the_video_model(settings):
    transport = FakeTransport()
    creative = _creative(settings, transport)

    result = creative.generate("a drone shot", modality=Modality.VIDEO)
    assert result.ok is True
    assert result.model_id == "Minimax-Hailuo-2.3"


def test_an_unknown_modality_leaves_the_model_id_undecided():
    # An id that names no modality is not guessed at.
    assert likely_modality("Minimax-Hailuo-2.3") == Modality.VIDEO
    assert likely_modality("gpt-image-2") == Modality.IMAGE
    assert likely_modality("elevenlabs-tts-v3") == Modality.AUDIO


# --------------------------------------------------------------------------- #
# Building the payload
# --------------------------------------------------------------------------- #


def test_the_payload_is_built_from_the_models_own_schema(settings):
    transport = FakeTransport()
    creative = _creative(settings, transport)

    creative.generate(
        "a red cube on white",
        modality=Modality.IMAGE,
        options={"aspect_ratio": "16:9", "n": 2},
    )

    body = _submitted(transport)
    assert body["model"] == "gpt-image-2"
    payload = body["payload"]
    assert payload["prompt"] == "a red cube on white"
    # The model calls the size "aspect_ratio" and only accepts these values.
    assert payload["aspect_ratio"] == "1344x768"
    assert payload["n"] == 2


def test_an_option_the_model_does_not_accept_is_reported_as_dropped(settings):
    transport = FakeTransport()
    creative = _creative(settings, transport)

    result = creative.generate(
        "a red cube",
        modality=Modality.IMAGE,
        options={"duration": 12},
    )
    assert "duration" in result.dropped_options
    assert "duration" not in _submitted(transport)["payload"]


def test_a_video_model_gets_its_own_parameter_names(settings):
    transport = FakeTransport()
    creative = _creative(settings, transport)

    creative.generate("a drone shot", modality=Modality.VIDEO, options={"duration": 8})

    payload = _submitted(transport)["payload"]
    # The video model spells the duration "durationSeconds".
    assert payload["durationSeconds"] == 8
    assert "duration" not in payload


def test_the_prompt_goes_where_the_model_keeps_its_prompt():
    spec = parse_spec(SAMPLE_IMAGE_SPEC, "gpt-image-2")
    payload, dropped = build_payload(spec, "hello", {"aspect_ratio": "1:1"})
    assert payload["prompt"] == "hello"
    assert payload["aspect_ratio"] == "1024x1024"
    assert dropped == []


def test_a_video_spec_defaults_its_own_parameters():
    spec = parse_spec(SAMPLE_VIDEO_SPEC, "Minimax-Hailuo-2.3")
    payload, _ = build_payload(spec, "a drone shot", {})
    assert payload["prompt"] == "a drone shot"
    assert payload["durationSeconds"] == 5  # the model's own default


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #


def test_an_image_is_priced_at_the_published_rate(settings):
    transport = FakeTransport()
    creative = _creative(settings, transport)

    estimate = creative.estimate(Modality.IMAGE, "a cat")
    # 42000 micro-USD per image is $0.042.
    assert estimate["estimated_cost_usd"] == pytest.approx(0.042)
    assert estimate["unit"] == "image"


def test_a_video_is_priced_per_second(settings):
    transport = FakeTransport()
    creative = _creative(settings, transport)

    estimate = creative.estimate(Modality.VIDEO, "a drone shot", {"duration": 10})
    # 56000 micro-USD per second is $0.056 per second.
    assert estimate["estimated_cost_usd"] == pytest.approx(0.56)
    assert estimate["unit"] == "second"


def test_the_estimate_is_labelled_as_an_estimate(settings):
    transport = FakeTransport()
    creative = _creative(settings, transport)

    estimate = creative.estimate(Modality.IMAGE, "a cat")
    assert "estimate" in estimate["note"].lower()


def test_a_generation_over_the_approval_threshold_is_held_back():
    strict = Settings(
        llm_base_url="https://mock.gmi/v1",
        media_base_url="https://mock.gmi/apikey",
        approval_threshold_usd=0.01,
        max_cost_per_run_usd=5.0,
        daily_budget_usd=50.0,
    )
    transport = FakeTransport()
    creative = _creative(strict, transport)

    result = creative.generate("a cat", modality=Modality.IMAGE)
    assert result.ok is False
    assert result.status == "blocked"
    assert result.media_urls == []
    # Nothing was submitted, so nothing was spent.
    assert not [
        entry for entry in transport.requests if entry["url"].endswith("/requests")
    ]


def test_an_approved_generation_over_the_threshold_proceeds():
    strict = Settings(
        llm_base_url="https://mock.gmi/v1",
        media_base_url="https://mock.gmi/apikey",
        approval_threshold_usd=0.01,
        max_cost_per_run_usd=5.0,
        daily_budget_usd=50.0,
    )
    transport = FakeTransport()
    creative = _creative(strict, transport)

    result = creative.generate("a cat", modality=Modality.IMAGE, approved=True)
    assert result.ok is True


# --------------------------------------------------------------------------- #
# Outcomes
# --------------------------------------------------------------------------- #


def test_a_successful_generation_returns_the_media_urls(settings):
    transport = FakeTransport()
    creative = _creative(settings, transport)

    result = creative.generate("a cat", modality=Modality.IMAGE)
    assert result.ok is True
    assert result.status == "success"
    assert result.media_urls == ["https://cdn.gmicloud.ai/out-1.png"]
    assert result.request_id == "req-mock-1"


def test_a_failed_generation_is_reported_as_a_failure(settings):
    transport = FakeTransport(media_status="failed")
    creative = _creative(settings, transport)

    result = creative.generate("a cat", modality=Modality.IMAGE)
    assert result.ok is False
    assert result.status == "failed"
    assert result.media_urls == []
    assert "refused" in (result.error or "")


def test_a_failed_generation_is_not_recorded_as_spend(settings):
    transport = FakeTransport(media_status="failed")
    creative = _creative(settings, transport)

    creative.generate("a cat", modality=Modality.IMAGE)
    assert creative.budget.run_spend == 0.0


def test_an_identical_request_is_not_submitted_twice(settings):
    from nexus.providers.gmi_media import clear_duplicates

    clear_duplicates()
    transport = FakeTransport()
    creative = _creative(settings, transport)

    first = creative.generate("a cat", modality=Modality.IMAGE)
    second = creative.generate("a cat", modality=Modality.IMAGE)

    assert first.ok and second.ok
    assert second.deduplicated is True
    assert second.estimated_cost_usd == 0.0  # the duplicate was not billed
    assert len(
        [e for e in transport.requests if e["method"] == "POST" and e["url"].endswith("/requests")]
    ) == 1


def test_a_generation_without_a_prompt_is_refused(settings):
    from nexus.errors import ValidationError

    transport = FakeTransport()
    creative = _creative(settings, transport)

    with pytest.raises(ValidationError):
        creative.generate("   ", modality=Modality.IMAGE)


def test_the_media_models_are_published_with_their_own_parameters(settings):
    transport = FakeTransport()
    client = HttpClient(transport=transport, max_retries=0, timeout=5.0)
    registry = ModelRegistry(settings, client)

    media = {m.id: m for m in registry.list_media(Modality.IMAGE)}
    assert "gpt-image-2" in media
    # A private workflow is listed — the catalogue publishes it — but it is
    # marked as not a product model, so it is never selected.
    private = media.get("550e8400-e29b-41d4-a716-446655440000")
    assert private is not None
    assert private.product is False
    assert media["gpt-image-2"].product is True
