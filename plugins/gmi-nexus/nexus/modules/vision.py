"""GMI Vision: understanding images with GMI's vision-capable models.

The point of this module is that *not every language model can read an
image*. Sending an image to a text-only model does not produce an error — it
produces a confident, wrong answer. So NEXUS never assumes:

* Vision capability comes from the live catalogue's own metadata when it
  publishes any, and from a documented pattern otherwise.
* A model whose vision support is only inferred is **probed** before it is
  trusted. The probe asks a model to name the colour of two solid squares; a
  model that reads both correctly has demonstrably processed the image.
* The probe result is cached, so the check is paid for once per model.

Screenshots come from Hermes' own ``computer_use`` tool, so NEXUS never
implements its own screen capture.
"""

from __future__ import annotations

import base64
import json
import random
import re
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..budget import BudgetGuard
from ..config import Settings
from ..errors import NexusError, ValidationError
from ..models import (
    PROBE_FAILED_PREFIX,
    PROBE_VERIFIED_PREFIX,
    Capability,
    TaskType,
    probe_verdict,
)

#: A 5x7 bitmap font for the digits, used by the vision probe.
#:
#: The probe shows a model a random three-digit number drawn as pixels and
#: asks it to read the number back. A model that cannot see images has to
#: guess all three digits — about one chance in nine hundred — and the
#: number is drawn afresh each time, so it cannot be memorised either.
#:
#: An earlier version of the probe asked a model to name the colours of two
#: squares. That was far too easy to fake: "red" and "blue" are the two
#: most guessable colour words there are, and on the live catalogue several
#: text-only models passed that probe and then described the artefact they
#: were given as "a modern white kitchen". A probe has to be harder to
#: pass by accident than the thing it is testing.
PROBE_DIGIT_FONT: dict[str, tuple[str, ...]] = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
}

#: How many pixels wide one dot of a probe digit is drawn.
PROBE_DIGIT_SCALE = 12

#: The gap between probe digits, in pixels.
PROBE_DIGIT_GAP = 12

#: The colours the probe digit is drawn in: black on white.
PROBE_INK = (20, 20, 20)
PROBE_PAPER = (255, 255, 255)

#: Phrases that mean "no image reached me" rather than "here is what the
#: image shows".
#:
#: A model that was sent no image — or an image its backend dropped — often
#: says so instead of guessing. Such an answer is not a description, and
#: presenting it as one would be a fabrication. The refusal is detected and
#: the model is passed over instead.
#:
#: The check is deliberately one-sided: it looks for a denial of sight, not
#: for a description. A real description of an image can contain any word
#: in the list of valid answers.
REFUSAL_MARKERS: tuple[str, ...] = (
    "can't see an image",
    "cannot see an image",
    "can not see an image",
    "can't see the image",
    "cannot see the image",
    "can't see any image",
    "don't see an image",
    "do not see an image",
    "no image is attached",
    "no image was attached",
    "no image has been provided",
    "no image is provided",
    "without an image",
    "please attach",
    "please upload",
    "please provide the image",
    "you forgot to attach",
    "unable to view the image",
    "unable to see the image",
    "unable to process the image",
    "cannot process images",
    "can't process images",
    "as an ai language model",
    "i'm sorry, but i cannot",
    "i am sorry, but i cannot",
)


def looks_like_refusal(text: str) -> bool:
    """Whether an answer is the model saying it saw no image.

    Such an answer carries no information about the image. Treating it as a
    description would be inventing one.
    """
    lowered = (text or "").strip().lower()
    if not lowered:
        # An empty answer is not a description either.
        return True
    return any(marker in lowered for marker in REFUSAL_MARKERS)


#: The largest image NEXUS will download and inline. Anything bigger is
#: left as a URL, because inlining it would make the request enormous.
MAX_INLINE_IMAGE_BYTES = 12 * 1024 * 1024

#: The image types that are worth inlining.
INLINABLE_IMAGE_TYPES: tuple[str, ...] = ("image/png", "image/jpeg", "image/webp", "image/gif")


