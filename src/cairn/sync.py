"""Replicate upstream artifacts into an integrity-checked, write-once local store.

Layout produced under ``site/``::

    site/<id>/v<version>/<artifact>          # the replicated bytes
    site/<id>/v<version>/provenance.json     # source + checksum + fetch metadata
    site/<id>/v<version>/SHA256SUMS          # `sha256sum -c` compatible

A released version is *frozen*: once its provenance is recorded, a later sync will
skip it (no-op). ``--verify`` re-fetches and fails loudly if the upstream bytes for a
released version have changed (re-tagging / tampering) - the fix is to cut a new version.
"""

from __future__ import annotations

import fnmatch
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import site_dir
from .manifest import Artifact, Release, Standard
from .util import http_client, sha256_hex

RAW_BASE = "https://raw.githubusercontent.com"
API_BASE = "https://api.github.com"

# Releases with these statuses are still moving (e.g. tracking a pre-release branch):
# they are re-fetched and overwritten on every sync rather than frozen. Everything else
# is write-once - a released version's bytes must never change.
MUTABLE_STATUSES = {"draft"}


class SyncError(Exception):
    pass


@dataclass
class Resolved:
    url: str
    repo: str | None = None
    ref: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _repo_owner_name(repo: str) -> tuple[str, str]:
    owner, name = repo.split("/", 1)
    return owner, name


def resolve(std: Standard, rel: Release, art: Artifact, client: httpx.Client) -> Resolved:
    """Turn an artifact declaration into a concrete download URL."""
    repo = art.repo or std.source.repo
    ref = art.ref or rel.ref or std.source.ref

    if art.from_ == "url":
        return Resolved(url=art.url)  # type: ignore[arg-type]

    if art.from_ == "repo":
        if not ref:
            raise SyncError(f"{std.id} {rel.version} {art.name}: no ref (set source.ref, release.ref, or artifact.ref)")
        return Resolved(url=f"{RAW_BASE}/{repo}/{ref}/{art.path}", repo=repo, ref=ref)

    if art.from_ == "github-pages":
        owner, name = _repo_owner_name(repo)
        return Resolved(url=f"https://{owner}.github.io/{name}/{art.path}", repo=repo)

    if art.from_ == "release-asset":
        tag = art.release_tag or rel.ref or std.source.ref
        if not tag:
            raise SyncError(f"{std.id} {rel.version} {art.name}: release-asset needs a release_tag")
        api = f"{API_BASE}/repos/{repo}/releases/tags/{tag}"
        resp = client.get(api)
        if resp.status_code != 200:
            raise SyncError(f"{std.id} {rel.version} {art.name}: cannot read release {tag} ({resp.status_code})")
        assets = resp.json().get("assets", [])
        matches = [a for a in assets if a["name"] == art.asset or fnmatch.fnmatch(a["name"], art.asset)]
        if not matches:
            available = ", ".join(a["name"] for a in assets) or "(none)"
            raise SyncError(f"{std.id} {rel.version} {art.name}: no asset matches '{art.asset}'. Available: {available}")
        if len(matches) > 1:
            raise SyncError(f"{std.id} {rel.version} {art.name}: '{art.asset}' matched several assets")
        return Resolved(url=matches[0]["browser_download_url"], repo=repo, ref=tag)

    raise SyncError(f"{std.id} {rel.version} {art.name}: unknown source type '{art.from_}'")


def _resolve_commit(repo: str, ref: str, client: httpx.Client) -> str | None:
    """Best-effort: pin a branch/tag ref to a commit SHA for provenance. Never fatal."""
    try:
        resp = client.get(f"{API_BASE}/repos/{repo}/commits/{ref}", headers={"Accept": "application/vnd.github+json"})
        if resp.status_code == 200:
            return resp.json().get("sha")
    except Exception:  # best-effort provenance enrichment must never break a sync
        pass
    return None


def _load_prior_provenance(prov_path: Path) -> dict | None:
    """Load provenance.json, returning None on any parse or structural error."""
    if not prov_path.exists():
        return None
    try:
        data = json.loads(prov_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("artifacts"), list):
        return None
    return data


