"""The Dev Agent: it writes and runs only through Hermes' own tools."""

from __future__ import annotations

import json

import pytest

from nexus.budget import BudgetGuard
from nexus.config import Settings
from nexus.http import HttpClient
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


# --------------------------------------------------------------------------- #
# Parsing the model's reply
# --------------------------------------------------------------------------- #


def test_a_file_list_is_parsed():
    changes, notes = _extract_files(FILE_REPLY)
    assert len(changes) == 1
    assert changes[0].path == "app/main.py"
    assert "def main()" in changes[0].content
    assert notes == "Added the entry point."


def test_a_reply_with_no_file_list_is_not_usable():
    assert _extract_files("I could not do that.") is None
    assert _extract_files('{"notes": "no files here"}') is None


def test_a_fenced_json_reply_is_parsed():
    fenced = "```json\n" + FILE_REPLY + "\n```"
    changes, _ = _extract_files(fenced)
    assert changes and changes[0].path == "app/main.py"


# --------------------------------------------------------------------------- #
# Implementing
# --------------------------------------------------------------------------- #


def test_the_files_are_written_through_hermes_own_write_file(settings, ctx):
    transport = FakeTransport(chat_reply=FILE_REPLY)
    agent = _agent(settings, transport, ctx)

    outcome = agent.implement("Add an entry point")

    assert outcome.ok is True
    assert outcome.files[0].path == "app/main.py"
    assert outcome.files[0].written is True

    written = [call for call in ctx.dispatched if call[0] == "write_file"]
    assert written
    assert written[0][1]["path"] == "app/main.py"
    assert "def main()" in written[0][1]["content"]


def test_nothing_is_written_when_the_model_gives_no_usable_files(settings, ctx):
    transport = FakeTransport(chat_reply="Sorry, I cannot help with that.")
    agent = _agent(settings, transport, ctx)

    outcome = agent.implement("Add an entry point", max_attempts=1)

    assert outcome.ok is False
    assert outcome.error
    assert not [call for call in ctx.dispatched if call[0] == "write_file"]


def test_a_write_failure_is_reported(settings, ctx):
    ctx.tool_responses["write_file"] = {"error": "permission denied"}
    transport = FakeTransport(chat_reply=FILE_REPLY)
    agent = _agent(settings, transport, ctx)

    outcome = agent.implement("Add an entry point", max_attempts=1)

    assert outcome.ok is False
    assert outcome.files[0].written is False
    assert "permission denied" in (outcome.error or "")


def test_the_test_command_runs_through_hermes_own_terminal(settings, ctx):
    ctx.tool_responses["terminal"] = {"output": "1 passed", "exit_code": 0}
    transport = FakeTransport(chat_reply=FILE_REPLY)
    agent = _agent(settings, transport, ctx)

    outcome = agent.implement("Add an entry point", test_command="pytest -q")

    assert outcome.ok is True
    assert outcome.tests_passed is True
    commands = [call for call in ctx.dispatched if call[0] == "terminal"]
    assert commands and commands[0][1]["command"] == "pytest -q"


def test_a_failing_test_triggers_a_fix_attempt(settings, ctx):
    ctx.tool_responses["terminal"] = {"output": "AssertionError: expected 43", "exit_code": 1}
    transport = FakeTransport(chat_reply=FILE_REPLY)
    agent = _agent(settings, transport, ctx)

    outcome = agent.implement("Add an entry point", test_command="pytest -q", max_attempts=2)

    assert outcome.ok is False
    assert outcome.tests_passed is False
    assert "AssertionError" in outcome.test_output
    # Two attempts: the original and one fix.
    assert outcome.attempts == 2
    assert len(transport.chat_requests()) == 2
    # The second call carried the failure output back to the model.
    second_prompt = transport.chat_requests()[1]["body"]["messages"][0]["content"]
    assert "AssertionError" in second_prompt


