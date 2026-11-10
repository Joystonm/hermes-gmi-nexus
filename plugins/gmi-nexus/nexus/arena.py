"""The Arena: real benchmarks across the live catalogue.

The Arena runs the *same* test cases against every model in a comparison, so
the numbers are comparable. Every figure it produces is measured: real API
calls, real latencies, real token counts from the provider's own usage block,
and real costs computed from the catalogue's published prices.

What it does not do is present an estimate as a measurement. A model that
failed is recorded as failed, a suite that could not run is recorded as
skipped, and the summary says how many cases each model actually completed.

The Arena's results feed the router: a model's measured score becomes one of
the signals the router weighs when it chooses where to send the next task.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from .config import Settings
from .errors import NexusError
from .models import ModelInfo, TaskType

#: How a model's answer is judged against the expected answer.
CHECK_CONTAINS = "contains"
CHECK_REGEX = "regex"
CHECK_JSON_KEYS = "json_keys"
CHECK_EQUALS = "equals"
CHECK_NON_EMPTY = "non_empty"


@dataclass(slots=True)
class BenchmarkCase:
    """One benchmark case, run identically against every model."""

    name: str
    prompt: str
    task_type: TaskType = TaskType.GENERAL_REASONING
    check: str = CHECK_NON_EMPTY
    expected: Any = None
    max_tokens: int = 512
    #: An optional judge prompt; when set, a judge model scores the answer.
    judge_criterion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "task_type": self.task_type.value,
            "check": self.check,
            "expected": self.expected,
            "max_tokens": self.max_tokens,
            "judge_criterion": self.judge_criterion,
        }


@dataclass(slots=True)
class CaseResult:
    """One model's answer to one case."""

    model_id: str
    case: BenchmarkCase
    passed: bool
    score: float
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    output: str = ""
    error: str | None = None
    judged: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "case": self.case.name,
            "passed": self.passed,
            "score": round(self.score, 3),
            "latency_ms": self.latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 8),
            "error": self.error,
            "judged": self.judged,
            "output": self.output[:2000],
        }


#: The default suite. Deliberately mixed: instruction following, formatting,
#: code, reasoning and summarisation, so a model cannot pass on one axis.
DEFAULT_SUITE: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        name="exact_token",
        prompt="Reply with exactly the single word NEXUS and nothing else.",
        check=CHECK_EQUALS,
        expected="NEXUS",
        max_tokens=32,
    ),
    BenchmarkCase(
        name="instruction_adherence",
        prompt=(
            "List exactly three colours, one per line, with no numbering and no other text."
        ),
        check=CHECK_REGEX,
        expected=r"^[^\n]+\n[^\n]+\n[^\n]+$",
        max_tokens=64,
    ),
    BenchmarkCase(
        name="json_contract",
        prompt=(
            'Return a JSON object with exactly the keys "name" (a string) and "count" '
            '(an integer). No other keys, no commentary.'
        ),
        check=CHECK_JSON_KEYS,
        expected=["name", "count"],
        max_tokens=128,
    ),
    BenchmarkCase(
        name="code_generation",
        prompt=(
            "Write a Python function named `fibonacci` that returns the nth Fibonacci "
            "number. Reply with the code only."
        ),
        check=CHECK_CONTAINS,
        expected="def fibonacci",
        task_type=TaskType.CODING,
        max_tokens=512,
    ),
    BenchmarkCase(
        name="arithmetic_reasoning",
        prompt=(
            "A train leaves at 09:15 and arrives at 13:40. How many minutes is the "
            "journey? Reply with the number only."
        ),
        check=CHECK_CONTAINS,
        expected="265",
        task_type=TaskType.COMPLEX_REASONING,
        max_tokens=256,
    ),
    BenchmarkCase(
        name="summarisation",
        prompt=(
            "Summarise this in one sentence: The database migration was scheduled for "
            "Saturday but the backup job failed silently at 02:14, so the migration was "
            "rolled back after forty minutes and the release slipped to Tuesday."
        ),
        check=CHECK_REGEX,
        expected=r"(backup|migration|rollback|Tuesday|release)",
        max_tokens=256,
    ),
)


