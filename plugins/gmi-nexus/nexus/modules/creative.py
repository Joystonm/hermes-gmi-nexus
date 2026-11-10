"""The Creative Engine: image, video and audio generation.

Every request goes through the same path:

1. **Choose a model.** The request-queue catalogue is filtered down to real
   product models, shortlisted, and then the shortlisted models' published
   descriptions are read so the choice is made on facts.
2. **Price it.** The model's own published price and pricing unit give an
   estimate, and the spend limits are checked before anything is submitted.
3. **Build the payload from the model's schema.** Parameter names differ per
   model — ``image`` here, ``image_path`` there, ``input_reference`` in a
   third — so NEXUS maps the request onto the parameters the model actually
   declares and reports anything it had to drop.
4. **Submit, poll, record.** The request is submitted once, polled to a
   terminal state, and the outcome recorded whether it succeeded or failed.
   A failed generation is reported as a failure; it is never dressed up as
   an empty success.
"""

from __future__ import annotations

import base64
import mimetypes
import os
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..budget import BudgetGuard
from ..config import Settings
from ..errors import NexusError, ValidationError
from ..models import Modality
from ..registry import catalogue_error_suffix

#: The semantic options the engine understands.
SUPPORTED_OPTIONS = (
    "aspect_ratio",
    "size",
    "width",
    "height",
    "duration",
    "resolution",
    "image",
    "mask",
    "negative_prompt",
    "quality",
    "n",
    "seed",
    "voice",
    "text",
    "style",
    "output_format",
    "steps",
    "guidance",
    "fps",
    "speed",
)


@dataclass(slots=True)
class CreativeResult:
    """The outcome of one creative request."""

    ok: bool
    modality: Modality
    model_id: str | None = None
    request_id: str | None = None
    media_urls: list[str] = field(default_factory=list)
    thumbnail_url: str | None = None
    status: str = ""
    estimated_cost_usd: float = 0.0
    dropped_options: list[str] = field(default_factory=list)
    deduplicated: bool = False
    latency_ms: int = 0
    error: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "modality": self.modality.value,
            "model_id": self.model_id,
            "request_id": self.request_id,
            "media_urls": list(self.media_urls),
            "thumbnail_url": self.thumbnail_url,
            "status": self.status,
            "estimated_cost_usd": round(self.estimated_cost_usd, 8),
            "dropped_options": list(self.dropped_options),
            "deduplicated": self.deduplicated,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "candidates": list(self.candidates),
        }


