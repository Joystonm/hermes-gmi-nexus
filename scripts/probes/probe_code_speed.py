#!/usr/bin/env python3
"""Measure how fast the catalogue's code models actually write.

The demo video has ninety seconds to show a page being written. That budget
is spent almost entirely on one thing: how many characters a code model
returns per second. A model that answers a sentence quickly and a model that
answers a source file quickly are very different animals, and the catalogue
publishes prices and context lengths — not speed.

So this probe sends each code-capable model the same writing task, records
how long it took and how much it wrote, and prints the characters per
second. The number that decides the demo is the one at the bottom.

    export GMI_API_KEY="your-key"
    python scripts/probes/probe_code_speed.py

Each figure is an **estimate at list price**; GMI's usage block reports token
counts and no dollar amount.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "gmi-nexus"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from nexus.config import Settings  # noqa: E402
from nexus.http import HttpClient  # noqa: E402
from nexus.models import Capability  # noqa: E402
from nexus.providers.gmi_llm import GmiLLMClient  # noqa: E402
from nexus.registry import ModelRegistry  # noqa: E402

#: The task every model is given. It asks for a file of a known rough size,
#: so the seconds-per-character figure means the same thing for all of them.
TASK = (
    "Write a single self-contained HTML file with a hero section, three "
    "feature cards and a footer. Inline CSS only, no JavaScript, no external "
    "assets. Reply with the file only, no commentary. "
    "Aim for about 2000 characters."
)


def main() -> int:
    if not os.environ.get("GMI_API_KEY"):
        print("Set GMI_API_KEY first.", file=sys.stderr)
        return 2

    settings = Settings()
    registry = ModelRegistry(settings)
    registry.list_models(refresh=True)

    models = [m for m in registry.list_models() if m.supports(Capability.CODE)]
    if not models:
        print("The catalogue declares no code-capable model.", file=sys.stderr)
        return 1

    http = HttpClient(timeout=float(settings.request_timeout_s), max_retries=0)
    client = GmiLLMClient(settings.llm_base_url, http, default_max_tokens=8192)
    rows: list[tuple[float, str, int, float, str]] = []
    spent = 0.0
    try:
        for model in models:
            started = time.perf_counter()
            try:
                response = client.chat(
                    model.id,
                    [{"role": "user", "content": TASK}],
                    max_tokens=8192,
                    model_info=model,
                )
            except Exception as exc:  # a model that cannot answer is a result
                elapsed = time.perf_counter() - started
                rows.append((999.0, model.id, 0, 0.0, f"failed: {exc}"))
                print(f"  {model.id:<50} FAILED  {exc}")
                continue
            elapsed = time.perf_counter() - started
            chars = len(response.text or "")
            rate = chars / elapsed if elapsed else 0.0
            spent += float(response.usage.cost_usd)
            note = " (cut off at the token limit)" if response.finish_reason == "length" else ""
            rows.append((elapsed, model.id, chars, rate, note))
            print(
                f"  {model.id:<50} {elapsed:7.1f}s  {chars:6d} chars  "
                f"{rate:7.1f} chars/s{note}"
            )
    finally:
        http.close()

    print()
    print("Fastest first:")
    for elapsed, model_id, chars, rate, note in sorted(rows):
        if rate <= 0:
            continue
        print(
            f"  {model_id:<50} {elapsed:7.1f}s  {chars:6d} chars  "
            f"{rate:7.1f} chars/s{note}"
        )
    print()
    print(f"Estimated cost of this probe: ${spent:.8f}")
    print(
        "An estimate at list price with the model's published discount applied. "
        "GMI's usage block reports token counts and no dollar figure, so this "
        "is not a billed amount."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
