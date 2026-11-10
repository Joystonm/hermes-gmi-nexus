#!/usr/bin/env python3
"""Find out why a model can see the probe image but not the real one.

The capability probe sends a 32x32 solid square as a data URI. A model
that passes it may still be unable to handle a full-size photograph. This
script varies one thing at a time — the image, its encoding, its size —
against one model, and prints what each combination produced.

    python scripts/probe_image_delivery.py [model-id]
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "plugins" / "gmi-nexus"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from nexus.config import Settings  # noqa: E402
from nexus.http import HttpClient  # noqa: E402
from nexus.modules.vision import solid_png_data_uri  # noqa: E402
from nexus.providers.gmi_llm import GmiLLMClient, build_user_message  # noqa: E402

#: A real, non-trivial image is needed; the hero asset from the last live
#: demo run is used when it is there.
DEMO_TRANSCRIPT = REPO_ROOT / "demo_output" / "last_run.json"


def real_image_url() -> str:
    if not DEMO_TRANSCRIPT.exists():
        return ""
    data = json.loads(DEMO_TRANSCRIPT.read_text(encoding="utf-8"))
    for step in data.get("steps", []):
        if step.get("step") == "media":
            urls = step.get("payload", {}).get("media_urls") or []
            if urls:
                return urls[0]
    return ""


def fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "GMI-NEXUS/1.0 (+Hermes Agent plugin)"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def main() -> int:
    model_id = sys.argv[1] if len(sys.argv) > 1 else "openai/gpt-6-luna"
    settings = Settings()
    client = HttpClient(timeout=120, max_retries=0)
    llm = GmiLLMClient(settings.llm_base_url, client)

    url = real_image_url()
    if not url:
        print("No live image available. Run the live demo first.")
        return 1

    raw = fetch(url)
    print(f"model: {model_id}")
    print(f"image: {len(raw)} bytes at {url[:70]}...")

    cases: list[tuple[str, str]] = [
        ("probe square, 32x32, data URI", solid_png_data_uri((220, 20, 30))),
        ("real image, remote URL", url),
        ("real image, data URI", "data:image/png;base64," + base64.b64encode(raw).decode()),
    ]

    for label, reference in cases:
        message = build_user_message(
            "Describe this image in one sentence. What colour dominates?", [reference]
        )
        try:
            response = llm.chat(model_id, [message], max_tokens=120)
            print(f"\n  [{label}]")
            print(f"      -> {response.text.strip()[:240]}")
            print(f"      usage: {response.usage.prompt_tokens} in / "
                  f"{response.usage.completion_tokens} out")
        except Exception as exc:
            print(f"\n  [{label}]")
            print(f"      -> ERROR {type(exc).__name__}: {str(exc)[:240]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
