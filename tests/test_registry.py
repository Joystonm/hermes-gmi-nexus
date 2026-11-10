"""Model discovery and capability detection."""

from __future__ import annotations

import pytest

from nexus.models import PROBE_VERIFIED_PREFIX, Capability, Modality, probe_verdict
from nexus.registry import (
    ModelRegistry,
    catalogue_error_suffix,
    classify_media_modality,
    infer_capabilities,
    parse_model_record,
    parse_pricing,
)
from nexus.http import HttpClient

from conftest import FakeTransport, SAMPLE_CATALOGUE


def test_prices_are_strings_in_the_catalogue_and_floats_after_parsing():
    raw = SAMPLE_CATALOGUE["data"][0]["pricing"]
    assert raw["prompt"] == "0.000000200"  # the API publishes strings
    pricing = parse_pricing(raw, discount=0.4)
    assert pricing.prompt == pytest.approx(0.0000002)
    assert pricing.discount_to_user == 0.4


def test_cost_applies_the_account_discount():
    pricing = parse_pricing({"prompt": "0.000001", "completion": "0.000002"}, discount=0.5)
    cost = pricing.cost(prompt_tokens=1000, completion_tokens=1000)
    assert cost == pytest.approx(0.0015)  # (0.001 + 0.002) * 0.5


def test_a_model_with_published_modalities_gets_metadata_evidence():
    capabilities, evidence = infer_capabilities(SAMPLE_CATALOGUE["data"][0])
    assert Capability.TEXT in capabilities
    assert evidence[Capability.TEXT.value].startswith("metadata:")


def test_capability_is_never_assumed_without_evidence():
    capabilities, evidence = infer_capabilities({"id": "acme/mystery-1"})
    assert Capability.VISION not in capabilities
    # Evidence is recorded only for the capabilities that were granted, and
    # it always names its source.
    assert all(
        source.startswith(("metadata:", "heuristic:"))
        for source in evidence.values()
    )


def test_vision_is_inferred_from_a_documented_pattern():
    capabilities, evidence = infer_capabilities({"id": "openai/gpt-4o"})
    assert Capability.VISION in capabilities
    assert "heuristic" in evidence[Capability.VISION.value]


def test_parse_model_record_keeps_the_live_fields():
    info = parse_model_record(SAMPLE_CATALOGUE["data"][1])
    assert info.id == "openai/gpt-4o-mini"
    assert info.owned_by == "openai"
    assert info.context_length == 128000
    assert info.pricing.prompt == pytest.approx(0.00000015)


def test_registry_reads_the_live_catalogue_and_caches_it(settings):
    transport = FakeTransport()
    registry = ModelRegistry(settings, HttpClient(transport=transport, max_retries=0))

    models = registry.list_models()
    assert len(models) == len(SAMPLE_CATALOGUE["data"])
    assert any(m.id == "openai/gpt-4o-mini" for m in models)

    # A second read is served from the cache.
    registry.list_models()
    catalogue_reads = [u for u in transport.urls() if u.endswith("/v1/models")]
    assert len(catalogue_reads) == 1


def test_registry_reports_where_the_catalogue_came_from(settings):
    transport = FakeTransport()
    registry = ModelRegistry(settings, HttpClient(transport=transport, max_retries=0))
    registry.list_models()
    source = registry.source_description()
    assert source["endpoint"].endswith("/v1/models")
    assert source["error"] is None
    assert "not a built-in list" in source["note"]


def test_a_failing_catalogue_is_reported_not_hidden(settings):
    class BrokenTransport(FakeTransport):
        def request(self, method, url, **kwargs):
            from nexus.http import HttpResponse

            return HttpResponse(status=500, body="upstream is down")

    registry = ModelRegistry(
        settings, HttpClient(transport=BrokenTransport(), max_retries=0)
    )
    models = registry.list_models()
    assert models == []
    assert registry.last_error is not None
    assert "llm catalogue" in registry.last_error


