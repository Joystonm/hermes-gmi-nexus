#!/usr/bin/env python3
"""Measure how long the catalogue's image models actually take.

The demo video has ninety seconds, and the image generation is the phase that
eats them. The catalogue publishes each image model's price, its parameter
schema and whether it is active — but not how long it takes. Two runs of the
same prompt can differ by a factor of five, so the video cannot be timed from
the catalogue; it has to be measured.

This probe sends one real generation to each configuration in turn and records
the wall-clock seconds. The configurations are the ones the plugin's own
operator settings can express:

* the model the media router picks, at its published default quality;
* the same model at the lowest quality tier it publishes;
* two cheaper models asked for fewer denoising steps.

Every request is a real one and nothing is deduplicated, so the numbers are
the numbers the video will see.

    export GMI_API_KEY="your-key"
    python scripts/probes/probe_image_speed.py

Each figure is an **estimate at list price**; GMI's usage block reports token
counts and no dollar amount.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "gmi-nexus"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from nexus.config import Settings  # noqa: E402
from nexus.http import HttpClient  # noqa: E402
from nexus.media_catalog import MediaCatalog  # noqa: E402
from nexus.models import Modality  # noqa: E402
from nexus.providers.gmi_media import GmiMediaClient  # noqa: E402

#: One configuration per line: a model, the options the plugin would send it,
#: and the art direction that makes the request unique. The art direction
#: differs on purpose — the creative engine suppresses an identical earlier
#: request, and a probe that returned a cached result would measure nothing.
CONFIGURATIONS: tuple[tuple[str, dict[str, Any], str], ...] = (
    (
        "gpt-image-2.5-flare-generate",
        {"quality": "medium"},
        "A single beam of light splitting into thin parallel beams, deep blue "
        "on a pale background, one faint horizontal rule across the middle.",
    ),
    (
        "gpt-image-2.5-flare-generate",
        {"quality": "low"},
        "A single beam of light splitting into thin parallel beams, deep blue "
        "on a pale background, with a narrow vertical band down the left edge.",
    ),
    (
        "Qwen-Image-2512",
        {"steps": 8, "width": 768, "height": 768},
        "A single beam of light splitting into thin parallel beams, deep blue "
        "on a pale background, with a small circular node at the beam's origin.",
    ),
    (
        "Flux2-Klein",
        {"steps": 4, "width": 768, "height": 768},
        "A single beam of light splitting into thin parallel beams, deep blue "
        "on a pale background, with a dotted guide line under the fan.",
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--approve-spend",
        action="store_true",
        help="confirm that this probe may spend GMI Cloud credits",
    )
    parser.add_argument(
        "--home",
        default="",
        help="a Hermes home for the probe's own records (default: a temporary one)",
    )
    args = parser.parse_args()

    if not os.environ.get("GMI_API_KEY"):
        print("Set GMI_API_KEY first.", file=sys.stderr)
        return 2
    if not args.approve_spend:
        print(
            "Refusing to run without --approve-spend.\n"
            "This probe makes real image generations and spends credits.",
            file=sys.stderr,
        )
        return 2

    import tempfile

    if args.home:
        home = Path(args.home).expanduser().resolve()
        home.mkdir(parents=True, exist_ok=True)
    else:
        home = Path(tempfile.mkdtemp(prefix="nexus-probe-home-"))
    os.environ["HERMES_HOME"] = str(home)

    settings = Settings()
    http = HttpClient(timeout=float(settings.request_timeout_s), max_retries=0)
    catalog = MediaCatalog(settings.media_base_url, http)
    client = GmiMediaClient(settings.media_base_url, http, catalog=catalog)

    rows: list[tuple[float, str, str, float, str]] = []
    spent = 0.0
    try:
        for model_id, options, prompt in CONFIGURATIONS:
            label = f"{model_id} {options or '(defaults)'}"
            started = time.perf_counter()
            try:
                result = client.generate(
                    model_id,
                    prompt,
                    options,
                    modality=Modality.IMAGE,
                    timeout=240.0,
                    allow_dedupe=False,
                )
            except Exception as exc:  # a model that cannot answer is a result
                elapsed = time.perf_counter() - started
                rows.append((999.0, label, "", 0.0, f"failed: {exc}"))
                print(f"  {label:<52} FAILED  {exc}")
                continue
            elapsed = time.perf_counter() - started
            cost = float(result.estimated_cost_usd)
            spent += cost
            note = "" if result.ok else f" — {result.status}: {result.error}"
            rows.append((elapsed, label, result.status, cost, note))
            print(
                f"  {label:<52} {elapsed:7.1f}s  {result.status:<8} "
                f"~${cost:.8f}{note}"
            )
    finally:
        http.close()

    print()
    print("Fastest first:")
    for elapsed, label, status, cost, note in sorted(rows):
        print(f"  {label:<52} {elapsed:7.1f}s  {status:<8} ~${cost:.8f}{note}")
    print()
    print(f"Estimated cost of this probe: ${spent:.8f}")
    print(
        "An estimate at list price. GMI's usage block reports token counts and "
        "no dollar figure, so this is not a billed amount."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
