"""GMI Cloud media client for image, video and audio generation.

The request-queue API takes a model id and a *model-specific* payload::

    POST {base}/requests
    {"model": "...", "payload": {...}}

and answers with a request id that is polled until it succeeds::

    GET {base}/requests/{request_id}
    {"request_id": ..., "status": "success",
     "outcome": {"media_urls": [{"id": ..., "url": ...}], ...}}

The payload shape is **not** the same across models, and GMI publishes each
model's own parameter list. So every payload is built from that published
schema by :mod:`nexus.media_catalog` — NEXUS never reuses one model's
parameters for another, and never guesses a parameter name.

Generation is the most expensive thing NEXUS does, so an identical payload
that was already submitted is answered from the local cache instead of being
submitted (and billed) a second time.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..errors import NexusError, TimeoutError_, ValidationError
from ..http import HttpClient
from ..media_catalog import MediaCatalog, MediaModelSpec, build_payload, missing_required
from ..models import Modality

#: Terminal and in-flight statuses of the request queue.
STATUS_QUEUED = "queued"
STATUS_PROCESSING = "processing"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

#: Statuses that mean the request will never produce media.
FAILED_STATUSES = (STATUS_FAILED, STATUS_CANCELLED)


@dataclass(slots=True)
class MediaResult:
    """The outcome of one media generation request."""

    request_id: str
    model_id: str
    status: str
    media_urls: list[str] = field(default_factory=list)
    thumbnail_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    elapsed_ms: int = 0
    #: True when the request was answered from the local duplicate cache.
    deduplicated: bool = False
    #: The estimated USD cost of this generation at list price.
    estimated_cost_usd: float = 0.0
    #: Options the model's schema did not accept.
    dropped_options: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS and bool(self.media_urls)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "model_id": self.model_id,
            "status": self.status,
            "media_urls": list(self.media_urls),
            "thumbnail_url": self.thumbnail_url,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "deduplicated": self.deduplicated,
            "estimated_cost_usd": round(self.estimated_cost_usd, 8),
            "dropped_options": list(self.dropped_options),
        }


class GmiMediaClient:
    """Submits and polls GMI media generation requests."""

    def __init__(
        self,
        base_url: str,
        client: HttpClient,
        *,
        catalog: MediaCatalog | None = None,
        poll_interval: float = 3.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client
        self.catalog = catalog or MediaCatalog(base_url, client)
        self.poll_interval = poll_interval

    def _headers(self) -> dict[str, str]:
        from ..config import require_api_key

        return {
            "Authorization": f"Bearer {require_api_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # -- submission --------------------------------------------------------- #

    def submit(self, model_id: str, payload: dict[str, Any]) -> str:
        """Submit a generation request and return its request id."""
        body = {"model": model_id, "payload": payload}
        response = self.client.request(
            "POST", f"{self.base_url}/requests", headers=self._headers(), json_body=body
        )
        data = response.json()
        if not isinstance(data, dict):
            raise NexusError(f"model {model_id} returned a non-JSON submission response")
        request_id = str(data.get("request_id") or data.get("id") or "").strip()
        if not request_id:
            raise NexusError(f"model {model_id} did not return a request id")
        return request_id

    # -- polling ------------------------------------------------------------ #

    def get_request(self, request_id: str) -> dict[str, Any]:
        """Read the current state of one request."""
        response = self.client.request(
            "GET", f"{self.base_url}/requests/{request_id}", headers=self._headers()
        )
        data = response.json()
        if not isinstance(data, dict):
            raise NexusError(f"request {request_id} returned a non-JSON response")
        return data

    def wait(
        self,
        request_id: str,
        *,
        timeout: float = 300.0,
        poll_interval: float | None = None,
    ) -> MediaResult:
        """Poll a request until it finishes, fails, or the timeout expires."""
        interval = self.poll_interval if poll_interval is None else poll_interval
        deadline = time.monotonic() + max(1.0, float(timeout))
        started = time.perf_counter()

        while True:
            payload = self.get_request(request_id)
            status = str(payload.get("status") or "").lower()
            elapsed = int((time.perf_counter() - started) * 1000)
            if status == STATUS_SUCCESS:
                return self._to_result(request_id, payload, elapsed)
            if status in FAILED_STATUSES:
                result = self._to_result(request_id, payload, elapsed)
                result.status = status
                result.error = result.error or f"generation {status}"
                return result
            if time.monotonic() >= deadline:
                raise TimeoutError_(
                    f"generation request {request_id} did not finish within {int(timeout)}s "
                    f"(last status: {status or 'unknown'})"
                )
            time.sleep(interval)

    # -- the main entry point ---------------------------------------------- #

    def estimate_cost(
        self,
        model_id: str,
        options: dict[str, Any] | None = None,
        *,
        spec: MediaModelSpec | None = None,
    ) -> float:
        """Estimate the USD cost of one generation of this model."""
        spec = spec or self.catalog.get_spec(model_id)
        if spec is None:
            return 0.0
        options = options or {}
        count = int(options.get("n") or 1)
        duration = options.get("duration")
        try:
            duration = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        text = str(options.get("text") or options.get("prompt") or "")
        return spec.estimate_cost(count=count, duration=duration, text=text)

    def generate(
        self,
        model_id: str,
        prompt: str,
        options: dict[str, Any] | None = None,
        *,
        modality: Modality = Modality.IMAGE,
        timeout: float = 300.0,
        allow_dedupe: bool = True,
    ) -> MediaResult:
        """Run one generation end to end."""
        spec = self.catalog.require_spec(model_id)
        payload, dropped = build_payload(spec, prompt, options)

        missing = missing_required(spec, payload)
        if missing:
            raise ValidationError(
                f"{model_id} requires parameter(s) {', '.join(missing)} that NEXUS did not supply"
            )

        digest = _payload_hash(model_id, payload)
        if allow_dedupe:
            existing = _find_duplicate(digest)
            if existing is not None:
                existing.dropped_options = dropped
                existing.estimated_cost_usd = 0.0  # nothing was spent the second time
                return existing

        estimated = spec.estimate_cost(
            count=int((options or {}).get("n") or 1),
            duration=_as_float((options or {}).get("duration")),
            text=str((options or {}).get("text") or prompt),
        )

        request_id = self.submit(model_id, payload)
        _remember(digest, request_id, model_id)
        result = self.wait(request_id, timeout=timeout)
        result.dropped_options = dropped
        result.estimated_cost_usd = estimated if result.ok else 0.0
        result.raw.setdefault("payload_hash", digest)
        result.raw.setdefault("payload", payload)

        try:
            from .. import storage

            storage.update_media_request(request_id, result.status, result.media_urls, estimated)
        except Exception:
            pass
        return result

    # -- helpers ------------------------------------------------------------ #

    @staticmethod
    def _to_result(request_id: str, payload: dict[str, Any], elapsed_ms: int) -> MediaResult:
        outcome = payload.get("outcome") if isinstance(payload.get("outcome"), dict) else {}
        urls: list[str] = []
        for item in outcome.get("media_urls") or []:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict):
                url = item.get("url") or item.get("media_url") or item.get("image_url")
                if url:
                    urls.append(str(url))
        thumbnail = outcome.get("thumbnail_image_url") or outcome.get("thumbnail_url")
        error = payload.get("error") or outcome.get("error")
        if isinstance(error, dict):
            error = error.get("message") or json.dumps(error)
        return MediaResult(
            request_id=request_id,
            model_id=str(payload.get("model") or ""),
            status=str(payload.get("status") or "").lower(),
            media_urls=urls,
            thumbnail_url=str(thumbnail) if thumbnail else None,
            raw=payload,
            error=str(error) if error else None,
            elapsed_ms=elapsed_ms,
        )

    def list_requests(self, limit: int = 20) -> list[dict[str, Any]]:
        """List recent generation requests."""
        response = self.client.request("GET", f"{self.base_url}/requests", headers=self._headers())
        data = response.json()
        if isinstance(data, dict):
            items = data.get("data") or data.get("requests") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []
        return [item for item in items if isinstance(item, dict)][:limit]


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Duplicate suppression
# --------------------------------------------------------------------------- #

_DUPLICATES: dict[str, str] = {}


def _payload_hash(model_id: str, payload: dict[str, Any]) -> str:
    """Return a stable hash of a model plus payload."""
    import hashlib

    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(f"{model_id}|{canonical}".encode("utf-8")).hexdigest()


def _remember(digest: str, request_id: str, model_id: str) -> None:
    """Record a submitted request against its payload hash."""
    from .. import storage

    _DUPLICATES[digest] = request_id
    try:
        storage.record_media_request(request_id, model_id, digest)
    except Exception:
        pass


def _find_duplicate(digest: str) -> MediaResult | None:
    """Return a completed result for an identical earlier request, if any."""
    from .. import storage

    request_id = _DUPLICATES.get(digest)
    if request_id is None:
        try:
            request_id = storage.find_media_request(digest)
        except Exception:
            return None
    if not request_id:
        return None
    try:
        record = storage.media_request(request_id)
    except Exception:
        return None
    if not record:
        return None
    if record.get("status") != STATUS_SUCCESS:
        return None
    urls = record.get("media_urls") or []
    if not urls:
        return None
    return MediaResult(
        request_id=request_id,
        model_id=str(record.get("model_id") or ""),
        status=STATUS_SUCCESS,
        media_urls=[str(u) for u in urls],
        deduplicated=True,
    )


def clear_duplicates() -> None:
    """Forget the in-memory duplicate cache (used by tests)."""
    _DUPLICATES.clear()


__all__ = [
    "FAILED_STATUSES",
    "STATUS_CANCELLED",
    "STATUS_FAILED",
    "STATUS_PROCESSING",
    "STATUS_QUEUED",
    "STATUS_SUCCESS",
    "GmiMediaClient",
    "MediaResult",
    "clear_duplicates",
]
