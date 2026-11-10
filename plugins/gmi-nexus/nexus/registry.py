"""Live GMI Cloud catalogue discovery and capability detection.

Two catalogues are read:

* ``GET {llm_base_url}/models`` — the routable text/multimodal models, with
  their real list pricing, context window and organisation discount.
* ``GET {media_base_url}/models`` — the request-queue catalogue used for
  image, video and audio generation.

Capabilities are never asserted from a hard-coded model list. Each capability
carries the evidence that justified it:

``metadata``
    The catalogue itself described it (``input_modalities`` /
    ``output_modalities`` / ``supported_parameters``).
``heuristic``
    The model id or description matches a known pattern. Treat as a hint.
``probe``
    NEXUS verified it by making the model accept (or reject) a probe request.

Routing treats ``heuristic`` capabilities as usable but verifies the risky one
— vision — with a probe before it is trusted, because sending an image to a
text-only model produces a confident, wrong answer rather than an error.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Iterable

from .config import Settings
from .errors import NexusError, ValidationError
from .http import HttpClient
from .media_router import is_product_model
from .models import Capability, MediaModelInfo, ModelInfo, Modality, Pricing, probe_verdict

#: How long a discovered catalogue is considered fresh.
CATALOGUE_TTL_SECONDS = 300.0

#: Context length at or above which a model is treated as long-context.
LONG_CONTEXT_TOKENS = 200_000

# --------------------------------------------------------------------------- #
# Heuristic patterns. These only ever *hint*; they never become "verified".
# --------------------------------------------------------------------------- #

_VISION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bVL\b", r"-VL[-/]", r"super-vl", r"vision", r"\bomni\b", r"-omni",
        r"gpt-4o", r"gpt-5", r"gpt-6", r"gemini", r"gemma-3", r"gemma-4",
        r"qwen.*vl", r"internvl", r"llava", r"pixtral", r"phi-3-vision",
        r"phi-4", r"nemotron.*vl", r"glm-4v", r"step-3v", r"kimi-vl", r"ernie.*vl",
    )
)

_CODE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"coder", r"code", r"devstral", r"starcoder", r"codestral", r"qwen.*coder",
    )
)

_REASONING_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"thinking", r"reasoning", r"reasoner", r"\br1\b", r"\br2\b", r"\bseed\b",
        r"-vl-exp", r"exp$",
    )
)

_TOOL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (r"instruct", r"chat", r"-\d", r"gemini", r"gpt-", r"glm", r"kimi", r"minimax")
)

# --------------------------------------------------------------------------- #
# Media catalogue classification
# --------------------------------------------------------------------------- #

_VIDEO_HINTS: tuple[str, ...] = (
    "video", "i2v", "t2v", "r2v", "kling", "veo", "seedance", "wan2", "wan3",
    "ltx", "hunyuan1", "pixverse", "vidu", "skyreels", "sora", "mochi",
    "framepack", "hailuo", "animator", "animate",
)

_AUDIO_HINTS: tuple[str, ...] = (
    "tts", "speech", "audio", "voice", "music", "sonic", "chatterbox", "inworld",
    "speech-2", "melody",
)

_IMAGE_HINTS: tuple[str, ...] = (
    "image", "flux", "seedream", "sdxl", "midjourney", "recraft", "ideogram",
    "z-image", "bria-", "reve-", "qwen-image", "glm-image", "luma-uni", "illust",
    "ernie-image", "kolors", "hunyuan-image", "hy-image", "sunburst", "flare",
)

_3D_HINTS: tuple[str, ...] = ("3d", "trellis", "hunyuan-3d", "tripo", "mesh")


def classify_media_modality(model_id: str) -> Modality:
    """Infer the modality of a request-queue model from its identifier.

    The request-queue catalogue publishes only ids, so this is a heuristic.
    It is used to *group* models for the operator, never to decide that a
    model can be called with a particular payload — the payload format is
    chosen from the model's own documentation at call time.
    """
    lowered = model_id.lower()
    # Video is checked first: "image-to-video" also contains "image".
    if any(hint in lowered for hint in _VIDEO_HINTS):
        return Modality.VIDEO
    if any(hint in lowered for hint in _AUDIO_HINTS):
        return Modality.AUDIO
    if any(hint in lowered for hint in _3D_HINTS):
        return Modality.IMAGE
    if any(hint in lowered for hint in _IMAGE_HINTS):
        return Modality.IMAGE
    return Modality.IMAGE


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_pricing(raw: Any, discount: Any = None) -> Pricing:
    """Build a :class:`Pricing` from the catalogue's ``pricing`` block."""
    if not isinstance(raw, dict):
        raw = {}
    overrides = raw.get("overrides")
    return Pricing(
        prompt=_as_float(raw.get("prompt")),
        completion=_as_float(raw.get("completion")),
        request=_as_float(raw.get("request")),
        image=_as_float(raw.get("image")),
        input_cache_read=_as_float(raw.get("input_cache_read")),
        input_cache_write=_as_float(raw.get("input_cache_write")),
        overrides=[o for o in overrides if isinstance(o, dict)] if isinstance(overrides, list) else [],
        discount_to_user=_as_float(discount),
    )


