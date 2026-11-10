"""Evaluating whether a step actually produced what it was asked for.

Evaluation has two layers, and it always says which ones ran:

* **Deterministic checks** — free, fast and certain. Did the step return
  anything? Does a coding step return code? Does an image step return media
  URLs rather than a description of one?
* **Independent verification** — a *different* model is asked to judge the
  output against the step's requirement. This costs money, so it is opt-in and
  never runs against the model that produced the answer.

A step that fails its checks is not silently marked as done. The report says
what failed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .models import Step, StepResult, TaskType

#: The prompt used for independent verification.
#:
#: The requirement and output are spliced in by string replacement rather than
#: by ``str.format``, because the prompt contains literal JSON braces that
#: ``format`` would try to interpret as replacement fields.
VERIFY_PROMPT = (
    "You are checking another system's work. You are given a requirement and the "
    "output that was produced. Decide whether the output satisfies the "
    "requirement.\n\n"
    "Reply with JSON only: {\"passed\": true|false, \"score\": 0.0-1.0, "
    "\"reason\": \"one sentence\"}\n\n"
    "Requirement:\n__REQUIREMENT__\n\n"
    "Output:\n__OUTPUT__\n"
)

#: Patterns that suggest a step produced runnable code.
_CODE_FENCE = re.compile(r"```", re.MULTILINE)

#: Markers that show a step deferred the work instead of doing it.
_PLACEHOLDER = re.compile(
    r"\b(TODO|FIXME|TBD|XXX|placeholder|lorem ipsum|coming soon|"
    r"to be (?:written|added|completed|implemented|done))\b"
    r"|\byour code here\b"
    r"|\bimplement this\b"
    r"|\binsert [^.\n]{0,30} here\b"
    r"|\[(?:your|insert|add|placeholder)[^\]]*\]",
    re.IGNORECASE,
)

#: An answer longer than this that merely mentions a marker is still an answer.
_PLACEHOLDER_LENGTH_LIMIT = 240


def _is_placeholder(text: str) -> bool:
    """Whether an output is a deferral rather than an answer.

    A long answer that happens to mention "TODO" is still an answer, so the
    check only fires on a short output that leads with the marker — which is
    what an unfinished response looks like.
    """
    match = _PLACEHOLDER.search(text)
    if not match:
        return False
    if len(text) > _PLACEHOLDER_LENGTH_LIMIT:
        return False
    return match.start() <= 2 or len(match.group(0)) >= 8


@dataclass(slots=True)
class Evaluation:
    """The verdict on one step."""

    passed: bool
    score: float
    method: str
    checks: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "score": round(self.score, 3),
            "method": self.method,
            "checks": list(self.checks),
            "failures": list(self.failures),
            "reason": self.reason,
        }


class Evaluator:
    """Checks step results."""

    def __init__(self, verifier: Callable[[str], str] | None = None) -> None:
        #: Optional callable used for independent verification.
        self.verifier = verifier

    # -- deterministic checks ----------------------------------------------- #

    def check(self, step: Step, result: StepResult) -> Evaluation:
        """Run the deterministic checks for one step."""
        checks: list[str] = []
        failures: list[str] = []

        if result.status != "ok":
            failures.append(f"the step did not succeed (status: {result.status})")
            return Evaluation(
                passed=False,
                score=0.0,
                method="deterministic",
                checks=checks,
                failures=failures,
                reason="the step failed before it could be evaluated",
            )

        if step.task_type in (TaskType.IMAGE_GENERATION, TaskType.IMAGE_EDITING):
            if not result.media_urls:
                failures.append("the step reported success but produced no media URLs")
            else:
                checks.append(f"{len(result.media_urls)} media URL(s) were returned")
        elif step.task_type in (TaskType.VIDEO, TaskType.AUDIO):
            if not result.media_urls:
                failures.append("the step reported success but produced no media URLs")
            else:
                checks.append(f"{len(result.media_urls)} media URL(s) were returned")
        else:
            text = (result.output or "").strip()
            if not text:
                failures.append("the model returned an empty response")
            else:
                checks.append(f"the model returned {len(text)} characters")
                if _is_placeholder(text):
                    failures.append("the model returned a placeholder instead of an answer")
                else:
                    checks.append("the response is not a placeholder")

        if step.task_type is TaskType.CODING and result.status == "ok":
            body = result.output or ""
            if not _CODE_FENCE.search(body):
                failures.append("a coding step returned no code block")
            else:
                checks.append("the coding step returned a code block")
            if _PLACEHOLDER.search(body):
                failures.append("the coding step left a placeholder in the output")
            else:
                checks.append("the coding step contains no placeholders")

        if step.task_type is TaskType.CODE_REVIEW and result.status == "ok":
            body = result.output or ""
            if len(body) < 80:
                failures.append("the review is too short to be actionable")
            else:
                checks.append("the review produced substantive findings")

        score = 1.0 if not failures else max(0.0, 1.0 - 0.34 * len(failures))
        return Evaluation(
            passed=not failures,
            score=score,
            method="deterministic",
            checks=checks,
            failures=failures,
            reason="all deterministic checks passed" if not failures else "; ".join(failures),
        )

    # -- independent verification ------------------------------------------- #

    def verify(
        self,
        step: Step,
        result: StepResult,
        *,
        model_id: str | None = None,
    ) -> Evaluation:
        """Verify a step's output with an independent model."""
        base = self.check(step, result)
        if self.verifier is None:
            base.method = base.method + " (no verifier configured)"
            return base
        if result.status != "ok":
            return base

        prompt = VERIFY_PROMPT.replace(
            "__REQUIREMENT__", step.description
        ).replace("__OUTPUT__", (result.output or "")[:8000])
        try:
            raw = self.verifier(prompt)
        except Exception as exc:
            base.failures.append(f"the verifier could not be reached: {exc}")
            base.passed = False
            base.method = base.method + " + model (verifier failed)"
            return base

        verdict = _parse_verdict(raw)
        if verdict is None:
            base.failures.append("the verifier's reply could not be parsed")
            base.passed = False
            base.method = base.method + " + model (unusable reply)"
            return base

        passed, score, reason = verdict
        # The verifier is advisory: it can lower a passing score but a
        # deterministic failure is never overruled.
        combined_score = round((base.score + score) / 2.0, 3)
        final_passed = base.passed and passed
        failures = list(base.failures)
        if not passed:
            failures.append(f"independent verification failed: {reason}")
        method = base.method + " + model"
        if model_id:
            method += f" ({model_id})"
        return Evaluation(
            passed=final_passed,
            score=combined_score,
            method=method,
            checks=list(base.checks) + ["an independent model reviewed the output"],
            failures=failures,
            reason=reason,
        )


def _parse_verdict(raw: str | None) -> tuple[bool, float, str] | None:
    """Parse the verifier's JSON verdict."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    import json

    try:
        payload = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    passed = bool(payload.get("passed"))
    try:
        score = float(payload.get("score", 1.0 if passed else 0.0))
    except (TypeError, ValueError):
        score = 1.0 if passed else 0.0
    score = max(0.0, min(1.0, score))
    reason = str(payload.get("reason") or "").strip()
    return passed, score, reason


__all__ = ["Evaluation", "Evaluator", "VERIFY_PROMPT"]
