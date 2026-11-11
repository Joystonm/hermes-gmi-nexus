"""What NEXUS is allowed to read from the machine, and to fetch from it.

These tests answer the third and fourth blockers raised on the Hermes
catalogue pull request:

3. *``nexus_media`` uploads any local path given as an image or a mask* — a
   named path could be the profile's own ``.env``;
4. *vision fetches any model-supplied URL locally with redirects on* — a
   named URL could point at the cloud metadata endpoint.

Every test here is offline. Nothing reaches GMI Cloud, and nothing spends.
"""

from __future__ import annotations

import base64
import os

import pytest

from nexus.errors import ValidationError
from nexus.http import HttpClient, HttpResponse
from nexus.models import Modality
from nexus.netguard import (
    MAX_REDIRECTS,
    assert_public_url,
    fetch_public,
    is_blocked_address,
)
from nexus.paths import (
    detect_image_type,
    is_local_path,
    resolve_local_image,
)

from conftest import FakeTransport


#: The smallest real PNG: one pixel, one colour.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg=="
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def outside_home(tmp_path, monkeypatch):
    """A home directory that is *not* the temporary directory.

    The suite's own isolation points ``HERMES_HOME`` at the temporary
    directory, which would make every temporary file a file inside Hermes'
    home. This moves it aside so a test can have a file that is allowed.
    """
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def a_png(tmp_path):
    """A real PNG in the temporary directory."""
    path = tmp_path / "picture.png"
    path.write_bytes(PNG_BYTES)
    return path


@pytest.fixture
def public_dns(monkeypatch):
    """Answer name resolution with a public address."""
    from nexus import netguard

    monkeypatch.setattr(netguard, "resolve_host", lambda host, port: ["93.184.216.34"])


def _vision(settings, transport):
    """A vision module wired to a mock transport."""
    from nexus.budget import BudgetGuard
    from nexus.modules.vision import VisionModule
    from nexus.providers.gmi_llm import GmiLLMClient
    from nexus.registry import ModelRegistry
    from nexus.router import Router

    client = HttpClient(transport=transport, max_retries=0, timeout=5.0)
    registry = ModelRegistry(settings, client)
    return VisionModule(
        settings,
        registry,
        Router(settings),
        GmiLLMClient(settings.llm_base_url, client),
        BudgetGuard(settings),
        None,
    )


# --------------------------------------------------------------------------- #
# Blocker 3: a named path is checked, not trusted
# --------------------------------------------------------------------------- #


def test_the_profile_own_dotenv_is_refused(outside_home, a_png):
    """A file inside Hermes' home is never read, let alone sent.

    This is the exact shape of the bug: the model names a path, and the
    path is the credential file.
    """
    secret = outside_home / ".env"
    secret.write_text("GMI_API_KEY=sk-not-a-real-key\n", encoding="utf-8")

    with pytest.raises(ValidationError) as raised:
        resolve_local_image(str(secret), purpose="sent")

    assert "Hermes" in str(raised.value)


def test_a_dotenv_named_as_an_image_is_refused(outside_home, tmp_path):
    """The extension is not enough; the bytes have to agree.

    The file is in an allowed place, so the home-directory rule does not
    catch it — the magic bytes do.
    """
    secret = tmp_path / "picture.png"
    secret.write_text("GMI_API_KEY=sk-not-a-real-key\n", encoding="utf-8")

    with pytest.raises(ValidationError) as raised:
        resolve_local_image(str(secret), purpose="sent")

    assert "not an image file" in str(raised.value)


def test_a_file_that_is_not_an_image_is_refused(outside_home, tmp_path):
    """A text file is refused whatever it is called."""
    path = tmp_path / "notes.txt"
    path.write_text("just some notes", encoding="utf-8")

    with pytest.raises(ValidationError) as raised:
        resolve_local_image(str(path), purpose="sent")

    assert "only image files" in str(raised.value)


def test_a_file_that_does_not_exist_is_refused(outside_home, tmp_path):
    """There is nothing to send."""
    with pytest.raises(ValidationError) as raised:
        resolve_local_image(str(tmp_path / "absent.png"), purpose="sent")

    assert "does not exist" in str(raised.value)


