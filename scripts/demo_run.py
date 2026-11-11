#!/usr/bin/env python3
"""Run the hackathon demo scenario, end to end.

The scenario is the one in the brief:

    analyse → break down → select models → generate visual assets →
    implement through Hermes' tools → inspect with a vision model →
    detect issues → correct → verify → display the execution and
    model-performance report

Everything the demo prints comes from the plugin's own tool handlers, so
what you see is what an agent would get from the same eight tools.

The Hermes side is a stand-in. ``LocalHost`` implements the three tools the
plugin dispatches — ``read_file``, ``write_file`` and ``terminal`` — with a
workspace jail and a short refusal list, so the demo can run outside Hermes.
Inside a real Hermes install the same calls reach Hermes' own tools and
Hermes' approval policy applies to them. That path is verified separately, by
``scripts/validate_installation.py`` and by the dashboard, which was opened
in a browser inside a real Hermes install.

Two modes
---------

``--dry-run`` (the default)
    No network, no credential, no money. GMI Cloud is replaced by a scripted
    stand-in, so the *orchestration* is real — the router really scores the
    catalogue, the budget really refuses an over-limit request, the Dev Agent
    really writes a file and really runs its test — while the *outputs* are
    simulated. Every simulated value is labelled ``simulated`` in the
    transcript, so a dry run can never be mistaken for a real one.

``--live``
    Real GMI Cloud calls. Needs ``GMI_API_KEY`` and ``--approve-spend``, and
    it prints what it is about to spend before it spends it. The cost of the
    run is bounded by the plugin's own limits, which this script does not
    raise.

    ``--approve-spend`` is a *person's* approval. Inside Hermes the same
    authorisation arrives as a single-use token that the plugin's
    ``pre_tool_call`` hook issues once a person has answered Hermes' own
    approval prompt; outside Hermes, this script issues that token itself.
    The model cannot approve its own spend either way.

Usage
-----

    python scripts/demo_run.py --dry-run
    python scripts/demo_run.py --live --approve-spend
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "plugins" / "gmi-nexus"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from nexus.http import HttpResponse  # noqa: E402
from nexus.tools import NexusToolBox  # noqa: E402

#: The goal the brief describes.
GOAL = (
    "Build a landing page for a new AI startup that ships a multimodal "
    "model router. Write the page, generate a hero visual for it, and "
    "check the result."
)

#: The task the Dev Agent is given. It is deliberately narrow: the demo
#: should produce something that can be *run*, so the verification step has
#: something to verify.
DEV_TASK = (
    "Create a single self-contained file 'landing/index.html' that is the "
    "landing page for an AI startup called Northbeam. It must have a hero "
    "section with a headline and a call to action, a features section with "
    "three cards, and a footer. Use only inline CSS and no external assets."
)

#: The prompt the hero visual is generated from. The corrections found by
#: the vision model are appended to it when the asset is regenerated.
HERO_PROMPT = (
    "Abstract hero illustration for an AI infrastructure startup: "
    "a single beam of light splitting into many thin parallel "
    "beams, deep blue on a pale background, clean, minimal, no text."
)

#: The number the dry run's capability probe draws.
#:
#: The real probe draws a random three-digit number and asks the model to
#: read it back, because a probe that can be passed by accident proves
#: nothing. The dry run has no real model to answer, so its stand-in draws
#: a fixed number and reads it back. The orchestration is real; the answer
#: is simulated, like every other answer in this mode.
DEMO_PROBE_NUMBER = "473"


def hero_task(task: str, image: str) -> str:
    """Return the Dev Agent's task, with the hero asset to embed."""
    if not image:
        return task
    return (
        f"{task}\n\nA hero visual has been generated and is served from "
        f"{image}. Put it at the top of the hero section as an <img> element "
        f"with that URL as its src, and give it a short alt text."
    )

#: The command that proves the file is real and valid.
TEST_COMMAND = "python -c \"import pathlib,sys; p=pathlib.Path('landing/index.html'); t=p.read_text(encoding='utf-8'); assert '<h1' in t and '<html' in t; print('landing page OK,', len(t), 'bytes')\""


# --------------------------------------------------------------------------- #
# A stand-in for the slice of Hermes' plugin context the plugin uses.
# --------------------------------------------------------------------------- #


