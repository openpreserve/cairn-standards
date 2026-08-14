"""Small shared helpers: HTTP client, hashing, MIME inference, semver."""

from __future__ import annotations

import hashlib
import os

import httpx

from . import __version__

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