def test_a_directory_is_refused(outside_home, tmp_path):
    """A directory is not an image."""
    with pytest.raises(ValidationError) as raised:
        resolve_local_image(str(tmp_path), purpose="sent")

    assert "regular file" in str(raised.value)


def test_a_file_outside_the_allowed_roots_is_refused(outside_home, tmp_path, monkeypatch):
    """The working directory, the plugin's data directory and the temporary
    directory are the places NEXUS may read from. A file anywhere else is
    refused even when it is a perfectly good picture."""
    from nexus import paths

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    picture = elsewhere / "picture.png"
    picture.write_bytes(PNG_BYTES)

    monkeypatch.setattr(paths, "allowed_roots", lambda: [tmp_path / "somewhere-else"])

    with pytest.raises(ValidationError) as raised:
        resolve_local_image(str(picture), purpose="sent")

    assert "outside the working directory" in str(raised.value)


def test_a_symlink_out_of_the_allowed_roots_is_refused(outside_home, tmp_path, monkeypatch):
    """A symlink is followed to where it really points.

    A link inside the working directory that points at a secret elsewhere is
    still a read of that secret.
    """
    from nexus import paths

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    secret = elsewhere / "secret.png"
    secret.write_bytes(PNG_BYTES)

    link = tmp_path / "innocent.png"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows
        pytest.skip(f"this platform does not allow the test to make a symlink: {exc}")

    monkeypatch.setattr(paths, "allowed_roots", lambda: [tmp_path])

    with pytest.raises(ValidationError) as raised:
        resolve_local_image(str(link), purpose="sent")

    assert "outside the working directory" in str(raised.value)


def test_a_symlink_into_the_profile_is_refused(outside_home, tmp_path, monkeypatch):
    """A link that points into Hermes' home is refused too."""
    from nexus import paths

    secret = outside_home / ".env"
    secret.write_text("GMI_API_KEY=sk-not-a-real-key\n", encoding="utf-8")

    link = tmp_path / "picture.png"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows
        pytest.skip(f"this platform does not allow the test to make a symlink: {exc}")

    monkeypatch.setattr(paths, "allowed_roots", lambda: [tmp_path])

    with pytest.raises(ValidationError) as raised:
        resolve_local_image(str(link), purpose="sent")

    assert "Hermes" in str(raised.value)


def test_a_real_image_in_an_allowed_root_is_accepted(outside_home, a_png):
    """The rule does not block the ordinary case."""
    resolved = resolve_local_image(str(a_png), purpose="sent")

    assert resolved == str(a_png.resolve())


def test_the_image_type_comes_from_the_bytes():
    """The magic bytes decide, not the name."""
    assert detect_image_type(PNG_BYTES[:32]) == "image/png"
    assert detect_image_type(b"GMI_API_KEY=sk-not-a-real-key") is None


def test_a_url_is_not_a_local_path():
    """A URL is fetched under the network rules, not the file rules."""
    assert is_local_path("/tmp/a.png") is True
    assert is_local_path("https://cdn.example/a.png") is False
    assert is_local_path("data:image/png;base64,AAAA") is False
    assert is_local_path("") is False


def test_the_media_tool_refuses_a_named_profile_file(settings, outside_home, tmp_path):
    """``nexus_media`` reads the file only through the checked path."""
    from nexus.modules.creative import _prepare_media_value

    secret = tmp_path / "secret.png"
    secret.write_text("GMI_API_KEY=sk-not-a-real-key\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        _prepare_media_value(str(secret), None)


def test_the_media_tool_sends_a_real_image(settings, outside_home, a_png):
    """A permitted image is read and encoded."""
    from nexus.modules.creative import _prepare_media_value

    value = _prepare_media_value(str(a_png), None)

    assert isinstance(value, str)
    assert value.startswith("data:image/png;base64,")
    assert base64.b64decode(value.split(",", 1)[1]) == PNG_BYTES


def test_vision_refuses_a_named_profile_file(settings, outside_home, tmp_path, public_dns):
    """Vision checks a local path the same way."""
    secret = tmp_path / "secret.png"
    secret.write_text("GMI_API_KEY=sk-not-a-real-key\n", encoding="utf-8")

    vision = _vision(settings, FakeTransport())

    with pytest.raises(ValidationError):
        vision._prepare_images([str(secret)])


def test_the_llm_client_refuses_a_named_profile_file(settings, outside_home, tmp_path):
    """The provider's own image encoder checks the path as well."""
    from nexus.providers.gmi_llm import encode_image

    secret = tmp_path / "secret.png"
    secret.write_text("GMI_API_KEY=sk-not-a-real-key\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        encode_image(str(secret))


# --------------------------------------------------------------------------- #
# Blocker 4: a named URL is checked, not trusted
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",
        "169.254.1.1",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fd00::1",
        "fe80::1",
    ],
)
def test_an_internal_address_is_refused(address):
    """The cloud metadata endpoint is the prize here, so it is in the list."""
    import ipaddress

    assert is_blocked_address(ipaddress.ip_address(address)) is True


