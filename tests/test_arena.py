"""The Arena: identical cases, measured results, and the router's input."""

from __future__ import annotations

import pytest

from nexus.arena import Arena, CHECK_CONTAINS, CHECK_EQUALS, CHECK_JSON_KEYS, CHECK_REGEX, BenchmarkCase
from nexus.config import Settings
from nexus.http import HttpClient
from nexus.models import TaskType
from nexus.providers.gmi_llm import GmiLLMClient
from nexus.registry import ModelRegistry

from conftest import FakeTransport


def _arena(settings, transport, **kwargs) -> Arena:
    client = HttpClient(transport=transport, max_retries=0, timeout=5.0)
    return Arena(settings, GmiLLMClient(settings.llm_base_url, client), **kwargs)


def _models(settings, transport):
    client = HttpClient(transport=transport, max_retries=0, timeout=5.0)
    return ModelRegistry(settings, client).list_models()


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


def test_an_exact_match_passes():
    case = BenchmarkCase(name="t", prompt="p", check=CHECK_EQUALS, expected="NEXUS")
    arena = Arena(Settings(), None)
    assert arena._score(case, "NEXUS") == (True, 1.0)
    assert arena._score(case, "nexus") == (False, 0.0)


def test_a_contains_check_passes_on_a_substring():
    case = BenchmarkCase(name="t", prompt="p", check=CHECK_CONTAINS, expected="def fibonacci")
    arena = Arena(Settings(), None)
    assert arena._score(case, "Here: def fibonacci(n): ...")[0] is True


def test_a_regex_check_is_case_insensitive():
    case = BenchmarkCase(name="t", prompt="p", check=CHECK_REGEX, expected=r"(backup|Tuesday)")
    arena = Arena(Settings(), None)
    assert arena._score(case, "the backup failed on Tuesday")[0] is True


def test_a_json_check_accepts_a_fenced_object():
    case = BenchmarkCase(name="t", prompt="p", check=CHECK_JSON_KEYS, expected=["name", "count"])
    arena = Arena(Settings(), None)
    passed, _ = arena._score(case, '```json\n{"name": "x", "count": 2}\n```')
    assert passed is True


def test_a_json_check_rejects_a_missing_key():
    case = BenchmarkCase(name="t", prompt="p", check=CHECK_JSON_KEYS, expected=["name", "count"])
    arena = Arena(Settings(), None)
    assert arena._score(case, '{"name": "x"}')[0] is False


def test_an_empty_answer_scores_zero():
    case = BenchmarkCase(name="t", prompt="p")
    arena = Arena(Settings(), None)
    assert arena._score(case, "   ") == (False, 0.0)


# --------------------------------------------------------------------------- #
# Running the suite
# --------------------------------------------------------------------------- #


def test_every_model_runs_the_identical_cases(settings):
    transport = FakeTransport(chat_reply="NEXUS")
    arena = _arena(settings, transport)
    models = _models(settings, transport)

    suite = [
        BenchmarkCase(name="one", prompt="p1", check=CHECK_EQUALS, expected="NEXUS"),
        BenchmarkCase(name="two", prompt="p2", check=CHECK_CONTAINS, expected="x"),
    ]
    summary = arena.run(models[:2], suite)

    assert summary["cases"] == 2
    for row in summary["models"]:
        assert row["cases"] == 2
        assert row["completed"] == 2

    # The same prompts went to every model.
    prompts = [
        entry["body"]["messages"][0]["content"]
        for entry in transport.chat_requests()
    ]
    assert prompts[:2] == ["p1", "p2"]
    assert prompts[2:4] == ["p1", "p2"]


def test_a_failed_call_is_recorded_as_failed_not_skipped(settings):
    class FailingTransport(FakeTransport):
        def request(self, method, url, **kwargs):
            if url.endswith("/chat/completions"):
                return self._json(500, {"error": {"message": "upstream is down"}})
            return super().request(method, url, **kwargs)

    transport = FailingTransport()
    arena = _arena(settings, transport)
    models = _models(settings, transport)

    suite = [BenchmarkCase(name="one", prompt="p1", check=CHECK_EQUALS, expected="NEXUS")]
    summary = arena.run(models[:1], suite)

    row = summary["models"][0]
    assert row["failed"] == 1
    assert row["completed"] == 0
    assert row["pass_rate"] == 0.0
    assert row["errors"]


