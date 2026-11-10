"""Per-model media metadata: parameter schemas, modality and pricing.

GMI's request queue publishes a full description of every media model at
``GET {media_base}/models/{id}``:

``model_type``
    ``image``, ``video`` or ``audio`` — the authoritative modality.
``parameters``
    The model's own parameter list, with types, required flags, defaults and
    enum options.
``price_info``
    A unit and a price in micro-USD (``42000`` → ``$0.042``).
``percentage_discount``
    The account's discount on this model.

The parameter names are *not* consistent across models. One image model takes
``image`` as its input image, another ``image_path``, another ``images``, a
fourth ``input_reference``; video models spell the duration ``duration``,
``durationSeconds`` or ``seconds``. So a payload is built from the model's own
schema, and the user's options are mapped onto it by meaning rather than by
name. Anything the model does not accept is reported as dropped rather than
silently ignored.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from .errors import NexusError, ValidationError
from .http import HttpClient
from .models import Modality

#: How long a fetched model description stays fresh.
SPEC_TTL_SECONDS = 3600.0

#: Prices in the catalogue are expressed in micro-USD.
MICRO_USD = 1_000_000.0

#: Semantic option name -> the parameter names a model might use for it.
#: The first name the model actually declares wins.
OPTION_ALIASES: dict[str, tuple[str, ...]] = {
    "aspect_ratio": ("aspect_ratio", "aspectRatio", "ratio", "size", "image_size"),
    "size": ("size", "image_size", "resolution"),
    "width": ("width",),
    "height": ("height",),
    "duration": ("duration", "durationSeconds", "duration_seconds", "seconds", "length"),
    "resolution": ("resolution", "size"),
    "image": ("image", "image_path", "images", "input_reference", "reference_image",
              "image_url", "first_frame_image", "first_frame", "image_path_url"),
    "mask": ("mask", "mask_image", "mask_path"),
    "negative_prompt": ("negative_prompt", "negativePrompt"),
    "quality": ("quality",),
    "n": ("n", "max_images", "num_images", "number_of_images"),
    "seed": ("seed",),
    "voice": ("voice", "voice_id"),
    "text": ("text", "prompt"),
    "style": ("style", "style_id"),
    "output_format": ("output_format", "format", "image_output_format"),
    "steps": ("num_inference_steps", "steps"),
    "guidance": ("guidance_scale", "guidance"),
    "fps": ("fps",),
    "speed": ("speed",),
    "prompt_optimizer": ("prompt_optimizer",),
    "generate_audio": ("generate_audio", "generateAudio", "generate_audio_switch"),
}

#: Aspect ratio -> the pixel size that expresses it.
ASPECT_SIZES: dict[str, str] = {
    "1:1": "1024x1024",
    "square": "1024x1024",
    "4:3": "1152x864",
    "3:4": "864x1152",
    "3:2": "1216x832",
    "2:3": "832x1216",
    "16:9": "1344x768",
    "9:16": "768x1344",
    "21:9": "1536x656",
}

#: Fallback duration for video models when the caller does not give one.
DEFAULT_VIDEO_DURATION = 5


@dataclass(slots=True)
class MediaParameter:
    """One parameter a media model declares."""

    name: str
    type: str = "string"
    required: bool = False
    default: Any = None
    options: list[str] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "default": self.default,
            "options": list(self.options),
            "description": self.description,
        }


@dataclass(slots=True)
class MediaModelSpec:
    """The published description of one media model."""

    id: str
    display_name: str = ""
    modality: Modality = Modality.IMAGE
    provider: str = ""
    status: str = ""
    brief_description: str = ""
    parameters: list[MediaParameter] = field(default_factory=list)
    price_info: dict[str, Any] = field(default_factory=dict)
    discount: float = 0.0
    fetched_at: float = 0.0
    #: True when the model could not be described and only generic defaults apply.
    approximate: bool = False

    # -- parameter helpers -------------------------------------------------- #

    def parameter_names(self) -> list[str]:
        return [p.name for p in self.parameters]

    def parameter(self, name: str) -> MediaParameter | None:
        for param in self.parameters:
            if param.name == name:
                return param
        return None

    def resolve_option(self, semantic: str) -> str | None:
        """Return the parameter name this model uses for a semantic option."""
        for candidate in OPTION_ALIASES.get(semantic, (semantic,)):
            if self.parameter(candidate) is not None:
                return candidate
        return None

    # -- pricing ------------------------------------------------------------ #

    def unit(self) -> str:
        """Return the pricing unit: image, request, second, character, ..."""
        info = self.price_info or {}
        unit = str(info.get("unit") or "").lower()
        if unit:
            return unit
        pricing_type = str(info.get("pricing_type") or "").lower()
        for token in ("second", "character", "image", "request", "generation", "token"):
            if token in pricing_type:
                return token
        return "request"

    def unit_price_usd(self) -> float:
        """Return the price of one unit, in USD."""
        info = self.price_info or {}
        price = info.get("price")
        if price is None:
            chart = info.get("price_chart")
            if isinstance(chart, dict):
                price = chart.get("rate")
        try:
            value = float(price)
        except (TypeError, ValueError):
            return 0.0
        return value / MICRO_USD

    def estimate_cost(
        self,
        *,
        count: int = 1,
        duration: float | None = None,
        text: str = "",
    ) -> float:
        """Estimate the USD cost of one generation at list price.

        The account's published discount is applied when the catalogue
        reports one. The result is an estimate of what the call is expected
        to cost, not a statement of what was billed.
        """
        unit = self.unit()
        price = self.unit_price_usd()
        if price <= 0:
            return 0.0

        multiplier = 1.0
        if unit == "second":
            seconds = duration if duration is not None else DEFAULT_VIDEO_DURATION
            multiplier = float(seconds)
        elif unit == "character":
            # Priced per 1000 characters.
            multiplier = max(1.0, math.ceil(len(text or "") / 1000.0))
        else:
            multiplier = float(max(1, count))

        total = price * multiplier
        discount = self.discount
        if discount and 0 < discount < 1:
            total *= 1.0 - discount
        return round(total, 8)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name or self.id,
            "modality": self.modality.value,
            "provider": self.provider,
            "status": self.status,
            "brief_description": self.brief_description,
            "parameters": [p.to_dict() for p in self.parameters],
            "price_info": dict(self.price_info),
            "unit": self.unit(),
            "unit_price_usd": self.unit_price_usd(),
            "discount": self.discount,
            "approximate": self.approximate,
            "fetched_at": self.fetched_at,
        }


def _modality_from_model_type(model_type: Any) -> Modality:
    """Map the catalogue's ``model_type`` onto a modality."""
    value = str(model_type or "").lower()
    if value.startswith("video"):
        return Modality.VIDEO
    if value.startswith("audio") or value.startswith("speech") or value.startswith("music"):
        return Modality.AUDIO
    return Modality.IMAGE


