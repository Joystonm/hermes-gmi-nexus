"""The narration's claims, and the check that they are true of the run.

The demo video's voice says things about a real run. These tests cover the
checker that stops the build when the voice and the run disagree — the one
place where a plausible-sounding video could quietly become fiction.

Nothing here talks to GMI Cloud and nothing here spends money.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load_claims():
    """Load ``scripts/video/claims.py`` without putting it on the path."""
    path = _ROOT / "scripts" / "video" / "claims.py"
    spec = importlib.util.spec_from_file_location("video_claims", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


claims = _load_claims()

#: The line the voice really says for the catalogue segment.
_CATALOGUE_LINE = (
    "Seventy-three language models, three hundred and seventy-eight "
    "generation models."
)


def _run() -> dict[str, Any]:
    """A run shaped like the one the demo video is cut from."""
    return {
        "steps": [
            {
                "step": "analyse",
                "basis": "measured",
                "payload": {
                    "catalogue": {"llm_models": 73, "media_models": 378},
                },
            },
            {
                "step": "media",
                "basis": "measured",
                "payload": {"model_id": "gpt-image-2.5-flare-generate"},
            },
            {
                "step": "vision",
                "basis": "measured",
                "payload": {"issues": []},
            },
            {
                "step": "dev",
                "basis": "measured",
                "payload": {
                    "tests_passed": True,
                    "recovery": [
                        {"model_id": "Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8"}
                    ],
                },
            },
            {
                "step": "report",
                "basis": "measured",
                "payload": {
                    "usage": {"prompt_tokens": 13702},
                    "budget": {"daily_spend_usd": 0.327537},
                },
            },
        ]
    }


# --------------------------------------------------------------------------- #
# Numbers written the way a person says them
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value,words",
    [
        (0, "zero"),
        (7, "seven"),
        (19, "nineteen"),
        (20, "twenty"),
        (73, "seventy-three"),
        (99, "ninety-nine"),
        (100, "one hundred"),
        (378, "three hundred and seventy-eight"),
        (1000, "one thousand"),
        (13702, "thirteen thousand and seven hundred and two"),
    ],
)
def test_spoken_number(value: int, words: str) -> None:
    assert claims.spoken_number(value) == words


def test_spoken_number_rejects_a_negative() -> None:
    assert claims.spoken_number(-5) == "minus five"


# --------------------------------------------------------------------------- #
# Reading a value out of a run
# --------------------------------------------------------------------------- #


def test_resolve_reads_a_value_inside_a_payload() -> None:
    assert claims.resolve(_run(), "analyse.catalogue.llm_models") == 73


def test_resolve_reads_a_value_on_the_step_itself() -> None:
    assert claims.resolve(_run(), "dev.basis") == "measured"


def test_resolve_indexes_a_list() -> None:
    found = claims.resolve(_run(), "dev.recovery.0.model_id")
    assert found == "Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8"


def test_resolve_returns_nothing_for_an_unknown_step() -> None:
    assert claims.resolve(_run(), "nowhere.at.all") is None


def test_resolve_returns_nothing_for_an_unknown_key() -> None:
    assert claims.resolve(_run(), "analyse.catalogue.nothing_here") is None


# --------------------------------------------------------------------------- #
# The check itself
# --------------------------------------------------------------------------- #


def test_check_passes_when_the_run_says_what_the_voice_says() -> None:
    assert claims.check(_run(), {"catalogue": _CATALOGUE_LINE}) == []


def test_check_reports_a_catalogue_that_has_changed() -> None:
    run = _run()
    run["steps"][0]["payload"]["catalogue"]["llm_models"] = 80
    problems = claims.check(run, {"catalogue": _CATALOGUE_LINE})
    assert len(problems) == 1
    assert "analyse.catalogue.llm_models" in problems[0]
    assert "is 80 in this run" in problems[0]


def test_check_reports_a_phrase_the_script_no_longer_says() -> None:
    problems = claims.check(_run(), {"catalogue": "A great many language models."})
    assert len(problems) == 2
    assert all("does not say" in problem for problem in problems)


def test_check_accepts_a_float_within_a_tiny_tolerance() -> None:
    run = _run()
    run["steps"][4]["payload"]["budget"]["daily_spend_usd"] = 0.32753700001
    assert claims.check(run) == []


def test_check_rejects_a_float_that_is_moved() -> None:
    run = _run()
    run["steps"][4]["payload"]["budget"]["daily_spend_usd"] = 0.32
    problems = claims.check(run)
    assert len(problems) == 1
    assert "daily_spend_usd" in problems[0]


def test_check_accepts_a_claim_list_from_a_manifest() -> None:
    custom = [{"segment": "x", "path": "analyse.catalogue.llm_models", "value": 73}]
    assert claims.check(_run(), {}, custom) == []
    wrong = [{"segment": "x", "path": "analyse.catalogue.llm_models", "value": 74}]
    assert claims.check(_run(), {}, wrong) != []


# --------------------------------------------------------------------------- #
# The claims the demo video actually ships with
# --------------------------------------------------------------------------- #


def test_every_shipped_claim_names_a_step_that_exists() -> None:
    for claim in claims.CLAIMS:
        assert "." in claim["path"], claim
        step = claim["path"].partition(".")[0]
        assert step in {"analyse", "media", "vision", "dev", "report"}, claim


def test_every_shipped_claim_is_true_of_the_recorded_run() -> None:
    """The run the shipped demo video is cut from, when it is still on disk."""
    path = _ROOT / "demo_output" / "reel2" / "video_run.json"
    if not path.exists():
        pytest.skip("the recorded run is not in the tree")
    run = json.loads(path.read_text(encoding="utf-8"))
    assert claims.check(run) == []
