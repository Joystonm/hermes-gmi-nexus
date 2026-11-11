"""Shared fixtures.

Everything here is a mock. No test in this suite talks to GMI Cloud, and no
test spends money — the live paths are covered by the explicitly-labelled live
tests, which are skipped unless a credential and an opt-in flag are present.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "gmi-nexus"))

from nexus.config import Settings  # noqa: E402
from nexus.http import HttpResponse  # noqa: E402
from nexus.orchestrator import Nexus  # noqa: E402

#: The LLM catalogue used by the mocks. It mirrors the shape of the live
#: ``GET /v1/models`` response: string prices, a per-model discount, and one
#: model that publishes rich modality metadata.
SAMPLE_CATALOGUE: dict[str, Any] = {
    "object": "list",
    "data": [
        {
            "id": "gmicloud/Ripple-65B-Preview",
            "name": "ricursiveAI/Ripple-65B-Preview",
            "description": "A reasoning model with native tool calling.",
            "is_ready": True,
            "context_length": 1048576,
            "quantization": "bf16",
            "owned_by": "ricursiveAI",
            "created": 1792449200,
            "pricing": {
                "prompt": "0.000000200",
                "completion": "0.000000600",
                "request": "0",
                "image": "0",
            },
            "discount_to_user": 0.4,
            "schema_version": "2.4",
            "input_modalities": [
                {
                    "type": "text",
                    "pricing": [{"type": "prompt", "unit": "token", "cost_usd": "0.000000200"}],
                    "supported_inputs": {
                        "max_context_length": {"unit": "token", "value": 1048576}
                    },
                }
            ],
            "output_modalities": [
                {
                    "type": "text",
                    "max_length": {"unit": "token", "value": 131072},
                    "streaming": True,
                    "supported_parameters": {
                        "max_tokens": {"type": "integer", "min": 1, "max": 16384},
                        "temperature": {"type": "range", "min": 0, "max": 2},
                        "tools": {"type": "boolean"},
                    },
                }
            ],
        },
        {
            "id": "openai/gpt-4o-mini",
            "name": "GPT-4o Mini",
            "owned_by": "openai",
            "context_length": 128000,
            "quantization": "fp16",
            "created": 1790000000,
            "pricing": {
                "prompt": "0.000000150",
                "completion": "0.000000600",
                "request": "0",
                "image": "0",
            },
            "discount_to_user": 0.0,
        },
        {
            "id": "google/gemini-3.1-flash-lite-preview",
            "name": "Gemini 3.1 Flash Lite",
            "owned_by": "google",
            "context_length": 1048576,
            "pricing": {
                "prompt": "0.000000075",
                "completion": "0.000000300",
                "request": "0",
                "image": "0",
            },
            "discount_to_user": 0.1,
        },
        {
            "id": "deepseek-ai/deepseek-v4.1-flash-1017",
            "name": "DeepSeek-V4.1-Flash-1017",
            "owned_by": "deepseek-ai",
            "context_length": 1048575,
            "pricing": {
                "prompt": "0.000000300",
                "completion": "0.000001200",
                "request": "0",
                "image": "0",
            },
            "discount_to_user": 0.4,
        },
        {
            # A code specialist. The live catalogue publishes far more models
            # than this fixture; it needs at least one specialist so the
            # router's "prefer the specialist" behaviour can be exercised.
            "id": "acme/qwen3-coder-plus",
            "name": "Qwen3 Coder Plus",
            "description": "A code-specialised model.",
            "owned_by": "acme",
            "context_length": 262144,
            "pricing": {
                "prompt": "0.000000250",
                "completion": "0.000000950",
                "request": "0",
                "image": "0",
            },
            "discount_to_user": 0.2,
        },
    ],
}

#: The media catalogue: two product models and one entry that must be filtered
#: out because it is a private workflow rather than a product.
SAMPLE_MEDIA_IDS: list[str] = [
    "gpt-image-2",
    "seedream-4-0",
    "Minimax-Hailuo-2.3",
    "550e8400-e29b-41d4-a716-446655440000",
]

#: The published description of one image model, with its own parameter names.
SAMPLE_IMAGE_SPEC: dict[str, Any] = {
    "model": "gpt-image-2",
    "display_name": "GPT Image 2",
    "model_type": "image",
    "external_provider": "openai",
    "status": "active",
    "brief_description": "Image generation model.",
    "percentage_discount": 0,
    "parameters": [
        {"name": "prompt", "type": "string", "required": True, "description": "The prompt."},
        {
            "name": "aspect_ratio",
            "type": "enum",
            "required": False,
            "options": ["1024x1024", "1344x768", "768x1344"],
            "description": "Output size.",
        },
        {"name": "n", "type": "integer", "required": False, "default_value": 1},
        {
            "name": "image",
            "type": "string",
            "required": False,
            "description": "Optional input image as base64.",
        },
        {
            "name": "mask",
            "type": "string",
            "required": False,
            "description": "Optional mask as base64.",
        },
    ],
    "price_info": {"price": 42000, "unit": "image"},
}

#: A video model whose parameter names differ from the image model's.
SAMPLE_VIDEO_SPEC: dict[str, Any] = {
    "model": "Minimax-Hailuo-2.3",
    "display_name": "Minimax Hailuo 2.3",
    "model_type": "video",
    "external_provider": "minimax",
    "status": "active",
    "brief_description": "Video generation model.",
    "percentage_discount": 0,
    "parameters": [
        {"name": "prompt", "type": "string", "required": True},
        {"name": "durationSeconds", "type": "integer", "required": False, "default_value": 5},
        {"name": "resolution", "type": "string", "required": False, "options": ["720p", "1080p"]},
    ],
    "price_info": {"price": 56000, "unit": "second"},
}


#: The wording of the capability probe's prompt. A request carrying it is a
#: probe, and the mock answers it as a vision model would.
PROBE_PROMPT_MARKER = "three-digit number"

#: The number the probe image shows during the offline tests. The plugin
#: draws a fresh random number for every real probe — that is the whole
#: point of it — but a mock has to be able to answer it, so the tests pin it.
PROBE_NUMBER = "473"

#: The real number-drawing function, kept before the tests replace it, so a
#: test can check that the real one is genuinely random.
import nexus.modules.vision as _vision_module  # noqa: E402

ORIGINAL_RANDOM_PROBE_NUMBER = _vision_module.random_probe_number


def _is_probe_request(json_body: Any) -> bool:
    """Whether a chat request is the capability probe."""
    if not isinstance(json_body, dict):
        return False
    for message in json_body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            if PROBE_PROMPT_MARKER in content:
                return True
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and PROBE_PROMPT_MARKER in str(part.get("text") or ""):
                    return True
    return False


class FakeTransport:
    """A scripted transport that answers the endpoints NEXUS calls."""
    def __init__(
        self,
        catalogue: dict[str, Any] | None = None,
        media_ids: list[str] | None = None,
        media_specs: dict[str, dict[str, Any]] | None = None,
        chat_reply: str = "Here is the answer.",
        chat_status: int = 200,
        chat_statuses: list[int] | None = None,
        media_status: str = "success",
        probe_reply: str = PROBE_NUMBER,
        chat_finish_reason: str = "stop",
        submit_delay: float = 0.0,
        chat_replies: list[Any] | None = None,
        chat_finish_reasons: list[str] | None = None,
    ) -> None:
        self.catalogue = catalogue if catalogue is not None else SAMPLE_CATALOGUE
        self.media_ids = media_ids if media_ids is not None else list(SAMPLE_MEDIA_IDS)
        self.media_specs = media_specs or {
            "gpt-image-2": SAMPLE_IMAGE_SPEC,
            "Minimax-Hailuo-2.3": SAMPLE_VIDEO_SPEC,
        }
        self.chat_reply = chat_reply
        self.chat_status = chat_status
        #: Status codes for the first chat calls, in order. The call after
        #: the list is exhausted falls back to ``chat_status``.
        self.chat_statuses = list(chat_statuses) if chat_statuses else []
        self.media_status = media_status
        #: What this model answers when it is asked the capability probe.
        #: A model that can see reads the drawn number; one that cannot says so.
        self.probe_reply = probe_reply
        #: The finish reason the provider reports. ``"length"`` means the
        #: reply was cut off by the token limit, which is what a real code
        #: model does when the request asks for more room than it was given.
        self.chat_finish_reason = chat_finish_reason
        #: The reply for each chat call in turn. A ``None`` entry is a
        #: provider that answers with a null body, which is what a reasoning
        #: model does when it spends its whole token budget thinking and is
        #: cut off before it says anything. Calls past the end of the list
        #: fall back to ``chat_reply``.
        self.chat_replies = list(chat_replies) if chat_replies else []
        #: The finish reason for each chat call in turn, falling back to
        #: ``chat_finish_reason``.
        self.chat_finish_reasons = list(chat_finish_reasons) if chat_finish_reasons else []
        #: How long the submission call blocks for, in seconds. The real
        #: request queue holds the create call open while the request is
        #: admitted and processed, so a mock that returns instantly would
        #: hide the difference between the queue's time and the polling
        #: loop's.
        self.submit_delay = float(submit_delay)
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    # -- helpers ------------------------------------------------------------ #

    def urls(self) -> list[str]:
        return [entry["url"] for entry in self.requests]

    def bodies(self) -> list[Any]:
        return [entry["body"] for entry in self.requests]

    def chat_requests(self) -> list[dict[str, Any]]:
        return [entry for entry in self.requests if entry["url"].endswith("/chat/completions")]

    # -- the transport interface -------------------------------------------- #

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        data: Any = None,
        files: Any = None,
        timeout: float = 60.0,
        follow_redirects: bool = True,
    ) -> HttpResponse:
        chat_index = len(self.chat_requests()) if url.endswith("/chat/completions") else 0
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "body": json_body,
            }
        )

        if url.endswith("/v1/models") and method == "GET":
            return self._json(200, self.catalogue)

        if url.endswith("/apikey/models") and method == "GET":
            return self._json(200, {"model_ids": self.media_ids})

        if "/apikey/models/" in url and method == "GET":
            model_id = url.rsplit("/", 1)[-1]
            spec = self.media_specs.get(model_id)
            if spec is None:
                return self._json(404, {"error": {"message": "no such model"}})
            return self._json(200, spec)

        if url.endswith("/chat/completions"):
            status = self.chat_status
            if chat_index < len(self.chat_statuses):
                status = self.chat_statuses[chat_index]
            if status != 200:
                return self._json(status, {"error": {"message": "mock failure"}})
            reply = self.chat_reply
            if chat_index < len(self.chat_replies):
                reply = self.chat_replies[chat_index]
            finish_reason = self.chat_finish_reason
            if chat_index < len(self.chat_finish_reasons):
                finish_reason = self.chat_finish_reasons[chat_index]
            if _is_probe_request(json_body):
                # The capability probe asks for a three-digit number that was
                # drawn as pixels. A model that can see reads it back; this
                # mock answers according to ``probe_reply`` so a test can make
                # it blind — or merely wrong — on purpose.
                reply = self.probe_reply
            return self._json(
                200,
                {
                    "id": "chatcmpl-mock",
                    "model": json_body.get("model") if isinstance(json_body, dict) else "mock",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": reply},
                            "finish_reason": finish_reason,
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 120,
                        "completion_tokens": 40,
                        "total_tokens": 160,
                    },
                },
            )

        if url.endswith("/requests") and method == "POST":
            if self.submit_delay:
                time.sleep(self.submit_delay)
            return self._json(200, {"request_id": "req-mock-1", "status": "queued"})

        if "/requests/" in url and method == "GET":
            if self.media_status == "failed":
                return self._json(
                    200,
                    {
                        "request_id": "req-mock-1",
                        "status": "failed",
                        "error": {"message": "the model refused the request"},
                    },
                )
            return self._json(
                200,
                {
                    "request_id": "req-mock-1",
                    "status": self.media_status,
                    "outcome": {
                        "media_urls": ["https://cdn.gmicloud.ai/out-1.png"],
                        "thumbnail_image_url": "https://cdn.gmicloud.ai/thumb-1.png",
                    },
                },
            )

        return self._json(404, {"error": {"message": f"no mock route for {method} {url}"}})

    def close(self) -> None:
        self.closed = True

    @staticmethod
    def _json(status: int, payload: Any) -> HttpResponse:
        return HttpResponse(status=status, body=json.dumps(payload), headers={})


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    """Keep every test's history in its own database."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from nexus import storage

    storage.close_connection()
    yield tmp_path
    storage.close_connection()