def judge_prompt(case: BenchmarkCase, output: str) -> str:
    """Build the prompt for an independent judge."""
    criterion = case.judge_criterion or "Does the answer satisfy the request?"
    return (
        "Rate the answer from 0.0 to 1.0 against the criterion. "
        "Reply with JSON only: {\"score\": 0.0, \"reason\": \"one sentence\"}\n\n"
        f"Criterion: {criterion}\n\nRequest:\n{case.prompt}\n\nAnswer:\n{output[:6000]}"
    )


class Arena:
    """Runs benchmarks against the live catalogue."""

    def __init__(
        self,
        settings: Settings,
        llm_client: Any,
        *,
        judge: Callable[[str], str] | None = None,
        judge_model: str | None = None,
    ) -> None:
        self.settings = settings
        self.llm_client = llm_client
        self.judge = judge
        self.judge_model = judge_model

    # -- running ------------------------------------------------------------ #

    def run(
        self,
        models: Sequence[ModelInfo],
        suite: Sequence[BenchmarkCase] | None = None,
        *,
        budget: Any | None = None,
        approved: bool = False,
        suite_name: str = "default",
    ) -> dict[str, Any]:
        """Run the suite against every model and return the comparison."""
        cases = list(suite or DEFAULT_SUITE)
        benchmark_id = uuid.uuid4().hex
        model_ids = [m.id for m in models]
        from . import storage

        storage.create_benchmark(benchmark_id, suite_name, model_ids)

        results: list[CaseResult] = []
        started = time.time()

        for model in models:
            for index, case in enumerate(cases):
                result = self._run_case(model, case, index, budget=budget, approved=approved)
                results.append(result)
                storage.record_benchmark_result(benchmark_id, result.to_dict())

        summary = self.summarise(results, models)
        summary["benchmark_id"] = benchmark_id
        summary["suite"] = suite_name
        summary["cases"] = len(cases)
        summary["duration_seconds"] = round(time.time() - started, 2)
        storage.finish_benchmark(benchmark_id, "completed", summary)
        return summary

    def _run_case(
        self,
        model: ModelInfo,
        case: BenchmarkCase,
        index: int,
        *,
        budget: Any | None,
        approved: bool,
    ) -> CaseResult:
        """Run one case against one model."""
        result = CaseResult(model_id=model.id, case=case, passed=False, score=0.0)
        estimate = model.pricing.cost(
            prompt_tokens=max(1, len(case.prompt) // 4),
            completion_tokens=case.max_tokens,
            requests=1,
        )
        if budget is not None:
            try:
                budget.enforce(
                    estimate,
                    approved=approved,
                    description=f"a benchmark call to {model.id}",
                )
            except NexusError as exc:
                result.error = f"not run: {exc}"
                return result

        started = time.perf_counter()
        try:
            response = self.llm_client.chat(
                model.id,
                [{"role": "user", "content": case.prompt}],
                max_tokens=case.max_tokens,
                temperature=0.0,
                model_info=model,
            )
        except NexusError as exc:
            result.error = str(exc)
            result.latency_ms = int((time.perf_counter() - started) * 1000)
            return result
        except Exception as exc:  # pragma: no cover - defensive
            result.error = str(exc)
            result.latency_ms = int((time.perf_counter() - started) * 1000)
            return result

        result.latency_ms = response.latency_ms or int((time.perf_counter() - started) * 1000)
        result.prompt_tokens = response.usage.prompt_tokens
        result.completion_tokens = response.usage.completion_tokens
        result.cost_usd = response.usage.cost_usd
        result.output = response.text

        passed, score = self._score(case, response.text)
        if self.judge is not None and case.judge_criterion:
            judged_score, reason = self._judge(case, response.text)
            if judged_score is not None:
                result.judged = True
                score = round((score + judged_score) / 2.0, 3)
                passed = passed and judged_score >= 0.5
                result.error = None if result.error else None
        result.passed = passed
        result.score = score
        return result

    # -- scoring ------------------------------------------------------------ #

    def _score(self, case: BenchmarkCase, output: str) -> tuple[bool, float]:
        """Score an answer with the case's deterministic check."""
        text = (output or "").strip()
        if not text:
            return False, 0.0

        if case.check == CHECK_EQUALS:
            return (text == str(case.expected), 1.0 if text == str(case.expected) else 0.0)

        if case.check == CHECK_CONTAINS:
            found = str(case.expected).lower() in text.lower()
            return found, 1.0 if found else 0.0

        if case.check == CHECK_REGEX:
            found = re.search(str(case.expected), text, re.MULTILINE | re.IGNORECASE) is not None
            return found, 1.0 if found else 0.0

        if case.check == CHECK_JSON_KEYS:
            try:
                payload = json.loads(text)
            except (ValueError, TypeError):
                # Some models wrap JSON in prose or a fence.
                start = text.find("{")
                end = text.rfind("}")
                if start == -1 or end <= start:
                    return False, 0.0
                try:
                    payload = json.loads(text[start : end + 1])
                except (ValueError, TypeError):
                    return False, 0.0
            if not isinstance(payload, dict):
                return False, 0.0
            keys = set(payload.keys())
            wanted = set(case.expected or [])
            return wanted.issubset(keys), 1.0 if wanted.issubset(keys) else 0.0

        return True, 1.0

    def _judge(self, case: BenchmarkCase, output: str) -> tuple[float | None, str]:
        """Ask the judge model to score the answer."""
        if self.judge is None:
            return None, ""
        try:
            raw = self.judge(judge_prompt(case, output))
        except Exception:
            return None, ""
        if not raw:
            return None, ""
        text = raw.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None, ""
        try:
            payload = json.loads(text[start : end + 1])
        except (ValueError, TypeError):
            return None, ""
        try:
            score = float(payload.get("score", 0.0))
        except (TypeError, ValueError):
            return None, ""
        return max(0.0, min(1.0, score)), str(payload.get("reason") or "")

    # -- summarising -------------------------------------------------------- #

    def summarise(self, results: Sequence[CaseResult], models: Sequence[ModelInfo]) -> dict[str, Any]:
        """Turn per-case results into a comparison."""
        by_model: dict[str, list[CaseResult]] = {}
        for result in results:
            by_model.setdefault(result.model_id, []).append(result)

        scores: dict[str, float] = {}
        rows: list[dict[str, Any]] = []
        for model in models:
            model_results = by_model.get(model.id, [])
            completed = [r for r in model_results if r.error is None]
            failed = [r for r in model_results if r.error is not None]
            passed = [r for r in completed if r.passed]
            pass_rate = (len(passed) / len(completed)) if completed else 0.0
            mean_score = (
                sum(r.score for r in completed) / len(completed) if completed else 0.0
            )
            scores[model.id] = round(pass_rate, 4)
            rows.append(
                {
                    "model_id": model.id,
                    "cases": len(model_results),
                    "completed": len(completed),
                    "failed": len(failed),
                    "passed": len(passed),
                    "pass_rate": round(pass_rate, 4),
                    "mean_score": round(mean_score, 4),
                    "total_latency_ms": sum(r.latency_ms for r in model_results),
                    "mean_latency_ms": (
                        round(sum(r.latency_ms for r in model_results) / len(model_results))
                        if model_results
                        else 0
                    ),
                    "prompt_tokens": sum(r.prompt_tokens for r in model_results),
                    "completion_tokens": sum(r.completion_tokens for r in model_results),
                    "total_cost_usd": round(sum(r.cost_usd for r in model_results), 8),
                    "errors": [r.error for r in failed if r.error],
                }
            )

        rows.sort(key=lambda row: (-row["pass_rate"], row["total_latency_ms"]))
        return {
            "scores": scores,
            "models": rows,
            "measured": True,
            "note": (
                "every figure here comes from real API calls: latencies were measured, "
                "token counts came from the provider's usage block, and costs use the "
                "catalogue's published prices"
            ),
        }

    # -- reporting ---------------------------------------------------------- #

    @staticmethod
    def compare(benchmark_id: str) -> dict[str, Any]:
        """Return a stored benchmark with its per-case results."""
        from . import storage

        benchmarks = storage.list_benchmarks(limit=100)
        benchmark = next((b for b in benchmarks if b.get("id") == benchmark_id), None)
        if benchmark is None:
            return {"error": f"no benchmark with id {benchmark_id}"}
        return {
            "benchmark": benchmark,
            "results": storage.benchmark_results(benchmark_id),
        }


__all__ = [
    "CHECK_CONTAINS",
    "CHECK_EQUALS",
    "CHECK_JSON_KEYS",
    "CHECK_NON_EMPTY",
    "CHECK_REGEX",
    "DEFAULT_SUITE",
    "Arena",
    "CaseResult",
    "BenchmarkCase",
    "judge_prompt",
]
