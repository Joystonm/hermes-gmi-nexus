#!/usr/bin/env python3
"""Probe every model the catalogue *declares* vision-capable.

The catalogue says a model can read an image. That is a claim, not a fact.
This script sends each declared model the plugin's own two-square probe and
reports which ones can actually see, so the gap between the claim and the
reality is measured rather than assumed.

Each probe is one short completion (32 tokens). It costs a fraction of a cent.

    GMI_API_KEY=... python scripts/probe_vision_models.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "plugins" / "gmi-nexus"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from nexus.budget import BudgetGuard  # noqa: E402
from nexus.config import Settings  # noqa: E402
from nexus.http import HttpClient  # noqa: E402
from nexus.models import Capability  # noqa: E402
from nexus.modules.vision import VisionModule  # noqa: E402
from nexus.providers.gmi_llm import GmiLLMClient  # noqa: E402
from nexus.registry import ModelRegistry  # noqa: E402
from nexus.router import Router  # noqa: E402


def main() -> int:
    settings = Settings(
        approval_threshold_usd=1.0,
        daily_budget_usd=5.0,
        max_cost_per_run_usd=1.0,
    )
    client = HttpClient(timeout=90, max_retries=0)
    registry = ModelRegistry(settings, client)
    registry.refresh(force=True)

    declared = [
        model
        for model in registry.list_models()
        if model.supports(Capability.VISION) and model.is_ready
    ]
    print(f"models the catalogue declares vision-capable: {len(declared)}")

    llm = GmiLLMClient(settings.llm_base_url, client)
    vision = VisionModule(settings, registry, Router(settings), llm, BudgetGuard(settings), None)

    verified: list[tuple[str, str]] = []
    rejected: list[tuple[str, str]] = []
    for model in declared:
        try:
            result = vision.probe(model.id)
        except Exception as exc:  # pragma: no cover - a transport fault
            rejected.append((model.id, f"EXC {type(exc).__name__}: {exc}"))
            continue
        (verified if result["verified"] else rejected).append(
            (model.id, str(result.get("evidence", "")))
        )

    print("\n=== VERIFIED — can actually see ===")
    for model_id, evidence in verified:
        print(f"  OK   {model_id}\n       {evidence}")
    print("\n=== NOT VERIFIED — declared, but cannot see ===")
    for model_id, evidence in rejected:
        print(f"  --   {model_id}\n       {evidence}")

    print(f"\n{len(verified)} of {len(declared)} declared vision models can actually see.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
