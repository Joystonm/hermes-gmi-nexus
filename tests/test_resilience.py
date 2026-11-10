"""What the plugin does when the service says a model is gone.

GMI's catalogue and the models the provider can actually serve are published
separately. A retired model keeps its place in the catalogue, so the router
will choose it, call it, and be refused — and the refusal arrives wrapped in
a generic "backend request failed", with the real reason nested inside it.

These tests pin the three responses to that: the reason is reported, the
verdict is remembered, and a model that has been retired is not asked again.
"""

from __future__ import annotations

import json

from nexus.budget import BudgetGuard
from nexus.config import Settings
from nexus.errors import _extract_error_message, is_retirement_message
from nexus.http import HttpClient, HttpResponse
from nexus.modules.coding import DevAgent, _extract_files
from nexus.providers.gmi_llm import GmiLLMClient
from nexus.registry import ModelRegistry
from nexus.router import Router

from conftest import FakeTransport

#: A model reply that lists one file, the shape the Dev Agent asks for.
FILE_REPLY = json.dumps(
    {
        "files": [{"path": "app/main.py", "content": "def main():\n    return 42\n"}],
        "notes": "Added the entry point.",
    }
)

#: The body GMI returns for a model the provider has dropped. The wrapper
#: says only that the backend failed; the reason is one level down.
RETIREMENT_BODY = {
    "error": {
        "message": "Backend request failed with status 400",
        "type": "backend_error",
        "code": 400,
        "details": json.dumps(
            {
                "error": {
                    "code": "UnavailableModel",
                    "message": (
                        "The requested model has been deprecated and is no "
                        "longer available."
                    ),
                    "type": "BadRequest",
                }
            }
        ),
    }
}


def _agent(settings, transport, ctx=None) -> DevAgent:
    client = HttpClient(transport=transport, max_retries=0, timeout=5.0)
    registry = ModelRegistry(settings, client)
    return DevAgent(
        settings,
        registry,
        Router(settings),
        GmiLLMClient(settings.llm_base_url, client),
        BudgetGuard(settings),
        ctx,
    )


class ScriptedChatTransport(FakeTransport):
    """Answers the first chat calls from a script, then behaves normally."""

    def __init__(self, script, **kwargs) -> None:
        super().__init__(**kwargs)
        self.script = list(script)

    def request(self, method, url, *, headers=None, json_body=None, data=None,
                files=None, timeout=60.0):
        if url.endswith("/chat/completions") and self.script:
            entry = self.script.pop(0)
            self.requests.append(
                {
                    "method": method,
                    "url": url,
                    "headers": dict(headers or {}),
                    "body": json_body,
                }
            )
            return HttpResponse(
                status=entry["status"], body=json.dumps(entry["body"]), headers={}
            )
        return super().request(
            method, url, headers=headers, json_body=json_body, data=data,
            files=files, timeout=timeout,
        )


# --------------------------------------------------------------------------- #
# Reading the provider's real reason
# --------------------------------------------------------------------------- #


def test_the_nested_reason_is_reported_not_just_the_wrapper():
    message = _extract_error_message(json.dumps(RETIREMENT_BODY))
    assert "deprecated" in message
    assert "no longer available" in message


def test_a_retirement_is_recognised_from_its_wording():
    assert is_retirement_message("The requested model has been deprecated and is no longer available.")
    assert is_retirement_message("UnavailableModel")
    # A transient backend failure is not a retirement.
    assert not is_retirement_message("Backend request failed with status 500")
    assert not is_retirement_message("rate limit exceeded")


def test_an_error_with_no_nested_detail_still_reads_well():
    body = json.dumps({"error": {"message": "rate limit exceeded", "type": "rate_limit_error"}})
    assert _extract_error_message(body) == "rate limit exceeded"


# --------------------------------------------------------------------------- #
# Remembering a retired model
# --------------------------------------------------------------------------- #


def test_a_model_the_provider_retires_is_recorded(settings, ctx):
    from nexus import storage

    transport = ScriptedChatTransport(
        [{"status": 400, "body": RETIREMENT_BODY}], chat_reply=FILE_REPLY
    )
    agent = _agent(settings, transport, ctx)

    agent.implement("Add an entry point", max_attempts=2)

    called = [entry["body"]["model"] for entry in transport.chat_requests()]
    assert called, "the Dev Agent must have tried a model"
    assert storage.is_retired_model(called[0])