def test_a_public_address_is_allowed():
    import ipaddress

    assert is_blocked_address(ipaddress.ip_address("93.184.216.34")) is False


def test_an_ipv4_mapped_ipv6_address_is_judged_as_ipv4():
    """``::ffff:127.0.0.1`` is the loopback address wearing a hat."""
    import ipaddress

    assert is_blocked_address(ipaddress.ip_address("::ffff:127.0.0.1")) is True


def test_the_metadata_endpoint_is_refused_by_name(monkeypatch):
    """Some metadata services are named, not addressed."""
    from nexus import netguard

    monkeypatch.setattr(netguard, "resolve_host", lambda host, port: ["93.184.216.34"])

    with pytest.raises(ValidationError) as raised:
        assert_public_url("http://metadata.google.internal/computeMetadata/v1/")

    assert "metadata" in str(raised.value).lower()


def test_the_metadata_endpoint_by_address_is_refused(monkeypatch):
    """``169.254.169.254`` is the address, whatever the name says."""
    from nexus import netguard

    monkeypatch.setattr(netguard, "resolve_host", lambda host, port: ["169.254.169.254"])

    with pytest.raises(ValidationError) as raised:
        assert_public_url("http://metadata.example/latest/meta-data/")

    assert "internal address" in str(raised.value)


def test_a_loopback_url_is_refused(monkeypatch):
    from nexus import netguard

    monkeypatch.setattr(netguard, "resolve_host", lambda host, port: ["127.0.0.1"])

    with pytest.raises(ValidationError) as raised:
        assert_public_url("http://localhost:8080/admin")

    assert "internal address" in str(raised.value)


def test_a_host_that_cannot_be_resolved_is_refused(monkeypatch):
    """An address that cannot be checked is not an address that was checked."""
    import socket

    from nexus import netguard

    def _boom(host, port):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(netguard, "resolve_host", _boom)

    with pytest.raises(ValidationError) as raised:
        assert_public_url("https://nowhere.invalid/a.png")

    assert "could not be resolved" in str(raised.value)


def test_a_url_carrying_credentials_is_refused():
    with pytest.raises(ValidationError) as raised:
        assert_public_url("https://user:password@cdn.example/a.png")

    assert "credentials" in str(raised.value)


def test_a_non_http_url_is_refused():
    with pytest.raises(ValidationError) as raised:
        assert_public_url("file:///etc/passwd")

    assert "http" in str(raised.value)


def test_a_name_that_answers_with_a_private_address_is_refused(monkeypatch):
    """A name that answers with both is refused: which one the socket picks
    is not ours to decide."""
    from nexus import netguard

    monkeypatch.setattr(
        netguard, "resolve_host", lambda host, port: ["93.184.216.34", "127.0.0.1"]
    )

    with pytest.raises(ValidationError) as raised:
        assert_public_url("https://cdn.example/a.png")

    assert "internal address" in str(raised.value)


class _RedirectingClient:
    """A client that answers the first request with a redirect."""

    def __init__(self, location: str, hops: int = 1) -> None:
        self.location = location
        self.hops = hops
        self.calls: list[str] = []

    def request(self, method, url, *, headers=None, timeout=60.0, follow_redirects=True):
        self.calls.append(url)
        if len(self.calls) <= self.hops:
            return HttpResponse(status=302, body="", headers={"location": self.location})
        return HttpResponse(status=200, body="", headers={}, raw=PNG_BYTES)