def test_the_fix_prompt_carries_the_original_task(settings, ctx):
    # A live run failed on exactly this. The first attempt wrote
    # "example.txt" instead of the "landing/index.html" the task named,
    # the test failed, and the retry prompt carried only the failure
    # output — so the model, no longer knowing the task, had no reason to
    # write the right path either and repeated the mistake. The task
    # travels with the fix.
    ctx.tool_responses["terminal"] = {"output": "FileNotFoundError: landing/index.html", "exit_code": 1}
    transport = FakeTransport(chat_reply=FILE_REPLY)
    agent = _agent(settings, transport, ctx)

    outcome = agent.implement(
        "Create the file 'landing/index.html' for the landing page",
        test_command="pytest -q",
        max_attempts=2,
    )

    assert outcome.attempts == 2
    second_prompt = transport.chat_requests()[1]["body"]["messages"][0]["content"]
    assert "landing/index.html" in second_prompt
    assert "FileNotFoundError" in second_prompt


def test_the_fix_prompt_carries_the_task_after_a_write_failure(settings, ctx):
    ctx.tool_responses["write_file"] = {"error": "permission denied"}
    transport = FakeTransport(chat_reply=FILE_REPLY)
    agent = _agent(settings, transport, ctx)

    outcome = agent.implement(
        "Create the file 'landing/index.html' for the landing page",
        max_attempts=2,
    )

    assert outcome.attempts == 2
    second_prompt = transport.chat_requests()[1]["body"]["messages"][0]["content"]
    assert "landing/index.html" in second_prompt


def test_the_code_call_asks_for_room_to_write_a_whole_file(settings, ctx):
    # A live run failed on exactly this. The inference client's default of
    # 2048 tokens is enough for a sentence and not enough for a source
    # file, so the reply came back cut off mid-JSON and the file list
    # could not be parsed at all.
    transport = FakeTransport(chat_reply=FILE_REPLY)
    agent = _agent(settings, transport, ctx)

    agent.implement("Add an entry point")

    body = transport.chat_requests()[0]["body"]
    assert body["max_tokens"] == settings.code_max_tokens
    assert body["max_tokens"] >= 4096


def test_a_reply_cut_off_by_the_token_limit_is_named_as_such(settings, ctx):
    # A truncated reply is not a refusal and not an unparseable reply: it is
    # a reply that ran out of room. The report has to say which it was,
    # because the two need different fixes.
    transport = FakeTransport(chat_reply='{"files": [{"path": "a.py", "cont', chat_finish_reason="length")
    agent = _agent(settings, transport, ctx)

    outcome = agent.implement("Add an entry point", max_attempts=1)

    assert outcome.ok is False
    assert "cut off" in (outcome.error or "")
    assert "token limit" in (outcome.error or "")


def test_a_truncated_reply_is_asked_for_more_room_next_time(settings, ctx):
    transport = FakeTransport(chat_reply='{"files": [{"path": "a.py", "cont', chat_finish_reason="length")
    agent = _agent(settings, transport, ctx)

    agent.implement("Add an entry point", max_attempts=2)

    bodies = [entry["body"] for entry in transport.chat_requests()]
    assert bodies[0]["max_tokens"] == settings.code_max_tokens
    assert bodies[1]["max_tokens"] == settings.code_max_tokens * 2


def test_the_dev_agent_checks_the_budget_before_it_spends(settings, ctx):
    # The Dev Agent spends money, so it goes through the same gate as the
    # rest of the pipeline: the per-run cap, the daily cap and the
    # approval threshold, all before the request goes out.
    settings.approval_threshold_usd = 0.0
    transport = FakeTransport(chat_reply=FILE_REPLY)
    agent = _agent(settings, transport, ctx)

    outcome = agent.implement("Add an entry point", max_attempts=1)

    assert outcome.ok is False
    assert "approval threshold" in (outcome.error or "")
    assert not transport.chat_requests()


def test_the_dev_agent_spends_when_the_call_is_approved(settings, ctx):
    settings.approval_threshold_usd = 0.0
    transport = FakeTransport(chat_reply=FILE_REPLY)
    agent = _agent(settings, transport, ctx)

    outcome = agent.implement("Add an entry point", max_attempts=1, approved=True)

    assert outcome.ok is True
    assert transport.chat_requests()