class LocalHost:
    """The slice of Hermes' plugin context that NEXUS needs.

    NEXUS does not run code itself. It calls Hermes' own tools through
    ``ctx.dispatch_tool``, so Hermes' approval policy, sandboxing and audit
    trail apply. This stand-in implements the same three tools — ``read_file``,
    ``write_file`` and ``terminal`` — inside a private temporary workspace.

    Two guard rails are worth naming, because the real Hermes terminal tool
    applies the *host's* policy and this one applies its own:

    * ``write_file`` refuses any path outside the workspace, so a demo can
      never overwrite a file it was not given.
    * ``terminal`` refuses a small list of destructive commands outright.
      Nothing in the demo needs them.
    """

    #: Commands the demo will not run, whatever the model asks for.
    DESTRUCTIVE = (
        r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*r[a-zA-Z]*f",
        r"\bmkfs\b",
        r"\bdd\s+.*of=/dev/",
        r":\(\)\s*\{",
        r"\bshutdown\b",
        r"\breboot\b",
        r"\bformat\s+[a-zA-Z]:",
        r"\bdel\s+/[a-zA-Z]",
    )

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.calls: list[dict[str, Any]] = []
        self.refusals: list[str] = []

    # -- the plugin context surface ----------------------------------------- #

    def get_config(self, key: str, default: Any = None) -> Any:
        """Return a plugin setting. The demo keeps the defaults."""
        return default

    def dispatch_tool(self, tool: str, args: dict[str, Any]) -> str:
        """Run one Hermes tool and return its JSON result."""
        self.calls.append({"tool": tool, "args": dict(args)})
        handler = {
            "read_file": self._read_file,
            "write_file": self._write_file,
            "terminal": self._terminal,
        }.get(tool)
        if handler is None:
            return json.dumps({"error": f"the demo host has no '{tool}' tool"})
        return json.dumps(handler(args))

    # -- the tools ----------------------------------------------------------- #

    def _resolve(self, raw_path: str) -> Path | None:
        """Resolve a path inside the workspace, or ``None`` if it escapes."""
        candidate = (self.workspace / raw_path).resolve()
        try:
            candidate.relative_to(self.workspace)
        except ValueError:
            self.refusals.append(f"write outside the workspace refused: {raw_path}")
            return None
        return candidate

    def _read_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve(str(args.get("path") or ""))
        if path is None:
            return {"error": "that path is outside the demo workspace"}
        if not path.exists():
            return {"error": f"no such file: {args.get('path')}"}
        return {"content": path.read_text(encoding="utf-8", errors="replace")}

    def _write_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve(str(args.get("path") or ""))
        if path is None:
            return {"error": "that path is outside the demo workspace"}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(args.get("content") or ""), encoding="utf-8")
        return {"ok": True, "path": str(path.relative_to(self.workspace))}

    def _terminal(self, args: dict[str, Any]) -> dict[str, Any]:
        command = str(args.get("command") or "")
        for pattern in self.DESTRUCTIVE:
            if re.search(pattern, command):
                self.refusals.append(f"destructive command refused: {command}")
                return {
                    "error": (
                        "the demo host refuses destructive commands; this one "
                        "was not run"
                    ),
                    "exit_code": 1,
                    "output": "",
                }
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=int(args.get("timeout") or 180),
            )
        except subprocess.TimeoutExpired:
            return {"error": "the command timed out", "exit_code": 1, "output": ""}
        return {
            "exit_code": completed.returncode,
            "output": (completed.stdout or "") + (completed.stderr or ""),
        }


# --------------------------------------------------------------------------- #
# The scripted stand-in for GMI Cloud, used by the free dry run.
# --------------------------------------------------------------------------- #

#: The host the dry run's generated assets are served from.
#:
#: It is a reserved name that can never be registered or resolved, which is
#: exactly what a dry run wants: the URL is obviously not a real one, and
#: nothing can reach it.
DEMO_ASSET_HOST = "example.invalid"


def demo_resolve_host(host: str, port: int) -> list[str]:
    """The dry run's stand-in for DNS.

    The plugin checks that a URL it was given resolves to a public address
    before it fetches it, and refuses a host that cannot be resolved at all.
    The dry run makes no network calls, so its assets are served by the
    stand-in transport instead — which means the stand-in has to stand in
    for the resolver too, or the plugin would refuse the dry run's own
    asset for the right reason at the wrong moment.

    Only the dry run's own asset host is answered. Everything else is
    refused, so the plugin's refusal path stays exactly as it is.
    """
    if host == DEMO_ASSET_HOST:
        return ["93.184.216.34"]
    raise socket.gaierror(f"the dry run has no address for {host!r}")


def demo_asset_png() -> bytes:
    """The bytes of the image the dry run's stand-in serves.

    A real PNG, so the plugin's check on the bytes — that what arrived is
    an image, whatever the server called it — has something real to agree
    with. It is drawn with the plugin's own PNG writer, so the dry run
    needs no image library.
    """
    from nexus.modules.vision import png_data_uri

    width, height = 64, 36
    pixels: list[list[tuple[int, int, int]]] = []
    for y in range(height):
        row: list[tuple[int, int, int]] = []
        for x in range(width):
            # A pale field with a few deep-blue beams through it: the same
            # picture the real hero prompt asks for, in miniature.
            beam = any(abs(x - offset) < 2 for offset in (10, 24, 40, 54))
            row.append((30, 58, 140) if beam else (238, 242, 250))
        pixels.append(row)
    data_uri = png_data_uri(pixels)
    return base64.b64decode(data_uri.split(",", 1)[1])

#: A small catalogue that exercises the router: a code specialist, a
#: reasoning model, a vision model that *declares* its modality, a cheap
#: generalist and a middling one.
DEMO_CATALOGUE: dict[str, Any] = {
    "object": "list",
    "data": [
        {
            "id": "openai/gpt-4o",
            "name": "GPT-4o",
            "owned_by": "openai",
            "description": "A multimodal model with vision.",
            "context_length": 128000,
            "pricing": {"prompt": "0.0000025", "completion": "0.00001"},
            "discount_to_user": 0.3,
            "input_modalities": [{"type": "text"}, {"type": "image"}],
            "output_modalities": [
                {
                    "type": "text",
                    "supported_parameters": {"tools": {"type": "boolean"}},
                }
            ],
        },
        {
            "id": "anthropic/claude-sonnet-5",
            "name": "Claude Sonnet 5",
            "owned_by": "anthropic",
            "description": "A strong generalist model for code and reasoning.",
            "context_length": 200000,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            "discount_to_user": 0.3,
        },
        {
            "id": "qwen/qwen3-coder-plus",
            "name": "Qwen3 Coder Plus",
            "owned_by": "qwen",
            "description": "A code-specialised model.",
            "context_length": 262144,
            "pricing": {"prompt": "0.0000012", "completion": "0.000004"},
            "discount_to_user": 0.4,
        },
        {
            "id": "google/gemini-3.1-flash-lite",
            "name": "Gemini 3.1 Flash Lite",
            "owned_by": "google",
            "description": "A fast, cheap generalist.",
            "context_length": 1048576,
            "pricing": {"prompt": "0.000000075", "completion": "0.0000003"},
            "discount_to_user": 0.1,
        },
    ],
}