def test_a_redirect_into_the_private_network_is_refused(monkeypatch):
    """Every hop is checked, not just the first.

    A public URL that answers with a redirect to ``169.254.169.254`` is the
    classic way past a check that only looks at the URL it was given.
    """
    from nexus import netguard

    def _resolve(host, port):
        if host == "internal.example":
            return ["169.254.169.254"]
        return ["93.184.216.34"]

    monkeypatch.setattr(netguard, "resolve_host", _resolve)

    client = _RedirectingClient("http://internal.example/latest/meta-data/")
    with pytest.raises(ValidationError) as raised:
        fetch_public("https://cdn.example/a.png", client, timeout=5.0)

    assert "internal address" in str(raised.value)


def test_a_redirect_is_followed_only_when_the_next_host_is_public(monkeypatch):
    """The ordinary case still works."""
    from nexus import netguard

    monkeypatch.setattr(netguard, "resolve_host", lambda host, port: ["93.184.216.34"])

    client = _RedirectingClient("https://cdn.example/b.png")
    fetched = fetch_public("https://cdn.example/a.png", client, timeout=5.0)

    assert fetched is not None
    assert fetched[0] == "https://cdn.example/b.png"
    assert client.calls == ["https://cdn.example/a.png", "https://cdn.example/b.png"]


def test_a_redirect_loop_is_refused(monkeypatch):
    """A chain that never ends is not followed for ever."""
    from nexus import netguard

    monkeypatch.setattr(netguard, "resolve_host", lambda host, port: ["93.184.216.34"])

    client = _RedirectingClient("https://cdn.example/a.png", hops=99)
    with pytest.raises(ValidationError) as raised:
        fetch_public("https://cdn.example/a.png", client, timeout=5.0)

    assert f"more than {MAX_REDIRECTS}" in str(raised.value)


def test_vision_refuses_an_internal_url(settings, monkeypatch):
    """A refusal is not a failed download.

    The URL points inside the machine's own network, so NEXUS does not
    fetch it and does not fall back on sending it along.
    """
    from nexus import netguard

    monkeypatch.setattr(netguard, "resolve_host", lambda host, port: ["127.0.0.1"])

    vision = _vision(settings, FakeTransport())

    with pytest.raises(ValidationError) as raised:
        vision._prepare_images(["http://localhost:8080/secret.png"])

    assert "refused" in str(raised.value)


def test_vision_refuses_a_body_that_is_not_an_image(settings, public_dns):
    """The type comes from the bytes, not from the server's header.

    A server that answers ``Content-Type: image/png`` with a page of text
    must not have that page sent to a model as an image.
    """
    transport = FakeTransport()

    class NotAnImageTransport(FakeTransport):
        def request(self, method, url, **kwargs):
            if url.startswith("https://cdn.example/"):
                return HttpResponse(
                    status=200,
                    body="",
                    headers={"content-type": "image/png"},
                    raw=b"<html>not an image</html>",
                )
            return super().request(method, url, **kwargs)

    transport = NotAnImageTransport()
    vision = _vision(settings, transport)

    prepared = vision._prepare_images(["https://cdn.example/a.png"])

    # The download did not yield an image, so the URL goes as it stands —
    # and no ``image/png`` fallback is invented for it.
    assert prepared == ["https://cdn.example/a.png"]


def test_vision_sends_an_image_body_as_the_type_the_bytes_say(settings, public_dns):
    """A real image is inlined with the type its bytes describe."""
    from nexus.modules.vision import _inline_remote_image

    class ImageTransport(FakeTransport):
        def request(self, method, url, **kwargs):
            if url.startswith("https://cdn.example/"):
                return HttpResponse(
                    status=200,
                    body="",
                    headers={"content-type": "text/plain"},
                    raw=PNG_BYTES,
                )
            return super().request(method, url, **kwargs)

    client = HttpClient(transport=ImageTransport(), max_retries=0, timeout=5.0)

    uri = _inline_remote_image("https://cdn.example/a.png", client, timeout=5.0)

    assert uri is not None
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == PNG_BYTES