def test_a_catalogue_that_could_not_be_read_is_not_reported_as_an_empty_one(settings):
    """A failed catalogue read must reach the caller as a failure, not a fact.

    An empty catalogue and a catalogue that could not be read are two
    different states. When the read fails, "no model satisfies this task"
    is a statement about the request that was made, not about GMI Cloud.
    """
    from nexus.http import HttpResponse

    class BrokenTransport(FakeTransport):
        def request(self, method, url, **kwargs):
            return HttpResponse(status=401, body="unauthorized")

    registry = ModelRegistry(
        settings, HttpClient(transport=BrokenTransport(), max_retries=0)
    )
    assert registry.list_models() == []
    assert registry.last_error is not None

    suffix = catalogue_error_suffix(registry)
    assert "the catalogue could not be read" in suffix
    assert registry.last_error in suffix


def test_a_healthy_catalogue_adds_nothing_to_the_message(settings):
    """When the catalogue read fine, the message stays exactly as it was."""
    registry = ModelRegistry(settings, HttpClient(transport=FakeTransport(), max_retries=0))
    registry.list_models()
    assert registry.last_error is None
    assert catalogue_error_suffix(registry) == ""


def test_media_modality_is_derived_from_the_model_id():
    assert classify_media_modality("gpt-image-2") is Modality.IMAGE
    assert classify_media_modality("Minimax-Hailuo-2.3") is Modality.VIDEO
    assert classify_media_modality("elevenlabs-tts-v3") is Modality.AUDIO


def test_product_models_are_listed_before_private_workflows(settings):
    """The named, routable models come first; the account's private ones follow.

    Sorting on the id alone would put every bare-UUID workflow first, because
    a digit sorts before a letter — hiding the models NEXUS would actually
    route to behind the ones it never selects.
    """
    transport = FakeTransport(
        media_ids=[
            "00d489c6-3929-4e8f-9972-97b1008d8fae",
            "gpt-image-2",
            "0ab92031-410d-4422-8702-4a1cd08daf2b",
            "flux-1.1-pro",
        ]
    )
    registry = ModelRegistry(settings, HttpClient(transport=transport, max_retries=0))

    media = registry.list_media()

    assert [m.id for m in media][:2] == ["flux-1.1-pro", "gpt-image-2"]
    assert [m.product for m in media] == [True, True, False, False]
    assert all(not m.product for m in media[2:])


def test_probe_results_are_recorded_with_their_evidence(settings):
    transport = FakeTransport()
    registry = ModelRegistry(settings, HttpClient(transport=transport, max_retries=0))
    registry.list_models()
    evidence = f"{PROBE_VERIFIED_PREFIX} — read 473 from a drawn number"
    registry.apply_probe("openai/gpt-4o-mini", Capability.VISION, True, evidence)

    info = registry.get("openai/gpt-4o-mini")
    assert info is not None
    assert info.supports(Capability.VISION)
    assert info.capability_evidence[Capability.VISION.value] == evidence


def test_a_verdict_from_an_older_probe_protocol_is_not_restored(settings):
    """A verdict from a superseded probe does not count.

    The first version of the probe asked a model to name two colours, which
    was easy to pass by accident. Verdicts recorded under it say nothing
    about whether the model can see, so they are discarded and the model is
    probed again.
    """
    transport = FakeTransport()
    registry = ModelRegistry(settings, HttpClient(transport=transport, max_retries=0))
    registry.list_models()
    registry.apply_probe(
        "openai/gpt-4o-mini", Capability.VISION, True, "probe:verified — named red blue"
    )

    fresh = ModelRegistry(settings, HttpClient(transport=transport, max_retries=0))
    fresh.list_models()
    info = fresh.get("openai/gpt-4o-mini")
    assert info is not None
    assert probe_verdict(info.capability_evidence.get(Capability.VISION.value, "")) is None
