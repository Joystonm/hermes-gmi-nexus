"""Task classification and planning."""

from __future__ import annotations

import pytest

from nexus.classifier import classify, classify_text
from nexus.models import Capability, TaskType
from nexus.planner import Planner, split_clauses


def test_a_coding_request_is_classified_as_coding():
    result = classify_text("Write a Python function that parses CSV files.")
    assert result.task_type is TaskType.CODING
    assert Capability.CODE in result.required_capabilities
    assert result.evidence


def test_an_image_generation_request_is_classified_as_such():
    result = classify_text("Generate a hero image of a mountain at sunset.")
    assert result.task_type is TaskType.IMAGE_GENERATION


def test_an_image_under_the_request_is_not_a_generation_request():
    result = classify_text("What is wrong with this screenshot?")
    assert result.task_type is TaskType.IMAGE_UNDERSTANDING


def test_an_attached_image_makes_it_a_multimodal_request():
    result = classify_text("Explain what this picture shows", media_mimes=("image/png",))
    assert result.task_type in (
        TaskType.IMAGE_UNDERSTANDING,
        TaskType.MULTIMODAL,
        TaskType.DOCUMENT_ANALYSIS,
    )
    assert result.media_count == 1


def test_a_named_model_is_honoured():
    result = classify_text("Use gpt-4o-mini to summarise this document for me.")
    assert result.requested_model == "gpt-4o-mini"


def test_classification_records_which_route_it_took():
    result = classify_text("Review this code for security problems.")
    assert result.task_type is TaskType.CODE_REVIEW
    assert result.model_assisted is False
    assert result.confidence > 0


def test_a_model_is_only_consulted_when_the_rules_are_unsure():
    calls: list[str] = []

    def classifier(prompt: str) -> str:
        calls.append(prompt)
        return '{"task_type": "coding", "confidence": 0.9}'

    # A clear request never reaches the model.
    classify("Write a Python function to sort a list", classifier_llm=classifier)
    assert calls == []

    # A vague one does.
    classify("do the thing with the stuff", classifier_llm=classifier)
    assert calls


def test_split_clauses_finds_the_separate_asks():
    parts = split_clauses("Design the API, then implement it, and finally write the docs.")
    assert len(parts) >= 3


def test_the_planner_breaks_a_landing_page_request_into_stages():
    plan = Planner().plan(
        "Build a landing page for a new AI startup called Vantage",
        classify_text("Build a landing page for a new AI startup called Vantage"),
    )
    assert plan.steps
    descriptions = " ".join(step.description.lower() for step in plan.steps)
    assert "visual" in descriptions or "image" in descriptions or "asset" in descriptions


def test_every_planned_step_carries_the_prompt_it_needs():
    plan = Planner().plan(
        "Summarise the quarterly report and list the risks",
        classify_text("Summarise the quarterly report and list the risks"),
    )
    assert plan.steps
    for step in plan.steps:
        assert step.prompt.strip()
        assert step.description.strip()


def test_an_empty_goal_produces_no_steps():
    plan = Planner().plan("", None)
    assert plan.steps == []
    assert plan.notes