def _atomic_write(path: Path, data: bytes) -> None:
    """Write bytes to path via a temp file in the same directory, then rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        os.write(fd, data)
        os.close(fd)
        os.replace(tmp, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(tmp).unlink(missing_ok=True)
        raise


def _fetch(url: str, client: httpx.Client) -> bytes:
    resp = client.get(url)
    if resp.status_code != 200:
        raise SyncError(f"fetch failed [{resp.status_code}] {url}")
    return resp.content


@dataclass
class SyncStats:
    fetched: int = 0
    verified: int = 0
    skipped: int = 0
    planned: int = 0


def sync_standard(
    std: Standard,
    root: Path,
    client: httpx.Client,
    *,
    verify: bool = False,
    dry_run: bool = False,
    log=print,
) -> SyncStats:
    stats = SyncStats()
    for rel in std.releases:
        vdir = site_dir(root) / std.id / f"v{rel.version}"
        prov_path = vdir / "provenance.json"
        prior = _load_prior_provenance(prov_path)
        prior_arts = {a["name"]: a for a in prior["artifacts"]} if prior else {}

        records: list[dict] = []
        commit_cache: dict[tuple[str, str], str | None] = {}
        mutable = rel.status in MUTABLE_STATUSES

        for art in rel.artifacts:
            resolved = resolve(std, rel, art, client)
            dest = vdir / art.name
            frozen = prior_arts.get(art.name)
            is_frozen = frozen is not None and dest.exists() and not mutable

            if dry_run:
                head_ok = _reachable(resolved.url, client)
                log(f"  [plan] {std.id} v{rel.version}/{art.name} <- {resolved.url} {'OK' if head_ok else 'UNREACHABLE'}")
                stats.planned += 1
                continue

            if is_frozen and not verify:
                stats.skipped += 1
                records.append(frozen)
                continue

            data = _fetch(resolved.url, client)
            digest = sha256_hex(data)

            if frozen and not mutable and frozen.get("sha256") and digest != frozen["sha256"]:
                raise SyncError(
                    f"FROZEN VERSION CHANGED: {std.id} v{rel.version}/{art.name}\n"
                    f"  recorded sha256 {frozen.get('sha256')}\n"
                    f"  upstream sha256 {digest}\n"
                    f"  A released version's bytes must never change. Cut a new version instead."
                )

            if is_frozen and verify:
                if frozen.get("sha256"):
                    stats.verified += 1
                    records.append(frozen)
                    continue
                # No hash was ever recorded - fall through to compute and store it

            commit = None
            if resolved.repo and resolved.ref:
                key = (resolved.repo, resolved.ref)
                if key not in commit_cache:
                    commit_cache[key] = _resolve_commit(resolved.repo, resolved.ref, client)
                commit = commit_cache[key]

            vdir.mkdir(parents=True, exist_ok=True)
            _atomic_write(dest, data)
            records.append(
                {
                    "name": art.name,
                    "role": art.role,
                    "media_type": art.content_type(),
                    "bytes": len(data),
                    "sha256": digest,
                    "source": {
                        "from": art.from_,
                        "url": resolved.url,
                        "repo": resolved.repo,
                        "ref": resolved.ref,
                        "commit": commit,
                    },
                    "fetched_at": _now(),
                }
            )
            stats.fetched += 1
            log(f"  [get]  {std.id} v{rel.version}/{art.name} ({len(data)} bytes, sha256 {digest[:12]}…)")

        if dry_run:
            continue

        # Remove artifacts that were in the last recorded provenance but are no longer
        # in the manifest. Only files we previously wrote are candidates - this avoids
        # touching render output (index.html) or anything else that sync didn't create.
        if prior:
            current_names = {art.name for art in rel.artifacts}
            old_names = {a["name"] for a in prior["artifacts"]}
            for orphan_name in old_names - current_names:
                orphan = vdir / orphan_name
                if orphan.exists():
                    orphan.unlink()
                    log(f"  [del]  {std.id} v{rel.version}/{orphan_name}")

        vdir.mkdir(parents=True, exist_ok=True)
        provenance = {
            "standard": std.id,
            "version": rel.version,
            "status": rel.status,
            "generated_by": "cairn sync",
            "updated_at": _now(),
            "artifacts": records,
        }
        _atomic_write(prov_path, (json.dumps(provenance, indent=2) + "\n").encode())
        sums = "".join(f"{r['sha256']}  {r['name']}\n" for r in records)
        _atomic_write(vdir / "SHA256SUMS", sums.encode())

    return stats


def _reachable(url: str, client: httpx.Client) -> bool:
    try:
        resp = client.head(url)
        if resp.status_code in (405, 501):  # server dislikes HEAD; try a lightweight GET
            resp = client.get(url, headers={"Range": "bytes=0-0"})
        return resp.status_code < 400
    except httpx.HTTPError:
        return False


def sync_all(
    standards: list[Standard],
    root: Path,
    *,
    only: list[str] | None = None,
    verify: bool = False,
    dry_run: bool = False,
    log=print,
) -> SyncStats:
    total = SyncStats()
    with http_client() as client:
        for std in standards:
            if only and std.id not in only:
                continue
            log(f"[sync] {std.id}")
            stats = sync_standard(std, root, client, verify=verify, dry_run=dry_run, log=log)
            total.fetched += stats.fetched
            total.verified += stats.verified
            total.skipped += stats.skipped
            total.planned += stats.planned
    return total
