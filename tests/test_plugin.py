"""The plugin surface: what it registers, and what it refuses to leak."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FakeTransport

#: The tools the manifest declares, and the tools the code registers.
EXPECTED_TOOLS = (
    "nexus_models",
    "nexus_analyze",
    "nexus_run",
    "nexus_benchmark",
    "nexus_vision",
    "nexus_media",
    "nexus_dev",
    "nexus_report",
)

MANIFEST = Path(__file__).resolve().parents[1] / "plugins" / "gmi-nexus" / "plugin.yaml"


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_every_tool_is_registered(plugin_module, ctx):
    plugin_module.register(ctx)

    assert set(ctx.tools) == set(EXPECTED_TOOLS)
    assert "nexus" in ctx.commands


def test_the_registered_tools_match_the_manifest(plugin_module, ctx):
    plugin_module.register(ctx)

    declared = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- nexus_"):
            declared.append(stripped[2:].strip())
    assert sorted(declared) == sorted(ctx.tools)


def test_every_tool_carries_the_hermes_schema_shape(plugin_module, ctx):
    plugin_module.register(ctx)

    for name, entry in ctx.tools.items():
        schema = entry["schema"]
        assert schema["name"] == name
        assert schema["description"]
        parameters = schema["parameters"]
        assert parameters["type"] == "object"
        assert isinstance(parameters["properties"], dict)
        assert isinstance(parameters["required"], list)
        for required in parameters["required"]:
            assert required in parameters["properties"], f"{name}: {required} is not declared"


def test_the_tools_declare_the_credential_they_need(plugin_module, ctx):
    plugin_module.register(ctx)

    for name, entry in ctx.tools.items():
        assert entry["requires_env"] == ["GMI_API_KEY"], name


def test_a_tool_without_a_credential_is_hidden_from_the_model(plugin_module, ctx, monkeypatch):
    monkeypatch.delenv("GMI_API_KEY", raising=False)
    plugin_module.register(ctx)

    # Every tool but the report needs the credential to do anything.
    assert ctx.tools["nexus_run"]["check_fn"] is not None
    assert ctx.tools["nexus_report"]["check_fn"] is None


def test_the_tools_are_offered_when_the_credential_is_present(plugin_module, ctx):
    plugin_module.register(ctx)

    for name, entry in ctx.tools.items():
        if entry["check_fn"] is None:
            continue
        assert entry["check_fn"]() is True, name


def test_the_tools_do_not_collide_with_a_hermes_builtin(plugin_module, ctx):
    plugin_module.register(ctx)

    builtins = {
        "write_file", "read_file", "terminal", "vision_analyze", "computer_use",
        "execute_code", "image_generate", "video_generate", "text_to_speech",
        "browser_exec", "delegate_task",
    }
    assert not (set(ctx.tools) & builtins)


# --------------------------------------------------------------------------- #
# The tool handlers
# --------------------------------------------------------------------------- #


@pytest.fixture
def toolbox(settings, transport, ctx):
    """A tool box whose engine talks to the mock transport."""
    from nexus.http import HttpClient
    from nexus.orchestrator import Nexus
    from nexus.tools import NexusToolBox

    engine = Nexus(settings, ctx=ctx)
    engine.http.close()
    client = HttpClient(transport=transport, max_retries=0, timeout=5.0)
    engine.http = client
    engine.registry.client = client
    engine.llm.client = client
    engine.media.client = client
    engine.media.catalog.client = client

    box = NexusToolBox(ctx)
    box._engine = engine
    return box


def test_every_handler_returns_a_json_string(toolbox):
    results = [
        toolbox.nexus_models({}),
        toolbox.nexus_analyze({"goal": "Summarise the quarterly report"}),
        toolbox.nexus_run({"goal": "Summarise the quarterly report"}),
        toolbox.nexus_report({}),
    ]
    for raw in results:
        assert isinstance(raw, str)
        json.loads(raw)


def test_a_missing_required_argument_is_a_readable_error(toolbox):
    payload = json.loads(toolbox.nexus_analyze({}))
    assert "error" in payload
    assert "goal" in payload["error"]


def test_an_unknown_modality_is_refused_with_the_valid_ones(toolbox):
    payload = json.loads(toolbox.nexus_media({"prompt": "a cat", "modality": "hologram"}))
    assert "error" in payload
    assert "image" in payload["error"]


def test_an_unknown_capability_is_refused_with_the_valid_ones(toolbox):
    payload = json.loads(toolbox.nexus_models({"capability": "telepathy"}))
    assert "error" in payload
    assert "vision" in payload["error"]


def test_a_model_that_is_not_in_the_catalogue_is_reported(toolbox):
    payload = json.loads(toolbox.nexus_benchmark({"models": ["acme/does-not-exist"]}))
    assert "error" in payload
    assert "acme/does-not-exist" in payload["error"]


def test_the_report_says_when_there_is_no_history(toolbox):
    payload = json.loads(toolbox.nexus_report({}))
    assert payload["recent_runs"] == []
    assert "recent_runs_note" in payload


def test_no_tool_output_ever_contains_the_credential(toolbox):
    outputs = [
        toolbox.nexus_models({}),
        toolbox.nexus_analyze({"goal": "Summarise the quarterly report"}),
        toolbox.nexus_run({"goal": "Summarise the quarterly report"}),
        toolbox.nexus_report({}),
    ]
    for raw in outputs:
        assert "test-key-not-real" not in raw


def test_the_settings_never_contain_the_credential(toolbox):
    payload = json.loads(toolbox.nexus_report({}))
    assert "test-key-not-real" not in json.dumps(payload)


# --------------------------------------------------------------------------- #
# The slash command
# --------------------------------------------------------------------------- #


def test_the_slash_command_returns_readable_text(plugin_module, ctx):
    plugin_module.register(ctx)

    output = ctx.commands["nexus"]["handler"]("status")
    assert isinstance(output, str)
    assert output


def test_the_slash_command_accepts_a_models_argument(plugin_module, ctx):
    plugin_module.register(ctx)

    output = ctx.commands["nexus"]["handler"]("models")
    assert isinstance(output, str)


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #


def test_the_key_is_read_from_the_environment_when_the_secret_scope_is_absent(monkeypatch):
    from nexus.config import read_api_key

    monkeypatch.setenv("GMI_API_KEY", "  env-key  ")
    assert read_api_key() == "env-key"


def test_the_environment_is_never_read_when_hermes_is_present(monkeypatch):
    """Catalog rule 11: inside Hermes the secret scope is the only authority.

    Hermes' own ``get_secret`` refuses to fall back to ``os.environ`` when
    profiles are multiplexed, precisely because that would hand a plugin
    another profile's key. The plugin must not undo that guard by reading
    the environment itself.
    """
    from nexus import config

    monkeypatch.setenv("GMI_API_KEY", "another-profiles-key")
    monkeypatch.setattr(config, "_hermes_secret_reader", lambda: (lambda name: None))
    assert config.read_api_key() is None


def test_a_secret_scope_that_cannot_answer_does_not_fall_back_to_the_environment(monkeypatch):
    """Hermes raises when profiles are multiplexed with no scope bound.

    That raise is a refusal to read the environment on the caller's behalf.
    The plugin must honour it instead of catching it and reading the
    environment itself, which is the leak the refusal exists to prevent.
    """
    from nexus import config

    def refuse(name):
        raise RuntimeError("no secret scope bound while multiplexing is on")

    monkeypatch.setenv("GMI_API_KEY", "another-profiles-key")
    monkeypatch.setattr(config, "_hermes_secret_reader", lambda: refuse)
    assert config.read_api_key() is None


def test_no_key_means_no_key_not_an_empty_string(monkeypatch):
    from nexus.config import read_api_key

    monkeypatch.delenv("GMI_API_KEY", raising=False)
    assert read_api_key() is None


def test_a_missing_key_is_an_actionable_error(monkeypatch):
    from nexus.config import require_api_key
    from nexus.errors import NotConfiguredError

    monkeypatch.delenv("GMI_API_KEY", raising=False)
    with pytest.raises(NotConfiguredError) as excinfo:
        require_api_key()
    assert "GMI_API_KEY" in str(excinfo.value)


def test_the_settings_never_carry_the_key():
    from nexus.config import Settings

    settings = Settings()
    assert "GMI_API_KEY" not in settings.to_dict()
    assert "api_key" not in settings.to_dict()


def test_the_plugin_never_writes_the_key_into_the_plugin_directory(monkeypatch):
    """The credential must not be persisted next to the code."""
    monkeypatch.setenv("GMI_API_KEY", "test-key-not-real")
    root = Path(__file__).resolve().parents[1] / "plugins" / "gmi-nexus"

    # The plugin's own code and manifest, not the test fixtures that
    # deliberately contain a placeholder.
    sources = [root / "__init__.py", root / "plugin.yaml"]
    sources += sorted((root / "nexus").rglob("*.py"))
    sources += sorted((root / "dashboard").rglob("*.py"))
    sources += sorted((root / "dashboard").rglob("*.json"))

    for path in sources:
        if not path.is_file():
            continue
        assert "test-key-not-real" not in path.read_text(encoding="utf-8", errors="ignore")

