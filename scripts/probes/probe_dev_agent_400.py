#!/usr/bin/env python3
"""Why does the Dev Agent's call to the code model return 400?

The live demo failed its Dev Agent step with "Backend request failed with
status 400" on Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8. This script prints
what the catalogue publishes about that model, then tries the same prompt
with different parameter sets to find which one the endpoint rejects.

    GMI_API_KEY=... python scripts/probe_dev_agent_400.py [model-id]
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
from nexus.models import Capability  # noqa: E402
from nexus.providers.gmi_llm import GmiLLMClient  # noqa: E402
from nexus.registry import ModelRegistry  # noqa: E402

DEFAULT_MODEL = "Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8"


def main() -> int:
    model_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MODEL
    settings = Settings()
    client = HttpClient(timeout=120, max_retries=0)
    registry = ModelRegistry(settings, client)
    registry.refresh(force=True)

    info = registry.get(model_id)
    print("=== what the catalogue publishes ===")
    if info is None:
        print(f"{model_id} is not in the catalogue")
    else:
        print(json.dumps(info.to_dict(), indent=2)[:3000])

    llm = GmiLLMClient(settings.llm_base_url, client)
    prompt = "Write a Python function that adds two numbers. Reply with code only."

    variants = [
        ("no max_tokens", {"max_tokens": None}),
        ("max_tokens 2048", {"max_tokens": 2048}),
        ("max_tokens 512", {"max_tokens": 512}),
        ("max_tokens 4096", {"max_tokens": 4096}),
        ("max_tokens + temperature", {"max_tokens": 2048, "temperature": 0.2}),
    ]

    print("\n=== what the endpoint accepts ===")
    for label, kwargs in variants:
        try:
            response = llm.chat(model_id, [{"role": "user", "content": prompt}], **kwargs)
            print(f"[{label}] OK -> {response.text.strip()[:120]!r}")
        except Exception as exc:
            print(f"[{label}] {type(exc).__name__}: {str(exc)[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