#: The generation models the dry run offers, each with its own parameter
#: names. The image model and the audio model deliberately do not share a
#: parameter schema — that is the point.
DEMO_MEDIA_SPECS: dict[str, dict[str, Any]] = {
    "gpt-image-2": {
        "model": "gpt-image-2",
        "display_name": "GPT Image 2",
        "model_type": "image",
        "external_provider": "openai",
        "status": "active",
        "brief_description": "Image generation.",
        "percentage_discount": 0,
        "parameters": [
            {"name": "prompt", "type": "string", "required": True, "description": "The prompt."},
            {
                "name": "aspect_ratio",
                "type": "enum",
                "required": False,
                "options": ["1024x1024", "1344x768", "768x1344"],
                "description": "Output size.",
            },
            {"name": "n", "type": "integer", "required": False, "default_value": 1},
        ],
        "price_info": {"price": 42000, "unit": "image"},
    },
    "elevenlabs-tts-v3": {
        "model": "elevenlabs-tts-v3",
        "display_name": "ElevenLabs TTS v3",
        "model_type": "audio",
        "external_provider": "elevenlabs",
        "status": "active",
        "brief_description": "Text to speech.",
        "percentage_discount": 0,
        "parameters": [
            {"name": "text", "type": "string", "required": True, "description": "What to say."},
            {
                "name": "voice_id",
                "type": "string",
                "required": True,
                "description": "The voice to read it in.",
            },
            {"name": "output_format", "type": "enum", "required": False, "options": ["mp3", "wav"]},
        ],
        "price_info": {"price": 500, "unit": "request"},
    },
}


