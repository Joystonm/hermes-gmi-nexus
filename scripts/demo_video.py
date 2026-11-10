#!/usr/bin/env python3
"""The run the demo video records.

``scripts/demo_run.py`` is the whole nine-phase scenario, and against the real
service it takes about four minutes — too long to record uncut. This script
is the same plugin on the same real service, arranged so that it finishes in
**well under ninety seconds**, so the video can be one continuous, unedited
screen recording with nothing cut out of it.

Every phase below is a real call to the plugin's own tool handlers, against
GMI Cloud, with a real credential:

1. **THE CATALOGUE IS LIVE** — ``nexus_analyze`` reads the catalogue and
   scores it. No model is called and nothing is spent.
2. **GENERATE** — ``nexus_media`` submits a real generation request to the
   request queue, using the model's own published schema.
3. **SEE** — ``nexus_vision`` sends the generated asset to a vision model
   that has been *probed*, and lists the concrete defects it finds.
4. **BUILD** — ``nexus_dev`` writes a real file and runs a real test
   through Hermes' own ``write_file`` and ``terminal`` tools, with the
   defects the vision model found folded into the task.
5. **REPORT** — ``nexus_report`` says what ran, what it cost, and what the
   cost is an estimate of.

The point of the reel is that the modules feed each other: the catalogue
decides who generates, the generated asset is what the vision model reads,
and what the vision model reads is what the Dev Agent is told to build
around.

Nothing here is a re-enactment of a recorded transcript, and nothing is
simulated. The script has no dry-run mode on purpose: a recording that
cannot be faked is the whole point.

Usage
-----

    GMI_API_KEY=<key> python scripts/demo_video.py --approve-spend

It refuses to start without a credential and without ``--approve-spend``. The
routing preview in the first phase prints what the generation is estimated to
cost *before* it is spent, and the script does not raise the plugin's own
limits.

Two things make the difference between a four-minute run and a
seventy-second one, and both are the plugin behaving as designed rather than
anything being cut out of the recording:

**The capability probe is cached.** Verifying that a model can really read an
image is a real request, and it is the only evidence that a declared
capability is true. The plugin records the verdict in its own persistent
data directory and reuses it, so the probe is paid for once per model rather
than once per run. Give this script the same ``--home`` twice and the second
run starts with the verdicts already in place.

**The operator chooses the code model.** ``blocked_models`` is a documented
setting, and the demo blocks the code model that
``scripts/probes/probe_code_speed.py`` measured as four times slower than its
sibling at writing a file. If that model is ever retired the block stops
matching anything and the router falls back to the catalogue on its own — the
run still works, it is just slower.

What is left is dominated by one thing: the request queue's own latency for
an image generation. Seven real generations were measured across this
project — six between 14.5s and 21.3s, and one 109.7s outlier, when the
queue held the request for a minute and a half. The catalogue publishes a
model's price and its parameter schema but not its speed, so this could only
be measured, not predicted. The script prints the total and says plainly
whether it came in under ninety seconds, because a take that ran long has to
be recorded again.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "plugins" / "gmi-nexus"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

# The Hermes-tool stand-in is shared with the full demo, so both scripts
# dispatch to the same three tools under the same rules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo_run import LocalHost, TEST_COMMAND, hero_task  # noqa: E402

from nexus.tools import NexusToolBox  # noqa: E402

#: The goal. It is analysed and routed, but it is never run as a whole
#: pipeline — the video shows the modules, not one long plan.
GOAL = (
    "Build a landing page for an AI startup that ships a multimodal model "
    "router, generate a hero visual for it, and check the result."
)

#: The prompt the hero visual is generated from.
HERO_PROMPT = (
    "Abstract hero illustration for an AI infrastructure startup: "
    "a single beam of light splitting into many thin parallel beams, "
    "deep blue on a pale background, clean, minimal, no text."
)

#: Art direction added to the hero prompt, one clause at a time.
#:
#: The creative engine suppresses an identical earlier request — that is
#: what stops a re-run of the demo from paying twice for the same picture —
#: so a recording that wants a *real* generation needs a request that has not
#: been made before. These read as ordinary art direction, and the script
#: tries them in turn until one produces a new asset. Nothing is faked: the
#: prompt really is different, so the request really is new.
ACCENTS: tuple[str, ...] = (
    "One thin diagonal accent line crosses the upper right.",
    "A faint horizontal rule runs through the middle of the frame.",
    "The beam casts a soft shadow onto the background.",
    "A narrow vertical band of deeper blue runs down the left edge.",
    "A small circular node marks where the beam begins.",
    "The beam enters from the lower right rather than the left.",
    "A dotted guide line sits beneath the fan of beams.",
    "A subtle grain runs across the background.",
)

#: The file the Dev Agent is asked to write. The path is named exactly, so
#: there is no ambiguity about what should appear on disk. The size is
#: capped because the video's time budget is spent on tokens generated.
DEV_TASK = (
    "Create a single self-contained file 'landing/index.html': a compact "
    "landing page for an AI startup called Northbeam. It needs a hero "
    "section with a headline and a call to action, a features section with "
    "three cards, and a footer. Inline CSS only, no JavaScript and no "
    "external assets. Keep the whole file under 3000 bytes."
)

#: The code model the operator blocks, and why.
#:
#: ``scripts/probes/probe_code_speed.py`` measured every code-capable model
#: in the live catalogue on the same writing task. On the day it was run,
#: ``moonshotai/kimi-k2.7-code-highspeed`` returned about 204 characters a
#: second and its slower sibling about 48 — four minutes and two seconds
#: against twenty seconds for the same file. Blocking the slow one is a
#: documented operator setting, and it is what an operator would do after
#: measuring. If the model is retired the block stops matching anything and
#: the router falls back to the catalogue on its own.
BLOCKED_CODE_MODEL = "moonshotai/kimi-k2.7-code"

#: How many of the vision model's findings are folded into the Dev Agent's
#: task. Three is enough to show that the modules feed each other and few
#: enough to keep the task, and so the run, short.
ISSUES_TO_CARRY = 3


class VideoHost(LocalHost):
    """The demo host, with the operator settings the video runs under.

    It is the same host the full demo uses — the same three Hermes tools,
    the same workspace jail and the same refusal of destructive commands —
    with the plugin's own settings handed in rather than left at their
    defaults.
    """

    def __init__(self, workspace: Path, config: dict[str, Any]) -> None:
        super().__init__(workspace)
        self.config = config

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)


class Reel:
    """Prints the video's transcript, one phase at a time."""

    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.steps: list[dict[str, Any]] = []

    def banner(self, title: str, note: str = "") -> None:
        print()
        print("=" * 78)
        print(f"  {title}")
        if note:
            print(f"  {note}")
        print("=" * 78)

    def field(self, label: str, value: Any, indent: int = 2) -> None:
        print(f"{' ' * indent}{label:<22} {value}")

    def elapsed(self) -> str:
        return f"{time.perf_counter() - self.started:.1f}s"

    def record(self, name: str, payload: dict[str, Any]) -> None:
        self.steps.append({"step": name, "basis": "measured", "payload": payload})