@pytest.fixture(autouse=True)
def fake_credential(monkeypatch):
    """Provide a placeholder credential without touching a real one.

    The live tests are the exception. They opt in with ``GMI_LIVE_TESTS=1``
    and they need the *real* credential, so the placeholder is installed
    only when that opt-in is absent. Installing it unconditionally would
    silently replace a real key with ``test-key-not-real`` and make every
    live test fail against the real API — which is exactly what happened
    before this was fixed.
    """
    live = os.environ.get("GMI_LIVE_TESTS", "").strip().lower() in ("1", "true", "yes", "on")
    if not live:
        monkeypatch.setenv("GMI_API_KEY", "test-key-not-real")
    yield


@pytest.fixture(autouse=True)
def pinned_probe_number(monkeypatch):
    """Pin the number the probe image shows for the whole offline suite.

    A real probe draws a fresh random number each time, so a text-only
    model cannot pass by guessing. A mock cannot read the number off the
    image it was sent, so the tests fix it to one value and script the
    answer to match.
    """
    global ORIGINAL_RANDOM_PROBE_NUMBER
    import nexus.modules.vision as vision

    monkeypatch.setattr(vision, "random_probe_number", lambda: PROBE_NUMBER)
    yield


@pytest.fixture
def settings() -> Settings:
    """Settings pointed at the mock endpoints."""
    return Settings(
        llm_base_url="https://mock.gmi/v1",
        media_base_url="https://mock.gmi/apikey",
        request_timeout_s=5,
        max_retries=1,
        max_cost_per_run_usd=5.0,
        daily_budget_usd=50.0,
        approval_threshold_usd=10.0,
    )


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def engine(settings, transport) -> Nexus:
    """An engine wired to the mock transport."""
    from nexus.http import HttpClient

    client = HttpClient(transport=transport, max_retries=0, timeout=5.0)
    nexus = Nexus(settings, ctx=None)
    nexus.http.close()
    nexus.http = client
    nexus.registry.client = client
    nexus.llm.client = client
    nexus.media.client = client
    nexus.media.catalog.client = client
    return nexus


