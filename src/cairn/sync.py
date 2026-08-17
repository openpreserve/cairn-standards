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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import site_dir
from .manifest import MUTABLE_STATUSES, Artifact, Release, Standard, artifact_locator
from .util import atomic_write, ensure_published_mode, http_client, sha256_hex

RAW_BASE = "https://raw.githubusercontent.com"
API_BASE = "https://api.github.com"


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
    """Turn an artifact declaration into a concrete download URL.

    Precedence comes from `artifact_locator` rather than being restated here, so the
    write-once check compares exactly the coordinates this function will fetch from.
    """
    locator = artifact_locator(std, rel, art)
    repo = locator["repo"]
    ref = locator["ref"]

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
        try:
            assets = resp.json().get("assets", [])
        except Exception:
            raise SyncError(f"{std.id} {rel.version} {art.name}: GitHub API returned non-JSON for release {tag}")
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
    """Load provenance.json, returning None on any parse or structural error.

    Callers index into every record by name, so the element shape is checked here too. A
    hand-edited or foreign-written file would otherwise surface as a raw TypeError or
    KeyError from deep in the sync loop, which is exactly what treating an unreadable
    provenance file as a first run is meant to avoid.
    """
    if not prov_path.exists():
        return None
    try:
        data = json.loads(prov_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("artifacts"), list):
        return None
    if not all(isinstance(a, dict) and isinstance(a.get("name"), str) for a in data["artifacts"]):
        return None
    return data


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
    repaired: int = 0
    # (standard id, message) for standards that failed. Collected rather than raised so one
    # bad upstream cannot stop every other standard from syncing.
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def absorb(self, other: SyncStats) -> None:
        self.fetched += other.fetched
        self.verified += other.verified
        self.skipped += other.skipped
        self.planned += other.planned
        self.repaired += other.repaired
        self.failures.extend(other.failures)


@dataclass
class PlannedArtifact:
    """One artifact's intended outcome. Nothing here has touched the filesystem yet."""

    name: str
    dest: Path
    action: str  # "write" | "skip" | "verify"
    record: dict
    data: bytes | None = None


@dataclass
class ReleasePlan:
    release: Release
    vdir: Path
    artifacts: list[PlannedArtifact]
    orphans: list[str]

    @property
    def records(self) -> list[dict]:
        return [p.record for p in self.artifacts]