def test_a_retired_model_is_not_offered_as_a_candidate(settings, ctx):
    from nexus import storage

    storage.save_retired_model("acme/qwen3-coder-plus", "the provider retired it")
    transport = FakeTransport(chat_reply=FILE_REPLY)
    agent = _agent(settings, transport, ctx)

    candidates = [model.id for model in agent._candidate_models("Add an entry point")]

    assert "acme/qwen3-coder-plus" not in candidates


def test_a_model_retired_during_a_run_is_not_asked_again(settings, ctx):
    from nexus import storage

    transport = ScriptedChatTransport(
        [
            {"status": 400, "body": RETIREMENT_BODY},
            {"status": 400, "body": RETIREMENT_BODY},
        ],
        chat_reply=FILE_REPLY,
    )
    agent = _agent(settings, transport, ctx)

    outcome = agent.implement("Add an entry point", max_attempts=3)

    called = [entry["body"]["model"] for entry in transport.chat_requests()]
    assert called
    # No model is asked twice: the first refusal retires it, and a retired
    # model is not a candidate.
    assert len(called) == len(set(called))
    for model_id in called:
        assert storage.is_retired_model(model_id)
    assert outcome.ok is False


def test_a_retired_model_is_marked_not_ready_after_a_catalogue_read(settings):
    from nexus import storage

    storage.save_retired_model("acme/qwen3-coder-plus", "the provider retired it")
    client = HttpClient(transport=FakeTransport(), max_retries=0, timeout=5.0)
    registry = ModelRegistry(settings, client)

    registry.refresh(force=True)

    model = registry.get("acme/qwen3-coder-plus")
    assert model is not None
    assert model.is_ready is False
    assert "retired" in model.retired_reason


def test_a_retirement_can_be_forgotten(settings):
    from nexus import storage

    storage.save_retired_model("acme/qwen3-coder-plus", "the provider retired it")
    assert storage.is_retired_model("acme/qwen3-coder-plus") is True

    storage.clear_retired_model("acme/qwen3-coder-plus")

    assert storage.is_retired_model("acme/qwen3-coder-plus") is False


# --------------------------------------------------------------------------- #
# A reply that is right but not quite JSON
# --------------------------------------------------------------------------- #


def test_a_reply_with_raw_newlines_inside_the_content_is_still_parsed():
    # A model asked for a whole file as a JSON string will sometimes write
    # the file's newlines literally. The answer is right and the JSON is not,
    # and throwing the reply away loses good work.
    raw = (
        '{"files": [{"path": "a.py", "content": "line one\nline two\n"}], '
        '"notes": "ok"}'
    )
    changes, notes = _extract_files(raw)
    assert changes and changes[0].path == "a.py"
    assert "line one" in changes[0].content
    assert notes == "ok"


def test_a_valid_reply_is_still_parsed_after_the_repair():
    changes, _ = _extract_files(FILE_REPLY)
    assert changes and changes[0].path == "app/main.py"


def test_a_reply_that_is_not_json_at_all_is_still_refused():
    assert _extract_files("I could not do that.") is None
    assert _extract_files('{"notes": "no files here"}') is None


def test_the_retry_shows_the_model_the_reply_that_failed(settings, ctx):
    transport = FakeTransport(chat_reply="I could not do that.")
    agent = _agent(settings, transport, ctx)

    agent.implement("Add an entry point", max_attempts=2)

    second_prompt = transport.chat_requests()[1]["body"]["messages"][0]["content"]
    assert "I could not do that." in second_prompt
    assert "JSON only" in second_prompt


def test_the_retry_explains_how_to_escape_a_file(settings, ctx):
    transport = FakeTransport(chat_reply="not json")
    agent = _agent(settings, transport, ctx)

    agent.implement("Add an entry point", max_attempts=2)

    second_prompt = transport.chat_requests()[1]["body"]["messages"][0]["content"]
    assert "\\n" in second_prompt


def test_a_usable_reply_needs_no_retry(settings, ctx):
    transport = FakeTransport(chat_reply=FILE_REPLY)
    agent = _agent(settings, transport, ctx)

    outcome = agent.implement("Add an entry point", max_attempts=3)

    assert outcome.ok is True
    assert outcome.attempts == 1
    assert len(transport.chat_requests()) == 1
