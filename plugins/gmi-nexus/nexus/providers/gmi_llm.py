"""GMI Cloud LLM client.

GMI's inference API is OpenAI-compatible: ``POST {base}/chat/completions`` with
a Bearer token, a model id, and a messages array. Vision works through the
OpenAI content-array form, where an image is a part of the message with an
``image_url`` that carries a ``data:`` URI.

Three response shapes are handled because GMI exposes three endpoint families
(``/chat/completions``, ``/messages``, ``/responses``) and the caller may pick
any of them:

* ``choices[0].message.content`` — chat completions
* ``content[0].text`` — messages
* ``output_text`` / ``output[*].content[*].text`` — responses

Token usage is read from the provider's own ``usage`` block whenever it is
present. GMI's inference endpoint reports token counts there but no dollar
figure, so the measured tokens are priced at the model's published rate and
the figure stays an *estimate at list price* — never a billed amount. When
the provider sends no usage block at all, the token counts are estimated
from the request and the result is flagged ``measured=False`` so nothing
downstream can present it as measured.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..errors import BadRequestError, ModelUnavailableError, NexusError, ValidationError
from ..http import HttpClient
from ..models import ModelInfo, Usage

#: The endpoint families NEXUS knows how to call.
ENDPOINT_CHAT = "chat/completions"
ENDPOINT_MESSAGES = "messages"
ENDPOINT_RESPONSES = "responses"

#: How a part of a multimodal message is encoded.
#:
#: ``image_url`` is the OpenAI-compatible spelling, and it is what GMI's
#: endpoint accepts. Some models reject anything else outright — asking for
#: an ``image`` part returns "Invalid value: 'image'. Supported values are:
#: 'text', 'image_url', 'input_audio', 'refusal'" — and the rest accept the
#: wrong spelling silently, receiving no image at all and answering "I can't
#: see an image attached". The second failure is the dangerous one, because
#: it looks like the model being incapable rather than the request being
#: malformed.
IMAGE_PART = "image_url"
TEXT_PART = "text"


@dataclass(slots=True)
class LLMResponse:
    """One completed inference call."""

    text: str
    model_id: str
    usage: Usage
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "model_id": self.model_id,
            "usage": self.usage.to_dict(),
            "latency_ms": self.latency_ms,
            "finish_reason": self.finish_reason,
            "tool_calls": self.tool_calls,
        }


def encode_image(path: str) -> str:
    """Return a ``data:`` URI for a local image file."""
    if not os.path.exists(path):
        raise ValidationError(f"image file does not exist: {path}")
    mime, _ = mimetypes.guess_type(path)
    if not mime or not mime.startswith("image/"):
        raise ValidationError(f"not a recognised image file: {path}")
    with open(path, "rb") as handle:
        payload = base64.b64encode(handle.read()).decode("ascii")
    return f"data:{mime};base64,{payload}"


def is_remote_image(reference: str) -> bool:
    """Whether an image reference is already a URL rather than a local path."""
    return reference.startswith(("http://", "https://", "data:"))


def build_user_message(
    text: str, images: Sequence[str] = (), image_urls: Sequence[str] = ()
) -> dict[str, Any]:
    """Build a chat message that carries text and, optionally, images.

    ``images`` are local paths or already-resolved references; ``image_urls``
    are remote URLs or ``data:`` URIs. A reference that is already a URL is
    passed through untouched, so callers never have to know which is which.
    Images are placed before the text, which is the ordering the
    OpenAI-compatible endpoints expect.
    """
    references = [*images, *image_urls]
    if not references:
        return {"role": "user", "content": text}

    parts: list[dict[str, Any]] = []
    for reference in references:
        url = reference if is_remote_image(reference) else encode_image(reference)
        parts.append({"type": IMAGE_PART, "image_url": {"url": url}})
    parts.append({"type": TEXT_PART, "text": text})
    return {"role": "user", "content": parts}


class GmiLLMClient:
    """Calls GMI's inference endpoints."""

    def __init__(
        self,
        base_url: str,
        client: HttpClient,
        *,
        endpoint: str = ENDPOINT_CHAT,
        default_max_tokens: int = 2048,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client
        self.endpoint = endpoint
        self.default_max_tokens = default_max_tokens

    # -- request construction ---------------------------------------------- #

    def _headers(self) -> dict[str, str]:
        from ..config import require_api_key

        return {
            "Authorization": f"Bearer {require_api_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _body(
        self,
        model_id: str,
        messages: Sequence[dict[str, Any]],
        *,
        temperature: float | None,
        max_tokens: int | None,
        tools: Sequence[dict[str, Any]] | None,
        tool_choice: Any | None,
        extra: dict[str, Any] | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model_id, "messages": list(messages)}
        if self.endpoint == ENDPOINT_RESPONSES:
            # The responses API takes `input`, not `messages`.
            body.pop("messages")
            body["input"] = list(messages)
        if temperature is not None:
            body["temperature"] = float(temperature)
        if max_tokens is not None:
            body["max_tokens"] = int(max_tokens)
        if tools:
            body["tools"] = list(tools)
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        if extra:
            body.update(extra)
        return body

    # -- the call ----------------------------------------------------------- #

    def chat(
        self,
        model_id: str,
        messages: Sequence[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        extra: dict[str, Any] | None = None,
        model_info: ModelInfo | None = None,
    ) -> LLMResponse:
        """Run one inference call and return the parsed response."""
        if not messages:
            raise ValidationError("a chat call needs at least one message")
        body = self._body(
            model_id,
            messages,
            temperature=temperature,
            max_tokens=max_tokens if max_tokens is not None else self.default_max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            extra=extra,
        )
        url = f"{self.base_url}/{self.endpoint}"
        started = time.perf_counter()
        response = self.client.request("POST", url, headers=self._headers(), json_body=body)
        elapsed = int((time.perf_counter() - started) * 1000)

        payload = response.json()
        if not isinstance(payload, dict):
            raise NexusError(f"model {model_id} returned a non-JSON response")

        text, finish_reason, tool_calls = _extract_content(payload)
        usage = _extract_usage(payload, model_info)
        if not usage.measured:
            # No usage block: fall back to the catalogue's prices so the run
            # still has a defensible cost figure, and mark it unmeasured.
            usage = _estimate_usage(model_info, messages, text, usage.requests)

        return LLMResponse(
            text=text,
            model_id=model_id,
            usage=usage,
            latency_ms=response.elapsed_ms or elapsed,
            raw=payload,
            finish_reason=finish_reason,
            tool_calls=tool_calls,
        )

    def complete(self, model_id: str, prompt: str, **kwargs: Any) -> LLMResponse:
        """Run a single-turn completion."""
        return self.chat(model_id, [{"role": "user", "content": prompt}], **kwargs)

    def available(self) -> bool:
        """Return whether the client can be used (a key is present)."""
        from ..config import read_api_key

        return bool(read_api_key())


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #


def _extract_content(payload: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    """Pull the text, finish reason and tool calls out of any response shape."""
    # OpenAI chat completions.
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        content = message.get("content")
        text = _content_to_text(content)
        tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        return text, str(choice.get("finish_reason") or ""), [dict(c) for c in tool_calls if isinstance(c, dict)]

    # Anthropic-style messages.
    content = payload.get("content")
    if isinstance(content, list):
        text = "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") in ("text", None)
        )
        return text, str(payload.get("stop_reason") or ""), []

    # OpenAI responses.
    if "output_text" in payload:
        output_text = payload.get("output_text")
        text = output_text if isinstance(output_text, str) else json.dumps(output_text)
        return text, str(payload.get("status") or ""), []

    output = payload.get("output")
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                    parts.append(str(part.get("text") or ""))
        return "".join(parts), str(payload.get("status") or ""), []

    # Some gateways return a bare string.
    if isinstance(payload.get("text"), str):
        return payload["text"], "", []

    raise NexusError("could not find any response content in the model reply")


def _content_to_text(content: Any) -> str:
    """Flatten a message content value into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" or "text" in part:
                    parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return str(content)


def _extract_usage(payload: dict[str, Any], model_info: ModelInfo | None = None) -> Usage:
    """Read the provider's own usage block, if it sent one.

    GMI's inference endpoint reports **token counts** in its usage block but
    no dollar figure. The tokens are therefore measured while the cost is
    not, so the measured tokens are priced at the model's published rate —
    the same estimate the pre-request check uses, so the two agree.

    Pricing the tokens here rather than leaving the cost at zero matters:
    a zero cost would let a run spend against the budget guard for nothing,
    and would report a figure the account was actually charged against.
    """
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return Usage(measured=False)

    prompt_tokens = int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0)
    completion_tokens = int(raw.get("completion_tokens") or raw.get("output_tokens") or 0)
    requests = int(raw.get("requests") or 1)
    if not prompt_tokens and not completion_tokens:
        return Usage(measured=False)

    cost = raw.get("cost_usd")
    if cost is None:
        cost = raw.get("total_cost_usd")
    if not isinstance(cost, (int, float)) or float(cost) == 0.0:
        # The provider reported tokens but no cost. Price them from the
        # catalogue, and say so.
        cost = _price_tokens(model_info, prompt_tokens, completion_tokens, requests)
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        requests=requests,
        cost_usd=float(cost),
        measured=True,
    )


def _price_tokens(
    model_info: ModelInfo | None,
    prompt_tokens: int,
    completion_tokens: int,
    requests: int,
) -> float:
    """Price measured tokens at the model's published rate.

    Returns ``0.0`` when the catalogue publishes no price for the model —
    which is reported as unknown rather than as free.
    """
    if model_info is None:
        return 0.0
    pricing = model_info.pricing
    if not pricing or (not pricing.prompt and not pricing.completion and not pricing.request):
        return 0.0
    return pricing.cost(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        requests=requests,
    )


def _estimate_usage(
    model_info: ModelInfo | None,
    messages: Sequence[dict[str, Any]],
    text: str,
    requests: int,
) -> Usage:
    """Estimate token usage when the provider did not report it."""
    from ..router import estimate_tokens

    prompt_chars = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            prompt_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    prompt_chars += len(str(part.get("text") or ""))
    prompt_tokens = estimate_tokens(" " * prompt_chars)
    completion_tokens = estimate_tokens(text)
    cost = 0.0
    if model_info is not None:
        cost = model_info.pricing.cost(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, requests=requests
        )
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        requests=requests,
        cost_usd=cost,
        measured=False,
    )


__all__ = [
    "ENDPOINT_CHAT",
    "ENDPOINT_MESSAGES",
    "ENDPOINT_RESPONSES",
    "GmiLLMClient",
    "LLMResponse",
    "build_user_message",
    "encode_image",
    "is_remote_image",
]
