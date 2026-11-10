#!/usr/bin/env python3
"""Cut the GMI NEXUS demo video.

The video is one continuous take: a terminal on the left showing the real
output of a live run, a panel on the right showing what that output *is* —
the catalogue counts, the routed models, the image the plugin generated, the
landing page the Dev Agent wrote, the report's numbers — and a ribbon along
the bottom showing which of the eight phases is running.

Nothing in it is drawn by hand. Every figure on screen is read out of the
run's own JSON and every line in the terminal is a line the plugin printed.

Usage
-----

    python scripts/video/build_video.py \\
        --run demo_output/reel2/video_run.json \\
        --transcript demo_output/reel2/transcript.txt \\
        --narration demo_output/video_build/narration.json \\
        --image demo_output/reel2/hero.jpg \\
        --screenshot demo_output/reel2/landing.png \\
        --out demo_output/gmi-nexus-demo.mp4
"""

from __future__ import annotations

import argparse
import array
import json
import math
import re
import subprocess
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------- #
# The canvas
# --------------------------------------------------------------------------- #

WIDTH, HEIGHT = 1920, 1080
FPS = 30

BG = (7, 11, 22)
PANEL = (12, 17, 32)
PANEL_ALT = (16, 22, 40)
BORDER = (28, 40, 66)
TEXT = (216, 226, 242)
DIM = (122, 138, 168)
FAINT = (80, 92, 118)
ACCENT = (94, 160, 255)
GREEN = (110, 226, 176)
AMBER = (245, 190, 96)
RED = (255, 122, 122)
WHITE = (245, 248, 255)

FONT_DIR = Path("C:/Windows/Fonts")

#: The eight phases of the pipeline, in order.
PIPELINE: tuple[str, ...] = (
    "UNDERSTAND",
    "PLAN",
    "ROUTE",
    "EXECUTE",
    "EVALUATE",
    "RECOVER",
    "LEARN",
    "REPORT",
)


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / name), size)


# --------------------------------------------------------------------------- #
# The narration timeline
# --------------------------------------------------------------------------- #


@dataclass
class Segment:
    """One narrated beat and the wall-clock window it occupies."""

    id: str
    text: str
    path: str
    duration: float
    start: float = 0.0

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class Timeline:
    """Every beat of the video, in order, with its start time."""

    segments: list[Segment] = field(default_factory=list)
    title_seconds: float = 2.2
    gap: float = 0.3

    @property
    def total(self) -> float:
        return self.segments[-1].end if self.segments else self.title_seconds

    def segment_at(self, t: float) -> Segment | None:
        for segment in self.segments:
            if segment.start <= t < segment.end:
                return segment
        return None


