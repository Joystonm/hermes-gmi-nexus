#!/usr/bin/env python3
"""Does delivering an image as a URL work where a data URI does not?

A data URI carries the whole image inside the request body. A URL lets the
provider fetch it. The two are very different in size, and the live models
behave differently for each. This script sends the same real asset both ways
to every model that passed the capability probe, plus a couple that failed
it, and prints what each one says.

    GMI_API_KEY=... python scripts/probe_url_vs_datauri.py
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "plugins" / "gmi-nexus"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from nexus.config import Settings  # noqa: E402
from nexus.http import HttpClient  # noqa: E402
from nexus.providers.gmi_llm import GmiLLMClient, build_user_message  # noqa: E402

HERO = REPO_ROOT / "demo_output" / "hero.png"
TRANSCRIPT = REPO_ROOT / "demo_output" / "last_run.json"

MODELS = [
    "google/gemini-3.5-flash-lite",
    "openai/gpt-5.5",
    "google/gemini-3.1-flash-lite-preview",
    "openai/gpt-6.1-sol",
    "openai/gpt-6-astra",
    "google/gemini-4-argon",
    "deepseek-ai/deepseek-v4-flash-vision-exp",
]


def media_url() -> str:
    data = json.loads(TRANSCRIPT.read_text(encoding="utf-8"))
    for step in data.get("steps", []):
        if step.get("step") == "media":
            urls = step.get("payload", {}).get("media_urls") or []
            if urls:
                return urls[0]
    return ""


def main() -> int:
    raw = HERO.read_bytes()
    uri = "data:image/png;base64," + base64.b64encode(raw).decode()
    url = media_url()
    print(f"asset: {len(raw):,} bytes")
    print(f"url:   {url[:90]}")
    print()

    settings = Settings()
    client = HttpClient(timeout=120, max_retries=0)
    llm = GmiLLMClient(settings.llm_base_url, client)
    question = (
        "Describe this image in one sentence. What is the dominant colour, "
        "and what shape does the main subject form?"
    )

    for model_id in MODELS:
        for label, reference in (("URL", url), ("dataURI", uri)):
            if not reference:
                continue
            message = build_user_message(question, [reference])
            try:
                response = llm.chat(model_id, [message], max_tokens=100)
                print(f"[{model_id}] {label}: {response.text.strip()[:160]!r}")
            except Exception as exc:
                print(f"[{model_id}] {label}: ERR {type(exc).__name__} {str(exc)[:90]}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