class CreativeEngine:
    """Generates images, video and audio with GMI's request-queue models."""

    def __init__(
        self,
        settings: Settings,
        registry: Any,
        media_client: Any,
        media_router: Any,
        budget: BudgetGuard,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.media_client = media_client
        self.media_router = media_router
        self.budget = budget

    # -- pricing ------------------------------------------------------------ #

    def estimate(
        self,
        modality: Modality,
        prompt: str = "",
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Estimate the cost of a generation before it is submitted."""
        options = options or {}
        media_models = [info.id for info in self.registry.list_media(modality)]
        candidate, considered = self.media_router.select(
            media_models,
            modality,
            prompt,
            options,
            preferred=self.settings.preferred_models,
        )
        return {
            "modality": modality.value,
            "model_id": candidate.model_id if candidate else None,
            "estimated_cost_usd": round(candidate.estimated_cost_usd, 8) if candidate else 0.0,
            "unit": candidate.spec.unit() if candidate and candidate.spec else None,
            "candidates": [c.to_dict() for c in considered],
            "note": "estimated from the model's published price, before any billing",
        }

    # -- generation --------------------------------------------------------- #

    def generate(
        self,
        prompt: str,
        *,
        modality: Modality = Modality.IMAGE,
        options: dict[str, Any] | None = None,
        approved: bool = False,
        timeout: float = 300.0,
    ) -> CreativeResult:
        """Run one creative request."""
        if not prompt or not prompt.strip():
            raise ValidationError("a creative request needs a prompt")
        options = self._clean_options(options)

        media_models = [info.id for info in self.registry.list_media(modality)]
        candidate, considered = self.media_router.select(
            media_models,
            modality,
            prompt,
            options,
            preferred=self.settings.preferred_models,
        )
        if candidate is None or candidate.spec is None:
            reasons = "; ".join(f"{c.model_id}: {c.rejected}" for c in considered if c.rejected)
            return CreativeResult(
                ok=False,
                modality=modality,
                status="no_candidate",
                error=(
                    reasons
                    or "no media model was available for this request"
                    + catalogue_error_suffix(self.registry)
                ),
                candidates=[c.to_dict() for c in considered],
            )

        estimate = candidate.estimated_cost_usd
        try:
            self.budget.enforce(
                estimate,
                approved=approved,
                description=f"a {modality.value} generation with {candidate.model_id}",
            )
        except NexusError as exc:
            return CreativeResult(
                ok=False,
                modality=modality,
                model_id=candidate.model_id,
                status="blocked",
                error=str(exc),
                estimated_cost_usd=estimate,
                candidates=[c.to_dict() for c in considered],
            )

        prepared = self._prepare_options(candidate.spec, options)

        try:
            generation = self.media_client.generate(
                candidate.model_id,
                prompt,
                prepared,
                modality=modality,
                timeout=timeout,
            )
        except NexusError as exc:
            return CreativeResult(
                ok=False,
                modality=modality,
                model_id=candidate.model_id,
                status="failed",
                error=str(exc),
                estimated_cost_usd=0.0,
                candidates=[c.to_dict() for c in considered],
            )

        if not generation.ok:
            return CreativeResult(
                ok=False,
                modality=modality,
                model_id=candidate.model_id,
                request_id=generation.request_id,
                status=generation.status or "failed",
                error=generation.error or f"generation ended with status {generation.status}",
                estimated_cost_usd=0.0,
                dropped_options=list(generation.dropped_options),
                latency_ms=generation.elapsed_ms,
                candidates=[c.to_dict() for c in considered],
            )

        self.budget.record(
            "media",
            candidate.model_id,
            generation.estimated_cost_usd,
            requests=1,
            detail=prompt[:200],
        )
        return CreativeResult(
            ok=True,
            modality=modality,
            model_id=candidate.model_id,
            request_id=generation.request_id,
            media_urls=list(generation.media_urls),
            thumbnail_url=generation.thumbnail_url,
            status=generation.status,
            estimated_cost_usd=generation.estimated_cost_usd,
            dropped_options=list(generation.dropped_options),
            deduplicated=generation.deduplicated,
            latency_ms=generation.elapsed_ms,
            candidates=[c.to_dict() for c in considered],
        )

    def generate_image(self, prompt: str, **kwargs: Any) -> CreativeResult:
        """Generate an image from a prompt."""
        return self.generate(prompt, modality=Modality.IMAGE, **kwargs)

    def edit_image(self, prompt: str, image: str, **kwargs: Any) -> CreativeResult:
        """Edit an existing image."""
        options = dict(kwargs.pop("options", {}) or {})
        options["image"] = image
        return self.generate(prompt, modality=Modality.IMAGE, options=options, **kwargs)

    def masked_edit(self, prompt: str, image: str, mask: str, **kwargs: Any) -> CreativeResult:
        """Edit only the masked region of an image."""
        options = dict(kwargs.pop("options", {}) or {})
        options["image"] = image
        options["mask"] = mask
        return self.generate(prompt, modality=Modality.IMAGE, options=options, **kwargs)

    def generate_video(self, prompt: str, **kwargs: Any) -> CreativeResult:
        """Generate a video from a prompt."""
        return self.generate(prompt, modality=Modality.VIDEO, **kwargs)

    def generate_audio(self, text: str, **kwargs: Any) -> CreativeResult:
        """Generate speech or audio from text."""
        options = dict(kwargs.pop("options", {}) or {})
        options.setdefault("text", text)
        return self.generate(text, modality=Modality.AUDIO, options=options, **kwargs)

    # -- helpers ------------------------------------------------------------ #

    @staticmethod
    def _clean_options(options: dict[str, Any] | None) -> dict[str, Any]:
        """Keep only the options NEXUS understands."""
        if not options:
            return {}
        return {
            key: value
            for key, value in options.items()
            if key in SUPPORTED_OPTIONS and value is not None
        }

    def _prepare_options(self, spec: Any, options: dict[str, Any]) -> dict[str, Any]:
        """Encode input images the way the model's own schema expects."""
        prepared: dict[str, Any] = {}
        for key, value in options.items():
            if key in ("image", "mask"):
                target = spec.resolve_option(key)
                parameter = spec.parameter(target) if target else None
                prepared[key] = _prepare_media_value(value, parameter)
            else:
                prepared[key] = value
        return prepared


def _prepare_media_value(value: Any, parameter: Any) -> Any:
    """Prepare an image or mask value for a model.

    The model's own parameter description says what it wants: a path, a URL, a
    ``data:`` URI or raw base64. Reading that description is the difference
    between a working request and a rejected one.
    """
    if not isinstance(value, str) or not value:
        return value
    if value.startswith(("http://", "https://", "data:")):
        return value
    if not os.path.exists(value):
        raise ValidationError(f"the input file does not exist: {value}")

    description = (parameter.description if parameter is not None else "").lower()
    mime, _ = mimetypes.guess_type(value)
    mime = mime or "image/png"
    with open(value, "rb") as handle:
        payload = base64.b64encode(handle.read()).decode("ascii")

    if "base64" in description and "data:" not in description:
        # The parameter documents raw base64 rather than a data URI.
        return payload
    if "path" in description and "url" not in description:
        return value
    return f"data:{mime};base64,{payload}"


__all__ = ["SUPPORTED_OPTIONS", "CreativeEngine", "CreativeResult"]