def probe_duration(path: Path) -> float:
    """Measure a media file's duration in seconds."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def build_timeline(narration: dict[str, Any], order: Sequence[str]) -> Timeline:
    """Lay the narration out on the clock."""
    by_id = {segment["id"]: segment for segment in narration["segments"]}
    timeline = Timeline()
    clock = timeline.title_seconds
    for segment_id in order:
        entry = by_id[segment_id]
        segment = Segment(
            id=segment_id,
            text=entry["text"],
            path=entry["path"],
            duration=probe_duration(Path(entry["path"])),
        )
        clock += timeline.gap
        segment.start = clock
        clock += segment.duration
        timeline.segments.append(segment)
    return timeline


# --------------------------------------------------------------------------- #
# Reading the run
# --------------------------------------------------------------------------- #


@dataclass
class Phase:
    """One numbered phase of the reel, with the lines it printed."""

    number: str
    title: str
    note: str
    lines: list[str] = field(default_factory=list)


def parse_transcript(text: str) -> tuple[list[str], list[Phase]]:
    """Split the transcript into its header and its numbered phases."""
    header: list[str] = []
    phases: list[Phase] = []
    current: Phase | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if set(stripped) == {"="}:
            continue
        numbered = re.match(r"^(\d)\.\s+(.*)$", stripped)
        if numbered:
            current = Phase(
                number=numbered.group(1), title=numbered.group(2).strip(), note=""
            )
            phases.append(current)
            continue
        if current is None:
            header.append(line)
            continue
        if not current.lines and not current.note:
            # The line between the phase's title and its closing rule.
            current.note = stripped
            continue
        current.lines.append(line)
    return header, phases


def step_payload(run: dict[str, Any], name: str) -> dict[str, Any]:
    for step in run.get("steps") or []:
        if step.get("step") == name:
            return step.get("payload") or {}
    return {}


# --------------------------------------------------------------------------- #
# Drawing helpers
# --------------------------------------------------------------------------- #


def rounded(draw: ImageDraw.ImageDraw, box, radius: int, fill, outline=None, width: int = 1) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def panel(draw: ImageDraw.ImageDraw, box, title: str) -> tuple[int, int, int, int]:
    """Draw a card with a title bar and return its inner box."""
    x0, y0, x1, y1 = box
    rounded(draw, box, 14, PANEL, BORDER)
    draw.rectangle((x0, y0, x1, y0 + 52), fill=PANEL_ALT)
    draw.line((x0, y0 + 52, x1, y0 + 52), fill=BORDER)
    draw.ellipse((x0 + 18, y0 + 21, x0 + 28, y0 + 31), fill=(255, 95, 86))
    draw.ellipse((x0 + 36, y0 + 21, x0 + 46, y0 + 31), fill=(255, 189, 46))
    draw.ellipse((x0 + 54, y0 + 21, x0 + 64, y0 + 31), fill=(39, 201, 63))
    draw.text((x0 + 84, y0 + 26), title, font=font("consola.ttf", 17), fill=DIM)
    return (x0 + 1, y0 + 53, x1 - 1, y1 - 1)


def wrap(draw: ImageDraw.ImageDraw, text: str, fnt, width: int) -> list[str]:
    """Wrap text to a pixel width."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=fnt) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def stat_tile(draw: ImageDraw.ImageDraw, box, value: str, label: str, colour=WHITE, big: int = 52) -> None:
    x0, y0, x1, y1 = box
    rounded(draw, box, 12, PANEL_ALT, BORDER)
    draw.text((x0 + 22, y0 + 14), value, font=font("segoeuib.ttf", big), fill=colour)
    draw.text((x0 + 24, y0 + 22 + big), label, font=font("segoeui.ttf", 19), fill=DIM)


def fitted(path: Path, area: tuple[int, int, int, int], crop_top: float = 1.0) -> Image.Image | None:
    """Load an image and scale it to fit an area, optionally cropping."""
    try:
        picture = Image.open(path).convert("RGB")
    except Exception:
        return None
    if crop_top < 1.0:
        picture = picture.crop((0, 0, picture.width, int(picture.height * crop_top)))
    ratio = min((area[2] - area[0]) / picture.width, (area[3] - area[1]) / picture.height)
    size = (max(1, int(picture.width * ratio)), max(1, int(picture.height * ratio)))
    return picture.resize(size, Image.LANCZOS)


def place(canvas: Image.Image, draw: ImageDraw.ImageDraw, picture: Image.Image | None, area, border=True) -> None:
    """Centre a picture inside an area, optionally framing it."""
    if picture is None:
        return
    if border:
        rounded(draw, area, 12, PANEL_ALT, BORDER)
    x = area[0] + (area[2] - area[0] - picture.width) // 2
    y = area[1] + (area[3] - area[1] - picture.height) // 2
    canvas.paste(picture, (x, y))


# --------------------------------------------------------------------------- #
# The terminal
# --------------------------------------------------------------------------- #

TERM_FONT = 21
TERM_LINE = 30
TERM_PAD = 22


@dataclass
class TermLine:
    """One line of the terminal, and what kind of line it is."""

    text: str
    kind: str


@dataclass
class TermBlock:
    """The lines of one phase: its banner and its fields."""

    number: str
    banner: list[int] = field(default_factory=list)
    fields: list[int] = field(default_factory=list)


def terminal_lines(
    header: list[str], phases: list[Phase]
) -> tuple[list[TermLine], dict[str, TermBlock]]:
    """Flatten the transcript into drawable lines and per-phase indexes."""
    lines: list[TermLine] = []
    blocks: dict[str, TermBlock] = {}
    for line in header:
        lines.append(TermLine(line, "header"))
    for phase in phases:
        block = TermBlock(number=phase.number)
        block.banner.append(len(lines))
        lines.append(TermLine("=" * 78, "rule"))
        block.banner.append(len(lines))
        lines.append(TermLine(f"  {phase.number}. {phase.title}", "phase"))
        if phase.note:
            block.banner.append(len(lines))
            lines.append(TermLine(f"  {phase.note}", "note"))
        block.banner.append(len(lines))
        lines.append(TermLine("=" * 78, "rule"))
        for line in phase.lines:
            block.fields.append(len(lines))
            lines.append(TermLine(line, "field"))
        blocks[phase.number] = block
    return lines, blocks


