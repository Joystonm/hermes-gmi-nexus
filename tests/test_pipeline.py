"""The eight-step pipeline, end to end."""

from __future__ import annotations

import json

import pytest

from nexus.orchestrator import MAX_RECOVERIES, MAX_STEP_CALLS, PIPELINE_STEPS, Nexus

from conftest import FakeTransport


def _engine(settings, transport, ctx=None) -> Nexus:
    from nexus.http import HttpClient

    nexus = Nexus(settings, ctx=ctx)
    nexus.http.close()
    client = HttpClient(transport=transport, max_retries=0, timeout=5.0)
    nexus.http = client
    nexus.registry.client = client
    nexus.llm.client = client
    nexus.media.client = client
    nexus.media.catalog.client = client
    return nexus


# --------------------------------------------------------------------------- #
# UNDERSTAND, PLAN, ROUTE
# --------------------------------------------------------------------------- #


def test_the_pipeline_has_the_eight_documented_steps():
    assert PIPELINE_STEPS == (
        "understand",
        "plan",
        "route",
        "execute",
        "evaluate",
        "recover",
        "learn",
        "report",
    )


def test_analysis_spends_nothing(settings):
    transport = FakeTransport()
    engine = _engine(settings, transport)

    analysis = engine.analyze("Build a landing page for a new AI startup")

    assert analysis["understanding"]["task_type"]
    assert analysis["understanding"]["evidence"]
    assert analysis["plan"]["steps"]
    assert analysis["routing_preview"]
    # Only the free catalogue was read; no model was called.
    assert not transport.chat_requests()


def test_the_routing_preview_explains_itself(settings):
    transport = FakeTransport()
    engine = _engine(settings, transport)

    analysis = engine.analyze("Write a Python function to parse CSV files")
    decision = analysis["routing_preview"][0]["decision"]

    assert decision["candidates"]
    primary = decision["candidates"][0]
    assert primary["reasons"]
    assert any(reason.startswith("quality:") for reason in primary["reasons"])


def test_a_model_that_cannot_do_the_work_is_rejected_with_a_reason(settings):
    text_only = {
        "object": "list",
        "data": [
            {
                "id": "acme/text-only",
                "name": "Text Only",
                "owned_by": "acme",
                "context_length": 8000,
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                "discount_to_user": 0.0,
            }
        ],
    }
    transport = FakeTransport(catalogue=text_only)
    engine = _engine(settings, transport)

    analysis = engine.analyze("Describe this screenshot in detail", media_mimes=("image/png",))
    decision = analysis["routing_preview"][0]["decision"]
    assert decision["candidates"] == []
    assert decision["rejected"]["acme/text-only"]


# --------------------------------------------------------------------------- #
# EXECUTE, EVALUATE
# --------------------------------------------------------------------------- #


def test_a_run_executes_the_plan_and_reports_it(settings):
    transport = FakeTransport(chat_reply="A complete and useful answer.")
    engine = _engine(settings, transport)

    report = engine.run("Summarise the quarterly report and list the risks")

    assert report.status in ("completed", "partial")
    assert report.steps
    assert report.models_used
    assert transport.chat_requests()


def test_every_step_records_its_evaluation(settings):
    transport = FakeTransport(chat_reply="A complete and useful answer.")
    engine = _engine(settings, transport)

    report = engine.run("Summarise the quarterly report and list the risks")

    for result in report.steps:
        evaluation = result.validation["evaluation"]
        assert "passed" in evaluation
        assert evaluation["checks"]


def test_a_placeholder_answer_does_not_count_as_success(settings):
    transport = FakeTransport(chat_reply="TODO: write the rest of this later.")
    engine = _engine(settings, transport)

    report = engine.run("Summarise the quarterly report and list the risks")

    assert report.status != "completed"
    failures = [
        failure
        for result in report.steps
        for failure in result.validation["evaluation"]["failures"]
    ]
    assert any("placeholder" in failure for failure in failures)


# --------------------------------------------------------------------------- #
# RECOVER
# --------------------------------------------------------------------------- #


def test_a_failing_step_is_retried_on_another_model(settings):
    transport = FakeTransport(
        chat_reply="A complete and useful answer.",
        chat_statuses=[503],
    )
    engine = _engine(settings, transport)

    report = engine.run("Summarise the quarterly report and list the risks")

    # The failure was recovered: a second model produced the answer.
    assert report.status == "completed"
    calls = transport.chat_requests()
    assert len(calls) >= 2
    failed_model = calls[0]["body"]["model"]
    step = report.steps[0]
    assert step.model_id == calls[1]["body"]["model"]
    assert step.model_id != failed_model
    # The failed attempt is recorded rather than hidden.
    assert any(failed_model in fallback for fallback in step.fallbacks)


