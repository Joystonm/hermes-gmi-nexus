"""One live probe: why did a prose step come back with no visible text?

The live demo run of 2026-11-11 finished with the run status ``partial``
because one of six steps — "Write the section copy and call to action" —
came back from ``deepseek-ai/DeepSeek-V4-Flash`` with 2048 completion
tokens and an empty body. The plugin reported that honestly, but the
question worth answering is whether the *plugin* dropped a completion or
the *model* never produced one.

This script asks that model the same question, with the same 2048-token
budget, and prints the provider's raw reply so the two can be told apart.

The answer, when it was run: the provider sent ``content: null``,
``finish_reason: "length"`` and 2048 ``reasoning_tokens``. The model spent
its whole budget thinking and was cut off before it said anything — the
plugin dropped nothing. It also showed the second half of the defect: the
executor had recorded that empty reply as a step that succeeded.

It makes exactly one request and costs a fraction of a cent.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "plugins", "gmi-nexus"
    ),
)

from nexus.config import require_api_key  # noqa: E402
from nexus.http import HttpClient  # noqa: E402
from nexus.providers.gmi_llm import GmiLLMClient  # noqa: E402

MODEL = "deepseek-ai/DeepSeek-V4-Flash"
PROMPT = (
    "Original goal: Build a landing page for a new AI startup that ships a "
    "multimodal model router. Write the page, generate a hero visual for it, "
    "and check the result.\n\n"
    "Write the section copy and call to action."
)

captured: list[dict] = []
original_request = HttpClient.request


def spy(self, method, url, **kwargs):  # type: ignore[no-untyped-def]
    response = original_request(self, method, url, **kwargs)
    if "chat/completions" in url or "/v1/messages" in url or "/responses" in url:
        captured.append({"url": url, "body": response.body})
    return response


def main() -> int:
    if not os.environ.get("GMI_API_KEY"):
        print("GMI_API_KEY is not set", file=sys.stderr)
        return 2
    HttpClient.request = spy  # type: ignore[method-assign]
    client = GmiLLMClient("https://api.gmi-serving.com/v1", HttpClient())
    response = client.complete(MODEL, PROMPT, max_tokens=2048)

    print("model            :", MODEL)
    print("finish_reason    :", response.finish_reason)
    print("completion_tokens:", response.usage.completion_tokens)
    print("prompt_tokens    :", response.usage.prompt_tokens)
    print("text length      :", len(response.text or ""))
    print("text (first 400) :", repr((response.text or "")[:400]))
    print("tool calls       :", response.tool_calls)

    if captured:
        raw = json.loads(captured[-1]["body"])
        print("\n--- the provider's raw reply ---")
        print("top-level keys   :", sorted(raw))
        choices = raw.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") or {}
            print("message keys     :", sorted(message))
            content = message.get("content")
            print("content type     :", type(content).__name__)
            print("content          :", repr(content)[:600])
            if "reasoning_content" in message:
                print("reasoning_content:", repr(message.get("reasoning_content"))[:300])
        print("usage            :", json.dumps(raw.get("usage"), ensure_ascii=False)[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