def field_colours(label: str) -> tuple[tuple, tuple]:
    """The colours a field's label and value are drawn in."""
    label = label.strip()
    if label == "error":
        return RED, RED
    if label in ("spent", "spent today", "estimated cost", "in the request queue"):
        return AMBER, AMBER
    if label in ("status", "tests passed"):
        return GREEN, GREEN
    if label.startswith("recovered from"):
        return AMBER, AMBER
    if label in ("summary", "note", "operator blocked"):
        return DIM, TEXT
    return DIM, TEXT


def draw_terminal(
    draw: ImageDraw.ImageDraw,
    box,
    lines: Sequence[TermLine],
    revealed: int,
    cursor: bool,
) -> None:
    """Draw the terminal with the first ``revealed`` lines shown, scrolled.

    A field whose value is too wide for the panel is wrapped under its own
    label, so nothing is cut off and nothing overlaps the frame.
    """
    x0, y0, x1, y1 = box
    fnt = font("consola.ttf", TERM_FONT)
    bold = font("consolab.ttf", TERM_FONT)
    width = x1 - x0 - 2 * TERM_PAD

    rows: list[list[tuple[float, str, tuple, ImageFont.FreeTypeFont]]] = []
    for line in lines[:revealed]:
        text, kind = line.text, line.kind
        if kind == "field":
            match = re.match(r"^(\s*)(.*?)\s{2,}(\S.*)$", text)
            if match:
                indent, label, value = match.group(1), match.group(2), match.group(3)
                lcolour, vcolour = field_colours(label)
                label_text = indent + label
                offset = draw.textlength(label_text, font=fnt) + 12
                chunks = wrap(draw, value, fnt, width - offset) or [value]
                rows.append(
                    [(0.0, label_text, lcolour, fnt), (offset, chunks[0], vcolour, fnt)]
                )
                for chunk in chunks[1:]:
                    rows.append([(offset, chunk, vcolour, fnt)])
                continue
        if kind == "rule":
            rows.append([(0.0, text, FAINT, fnt)])
        elif kind == "phase":
            rows.append([(0.0, text, ACCENT, bold)])
        elif kind == "note":
            rows.append([(0.0, text, DIM, fnt)])
        elif kind == "header":
            rows.append([(0.0, text, ACCENT, bold)])
        else:
            rows.append([(0.0, text, DIM, fnt)])

    fits = max(1, (y1 - y0 - 2 * TERM_PAD) // TERM_LINE)
    shown = rows[-fits:]
    y = y0 + TERM_PAD
    for row in shown:
        for offset, text, colour, use in row:
            draw.text((x0 + TERM_PAD + offset, y), text, font=use, fill=colour)
        y += TERM_LINE
    if cursor and len(rows) < fits:
        draw.rectangle(
            (x0 + TERM_PAD, y + 4, x0 + TERM_PAD + 11, y + TERM_LINE - 6), fill=ACCENT
        )


# --------------------------------------------------------------------------- #
# The right panel, per beat
# --------------------------------------------------------------------------- #


def right_intro(draw: ImageDraw.ImageDraw, box) -> None:
    x0, y0, x1, y1 = box
    draw.text((x0, y0 + 36), "GMI", font=font("segoeuib.ttf", 74), fill=WHITE)
    draw.text((x0, y0 + 116), "NEXUS", font=font("segoeuib.ttf", 74), fill=ACCENT)
    draw.text((x0, y0 + 218), "A Hermes Agent plugin", font=font("segoeui.ttf", 30), fill=TEXT)
    draw.text((x0, y0 + 264), "One goal in.", font=font("segoeui.ttf", 25), fill=DIM)
    draw.text((x0, y0 + 300), "A coordinated multi-model workflow out.", font=font("segoeui.ttf", 25), fill=DIM)
    rule_y = y0 + 366
    draw.line((x0, rule_y, x1 - 30, rule_y), fill=BORDER)
    draw.text((x0, rule_y + 24), "UNDERSTAND → PLAN → ROUTE → EXECUTE", font=font("consola.ttf", 21), fill=ACCENT)
    draw.text((x0, rule_y + 60), "EVALUATE → RECOVER → LEARN → REPORT", font=font("consola.ttf", 21), fill=ACCENT)
    draw.text((x0, rule_y + 124), "five modules · eight tools · one command", font=font("consola.ttf", 19), fill=DIM)
    draw.text((x0, rule_y + 158), "no third-party dependencies", font=font("consola.ttf", 19), fill=DIM)


def right_catalogue(draw: ImageDraw.ImageDraw, box, run: dict[str, Any]) -> None:
    x0, y0, x1, y1 = box
    catalogue = step_payload(run, "analyse").get("catalogue") or {}
    stat_tile(draw, (x0, y0 + 8, x1 - 30, y0 + 146), str(catalogue.get("llm_models") or 0), "language models", WHITE)
    stat_tile(draw, (x0, y0 + 160, x1 - 30, y0 + 298), str(catalogue.get("media_models") or 0), "generation models", ACCENT)
    by = catalogue.get("media_by_modality") or {}
    if isinstance(by, dict) and by:
        draw.text(
            (x0 + 4, y0 + 314),
            "   ".join(f"{count} {name}" for name, count in by.items()),
            font=font("segoeui.ttf", 19),
            fill=DIM,
        )
    draw.text((x0 + 4, y0 + 360), "read live from GMI Cloud, just now", font=font("segoeui.ttf", 20), fill=DIM)
    draw.text((x0 + 4, y0 + 396), "no model id is hard-coded", font=font("segoeui.ttf", 20), fill=GREEN)


def right_route(draw: ImageDraw.ImageDraw, box, run: dict[str, Any]) -> None:
    x0, y0, x1, y1 = box
    preview = step_payload(run, "analyse").get("routing_preview") or []
    rows: list[tuple[str, str, str]] = []
    for entry in preview:
        candidates = (entry.get("decision") or {}).get("candidates") or []
        if not candidates:
            continue
        chosen = candidates[0]
        rows.append(
            (
                str(entry.get("task_type") or ""),
                str(chosen.get("model") or ""),
                f"${float(chosen.get('estimated_cost_usd') or 0):.6f}",
            )
        )
    draw.text((x0 + 4, y0 + 2), "routed, live, from the catalogue", font=font("segoeui.ttf", 20), fill=DIM)
    y = y0 + 44
    right = x1 - 30
    for task, model, cost in rows:
        draw.text((x0 + 4, y), task, font=font("consola.ttf", 18), fill=ACCENT)
        y += 24
        fnt = font("consola.ttf", 18)
        draw.text((x0 + 4, y), model, font=fnt, fill=TEXT)
        cost_fnt = font("consola.ttf", 17)
        cost_width = draw.textlength(cost, font=cost_fnt)
        draw.text((right - cost_width, y), cost, font=cost_fnt, fill=AMBER)
        y += 26
        draw.line((x0 + 4, y, right, y), fill=(22, 30, 52))
        y += 14
    draw.text((x0 + 4, y1 - 44), "analysis calls no model — $0.00", font=font("segoeui.ttf", 20), fill=GREEN)


def right_generate(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    box,
    run: dict[str, Any],
    image: Path | None,
    show_defects: bool,
) -> None:
    x0, y0, x1, y1 = box
    media = step_payload(run, "media")
    vision = step_payload(run, "vision")
    latency = (media.get("latency_ms") or 0) / 1000
    if image is not None and image.exists():
        if show_defects:
            area = (x0, y0, x0 + 200, y0 + 200)
            picture = fitted(image, area)
            place(canvas, draw, picture, area, border=False)
        else:
            area = (x0, y0, x1 - 30, y0 + 392)
            picture = fitted(image, area)
            place(canvas, draw, picture, area)
    if not show_defects:
        draw.text(
            (x0 + 4, y0 + 404),
            f"{media.get('model_id') or ''} · {latency:.1f}s in the queue",
            font=font("consola.ttf", 18),
            fill=DIM,
        )
        draw.text((x0 + 4, y0 + 442), "a real request, a real image", font=font("segoeui.ttf", 20), fill=GREEN)
        return
    draw.text(
        (x0 + 4, y0 + 216),
        f"{media.get('model_id') or ''}",
        font=font("consola.ttf", 18),
        fill=ACCENT,
    )
    draw.text(
        (x0 + 4, y0 + 244),
        f"· {latency:.1f}s in the request queue",
        font=font("consola.ttf", 18),
        fill=DIM,
    )
    draw.text((x0 + 4, y0 + 288), "the vision model read it back:", font=font("segoeui.ttf", 21), fill=TEXT)
    issues = (vision.get("structured") or {}).get("issues") or []
    y = y0 + 326
    for issue in issues[:3]:
        severity = str(issue.get("severity") or "low")
        colour = RED if severity == "high" else (AMBER if severity == "medium" else DIM)
        area = str(issue.get("area") or "the image")
        problem = str(issue.get("problem") or "")
        draw.text((x0 + 4, y), f"[{severity}]", font=font("consola.ttf", 18), fill=colour)
        draw.text((x0 + 92, y), area, font=font("segoeuib.ttf", 19), fill=TEXT)
        y += 26
        fnt = font("segoeui.ttf", 18)
        for line in wrap(draw, problem, fnt, 520)[:3]:
            draw.text((x0 + 20, y), line, font=fnt, fill=DIM)
            y += 23
        y += 8


def right_build(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    box,
    run: dict[str, Any],
    screenshot: Path | None,
) -> None:
    x0, y0, x1, y1 = box
    dev = step_payload(run, "dev")
    if screenshot is not None and screenshot.exists():
        area = (x0, y0, x1 - 30, y0 + 452)
        picture = fitted(screenshot, area, crop_top=0.68)
        place(canvas, draw, picture, area)
    y = y0 + 466
    files = dev.get("files") or []
    if files:
        draw.text(
            (x0 + 4, y),
            f"{files[0].get('path')} · {files[0].get('bytes')} bytes",
            font=font("consola.ttf", 20),
            fill=GREEN,
        )
        y += 30
    draw.text((x0 + 4, y), "written through Hermes' write_file", font=font("segoeui.ttf", 19), fill=DIM)
    y += 26
    draw.text((x0 + 4, y), "tested through Hermes' terminal", font=font("segoeui.ttf", 19), fill=DIM)
    y += 34
    recovery = dev.get("recovery") or []
    if recovery:
        text = (
            f"the first model was retired by the provider — recovered on "
            f"{dev.get('model_id')}"
        )
        fnt = font("segoeui.ttf", 19)
        for line in wrap(draw, text, fnt, 590)[:2]:
            draw.text((x0 + 4, y), line, font=fnt, fill=AMBER)
            y += 25
        y += 4
        for line in wrap(draw, str(recovery[0].get("outcome") or ""), font("segoeui.ttf", 17), 580)[:2]:
            draw.text((x0 + 4, y), line, font=font("segoeui.ttf", 17), fill=FAINT)
            y += 22


def right_report(draw: ImageDraw.ImageDraw, box, run: dict[str, Any]) -> None:
    x0, y0, x1, y1 = box
    report = step_payload(run, "report")
    usage = report.get("usage") or {}
    budget = report.get("budget") or {}
    stat_tile(draw, (x0, y0 + 4, x1 - 30, y0 + 128), f"{usage.get('prompt_tokens') or 0:,}", "tokens used", WHITE, big=44)
    stat_tile(
        draw,
        (x0, y0 + 142, x1 - 30, y0 + 266),
        f"${budget.get('daily_spend_usd') or 0}",
        "spent, estimated at list price",
        AMBER,
        big=40,
    )
    right = x1 - 30
    y = y0 + 286
    draw.text((x0 + 4, y), "what actually ran", font=font("segoeui.ttf", 21), fill=TEXT)
    y += 32
    ran: list[tuple[str, str, str]] = []
    media = step_payload(run, "media")
    vision = step_payload(run, "vision")
    dev = step_payload(run, "dev")
    for model_id, role, cost in (
        (media.get("model_id"), "the hero image", f"${media.get('estimated_cost_usd') or 0}"),
        (vision.get("model_id"), "the critique", f"${vision.get('cost_usd') or 0}"),
        (dev.get("model_id"), "the code", f"${dev.get('cost_usd') or 0}"),
    ):
        if model_id:
            ran.append((str(model_id), role, cost))
    for model_id, role, cost in ran:
        draw.text((x0 + 4, y), model_id, font=font("consola.ttf", 17), fill=ACCENT)
        y += 22
        fnt = font("segoeui.ttf", 17)
        draw.text((x0 + 4, y), role, font=fnt, fill=DIM)
        cost_fnt = font("consola.ttf", 17)
        cost_width = draw.textlength(cost, font=cost_fnt)
        draw.text((right - cost_width, y), cost, font=cost_fnt, fill=AMBER)
        y += 30
    recovery = dev.get("recovery") or []
    if recovery:
        y += 6
        draw.text((x0 + 4, y), "what failed", font=font("segoeui.ttf", 21), fill=TEXT)
        y += 30
        draw.text(
            (x0 + 4, y),
            str(recovery[0].get("model_id") or ""),
            font=font("consola.ttf", 17),
            fill=RED,
        )
        y += 22
        for line in wrap(draw, str(recovery[0].get("outcome") or ""), font("segoeui.ttf", 16), 590)[:2]:
            draw.text((x0 + 4, y), line, font=font("segoeui.ttf", 16), fill=FAINT)
            y += 21
        y += 4
        draw.text(
            (x0 + 4, y),
            "the plugin retired it and recovered on another model",
            font=font("segoeui.ttf", 17),
            fill=GREEN,
        )
        y += 26
    note = str(report.get("cost_note") or "")
    for line in wrap(draw, note, font("segoeui.ttf", 16), 580)[:3]:
        draw.text((x0 + 4, y), line, font=font("segoeui.ttf", 16), fill=FAINT)
        y += 21


# --------------------------------------------------------------------------- #
# The frame
# --------------------------------------------------------------------------- #


def pipeline_highlight(t: float, timeline: Timeline) -> set[str]:
    """Which of the eight phases are running at this moment."""
    segment = timeline.segment_at(t)
    if segment is None:
        return set()
    fraction = (t - segment.start) / max(segment.duration, 0.001)
    if segment.id == "intro":
        return {"UNDERSTAND"}
    if segment.id == "catalogue":
        return {"UNDERSTAND", "PLAN"}
    if segment.id == "route":
        return {"ROUTE"}
    if segment.id == "generate":
        return {"EXECUTE"} if fraction < 0.45 else {"EVALUATE"}
    if segment.id == "build":
        return {"RECOVER", "LEARN"}
    if segment.id == "report":
        return {"REPORT"}
    return set(PIPELINE)


def draw_pipeline(draw: ImageDraw.ImageDraw, t: float, timeline: Timeline) -> None:
    """The eight-phase ribbon along the bottom."""
    active = pipeline_highlight(t, timeline)
    y = 966
    slot = (WIDTH - 112) / len(PIPELINE)
    fnt = font("consola.ttf", 19)
    for index, name in enumerate(PIPELINE):
        x = 56 + index * slot
        colour = ACCENT if name in active else FAINT
        draw.text((x, y), name, font=fnt, fill=colour)
        if name in active:
            width = draw.textlength(name, font=fnt)
            draw.rectangle((x, y + 26, x + width, y + 29), fill=ACCENT)


def draw_title_card(draw: ImageDraw.ImageDraw, t: float) -> None:
    """The splash before the run starts."""
    draw.rectangle((0, 0, WIDTH, HEIGHT), fill=BG)
    draw.text((WIDTH // 2, 430), "GMI NEXUS", font=font("segoeuib.ttf", 96), fill=WHITE, anchor="mm")
    draw.text(
        (WIDTH // 2, 540),
        "a Hermes Agent plugin",
        font=font("segoeui.ttf", 32),
        fill=ACCENT,
        anchor="mm",
    )
    draw.text(
        (WIDTH // 2, 600),
        "one goal · many models · one coordinated system",
        font=font("segoeui.ttf", 24),
        fill=DIM,
        anchor="mm",
    )


def draw_end_card(draw: ImageDraw.ImageDraw, t: float, timeline: Timeline) -> None:
    """The closing card: the name, the pipeline, and the ask."""
    draw.rectangle((0, 0, WIDTH, HEIGHT), fill=BG)
    segment = timeline.segment_at(t)
    fraction = 0.0
    if segment:
        fraction = (t - segment.start) / max(segment.duration, 0.001)
    draw.text((WIDTH // 2, 250), "GMI NEXUS", font=font("segoeuib.ttf", 92), fill=WHITE, anchor="mm")
    draw.text(
        (WIDTH // 2, 348),
        "one goal · many models · one coordinated system",
        font=font("segoeui.ttf", 30),
        fill=DIM,
        anchor="mm",
    )
    shown = max(1, min(len(PIPELINE), int(fraction * len(PIPELINE) * 1.7) + 1))
    fnt = font("consola.ttf", 26)
    width = sum(draw.textlength(name, font=fnt) + 40 for name in PIPELINE[:shown])
    x = (WIDTH - width) / 2
    for name in PIPELINE[:shown]:
        draw.text((x, 452), name, font=fnt, fill=ACCENT)
        x += draw.textlength(name, font=fnt) + 40
    draw.text(
        (WIDTH // 2, 560),
        "eight phases · five modules · eight tools · one command",
        font=font("segoeui.ttf", 21),
        fill=FAINT,
        anchor="mm",
    )
    draw.text(
        (WIDTH // 2, 680),
        "Built against the live GMI Cloud service.",
        font=font("segoeui.ttf", 26),
        fill=TEXT,
        anchor="mm",
    )
    draw.text(
        (WIDTH // 2, 722),
        "Every number in this video came from one real run.",
        font=font("segoeui.ttf", 22),
        fill=DIM,
        anchor="mm",
    )
    draw.text(
        (WIDTH // 2, 830),
        "@NousResearch  ·  Hermes Agent Plugin Hackathon",
        font=font("consola.ttf", 24),
        fill=ACCENT,
        anchor="mm",
    )


def draw_frame(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    t: float,
    timeline: Timeline,
    lines: Sequence[TermLine],
    revealed: int,
    run: dict[str, Any],
    image: Path | None,
    screenshot: Path | None,
    fraction: float,
) -> None:
    """Draw one frame of the video."""
    draw.rectangle((0, 0, WIDTH, HEIGHT), fill=BG)
    segment = timeline.segment_at(t)
    segment_id = segment.id if segment else "title"

    if segment_id == "title":
        draw_title_card(draw, t)
        return
    if segment_id == "outro":
        draw_end_card(draw, t, timeline)
        return

    draw.text((56, 28), "GMI NEXUS", font=font("segoeuib.ttf", 34), fill=WHITE)
    draw.text((252, 36), "a Hermes Agent plugin", font=font("segoeui.ttf", 21), fill=DIM)
    draw.text(
        (WIDTH - 56, 36),
        "live, against GMI Cloud",
        font=font("consola.ttf", 20),
        fill=ACCENT,
        anchor="ra",
    )
    draw.line((56, 92, WIDTH - 56, 92), fill=BORDER)

    term_inner = panel(draw, (56, 120, 1186, 920), "gmi-nexus — one live run")
    draw_terminal(draw, term_inner, lines, revealed, cursor=(revealed < len(lines)))

    side_inner = panel(draw, (1216, 120, 1864, 920), "what this means")
    if segment_id == "intro":
        right_intro(draw, side_inner)
    elif segment_id == "catalogue":
        right_catalogue(draw, side_inner, run)
    elif segment_id == "route":
        right_route(draw, side_inner, run)
    elif segment_id == "generate":
        right_generate(canvas, draw, side_inner, run, image, show_defects=fraction > 0.45)
    elif segment_id == "build":
        right_build(canvas, draw, side_inner, run, screenshot)
    elif segment_id == "report":
        right_report(draw, side_inner, run)

    draw_pipeline(draw, t, timeline)


# --------------------------------------------------------------------------- #
# The reveal schedule
# --------------------------------------------------------------------------- #


@dataclass
class Reveal:
    """When each terminal line appears, and how many are on screen."""

    lines: list[TermLine]
    #: segment id -> [(line index, time)]
    schedule: dict[str, list[tuple[int, float]]] = field(default_factory=dict)

    def count_at(self, segment_id: str, t: float) -> int:
        count = 0
        for index, when in self.schedule.get(segment_id, []):
            if when <= t:
                count = max(count, index + 1)
        return count


def build_reveal(
    timeline: Timeline, lines: list[TermLine], blocks: dict[str, TermBlock]
) -> Reveal:
    """Decide when each terminal line appears.

    The lines of a phase are spread across the window in which that phase is
    on screen, in proportion to how much there is to read, so the terminal
    types at a steady rate rather than dumping everything at once.
    """

    def spread(
        segment: Segment,
        positions: Sequence[int],
        window: tuple[float, float],
    ) -> list[tuple[int, float]]:
        """Spread line positions across a fraction of a segment's window."""
        if not positions:
            return []
        weights = [max(1, len(lines[p].text)) for p in positions]
        total = sum(weights)
        begin = segment.start + window[0] * segment.duration
        span = max(0.4, (window[1] - window[0]) * segment.duration)
        entries: list[tuple[int, float]] = []
        clock = 0.0
        for position, weight in zip(positions, weights):
            clock += span * weight / total
            entries.append((position, begin + clock))
        return entries

    reveal = Reveal(lines=lines)
    for segment in timeline.segments:
        entries: list[tuple[int, float]] = []
        if segment.id == "intro":
            entries = [
                (index, segment.start + 0.2 + index * 0.3)
                for index in range(min(2, len(lines)))
            ]
        elif segment.id == "catalogue":
            block = blocks.get("1", TermBlock("1"))
            entries = spread(segment, block.banner + block.fields[:3], (0.0, 1.0))
        elif segment.id == "route":
            block = blocks.get("1", TermBlock("1"))
            entries = spread(segment, block.fields[3:], (0.0, 1.0))
        elif segment.id == "generate":
            first = blocks.get("2", TermBlock("2"))
            second = blocks.get("3", TermBlock("3"))
            entries = spread(segment, first.banner + first.fields, (0.0, 0.45))
            entries += spread(segment, second.banner + second.fields, (0.45, 1.0))
        elif segment.id == "build":
            block = blocks.get("4", TermBlock("4"))
            entries = spread(segment, block.banner + block.fields, (0.0, 1.0))
        elif segment.id == "report":
            block = blocks.get("5", TermBlock("5"))
            entries = spread(segment, block.banner + block.fields, (0.0, 1.0))
        reveal.schedule[segment.id] = entries
    return reveal


# --------------------------------------------------------------------------- #
# The build
# --------------------------------------------------------------------------- #


def read_wav(path: Path) -> tuple[bytes, int]:
    """Read a mono 16-bit WAV and return its samples and its rate."""
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    return frames, rate


def normalise(samples: bytes) -> bytes:
    """Scale the narration so its loudest peak sits just under 0 dBFS.

    Speech that was synthesised quietly is hard to hear on a phone, and the
    video is meant to be watched on one.
    """
    data = array.array("h")
    data.frombytes(samples)
    if not data:
        return samples
    peak = max((abs(value) for value in data), default=0)
    if peak == 0:
        return samples
    gain = (32767 * 0.89) / peak
    if 0.98 <= gain <= 1.02:
        return samples
    for index, value in enumerate(data):
        scaled = int(value * gain)
        data[index] = 32767 if scaled > 32767 else (-32768 if scaled < -32768 else scaled)
    return data.tobytes()


def write_wav(path: Path, samples: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples)


def build(args: argparse.Namespace) -> int:
    run = json.loads(Path(args.run).read_text(encoding="utf-8"))
    transcript = Path(args.transcript).read_text(encoding="utf-8")
    narration = json.loads(Path(args.narration).read_text(encoding="utf-8"))

    header, phases = parse_transcript(transcript)
    if len(phases) < 5:
        print(f"Only {len(phases)} phases found in the transcript.", file=sys.stderr)
        return 1

    order = ["intro", "catalogue", "route", "generate", "build", "report", "outro"]
    timeline = build_timeline(narration, order)
    lines, blocks = terminal_lines(header, phases)
    reveal = build_reveal(timeline, lines, blocks)

    image = Path(args.image) if args.image else None
    screenshot = Path(args.screenshot) if args.screenshot else None

    # -- the audio: the narration, laid on the same clock ------------------ #
    rate = 24000
    parts: list[bytes] = [b"\x00\x00" * int(rate * timeline.title_seconds)]
    for segment in timeline.segments:
        samples, rate = read_wav(Path(segment.path))
        parts.append(b"\x00\x00" * int(rate * timeline.gap))
        parts.append(samples)
    audio_path = Path(args.out).parent / "narration.wav"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    joined = normalise(b"".join(parts))
    write_wav(audio_path, joined, rate)
    audio_seconds = len(joined) / 2 / rate

    total = timeline.total
    frames = int(math.ceil(total * FPS))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    print(
        f"Cutting {frames} frames ({total:.1f}s of video, {audio_seconds:.1f}s of audio) "
        f"at {WIDTH}x{HEIGHT}..."
    )

    process = subprocess.Popen(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{WIDTH}x{HEIGHT}",
            "-r", str(FPS), "-i", "-",
            "-i", str(audio_path),
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "1",
            "-strict", "-2",
            "-shortest", str(out),
        ],
        stdin=subprocess.PIPE,
    )
    assert process.stdin is not None

    canvas = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(canvas)
    for index in range(frames):
        t = index / FPS
        segment = timeline.segment_at(t)
        segment_id = segment.id if segment else "title"
        fraction = 0.0
        if segment:
            fraction = (t - segment.start) / max(segment.duration, 0.001)
        revealed = reveal.count_at(segment_id, t) if segment else 0
        draw_frame(
            canvas, draw, t, timeline, reveal.lines, revealed, run, image, screenshot, fraction
        )
        process.stdin.write(canvas.tobytes())
        if index % 150 == 0:
            print(f"  {index}/{frames} frames", flush=True)

    process.stdin.close()
    process.wait()
    print(f"Wrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--narration", required=True)
    parser.add_argument("--image", default="")
    parser.add_argument("--screenshot", default="")
    parser.add_argument("--out", default="demo_output/gmi-nexus-demo.mp4")
    return build(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
