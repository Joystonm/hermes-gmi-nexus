"""Which local files NEXUS is allowed to read and send to GMI Cloud.

A model can name a path: ``nexus_media`` takes an ``image`` or a ``mask``,
``nexus_vision`` takes images. Whatever it names, NEXUS may read and upload
it — so the path is checked, not trusted. A named path is not a picture of a
picture; it is a path, and a path can be a profile's ``.env``.

The checks are:

* the path is resolved, so a symlink is followed to where it really points;
* it must be a regular file;
* its extension must name an image type, and its first bytes must be that
  image type — an extension alone proves nothing, because the extension is
  the part a caller chooses;
* it must live in a directory the agent is allowed to read from: the
  working directory, NEXUS' own plugin-data directory, or the system
  temporary directory;
* and it must never live inside Hermes' own home directory, which is where
  the profile's ``.env`` and every other secret lives — with the single
  exception of NEXUS' own data directory, which is inside it by design.

The rules are fail-closed: a file whose type cannot be established is
refused, not guessed at.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import ValidationError

#: The image types NEXUS is willing to read and upload, by extension.
IMAGE_TYPES: dict[str, tuple[str, ...]] = {
    ".png": ("image/png",),
    ".jpg": ("image/jpeg",),
    ".jpeg": ("image/jpeg",),
    ".gif": ("image/gif",),
    ".webp": ("image/webp",),
    ".bmp": ("image/bmp",),
    ".tif": ("image/tiff",),
    ".tiff": ("image/tiff",),
}


def detect_image_type(head: bytes) -> str | None:
    """Return the image type a file's first bytes describe, or ``None``.

    The magic bytes are what the file actually is; the extension is only
    what somebody called it.
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    return None


def hermes_home() -> Path | None:
    """Return Hermes' home directory, when the plugin runs inside Hermes."""
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()).resolve()
    except Exception:
        pass
    override = os.environ.get("HERMES_HOME")
    if override:
        try:
            return Path(override).resolve()
        except OSError:
            return None
    return None


def plugin_data_dir() -> Path | None:
    """Return NEXUS' own persistent data directory, when Hermes provides one."""
    try:
        from plugins.plugin_storage import plugin_data_dir as _data_dir

        return Path(_data_dir("gmi-nexus")).resolve()
    except Exception:
        return None


def allowed_roots() -> list[Path]:
    """Return the directories a local file may be read from."""
    roots: list[Path] = []
    try:
        roots.append(Path(os.getcwd()).resolve())
    except OSError:
        pass
    data = plugin_data_dir()
    if data is not None:
        roots.append(data)
    try:
        roots.append(Path(tempfile.gettempdir()).resolve())
    except OSError:
        pass
    return roots


def _within(path: Path, root: Path) -> bool:
    """Whether a path is inside a root."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_local_image(path: str, *, purpose: str = "send") -> str:
    """Return the resolved path of an image file NEXUS may read and send.

    Raises :class:`ValidationError` when the file may not be read.
    """
    raw = str(path or "").strip()
    if not raw:
        raise ValidationError("the file path is empty")

    try:
        resolved = Path(raw).expanduser().resolve()
    except OSError as exc:
        raise ValidationError(f"the path could not be resolved: {exc}") from exc

    home = hermes_home()
    if home is not None and _within(resolved, home):
        data = plugin_data_dir()
        if data is None or not _within(resolved, data):
            raise ValidationError(
                f"{raw} is inside Hermes' own home directory, and files from there "
                "are never read or sent anywhere"
            )

    if not resolved.exists():
        raise ValidationError(f"the file does not exist: {raw}")
    if not resolved.is_file():
        raise ValidationError(f"{raw} is not a regular file")

    extension = resolved.suffix.lower()
    expected = IMAGE_TYPES.get(extension)
    if expected is None:
        raise ValidationError(
            f"only image files can be {purpose}d, and {raw} has "
            f"{extension or 'no'} extension"
        )

    try:
        with open(resolved, "rb") as handle:
            head = handle.read(32)
    except OSError as exc:
        raise ValidationError(f"the file could not be read: {exc}") from exc

    detected = detect_image_type(head)
    if detected is None:
        raise ValidationError(
            f"{raw} is not an image file: its contents match no image format"
        )
    if detected not in expected:
        raise ValidationError(
            f"{raw} is named {extension} but its contents are {detected}"
        )

    if not any(_within(resolved, root) for root in allowed_roots()):
        raise ValidationError(
            f"{raw} is outside the working directory, so NEXUS will not read it"
        )

    return str(resolved)


def is_local_path(value: Any) -> bool:
    """Whether a reference looks like a local path rather than a URL."""
    if not isinstance(value, str) or not value:
        return False
    return not value.startswith(("http://", "https://", "data:"))


__all__ = [
    "IMAGE_TYPES",
    "allowed_roots",
    "detect_image_type",
    "hermes_home",
    "is_local_path",
    "plugin_data_dir",
    "resolve_local_image",
]
