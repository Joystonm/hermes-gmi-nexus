#!/usr/bin/env python3
"""What does the Dev Agent's model actually reply with?

The live demo failed both of its implement phases with "the model did not
return a usable file list" on moonshotai/kimi-k2.7-code. The mocked tests
cannot find this, because they mock the model. This script sends the real
implement prompt to the real model and prints the raw reply, so the failure
can be seen rather than guessed at.

    GMI_API_KEY=... python scripts/probe_dev_reply.py [model-id]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "plugins" / "gmi-nexus"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from nexus.config import Settings  # noqa: E402
from nexus.http import HttpClient  # noqa: E402
from nexus.modules.coding import IMPLEMENT_PROMPT, _extract_files, _extract_json  # noqa: E402
from nexus.providers.gmi_llm import GmiLLMClient  # noqa: E402
from nexus.registry import ModelRegistry  # noqa: E402

DEFAULT_MODEL = "moonshotai/kimi-k2.7-code"

TASK = (
    "Create a single self-contained file 'landing/index.html' that is the "
    "landing page for an AI startup called Northbeam. It must have a hero "
    "section with a headline and a call to action, a features section with "
    "three cards, and a footer. Use only inline CSS and no external assets."
)


def main() -> int:
    model_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MODEL
    settings = Settings()
    client = HttpClient(timeout=180, max_retries=0)
    registry = ModelRegistry(settings, client)
    registry.refresh(force=True)

    llm = GmiLLMClient(settings.llm_base_url, client)
    prompt = IMPLEMENT_PROMPT.format(task=TASK, context="")

    response = llm.chat(model_id, [{"role": "user", "content": prompt}])
    text = response.text or ""

    print(f"=== model: {model_id}")
    print(f"=== reply length: {len(text)} chars")
    print(f"=== usage: {json.dumps(response.usage.to_dict())}")
    print()

    raw_path = REPO_ROOT / "demo_output" / "dev_reply_raw.txt"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(text, encoding="utf-8")
    print(f"=== raw reply written to {raw_path}")
    print()

    print("=== what the parser makes of it ===")
    payload = _extract_json(text)
    print(f"_extract_json -> {type(payload).__name__}")
    if payload is not None:
        print(f"  keys: {sorted(payload.keys())}")
        files = payload.get("files")
        print(f"  files is a list: {isinstance(files, list)}")
        if isinstance(files, list):
            for entry in files:
                if isinstance(entry, dict):
                    print(f"    path={entry.get('path')!r} "
                          f"content_len={len(str(entry.get('content') or ''))}")
                else:
                    print(f"    not a dict: {type(entry).__name__}")
    print(f"_extract_files -> {_extract_files(text) is not None}")

    # Where the braces are, and what the slice the parser takes looks like.
    start = text.find("{")
    end = text.rfind("}")
    print()
    print(f"=== first '{{' at {start}, last '}}' at {end}, len {len(text)}")
    if start != -1 and end > start:
        slice_ = text[start : end + 1]
        print(f"=== slice length: {len(slice_)}")
        try:
            json.loads(slice_)
            print("=== json.loads(slice) -> OK")
        except ValueError as exc:
            print(f"=== json.loads(slice) -> ValueError: {exc}")
            # Show the offending neighbourhood.
            pos = getattr(exc, "pos", None)
            if pos is not None:
                lo = max(0, pos - 200)
                print(f"--- around position {pos} ---")
                print(repr(slice_[lo : pos + 200]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