def _parse_parameters(raw: Any) -> list[MediaParameter]:
    """Parse the catalogue's parameter list."""
    parameters: list[MediaParameter] = []
    if not isinstance(raw, list):
        return parameters
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        options: list[str] = []
        raw_options = entry.get("options")
        if isinstance(raw_options, list):
            for option in raw_options:
                if isinstance(option, dict):
                    value = option.get("value")
                    if value is not None:
                        options.append(str(value))
                elif option is not None:
                    options.append(str(option))
        parameters.append(
            MediaParameter(
                name=name,
                type=str(entry.get("type") or "string").lower(),
                required=bool(entry.get("required")),
                default=entry.get("default_value"),
                options=options,
                description=str(entry.get("description") or ""),
            )
        )
    return parameters


def parse_spec(payload: dict[str, Any], model_id: str) -> MediaModelSpec:
    """Build a :class:`MediaModelSpec` from the catalogue's response."""
    raw_discount = payload.get("percentage_discount")
    try:
        discount = float(raw_discount) if raw_discount is not None else 0.0
    except (TypeError, ValueError):
        discount = 0.0
    # The field is a percentage; anything above 1 is already a percentage.
    if discount > 1:
        discount = discount / 100.0

    return MediaModelSpec(
        id=str(payload.get("model") or model_id),
        display_name=str(payload.get("display_name") or model_id),
        modality=_modality_from_model_type(payload.get("model_type")),
        provider=str(payload.get("external_provider") or ""),
        status=str(payload.get("status") or ""),
        brief_description=str(payload.get("brief_description") or ""),
        parameters=_parse_parameters(payload.get("parameters")),
        price_info=payload.get("price_info") if isinstance(payload.get("price_info"), dict) else {},
        discount=discount,
        fetched_at=time.time(),
    )


