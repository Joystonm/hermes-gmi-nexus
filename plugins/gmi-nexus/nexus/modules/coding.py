"""The Dev Agent: analyse, generate, write, run, inspect, fix, verify.

This module deliberately owns no execution environment of its own. It has no
shell, no file writer and no test runner. Every side effect goes through
Hermes' own tools via ``ctx.dispatch_tool`` — ``write_file`` to create code,
``read_file`` to read it back, ``terminal`` to run the tests — so Hermes'
approval policy, sandboxing and audit trail apply to NEXUS exactly as they
apply to the agent itself.

The loop is: ask a code-capable GMI model for an implementation, write the
files, run the tests, read the failure output, feed it back for a fix, and
stop when the tests pass or the retry budget is spent. The report says which
of those actually happened.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..budget import BudgetGuard
from ..config import Settings
from ..errors import NexusError, ValidationError
from ..models import Capability, TaskType

#: The prompt used to request an implementation.
IMPLEMENT_PROMPT = (
    "You are implementing a change in an existing project.\n\n"
    "Task:\n{task}\n\n"
    "{context}"
    "Write the complete contents of every file you need to create or change. "
    "Reply with JSON only:\n"
    '{{"files": [{{"path": "relative/path.ext", "content": "the full file content"}}], '
    '"notes": "one sentence about what you did"}}\n\n'
    "Rules: no placeholders, no ellipses, no \"rest of the file unchanged\". "
    "Every file you list must be complete and runnable as written. "
    "The paths must be exactly the paths the task asked for — if the task named "
    "a file, that file is the one to write."
)

#: The prompt used to fix code after a test failure.
FIX_PROMPT = (
    "The change you made does not pass its tests. Here is the failure output:\n\n"
    "```\n{output}\n```\n\n"
    "Here are the files as they are now:\n\n{files}\n\n"
    "The original task was:\n\n{task}\n\n"
    "Fix the problem. Reply with JSON only, in the same shape as before:\n"
    '{{"files": [{{"path": "...", "content": "..."}}], "notes": "..."}}\n\n'
    "Only list the files you actually change. Keep the fix minimal and targeted. "
    "The paths must be exactly the paths the task asked for — if the task named "
    "a file, that file is the one to write."
)

#: The prompt used to review existing code.
REVIEW_PROMPT = (
    "Review the following files for correctness, security and maintainability.\n\n"
    "{files}\n\n"
    "Report the concrete problems you find, most important first. For each one give "
    "the file, what is wrong, and the change you would make. Do not report style "
    "preferences as problems, and do not invent problems you cannot point at.\n\n"
    "Reply with JSON only:\n"
    '{{"issues": [{{"severity": "high|medium|low", "file": "...", "problem": "...", '
    '"fix": "..."}}], "summary": "one sentence"}}'
)

#: The tools this module uses, and the reason each one is used.
TOOL_WRITE = "write_file"
TOOL_READ = "read_file"
TOOL_TERMINAL = "terminal"

#: The ceiling on a retried token budget.
#:
#: When a reply comes back truncated the Dev Agent asks for more room rather
#: than asking the same model the same bounded question again. The ceiling
#: keeps that from becoming unbounded.
MAX_CODE_TOKENS = 32768


@dataclass(slots=True)
class FileChange:
    """One file the Dev Agent wrote."""

    path: str
    content: str
    written: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "bytes": len(self.content),
            "written": self.written,
            "error": self.error,
        }


@dataclass(slots=True)
class DevOutcome:
    """The result of a Dev Agent run."""

    ok: bool
    files: list[FileChange] = field(default_factory=list)
    notes: str = ""
    model_id: str | None = None
    attempts: int = 0
    test_output: str = ""
    tests_passed: bool | None = None
    cost_usd: float = 0.0
    latency_ms: int = 0
    error: str | None = None
    findings: list[dict[str, Any]] = field(default_factory=list)
    #: What each model that was tried did, in order. A run that fell back
    #: from one model to another says so here, so the report and the
    #: published route can be compared.
    recovery: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "files": [f.to_dict() for f in self.files],
            "notes": self.notes,
            "model_id": self.model_id,
            "attempts": self.attempts,
            "tests_passed": self.tests_passed,
            "test_output": self.test_output[-4000:],
            "cost_usd": round(self.cost_usd, 8),
            "latency_ms": self.latency_ms,
            "error": self.error,
            "findings": list(self.findings),
            "recovery": list(self.recovery),
        }


class DevAgent:
    """Implements, tests and fixes code through Hermes' own tools."""

    def __init__(
        self,
        settings: Settings,
        registry: Any,
        router: Any,
        llm_client: Any,
        budget: BudgetGuard,
        ctx: Any | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.router = router
        self.llm_client = llm_client
        self.budget = budget
        self.ctx = ctx

    # -- tool access -------------------------------------------------------- #

    def _dispatch(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Call a Hermes tool and normalise its result."""
        if self.ctx is None:
            raise ValidationError(
                "this operation needs Hermes' tools, but no plugin context is available"
            )
        raw = self.ctx.dispatch_tool(tool, args)
        payload = _parse_tool_result(raw)
        if isinstance(payload, dict) and payload.get("error"):
            return {"error": str(payload["error"])}
        return payload if isinstance(payload, dict) else {"result": payload}

    def read_file(self, path: str) -> str:
        """Read a file through Hermes' ``read_file`` tool."""
        result = self._dispatch(TOOL_READ, {"path": path})
        if result.get("error"):
            return ""
        content = result.get("content")
        if isinstance(content, str):
            return content
        return str(result.get("result") or "")

    def write_file(self, path: str, content: str) -> tuple[bool, str]:
        """Write a file through Hermes' ``write_file`` tool."""
        result = self._dispatch(TOOL_WRITE, {"path": path, "content": content})
        if result.get("error"):
            return False, str(result["error"])
        return True, ""

    def run_command(self, command: str, *, timeout: int = 180) -> tuple[int, str]:
        """Run a command through Hermes' ``terminal`` tool."""
        result = self._dispatch(TOOL_TERMINAL, {"command": command, "timeout": timeout})
        if result.get("error"):
            return 1, str(result["error"])
        output = result.get("output") or result.get("stdout") or ""
        exit_code = result.get("exit_code")
        if exit_code is None:
            exit_code = result.get("returncode", 0)
        return int(exit_code), str(output)

    # -- the main loop ------------------------------------------------------ #

    def implement(
        self,
        task: str,
        *,
        context_files: Sequence[str] = (),
        test_command: str | None = None,
        max_attempts: int = 2,
        approved: bool = False,
    ) -> DevOutcome:
        """Implement a change, write it, test it and fix it if the test fails.

        A model that fails is passed over: the next candidate is tried. The
        models that were tried, and what each one did, are reported, so a
        run that fell back says so rather than looking like it went to
        plan.
        """
        if not task.strip():
            raise ValidationError("the Dev Agent needs a task")

        outcome = DevOutcome(ok=False)
        candidates = self._candidate_models(task)
        if not candidates:
            outcome.error = "no code-capable model is available in the live catalogue"
            return outcome

        started = time.perf_counter()
        prompt = IMPLEMENT_PROMPT.format(
            task=task,
            context=self._context_block(context_files),
        )

        attempt = 0
        last_error = ""
        model = candidates[0]
        # The token budget for a reply that has to carry whole files. It grows
        # when a reply comes back truncated, because a reply that was cut off
        # needs more room, not the same question asked again.
        token_budget = max(1024, int(self.settings.code_max_tokens))
        while attempt < max(1, int(max_attempts)):
            attempt += 1
            outcome.attempts = attempt

            response = None
            for candidate in candidates:
                estimate = self._estimate_cost(candidate, prompt, token_budget)
                try:
                    self.budget.enforce(
                        estimate,
                        approved=approved,
                        description=f"a call to {candidate.id}",
                    )
                except NexusError as exc:
                    # The budget refuses every model equally, so there is no
                    # point trying the next one.
                    outcome.error = str(exc)
                    break
                try:
                    response = self.llm_client.chat(
                        candidate.id,
                        [{"role": "user", "content": prompt}],
                        model_info=candidate,
                        max_tokens=token_budget,
                    )
                except NexusError as exc:
                    # This model is unusable for this request. Remember it and
                    # try the next one rather than failing the whole run.
                    outcome.recovery.append(
                        {"model_id": candidate.id, "outcome": f"call failed: {exc}"}
                    )
                    continue
                model = candidate
                break
            if outcome.error:
                break
            if response is None:
                outcome.error = (
                    "every code-capable model failed: "
                    + "; ".join(
                        f"{entry['model_id']}: {entry['outcome']}"
                        for entry in outcome.recovery
                    )
                )
                break
            outcome.model_id = model.id

            outcome.cost_usd += response.usage.cost_usd
            outcome.latency_ms += response.latency_ms
            self.budget.record(
                "dev_agent",
                model.id,
                response.usage.cost_usd,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                requests=1,
                detail=task[:200],
            )

            payload = _extract_files(response.text)
            if payload is None:
                if response.finish_reason == "length":
                    # The reply was cut off by the token limit, so this is a
                    # half-written file list rather than a refusal. Say so,
                    # and ask for more room on the next attempt.
                    last_error = (
                        f"the reply was cut off at the model's token limit "
                        f"({token_budget} tokens) before the file list was complete"
                    )
                    token_budget = min(token_budget * 2, MAX_CODE_TOKENS)
                else:
                    last_error = "the model did not return a usable file list"
                prompt = (
                    "Your reply could not be parsed. Reply with JSON only, in exactly "
                    "this shape:\n"
                    '{{"files": [{{"path": "...", "content": "..."}}], "notes": "..."}}\n\n'
                    "The reply must be complete — a half-written file list is not "
                    "usable. If the file is long, that is fine: send all of it."
                )
                continue

            changes, notes = payload
            outcome.notes = notes or outcome.notes

            written: list[FileChange] = []
            write_error = ""
            for change in changes:
                ok, error = self.write_file(change.path, change.content)
                written.append(FileChange(change.path, change.content, ok, error or None))
                if not ok:
                    write_error = error
            outcome.files = written

            if write_error:
                last_error = f"a file could not be written: {write_error}"
                prompt = (
                    f"The file write failed: {write_error}\n\n"
                    + FIX_PROMPT.format(
                        output=last_error,
                        files=self._files_block(written),
                        task=task,
                    )
                )
                continue

            if not test_command:
                outcome.ok = True
                break

            exit_code, output = self.run_command(test_command)
            outcome.test_output = output
            outcome.tests_passed = exit_code == 0
            if exit_code == 0:
                outcome.ok = True
                break

            last_error = output[-4000:] or f"the command exited with code {exit_code}"
            prompt = FIX_PROMPT.format(
                output=last_error,
                files=self._files_block(written),
                task=task,
            )

        if not outcome.ok and outcome.error is None:
            outcome.error = last_error or "the implementation did not complete"
        outcome.latency_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    def review(self, paths: Sequence[str], *, approved: bool = False) -> DevOutcome:
        """Review existing files and return structured findings."""
        if not paths:
            raise ValidationError("the review needs at least one file")
        candidates = self._candidate_models("code review")
        if not candidates:
            return DevOutcome(ok=False, error="no code-capable model is available")

        contents: list[tuple[str, str]] = []
        for path in paths:
            contents.append((path, self.read_file(path)))
        if not any(content for _, content in contents):
            return DevOutcome(
                ok=False,
                error="none of the requested files could be read through Hermes' read_file tool",
            )

        prompt = REVIEW_PROMPT.format(files=self._pairs_block(contents))
        response = None
        model = candidates[0]
        recovery: list[dict[str, Any]] = []
        token_budget = max(1024, int(self.settings.code_max_tokens))
        for candidate in candidates:
            estimate = self._estimate_cost(candidate, prompt, token_budget)
            try:
                self.budget.enforce(
                    estimate,
                    approved=approved,
                    description=f"a call to {candidate.id}",
                )
            except NexusError as exc:
                return DevOutcome(
                    ok=False,
                    error=str(exc),
                    recovery=recovery,
                )
            try:
                response = self.llm_client.chat(
                    candidate.id,
                    [{"role": "user", "content": prompt}],
                    model_info=candidate,
                    max_tokens=token_budget,
                )
            except NexusError as exc:
                recovery.append({"model_id": candidate.id, "outcome": f"call failed: {exc}"})
                continue
            model = candidate
            break
        if response is None:
            return DevOutcome(
                ok=False,
                error=(
                    "every code-capable model failed: "
                    + "; ".join(
                        f"{entry['model_id']}: {entry['outcome']}" for entry in recovery
                    )
                ),
                recovery=recovery,
            )

        self.budget.record(
            "dev_agent",
            model.id,
            response.usage.cost_usd,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            requests=1,
            detail="code review",
        )
        findings = _extract_json(response.text) or {}
        issues = findings.get("issues")
        return DevOutcome(
            ok=True,
            model_id=model.id,
            notes=str(findings.get("summary") or ""),
            cost_usd=response.usage.cost_usd,
            latency_ms=response.latency_ms,
            findings=issues if isinstance(issues, list) else [],
            recovery=recovery,
        )

    # -- helpers ------------------------------------------------------------ #

    def _estimate_cost(self, model: Any, prompt: str, completion_tokens: int) -> float:
        """Estimate what one call will cost, at list price.

        The estimate is what the budget gate is given before the request goes
        out. It prices the *budget* the reply is allowed, not the reply that
        comes back, so it is an upper bound on the call rather than a
        prediction of it.
        """
        from ..router import estimate_tokens

        return model.pricing.cost(
            prompt_tokens=estimate_tokens(prompt),
            completion_tokens=completion_tokens,
            requests=1,
        )

    def _candidate_models(self, task: str) -> list[Any]:
        """Return the code-capable models for this task, best first.

        The catalogue marks a model as ready, but readiness is a claim: on
        the live catalogue one of the published code models answers every
        request with "Backend request failed with status 400". So the list
        is a set of candidates and the Dev Agent tries them in turn.
        """
        models = [
            model
            for model in self.registry.list_models()
            if model.supports(Capability.TEXT) and model.supports(Capability.CODE)
        ]
        if not models:
            models = [
                model
                for model in self.registry.list_models()
                if model.supports(Capability.TEXT)
            ]
        decision = self.router.route(
            TaskType.CODING,
            (Capability.TEXT,),
            models,
            strategy="balanced",
            prompt=task,
        )
        return [candidate.model for candidate in decision.candidates]

    def _pick_model(self, task: str) -> Any | None:
        """Choose a code-capable model for this task."""
        candidates = self._candidate_models(task)
        return candidates[0] if candidates else None

    def _context_block(self, paths: Sequence[str]) -> str:
        """Return the current contents of the files the task touches."""
        if not paths:
            return ""
        pairs = [(path, self.read_file(path)) for path in paths]
        body = self._pairs_block(pairs)
        return f"Current contents of the relevant files:\n\n{body}\n\n"

    def _pairs_block(self, pairs: Sequence[tuple[str, str]]) -> str:
        blocks: list[str] = []
        for path, content in pairs:
            blocks.append(f"--- {path} ---\n{content[:12000]}")
        return "\n\n".join(blocks)

    def _files_block(self, files: Sequence[FileChange]) -> str:
        return self._pairs_block([(f.path, f.content) for f in files])


def _parse_tool_result(raw: Any) -> Any:
    """Parse a Hermes tool result, which is a JSON string."""
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {"raw": str(raw)}


def _extract_json(raw: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a model reply."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _extract_files(raw: str) -> tuple[list[FileChange], str] | None:
    """Parse the model's file list."""
    payload = _extract_json(raw)
    if payload is None:
        return None
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        return None
    changes: list[FileChange] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "").strip()
        content = entry.get("content")
        if not path or not isinstance(content, str):
            continue
        changes.append(FileChange(path=path, content=content))
    if not changes:
        return None
    return changes, str(payload.get("notes") or "")


__all__ = [
    "FIX_PROMPT",
    "IMPLEMENT_PROMPT",
    "REVIEW_PROMPT",
    "DevAgent",
    "DevOutcome",
    "FileChange",
]
