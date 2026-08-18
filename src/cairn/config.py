"""Filesystem layout and path resolution for a Cairn workspace."""

from __future__ import annotations

import os
from pathlib import Path

STANDARDS_DIRNAME = "standards"
SCHEMA_RELPATH = Path("schemas") / "standard.schema.json"

# Build outputs (git-ignored).
SITE_DIRNAME = "site"                       # the nginx document root
# The page the render writes into each release directory. It sits among write-once artifacts
# that the sync's orphan reaper is allowed to delete, so the reaper has to know it is not one
# of them. Named here rather than in either module, because a copy in the reaper is a copy of
# the render's decision, and the reaper deletes files on the strength of it.
RELEASE_PAGE_NAME = "index.html"
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
    """Output document root. Overridable via CAIRN_SITE_DIR (used by the container)."""
    override = os.environ.get("CAIRN_SITE_DIR")
    return Path(override) if override else root / SITE_DIRNAME


def build_dir(root: Path) -> Path:
    return root / BUILD_DIRNAME


def nginx_routes_path(root: Path) -> Path:
    """Generated nginx routes file. Overridable via CAIRN_ROUTES_FILE."""
    override = os.environ.get("CAIRN_ROUTES_FILE")
    return Path(override) if override else build_dir(root) / NGINX_ROUTES_RELPATH
