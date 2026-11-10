#!/usr/bin/env python3
"""What the narration claims, and the check that it is true of the run.

The voice in the demo video says things about a real run: how many models the
catalogue had, which model the router chose, that a model was refused and the
work recovered on another. If the narration is ever cut against a *different*
run — a second recording, a re-run, a rehearsal — those sentences become
fiction while the screen still shows the truth.

So every factual claim in the narration is written down here as a path into
the run's own JSON, together with the value the voice says out loud. Before
the narration is synthesised, and again before the video is cut, the paths are
resolved against the run and compared. A mismatch stops the build.

Nothing here calls the network and nothing here costs anything.
"""

from __future__ import annotations

from typing import Any

#: The claims, one per narrated segment.
#:
#: ``path``   where the value lives in the run's JSON, as
#:            ``<step>.<key>.<key>`` — the step's payload.
#: ``value``  what the voice says it is.
#: ``spoken`` the words the voice uses, when a number is read out loud. It
#:            is checked against the segment's text, so the script and the
#:            number cannot drift apart.
CLAIMS: tuple[dict[str, Any], ...] = (
    {
        "segment": "catalogue",
        "path": "analyse.catalogue.llm_models",
        "value": 73,
        "spoken": "seventy-three",
    },
    {
        "segment": "catalogue",
        "path": "analyse.catalogue.media_models",
        "value": 378,
        "spoken": "three hundred and seventy-eight",
    },
    {
        "segment": "catalogue",
        "path": "analyse.basis",
        "value": "measured",
    },
    {
        "segment": "generate",
        "path": "media.basis",
        "value": "measured",
    },
    {
        "segment": "generate",
        "path": "vision.basis",
        "value": "measured",
    },
    {
        "segment": "build",
        "path": "dev.basis",
        "value": "measured",
    },
    {
        "segment": "build",
        "path": "dev.tests_passed",
        "value": True,
    },
    {
        "segment": "build",
        "path": "dev.recovery.0.model_id",
        "value": "Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8",
    },
    {
        "segment": "report",
        "path": "report.basis",
        "value": "measured",
    },
    {
        "segment": "report",
        "path": "report.usage.prompt_tokens",
        "value": 13702,
    },
    {
        "segment": "report",
        "path": "report.budget.daily_spend_usd",
        "value": 0.327537,
    },
)

_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
)
_TENS = (
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety",
)


def spoken_number(value: int) -> str:
    """Write an integer the way a person says it.

    Only what the demo needs: thousands, hundreds and units, with the
    ``and`` British English puts in the middle. It is used to check that the
    number in the script is the number the run measured.
    """
    value = int(value)
    if value < 0:
        return "minus " + spoken_number(-value)
    if value < 20:
        return _ONES[value]
    if value < 100:
        tens, units = divmod(value, 10)
        return _TENS[tens] + (f"-{_ONES[units]}" if units else "")
    if value < 1000:
        hundreds, rest = divmod(value, 100)
        out = f"{_ONES[hundreds]} hundred"
        return out + (f" and {spoken_number(rest)}" if rest else "")
    thousands, rest = divmod(value, 1000)
    out = f"{_ONES[thousands]} thousand"
    return out + (f" and {spoken_number(rest)}" if rest else "")


def _normalise(text: str) -> str:
    """Fold a line of script into comparable words."""
    for ch in "-—–,.;:!?":
        text = text.replace(ch, " ")
    return " ".join(text.lower().split())


def resolve(run: dict[str, Any], path: str) -> Any:
    """Read a value out of a run's JSON.

    ``path`` is ``<step>.<key>.<key>``. The step's payload is searched first
    and the step itself second, so ``analyse.basis`` finds the step's own
    ``basis`` and ``analyse.catalogue.llm_models`` finds one inside its
    payload. A numeric key indexes a list.
    """
    step_name, _, rest = path.partition(".")
    step: dict[str, Any] | None = None
    for entry in run.get("steps") or []:
        if entry.get("step") == step_name:
            step = entry
            break
    if step is None:
        return None
    payload: Any = step.get("payload") or {}
    value: Any = payload
    for key in rest.split("."):
        if not key:
            continue
        if isinstance(value, list):
            try:
                value = value[int(key)]
            except (ValueError, IndexError):
                return None
        elif isinstance(value, dict) and key in value:
            value = value[key]
        elif key in step:
            value = step[key]
        else:
            return None
    return value


def check(
    run: dict[str, Any],
    segments: dict[str, str] | None = None,
    claim_list: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Return every claim that the run does not support.

    ``segments`` maps a segment id to the text that is spoken for it; when it
    is given, the spoken form of a number is checked against the text as well,
    so the script and the number cannot drift apart. ``claim_list`` replaces
    the module's own claims with a list read from a manifest.
    """
    problems: list[str] = []
    for claim in claim_list if claim_list is not None else CLAIMS:
        found = resolve(run, claim["path"])
        expected = claim["value"]
        if isinstance(expected, float):
            matches = isinstance(found, (int, float)) and abs(float(found) - expected) < 1e-9
        else:
            matches = found == expected
        if not matches:
            problems.append(
                f"{claim['segment']}: {claim['path']} is {found!r} in this run, "
                f"but the narration says {expected!r}"
            )
            continue
        spoken = claim.get("spoken")
        text = (segments or {}).get(claim["segment"])
        if spoken and text is not None and _normalise(spoken) not in _normalise(text):
            problems.append(
                f"{claim['segment']}: the script does not say {spoken!r}"
            )
    return problems