def _inline_remote_image(reference: str, client: Any, timeout: float) -> str | None:
    """Download a remote image and return it as a ``data:`` URI.

    GMI's gateway rejects plain HTTP links for some models — "HTTP links are
    not supported for requests restricted by VPCSC" — while the same image
    inlined as a ``data:`` URI is accepted by every model that can see at
    all. So NEXUS fetches the image itself and sends the bytes.

    Returns ``None`` when the image cannot be fetched or is not an image;
    the caller then falls back to sending the URL as it stands.
    """
    import base64 as _base64

    try:
        response = client.request(
            "GET",
            reference,
            headers={"Accept": "image/*"},
            timeout=timeout,
        )
    except Exception:
        return None
    if not response.ok:
        return None
    content_type = str(response.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type and content_type not in INLINABLE_IMAGE_TYPES:
        return None
    if not content_type:
        content_type = "image/png"
    raw = response.raw
    if not raw:
        return None
    if len(raw) > MAX_INLINE_IMAGE_BYTES:
        return None
    payload = _base64.b64encode(raw).decode("ascii")
    return f"data:{content_type};base64,{payload}"


def probe_number_data_uri(number: str) -> str:
    """Return a ``data:`` URI showing ``number`` as drawn digits.

    The digits are drawn from :data:`PROBE_DIGIT_FONT` at
    :data:`PROBE_DIGIT_SCALE` pixels per dot, black on white.
    """
    scale = PROBE_DIGIT_SCALE
    glyph_width = 5 * scale
    glyph_height = 7 * scale
    gap = PROBE_DIGIT_GAP
    width = len(number) * glyph_width + max(0, len(number) - 1) * gap
    margin = scale * 2
    pixels = [
        [(PROBE_PAPER) for _ in range(width + margin * 2)]
        for _ in range(glyph_height + margin * 2)
    ]

    for index, character in enumerate(number):
        rows = PROBE_DIGIT_FONT.get(character)
        if rows is None:
            continue
        left = margin + index * (glyph_width + gap)
        for row_index, row in enumerate(rows):
            for column_index, filled in enumerate(row):
                if filled != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        pixels[
                            margin + row_index * scale + dy
                        ][left + column_index * scale + dx] = PROBE_INK
    return png_data_uri(pixels)


def random_probe_number() -> str:
    """Return a random three-digit number for a probe to show.

    A fresh number each time means a model cannot pass the probe by
    learning the answer.
    """
    return f"{random.randint(100, 999)}"

#: The prompt used to ask a model to analyse an image.
ANALYSIS_PROMPT = (
    "{question}\n\n"
    "Answer concretely. If something is not visible in the image, say so rather than "
    "guessing."
)

#: The prompt used to detect problems in a visual artefact.
#:
#: The criteria are spliced in by string replacement rather than by
#: ``str.format``, because the prompt contains literal JSON braces that
#: ``format`` would try to interpret as replacement fields.
ISSUE_PROMPT = (
    "You are reviewing a visual artefact. Identify the concrete problems with it.\n\n"
    "Look for: layout and alignment defects, inconsistent spacing, typography that "
    "does not match, colour problems, contrast and legibility, overlapping or clipped "
    "elements, broken or placeholder content, and anything that looks unfinished.\n\n"
    "Reply with JSON only: {\"issues\": [{\"severity\": \"high|medium|low\", "
    "\"area\": \"where in the image\", \"problem\": \"what is wrong\", "
    "\"fix\": \"what to change\"}], \"summary\": \"one sentence\"}\n\n"
    "Additional criteria: __CRITERIA__\n"
)

#: The prompt used to compare two images.
COMPARE_PROMPT = (
    "Compare these two images. The first is the 'before' and the second is the 'after'.\n\n"
    "Report: what changed, what improved, what regressed, and anything that should "
    "have changed but did not.\n\n"
    "Reply with JSON only: {\"changes\": [\"...\"], \"improvements\": [\"...\"], "
    "\"regressions\": [\"...\"], \"summary\": \"one sentence\"}"
)


def solid_png_data_uri(rgb: tuple[int, int, int], size: int = 32) -> str:
    """Return a ``data:`` URI for a solid-colour PNG.

    Written with ``zlib`` and ``struct`` so the plugin needs no image library
    to build its own probe images.
    """
    return png_data_uri([[rgb] * size for _ in range(size)])


def png_data_uri(pixels: list[list[tuple[int, int, int]]]) -> str:
    """Return a ``data:`` URI for a PNG built from a grid of RGB triples.

    ``pixels`` is a list of rows, each a list of ``(r, g, b)`` triples. The
    plugin draws its own probe images this way, so it needs no image
    library — only ``zlib`` and ``struct``.
    """
    height = len(pixels)
    width = len(pixels[0]) if height else 0
    raw = b"".join(
        b"\x00" + b"".join(bytes(colour) for colour in row) for row in pixels
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


@dataclass(slots=True)
class VisionOutcome:
    """The result of one vision request."""

    ok: bool
    text: str = ""
    model_id: str | None = None
    cost_usd: float = 0.0
    latency_ms: int = 0
    images: list[str] = field(default_factory=list)
    structured: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "text": self.text,
            "model_id": self.model_id,
            "cost_usd": round(self.cost_usd, 8),
            "latency_ms": self.latency_ms,
            "images": list(self.images),
            "structured": dict(self.structured),
            "error": self.error,
        }


class VisionModule:
    """Analyses images with GMI's vision-capable models."""

    def __init__(
        self,
        settings: Settings,
        registry: Any,
        router: Any,
        llm_client: Any,
        budget: BudgetGuard,
        ctx: Any | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.router = router
        self.llm_client = llm_client
        self.budget = budget
        self.ctx = ctx

    # -- model selection ---------------------------------------------------- #

    def vision_models(self) -> list[Any]:
        """Return the live models that can read an image."""
        return [
            model
            for model in self.registry.list_models()
            if model.supports(Capability.VISION) and model.is_ready
        ]

    def candidate_models(self, *, strategy: str = "balanced", prompt: str = "") -> list[Any]:
        """Return the vision-capable models, in the order they should be tried.

        The catalogue *claims* a model can read an image. That claim is not
        always true, so the list is a set of candidates rather than a single
        answer: each one is tried until one of them actually reads the image.
        """
        decision = self.router.route(
            TaskType.IMAGE_UNDERSTANDING,
            (Capability.TEXT, Capability.VISION),
            self.vision_models(),
            strategy=strategy,
            prompt=prompt,
        )
        return [candidate.model for candidate in decision.candidates]

    def pick_model(self, *, strategy: str = "balanced", prompt: str = "") -> Any | None:
        """Choose a vision-capable model, preferring verified ones.

        The candidates are tried in route order. A model that has already
        been probed is used or skipped immediately; an unprobed one is
        probed on the spot, and the search stops at the first that verifies.
        That keeps the cost of verification proportional to how many it
        takes to find one, rather than to how many the catalogue advertises.
        """
        verify = bool(getattr(self.settings, "verify_vision_capability", True))
        for model in self.candidate_models(strategy=strategy, prompt=prompt):
            evidence = str(model.capability_evidence.get(Capability.VISION.value, ""))
            verdict = probe_verdict(evidence)
            if verdict is True:
                # Already probed and confirmed.
                return model
            if verdict is False:
                # Already probed and refuted. Do not try it again.
                continue
            if not verify:
                # The operator asked for no probing. Their call.
                return model
            outcome = self.probe(model.id)
            if outcome.get("verified"):
                return model
        return None

    # -- capability probing ------------------------------------------------- #

    def probe(self, model_id: str) -> dict[str, Any]:
        """Verify that a model can actually read an image.

        A random three-digit number is drawn as pixels and the model is asked
        to read it back. A model that cannot see images has to guess all
        three digits — roughly one chance in nine hundred — and because the
        number is drawn afresh for every probe it cannot be learned either.

        The number is not written down before the answer is checked: it is
        drawn, rendered, sent, and only then compared. That is the point of
        the whole exercise. An earlier probe asked the model to name two
        colours, which several text-only models on the live catalogue
        guessed correctly by accident and then answered questions about
        images they could not see.
        """
        from ..providers.gmi_llm import IMAGE_PART, build_user_message

        number = random_probe_number()
        parts: list[dict[str, Any]] = [
            {"type": IMAGE_PART, "image_url": {"url": probe_number_data_uri(number)}}
        ]
        parts.append(
            {
                "type": "text",
                "text": (
                    "What three-digit number is shown in this image? "
                    "Reply with only the three digits, and nothing else."
                ),
            }
        )
        messages = [{"role": "user", "content": parts}]

        try:
            response = self.llm_client.chat(model_id, messages, max_tokens=16, temperature=0.0)
        except NexusError as exc:
            result = {
                "model_id": model_id,
                "verified": False,
                "evidence": f"probe failed: {exc}",
            }
            self.registry.apply_probe(
                model_id, Capability.VISION, False, result["evidence"]
            )
            return result

        answer = (response.text or "").strip()
        digits = re.findall(r"\d", answer)
        # The model may reply "473" or "The number is 473." — either way the
        # digits are compared. Three digits in a row is the pass condition.
        read = "".join(digits) if len(digits) >= 3 else ""
        verified = read == number
        # The evidence carries the verdict in its first words, so a later
        # reader — the router, the dashboard — can tell a model that
        # *failed* its probe from one that was never probed at all.
        evidence = (
            f"{PROBE_VERIFIED_PREFIX} — read {read} from a drawn number"
            if verified
            else f"{PROBE_FAILED_PREFIX} — answered {answer[:60]!r} instead of the drawn number"
        )
        self.registry.apply_probe(model_id, Capability.VISION, verified, evidence)
        self.budget.record(
            "vision_probe", model_id, response.usage.cost_usd, requests=1, detail=evidence
        )
        return {
            "model_id": model_id,
            "verified": verified,
            "evidence": evidence,
            "cost_usd": round(response.usage.cost_usd, 8),
        }

    def verified_vision_models(self, *, probe_unverified: bool = True) -> list[Any]:
        """Return vision-capable models, probing the unverified ones if asked.

        Only a model that has *passed* a probe counts as verified. A model
        whose capability came from the catalogue's own metadata is a claim,
        not a proof: the live catalogue advertises image input on several
        models that cannot actually see, so metadata alone is not enough.
        """
        models = self.vision_models()
        if not probe_unverified:
            return [
                model
                for model in models
                if probe_verdict(
                    model.capability_evidence.get(Capability.VISION.value, "")
                )
                is True
            ]

        verified: list[Any] = []
        for model in models:
            evidence = model.capability_evidence.get(Capability.VISION.value, "")
            verdict = probe_verdict(evidence)
            if verdict is True:
                verified.append(model)
                continue
            if verdict is False:
                continue
            # Only claimed so far: verify before trusting it with an image.
            outcome = self.probe(model.id)
            if outcome.get("verified"):
                verified.append(model)
        return verified

    # -- analysis ----------------------------------------------------------- #

    def _prepare_images(self, images: Sequence[str]) -> list[str]:
        """Return the images in the form that actually reaches a model.

        A remote URL is downloaded and inlined as a ``data:`` URI. GMI's
        gateway refuses plain HTTP links for some models — "HTTP links are
        not supported for requests restricted by VPCSC" — and the models
        that accept the link silently ignore the image and answer "I can't
        see an image attached". Inlining sidesteps both behaviours.

        A local path and an existing ``data:`` URI are left exactly as they
        are. If the download fails the URL is sent as it stands, because a
        URL that works for some models beats no image for any of them.
        """
        client = getattr(self.llm_client, "client", None)
        timeout = float(getattr(self.settings, "request_timeout_s", 60))
        prepared: list[str] = []
        for reference in images:
            if reference.startswith("data:"):
                prepared.append(reference)
                continue
            if reference.startswith(("http://", "https://")) and client is not None:
                inlined = _inline_remote_image(reference, client, timeout)
                prepared.append(inlined if inlined else reference)
                continue
            prepared.append(reference)
        return prepared

    def analyse(
        self,
        images: Sequence[str],
        question: str,
        *,
        strategy: str = "balanced",
        approved: bool = False,
    ) -> VisionOutcome:
        """Answer a question about one or more images.

        The candidates are tried in turn. A model that answers "I can't see
        an image" has not described anything — the image never reached it —
        so its answer is discarded, the model is marked as unable to see,
        and the next candidate is tried. If no candidate can read the image,
        that is what the caller is told. No description is ever invented to
        fill the gap.
        """
        if not images:
            raise ValidationError("vision analysis needs at least one image")

        candidates = self.candidate_models(strategy=strategy, prompt=question)
        if not candidates:
            return VisionOutcome(
                ok=False,
                error=(
                    "no model in the live catalogue is marked as vision-capable; "
                    "run a capability probe to find one"
                ),
            )

        from ..providers.gmi_llm import build_user_message

        prompt = ANALYSIS_PROMPT.format(question=question)
        attempts: list[dict[str, Any]] = []
        for model in candidates:
            evidence = str(model.capability_evidence.get(Capability.VISION.value, ""))
            verdict = probe_verdict(evidence)
            if verdict is False:
                # Already known to be unable to see.
                continue
            if verdict is None and getattr(self.settings, "verify_vision_capability", True):
                outcome = self.probe(model.id)
                if not outcome.get("verified"):
                    attempts.append(
                        {"model_id": model.id, "outcome": "failed its capability probe"}
                    )
                    continue

            estimate = model.pricing.cost(
                prompt_tokens=512, completion_tokens=1024, requests=1
            )
            try:
                self.budget.enforce(
                    estimate, approved=approved, description=f"a vision call to {model.id}"
                )
            except NexusError as exc:
                return VisionOutcome(ok=False, error=str(exc), model_id=model.id)

            message = build_user_message(prompt, self._prepare_images(images))
            try:
                response = self.llm_client.chat(model.id, [message], model_info=model)
            except NexusError as exc:
                attempts.append({"model_id": model.id, "outcome": f"call failed: {exc}"})
                continue

            self.budget.record(
                "vision",
                model.id,
                response.usage.cost_usd,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                requests=1,
                detail=question[:200],
            )

            if looks_like_refusal(response.text):
                # The model saw no image. Say so, remember it, and try the
                # next candidate rather than reporting a non-answer.
                refusal = f"{PROBE_FAILED_PREFIX} — answered {response.text.strip()[:80]!r}"
                self.registry.apply_probe(model.id, Capability.VISION, False, refusal)
                attempts.append({"model_id": model.id, "outcome": "reported no image"})
                continue

            return VisionOutcome(
                ok=True,
                text=response.text,
                model_id=model.id,
                cost_usd=response.usage.cost_usd,
                latency_ms=response.latency_ms,
                images=list(images),
            )

        detail = "; ".join(
            f"{attempt['model_id']}: {attempt['outcome']}" for attempt in attempts
        )
        return VisionOutcome(
            ok=False,
            error=(
                "no vision-capable model could read the image; "
                f"{len(attempts)} candidate(s) tried ({detail})"
            ),
            images=list(images),
        )

    def detect_issues(
        self,
        images: Sequence[str],
        criteria: str = "",
        *,
        approved: bool = False,
    ) -> VisionOutcome:
        """Return structured feedback about problems in a visual artefact."""
        outcome = self.analyse(
            images,
            ISSUE_PROMPT.replace("__CRITERIA__", criteria or "none specified"),
            approved=approved,
        )
        if outcome.ok:
            outcome.structured = _extract_json(outcome.text) or {}
            outcome.text = outcome.structured.get("summary") or outcome.text
        return outcome

    def compare(
        self,
        before: str,
        after: str,
        *,
        question: str = "",
        approved: bool = False,
    ) -> VisionOutcome:
        """Compare two images and report the differences."""
        prompt = COMPARE_PROMPT + (f"\n\nFocus on: {question}" if question else "")
        outcome = self.analyse([before, after], prompt, approved=approved)
        if outcome.ok:
            outcome.structured = _extract_json(outcome.text) or {}
            outcome.text = outcome.structured.get("summary") or outcome.text
        return outcome

    # -- screenshots -------------------------------------------------------- #

    def capture_screenshot(self, *, app: str = "screen") -> dict[str, Any]:
        """Capture a screenshot through Hermes' own ``computer_use`` tool."""
        if self.ctx is None:
            return {
                "ok": False,
                "error": "no Hermes context is available, so NEXUS cannot capture a screen",
            }
        try:
            raw = self.ctx.dispatch_tool(
                "computer_use", {"action": "capture", "mode": "vision", "app": app}
            )
        except Exception as exc:
            return {"ok": False, "error": f"screenshot capture failed: {exc}"}
        payload = _parse_tool_result(raw)
        if isinstance(payload, dict) and payload.get("error"):
            # The capture tool reported its own failure; pass it on rather
            # than replacing it with a vaguer message.
            return {"ok": False, "error": str(payload["error"]), "app": app}
        image_url = _find_image(payload)
        if not image_url:
            return {
                "ok": False,
                "error": "the capture tool did not return an image",
                "app": app,
                "raw": str(payload)[:500],
            }
        return {"ok": True, "image": image_url, "app": app}


def _extract_json(raw: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a model reply."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _parse_tool_result(raw: Any) -> Any:
    """Parse a Hermes tool result, which is a JSON string."""
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {"raw": str(raw)}


def _find_image(payload: Any) -> str | None:
    """Find an image reference in a tool result."""
    if isinstance(payload, str):
        return payload if payload.startswith(("http", "data:", "/")) else None
    if isinstance(payload, dict):
        for key in ("image_url", "image", "path", "file_path", "screenshot"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        for value in payload.values():
            found = _find_image(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_image(value)
            if found:
                return found
    return None


__all__ = [
    "ANALYSIS_PROMPT",
    "COMPARE_PROMPT",
    "ISSUE_PROMPT",
    "PROBE_DIGIT_FONT",
    "VisionModule",
    "VisionOutcome",
    "png_data_uri",
    "probe_number_data_uri",
    "random_probe_number",
    "solid_png_data_uri",
]
