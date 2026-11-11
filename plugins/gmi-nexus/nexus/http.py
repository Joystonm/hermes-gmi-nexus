"""A small HTTP transport with retries, timeouts and typed errors.

``httpx`` ships with Hermes, so it is used when importable. The standard
library fallback keeps the engine testable (and usable) in an environment
where Hermes' own dependency set is not installed. Both paths implement the
same :class:`Transport` interface, so tests can substitute a fake.

Every request carries an explicit ``User-Agent``. GMI's serving host sits
behind a rule that rejects the standard library's default
``Python-urllib/x.y`` agent with a Cloudflare 403, so a request without a
real agent header can fail with "access denied" before it ever reaches the
API. Setting the header here means no caller can forget to.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .errors import NexusError, RateLimitError, TimeoutError_, TransientError, error_from_response

#: The User-Agent sent with every request to GMI Cloud.
USER_AGENT = "GMI-NEXUS/1.0 (+Hermes Agent plugin)"

#: The status codes that are a redirect rather than an answer.
REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})


@dataclass(slots=True)
class HttpResponse:
    """A normalized HTTP response."""

    status: int
    body: str
    headers: dict[str, str] = field(default_factory=dict)
    elapsed_ms: int = 0
    #: The undecoded bytes of the response. ``body`` is decoded text, which
    #: is what the API calls want, but a caller that needs the original
    #: bytes — downloading an image, say — reads them from here. It is
    #: empty when the transport did not keep them.
    raw: bytes = b""

    def json(self) -> Any:
        """Parse the body as JSON, returning ``None`` when it is not JSON."""
        if not self.body:
            return None
        try:
            return json.loads(self.body)
        except (ValueError, TypeError):
            return None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Transport(Protocol):
    """The interface the engine depends on."""

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
    ) -> HttpResponse: ...


class HttpxTransport:
    """Transport backed by ``httpx`` (present in every Hermes install)."""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - httpx ships with Hermes
                raise NexusError("httpx is not available; install it or use the urllib transport") from exc
            self._client = httpx.Client(follow_redirects=True)
        return self._client

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
        client = self._ensure_client()
        started = time.perf_counter()
        try:
            response = client.request(
                method.upper(),
                url,
                headers=_with_user_agent(headers),
                json=json_body,
                data=data,
                files=files,
                timeout=timeout,
                follow_redirects=follow_redirects,
            )
        except Exception as exc:
            raise _transport_error(exc) from exc
        elapsed = int((time.perf_counter() - started) * 1000)
        return HttpResponse(
            status=response.status_code,
            body=response.text,
            headers={k.lower(): v for k, v in response.headers.items()},
            elapsed_ms=elapsed,
            raw=response.content,
        )

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            try:
                self._client.close()
            except Exception:
                pass


class UrllibTransport:
    """Standard-library transport used when ``httpx`` is unavailable."""

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
        import urllib.error
        import urllib.request

        payload: bytes | None = None
        request_headers = _with_user_agent(headers)
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        elif data is not None:
            payload = data if isinstance(data, bytes) else str(data).encode("utf-8")

        request = urllib.request.Request(url, data=payload, headers=request_headers, method=method.upper())
        started = time.perf_counter()
        try:
            with _open(request, timeout=timeout, follow_redirects=follow_redirects) as response:
                raw = response.read()
                body = raw.decode("utf-8", errors="replace")
                elapsed = int((time.perf_counter() - started) * 1000)
                return HttpResponse(
                    status=response.status,
                    body=body,
                    headers={k.lower(): v for k, v in response.headers.items()},
                    elapsed_ms=elapsed,
                    raw=raw,
                )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            elapsed = int((time.perf_counter() - started) * 1000)
            # The headers are kept: a redirect that was not followed is
            # reported through its ``Location`` header, and the caller has to
            # see it to decide whether the new host is safe to fetch.
            headers_out = {k.lower(): v for k, v in (exc.headers or {}).items()}
            return HttpResponse(
                status=exc.code,
                body=body,
                headers=headers_out,
                elapsed_ms=elapsed,
            )
        except Exception as exc:
            raise _transport_error(exc) from exc

    def close(self) -> None:
        return None


def _with_user_agent(headers: dict[str, str] | None) -> dict[str, str]:
    """Return the request headers with an explicit User-Agent applied."""
    merged = dict(headers or {})
    merged.setdefault("User-Agent", USER_AGENT)
    return merged


def _open(request: Any, *, timeout: float, follow_redirects: bool) -> Any:
    """Open a request, following redirects or not.

    ``urllib`` follows redirects by default and gives no way to inspect the
    new host before it connects to it. When a caller asks for redirects not
    to be followed, the redirect handler is replaced with one that refuses,
    so the 3xx comes back as a response and the caller can check where it
    points before fetching it.
    """
    import urllib.request

    if follow_redirects:
        return urllib.request.urlopen(request, timeout=timeout)

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        """Refuses to follow a redirect.

        Returning ``None`` from ``redirect_request`` tells urllib not to
        follow it, so the 3xx comes back as an error the caller can read —
        including its ``Location`` header — instead of a request that has
        already gone somewhere unvetted.
        """

        def redirect_request(self, *args: Any, **kwargs: Any) -> None:
            return None

    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def _transport_error(exc: Exception) -> NexusError:
    """Translate a transport-level exception into a typed NEXUS error."""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "timeout" in name or "timed out" in text:
        return TimeoutError_(f"Request timed out: {exc}")
    if "connect" in name or "connection" in text or "network" in text:
        return TransientError(f"Network error: {exc}")
    return TransientError(f"HTTP transport error: {exc}")


def default_transport() -> Transport:
    """Return the best available transport."""
    try:
        import httpx  # noqa: F401

        return HttpxTransport()
    except ImportError:
        return UrllibTransport()


class HttpClient:
    """Retries, backoff and error classification on top of a transport."""

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        max_retries: int = 2,
        timeout: float = 60.0,
        backoff_base: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._transport = transport or default_transport()
        self.max_retries = max(0, int(max_retries))
        self.timeout = float(timeout)
        self.backoff_base = float(backoff_base)
        self._sleep = sleep
        self._rng = rng

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        data: Any = None,
        files: Any = None,
        timeout: float | None = None,
        retry: bool = True,
        follow_redirects: bool = True,
    ) -> HttpResponse:
        """Perform a request, retrying transient failures with backoff.

        Retries are bounded by ``max_retries``; a 4xx is never retried because
        repeating a rejected request cannot change the answer.

        When ``follow_redirects`` is false the redirect itself comes back as
        a response instead of being followed. A caller that has to check
        where a redirect points — a URL a model supplied, say — needs to see
        it before anything connects to the new host.
        """
        effective_timeout = self.timeout if timeout is None else float(timeout)
        attempts = 0
        last_error: NexusError | None = None
        while attempts <= self.max_retries:
            attempts += 1
            try:
                response = self._transport.request(
                    method,
                    url,
                    headers=headers,
                    json_body=json_body,
                    data=data,
                    files=files,
                    timeout=effective_timeout,
                    follow_redirects=follow_redirects,
                )
            except NexusError as exc:
                last_error = exc
                if not (retry and exc.retryable and attempts <= self.max_retries):
                    raise
                self._backoff(attempts, retry_after=0.0)
                continue

            if response.ok:
                return response

            if not follow_redirects and response.status in REDIRECT_STATUSES:
                # The caller asked to see the redirect rather than follow it.
                return response

            error = error_from_response(response.status, response.body)
            if not (retry and error.retryable and attempts <= self.max_retries):
                raise error
            last_error = error
            self._backoff(attempts, retry_after=_retry_after(response))

        raise last_error or TransientError("Request failed after retries")

    def _backoff(self, attempt: int, *, retry_after: float) -> None:
        """Sleep before the next attempt: honour ``Retry-After``, else jittered backoff."""
        delay = retry_after if retry_after > 0 else self.backoff_base * (2 ** (attempt - 1))
        delay += self._rng() * 0.1
        self._sleep(min(delay, 30.0))

    def close(self) -> None:
        closer = getattr(self._transport, "close", None)
        if callable(closer):
            closer()


def _retry_after(response: HttpResponse) -> float:
    """Read the ``Retry-After`` header as a number of seconds."""
    raw = response.headers.get("retry-after")
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "HttpClient",
    "HttpResponse",
    "HttpxTransport",
    "RateLimitError",
    "REDIRECT_STATUSES",
    "Transport",
    "USER_AGENT",
    "UrllibTransport",
    "default_transport",
]