class DemoTransport:
    """A scripted stand-in for GMI Cloud.

    It answers the same endpoints the plugin calls, with the same shapes, so
    the plugin's real code runs unchanged. What it returns is *simulated* —
    the demo labels it as such everywhere it is shown.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._chat_calls = 0
        self._media_calls = 0
        #: request id → the asset that request produced. Each generation gets
        #: its own asset, so a before/after comparison compares two different
        #: images rather than the same one twice.
        self._assets: dict[str, str] = {}

    # -- bookkeeping -------------------------------------------------------- #

    def urls(self) -> list[str]:
        return [entry["url"] for entry in self.requests]

    # -- the transport interface --------------------------------------------- #

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        data: Any = None,
        files: Any = None,
        timeout: float = 60.0,
        follow_redirects: bool = True,
    ) -> HttpResponse:
        self.requests.append(
            {"method": method, "url": url, "body": json_body, "headers": dict(headers or {})}
        )

        if url.endswith("/chat/completions"):
            return self._chat(json_body or {})
        if url.endswith("/apikey/models"):
            return HttpResponse(
                status=200,
                body=json.dumps({"model_ids": list(DEMO_MEDIA_SPECS)}),
                headers={"content-type": "application/json"},
            )
        if "/apikey/models/" in url:
            model_id = url.rsplit("/", 1)[-1]
            spec = DEMO_MEDIA_SPECS.get(model_id)
            if spec is None:
                return HttpResponse(status=404, body=json.dumps({"error": "no such model"}))
            return HttpResponse(status=200, body=json.dumps(spec), headers={"content-type": "application/json"})
        if url.endswith("/requests") and method == "POST":
            self._media_calls += 1
            request_id = f"demo-request-{self._media_calls:04d}"
            self._assets[request_id] = f"demo-asset-{self._media_calls:04d}"
            return HttpResponse(
                status=200,
                body=json.dumps({"request_id": request_id}),
                headers={"content-type": "application/json"},
            )
        if "/requests/" in url:
            request_id = url.rsplit("/", 1)[-1]
            asset_id = self._assets.get(request_id, "demo-asset-0000")
            return HttpResponse(
                status=200,
                body=json.dumps(
                    {
                        "request_id": request_id,
                        "status": "success",
                        "outcome": {
                            "media_urls": [
                                {
                                    "id": asset_id,
                                    "url": f"https://example.invalid/{asset_id}.png",
                                }
                            ]
                        },
                    }
                ),
                headers={"content-type": "application/json"},
            )
        if url.endswith("/models"):
            return HttpResponse(
                status=200, body=json.dumps(DEMO_CATALOGUE), headers={"content-type": "application/json"}
            )
        if url.startswith(f"https://{DEMO_ASSET_HOST}/"):
            # A generated asset. Real PNG bytes, so the plugin's check on
            # the bytes has something real to agree with.
            return HttpResponse(
                status=200,
                body="",
                headers={"content-type": "image/png"},
                raw=demo_asset_png(),
            )
        return HttpResponse(status=404, body=json.dumps({"error": f"no route for {url}"}))

    # -- the simulated replies ----------------------------------------------- #

    def _chat(self, body: dict[str, Any]) -> HttpResponse:
        """Answer one chat completion, according to what was asked."""
        self._chat_calls += 1
        prompt = _prompt_text(body)
        text = self._reply_for(prompt)
        payload = {
            "id": f"demo-chat-{self._chat_calls:04d}",
            "object": "chat.completion",
            "model": body.get("model"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            # The provider's own usage block. NEXUS prefers this over its own
            # estimate, so the dry run exercises the measured path too.
            "usage": {"prompt_tokens": 512, "completion_tokens": 256, "total_tokens": 768},
        }
        return HttpResponse(status=200, body=json.dumps(payload), headers={"content-type": "application/json"})

    @staticmethod
    def _reply_for(prompt: str) -> str:
        """Pick the reply that matches the prompt.

        The stub answers each kind of step the way that step asked to be
        answered, so the pipeline's own evaluator sees what a real model
        would have returned. The landing page it writes embeds whichever
        hero image the task asked for, so the page and the asset agree.
        """
        page = _LANDING_PAGE.replace("__HERO__", _hero_in(prompt))
        if "three-digit number" in prompt:
            # The capability probe. The stand-in can see, so it reads the
            # number back.
            return DEMO_PROBE_NUMBER
        if "You are implementing a change" in prompt or "does not pass its tests" in prompt:
            return json.dumps(
                {
                    "files": [
                        {
                            "path": "landing/index.html",
                            "content": page,
                        }
                    ],
                    "notes": "Wrote a self-contained landing page with a hero, three feature cards and a footer.",
                }
            )
        if "Produce complete, runnable code" in prompt:
            return (
                "Here is the page, as one self-contained file:\n\n"
                "```html\n" + page + "\n```\n\n"
                "The hero carries the headline and the call to action, the three "
                "feature cards sit in a row above the footer, and every style is "
                "inline so the file works on its own."
            )
        if "List concrete problems" in prompt:
            return (
                "The page is structurally sound, but two things should change. "
                "First, the hero section has no vertical spacing between the "
                "headline and the call-to-action button, so they read as one "
                "block. Second, the footer text is set at a very low contrast "
                "against the page background, which makes it hard to read."
            )
        if "You are reviewing a visual artefact" in prompt:
            return json.dumps(
                {
                    "issues": [
                        {
                            "severity": "medium",
                            "area": "the right edge",
                            "problem": "the beam is cut off by the edge of the frame",
                            "fix": "keep the whole beam inside the frame, with a margin around it",
                        },
                        {
                            "severity": "low",
                            "area": "the background",
                            "problem": "the background and the beams are so close in tone that the subject barely separates",
                            "fix": "darken the background so the beams read clearly",
                        },
                    ],
                    "summary": "Two concrete problems: the beam is clipped, and the subject does not separate from the background.",
                }
            )
        if "Compare these two images" in prompt:
            return json.dumps(
                {
                    "changes": ["the beam is no longer clipped by the frame"],
                    "improvements": [
                        "the whole beam is visible, with a margin around it",
                        "the subject separates from the background",
                    ],
                    "regressions": [],
                    "summary": "Both detected problems were fixed, and nothing regressed.",
                }
            )
        return (
            "Here is a plan for the landing page: a hero section with a headline "
            "and a call to action, three feature cards, and a footer. It is "
            "built as one self-contained HTML file with inline CSS."
        )


def _prompt_text(body: dict[str, Any]) -> str:
    """Pull the prompt text out of a chat request body."""
    parts: list[str] = []
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
    return "\n".join(parts)


#: The page the simulated Dev Agent writes. It is real HTML: the dry run
#: genuinely writes it to disk and genuinely tests it. ``__HERO__`` is
#: replaced with the URL of whichever asset was generated, so the page and
#: the image agree with each other.
_LANDING_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Northbeam - one agent, many models</title>
<style>
  :root { color-scheme: light; }
  body { margin: 0; font: 16px/1.6 system-ui, sans-serif; color: #0b0f19; }
  .hero { padding: 96px 24px; text-align: center; }
  .hero img { max-width: 720px; width: 100%; border-radius: 12px; }
  h1 { font-size: 48px; margin: 0 0 16px; }
  .cta { display: inline-block; padding: 12px 24px; border-radius: 8px;
         background: #2563eb; color: #fff; text-decoration: none; }
  .features { display: flex; gap: 24px; padding: 48px 24px; }
  .card { flex: 1; padding: 24px; border: 1px solid #e2e6ee; border-radius: 10px; }
  footer { padding: 32px 24px; color: #5b6478; text-align: center; }
</style>
</head>
<body>
  <section class="hero">
    <img src="__HERO__" alt="A single beam of light splitting into many thin parallel beams">
    <h1>One agent. Many models. One intelligent system.</h1>
    <p>Northbeam routes every step of your work to the model that is best at it.</p>
    <a class="cta" href="#start">Start building</a>
  </section>
  <section class="features">
    <div class="card"><h2>Live routing</h2><p>Models are discovered at runtime, not hard-coded.</p></div>
    <div class="card"><h2>Measured quality</h2><p>Every step is scored against its own criteria.</p></div>
    <div class="card"><h2>Bounded spend</h2><p>Per-run and daily ceilings, enforced before the request.</p></div>
  </section>
  <footer>Northbeam. Simulated output for the GMI NEXUS demo.</footer>
</body>
</html>
"""