def test_the_summary_says_the_figures_are_measured(settings):
    transport = FakeTransport(chat_reply="NEXUS")
    arena = _arena(settings, transport)
    models = _models(settings, transport)

    summary = arena.run(models[:1], [BenchmarkCase(name="one", prompt="p", check=CHECK_EQUALS,
                                            expected="NEXUS")])
    assert summary["measured"] is True
    assert "real API calls" in summary["note"]


def test_the_token_counts_come_from_the_provider(settings):
    transport = FakeTransport(chat_reply="NEXUS")
    arena = _arena(settings, transport)
    models = _models(settings, transport)

    summary = arena.run(models[:1], [BenchmarkCase(name="one", prompt="p", check=CHECK_EQUALS,
                                            expected="NEXUS")])
    row = summary["models"][0]
    assert row["prompt_tokens"] == 120
    assert row["completion_tokens"] == 40


def test_the_benchmark_is_stored(settings):
    from nexus import storage

    transport = FakeTransport(chat_reply="NEXUS")
    arena = _arena(settings, transport)
    models = _models(settings, transport)

    summary = arena.run(models[:2], [BenchmarkCase(name="one", prompt="p", check=CHECK_EQUALS,
                                            expected="NEXUS")])
    benchmark_id = summary["benchmark_id"]

    stored = storage.list_benchmarks(limit=10)
    assert any(b["id"] == benchmark_id for b in stored)

    results = storage.benchmark_results(benchmark_id)
    assert len(results) == 2


def test_the_benchmark_scores_feed_the_router(settings):
    from nexus.router import Router, RoutingSignals

    transport = FakeTransport(chat_reply="NEXUS")
    arena = _arena(settings, transport)
    models = _models(settings, transport)

    summary = arena.run(models[:2], [BenchmarkCase(name="one", prompt="p", check=CHECK_EQUALS,
                                            expected="NEXUS")])
    signals = RoutingSignals.from_storage()
    for model_id, score in summary["scores"].items():
        assert signals.benchmark[model_id] == pytest.approx(score)


def test_a_benchmark_over_the_approval_threshold_is_not_run(settings):
    strict = Settings(
        llm_base_url="https://mock.gmi/v1",
        approval_threshold_usd=0.0000001,
        max_cost_per_run_usd=5.0,
        daily_budget_usd=50.0,
    )
    transport = FakeTransport(chat_reply="NEXUS")
    arena = _arena(strict, transport)
    models = _models(strict, transport)

    from nexus.budget import BudgetGuard

    summary = arena.run(
        models[:1],
        [BenchmarkCase(name="one", prompt="p", check=CHECK_EQUALS, expected="NEXUS")],
        budget=BudgetGuard(strict),
    )
    row = summary["models"][0]
    assert row["failed"] == 1
    assert "not run" in row["errors"][0]
    assert not transport.chat_requests()


def test_a_benchmark_without_a_budget_still_runs(settings):
    transport = FakeTransport(chat_reply="NEXUS")
    arena = _arena(settings, transport)
    models = _models(settings, transport)

    summary = arena.run(models[:1], [BenchmarkCase(name="one", prompt="p", check=CHECK_EQUALS,
                                            expected="NEXUS")])
    assert summary["models"][0]["completed"] == 1


def test_an_independent_judge_can_score_an_answer(settings):
    transport = FakeTransport(chat_reply="NEXUS")
    judge = lambda prompt: '{"score": 0.9, "reason": "it matches"}'  # noqa: E731
    arena = _arena(settings, transport, judge=judge)
    models = _models(settings, transport)

    case = BenchmarkCase(
        name="one",
        prompt="p",
        check=CHECK_EQUALS,
        expected="NEXUS",
        judge_criterion="Is the answer right?",
    )
    summary = arena.run(models[:1], [case])
    assert summary["models"][0]["pass_rate"] == 1.0