def _modalities_from_metadata(record: dict[str, Any]) -> tuple[set[Capability], dict[str, str]]:
    """Read capabilities from the catalogue's own modality metadata."""
    capabilities: set[Capability] = set()
    evidence: dict[str, str] = {}

    for side, key in (("input_modalities", Capability.VISION), ("output_modalities", Capability.IMAGE_OUT)):
        entries = record.get(side)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("type") or "").lower()
            if kind in ("image", "image_url", "vision"):
                capabilities.add(key)
                evidence[key.value] = f"metadata:{side}"
            elif kind == "audio":
                capabilities.add(Capability.AUDIO)
                evidence[Capability.AUDIO.value] = f"metadata:{side}"
            elif kind == "video":
                capabilities.add(Capability.VIDEO)
                evidence[Capability.VIDEO.value] = f"metadata:{side}"
            elif kind == "text":
                capabilities.add(Capability.TEXT)
                evidence.setdefault(Capability.TEXT.value, f"metadata:{side}")

            # Tool support is declared through supported_parameters.
            params = entry.get("supported_parameters")
            if isinstance(params, dict) and params.get("tools") is True:
                capabilities.add(Capability.TOOLS)
                evidence[Capability.TOOLS.value] = "metadata:supported_parameters.tools"
            if isinstance(params, dict) and params.get("reasoning") is not None:
                capabilities.add(Capability.REASONING)
                evidence[Capability.REASONING.value] = "metadata:supported_parameters.reasoning"
    return capabilities, evidence


def infer_capabilities(record: dict[str, Any]) -> tuple[set[Capability], dict[str, str]]:
    """Derive a capability set for one catalogue record.

    Returns the capabilities and the evidence for each of them.
    """
    capabilities, evidence = _modalities_from_metadata(record)

    model_id = str(record.get("id") or "")
    description = str(record.get("description") or "")
    haystack = f"{model_id} {description}"

    if Capability.TEXT not in capabilities:
        capabilities.add(Capability.TEXT)
        evidence[Capability.TEXT.value] = "metadata:text-model"

    if Capability.VISION not in capabilities:
        if any(pattern.search(haystack) for pattern in _VISION_PATTERNS):
            capabilities.add(Capability.VISION)
            evidence[Capability.VISION.value] = "heuristic:model-id-pattern"
    if Capability.CODE not in capabilities:
        if any(pattern.search(haystack) for pattern in _CODE_PATTERNS):
            capabilities.add(Capability.CODE)
            evidence[Capability.CODE.value] = "heuristic:model-id-pattern"
    if Capability.REASONING not in capabilities:
        if any(pattern.search(haystack) for pattern in _REASONING_PATTERNS):
            capabilities.add(Capability.REASONING)
            evidence[Capability.REASONING.value] = "heuristic:model-id-pattern"
    if Capability.TOOLS not in capabilities:
        if any(pattern.search(haystack) for pattern in _TOOL_PATTERNS):
            capabilities.add(Capability.TOOLS)
            evidence[Capability.TOOLS.value] = "heuristic:model-id-pattern"

    context_length = _as_int(record.get("context_length"))
    if context_length and context_length >= LONG_CONTEXT_TOKENS:
        capabilities.add(Capability.LONG_CONTEXT)
        evidence[Capability.LONG_CONTEXT.value] = f"metadata:context_length={context_length}"

    return capabilities, evidence


def parse_model_record(record: dict[str, Any]) -> ModelInfo:
    """Turn one ``/v1/models`` entry into a :class:`ModelInfo`."""
    model_id = str(record.get("id") or "").strip()
    if not model_id:
        raise ValidationError("catalogue entry is missing an 'id'")
    capabilities, evidence = infer_capabilities(record)
    return ModelInfo(
        id=model_id,
        name=str(record.get("name") or model_id),
        owned_by=str(record.get("owned_by") or ""),
        context_length=_as_int(record.get("context_length")),
        quantization=str(record.get("quantization") or ""),
        created=_as_int(record.get("created")),
        pricing=parse_pricing(record.get("pricing"), record.get("discount_to_user")),
        description=str(record.get("description") or ""),
        capabilities=tuple(sorted(capabilities, key=lambda c: c.value)),
        capability_evidence=evidence,
        is_ready=bool(record.get("is_ready", True)),
        is_free=bool(record.get("is_free", False)),
    )


