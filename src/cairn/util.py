"""Small shared helpers: HTTP client, hashing, MIME inference, semver, durable writes."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import httpx

from . import __version__

# Everything written into the document root is read by nginx running as an unprivileged
# user, and nginx answers 403 for a file it cannot open. `tempfile.mkstemp` creates 0600
# and `os.replace` carries that mode onto the destination, so the mode is set explicitly
# rather than inherited from the temp file's default.
PUBLISHED_MODE = 0o644

USER_AGENT = f"cairn/{__version__} (+https://standards.openpreservation.org)"

# MIME types for the artifact kinds we serve. Kept in sync with deploy/nginx.conf.
_EXT_MEDIA_TYPES = {
    ".xsd": "application/xml",
    ".rng": "application/xml",
    ".nvdl": "application/xml",
    ".sch": "application/xml",
    ".xml": "application/xml",
    ".xhtml": "application/xhtml+xml",
    ".html": "text/html",
    ".pdf": "application/pdf",
    ".json": "application/json",
    ".txt": "text/plain",
    ".css": "text/css",
}


def http_client() -> httpx.Client:
    """An HTTP client with our UA and (if present) a GitHub token for higher rate limits."""
    headers = {"User-Agent": USER_AGENT}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(headers=headers, timeout=30.0, follow_redirects=True)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    """Write bytes via a temp file in the same directory, then rename over the target.

    Every write into the document root goes through here, so a reader never observes a
    partial file: the syncer can be killed mid-cycle and the render step rewrites pages in
    place while nginx is serving them.

    `os.fdopen` takes ownership of the descriptor so it is closed exactly once, including
    when the write itself fails. Closing it twice by hand would raise EBADF from the second
    attempt, masking the real error and skipping the temp-file cleanup.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, PUBLISHED_MODE)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def ensure_published_mode(path: Path) -> bool:
    """Widen *path* to PUBLISHED_MODE if it is not already readable by the web server.

    Frozen artifacts are never rewritten, so a file left unreadable by an earlier bug would
    stay a 403 forever. Repairing it here costs one stat per sync and needs no re-fetch.
    Returns True when a change was made.
    """
    try:
        current = path.stat().st_mode & 0o777
    except OSError:
        return False
    if current == PUBLISHED_MODE:
        return False
    path.chmod(PUBLISHED_MODE)
    return True


def media_type_for(name: str, override: str | None = None) -> str:
    if override:
        return override
    dot = name.rfind(".")
    ext = name[dot:].lower() if dot != -1 else ""
    return _EXT_MEDIA_TYPES.get(ext, "application/octet-stream")


def semver_key(version: str) -> tuple[int, int, int]:
    parts = version.split(".")
    if len(parts) != 3:
        raise ValueError(f"expected X.Y.Z version, got {version!r}")
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        raise ValueError(f"expected X.Y.Z version, got {version!r}")