def _artifact_record(art: Artifact, resolved: Resolved, data: bytes, digest: str, commit: str | None) -> dict:
    return {
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


def _plan_release(
    std: Standard,
    rel: Release,
    root: Path,
    client: httpx.Client,
    *,
    verify: bool,
) -> ReleasePlan:
    """Resolve, fetch and check a release without writing anything.

    Every invariant is asserted here, before the commit phase touches the document root.
    Checking after writing was the earlier mistake: a refused manifest edit still left the
    replacement file published under a frozen version, absent from provenance and from
    SHA256SUMS, cached immutably, and beyond the reach of the orphan reaper because the
    reaper only considers names the previous provenance recorded.
    """
    vdir = site_dir(root) / std.id / f"v{rel.version}"
    prior = _load_prior_provenance(vdir / "provenance.json")
    prior_arts = {a["name"]: a for a in prior["artifacts"]} if prior else {}
    mutable = rel.status in MUTABLE_STATUSES
    commit_cache: dict[tuple[str, str], str | None] = {}

    planned: list[PlannedArtifact] = []
    for art in rel.artifacts:
        resolved = resolve(std, rel, art, client)
        dest = vdir / art.name
        frozen = prior_arts.get(art.name)
        is_frozen = frozen is not None and dest.exists() and not mutable

        if is_frozen and not verify:
            planned.append(PlannedArtifact(art.name, dest, "skip", frozen))
            continue

        data = _fetch(resolved.url, client)
        digest = sha256_hex(data)

        if frozen and not mutable and frozen.get("sha256") and digest != frozen["sha256"]:
            raise SyncError(
                f"FROZEN VERSION CHANGED: {std.id} v{rel.version}/{art.name}\n"
                f"  recorded sha256 {frozen['sha256']}\n"
                f"  upstream sha256 {digest}\n"
                f"  A released version's bytes must never change. Cut a new version instead."
            )

        # A frozen artifact whose hash checks out is left exactly as it is. One that never
        # had a hash recorded falls through to be written, so the next run has something to
        # compare against.
        if is_frozen and verify and frozen.get("sha256"):
            planned.append(PlannedArtifact(art.name, dest, "verify", frozen))
            continue

        commit = None
        if resolved.repo and resolved.ref:
            key = (resolved.repo, resolved.ref)
            if key not in commit_cache:
                commit_cache[key] = _resolve_commit(resolved.repo, resolved.ref, client)
            commit = commit_cache[key]

        planned.append(
            PlannedArtifact(
                art.name, dest, "write", _artifact_record(art, resolved, data, digest, commit), data
            )
        )

    # Files the previous provenance recorded that the manifest no longer declares. Only
    # names sync itself wrote are candidates, so render output such as index.html is safe.
    current_names = {art.name for art in rel.artifacts}
    orphans = sorted({a["name"] for a in prior["artifacts"]} - current_names) if prior else []

    # Deleting a file from a frozen release turns a published 200 into a 404, which breaks
    # the same promise as changing its bytes. Withdrawn releases are exempt: that status
    # unpublishes the whole release deliberately and already answers 410.
    if orphans and not mutable and rel.is_served:
        raise SyncError(
            f"FROZEN VERSION LOST AN ARTIFACT: {std.id} v{rel.version}\n"
            f"  no longer in the manifest: {', '.join(orphans)}\n"
            f"  status is '{rel.status}', so these URLs are published and must keep resolving.\n"
            f"  Restore the artifact entries, or publish the change as a new version.\n"
            f"  `cairn validate --baseline` catches this on a pull request, before it reaches\n"
            f"  a deployment; if it has already been deployed, restoring the entries is the\n"
            f"  only fix that keeps the published URLs working."
        )

    return ReleasePlan(rel, vdir, planned, orphans)


def _commit_release(std: Standard, plan: ReleasePlan, *, log) -> SyncStats:
    """Apply a checked plan. Every failure mode has already been ruled out by _plan_release."""
    stats = SyncStats()
    rel = plan.release
    plan.vdir.mkdir(parents=True, exist_ok=True)

    for item in plan.artifacts:
        if item.action == "write":
            atomic_write(item.dest, item.data)
            stats.fetched += 1
            log(
                f"  [get]  {std.id} v{rel.version}/{item.name} "
                f"({item.record['bytes']} bytes, sha256 {item.record['sha256'][:12]}…)"
            )
            continue

        # Skipped and verified artifacts are not rewritten, so a file left unreadable by an
        # earlier bug would stay a 403 indefinitely. Repair the mode in place instead.
        if ensure_published_mode(item.dest):
            stats.repaired += 1
            log(f"  [mode] {std.id} v{rel.version}/{item.name} -> 0644")
        if item.action == "verify":
            stats.verified += 1
        else:
            stats.skipped += 1

    # Only reachable for mutable or withdrawn releases; the frozen case raised in the plan.
    for orphan_name in plan.orphans:
        orphan = plan.vdir / orphan_name
        if orphan.exists():
            orphan.unlink()
            log(f"  [del]  {std.id} v{rel.version}/{orphan_name}")

    _write_release_metadata(std, plan, log=log)
    return stats


def _write_release_metadata(std: Standard, plan: ReleasePlan, *, log) -> None:
    """Rewrite provenance.json and SHA256SUMS only when the recorded set actually changed.

    These live beside write-once artifacts and are documented as permanent. Rewriting them
    every cycle purely to move `updated_at` churns the mtime of files nothing has changed
    and invalidates their validators for no reason.
    """
    rel = plan.release
    records = plan.records
    prov_path = plan.vdir / "provenance.json"
    sums_path = plan.vdir / "SHA256SUMS"

    provenance = {
        "standard": std.id,
        "version": rel.version,
        "status": rel.status,
        "generated_by": "cairn sync",
        "updated_at": _now(),
        "artifacts": records,
    }

    prior = _load_prior_provenance(prov_path)
    unchanged = (
        prior is not None
        and prior.get("status") == rel.status
        and prior.get("artifacts") == records
        and sums_path.exists()
    )
    if unchanged:
        ensure_published_mode(prov_path)
        ensure_published_mode(sums_path)
        return

    atomic_write(prov_path, (json.dumps(provenance, indent=2) + "\n").encode())
    atomic_write(sums_path, "".join(f"{r['sha256']}  {r['name']}\n" for r in records).encode())
    log(f"  [meta] {std.id} v{rel.version}/provenance.json, SHA256SUMS")


def _plan_dry_run(std: Standard, rel: Release, client: httpx.Client, *, log) -> SyncStats:
    stats = SyncStats()
    for art in rel.artifacts:
        resolved = resolve(std, rel, art, client)
        ok = _reachable(resolved.url, client)
        log(f"  [plan] {std.id} v{rel.version}/{art.name} <- {resolved.url} {'OK' if ok else 'UNREACHABLE'}")
        stats.planned += 1
    return stats


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
        if dry_run:
            stats.absorb(_plan_dry_run(std, rel, client, log=log))
            continue
        plan = _plan_release(std, rel, root, client, verify=verify)
        stats.absorb(_commit_release(std, plan, log=log))
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
            # One standard's failure must not stop the others. An unreachable upstream or a
            # failed integrity check on eac is not a reason to stop replicating ead, and the
            # caller still renders the site from whatever is on disk, so the rest of the
            # registry stays current instead of freezing at its last good state.
            try:
                total.absorb(sync_standard(std, root, client, verify=verify, dry_run=dry_run, log=log))
            except (SyncError, httpx.HTTPError) as exc:
                # httpx errors are caught alongside SyncError so a network fault reaching one
                # upstream is isolated the same way a refused manifest edit is.
                total.failures.append((std.id, str(exc)))
                log(f"  [FAIL] {std.id}: {str(exc).splitlines()[0]}")
    return total
