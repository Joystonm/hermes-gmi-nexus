#!/usr/bin/env python3
"""Find the largest image GMI's vision models will actually look at.

The capability probe sends a 32x32 square and every candidate model passes
it — but a full-size photograph comes back as "I can't see an image
attached", with a prompt-token count that shows the image was dropped
before it was ever tokenised. This script varies the image's size and
encoding to find the boundary.

Pillow is used here because this is a development script; the plugin itself
imports nothing outside the standard library.

    python scripts/probe_image_limit.py [model-id]
"""

from __future__ import annotations

import base64
import io
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
from nexus.providers.gmi_llm import GmiLLMClient, build_user_message  # noqa: E402

DEMO_TRANSCRIPT = REPO_ROOT / "demo_output" / "last_run.json"


def source_image() -> bytes:
    data = json.loads(DEMO_TRANSCRIPT.read_text(encoding="utf-8"))
    for step in data.get("steps", []):
        if step.get("step") == "media":
            urls = step.get("payload", {}).get("media_urls") or []
            if urls:
                request = urllib.request.Request(
                    urls[0], headers={"User-Agent": "GMI-NEXUS/1.0 (+Hermes Agent plugin)"}
                )
                with urllib.request.urlopen(request, timeout=60) as response:
                    return response.read()
    raise SystemExit("No live image available. Run the live demo first.")


def main() -> int:
    from PIL import Image

    model_id = sys.argv[1] if len(sys.argv) > 1 else "openai/gpt-6-luna"
    raw = source_image()
    image = Image.open(io.BytesIO(raw)).convert("RGB")

    variants: list[tuple[str, str, int]] = []

    def add(label: str, data: bytes, mime: str) -> None:
        variants.append((label, f"data:{mime};base64," + base64.b64encode(data).decode(), len(data)))

    for size in (1024, 512, 256, 128):
        buffer = io.BytesIO()
        image.resize((size, size), Image.LANCZOS).save(buffer, format="PNG", optimize=True)
        add(f"PNG {size}x{size}", buffer.getvalue(), "image/png")

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    add("JPEG 1024x1024 q85", buffer.getvalue(), "image/jpeg")

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=45)
    add("JPEG 1024x1024 q45", buffer.getvalue(), "image/jpeg")

    print(f"model: {model_id}")
    print(f"source: {image.size} PNG, {len(raw)} bytes")
    print()

    settings = Settings()
    client = HttpClient(timeout=120, max_retries=0)
    llm = GmiLLMClient(settings.llm_base_url, client)

    for label, uri, nbytes in variants:
        message = build_user_message(
            "Describe this image in one sentence. What colour dominates?", [uri]
        )
        try:
            response = llm.chat(model_id, [message], max_tokens=90)
            print(f"[{label}]  {nbytes:,} bytes")
            print(f"   -> {response.text.strip()[:170]}")
            print(f"   usage: {response.usage.prompt_tokens} prompt tokens")
        except Exception as exc:
            print(f"[{label}]  {nbytes:,} bytes")
            print(f"   -> ERROR {type(exc).__name__}: {str(exc)[:170]}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