@pytest.fixture
def plugin_module():
    """The plugin's root module, loaded the way Hermes loads it."""
    import importlib.util

    root = Path(__file__).resolve().parents[1] / "plugins" / "gmi-nexus"
    spec = importlib.util.spec_from_file_location(
        "gmi_nexus_under_test", root / "__init__.py", submodule_search_locations=[str(root)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["gmi_nexus_under_test"] = module
    spec.loader.exec_module(module)
    return module


class FakeContext:
    """A stand-in for Hermes' ``PluginContext``."""

    def __init__(self) -> None:
        self.tools: dict[str, dict[str, Any]] = {}
        self.commands: dict[str, dict[str, Any]] = {}
        self.hooks: dict[str, list[Any]] = {}
        self.config: dict[str, Any] = {}
        self.dispatched: list[tuple[str, dict[str, Any]]] = []
        self.tool_responses: dict[str, Any] = {}

    def register_tool(self, name, toolset, schema, handler, check_fn=None,
                     requires_env=None, is_async=False, description="", emoji="",
                     override=False):
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "check_fn": check_fn,
            "requires_env": requires_env,
            "description": description,
            "emoji": emoji,
        }

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = {"handler": handler, "description": description}

    def register_hook(self, hook_name, callback, **kwargs):
        self.hooks.setdefault(hook_name, []).append(callback)
        return None

    def register_middleware(self, *args, **kwargs):
        return None

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def set_config(self, key, value):
        self.config[key] = value

    def dispatch_tool(self, tool_name, args, **kwargs):
        self.dispatched.append((tool_name, dict(args)))
        return self.tool_responses.get(tool_name, {"result": "ok"})

    def has_plugin(self, name):
        return False

    def has_capability(self, name):
        return False


@pytest.fixture
def ctx() -> FakeContext:
    return FakeContext()
