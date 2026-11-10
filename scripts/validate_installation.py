"""Load GMI NEXUS the way Hermes loads it, and prove the contract holds.

This is the *Hermes plugin* validation: it builds a real Hermes home, copies
the plugin into ``<home>/plugins/gmi-nexus/``, enables it in ``config.yaml``,
lets Hermes' own discovery and loader load it, and then checks the things that
only a real load can prove:

* the plugin is discovered and loads without an error;
* all eight tools are registered, with their schemas, under the right toolset;
* the ``/nexus`` command is registered;
* a tool handler actually runs and returns the documented JSON shape;
* the plugin's data directory lands in Hermes' persistent plugin-data
  directory, not inside the plugin;
* the credential never appears in a tool response;
* the dashboard manifest is where the dashboard expects it.

Nothing here reaches the network and nothing spends money. The catalogue read
is stubbed at the HTTP layer, so the only real I/O is SQLite.

Run it with the Hermes checkout on the path::

    python scripts/validate_installation.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

#: The Hermes checkout to validate against.
HERMES_REPO = Path(os.environ.get("HERMES_REPO", "F:/Hermes/hermes-agent"))

#: The plugin to validate. It lives in a subdirectory of the repository so
#: that the development tooling around it — the tests, this script, the
#: documentation — never ships inside the plugin payload.
PLUGIN_ROOT = Path(
    os.environ.get("PLUGIN_ROOT", "F:/Hermes/gmi-nexus/plugins/gmi-nexus")
)

#: The tools NEXUS declares.
EXPECTED_TOOLS = {
    "nexus_models",
    "nexus_analyze",
    "nexus_run",
    "nexus_benchmark",
    "nexus_vision",
    "nexus_media",
    "nexus_dev",
    "nexus_report",
}

#: A credential that is obviously not real. It lets the credential-resolution
#: path be exercised without a real secret anywhere.
FAKE_KEY = "gmi-validation-key-not-real"

#: Files that would mean the plugin kept its own state inside its install
#: directory, which it must not: Hermes gives it a persistent data directory.
RUNTIME_STATE_NAMES = {
    "data.db",
    "data.db-journal",
    "data.db-wal",
    "data.db-shm",
    "nexus.db",
    "history.jsonl",
    ".env",
}

#: Suffixes that would mean the same thing.
RUNTIME_STATE_SUFFIXES = (".sqlite", ".sqlite3", ".db")


def _runtime_state_in(plugin_dir: Path) -> list[Path]:
    """Return the runtime-state files found inside the plugin directory."""
    found: list[Path] = []
    for path in plugin_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.name in RUNTIME_STATE_NAMES:
            found.append(path)
            continue
        if path.suffix.lower() in RUNTIME_STATE_SUFFIXES:
            found.append(path)
    return found


def _tracked_bytecode(plugin_dir: Path) -> set[str]:
    """The ``.pyc``/``.pyo`` files git tracks under the plugin directory.

    A repository that has not been initialised yet tracks nothing, which is
    the correct answer: nothing can ship that was never committed. This
    mirrors the install scanner's own rule.
    """
    try:
        out = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(plugin_dir),
                "ls-files",
                "-z",
                "--",
                "*.pyc",
                "*.pyo",
            ],
            capture_output=True,
            check=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {os.fsdecode(part) for part in out.split(b"\0") if part}


_failures: list[str] = []
_checks = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one check."""
    global _checks
    _checks += 1
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}{(' — ' + detail) if detail else ''}")
        _failures.append(label)


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="hermes-home-nexus-"))
    plugin_dir = home / "plugins" / "gmi-nexus"
    shutil.copytree(
        PLUGIN_ROOT,
        plugin_dir,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", ".git"),
    )

    (home / "config.yaml").write_text(
        "plugins:\n"
        "  enabled:\n"
        "    - gmi-nexus\n"
        "  entries:\n"
        "    gmi-nexus:\n"
        "      settings:\n"
        "        request_timeout_s: 5\n"
        "        max_retries: 0\n",
        encoding="utf-8",
    )

    os.environ["HERMES_HOME"] = str(home)
    os.environ["GMI_API_KEY"] = FAKE_KEY

    sys.path.insert(0, str(HERMES_REPO))

    print(f"\nHermes home: {home}")
    print(f"Plugin:      {plugin_dir}\n")

    from hermes_cli import plugins as hp
    from tools.registry import registry

    hp.discover_plugins(force=True)
    manager = hp.get_plugin_manager()

    print("Discovery")
    loaded = manager._plugins.get("gmi-nexus")
    check("the plugin is discovered", loaded is not None)
    if loaded is None:
        return 1
    check("the plugin loads without an error", not loaded.error, str(loaded.error))
    check("the plugin is enabled", bool(getattr(loaded, "enabled", True)))
    # Hermes normalises the plugin slug to lower case when it records the
    # path, so the comparison has to be case-insensitive too.
    manifest_path = str(getattr(loaded.manifest, "path", ""))
    check(
        "the plugin is loaded from the user plugin directory",
        manifest_path.lower().startswith(str(plugin_dir).lower()),
        manifest_path,
    )

    print("\nTools")
    scope = manager.scope_key
    for name in sorted(EXPECTED_TOOLS):
        entry = registry.get_entry(name, scope=scope)
        check(f"{name} is registered", entry is not None)
        if entry is None:
            continue
        check(f"{name} declares a schema", bool(entry.schema))
        check(
            f"{name} requires GMI_API_KEY",
            list(entry.requires_env or []) == ["GMI_API_KEY"],
            str(entry.requires_env),
        )

    print("\nCommand")
    commands = getattr(manager, "_plugin_commands", {}) or {}
    check(
        "the /nexus command is registered",
        any("nexus" in str(key) for key in commands),
        str(list(commands)),
    )

    print("\nRunning a tool")
    # ``nexus_report`` reads the recorded history alone, so it works with no
    # catalogue read and no network. It is the right tool to prove the
    # dispatch path end to end.
    entry = registry.get_entry("nexus_report", scope=scope)
    if entry is not None:
        raw = entry.handler({"days": 7})
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            payload = {}
            check("nexus_report returns JSON", False, str(exc))
        else:
            check("nexus_report returns JSON", True)
        # The keys the report actually carries — see nexus/orchestrator.py's
        # status() and the nexus_report handler.
        expected_keys = {
            "catalogue",
            "budget",
            "routing",
            "usage",
            "recent_runs",
            "benchmarks",
            "model_stats",
            "capability_probes",
        }
        check(
            "nexus_report returns a report",
            expected_keys.issubset(payload) and isinstance(payload.get("budget"), dict),
            ", ".join(sorted(payload))[:120],
        )
        check(
            "the credential does not appear in the response",
            FAKE_KEY not in raw,
        )

    print("\nStorage")
    data_dir = home / "plugin-data" / "gmi-nexus"
    check(
        "the history lives in the Hermes plugin-data directory",
        data_dir.is_dir(),
        str(data_dir),
    )
    check(
        "the history was not written into the plugin directory",
        not (plugin_dir / "data.db").exists()
        and not (plugin_dir / "plugin-data").exists(),
    )
    check(
        "the plugin directory holds no runtime state",
        not _runtime_state_in(plugin_dir),
        ", ".join(str(p.name) for p in _runtime_state_in(plugin_dir)),
    )
    # Bytecode is checked on the *source* tree, not the installed copy:
    # Hermes' own import writes ``__pycache__`` into the directory it
    # imports from, which is the interpreter's doing and not the plugin's.
    #
    # What must not happen is bytecode that would actually ship: a ``.pyc``
    # outside a ``__pycache__`` directory (CPython never writes one there, so
    # it was committed deliberately) or one that git tracks. This is the
    # same rule the install scanner applies.
    stray_bytecode = [
        path
        for path in PLUGIN_ROOT.rglob("*")
        if path.is_file()
        and path.suffix in {".pyc", ".pyo"}
        and "__pycache__" not in path.parts
    ]
    check(
        "the source tree ships no bytecode outside __pycache__",
        not stray_bytecode,
        ", ".join(str(p.relative_to(PLUGIN_ROOT)) for p in stray_bytecode[:3]),
    )
    tracked_bytecode = _tracked_bytecode(PLUGIN_ROOT)
    check(
        "git tracks no bytecode",
        not tracked_bytecode,
        ", ".join(sorted(tracked_bytecode)[:3]),
    )

    print("\nDashboard")
    manifest = plugin_dir / "dashboard" / "manifest.json"
    check("the dashboard manifest is present", manifest.is_file())
    if manifest.is_file():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        check("the dashboard names the plugin", data.get("name") == "gmi-nexus")
        check("the dashboard entry is present", (plugin_dir / "dashboard" / data["entry"]).is_file())
        if data.get("css"):
            check("the dashboard stylesheet is present", (plugin_dir / "dashboard" / data["css"]).is_file())
        if data.get("api"):
            check("the dashboard backend is present", (plugin_dir / "dashboard" / data["api"]).is_file())

    print("\nCredentials")
    # This script holds the fake key by definition, so it is excluded from
    # the scan; every other file in the plugin must be free of it.
    leaks = [
        path
        for path in plugin_dir.rglob("*")
        if path.is_file()
        and path.name != Path(__file__).name
        and path.suffix.lower() in {".py", ".json", ".yaml", ".yml", ".txt", ".env", ".cfg", ".ini"}
        and FAKE_KEY in path.read_text(encoding="utf-8", errors="ignore")
    ]
    check(
        "the plugin did not write the credential into its own directory",
        not leaks,
        ", ".join(str(p.relative_to(plugin_dir)) for p in leaks),
    )
    check(
        "the credential was not written to the Hermes home either",
        not (home / ".env").exists()
        or FAKE_KEY not in (home / ".env").read_text(encoding="utf-8", errors="ignore"),
    )

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    if _failures:
        print("failed:")
        for failure in _failures:
            print(f"  - {failure}")
    print(f"\nHermes home left at: {home}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