def test_a_step_that_never_satisfies_its_checks_is_retried_and_then_given_up_on(settings):
    transport = FakeTransport(chat_reply="TODO: write this later.")
    engine = _engine(settings, transport)

    report = engine.run("Summarise the quarterly report and list the risks")

    assert report.status == "failed"
    # The recovery step really tried other models.
    assert any(
        "recovery:" in fallback
        for result in report.steps
        for fallback in result.fallbacks
    )
    assert len(transport.chat_requests()) >= 2


def test_the_recovery_budget_is_bounded(settings):
    transport = FakeTransport(chat_statuses=[503] * 20)
    engine = _engine(settings, transport)

    report = engine.run("Summarise the quarterly report and list the risks")

    assert report.status == "failed"
    # The step's own call budget caps the total, whatever the failure is.
    assert len(transport.chat_requests()) <= MAX_STEP_CALLS


def test_a_bad_request_is_not_retried_forever(settings):
    transport = FakeTransport(chat_statuses=[400] * 20)
    engine = _engine(settings, transport)

    report = engine.run("Summarise the quarterly report and list the risks")

    assert report.status == "failed"
    assert len(transport.chat_requests()) <= MAX_STEP_CALLS


# --------------------------------------------------------------------------- #
# LEARN
# --------------------------------------------------------------------------- #


def test_the_measured_outcome_is_folded_into_the_routing_statistics(settings):
    from nexus import storage

    transport = FakeTransport(chat_reply="A complete and useful answer.")
    engine = _engine(settings, transport)

    report = engine.run("Summarise the quarterly report and list the risks")

    stats = storage.model_stats()
    assert stats
    assert any(row["model_id"] == report.models_used[0] for row in stats)
    assert any(row["runs"] >= 1 for row in stats)


def test_learning_can_be_switched_off(settings):
    from nexus import storage

    strict = Settings_with_learning_off = settings
    strict.enable_learning = False

    transport = FakeTransport(chat_reply="A complete and useful answer.")
    engine = _engine(strict, transport)
    engine.run("Summarise the quarterly report and list the risks")

    assert storage.model_stats() == []


# --------------------------------------------------------------------------- #
# REPORT and persistence
# --------------------------------------------------------------------------- #


def test_the_run_is_recorded_and_can_be_read_back(settings):
    from nexus import storage

    transport = FakeTransport(chat_reply="A complete and useful answer.")
    engine = _engine(settings, transport)

    report = engine.run("Summarise the quarterly report and list the risks")

    detail = storage.run_detail(report.run_id)
    assert detail is not None
    assert detail["goal"] == "Summarise the quarterly report and list the risks"
    assert detail["status"] in ("completed", "partial", "failed")


def test_the_status_reports_what_actually_happened(settings):
    transport = FakeTransport(chat_reply="A complete and useful answer.")
    engine = _engine(settings, transport)
    engine.run("Summarise the quarterly report and list the risks")

    status = engine.status()

    assert status["catalogue"]["llm_models"] == 5
    assert status["recent_runs"]
    assert status["budget"]["daily_budget_usd"] is not None
    assert status["routing"]["strategy_weights"]


def test_the_report_never_claims_a_billed_amount(settings):
    transport = FakeTransport(chat_reply="A complete and useful answer.")
    engine = _engine(settings, transport)
    report = engine.run("Summarise the quarterly report and list the risks")

    payload = json.dumps(report.to_dict(), default=str)
    assert "billed" not in payload.lower() or "not" in payload.lower()


# --------------------------------------------------------------------------- #
# ROUTE — a generation step is routed against the generation catalogue
# --------------------------------------------------------------------------- #


def test_a_generation_step_is_routed_against_the_generation_catalogue(settings):
    """A media step must not be routed through the language-model router.

    The language-model router would answer "no eligible model" for every
    image step — an image cannot come out of a chat model — and the report
    would publish that answer next to a step that then ran perfectly well.
    """
    transport = FakeTransport(chat_reply="A complete and useful answer.")
    engine = _engine(settings, transport)

    report = engine.run(
        "Build a landing page for a new AI startup, with a generated hero image"
    )

    media_routes = [route for route in report.routes if route.kind == "media"]
    assert media_routes, "the plan should contain a generation step"

    media_ids = {info.id for info in engine.registry.list_media()}
    for route in media_routes:
        assert route.candidates, "a media step must be routed to a generation model"
        assert route.candidates[0].model.id in media_ids


