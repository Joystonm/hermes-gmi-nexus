#!/usr/bin/env python3
"""Is the "I can't see an image" answer deterministic or intermittent?

The size sweep produced a result that fits no size rule: a 1024x1024 JPEG
worked, a 128x128 PNG did not, and the two JPEG qualities disagreed with
each other. Either the gateway drops images by some rule not yet found, or
the answer is intermittent. This script repeats each variant several times
and reports the tally, which tells the two apart.

    python scripts/probe_image_repeat.py [model-id] [rounds]
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
from nexus.modules.vision import solid_png_data_uri  # noqa: E402
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
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    raw = source_image()
    image = Image.open(io.BytesIO(raw)).convert("RGB")

    def png(size: int) -> bytes:
        buffer = io.BytesIO()
        image.resize((size, size), Image.LANCZOS).save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()

    def jpeg(size: int, quality: int) -> bytes:
        buffer = io.BytesIO()
        image.resize((size, size), Image.LANCZOS).save(
            buffer, format="JPEG", quality=quality
        )
        return buffer.getvalue()

    def as_uri(data: bytes, mime: str) -> str:
        return f"data:{mime};base64," + base64.b64encode(data).decode()

    variants = [
        ("probe square PNG 32x32", solid_png_data_uri((220, 20, 30))),
        ("PNG 512x512", as_uri(png(512), "image/png")),
        ("JPEG 1024 q85", as_uri(jpeg(1024, 85), "image/jpeg")),
        ("JPEG 1024 q85 (again)", as_uri(jpeg(1024, 85), "image/jpeg")),
        ("JPEG 512 q85", as_uri(jpeg(512, 85), "image/jpeg")),
    ]

    settings = Settings()
    client = HttpClient(timeout=120, max_retries=0)
    llm = GmiLLMClient(settings.llm_base_url, client)

    print(f"model: {model_id}   rounds: {rounds}\n")
    for label, reference in variants:
        seen = 0
        answers: list[str] = []
        for _ in range(rounds):
            message = build_user_message(
                "Describe this image in one sentence. What colour dominates?",
                [reference],
            )
            try:
                response = llm.chat(model_id, [message], max_tokens=90)
                text = response.text.strip()
                answers.append(text[:90])
                if "can't see" not in text.lower() and "don't see" not in text.lower() \
                        and "not see an image" not in text.lower():
                    seen += 1
            except Exception as exc:
                answers.append(f"ERROR {type(exc).__name__}")
        print(f"[{label}]  saw the image {seen}/{rounds}")
        for answer in answers:
            print(f"    -> {answer}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
