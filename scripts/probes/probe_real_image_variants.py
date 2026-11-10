#!/usr/bin/env python3
"""Which delivery form of a real image does a model actually look at?

A model that passed the capability probe may still not see a full-size
asset. This script varies one thing at a time — the encoding, the size, and
whether the image travels as a data URI or as a URL — and reports the
prompt-token count alongside the answer, because the token count shows
whether the image reached the model at all.

    GMI_API_KEY=... python scripts/probe_real_image_variants.py [model-id]
"""

from __future__ import annotations

import base64
import io
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


def main() -> int:
    from PIL import Image

    model_id = sys.argv[1] if len(sys.argv) > 1 else "openai/gpt-5.5"
    raw = HERO.read_bytes()
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    print(f"model: {model_id}\nasset: {image.size} PNG, {len(raw):,} bytes\n")

    variants: list[tuple[str, str]] = []

    def add(label: str, data: bytes, mime: str) -> None:
        variants.append(
            (label, f"data:{mime};base64," + base64.b64encode(data).decode())
        )

    def png(size: int) -> bytes:
        buffer = io.BytesIO()
        image.resize((size, size), Image.LANCZOS).save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()

    def jpeg(size: int, quality: int = 85) -> bytes:
        buffer = io.BytesIO()
        image.resize((size, size), Image.LANCZOS).save(buffer, format="JPEG", quality=quality)
        return buffer.getvalue()

    add("PNG 1024", png(1024), "image/png")
    add("PNG 512", png(512), "image/png")
    add("PNG 256", png(256), "image/png")
    add("PNG 128", png(128), "image/png")
    add("JPEG 512 q85", jpeg(512), "image/jpeg")
    add("JPEG 256 q85", jpeg(256), "image/jpeg")

    settings = Settings()
    client = HttpClient(timeout=120, max_retries=0)
    llm = GmiLLMClient(settings.llm_base_url, client)

    question = "Describe this image in one sentence. What is the dominant colour?"

    for label, reference in variants:
        message = build_user_message(question, [reference])
        try:
            response = llm.chat(model_id, [message], max_tokens=80)
            tokens = response.usage.prompt_tokens
            print(f"[{label}]  {len(reference):,} chars, {tokens} prompt tokens")
            print(f"   -> {response.text.strip()[:220]}")
        except Exception as exc:
            print(f"[{label}]  ERROR {type(exc).__name__}: {str(exc)[:200]}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
