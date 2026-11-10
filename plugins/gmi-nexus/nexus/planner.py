"""Goal decomposition.

The planner turns one goal into an ordered list of steps. It works from the
request itself, using two mechanisms:

* **Artefact templates.** A goal that names a deliverable ("build a landing
  page", "write a script", "design an API") has a known shape. Templates give
  the run a structure that matches how the work is actually done — understand
  the brief, produce the content, generate the assets, implement, review,
  verify — instead of one opaque prompt.
* **Clause splitting.** A goal written as a list ("write the copy, then
  generate a hero image, then implement it") is split on its own punctuation
  and connectives, and each clause is classified separately.

A model can also propose a plan. When it does, the plan is validated against
the same vocabulary before it is accepted; a malformed plan is discarded rather
than half-applied.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Sequence

from .classifier import Classification, classify_text
from .models import Modality, Plan, Step, TaskType

#: Connectives that split a goal into ordered clauses.
_CONNECTIVES = re.compile(
    r"(?:^|\s)(?:then|next|after\s+that|afterwards|and\s+then|finally|also|"
    r"followed\s+by|second(?:ly)?|third|fourth|lastly)\b[,: ]*",
    re.IGNORECASE,
)

#: Numbered or bulleted list markers.
_LIST_MARKER = re.compile(r"(?:^|\n)\s*(?:\d+[.)]|[-*•])\s+")

#: References to the output of an earlier clause.
_BACK_REFERENCE = re.compile(
    r"\b(using|with|from|based\s+on|incorporating|including|applying)\s+"
    r"(that|those|these|it|them|the\s+above|above)\b",
    re.IGNORECASE,
)


def _artefact_template(goal: str) -> list[tuple[str, TaskType, tuple[Modality, ...]]] | None:
    """Return a step template for the deliverable the goal names."""
    lowered = goal.lower()

    if re.search(r"\b(landing\s*page|splash\s*page|one[- ]pager|marketing\s*page)\b", lowered):
        return [
            ("Analyse the brief and settle the page structure", TaskType.PLANNING, ()),
            ("Write the section copy and call to action", TaskType.GENERAL_REASONING, ()),
            ("Generate the hero image and supporting visual assets", TaskType.IMAGE_GENERATION, (Modality.IMAGE,)),
            ("Implement the page — markup, styling and layout", TaskType.CODING, ()),
            ("Review the result for visual and structural problems", TaskType.CODE_REVIEW, ()),
            ("Verify the finished page and summarise the result", TaskType.GENERAL_REASONING, ()),
        ]

    if re.search(r"\b(website|web\s*page|homepage|web\s*app|dashboard)\b", lowered):
        return [
            ("Analyse the brief and define the site's pages", TaskType.PLANNING, ()),
            ("Write the content for each page", TaskType.GENERAL_REASONING, ()),
            ("Generate the visual assets", TaskType.IMAGE_GENERATION, (Modality.IMAGE,)),
            ("Implement the site — markup, styling and navigation", TaskType.CODING, ()),
            ("Review the implementation", TaskType.CODE_REVIEW, ()),
            ("Verify the result and summarise", TaskType.GENERAL_REASONING, ()),
        ]

    if re.search(r"\b(api|service|endpoint|backend|micro\s*service)\b", lowered):
        return [
            ("Analyse the requirements and define the interface", TaskType.PLANNING, ()),
            ("Design the data model and error handling", TaskType.COMPLEX_REASONING, ()),
            ("Implement the service", TaskType.CODING, ()),
            ("Write tests for the new code", TaskType.CODING, ()),
            ("Review the implementation", TaskType.CODE_REVIEW, ()),
            ("Verify the tests pass and summarise", TaskType.GENERAL_REASONING, ()),
        ]

    if re.search(r"\b(script|tool|automation|cli|utility)\b", lowered):
        return [
            ("Analyse what the script must do", TaskType.PLANNING, ()),
            ("Implement the script", TaskType.CODING, ()),
            ("Test the script against the expected cases", TaskType.CODING, ()),
            ("Review the result and summarise", TaskType.CODE_REASONING, ()),
        ]

    if re.search(r"\b(analyse|analyze|research|investigate|study)\b", lowered) and re.search(
        r"\b(topic|question|trend|market|approach|options)\b", lowered
    ):
        return [
            ("Frame the question and what would answer it", TaskType.PLANNING, ()),
            ("Gather and analyse the material", TaskType.COMPLEX_REASONING, ()),
            ("Weigh the options and their trade-offs", TaskType.COMPLEX_REASONING, ()),
            ("Write the recommendation", TaskType.GENERAL_REASONING, ()),
        ]

    return None


def split_clauses(goal: str) -> list[str]:
    """Split a goal into ordered clauses."""
    text = goal.strip()
    if not text:
        return []

    # A bulleted or numbered list is the clearest statement of the steps.
    if _LIST_MARKER.search(text):
        parts = [part.strip() for part in _LIST_MARKER.split(text) if part.strip()]
        if len(parts) > 1:
            return parts

    # Otherwise split on sentence boundaries, then on connectives.
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]
    clauses: list[str] = []
    for sentence in sentences:
        parts = [p.strip() for p in _CONNECTIVES.split(sentence) if p and p.strip()]
        clauses.extend(parts if parts else [sentence])
    return [c for c in clauses if c]


class Planner:
    """Builds a plan for a goal."""

    def __init__(self, planner_llm: Callable[[str], str] | None = None) -> None:
        #: Optional model callable used for goals the rules cannot structure.
        self.planner_llm = planner_llm

    def plan(
        self,
        goal: str,
        classification: Classification | None = None,
        *,
        context: dict[str, Any] | None = None,
    ) -> Plan:
        """Return the plan for a goal."""
        goal = (goal or "").strip()
        if not goal:
            return Plan(goal=goal, notes=["The goal was empty."])

        template = _artefact_template(goal)
        if template is not None:
            plan = self._plan_from_template(goal, template)
            plan.notes.append("Plan derived from the deliverable template.")
            return plan

        clauses = split_clauses(goal)
        if len(clauses) > 1:
            plan = self._plan_from_clauses(goal, clauses)
            plan.notes.append(f"Plan derived from {len(clauses)} clauses in the request.")
            return plan

        # A single instruction: one step, classified on its own.
        task_type = classification.task_type if classification else classify_text(goal).task_type
        modalities = self._modalities_for(task_type)
        return Plan(
            goal=goal,
            steps=[
                Step(
                    index=0,
                    description=goal,
                    task_type=task_type,
                    prompt=goal,
                    modalities=modalities,
                )
            ],
            notes=["The request was a single instruction, so it became a single step."],
        )

    # -- strategies --------------------------------------------------------- #

    def _plan_from_template(
        self,
        goal: str,
        template: Sequence[tuple[str, TaskType, tuple[Modality, ...]]],
    ) -> Plan:
        steps: list[Step] = []
        for index, (description, task_type, modalities) in enumerate(template):
            steps.append(
                Step(
                    index=index,
                    description=description,
                    task_type=task_type,
                    prompt=self._prompt_for(index, description, task_type, goal),
                    modalities=modalities,
                )
            )
        return Plan(goal=goal, steps=steps)

    def _plan_from_clauses(self, goal: str, clauses: Sequence[str]) -> Plan:
        steps: list[Step] = []
        previous_index: int | None = None
        for index, clause in enumerate(clauses):
            classification = classify_text(clause)
            depends_on: tuple[int, ...] = ()
            if previous_index is not None and _BACK_REFERENCE.search(clause):
                depends_on = (previous_index,)
            steps.append(
                Step(
                    index=index,
                    description=clause,
                    task_type=classification.task_type,
                    prompt=clause,
                    modalities=self._modalities_for(classification.task_type),
                    depends_on=depends_on,
                )
            )
            previous_index = index
        return Plan(goal=goal, steps=steps)

    def _modalities_for(self, task_type: TaskType) -> tuple[Modality, ...]:
        if task_type in (TaskType.IMAGE_GENERATION, TaskType.IMAGE_EDITING):
            return (Modality.IMAGE,)
        if task_type is TaskType.VIDEO:
            return (Modality.VIDEO,)
        if task_type is TaskType.AUDIO:
            return (Modality.AUDIO,)
        return ()

    def _prompt_for(self, index: int, description: str, task_type: TaskType, goal: str) -> str:
        """Build the prompt that carries out one step."""
        if index == 0:
            return (
                f"Original goal: {goal}\n\n"
                f"Your job: {description}. State the concrete decisions and structure "
                "that the following steps will use."
            )
        if task_type is TaskType.IMAGE_GENERATION:
            return (
                f"Original goal: {goal}\n\n"
                f"Generate the visual assets for this project. {description}. "
                "Describe each asset you produce (subject, style, composition, palette) "
                "so the assets can be reviewed later."
            )
        if task_type is TaskType.CODING:
            return (
                f"Original goal: {goal}\n\n"
                f"{description}. Produce complete, runnable code — no placeholders, no "
                "pseudo-code. Use the assets and copy produced by the previous steps."
            )
        if task_type is TaskType.CODE_REVIEW:
            return (
                f"Original goal: {goal}\n\n"
                f"{description}. List concrete problems, each with the file or element it "
                "affects and how to fix it. Report no problem you cannot point at."
            )
        if task_type is TaskType.PLANNING:
            return f"Original goal: {goal}\n\n{description}."
        return f"Original goal: {goal}\n\n{description}."

    # -- model-assisted planning -------------------------------------------- #

    def plan_with_model(self, goal: str) -> Plan | None:
        """Ask a model for a plan and validate it before accepting it."""
        if self.planner_llm is None:
            return None
        prompt = (
            "Break the following goal into 2 to 6 ordered steps. "
            "Reply with JSON only: {\"steps\": [{\"description\": \"...\", "
            "\"task_type\": \"...\"}]}\n\n"
            "Allowed task types: " + ", ".join(t.value for t in TaskType) + "\n\n"
            f"Goal: {goal}"
        )
        try:
            raw = self.planner_llm(prompt)
        except Exception:
            return None
        if not raw:
            return None
        payload = _extract_json(raw)
        if not payload:
            return None
        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            return None

        steps: list[Step] = []
        for index, item in enumerate(raw_steps):
            if not isinstance(item, dict):
                return None
            description = str(item.get("description") or "").strip()
            if not description:
                return None
            try:
                task_type = TaskType(str(item.get("task_type") or "").strip().lower())
            except ValueError:
                task_type = TaskType.GENERAL_REASONING
            steps.append(
                Step(
                    index=index,
                    description=description,
                    task_type=task_type,
                    prompt=description,
                    modalities=self._modalities_for(task_type),
                )
            )
        return Plan(goal=goal, steps=steps, notes=["Plan proposed by a model and validated."])


def _extract_json(raw: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a model response."""
    text = raw.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


__all__ = ["Planner", "split_clauses"]