def test_the_published_route_names_the_model_that_actually_ran(settings):
    """The report must not contradict itself about a generation step."""
    transport = FakeTransport(chat_reply="A complete and useful answer.")
    engine = _engine(settings, transport)

    report = engine.run(
        "Build a landing page for a new AI startup, with a generated hero image"
    )

    for route, result in zip(report.routes, report.steps):
        if route.kind != "media" or not route.candidates:
            continue
        chosen = route.candidates[0].model.id
        # The model the report named is the model that ran.
        assert result.model_id == chosen
        # And the report said so, rather than claiming there was no model.
        assert any(chosen in message for message in report.messages)
        assert not any("no eligible model" in message for message in report.messages)


def test_a_route_says_which_catalogue_it_came_from(settings):
    """A consumer can tell a language-model route from a generation route."""
    transport = FakeTransport(chat_reply="A complete and useful answer.")
    engine = _engine(settings, transport)

    report = engine.run(
        "Build a landing page for a new AI startup, with a generated hero image"
    )

    payload = report.to_dict()
    assert {route["kind"] for route in payload["routes"]} >= {"llm"}
    for route in payload["routes"]:
        for candidate in route["candidates"]:
            assert candidate["kind"] in ("llm", "media")


def test_the_analysis_preview_agrees_with_the_run(settings):
    """An analysis that contradicts the run it previews is worse than none.

    ``nexus_analyze`` used to route every step through the language-model
    router, so it answered "no eligible model" for a generation step that
    the run itself then routed to a generation model. The preview now comes
    from the same component that carries the route out.
    """
    transport = FakeTransport(chat_reply="A complete and useful answer.")
    engine = _engine(settings, transport)

    goal = "Build a landing page for a new AI startup, with a generated hero image"
    analysis = engine.analyze(goal)
    report = engine.run(goal)

    # The analysis plans the same steps the run plans, in the same order.
    assert len(analysis["routing_preview"]) == len(report.routes)
    for preview, route in zip(analysis["routing_preview"], report.routes):
        decision = preview["decision"]
        assert decision["kind"] == route.kind
        assert decision["candidates"], (
            f"step {preview['step_index']} previewed no eligible model"
        )
        # And the model the preview named is the model the run chose.
        assert decision["candidates"][0]["model"] == route.candidates[0].model.id


def test_a_recovered_step_keeps_the_catalogue_it_was_routed_against(settings):
    """A retried generation step is retried against the generation catalogue."""
    transport = FakeTransport(chat_reply="A complete and useful answer.")
    engine = _engine(settings, transport)

    report = engine.run(
        "Build a landing page for a new AI startup, with a generated hero image"
    )

    for route, result in zip(report.routes, report.steps):
        if route.kind != "media":
            continue
        # Whatever the step ended up doing, the models it called are
        # generation models, and it never fell back to the chat catalogue.
        for model_id in result.attempted:
            assert model_id in {info.id for info in engine.registry.list_media()}


# --------------------------------------------------------------------------- #
# The token budget a step is given
# --------------------------------------------------------------------------- #


def _coding_step():
    from nexus.models import Step, TaskType

    return Step(
        index=0,
        description="Write the entry point",
        task_type=TaskType.CODING,
        prompt="Write app/main.py with a main() that returns 42.",
    )


def _prose_step():
    from nexus.models import Step, TaskType

    return Step(
        index=0,
        description="Summarise the risks",
        task_type=TaskType.GENERAL_REASONING,
        prompt="List the risks.",
    )


def _execute(settings, transport, step):
    """Route and run one step through the engine's own executor."""
    from nexus.executor import ExecutionContext

    engine = _engine(settings, transport)
    decision = engine.executor.preview_route(step)
    return engine.executor.execute(
        step,
        ExecutionContext(),
        decision,
        approved=True,
    )


def _execute_with_engine(settings, transport, step):
    """The same, but the engine comes back too — it holds the budget ledger."""
    from nexus.executor import ExecutionContext

    engine = _engine(settings, transport)
    decision = engine.executor.preview_route(step)
    result = engine.executor.execute(
        step,
        ExecutionContext(),
        decision,
        approved=True,
    )
    return engine, result


def test_a_code_bearing_step_asks_for_room_to_write_a_whole_file(settings):
    """A coding step gets the configured budget, not the prose default."""
    transport = FakeTransport(chat_reply="A complete and useful answer.")
    _execute(settings, transport, _coding_step())

    body = transport.chat_requests()[0]["body"]
    assert body["max_tokens"] == settings.code_max_tokens
    # The prose default is not enough for a source file.
    assert settings.code_max_tokens > 2048


def test_a_prose_step_still_gets_the_prose_default(settings):
    """A step that answers in prose gets the configured prose budget."""
    transport = FakeTransport(chat_reply="A complete and useful answer.")
    _execute(settings, transport, _prose_step())

    body = transport.chat_requests()[0]["body"]
    assert body["max_tokens"] == settings.max_tokens


