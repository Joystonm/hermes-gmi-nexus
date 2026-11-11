"""GMI Vision: capability probing, analysis, issue detection and screenshots."""

from __future__ import annotations

import json

import pytest

from nexus.config import Settings
from nexus.models import Capability
from nexus.modules.vision import (
    VisionModule,
    probe_number_data_uri,
    random_probe_number,
    solid_png_data_uri,
)
from nexus.providers.gmi_llm import GmiLLMClient, build_user_message
from nexus.http import HttpClient

from conftest import ORIGINAL_RANDOM_PROBE_NUMBER, PROBE_NUMBER, FakeTransport, _is_probe_request


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Answer name resolution with a public address.

    NEXUS resolves a host and checks the address before it fetches it, so a
    test that hands it a URL needs a resolver it controls. The tests that
    are about a private address replace this themselves.
    """
    from nexus import netguard

    monkeypatch.setattr(netguard, "resolve_host", lambda host, port: ["93.184.216.34"])


def _vision(settings, transport, ctx=None) -> VisionModule:
    from nexus.budget import BudgetGuard
    from nexus.registry import ModelRegistry
    from nexus.router import Router

    client = HttpClient(transport=transport, max_retries=0, timeout=5.0)
    registry = ModelRegistry(settings, client)
    return VisionModule(
        settings,
        registry,
        Router(settings),
        GmiLLMClient(settings.llm_base_url, client),
        BudgetGuard(settings),
        ctx,
    )


# --------------------------------------------------------------------------- #
# Message construction
# --------------------------------------------------------------------------- #


def test_a_remote_image_is_sent_as_an_image_part_not_as_a_path():
    message = build_user_message("what is this?", ["https://cdn.example/a.png"])
    parts = message["content"]
    # "image_url" is the spelling GMI's endpoint accepts. The wrong one is
    # rejected by some models and silently dropped by the rest, which then
    # answer "I can't see an image attached" — a failure that looks like an
    # incapable model rather than a malformed request.
    assert parts[0]["type"] == "image_url"
    assert parts[0]["image_url"]["url"] == "https://cdn.example/a.png"
    assert parts[-1]["type"] == "text"


def test_a_data_uri_is_passed_through_untouched():
    uri = solid_png_data_uri((10, 20, 30))
    message = build_user_message("what is this?", [uri])
    assert message["content"][0]["image_url"]["url"] == uri


def test_a_plain_text_message_has_no_parts():
    message = build_user_message("hello")
    assert message["content"] == "hello"


def test_the_probe_image_is_a_real_png():
    uri = solid_png_data_uri((220, 20, 30))
    assert uri.startswith("data:image/png;base64,")
    import base64

    raw = base64.b64decode(uri.split(",", 1)[1])
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")


# --------------------------------------------------------------------------- #
# Capability probing
# --------------------------------------------------------------------------- #


def test_a_model_that_reads_the_number_is_verified(settings):
    transport = FakeTransport(probe_reply=PROBE_NUMBER)
    vision = _vision(settings, transport)

    result = vision.probe("openai/gpt-4o-mini")
    assert result["verified"] is True
    assert PROBE_NUMBER in result["evidence"]


def test_a_model_that_guesses_wrongly_is_not_verified(settings):
    transport = FakeTransport(probe_reply="908")
    vision = _vision(settings, transport)

    result = vision.probe("openai/gpt-4o-mini")
    assert result["verified"] is False
    assert "908" in result["evidence"]


def test_a_plausible_but_wrong_number_is_rejected(settings):
    """The probe must be hard to pass by accident.

    An earlier probe asked a model to name two colours. "Red" and "blue"
    are the two most guessable colour words there are, and on the live
    catalogue several text-only models passed it and then described images
    they could not see. A three-digit number is roughly one guess in nine
    hundred, and it is drawn afresh every time.
    """
    transport = FakeTransport(probe_reply="123")
    vision = _vision(settings, transport)

    result = vision.probe("openai/gpt-4o-mini")
    assert result["verified"] is False


def test_a_model_that_repeats_the_digits_in_a_sentence_is_verified(settings):
    transport = FakeTransport(probe_reply=f"The number shown is {PROBE_NUMBER}.")
    vision = _vision(settings, transport)

    result = vision.probe("openai/gpt-4o-mini")
    assert result["verified"] is True


def test_each_probe_draws_a_different_number(settings, monkeypatch):
    """Two probes must not show the same number, or the answer could be
    memorised rather than read."""
    import nexus.modules.vision as vision

    monkeypatch.setattr(vision, "random_probe_number", ORIGINAL_RANDOM_PROBE_NUMBER)
    drawn = {random_probe_number() for _ in range(20)}
    assert len(drawn) > 1
    for number in drawn:
        assert number.isdigit()
        assert len(number) == 3


def test_the_probe_image_is_drawn_from_the_number(settings):
    """The image the model is shown really does carry the number."""
    first = probe_number_data_uri("123")
    second = probe_number_data_uri("456")
    assert first.startswith("data:image/png;base64,")
    assert first != second

    import base64

    raw = base64.b64decode(first.split(",", 1)[1])
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")


def test_a_failed_probe_is_recorded_as_a_failure(settings):
    transport = FakeTransport(chat_status=500)
    vision = _vision(settings, transport)

    result = vision.probe("openai/gpt-4o-mini")
    assert result["verified"] is False
    assert "probe failed" in result["evidence"]


def test_a_verified_probe_is_remembered(settings):
    transport = FakeTransport(probe_reply=PROBE_NUMBER)
    vision = _vision(settings, transport)
    vision.probe("openai/gpt-4o-mini")

    from nexus import storage

    probes = storage.capability_probes()
    assert any(p["model_id"] == "openai/gpt-4o-mini" for p in probes)


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #


def test_an_image_is_analysed_by_a_vision_capable_model(settings):
    transport = FakeTransport(chat_reply="It shows a red square.")
    vision = _vision(settings, transport)

    outcome = vision.analyse(["https://cdn.example/a.png"], "what is this?")
    assert outcome.ok
    assert outcome.text == "It shows a red square."
    assert outcome.model_id
    assert outcome.cost_usd >= 0.0


def test_the_image_reaches_the_model_as_an_image_part(settings):
    transport = FakeTransport(chat_reply="a square")
    vision = _vision(settings, transport)
    vision.analyse(["https://cdn.example/a.png"], "what is this?")

    # The first request is the capability probe; the analysis is the last.
    body = transport.chat_requests()[-1]["body"]
    parts = body["messages"][0]["content"]
    assert parts[0]["type"] == "image_url"
    assert parts[0]["image_url"]["url"] == "https://cdn.example/a.png"


def test_analysis_without_an_image_is_refused(settings):
    from nexus.errors import ValidationError

    vision = _vision(settings, FakeTransport())
    with pytest.raises(ValidationError):
        vision.analyse([], "what is this?")


def test_analysis_reports_the_error_when_the_api_fails(settings):
    transport = FakeTransport(chat_status=500)
    vision = _vision(settings, transport)

    outcome = vision.analyse(["https://cdn.example/a.png"], "what is this?")
    assert outcome.ok is False
    assert outcome.error
    assert outcome.text == ""


def test_no_vision_model_is_reported_rather_than_guessed(settings):
    # A catalogue whose only model is text-only.
    text_only = {
        "object": "list",
        "data": [
            {
                "id": "acme/text-only-model",
                "name": "Text Only",
                "owned_by": "acme",
                "context_length": 8000,
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                "discount_to_user": 0.0,
            }
        ],
    }
    transport = FakeTransport(catalogue=text_only)
    vision = _vision(settings, transport)

    outcome = vision.analyse(["https://cdn.example/a.png"], "what is this?")
    assert outcome.ok is False
    assert "vision-capable" in outcome.error


def test_issue_detection_returns_structured_feedback(settings):
    reply = json.dumps(
        {
            "issues": [
                {"severity": "high", "area": "header", "problem": "text is clipped", "fix": "reduce it"}
            ],
            "summary": "The header is clipped.",
        }
    )
    transport = FakeTransport(chat_reply=reply)
    vision = _vision(settings, transport)

    outcome = vision.detect_issues(["https://cdn.example/a.png"], "check spacing")
    assert outcome.ok
    assert outcome.structured["summary"] == "The header is clipped."
    assert outcome.structured["issues"][0]["severity"] == "high"


def test_a_comparison_reports_both_images(settings):
    transport = FakeTransport(chat_reply="Nothing changed.")
    vision = _vision(settings, transport)

    outcome = vision.compare(
        "https://cdn.example/before.png", "https://cdn.example/after.png"
    )
    assert outcome.ok

    body = transport.chat_requests()[-1]["body"]
    parts = body["messages"][0]["content"]
    images = [p for p in parts if p["type"] == "image_url"]
    assert len(images) == 2


# --------------------------------------------------------------------------- #
# A declared capability is a claim; only a probe is proof
# --------------------------------------------------------------------------- #


def test_a_model_that_cannot_see_is_not_offered_an_image(settings):
    """The catalogue says it can see. The probe says otherwise.

    This is the live catalogue's behaviour: several models advertise an
    image modality and then answer "I can't see an image" when given one.
    """
    transport = FakeTransport(
        chat_reply="It shows a red square.",
        probe_reply="I can't see an image.",
    )
    vision = _vision(settings, transport)

    outcome = vision.analyse(["https://cdn.example/a.png"], "what is this?")
    assert outcome.ok is False
    assert "vision-capable" in outcome.error
    # The model was probed, and the probe's answer is what stopped it.
    assert transport.chat_requests()


def test_a_blind_model_is_skipped_for_a_model_that_can_see(settings):
    """A blind candidate is passed over, and the next one is used."""
    from nexus.registry import ModelRegistry

    catalogue = {
        "object": "list",
        "data": [
            {
                "id": "acme/blind-vision-model",
                "name": "Blind Vision Model",
                "owned_by": "acme",
                "context_length": 32000,
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                "discount_to_user": 0.0,
                "input_modalities": [{"type": "text"}, {"type": "image"}],
            },
            {
                "id": "acme/seeing-vision-model",
                "name": "Seeing Vision Model",
                "owned_by": "acme",
                "context_length": 32000,
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                "discount_to_user": 0.0,
                "input_modalities": [{"type": "text"}, {"type": "image"}],
            },
        ],
    }

    class PartlyBlindTransport(FakeTransport):
        def __init__(self):
            super().__init__(catalogue=catalogue)
            self.blind = True

        def request(self, method, url, **kwargs):
            if url.endswith("/chat/completions"):
                body = kwargs.get("json_body") or {}
                model = body.get("model")
                if _is_probe_request(body):
                    # The blind model cannot see; the other one can.
                    self.probe_reply = (
                        "I can't see an image."
                        if model == "acme/blind-vision-model"
                        else PROBE_NUMBER
                    )
            return super().request(method, url, **kwargs)

    transport = PartlyBlindTransport()
    vision = _vision(settings, transport)

    outcome = vision.analyse(["https://cdn.example/a.png"], "what is this?")
    assert outcome.ok
    assert outcome.model_id == "acme/seeing-vision-model"


def test_probing_can_be_turned_off(settings):
    """An operator who does not want to pay for probes gets the catalogue's
    claim, and NEXUS says which one it used."""
    blind_settings = settings
    blind_settings.verify_vision_capability = False
    transport = FakeTransport(chat_reply="It shows a red square.", probe_reply="I can't see an image.")
    vision = _vision(blind_settings, transport)

    outcome = vision.analyse(["https://cdn.example/a.png"], "what is this?")
    assert outcome.ok
    # No probe was sent: only the analysis request.
    assert len(transport.chat_requests()) == 1


# --------------------------------------------------------------------------- #
# A refusal is not a description
# --------------------------------------------------------------------------- #


def test_a_model_that_says_it_saw_no_image_is_not_taken_at_its_word(settings):
    """"I can't see an image" is not a description of the image.

    A model whose backend dropped the image says so rather than describing
    something it never saw. Reporting that as the analysis would be
    presenting a non-answer as a result.
    """
    transport = FakeTransport(
        chat_reply="I can't see an image attached.",
        probe_reply=PROBE_NUMBER,
    )
    vision = _vision(settings, transport)

    outcome = vision.analyse(["https://cdn.example/a.png"], "what is this?")
    assert outcome.ok is False
    assert "could read the image" in outcome.error
    assert outcome.text == ""


def test_a_refusing_model_is_passed_over_for_one_that_answers(settings):
    """The next candidate is tried when the first one refuses."""
    catalogue = {
        "object": "list",
        "data": [
            {
                "id": "acme/refusing-vision-model",
                "name": "Refusing Vision Model",
                "owned_by": "acme",
                "context_length": 32000,
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                "discount_to_user": 0.0,
                "input_modalities": [{"type": "text"}, {"type": "image"}],
            },
            {
                "id": "acme/answering-vision-model",
                "name": "Answering Vision Model",
                "owned_by": "acme",
                "context_length": 32000,
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                "discount_to_user": 0.0,
                "input_modalities": [{"type": "text"}, {"type": "image"}],
            },
        ],
    }

    class RefusingFirstTransport(FakeTransport):
        def request(self, method, url, **kwargs):
            if url.endswith("/chat/completions"):
                body = kwargs.get("json_body") or {}
                model = body.get("model")
                if not _is_probe_request(body):
                    # The analysis call, not the probe.
                    if model == "acme/refusing-vision-model":
                        self.chat_reply = "I can't see an image attached."
                    else:
                        self.chat_reply = "A blue beam splits into thin rays."
            return super().request(method, url, **kwargs)

    transport = RefusingFirstTransport(catalogue=catalogue)
    vision = _vision(settings, transport)

    outcome = vision.analyse(["https://cdn.example/a.png"], "what is this?")
    assert outcome.ok
    assert outcome.model_id == "acme/answering-vision-model"
    assert "blue beam" in outcome.text


def test_a_refusing_model_is_marked_as_unable_to_see(settings):
    """The refusal is remembered, so the model is not offered an image again."""
    transport = FakeTransport(
        chat_reply="I can't see an image attached.",
        probe_reply=PROBE_NUMBER,
    )
    vision = _vision(settings, transport)
    vision.analyse(["https://cdn.example/a.png"], "what is this?")

    from nexus import storage

    probes = storage.capability_probes()
    failures = [p for p in probes if not p["verified"]]
    assert failures, "the refusal was not recorded"


def test_an_empty_answer_is_not_a_description(settings):
    transport = FakeTransport(chat_reply="", probe_reply=PROBE_NUMBER)
    vision = _vision(settings, transport)

    outcome = vision.analyse(["https://cdn.example/a.png"], "what is this?")
    assert outcome.ok is False
    assert outcome.text == ""


# --------------------------------------------------------------------------- #
# A remote image is inlined, because a link is not always followed
# --------------------------------------------------------------------------- #


def test_a_remote_image_is_inlined_before_it_is_sent(settings):
    """GMI's gateway refuses plain HTTP links for some models, so NEXUS
    downloads the image and sends the bytes."""
    import base64

    png = base64.b64decode(solid_png_data_uri((10, 20, 30)).split(",", 1)[1])

    class ImageTransport(FakeTransport):
        def request(self, method, url, **kwargs):
            if url.startswith("https://cdn.example/"):
                from nexus.http import HttpResponse

                return HttpResponse(
                    status=200,
                    body="",
                    headers={"content-type": "image/png"},
                    raw=png,
                )
            return super().request(method, url, **kwargs)

    transport = ImageTransport()
    vision = _vision(settings, transport)
    outcome = vision.analyse(["https://cdn.example/a.png"], "what is this?")
    assert outcome.ok

    body = transport.chat_requests()[-1]["body"]
    url = body["messages"][0]["content"][0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == png


def test_an_undownloadable_image_is_still_sent_as_a_url(settings):
    """A failed download is not a failed analysis: the URL goes as it stands."""
    transport = FakeTransport(chat_reply="A blue beam.", probe_reply=PROBE_NUMBER)
    vision = _vision(settings, transport)

    outcome = vision.analyse(["https://cdn.example/missing.png"], "what is this?")
    assert outcome.ok
    body = transport.chat_requests()[-1]["body"]
    url = body["messages"][0]["content"][0]["image_url"]["url"]
    assert url == "https://cdn.example/missing.png"


# --------------------------------------------------------------------------- #
# Screenshots
# --------------------------------------------------------------------------- #


def test_a_screenshot_is_captured_through_hermes_own_tool(settings, ctx):
    ctx.tool_responses["computer_use"] = {"image_url": "https://cdn.example/screen.png"}
    vision = _vision(settings, FakeTransport(), ctx)

    result = vision.capture_screenshot(app="screen")
    assert result["ok"] is True
    assert result["image"] == "https://cdn.example/screen.png"
    assert ctx.dispatched[0][0] == "computer_use"


def test_a_failed_capture_is_reported_not_faked(settings, ctx):
    ctx.tool_responses["computer_use"] = {"error": "no display available"}
    vision = _vision(settings, FakeTransport(), ctx)

    result = vision.capture_screenshot(app="screen")
    assert result["ok"] is False
    assert "no display available" in result["error"]


def test_a_capture_without_an_image_is_reported(settings, ctx):
    ctx.tool_responses["computer_use"] = {"result": "ok"}
    vision = _vision(settings, FakeTransport(), ctx)

    result = vision.capture_screenshot(app="screen")
    assert result["ok"] is False
    assert "did not return an image" in result["error"]


def test_a_capture_without_hermes_is_reported(settings):
    vision = _vision(settings, FakeTransport(), None)
    result = vision.capture_screenshot(app="screen")
    assert result["ok"] is False
    assert "no Hermes context" in result["error"]