def _hero_in(prompt: str) -> str:
    """Find the hero image URL inside a prompt.

    The Dev Agent is told which asset to embed, so the page it writes in
    the dry run carries the same URL the generation step produced.
    """
    match = re.search(r"https?://\S+", prompt)
    return match.group(0) if match else "https://example.invalid/demo-asset-0000.png"


# --------------------------------------------------------------------------- #
# The demo itself.
# --------------------------------------------------------------------------- #


class Transcript:
    """Collects what happened, for the summary at the end."""

    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []

    def add(self, name: str, payload: dict[str, Any], *, basis: str) -> None:
        self.steps.append({"step": name, "basis": basis, "payload": payload})

    def to_dict(self) -> dict[str, Any]:
        return {"steps": self.steps}


class Runner:
    """Walks the demo scenario, one phase at a time."""

    def __init__(
        self,
        toolbox: NexusToolBox,
        host: LocalHost,
        *,
        simulated: bool,
        approved: bool = False,
    ) -> None:
        self.box = toolbox
        self.host = host
        self.simulated = simulated
        #: Whether the person running the script authorised the spend. This
        #: is the demo's stand-in for Hermes' approval prompt: inside Hermes
        #: the same authorisation arrives as a token the ``pre_tool_call``
        #: hook issues once a person has answered the prompt.
        self.approved = approved
        self.transcript = Transcript()

    # -- helpers ------------------------------------------------------------- #

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Call one of the plugin's own tool handlers."""
        raw = getattr(self.box, tool)(self._approved_args(tool, args))
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = {"raw": str(raw)}
        return payload if isinstance(payload, dict) else {"result": payload}

    def _approved_args(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Attach the operator's approval to a call that may spend money.

        The plugin has no argument the *model* can set to approve its own
        spend: a call that reaches the approval threshold is held for a
        person, and only Hermes' own approval prompt — or, outside Hermes,
        the person running this script — can unlock it.

        So the authorisation here is a token, minted by the plugin's own
        ledger and bound to the arguments of this call. It is what the
        ``pre_tool_call`` hook issues inside Hermes, and it is what the
        ``--approve-spend`` flag means: *a person* said yes.
        """
        from nexus.approval import APPROVAL_TOKEN_KEY, SPENDING_TOOLS, approver

        if not self.approved or tool not in SPENDING_TOOLS:
            return args
        try:
            ceiling = float(self.box.settings().max_cost_per_run_usd)
        except Exception:
            ceiling = 0.0
        prepared = dict(args)
        prepared[APPROVAL_TOKEN_KEY] = approver().issue(
            tool,
            prepared,
            estimated_usd=None,
            ceiling_usd=ceiling,
        )
        return prepared

    def banner(self, title: str, note: str = "") -> None:
        print()
        print("=" * 78)
        print(f"  {title}")
        if note:
            print(f"  {note}")
        print("=" * 78)

    def field(self, label: str, value: Any, indent: int = 2) -> None:
        print(f"{' ' * indent}{label:<26} {value}")

    # -- the phases ---------------------------------------------------------- #

    def phase_analyse(self) -> dict[str, Any]:
        """UNDERSTAND and PLAN — no model is called, nothing is spent."""
        self.banner("1. UNDERSTAND + PLAN", "the goal is classified and broken into steps")
        analysis = self.call("nexus_analyze", {"goal": GOAL})
        understanding = analysis.get("understanding") or {}
        plan = analysis.get("plan") or {}
        steps = plan.get("steps") or []
        self.field("task type", understanding.get("task_label"))
        self.field("required capabilities", ", ".join(understanding.get("required_capabilities") or []) or "-")
        self.field("steps planned", len(steps))
        for step in steps:
            self.field(f"  step {step.get('index')}", f"{step.get('task_type')} — {step.get('description')}")
        catalogue = analysis.get("catalogue") or {}
        self.field("language models", catalogue.get("llm_models"))
        self.field("generation models", catalogue.get("media_models"))
        # An empty catalogue and a catalogue that could not be read are two
        # different states. Say which one this is, and stop if the read
        # failed — running the pipeline against a catalogue that was never
        # fetched would report "no eligible model" for every step, which
        # reads like a fact about GMI Cloud rather than a fault here.
        catalogue_error = catalogue.get("error")
        if catalogue_error:
            self.field("catalogue error", catalogue_error)
            raise SystemExit(
                "\nThe catalogue could not be read, so nothing below would be real.\n"
                f"  {catalogue_error}\n\n"
                "Check GMI_API_KEY and the network, then run this again."
            )
        budget = analysis.get("budget") or {}
        self.field(
            "limits",
            f"${budget.get('max_cost_per_run_usd')} a run, "
            f"${budget.get('daily_budget_usd')} a day, "
            f"approval above ${budget.get('approval_threshold_usd')}",
        )
        self.field("spent", "$0.00 — analysis calls no model")
        self.transcript.add("analyse", analysis, basis="measured" if not self.simulated else "simulated")
        return analysis

    def phase_route(self, analysis: dict[str, Any]) -> None:
        """ROUTE — every model scored, and the reasons it was ruled out."""
        self.banner("2. ROUTE", "each step is scored against the catalogue")
        estimated = 0.0
        for preview in analysis.get("routing_preview") or []:
            decision = preview.get("decision") or {}
            candidates = decision.get("candidates") or []
            chosen = candidates[0] if candidates else {}
            cost = float(chosen.get("estimated_cost_usd") or 0.0)
            estimated += cost
            self.field(
                f"step {preview.get('step_index')} -> {preview.get('task_type')}",
                f"{chosen.get('model') or '(none)'}   score {chosen.get('score')}"
                f"   ~${cost:.6f}",
            )
            for reason in (chosen.get("reasons") or [])[:2]:
                self.field("  because", reason)
            rejected = decision.get("rejected") or {}
            if rejected:
                for model_id, reason in list(rejected.items())[:2]:
                    self.field("  ruled out", f"{model_id} — {reason}")
                if len(rejected) > 2:
                    self.field("  ruled out", f"...and {len(rejected) - 2} more")
        # The last point before the first request goes out.
        self.field("plan estimated at", f"${estimated:.6f}  (an estimate, not a bill)")
        if not self.simulated:
            self.field("not counting", "the demo's two image generations and its vision and Dev Agent calls")

    def phase_execute(self) -> dict[str, Any]:
        """EXECUTE, EVALUATE, RECOVER, LEARN — the whole pipeline."""
        self.banner("3. EXECUTE THE PIPELINE", "the eight steps run for real")
        report = self.call("nexus_run", {"goal": GOAL})
        if report.get("error"):
            self.field("error", report["error"])
            return report
        self.field("run id", report.get("run_id"))
        self.field("status", report.get("status"))
        for step in report.get("steps") or []:
            self.field(
                f"  step {step.get('step_index')}",
                f"{step.get('status')} — {step.get('model_id') or 'no model'}"
                f"  ({step.get('attempts')} call(s), {step.get('latency_ms')} ms)",
            )
        self.field("models used", ", ".join(report.get("models_used") or []) or "-")
        self.field(
            "estimated cost",
            f"${float(report.get('total_cost_usd') or 0):.6f}  (an estimate, not a bill)",
        )
        for message in report.get("messages") or []:
            self.field("note", message)
        self.phase_recover(report)
        self.transcript.add("run", report, basis="measured" if not self.simulated else "simulated")
        return report

    def phase_recover(self, report: dict[str, Any]) -> None:
        """RECOVER — reconcile the published route with the route actually taken.

        The published route is what the router decided before anything ran.
        The executed route is what the models turned out to be. When they
        differ, the difference is the recovery: a model that was published
        and then failed, and the model that took over. Printing both without
        saying so would leave the two looking like they disagree.
        """
        routes = report.get("routes") or []
        steps = report.get("steps") or []
        published: dict[int, str] = {}
        for route in routes:
            candidates = route.get("candidates") or []
            if candidates:
                published[int(route.get("step_index", -1))] = str(
                    candidates[0].get("model") or ""
                )

        rows: list[str] = []
        for step in steps:
            index = int(step.get("step_index", -1))
            planned = published.get(index, "")
            used = str(step.get("model_id") or "")
            attempted = [str(a) for a in (step.get("attempted") or [])]
            fallbacks = [str(f) for f in (step.get("fallbacks") or [])]
            if not planned or not used:
                continue
            if planned == used:
                rows.append(f"step {index + 1}: {used} — as published")
            else:
                rows.append(
                    f"step {index + 1}: published {planned}, ran {used}"
                    + (f" after {', '.join(fallbacks)} failed" if fallbacks else "")
                )
            if len(attempted) > 1:
                rows.append(f"step {index + 1}: called {', '.join(attempted)}")

        self.banner("3b. RECOVER", "the published route against the route actually taken")
        if rows:
            for row in rows:
                self.field("recovered", row)
        else:
            self.field("recovered", "no step needed to deviate from the published route")

    def phase_generate(self) -> dict[str, Any]:
        """The visual asset — a real generation request in live mode."""
        self.banner("4. GENERATE A VISUAL ASSET", "the model's own published schema decides the request")
        result = self.generate_asset(HERO_PROMPT)
        if result.get("error"):
            self.field("error", result["error"])
        self.field("model", result.get("model_id"))
        self.field("status", result.get("status"))
        self.field("assets", len(result.get("media_urls") or []))
        self.field(
            "estimated cost",
            f"${float(result.get('estimated_cost_usd') or 0):.6f}  "
            f"({result.get('cost_note', 'an estimate, not a bill')})",
        )
        self.transcript.add("media", result, basis="measured" if not self.simulated else "simulated")
        return result

    def generate_asset(self, prompt: str) -> dict[str, Any]:
        """Generate one asset. No banner: the caller decides how it reads."""
        return self.call(
            "nexus_media",
            {
                "prompt": prompt,
                "modality": "image",
            },
        )

    def phase_implement(
        self,
        task: str,
        test_command: str,
        label: str = "5. IMPLEMENT THROUGH HERMES' TOOLS",
    ) -> dict[str, Any]:
        """IMPLEMENT — the files are written and the test is run, through tools."""
        self.banner(label, "write_file and terminal, never a shell of its own")
        outcome = self.call(
            "nexus_dev",
            {
                "mode": "implement",
                "task": task,
                "test_command": test_command,
            },
        )
        if outcome.get("error"):
            self.field("error", outcome["error"])
        self.field("model", outcome.get("model_id"))
        self.field("attempts", outcome.get("attempts"))
        for entry in outcome.get("recovery") or []:
            self.field("  recovered from", f"{entry.get('model_id')} — {entry.get('outcome')}")
        for change in outcome.get("files") or []:
            self.field(
                "  wrote",
                f"{change.get('path')} — "
                f"{'ok' if change.get('written') else change.get('error')}",
            )
        self.field("tests passed", outcome.get("tests_passed"))
        if outcome.get("test_output"):
            self.field("test output", str(outcome["test_output"]).strip()[:200])
        self.field("tools used", ", ".join(sorted({c["tool"] for c in self.host.calls})))
        self.transcript.add("dev", outcome, basis="measured" if not self.simulated else "simulated")
        return outcome

    def phase_inspect(self, image: str, label: str) -> dict[str, Any]:
        """INSPECT and DETECT — a vision model looks at the artefact."""
        self.banner(label, "a vision-capable model reads the artefact")
        outcome = self.call(
            "nexus_vision",
            {
                "mode": "issues",
                "images": [image],
                "criteria": "layout, spacing, typography, contrast and legibility",
            },
        )
        if outcome.get("error"):
            self.field("error", outcome["error"])
            return outcome
        self.field("model", outcome.get("model_id"))
        self.field("summary", outcome.get("text"))
        for issue in (outcome.get("structured") or {}).get("issues") or []:
            self.field(f"  [{issue.get('severity')}]", f"{issue.get('area')} — {issue.get('problem')}")
        self.transcript.add(label, outcome, basis="measured" if not self.simulated else "simulated")
        return outcome

    def phase_correct(
        self,
        issues: list[dict[str, Any]],
        task: str,
        test_command: str,
    ) -> dict[str, Any]:
        """CORRECT — the detected issues become the next instruction.

        Two things happen here, in this order, and both are real work: the
        asset is regenerated with the corrections in its prompt, and the page
        is rewritten to embed the corrected asset.
        """
        self.banner("7. CORRECT", "the detected issues become the next instruction")
        if not issues:
            self.field("nothing to correct", "no issues were detected")
            return {"image": "", "asset": {}, "implementation": {}}

        corrections = "; ".join(
            f"{issue.get('area')}: {issue.get('fix')}" for issue in issues
        )
        self.field("instructions", corrections[:300])

        self.banner("7a. CORRECT — REGENERATE THE ASSET", "the corrections go into the generation prompt")
        asset = self.generate_asset(f"{HERO_PROMPT} Corrections to apply: {corrections}")
        if asset.get("error"):
            self.field("error", asset["error"])
        self.field("model", asset.get("model_id"))
        self.field("status", asset.get("status"))
        self.field(
            "estimated cost",
            f"${float(asset.get('estimated_cost_usd') or 0):.6f}  "
            f"({asset.get('cost_note', 'an estimate, not a bill')})",
        )
        self.transcript.add("media-corrected", asset, basis="measured" if not self.simulated else "simulated")

        image = (asset.get("media_urls") or [""])[0]
        implementation = self.phase_implement(
            hero_task(task, image),
            test_command,
            label="7b. CORRECT — REWRITE THE PAGE AROUND THE FIXED ASSET",
        )
        return {"image": image, "asset": asset, "implementation": implementation}

    def phase_verify(
        self,
        before: str,
        after: str,
        implementation: dict[str, Any] | None = None,
    ) -> None:
        """VERIFY — the two assets are compared, and the test is re-run."""
        self.banner("8. VERIFY", "the corrected asset is compared with the original")
        outcome = self.call(
            "nexus_vision",
            {
                "mode": "compare",
                "before": before,
                "after": after,
                "question": "Did the corrections land?",
            },
        )
        if outcome.get("error"):
            self.field("error", outcome["error"])
        self.field("model", outcome.get("model_id"))
        self.field("summary", outcome.get("text"))
        for item in (outcome.get("structured") or {}).get("changes") or []:
            self.field("  changed", item)
        for item in (outcome.get("structured") or {}).get("improvements") or []:
            self.field("  improved", item)
        for item in (outcome.get("structured") or {}).get("regressions") or []:
            self.field("  regressed", item)
        if implementation:
            self.field("page tests passed", implementation.get("tests_passed"))
        self.transcript.add("verify", outcome, basis="measured" if not self.simulated else "simulated")

    def phase_report(self) -> dict[str, Any]:
        """REPORT — what actually happened."""
        self.banner("9. REPORT", "the execution and model-performance report")
        report = self.call("nexus_report", {"days": 7})
        self.field("runs recorded", len(report.get("recent_runs") or []))
        stats = report.get("model_stats") or []
        if stats:
            self.field("models with statistics", len(stats))
            for stat in stats[:8]:
                # One row per model *and* task type: reliability is tracked
                # per kind of work, so the task type is part of the name.
                self.field(
                    f"  {stat.get('model_id')} ({stat.get('task_type')})",
                    f"{stat.get('runs')} run(s), {stat.get('successes')} success"
                    f" — ${stat.get('total_cost_usd')} estimated",
                )
        usage = report.get("usage") or {}
        self.field("tokens used", usage.get("prompt_tokens"), indent=2)
        budget = report.get("budget") or {}
        label = "simulated spend today" if self.simulated else "spent today"
        self.field(label, f"${budget.get('daily_spend_usd')}")
        self.field("remaining today", f"${budget.get('daily_remaining_usd')}")
        self.field("note", report.get("cost_note"))
        self.transcript.add("report", report, basis="measured" if not self.simulated else "simulated")
        return report


# --------------------------------------------------------------------------- #
# Entry point.
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    # The demo prints arrows and em-dashes. Windows consoles default to a
    # code page that cannot encode them, which would crash the run over
    # presentation, so the streams are widened first.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - unusual streams
            pass

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--live",
        action="store_true",
        help="make real GMI Cloud calls (needs GMI_API_KEY and --approve-spend)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run with a scripted stand-in for GMI Cloud — no network, no spend (the default)",
    )
    parser.add_argument(
        "--approve-spend",
        action="store_true",
        help=(
            "confirm that the live run may spend GMI Cloud credits — this is a "
            "person's approval, carried to the plugin as a single-use token"
        ),
    )
    parser.add_argument(
        "--output",
        default=str(REPO_ROOT / "demo_output"),
        help="where the transcript is written (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    live = bool(args.live)
    if live and not args.approve_spend:
        print(
            "Refusing to run live without --approve-spend.\n"
            "A live run spends GMI Cloud credits. The dry run proves the same\n"
            "orchestration for nothing.",
            file=sys.stderr,
        )
        return 2
    if live and not os.environ.get("GMI_API_KEY"):
        print("Refusing to run live without GMI_API_KEY in the environment.", file=sys.stderr)
        return 2

    # Keep the demo out of the user's real Hermes history unless they ask.
    home = Path(tempfile.mkdtemp(prefix="nexus-demo-home-"))
    os.environ.setdefault("HERMES_HOME", str(home))
    workspace = Path(tempfile.mkdtemp(prefix="nexus-demo-work-"))

    print("GMI NEXUS — demo scenario")
    print(f"  mode          {'LIVE (real GMI Cloud calls, real spend)' if live else 'DRY (no network, no spend)'}")
    print(f"  hermes home   {home}")
    print(f"  workspace     {workspace}")
    if not live:
        print(
            "  note          GMI Cloud is replaced by a scripted stand-in. The\n"
            "                orchestration is real; the model outputs are simulated\n"
            "                and are labelled as such."
        )

    host = LocalHost(workspace)
    toolbox = NexusToolBox(host)

    if not live:
        # Replace the transport at the factory, so every component the plugin
        # builds — the registry, the executor, the media client — talks to the
        # stand-in instead of the network.
        import nexus.http as nexus_http
        import nexus.modules.vision as nexus_vision
        import nexus.netguard as nexus_netguard

        original = nexus_http.default_transport
        nexus_http.default_transport = DemoTransport
        # The stand-in reads back a fixed number, so the probe draws a fixed
        # number. The live run leaves this alone and draws at random.
        original_number = nexus_vision.random_probe_number
        nexus_vision.random_probe_number = lambda: DEMO_PROBE_NUMBER
        # The plugin resolves a URL's host before it fetches it, and refuses
        # one that cannot be resolved. The dry run's assets are served by the
        # stand-in, so the stand-in stands in for the resolver as well — for
        # its own host only, so the refusal path is untouched.
        original_resolver = nexus_netguard.resolve_host
        nexus_netguard.resolve_host = demo_resolve_host
        try:
            runner = Runner(toolbox, host, simulated=True, approved=bool(args.approve_spend))
            _run_scenario(runner, host)
        finally:
            nexus_http.default_transport = original
            nexus_vision.random_probe_number = original_number
            nexus_netguard.resolve_host = original_resolver
    else:
        runner = Runner(toolbox, host, simulated=False, approved=bool(args.approve_spend))
        _run_scenario(runner, host)

    # Write the transcript.
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    transcript_path = output / "last_run.json"
    transcript_path.write_text(
        json.dumps(
            {
                "mode": "live" if live else "dry-run",
                "simulated": not live,
                "goal": GOAL,
                "workspace": str(workspace),
                "hermes_home": str(home),
                "steps": runner.transcript.steps,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print()
    print(f"Transcript written to {transcript_path}")
    print(f"Workspace kept at   {workspace}")

    if host.refusals:
        print()
        print("Refusals (the demo host's own guard rails):")
        for refusal in host.refusals:
            print(f"  - {refusal}")

    # Clean up the temporary Hermes home, but never the workspace: the files
    # the demo wrote are the point of it.
    shutil.rmtree(home, ignore_errors=True)
    return 0


def _run_scenario(runner: Runner, host: LocalHost) -> None:
    """Run the nine phases of the demo."""
    analysis = runner.phase_analyse()
    runner.phase_route(analysis)
    runner.phase_execute()

    media = runner.phase_generate()
    image = (media.get("media_urls") or [""])[0]
    if not image:
        image = "https://example.invalid/demo-asset-0000.png"

    implementation = runner.phase_implement(hero_task(DEV_TASK, image), TEST_COMMAND)
    if implementation.get("error"):
        print("  the implementation did not complete; continuing with what exists")

    inspection = runner.phase_inspect(image, "6. INSPECT + DETECT ISSUES")
    issues = (inspection.get("structured") or {}).get("issues") or []

    correction = runner.phase_correct(issues, DEV_TASK, TEST_COMMAND)
    runner.phase_verify(image, correction.get("image") or image, correction.get("implementation"))
    runner.phase_report()


if __name__ == "__main__":
    raise SystemExit(main())
