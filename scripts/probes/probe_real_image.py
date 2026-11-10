#!/usr/bin/env python3
"""Do the models that passed the capability probe actually see a real image?

The probe shows a small, synthetic, high-contrast drawing. A real generated
asset is a photograph-like image with gradients and fine detail. This script
sends the live demo's real output to every model that passed the probe and
prints what each one says, so the answers can be compared against the asset
that was actually produced.

    GMI_API_KEY=... python scripts/probe_real_image.py [model-id ...]
"""

from __future__ import annotations

import base64
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

#: The models that passed the number probe on the live catalogue.
DEFAULT_MODELS = [
    "google/gemini-3.5-flash-lite",
    "openai/gpt-5.5",
    "google/gemini-3.1-flash-lite-preview",
    "openai/gpt-6.1-sol",
    "openai/gpt-6-astra",
]


def main() -> int:
    if not HERO.exists():
        print(f"No hero asset at {HERO}. Run the live demo first.")
        return 1

    models = sys.argv[1:] or DEFAULT_MODELS
    raw = HERO.read_bytes()
    uri = "data:image/png;base64," + base64.b64encode(raw).decode()
    print(f"asset: {len(raw):,} bytes at {HERO}\n")

    settings = Settings()
    client = HttpClient(timeout=120, max_retries=0)
    llm = GmiLLMClient(settings.llm_base_url, client)

    question = (
        "Describe this image in one sentence. What is the dominant colour, "
        "and what shape does the main subject form?"
    )
    for model_id in models:
        message = build_user_message(question, [uri])
        try:
            response = llm.chat(model_id, [message], max_tokens=120)
            print(f"[{model_id}]")
            print(f"   -> {response.text.strip()[:300]}")
            print(
                f"   usage: {response.usage.prompt_tokens} in / "
                f"{response.usage.completion_tokens} out"
            )
        except Exception as exc:
            print(f"[{model_id}]")
            print(f"   -> ERROR {type(exc).__name__}: {str(exc)[:300]}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
