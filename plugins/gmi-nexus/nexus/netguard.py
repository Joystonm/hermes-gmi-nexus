"""Guards for the one thing a model can ask NEXUS to fetch from the network.

NEXUS downloads a remote image when a model hands it a URL. That URL is an
argument, so it is checked rather than trusted: a URL that resolves to a
private, loopback or link-local address would turn the plugin into a way of
reading the machine's own network — the cloud metadata endpoint
(``169.254.169.254``) being the obvious prize.

The rules here are deliberately fail-closed:

* only ``http`` and ``https`` are fetched, and a URL carrying credentials in
  its authority is refused;
* the host is resolved and **every** address it resolves to must be a public
  one — a name that answers with both a public and a private address is
  refused, because which one the socket picks is not ours to decide;
* a redirect is followed only after the *new* host has passed the same
  check, so a public URL cannot bounce the fetch into the private network;
* a host that cannot be resolved is refused, because an address that cannot
  be checked is not an address that has been checked.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

from .errors import ValidationError

#: Cloud metadata hostnames. Blocked by name as well as by address.
BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {"metadata.google.internal", "metadata.goog"}
)

#: The maximum number of redirects NEXUS will follow for one image.
MAX_REDIRECTS = 3


def is_blocked_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether an address is one NEXUS must never fetch.

    Private, loopback, link-local, reserved, multicast and unspecified
    addresses are all refused, as is anything that is not globally routable.
    An IPv6 address that wraps an IPv4 one is judged by the IPv4 rules.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return is_blocked_address(mapped)
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    return not ip.is_global


def resolve_host(host: str, port: int) -> list[str]:
    """Resolve a host to the addresses it answers with.

    Split out so a test can supply its own answers without a resolver.
    """
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        addresses.append(str(sockaddr[0]))
    return addresses


def assert_public_url(url: str) -> str:
    """Return the URL when it is safe to fetch, and refuse it otherwise.

    Raises :class:`ValidationError` with a message written for the person
    reading it.
    """
    raw = str(url or "").strip()
    if not raw:
        raise ValidationError("the URL is empty")

    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValidationError(f"the URL could not be parsed: {exc}") from exc

    scheme = (parsed.scheme or "").strip().lower()
    if scheme not in ("http", "https"):
        raise ValidationError(
            f"only http and https URLs can be fetched, not {scheme or 'an empty scheme'!r}"
        )

    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise ValidationError("a URL carrying credentials in it is refused")

    host = (parsed.hostname or "").strip()
    if not host:
        raise ValidationError("the URL has no host")
    if host.lower() in BLOCKED_HOSTNAMES:
        raise ValidationError(f"{host} is a cloud metadata endpoint and is refused")

    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise ValidationError(f"the URL has an unusable port: {exc}") from exc

    try:
        addresses = resolve_host(host, port)
    except (socket.gaierror, OSError) as exc:
        raise ValidationError(
            f"the host {host!r} could not be resolved, so its address cannot be checked"
        ) from exc

    if not addresses:
        raise ValidationError(f"the host {host!r} resolved to no address")

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise ValidationError(f"the host {host!r} resolved to an unusable address") from None
        if is_blocked_address(ip):
            raise ValidationError(
                f"{host} resolves to {ip}, which is an internal address, so it is refused"
            )

    return raw


def is_redirect(status: int) -> bool:
    """Whether a status code is a redirect."""
    return status in (301, 302, 303, 307, 308)


def fetch_public(url: str, client: Any, timeout: float) -> tuple[str, Any] | None:
    """Fetch a URL, checking every hop on the way.

    Returns ``(final_url, response)``, or ``None`` when the fetch itself
    failed — a connection error, a 404, a timeout. Those are failures, and
    the caller may fall back to sending the URL along as it stands.

    A hop that is not a public address is a different thing: that is a
    refusal, and it raises :class:`ValidationError`. The difference matters
    to the caller, which must not treat "this points inside your network" as
    "the download did not work".
    """
    from urllib.parse import urljoin

    current = url
    for _ in range(MAX_REDIRECTS + 1):
        # Raises when this hop is not safe to fetch.
        assert_public_url(current)
        try:
            response = client.request(
                "GET",
                current,
                headers={"Accept": "image/*"},
                timeout=timeout,
                follow_redirects=False,
            )
        except Exception:
            return None
        if not is_redirect(response.status):
            return current, response
        location = str(response.headers.get("location") or "").strip()
        if not location:
            return None
        current = urljoin(current, location)

    raise ValidationError(
        f"the URL redirected more than {MAX_REDIRECTS} times, so it is refused"
    )


__all__ = [
    "BLOCKED_HOSTNAMES",
    "MAX_REDIRECTS",
    "assert_public_url",
    "fetch_public",
    "is_blocked_address",
    "is_redirect",
    "resolve_host",
]
