#!/usr/bin/env python3
"""Does image *size* break a model's vision?

A model that reads the small probe image correctly may still refuse a large
one. This script shows the same drawn number at several sizes and asks for
the number each time, so the only thing that changes is the size of the
image the model is shown.

    GMI_API_KEY=... python scripts/probe_probe_size.py [model-id]
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

sys.path.insert(0, str(REPO_ROOT / "plugins" / "gmi-nexus"))
from nexus.modules import vision as vision_module  # noqa: E402


def scaled_number(number: str, scale: int) -> str:
    """Render ``number`` with the probe font at a given dot scale."""
    original = vision_module.PROBE_DIGIT_SCALE
    original_gap = vision_module.PROBE_DIGIT_GAP
    vision_module.PROBE_DIGIT_SCALE = scale
    vision_module.PROBE_DIGIT_GAP = scale
    try:
        return vision_module.probe_number_data_uri(number)
    finally:
        vision_module.PROBE_DIGIT_SCALE = original
        vision_module.PROBE_DIGIT_GAP = original_gap


def main() -> int:
    model_id = sys.argv[1] if len(sys.argv) > 1 else "openai/gpt-5.5"
    number = "741"
    settings = Settings()
    client = HttpClient(timeout=120, max_retries=0)
    llm = GmiLLMClient(settings.llm_base_url, client)

    print(f"model: {model_id}\nnumber: {number}\n")
    for scale in (6, 12, 24, 48, 96):
        uri = scaled_number(number, scale)
        message = build_user_message(
            "What three-digit number is shown in this image? "
            "Reply with only the three digits, and nothing else.",
            [uri],
        )
        try:
            response = llm.chat(model_id, [message], max_tokens=16, temperature=0.0)
            print(
                f"[scale {scale:>3}]  {len(uri):>8,} chars, "
                f"{response.usage.prompt_tokens} prompt tokens "
                f"-> {response.text.strip()[:40]!r}"
            )
        except Exception as exc:
            print(f"[scale {scale:>3}]  ERROR {type(exc).__name__}: {str(exc)[:120]}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