def test_a_review_checks_the_budget_before_it_spends(settings, ctx):
    ctx.tool_responses["read_file"] = {"content": "def main():\n    return 42\n"}
    settings.approval_threshold_usd = 0.0
    transport = FakeTransport(chat_reply="{}")
    agent = _agent(settings, transport, ctx)

    outcome = agent.review(["app/main.py"])

    assert outcome.ok is False
    assert "approval threshold" in (outcome.error or "")
    assert not transport.chat_requests()


def test_the_retry_budget_is_respected(settings, ctx):
    ctx.tool_responses["terminal"] = {"output": "still failing", "exit_code": 1}
    transport = FakeTransport(chat_reply=FILE_REPLY)
    agent = _agent(settings, transport, ctx)

    outcome = agent.implement("Add an entry point", test_command="pytest -q", max_attempts=3)

    assert outcome.attempts == 3
    assert len(transport.chat_requests()) == 3


def test_a_second_attempt_that_passes_ends_the_loop(settings, ctx):
    responses = [
        {"output": "AssertionError: expected 43", "exit_code": 1},
        {"output": "1 passed", "exit_code": 0},
    ]

    class AlternatingTransport(FakeTransport):
        def __init__(self):
            super().__init__(chat_reply=FILE_REPLY)
            self._calls = 0

        def request(self, method, url, **kwargs):
            if url.endswith("/chat/completions"):
                self._calls += 1
            return super().request(method, url, **kwargs)

    transport = AlternatingTransport()
    ctx.tool_responses["terminal"] = responses[0]
    agent = _agent(settings, transport, ctx)

    original_dispatch = ctx.dispatch_tool

    def dispatch(tool_name, args, **kwargs):
        if tool_name == "terminal":
            index = min(len([c for c in ctx.dispatched if c[0] == "terminal"]), len(responses) - 1)
            ctx.tool_responses["terminal"] = responses[index]
        return original_dispatch(tool_name, args, **kwargs)

    ctx.dispatch_tool = dispatch

    outcome = agent.implement("Add an entry point", test_command="pytest -q", max_attempts=3)

    assert outcome.ok is True
    assert outcome.tests_passed is True
    assert outcome.attempts == 2


def test_no_hermes_context_is_a_clear_error_not_a_crash(settings):
    transport = FakeTransport(chat_reply=FILE_REPLY)
    agent = _agent(settings, transport, None)

    with pytest.raises(Exception) as excinfo:
        agent.implement("Add an entry point")
    assert "Hermes" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Reviewing
# --------------------------------------------------------------------------- #


def test_a_review_reads_the_files_through_hermes_own_read_file(settings, ctx):
    ctx.tool_responses["read_file"] = {"content": "def main():\n    return 42\n"}
    review = json.dumps(
        {
            "issues": [
                {"severity": "high", "file": "app/main.py", "problem": "no error handling",
                 "fix": "wrap the call"}
            ],
            "summary": "One real problem.",
        }
    )
    transport = FakeTransport(chat_reply=review)
    agent = _agent(settings, transport, ctx)

    outcome = agent.review(["app/main.py"])

    assert outcome.ok is True
    assert outcome.findings[0]["severity"] == "high"
    assert outcome.notes == "One real problem."

    reads = [call for call in ctx.dispatched if call[0] == "read_file"]
    assert reads and reads[0][1]["path"] == "app/main.py"


def test_a_review_of_unreadable_files_says_so(settings, ctx):
    ctx.tool_responses["read_file"] = {"error": "no such file"}
    transport = FakeTransport(chat_reply="{}")
    agent = _agent(settings, transport, ctx)

    outcome = agent.review(["missing.py"])

    assert outcome.ok is False
    assert "could be read" in (outcome.error or "")


def test_a_review_needs_at_least_one_file(settings, ctx):
    from nexus.errors import ValidationError

    transport = FakeTransport()
    agent = _agent(settings, transport, ctx)

    with pytest.raises(ValidationError):
        agent.review([])
