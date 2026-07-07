"""Filesystem layout and path resolution for a Cairn workspace."""

from __future__ import annotations

from pathlib import Path

STANDARDS_DIRNAME = "standards"
SCHEMA_RELPATH = Path("schemas") / "standard.schema.json"

# Build outputs (git-ignored).
SITE_DIRNAME = "site"                       # the nginx document root
BUILD_DIRNAME = "build"                     # generated non-served artifacts (nginx conf, ...)
NGINX_ROUTES_RELPATH = Path("nginx") / "cairn-routes.conf"


def find_root(start: Path | None = None) -> Path:
    """Walk upward from *start* (or cwd) to the workspace root.

    The root is the directory that holds both ``standards/`` and the manifest
    JSON Schema. Falls back to *start* if no marker is found.
    """
    start = (start or Path.cwd()).resolve()
    for candidate in (start, *start.parents):
        if (candidate / STANDARDS_DIRNAME).is_dir() and (candidate / SCHEMA_RELPATH).is_file():
            return candidate
    return start


def standards_dir(root: Path) -> Path:
    return root / STANDARDS_DIRNAME


def schema_path(root: Path) -> Path:
    return root / SCHEMA_RELPATH


def site_dir(root: Path) -> Path:
    return root / SITE_DIRNAME


def build_dir(root: Path) -> Path:
    return root / BUILD_DIRNAME


def nginx_routes_path(root: Path) -> Path:
    return build_dir(root) / NGINX_ROUTES_RELPATH
