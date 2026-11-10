#!/usr/bin/env python3
"""What does the raw response from the Dev Agent's model look like?

The live demo's implement phases failed with "the model did not return a
usable file list", and a direct call came back with *zero* characters of
content while the provider reported 2048 completion tokens. This script
prints the raw payload — keys, finish reason, and any field the text might
be hiding in — so the empty reply can be explained rather than guessed at.

    GMI_API_KEY=... python scripts/probe_dev_raw.py [model-id] [max_tokens]
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
from nexus.modules.coding import IMPLEMENT_PROMPT  # noqa: E402
from nexus.providers.gmi_llm import GmiLLMClient  # noqa: E402

DEFAULT_MODEL = "moonshotai/kimi-k2.7-code"

TASK = (
    "Create a single self-contained file 'landing/index.html' that is the "
    "landing page for an AI startup called Northbeam. It must have a hero "
    "section with a headline and a call to action, a features section with "
    "three cards, and a footer. Use only inline CSS and no external assets."
)


def summarise(value, depth: int = 0) -> str:
    """Describe a value's shape without dumping a whole page of text."""
    if isinstance(value, str):
        return f"str(len={len(value)})"
    if isinstance(value, list):
        return f"list(len={len(value)})"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {summarise(v, depth + 1)}" for k, v in value.items()) + "}"
    return f"{type(value).__name__}({value!r})"


def main() -> int:
    model_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MODEL
    max_tokens = int(sys.argv[2]) if len(sys.argv) > 2 else None
    settings = Settings()
    client = HttpClient(timeout=300, max_retries=0)
    llm = GmiLLMClient(settings.llm_base_url, client)
    prompt = IMPLEMENT_PROMPT.format(task=TASK, context="")

    kwargs = {}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    response = llm.chat(model_id, [{"role": "user", "content": prompt}], **kwargs)

    print(f"=== model: {model_id}")
    print(f"=== max_tokens sent: {max_tokens or 'default (2048)'}")
    print(f"=== text length: {len(response.text)}")
    print(f"=== finish_reason: {response.finish_reason!r}")
    print(f"=== usage: {json.dumps(response.usage.to_dict())}")
    print()

    raw = response.raw
    print("=== raw payload shape ===")
    print(summarise(raw))
    print()

    choices = raw.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice = choices[0]
        print("=== choices[0] keys ===")
        print(summarise(choice))
        print(f"=== choices[0].finish_reason = {choice.get('finish_reason')!r}")
        message = choice.get("message")
        if isinstance(message, dict):
            print("=== message keys ===")
            print(summarise(message))
            for key, value in message.items():
                if isinstance(value, str) and value:
                    print(f"--- message[{key!r}] first 300 chars ---")
                    print(value[:300])
                    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
