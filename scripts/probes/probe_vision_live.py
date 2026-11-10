#!/usr/bin/env python3
"""Run the plugin's own vision path against the real generated asset.

This is Module B as it will actually be used: the plugin picks a model,
probes it if it has not been probed, inlines the image and asks the
question. The answer is printed together with the model that produced it.

    GMI_API_KEY=... python scripts/probe_vision_live.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "plugins" / "gmi-nexus"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from nexus.budget import BudgetGuard  # noqa: E402
from nexus.config import Settings  # noqa: E402
from nexus.http import HttpClient  # noqa: E402
from nexus.modules.vision import VisionModule  # noqa: E402
from nexus.providers.gmi_llm import GmiLLMClient  # noqa: E402
from nexus.registry import ModelRegistry  # noqa: E402
from nexus.router import Router  # noqa: E402

TRANSCRIPT = REPO_ROOT / "demo_output" / "last_run.json"


def media_url() -> str:
    data = json.loads(TRANSCRIPT.read_text(encoding="utf-8"))
    for step in data.get("steps", []):
        if step.get("step") == "media":
            urls = step.get("payload", {}).get("media_urls") or []
            if urls:
                return urls[0]
    return ""


def main() -> int:
    url = media_url()
    if not url:
        print("No live image available. Run the live demo first.")
        return 1

    settings = Settings(approval_threshold_usd=1.0, daily_budget_usd=5.0)
    client = HttpClient(timeout=120, max_retries=1)
    registry = ModelRegistry(settings, client)
    registry.refresh(force=True)
    llm = GmiLLMClient(settings.llm_base_url, client)
    vision = VisionModule(settings, registry, Router(settings), llm, BudgetGuard(settings), None)

    outcome = vision.analyse(
        [url],
        "What is the dominant colour, and what shape does the main subject form?",
        approved=True,
    )
    print(f"ok:      {outcome.ok}")
    print(f"model:   {outcome.model_id}")
    print(f"cost:    ${outcome.cost_usd:.6f}")
    print(f"latency: {outcome.latency_ms} ms")
    print(f"text:    {outcome.text.strip()[:400]}")
    if outcome.error:
        print(f"error:   {outcome.error}")
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
