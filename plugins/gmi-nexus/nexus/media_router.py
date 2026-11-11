"""Choosing a media model for a request.

The request-queue catalogue lists every media model id but says nothing about
which one to use. NEXUS narrows the field in three passes:

1. **Filter.** Test harnesses, probe models, UUID-named private workflows and
   the catalogue's own scratch entries are dropped. This filter is a
   heuristic on the id, and it is labelled as one.
2. **Shortlist.** The remaining ids are ranked by family and version so that a
   small number of plausible candidates is left. Fetching every model's
   description would mean hundreds of requests.
3. **Decide.** The shortlist's published descriptions are read and the final
   choice is made on real facts: whether the model is active, whether it
   produces the modality that was asked for, what it actually costs, and
   whether it accepts the parameters the request needs.

A model whose description cannot be read is never selected — without its
parameter schema NEXUS cannot build a valid payload for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .media_catalog import MediaCatalog, MediaModelSpec
from .models import Modality

#: Id fragments that mark a catalogue entry as a test harness or scratch model.
_NOISE = re.compile(
    r"(test|capture|probe|ssrf|bypass|dummy|sample|debug|tmp|temp|zzz|"
    r"-1009|-1009b|custom-vtx|workflow-capture|zero-cost|dev-test)",
    re.IGNORECASE,
)

#: A bare UUID is a private workflow, not a product model.
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

#: Ids that look like a person's own asset name rather than a product model.
_PERSONAL = re.compile(r"_\d{2,4}$|^(cat|new|host|fashion|knitted|creator|christmas)\b", re.IGNORECASE)

#: Provider families NEXUS knows produce good results, best first. This is a
#: *tie-breaker*, not a whitelist: any active model can still win on cost.
PROVIDER_PREFERENCE: dict[str, tuple[str, ...]] = {
    Modality.IMAGE.value: ("gpt-image", "seedream", "flux", "luma-uni", "ideogram", "reve", "bria"),
    Modality.VIDEO.value: ("veo", "seedance", "kling", "wan", "sora", "ltx", "pixverse", "vidu"),
    Modality.AUDIO.value: ("elevenlabs", "minimax", "inworld", "chatterbox"),
}

#: Id fragments that mark a cheaper variant of a family.
_ECONOMY = re.compile(r"(lite|turbo|fast|mini|flash|preview|low)", re.IGNORECASE)

#: Id fragments that mark a top-tier variant of a family.
_PREMIUM = re.compile(r"(pro|master|max|ultra)", re.IGNORECASE)


@dataclass(slots=True)
class MediaCandidate:
    """One media model considered for a request."""

    model_id: str
    spec: MediaModelSpec | None = None
    score: float = 0.0
    estimated_cost_usd: float = 0.0
    reasons: list[str] = field(default_factory=list)
    rejected: str = ""

    def to_dict(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "model_id": self.model_id,
            "score": round(self.score, 4),
            "estimated_cost_usd": round(self.estimated_cost_usd, 8),
            "reasons": list(self.reasons),
        }
        if self.rejected:
            item["rejected"] = self.rejected
        if self.spec is not None:
            item["modality"] = self.spec.modality.value
            item["status"] = self.spec.status
            item["provider"] = self.spec.provider
            item["unit_price_usd"] = (
                self.spec.unit_price_usd()
                if self.spec.has_published_price()
                else None
            )
            item["unit"] = self.spec.unit()
        return item


def is_product_model(model_id: str) -> bool:
    """Return whether an id looks like a product model rather than noise."""
    candidate = (model_id or "").strip()
    if not candidate:
        return False
    if _UUID.match(candidate):
        return False
    if _NOISE.search(candidate):
        return False
    if _PERSONAL.search(candidate):
        return False
    return True


#: Tokens that positively identify a model's modality.
_VIDEO_TOKENS = (
    "video", "i2v", "t2v", "r2v", "kling", "veo", "seedance", "wan2", "wan3", "ltx",
    "hunyuan1", "pixverse", "vidu", "skyreels", "sora", "mochi", "hailuo", "kling",
)
_AUDIO_TOKENS = (
    "tts", "speech", "audio", "voice", "music", "sonic", "chatterbox", "inworld", "melody",
)
_IMAGE_TOKENS = (
    "image", "img", "flux", "stable-diffusion", "seedream", "midjourney", "ideogram",
    "dall", "recraft", "playground", "sdxl", "kolors", "ideogram",
)


def likely_modality(model_id: str) -> Modality | None:
    """Return the modality an id points at, or ``None`` when it says nothing.

    A name like ``Minimax-Hailuo-2.3`` is a video model but carries no token
    that proves it, so the honest answer is "unknown" rather than a guess.
    Callers treat an unknown id as worth describing and let the published
    ``model_type`` make the real decision.
    """
    lowered = (model_id or "").lower()
    if any(token in lowered for token in _VIDEO_TOKENS):
        return Modality.VIDEO
    if any(token in lowered for token in _AUDIO_TOKENS):
        return Modality.AUDIO
    if any(token in lowered for token in _IMAGE_TOKENS):
        return Modality.IMAGE
    return None


def heuristic_modality(model_id: str) -> Modality:
    """Infer a model's modality from its id, defaulting to image.

    Used only to shortlist candidates before their published descriptions are
    read; the published ``model_type`` is what actually decides.
    """
    return likely_modality(model_id) or Modality.IMAGE


def _shortlist_score(model_id: str, modality: Modality) -> float:
    """Score an id on family affinity, before its description is read."""
    lowered = model_id.lower()
    score = 0.0
    preferences = PROVIDER_PREFERENCE.get(modality.value, ())
    for index, family in enumerate(preferences):
        if family in lowered:
            score += 10.0 - index
            break
    if _PREMIUM.search(lowered):
        score += 1.0
    if _ECONOMY.search(lowered):
        score -= 0.5
    # Prefer names with a version number: they are the maintained releases.
    if re.search(r"[-_v]\d", lowered):
        score += 0.5
    return score


class MediaRouter:
    """Selects a media model for a request."""

    def __init__(self, catalog: MediaCatalog, *, shortlist_size: int = 8) -> None:
        self.catalog = catalog
        self.shortlist_size = shortlist_size

    def shortlist(
        self,
        model_ids: Sequence[str],
        modality: Modality,
        *,
        preferred: Iterable[str] = (),
    ) -> list[str]:
        """Return the ids worth describing, best first."""
        preferred_set = {p.strip().lower() for p in preferred if p and p.strip()}
        scored: list[tuple[float, str]] = []
        for model_id in model_ids:
            if not is_product_model(model_id):
                continue
            likely = likely_modality(model_id)
            if likely is not None and likely is not modality:
                # The id names a different modality outright, so it cannot be
                # the answer. An id that says nothing is kept: its published
                # description is what decides.
                continue
            score = _shortlist_score(model_id, modality)
            if likely is None:
                score -= 0.5  # the id gave no signal, so it ranks lower
            if model_id.lower() in preferred_set:
                score += 100.0
            scored.append((score, model_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [model_id for _, model_id in scored[: self.shortlist_size]]

    def select(
        self,
        model_ids: Sequence[str],
        modality: Modality,
        prompt: str,
        options: dict[str, Any] | None = None,
        *,
        preferred: Iterable[str] = (),
        max_cost_usd: float | None = None,
    ) -> tuple[MediaCandidate | None, list[MediaCandidate]]:
        """Choose a media model, returning the winner and the considered set."""
        options = options or {}
        shortlist = self.shortlist(model_ids, modality, preferred=preferred)
        considered: list[MediaCandidate] = []
        best: MediaCandidate | None = None

        for model_id in shortlist:
            spec = self.catalog.get_spec(model_id)
            candidate = MediaCandidate(model_id=model_id, spec=spec)
            if spec is None:
                candidate.rejected = "no published description is available for this model"
                considered.append(candidate)
                continue
            if spec.modality is not modality:
                candidate.rejected = (
                    f"publishes {spec.modality.value} output, not {modality.value}"
                )
                considered.append(candidate)
                continue

            if not spec.has_published_price():
                # The catalogue publishes no price for this model, so the
                # cost of using it cannot be bounded. NEXUS will not spend
                # on a price it cannot see, so the model is passed over
                # rather than treated as free.
                candidate.rejected = (
                    "the catalogue publishes no price for this model, so the "
                    "cost of using it cannot be bounded"
                )
                considered.append(candidate)
                continue

            cost = spec.estimate_cost(
                count=int(options.get("n") or 1),
                duration=_as_float(options.get("duration")),
                text=str(options.get("text") or prompt),
            )
            candidate.estimated_cost_usd = cost

            score = 0.0
            if spec.status == "active":
                score += 3.0
                candidate.reasons.append("model is active in the catalogue")
            else:
                candidate.reasons.append(f"model status is {spec.status or 'unknown'}")
            preferences = PROVIDER_PREFERENCE.get(modality.value, ())
            if spec.provider and spec.provider in preferences:
                score += 2.0
                candidate.reasons.append(f"provider {spec.provider} is a known {modality.value} provider")
            if cost > 0:
                score += 1.0 / (1.0 + cost)
                candidate.reasons.append(f"estimated ${cost:.6f} at list price")
            else:
                candidate.reasons.append("the published price for this model is zero")
            if spec.parameters:
                score += 0.5
            else:
                candidate.rejected = "the published description lists no parameters"

            if max_cost_usd is not None and cost > max_cost_usd:
                candidate.rejected = (
                    f"estimated ${cost:.6f} is over the ${max_cost_usd:.6f} available"
                )
                considered.append(candidate)
                continue

            candidate.score = score
            considered.append(candidate)
            if best is None or candidate.score > best.score:
                best = candidate

        return best, considered


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "ECONOMY",
    "NOISE",
    "PREMIUM",
    "PROVIDER_PREFERENCE",
    "MediaCandidate",
    "MediaRouter",
    "heuristic_modality",
    "is_product_model",
    "likely_modality",
]