def call(box: NexusToolBox, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call one of the plugin's own tool handlers and parse its reply."""
    raw = getattr(box, tool)(args)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        payload = {"raw": str(raw)}
    return payload if isinstance(payload, dict) else {"result": payload}


def carried_issues(issues: list[dict[str, Any]]) -> list[str]:
    """Turn the vision model's findings into instructions for the Dev Agent.

    Only the first few findings are carried, and each is trimmed, because the
    point of the reel is that the modules feed each other — not that the
    prompt grows without bound.
    """
    carried = []
    for issue in issues[:ISSUES_TO_CARRY]:
        area = str(issue.get("area") or "").strip()
        problem = str(issue.get("problem") or "").strip()
        fix = str(issue.get("fix") or "").strip()
        if not (area or problem):
            continue
        detail = fix or problem
        carried.append(f"{area or 'the image'}: {detail[:140]}")
    return carried


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover
            pass

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--approve-spend",
        action="store_true",
        help="confirm that this run may spend GMI Cloud credits",
    )
    parser.add_argument(
        "--home",
        default="",
        help=(
            "a persistent Hermes home for the run. Reusing one caches the "
            "capability probes, so the second run starts with the verdicts "
            "already recorded. Default: a fresh temporary home."
        ),
    )
    parser.add_argument(
        "--block-model",
        default=BLOCKED_CODE_MODEL,
        help="the code model the operator blocks (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        default=str(REPO_ROOT / "demo_output"),
        help="where the transcript is written (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    if not args.approve_spend:
        print(
            "Refusing to run without --approve-spend.\n"
            "This script makes real GMI Cloud calls and spends credits.",
            file=sys.stderr,
        )
        return 2
    if not os.environ.get("GMI_API_KEY"):
        print("Refusing to run without GMI_API_KEY in the environment.", file=sys.stderr)
        return 2

    if args.home:
        home = Path(args.home).expanduser().resolve()
        home.mkdir(parents=True, exist_ok=True)
        temporary_home = False
    else:
        home = Path(tempfile.mkdtemp(prefix="nexus-video-home-"))
        temporary_home = True
    os.environ["HERMES_HOME"] = str(home)
    workspace = Path(tempfile.mkdtemp(prefix="nexus-video-work-"))

    reel = Reel()
    print("GMI NEXUS — live, against GMI Cloud")
    print("  every line below is a real request to a real model")

    host = VideoHost(
        workspace,
        {"blocked_models": [args.block_model] if args.block_model else []},
    )
    box = NexusToolBox(host)

    try:
        # -- 1. the catalogue is live ------------------------------------- #
        reel.banner("1. THE CATALOGUE IS LIVE", "read from GMI Cloud just now")
        analysis = call(box, "nexus_analyze", {"goal": GOAL})
        catalogue = analysis.get("catalogue") or {}
        reel.field("language models", catalogue.get("llm_models"))
        reel.field("generation models", catalogue.get("media_models"))
        budget = analysis.get("budget") or {}
        reel.field(
            "limits",
            f"${budget.get('max_cost_per_run_usd')} a run, "
            f"${budget.get('daily_budget_usd')} a day, "
            f"approval above ${budget.get('approval_threshold_usd')}",
        )
        for preview in analysis.get("routing_preview") or []:
            decision = preview.get("decision") or {}
            candidates = decision.get("candidates") or []
            chosen = candidates[0] if candidates else {}
            reel.field(
                f"routed -> {preview.get('task_type')}",
                f"{chosen.get('model') or '(none)'}   "
                f"~${float(chosen.get('estimated_cost_usd') or 0):.6f}",
            )
        reel.field("spent", "$0.00 — analysis calls no model")
        if args.block_model:
            reel.field(
                "operator blocked",
                f"{args.block_model} — measured four times slower than its "
                "sibling at writing a file",
            )
        reel.record("analyse", analysis)

        verified = call(
            box, "nexus_models", {"modality": "llm", "capability": "vision", "verified_only": True}
        )
        reel.field(
            "vision models",
            f"{verified.get('count')} verified by probe "
            f"(the probe is a real request, and its verdict is cached)",
        )
        reel.record("models", verified)
        print(f"  ({reel.elapsed()})")

        # -- 2. generate --------------------------------------------------- #
        reel.banner("2. GENERATE", "the model's own published schema builds the request")
        asset: dict[str, Any] = {}
        prompt = ""
        for accent in ACCENTS:
            prompt = f"{HERO_PROMPT} {accent}"
            asset = call(
                box,
                "nexus_media",
                {
                    "prompt": prompt,
                    "modality": "image",
                    # The model publishes this option and the plugin passes it
                    # through. A JPEG is a tenth of the bytes of a PNG, which
                    # is a tenth of the time spent moving it into the vision
                    # model in the next phase.
                    "options": {"output_format": "jpeg"},
                    "approve": True,
                },
            )
            if not asset.get("deduplicated"):
                break
        if asset.get("error"):
            reel.field("error", asset["error"])
        reel.field("model", asset.get("model_id"))
        reel.field("status", asset.get("status"))
        reel.field("assets", len(asset.get("media_urls") or []))
        latency = asset.get("latency_ms")
        if isinstance(latency, int) and latency > 0:
            reel.field("in the request queue", f"{latency / 1000:.1f}s")
        reel.field(
            "estimated cost",
            f"${float(asset.get('estimated_cost_usd') or 0):.6f}  (an estimate, not a bill)",
        )
        if asset.get("deduplicated"):
            reel.field("note", "this request matched an earlier one and was not re-sent")
        reel.record("media", asset)
        image = (asset.get("media_urls") or [""])[0]
        print(f"  ({reel.elapsed()})")

        # -- 3. see --------------------------------------------------------- #
        if not image:
            # No asset means no image to read. The reel says so and stops
            # rather than carrying an empty step forward as if it had worked.
            reel.banner("3. SEE", "skipped — the generation produced no image")
            reel.field("reason", asset.get("error") or asset.get("status") or "no asset")
            inspection: dict[str, Any] = {}
            issues: list[dict[str, Any]] = []
        else:
            reel.banner("3. SEE", "a probed vision model reads the asset")
            inspection = call(
                box,
                "nexus_vision",
                {
                    "mode": "issues",
                    "images": [image],
                    "criteria": "composition, contrast, balance and how it would sit behind text",
                    "approve": True,
                },
            )
            if inspection.get("error"):
                reel.field("error", inspection["error"])
            reel.field("model", inspection.get("model_id"))
            reel.field("summary", inspection.get("text"))
            issues = (inspection.get("structured") or {}).get("issues") or []
            for issue in issues:
                reel.field(
                    f"  [{issue.get('severity')}]",
                    f"{issue.get('area')} — {issue.get('problem')}",
                )
            reel.record("vision", inspection)
        print(f"  ({reel.elapsed()})")

        # -- 4. build ------------------------------------------------------- #
        carried = carried_issues(issues)
        task = hero_task(DEV_TASK, image)
        if carried:
            task += (
                "\n\nA vision model reviewed that image and found these problems "
                "with it. Your page has to work around them — for instance by "
                "keeping the text clear of the busy part of the picture: "
                + "; ".join(carried)
            )
        reel.banner("4. BUILD", "written and tested through Hermes' own tools")
        outcome = call(
            box,
            "nexus_dev",
            {
                "mode": "implement",
                "task": task,
                "test_command": TEST_COMMAND,
                "approve": True,
            },
        )
        if outcome.get("error"):
            reel.field("error", outcome["error"])
        reel.field("model", outcome.get("model_id"))
        for entry in outcome.get("recovery") or []:
            reel.field(
                "  recovered from", f"{entry.get('model_id')} — {entry.get('outcome')}"
            )
        for change in outcome.get("files") or []:
            reel.field(
                "  wrote",
                f"{change.get('path')} — {'ok' if change.get('written') else change.get('error')}",
            )
        reel.field("tests passed", outcome.get("tests_passed"))
        if outcome.get("test_output"):
            reel.field("test output", str(outcome["test_output"]).strip()[:160])
        reel.field("tools used", ", ".join(sorted({c["tool"] for c in host.calls})))
        reel.record("dev", outcome)
        print(f"  ({reel.elapsed()})")

        # -- 5. report ------------------------------------------------------ #
        reel.banner("5. REPORT", "what actually ran")
        report = call(box, "nexus_report", {"days": 7})
        for stat in (report.get("model_stats") or [])[:5]:
            reel.field(
                f"  {stat.get('model_id')}",
                f"{stat.get('runs')} run(s), {stat.get('successes')} success "
                f"— ${stat.get('total_cost_usd')} estimated",
            )
        usage = report.get("usage") or {}
        reel.field("tokens used", usage.get("prompt_tokens"))
        budget = report.get("budget") or {}
        reel.field("spent today", f"${budget.get('daily_spend_usd')}")
        reel.field("note", report.get("cost_note"))
        reel.record("report", report)
        print(f"  ({reel.elapsed()})")

    finally:
        if temporary_home:
            shutil.rmtree(home, ignore_errors=True)

    total = time.perf_counter() - reel.started
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    path = output / "video_run.json"
    path.write_text(
        json.dumps(
            {
                "mode": "live",
                "simulated": False,
                "goal": GOAL,
                "workspace": str(workspace),
                "hermes_home": str(home),
                "elapsed_s": round(total, 2),
                "steps": reel.steps,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print()
    print(f"Total {reel.elapsed()} — transcript written to {path}")
    print(f"Workspace kept at   {workspace}")
    if total > 90:
        print("That is over ninety seconds. Re-record before posting.")
    if host.refusals:
        print()
        print("Refusals (the demo host's own guard rails):")
        for refusal in host.refusals:
            print(f"  - {refusal}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