class ModelRegistry:
    """Discovers and caches the live GMI Cloud catalogues."""

    def __init__(self, settings: Settings, client: HttpClient | None = None) -> None:
        self.settings = settings
        self.client = client or HttpClient(
            timeout=float(settings.request_timeout_s), max_retries=settings.max_retries
        )
        self._lock = threading.RLock()
        self._models: dict[str, ModelInfo] = {}
        self._media: dict[str, MediaModelInfo] = {}
        self._fetched_at: float = 0.0
        self._last_error: str | None = None

    # -- discovery ---------------------------------------------------------- #

    @property
    def last_error(self) -> str | None:
        """The last discovery error, if the catalogue could not be read."""
        return self._last_error

    def _auth_headers(self) -> dict[str, str]:
        from .config import require_api_key

        return {
            "Authorization": f"Bearer {require_api_key()}",
            "Accept": "application/json",
        }

    def refresh(self, force: bool = False) -> dict[str, Any]:
        """Re-read both catalogues.

        Returns a summary. A failure to read the media catalogue does not
        discard a successful LLM catalogue: the two are independent.
        """
        with self._lock:
            fresh = (time.time() - self._fetched_at) < CATALOGUE_TTL_SECONDS
            if fresh and not force:
                return self.summary()

        errors: list[str] = []
        models: dict[str, ModelInfo] = {}
        media: dict[str, MediaModelInfo] = {}

        try:
            response = self.client.request(
                "GET", f"{self.settings.llm_base_url}/models", headers=self._auth_headers()
            )
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValidationError("model catalogue was not a JSON object")
            entries = payload.get("data")
            if not isinstance(entries, list):
                raise ValidationError("model catalogue has no 'data' list")
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                try:
                    info = parse_model_record(entry)
                except ValidationError:
                    continue
                models[info.id] = info
        except NexusError as exc:
            errors.append(f"llm catalogue: {exc}")
            models = dict(self._models)
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"llm catalogue: {exc}")
            models = dict(self._models)

        try:
            response = self.client.request(
                "GET", f"{self.settings.media_base_url}/models", headers=self._auth_headers()
            )
            payload = response.json()
            raw_ids: Iterable[Any]
            if isinstance(payload, dict):
                raw_ids = payload.get("model_ids") or payload.get("data") or []
            elif isinstance(payload, list):
                raw_ids = payload
            else:
                raw_ids = []
            for raw in raw_ids:
                if isinstance(raw, str) and raw.strip():
                    model_id = raw.strip()
                    media[model_id] = MediaModelInfo(
                        id=model_id,
                        modality=classify_media_modality(model_id),
                        product=is_product_model(model_id),
                    )
                elif isinstance(raw, dict):
                    model_id = str(raw.get("id") or raw.get("model") or "").strip()
                    if model_id:
                        media[model_id] = MediaModelInfo(
                            id=model_id,
                            modality=classify_media_modality(model_id),
                            product=is_product_model(model_id),
                        )
        except NexusError as exc:
            errors.append(f"media catalogue: {exc}")
            media = dict(self._media)
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"media catalogue: {exc}")
            media = dict(self._media)

        with self._lock:
            if models:
                self._models = models
            if media:
                self._media = media
            self._fetched_at = time.time()
            self._last_error = "; ".join(errors) if errors else None
        self._restore_probes()
        return self.summary()

    def _restore_probes(self) -> None:
        """Re-apply recorded probe outcomes to the freshly read catalogue.

        A probe is a real request, and its outcome is the only evidence that
        a declared capability is true. Without this, every restart would
        re-probe the same models and pay for the same answers twice.
        """
        from . import storage

        try:
            recorded = storage.capability_probes()
        except Exception:  # pragma: no cover - a storage fault must not
            return  # break the catalogue read

        with self._lock:
            for row in recorded:
                model_id = str(row.get("model_id") or "")
                capability_value = str(row.get("capability") or "")
                info = self._models.get(model_id)
                if info is None or not capability_value:
                    continue
                try:
                    capability = Capability(capability_value)
                except ValueError:
                    continue
                evidence = str(row.get("evidence") or "")
                verdict = probe_verdict(evidence)
                if verdict is None:
                    # A verdict recorded under an older probe protocol. It
                    # does not count: the question it answered is not the
                    # question being asked now. The model keeps whatever the
                    # catalogue claims about it, and is probed again.
                    continue
                capabilities = set(info.capabilities)
                if verdict:
                    capabilities.add(capability)
                elif capability in capabilities:
                    capabilities.discard(capability)
                info.capabilities = tuple(sorted(capabilities, key=lambda c: c.value))
                info.capability_evidence[capability.value] = evidence

    def summary(self) -> dict[str, Any]:
        """Return a summary of what is currently known about the catalogue."""
        with self._lock:
            by_modality: dict[str, int] = {}
            for info in self._media.values():
                by_modality[info.modality.value] = by_modality.get(info.modality.value, 0) + 1
            vision = sum(1 for m in self._models.values() if m.supports(Capability.VISION))
            tool_models = sum(1 for m in self._models.values() if m.supports(Capability.TOOLS))
            return {
                "llm_models": len(self._models),
                "media_models": len(self._media),
                "media_by_modality": by_modality,
                "vision_capable": vision,
                "tool_capable": tool_models,
                "fetched_at": self._fetched_at,
                "catalogue_age_seconds": round(time.time() - self._fetched_at, 1),
                "error": self._last_error,
            }

    def source_description(self) -> dict[str, Any]:
        """Describe where the catalogue came from, for the tool responses.

        The point is provenance: a caller can tell a live catalogue read from a
        cached one, and can see the endpoint that produced it.
        """
        with self._lock:
            age = time.time() - self._fetched_at if self._fetched_at else None
            return {
                "endpoint": f"{self.settings.llm_base_url}/models",
                "media_endpoint": f"{self.settings.media_base_url}/models",
                "fetched_at": self._fetched_at or None,
                "age_seconds": round(age, 1) if age is not None else None,
                "from_cache": bool(self._fetched_at) and age is not None and age < CATALOGUE_TTL_SECONDS,
                "error": self._last_error,
                "note": "The live GMI Cloud catalogue, read at runtime — not a built-in list.",
            }

    # -- accessors ---------------------------------------------------------- #

    def list_models(self, refresh: bool = False) -> list[ModelInfo]:
        """Return every discovered model."""
        if refresh or not self._models:
            self.refresh(force=refresh)
        with self._lock:
            return list(self._models.values())

    def get(self, model_id: str) -> ModelInfo | None:
        """Return one model by id."""
        if not self._models:
            self.refresh()
        with self._lock:
            return self._models.get(model_id)

    def list_media(self, modality: Modality | None = None, refresh: bool = False) -> list[MediaModelInfo]:
        """Return the request-queue models, optionally filtered by modality.

        Product models come first, then the private workflows, and each group
        is alphabetical. The provider's catalogue mixes named product models
        with account-private entries whose id is a bare UUID; sorting on the id
        alone would put every UUID first, because a digit sorts before a
        letter — which hides the models NEXUS would actually route to behind
        the ones it never selects.
        """
        if refresh or not self._media:
            self.refresh(force=refresh)
        with self._lock:
            models = list(self._media.values())
        if modality is not None:
            models = [m for m in models if m.modality is modality]
        return sorted(models, key=lambda m: (not m.product, m.id))

    def find_media(self, model_id: str) -> MediaModelInfo | None:
        """Return one request-queue model by id."""
        if not self._media:
            self.refresh()
        with self._lock:
            return self._media.get(model_id)

    def apply_probe(self, model_id: str, capability: Capability, verified: bool, evidence: str) -> None:
        """Record a verified capability (or the failure of a verification)."""
        from . import storage

        with self._lock:
            info = self._models.get(model_id)
            if info is not None:
                capabilities = set(info.capabilities)
                if verified:
                    capabilities.add(capability)
                elif capability in capabilities:
                    capabilities.discard(capability)
                info.capabilities = tuple(sorted(capabilities, key=lambda c: c.value))
                info.capability_evidence[capability.value] = evidence
        storage.save_capability_probe(model_id, capability.value, verified, evidence)

    def cached_probes(self) -> list[dict[str, Any]]:
        """Return the capability probes recorded so far."""
        from . import storage

        return storage.capability_probes()

    def close(self) -> None:
        """Release the HTTP client."""
        self.client.close()


def catalogue_error_suffix(registry: Any) -> str:
    """Return a suffix naming the catalogue error, when there was one.

    An empty catalogue and a catalogue that could not be read are two
    different states. Reporting the first as the second turns an
    authentication or transport fault into "no model satisfies this task",
    which reads like a fact about the catalogue rather than a fault in the
    request that was made of it.
    """
    error = getattr(registry, "last_error", None)
    if not error:
        return ""
    return f" — the catalogue could not be read: {error}"


__all__ = [
    "CATALOGUE_TTL_SECONDS",
    "ModelRegistry",
    "catalogue_error_suffix",
    "classify_media_modality",
    "infer_capabilities",
    "parse_model_record",
    "parse_pricing",
]