class MediaCatalog:
    """Fetches and caches per-model media descriptions."""

    def __init__(self, base_url: str, client: HttpClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client
        self._lock = threading.RLock()
        self._specs: dict[str, MediaModelSpec] = {}

    def _headers(self) -> dict[str, str]:
        from .config import require_api_key

        return {
            "Authorization": f"Bearer {require_api_key()}",
            "Accept": "application/json",
        }

    def get_spec(self, model_id: str, *, refresh: bool = False) -> MediaModelSpec | None:
        """Return the description of one media model, or ``None`` if unavailable.

        A model that cannot be described is not usable: without its parameter
        schema NEXUS cannot build a correct payload, so it is reported as
        unavailable rather than guessed at.
        """
        with self._lock:
            cached = self._specs.get(model_id)
            if cached is not None and not refresh:
                if (time.time() - cached.fetched_at) < SPEC_TTL_SECONDS:
                    return cached

        try:
            response = self.client.request(
                "GET", f"{self.base_url}/models/{model_id}", headers=self._headers()
            )
        except NexusError:
            return None
        payload = response.json()
        if not isinstance(payload, dict):
            return None
        spec = parse_spec(payload, model_id)
        if not spec.parameters:
            spec.approximate = True
        with self._lock:
            self._specs[model_id] = spec
        return spec

    def require_spec(self, model_id: str) -> MediaModelSpec:
        """Return the description of one media model, raising when unavailable."""
        spec = self.get_spec(model_id)
        if spec is None:
            raise ValidationError(
                f"no published description is available for media model {model_id!r}; "
                "NEXUS will not guess its parameters"
            )
        return spec

    def cached(self) -> list[MediaModelSpec]:
        """Return the descriptions fetched so far."""
        with self._lock:
            return list(self._specs.values())


# --------------------------------------------------------------------------- #
# Payload construction
# --------------------------------------------------------------------------- #


def _coerce(parameter: MediaParameter, value: Any) -> Any:
    """Coerce a value to the parameter's declared type, or raise ``ValueError``."""
    kind = parameter.type
    if kind in ("integer", "int"):
        return int(value)
    if kind in ("number", "float"):
        return float(value)
    if kind in ("boolean", "bool"):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if kind == "enum":
        text = str(value)
        for option in parameter.options:
            if option == text:
                return text
        for option in parameter.options:
            if option.lower() == text.lower():
                return option
        raise ValueError(f"{text!r} is not one of {parameter.options}")
    return value


def _size_for_aspect(parameter: MediaParameter, aspect: str) -> str | None:
    """Map an aspect ratio onto a size the model actually accepts."""
    preferred = ASPECT_SIZES.get(aspect)
    if not parameter.options:
        return preferred
    if preferred and preferred in parameter.options:
        return preferred
    if aspect in parameter.options:
        return aspect
    # Pick the allowed size whose aspect ratio is closest to the request.
    target = _ratio_value(aspect)
    best: tuple[float, str] | None = None
    for option in parameter.options:
        if "x" not in option.lower():
            continue
        try:
            width, height = option.lower().split("x", 1)
            ratio = float(width) / float(height)
        except (TypeError, ValueError):
            continue
        distance = abs(ratio - target)
        if best is None or distance < best[0]:
            best = (distance, option)
    return best[1] if best else None


def _ratio_value(aspect: str) -> float:
    """Return an aspect ratio as a float."""
    try:
        width, height = aspect.split(":", 1)
        return float(width) / float(height)
    except (ValueError, ZeroDivisionError):
        return 1.0


def build_payload(
    spec: MediaModelSpec,
    prompt: str,
    options: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Build the payload for one media model from its own schema.

    Returns the payload and the list of options that were dropped because the
    model does not accept them.
    """
    options = dict(options or {})
    names = spec.parameter_names()
    dropped: list[str] = []
    payload: dict[str, Any] = {}

    # The prompt goes into whichever parameter the model calls the prompt.
    prompt_parameter = spec.resolve_option("text") or "prompt"
    if prompt_parameter not in names:
        prompt_parameter = "prompt" if "prompt" in names else (names[0] if names else "prompt")
    if not prompt or not prompt.strip():
        raise ValidationError("a media request needs a prompt")
    payload[prompt_parameter] = prompt.strip()

    # Options are matched by meaning, not by name.
    for semantic, value in options.items():
        if value is None:
            continue
        target = spec.resolve_option(semantic)
        if target is None:
            dropped.append(semantic)
            continue
        parameter = spec.parameter(target)
        if parameter is None:
            dropped.append(semantic)
            continue

        final_value = value
        if semantic == "aspect_ratio" and parameter.options:
            # The model names its output sizes in its own vocabulary — a ratio
            # like "16:9" has to become the pixel size it actually accepts.
            mapped = _size_for_aspect(parameter, str(value))
            if mapped is None:
                dropped.append(semantic)
                continue
            final_value = mapped
        elif semantic == "aspect_ratio" and target in ("size", "image_size", "resolution"):
            mapped = ASPECT_SIZES.get(str(value), str(value))
            if parameter.options and mapped not in parameter.options:
                mapped = _size_for_aspect(parameter, str(value))
                if mapped is None:
                    dropped.append(semantic)
                    continue
            final_value = mapped

        try:
            payload[target] = _coerce(parameter, final_value)
        except (TypeError, ValueError):
            dropped.append(semantic)

    # Fill in the model's own defaults for anything still missing.
    for parameter in spec.parameters:
        if parameter.name in payload:
            continue
        if parameter.required:
            continue
        if parameter.default not in (None, ""):
            payload[parameter.name] = parameter.default

    return payload, dropped


def missing_required(spec: MediaModelSpec, payload: dict[str, Any]) -> list[str]:
    """Return the required parameters the payload does not supply."""
    return [
        parameter.name
        for parameter in spec.parameters
        if parameter.required and not payload.get(parameter.name)
    ]


__all__ = [
    "ASPECT_SIZES",
    "DEFAULT_VIDEO_DURATION",
    "MICRO_USD",
    "OPTION_ALIASES",
    "SPEC_TTL_SECONDS",
    "MediaCatalog",
    "MediaModelSpec",
    "MediaParameter",
    "build_payload",
    "missing_required",
    "parse_spec",
]
