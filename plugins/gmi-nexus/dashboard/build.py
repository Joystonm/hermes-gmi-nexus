#!/usr/bin/env python3
"""Build the GMI NEXUS dashboard bundle.

There is no bundler here, and pretending otherwise would be a lie: the tab
is a plain IIFE that the dashboard loads as ``dist/index.js`` and a plain
stylesheet it loads as ``dist/style.css``. The host exposes React and its
utilities on ``window.__HERMES_PLUGIN_SDK__`` and the tab draws with them,
so there is nothing to compile and nothing to transpile.

What this script does is therefore honest and small:

* it copies ``src/index.js`` and ``src/style.css`` into ``dist/``, which is
  where ``manifest.json`` says the dashboard will look for them;
* it checks that the files the manifest names actually exist afterwards, so
  a manifest pointing at a file that was never built is caught here rather
  than by a blank tab in the dashboard;
* it can check the other direction too, with ``--check``: that ``dist/`` is
  identical to ``src/``. That is what a CI job runs, and it is what keeps
  the committed bundle honest.

Run it with::

    python dashboard/build.py           # build
    python dashboard/build.py --check   # fail if dist/ is out of date

The script is standard library only and takes no arguments beyond ``--check``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

#: The dashboard folder, wherever this script is run from.
ROOT = Path(__file__).resolve().parent

#: Where the sources live.
SRC = ROOT / "src"

#: Where the dashboard loads the bundle from.
DIST = ROOT / "dist"

#: The files that make up the tab.
FILES = ("index.js", "style.css")


def digest(path: Path) -> str:
    """Return the SHA-256 of a file, for the comparison in ``--check``."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest() -> dict:
    """Read the dashboard manifest."""
    return json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))


def build() -> int:
    """Copy the sources into ``dist/`` and check the manifest agrees."""
    DIST.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        source = SRC / name
        if not source.is_file():
            print(f"missing source: {source}", file=sys.stderr)
            return 1
        target = DIST / name
        target.write_bytes(source.read_bytes())
        print(f"built {target.relative_to(ROOT)} from {source.relative_to(ROOT)}")

    spec = manifest()
    for key in ("entry", "css"):
        name = str(spec.get(key) or "")
        if not name:
            print(f"the manifest has no {key}", file=sys.stderr)
            return 1
        path = ROOT / name
        if not path.is_file():
            print(
                f"the manifest points at {name}, which does not exist — the "
                "dashboard tab would not render",
                file=sys.stderr,
            )
            return 1
        print(f"manifest {key} -> {name} ({digest(path)[:12]})")

    api = str(spec.get("api") or "")
    if api and not (ROOT / api).is_file():
        print(f"the manifest points at {api}, which does not exist", file=sys.stderr)
        return 1
    return 0


def check() -> int:
    """Fail when ``dist/`` is not what ``src/`` would build."""
    problems: list[str] = []
    for name in FILES:
        source = SRC / name
        target = DIST / name
        if not source.is_file():
            problems.append(f"missing source: {source}")
            continue
        if not target.is_file():
            problems.append(f"missing build output: {target}")
            continue
        if digest(source) != digest(target):
            problems.append(
                f"{target.relative_to(ROOT)} is out of date — run "
                "'python dashboard/build.py'"
            )
    spec = manifest()
    for key in ("entry", "css"):
        name = str(spec.get(key) or "")
        if name and not (ROOT / name).is_file():
            problems.append(f"the manifest points at {name}, which does not exist")
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print("the dashboard bundle is up to date")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="only check that dist/ matches src/, and fail when it does not",
    )
    args = parser.parse_args(argv)
    return check() if args.check else build()


if __name__ == "__main__":
    raise SystemExit(main())