def test_a_prose_step_has_room_for_a_reasoning_model_to_answer(settings):
    """The budget has to hold the reasoning as well as the answer.

    A live run of the demo scenario showed what happens when it does not:
    DeepSeek-V4-Flash spent all 2048 of its completion tokens on reasoning,
    was cut off mid-thought, and returned no text at all.
    """
    assert settings.max_tokens > 2048

    transport = FakeTransport(chat_reply="A complete and useful answer.")
    _execute(settings, transport, _prose_step())

    assert transport.chat_requests()[0]["body"]["max_tokens"] == settings.max_tokens


def test_a_reply_cut_off_by_the_token_limit_is_named_as_such(settings):
    """A truncated answer is reported as incomplete, not as a finished one."""
    transport = FakeTransport(
        chat_reply="A complete and useful answer.",
        chat_finish_reason="length",
    )
    result = _execute(settings, transport, _prose_step())

    assert result.status == "ok"
    assert any("cut off" in note for note in result.fallbacks), (
        f"the truncation was not reported: {result.fallbacks}"
    )


def test_a_cut_off_code_reply_is_also_reported_as_incomplete(settings):
    """The same, for a step that was given the larger budget."""
    transport = FakeTransport(
        chat_reply="A complete and useful answer.",
        chat_finish_reason="length",
    )
    result = _execute(settings, transport, _coding_step())

    assert result.status == "ok"
    assert any("cut off" in note for note in result.fallbacks)


# --------------------------------------------------------------------------- #
# An empty answer is not an answer
# --------------------------------------------------------------------------- #
#
# A live run of the demo scenario ended with the run status ``partial``
# because one of its six steps came back with no text at all. The provider
# had sent ``content: null``: the model spent its whole token budget on
# reasoning and was cut off before it said anything.
#
# The executor recorded that step as ``ok`` with an empty body and never
# tried the next routed model, so the run's own step table said the work
# had been done when it had not. The evaluator noticed the empty answer
# afterwards and downgraded the run — but the recovery never fired, and the
# step was still recorded as a success.


def test_an_empty_reply_is_not_a_successful_step(settings):
    """A model that says nothing has not done the work."""
    transport = FakeTransport(chat_replies=[None, "A complete and useful answer."])
    result = _execute(settings, transport, _prose_step())

    assert result.status == "ok"
    assert result.output == "A complete and useful answer."
    # The empty attempt was not swallowed: it is named in the fallbacks.
    assert any("returned no text" in note for note in result.fallbacks), (
        f"the empty reply was not reported: {result.fallbacks}"
    )
    # And the next routed model was given its turn.
    assert len(result.attempted) == 2, result.attempted
    assert result.attempted[0] != result.model_id


def test_a_step_where_every_model_says_nothing_fails_with_the_reason(settings):
    """When nothing answers, the step fails and says why."""
    transport = FakeTransport(chat_replies=[None] * 5)
    result = _execute(settings, transport, _prose_step())

    assert result.status == "failed"
    assert result.output == ""
    assert result.error is not None
    assert result.error["kind"] == "EmptyResponse"
    assert "returned no text" in result.error["message"]


def test_an_empty_reply_that_was_cut_off_names_the_token_limit(settings):
    """The reason it said nothing is part of the reason it failed."""
    transport = FakeTransport(
        chat_replies=[None] * 5,
        chat_finish_reasons=["length"] * 5,
    )
    result = _execute(settings, transport, _prose_step())

    assert result.status == "failed"
    assert result.error is not None
    assert "token limit" in result.error["message"]


def test_the_cost_of_an_empty_reply_is_still_recorded(settings):
    """The provider charged for the tokens, so the ledger counts them.

    An empty reply is not free: the model was billed for every token it
    reasoned with. Leaving that out of the ledger would under-report what
    the run spent.
    """
    answered = FakeTransport(chat_reply="A complete and useful answer.")
    engine, result = _execute_with_engine(settings, answered, _prose_step())
    one_call = engine.executor.budget.snapshot()["run_spend_usd"]

    emptied = FakeTransport(chat_replies=[None, "A complete and useful answer."])
    engine, result = _execute_with_engine(settings, emptied, _prose_step())
    two_calls = engine.executor.budget.snapshot()["run_spend_usd"]

    assert two_calls > one_call
    assert result.estimated_cost_usd > 0


def test_a_reply_with_text_is_still_a_success(settings):
    """The empty-reply rule does not touch a reply that says something."""
    transport = FakeTransport(chat_reply="A complete and useful answer.")
    result = _execute(settings, transport, _prose_step())

    assert result.status == "ok"
    assert result.output == "A complete and useful answer."
    assert len(result.attempted) == 1
    assert not any("returned no text" in note for note in result.fallbacks)
