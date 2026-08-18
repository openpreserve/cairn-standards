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
import random
import time
from dataclasses import dataclass, replace, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

import httpx

from .config import PROVENANCE_NAME, RELEASE_PAGE_NAME, SUMS_NAME, site_dir
from .manifest import (
    Lifecycle,
    Artifact,
    Release,
    Standard,
    artifact_locator,
)
from .markers import Marker
from .util import (
    TEMP_PREFIX,
    DecodeError,
    is_provenance_record_set,
    ModeRepair,
    atomic_write,
    ensure_published_mode,
    http_client,
    read_text,
    reap_temp_files,
    sha256_hex,
)

RAW_BASE = "https://raw.githubusercontent.com"
API_BASE = "https://api.github.com"

# Written into a release directory by something other than the artifact loop: the render's
# page for that release, and this module's own metadata. Never candidates for reaping.
GENERATED_NAMES = frozenset({RELEASE_PAGE_NAME, PROVENANCE_NAME, SUMS_NAME})


class SyncError(Exception):
    pass


class StandardFailed(SyncError):
    """One standard's release failures, each already logged where it happened.

    Carried as its own type so `sync_all` does not log them a second time. Every release that
    fails logs its own line naming the version and opening with the marker; the standard-level
    line repeated a single failure verbatim, and for several it printed a summary containing no
    marker at all - in a log an operator is told to alert on marker strings.
    """


class ProvenanceUnreadable(SyncError):
    """provenance.json is present, was read, and is not a record set. Real damage."""


class ProvenanceUnavailable(SyncError):
    """provenance.json could not be read at all: permissions, I/O, a mount gone.

    Kept apart from damage because the two want opposite responses. Damage on a published
    release is permanent and needs a person with an independent copy. Being unable to read a
    file right now is very often transient, or a mode this service repairs by itself, and
    telling an operator to restore from backup for it is both wrong and expensive.
    """


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
    """Load provenance.json. None means no prior record exists; damage raises.

    Absence and damage are different facts, and collapsing them is how a preservation
    registry loses an artifact quietly. Treating an unreadable record as a first run disables
    every frozen check for that release, so a release whose provenance had rotted would adopt
    whatever upstream happens to serve now and rewrite both records to agree with it,
    destroying the only evidence that the published bytes had ever been anything else.
    """
    if not prov_path.exists():
        return None
    try:
        raw = read_text(prov_path)
    except DecodeError as exc:
        # Bytes that are not text are damage, not unavailability. DecodeError rather than
        # UnicodeDecodeError, which is a ValueError and so used to escape both this guard and
        # the per-standard isolation in sync_all.
        raise ProvenanceUnreadable(f"{prov_path}: {_describe(exc)}") from exc
    except OSError as exc:
        raise ProvenanceUnavailable(f"{prov_path}: {_describe(exc)}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProvenanceUnreadable(f"{prov_path}: {_describe(exc)}") from exc
    if not is_provenance_record_set(data):
        raise ProvenanceUnreadable(f"{prov_path}: not a provenance record set")
    return data


def _describe(exc: BaseException) -> str:
    """A message that is never empty and always names the failure.

    httpx wraps a bare socket timeout with no message of its own, so `str(exc)` is `""` and
    indexing its first line raises IndexError from inside the very handler meant to contain
    the failure. The class name is the useful part in that case, and only in that case: a
    SyncError already reads as the operator-facing message the docs quote, and prefixing it
    with its own class name turns a documented marker into an implementation detail.
    """
    text = str(exc).strip()
    if not text:
        return type(exc).__name__
    # Our own exception types already read as the operator-facing message the docs quote, so
    # prefixing them with their class name turns a documented marker into an implementation
    # detail. Anything from a library gets named, because its message alone rarely says what
    # kind of failure it was.
    return text if isinstance(exc, (SyncError, DecodeError)) else f"{type(exc).__name__}: {text}"


def _served_digest(dest: Path) -> str | None:
    """Hash the copy we actually serve, or None if it cannot be read."""
    try:
        return sha256_hex(dest.read_bytes())
    except OSError:
        return None


def _served_matches(dest: Path, digest: str) -> bool:
    """Whether the copy we serve still hashes to *digest*.

    A file that cannot be read counts as drift rather than an error. The caller's response
    either way is to write the correct bytes over it, which is also the right answer to a bad
    sector or to a write that was interrupted before its rename.
    """
    return _served_digest(dest) == digest


def _fetch(url: str, client: httpx.Client) -> bytes:
    """Retrieve *url*, retrying the same transient faults the reachability probe retries.

    The retry started life in the dry run only, which made the CI gate strictly more tolerant
    than the sync it gates: an upstream answering 429 once and 200 on the third try passed
    `cairn sync --dry-run`, the pull request merged, and the deployment then failed the standard
    on a single un-retried GET. A gate that can pass what the real thing rejects is worse than
    no gate, which is the same argument that made the dry run fail on UNREACHABLE at all.
    """
    try:
        resp = _with_retry(lambda: client.get(url))
    except httpx.HTTPError as exc:
        # _describe, not str(exc): httpx wraps a bare socket timeout with no message of its own,
        # so str() is "" and the operator gets a marker with nothing after it. Swallowing the
        # exception entirely made a DNS failure, a TLS error, a connection reset and a 30s
        # timeout indistinguishable in the log and in stats.failures.
        raise SyncError(f"fetch failed [{_describe(exc)}] {url}") from exc
    if resp.status_code != 200:
        raise SyncError(f"fetch failed [{resp.status_code}] {url}")
    return resp.content


def _with_retry(send) -> httpx.Response:
    """Call *send*, retrying a fault that might be transient. Raises if every attempt failed.

    Shared by the probe and the fetch so the two cannot drift on what counts as transient. Only
    a fast failure is retried: the client allows 30s, so asking again after a hang costs the
    whole timeout a second and third time, across every artifact, during exactly the upstream
    incident that made it slow.

    A response outranks an exception when reporting the outcome. Overwriting the response with
    None on a later attempt lost a 429 or 503 seen on the first, and the caller then reported
    "no response" for what was a rate limit.
    """
    last_response: httpx.Response | None = None
    last_error: httpx.HTTPError | None = None
    for attempt in range(REACHABILITY_ATTEMPTS):
        started = _monotonic()
        try:
            resp = send()
            if resp.status_code < 400 or (
                resp.status_code < 500 and resp.status_code not in RETRYABLE_CLIENT_STATUS
            ):
                return resp
            last_response = resp
        except httpx.HTTPError as exc:
            last_error = exc

        if _monotonic() - started >= REACHABILITY_RETRY_BELOW_SECONDS:
            break
        if attempt + 1 < REACHABILITY_ATTEMPTS:
            # Jittered, because every CI job of every open pull request probes the same handful
            # of hosts, and a fixed backoff has them all come back at the same instant.
            _sleep(REACHABILITY_BACKOFF_SECONDS * (attempt + 1) * random.uniform(0.5, 1.5))

    if last_response is not None:
        return last_response
    raise last_error if last_error is not None else httpx.HTTPError("no attempt was made")


@dataclass
class SyncStats:
    fetched: int = 0
    verified: int = 0
    skipped: int = 0
    planned: int = 0
    releases_attempted: int = 0
    releases_failed: int = 0
    published: int = 0
    repaired: int = 0
    restored: int = 0
    recovered: int = 0
    unreadable: int = 0
    unreachable: int = 0
    # (standard id, message) for standards that failed. Collected rather than raised so one
    # bad upstream cannot stop every other standard from syncing.
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def nothing_succeeded(self) -> bool:
        """Every unit of work the run attempted failed, so it established nothing at all.

        Distinct from `not ok`: a pass where most of the corpus was checked is a verification
        with a problem in it, and a pass where none of it was is not a verification. Only the
        second must withhold the verify stamp.

        The unit is the release, not the standard, because a failing release no longer
        abandons its siblings. A run that attempted nothing must not report that everything
        failed, or it would withhold the stamp for a cycle that simply had no work.
        """
        return self.releases_attempted > 0 and self.releases_failed == self.releases_attempted



class Action(StrEnum):
    """What the commit phase will do with one artifact."""

    WRITE = "write"      # new or changed bytes
    SKIP = "skip"        # already correct on disk, leave it alone
    VERIFY = "verify"    # frozen, re-read and confirmed against the record
    RESTORE = "restore"  # frozen, the served copy was wrong; put the recorded bytes back


@dataclass
class PlannedArtifact:
    """One artifact's intended outcome. Nothing here has touched the filesystem yet."""

    name: str
    dest: Path
    action: Action
    record: dict
    data: bytes | None = None


@dataclass
class ReleasePlan:
    release: Release
    vdir: Path
    artifacts: list[PlannedArtifact]
    orphans: list[str]
    # provenance.json was present but unreadable, and this release was allowed to rebuild it
    damaged_metadata: bool = False
    # what provenance.json held when the plan was made; None for a first run or a rebuild.
    # Carried rather than re-read, so the commit phase cannot see a different file than the
    # one every decision above was based on.
    prior: dict | None = None
    # this cycle is the one that publishes the release, so the write-once guards were off
    publishing: bool = False
    # published but not served: read, not written, including its metadata
    dormant: bool = False

    @property
    def records(self) -> list[dict]:
        return [p.record for p in self.artifacts]


class Verdict(StrEnum):
    """What to do with one artifact, chosen by `_decide` and nothing else.

    The branch used to be selected by a chain of early returns over four release-level
    booleans and three artifact-level ones, and every fix added another condition to it. The
    combinations outgrew what anyone could hold in their head: a published release that had
    lost provenance.json *and one of its two artifacts* took the write path and adopted
    upstream in silence, which is the promise this module exists to keep.

    Naming the outcomes makes the choice a pure function of observable evidence, so the whole
    space can be enumerated in a test rather than reasoned about one path at a time.
    """

    FETCH = "fetch"                      # not enough evidence yet; get the bytes and ask again
    SKIP_DORMANT = "skip-dormant"        # published, not served: read nothing, write nothing
    SKIP_FROZEN = "skip-frozen"          # recorded, present, and this is not a verify pass
    SKIP_CORROBORATED = "skip-corroborated"  # upstream agrees with what is already served
    VERIFY = "verify"                    # frozen, re-read and confirmed against the record
    RESTORE = "restore"                  # the served copy is wrong or gone; put it back
    WRITE = "write"                      # new or changed bytes
    REFUSE_CHANGED = "refuse-changed"
    REFUSE_REPOINTED = "refuse-repointed"
    REFUSE_UNVERIFIABLE = "refuse-unverifiable"


@dataclass(frozen=True)
class Evidence:
    """Everything `_decide` is allowed to look at. No filesystem, no network, no manifest.

    Release-level facts come from the plan; artifact-level ones are read once by the caller.
    Keeping them in one frozen object is what makes the decision reproducible in a test: the
    invariant matrix drives states through the real sync, and this drives the branch directly,
    including combinations the matrix cannot express because its dimensions are release-wide.
    """

    mutable: bool          # the release may be overwritten in place
    dormant: bool          # published but not served
    publishing: bool       # this cycle is the release's first publication
    promised: bool         # published, and not by this cycle: the guards apply
    verify: bool           # --verify: re-read what would otherwise be skipped
    recorded: bool         # provenance holds a record for this artifact name
    recorded_sha: str | None
    moved: bool            # the manifest now resolves to different coordinates
    on_disk: bool
    served_sha: str | None   # digest of the served copy; None if missing or unreadable
    upstream_sha: str | None  # None until the bytes have been fetched


def _decide(e: Evidence) -> Verdict:
    """Choose one outcome for one artifact. Pure, total, and the only place branches live.

    Called twice: once before fetching, where `upstream_sha` is None and the answer may be
    FETCH, and once after, where it may not be.
    """
    if e.dormant:
        return Verdict.SKIP_DORMANT

    # Nothing to compare against without the bytes. A record with no checksum carries no claim
    # to trust, so it does not earn the fast path.
    frozen_fast_path = e.recorded and e.recorded_sha and e.on_disk and not e.mutable and not e.publishing
    if e.upstream_sha is None:
        return Verdict.SKIP_FROZEN if (frozen_fast_path and not e.verify) else Verdict.FETCH

    if e.promised:
        # Refusals first, and all of them, before any accept path. Ordering them after the
        # corroboration shortcut below let a repoint through: identical bytes at a new tag
        # returned early and rewrote the recorded origin to follow the manifest, which is the
        # one thing a published release's provenance must never do.
        if e.recorded_sha and e.upstream_sha != e.recorded_sha:
            return Verdict.REFUSE_CHANGED
        if e.recorded and e.moved:
            return Verdict.REFUSE_REPOINTED
        if not e.recorded_sha:
            # No checksum anywhere, so the served copy is the only witness to what was
            # published. Upstream agreeing settles it; anything else needs a person, because
            # a copy that disagrees, cannot be read, or is not there leaves no way to tell a
            # volume that lost bytes from an upstream that was re-tagged.
            #
            # Reached with no record at all only for an artifact this release is promising,
            # since a published release may not gain artifacts - `compare_to_baseline` refuses
            # that edit. So "no record and no file" is a lost artifact, not a new one. Reading
            # it as new is what let a release that lost provenance.json and one artifact write
            # whatever upstream now served into a write-once URL and record it as published.
            if e.served_sha != e.upstream_sha:
                return Verdict.REFUSE_UNVERIFIABLE
            return Verdict.SKIP_CORROBORATED

        if not e.on_disk:
            # Recorded, corroborated by upstream, and simply gone from the volume. Recoverable,
            # but a write-once URL was answering 404, so it takes the repair path rather than
            # being logged as an ordinary fetch.
            return Verdict.RESTORE

    if e.recorded and e.recorded_sha == e.upstream_sha and e.on_disk:
        # Upstream agrees with the record, which leaves the served copy as the only thing still
        # unchecked - and the only one anyone reads. Bit rot, a truncated write or a bad restore
        # all leave upstream and the record agreeing while the bytes on disk are wrong.
        if e.served_sha != e.upstream_sha:
            return Verdict.RESTORE
        return Verdict.VERIFY if frozen_fast_path else Verdict.SKIP_CORROBORATED

    return Verdict.WRITE


class SourceState(StrEnum):
    """How an artifact's recorded origin compares to where it now resolves."""

    SAME = "same"
    MOVED = "moved"
    UNRECORDED = "unrecorded"  # the record has no origin to compare against


def _source_state(record: dict, art: Artifact, resolved: Resolved) -> SourceState:
    """Compare an artifact's recorded coordinates with the ones it resolves to now.

    UNRECORDED is kept separate from MOVED on purpose. A legacy or hand-written record with
    no url is not evidence that anything was repointed, and conflating the two would make
    every such record look like a repoint - which, on a frozen release, is now refused.
    """
    prior = record.get("source")
    if not isinstance(prior, dict) or not prior.get("url"):
        return SourceState.UNRECORDED
    moved = (
        prior.get("from") != art.from_
        or prior.get("url") != resolved.url
        or prior.get("repo") != resolved.repo
        or prior.get("ref") != resolved.ref
    )
    return SourceState.MOVED if moved else SourceState.SAME


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


def _orphan_names(vdir: Path, current_names: set[str], prior: dict | None, damaged_metadata: bool) -> list[str]:
    """Files in a release directory that the manifest no longer declares.

    Normally provenance is the authority on what the sync put there, so only names it recorded
    are candidates and the render's output is safe by construction.

    A record that was rebuilt, or simply lost, carries no such list, and returning nothing
    there strands those files permanently: from the next cycle the record is intact and does
    not name them either, so no reaper can see them again. Both cases scan the directory
    itself, holding back the names neither this module nor the manifest owns. Unreachable for a
    published release, whose damaged record is refused before this point, so nothing here can
    remove a file under a write-once promise.
    """
    if prior:
        return sorted({a["name"] for a in prior["artifacts"]} - current_names)
    if not (damaged_metadata or vdir.is_dir()):
        return []
    # Deliberately not caught. Swallowing it returned "no orphans", which is indistinguishable
    # from "nothing to reap": the files this function exists to remove would keep serving
    # forever, with nothing logged, no counter moved and the run exiting 0 - the silence the
    # three-way ModeRepair outcome was introduced to end. Raising fails this one release, which
    # is reported, isolated from its siblings, and retried next cycle.
    present = [entry.name for entry in vdir.iterdir() if entry.is_file()]
    return sorted(
        name
        for name in present
        if name not in current_names
        and name not in GENERATED_NAMES
        and not name.startswith(TEMP_PREFIX)
    )


def _plan_release(
    std: Standard,
    rel: Release,
    root: Path,
    client: httpx.Client,
    *,
    verify: bool,
    log,
) -> ReleasePlan:
    """Resolve, fetch and check a release without writing anything.

    Every invariant is asserted here, before the commit phase touches the document root.
    Checking after writing was the earlier mistake: a refused manifest edit still left the
    replacement file published under a frozen version, absent from provenance and from
    SHA256SUMS, cached immutably, and beyond the reach of the orphan reaper because the
    reaper only considers names the previous provenance recorded.
    """
    vdir = site_dir(root) / std.id / f"v{rel.version}"
    mutable = rel.is_mutable

    # Damaged provenance on a published frozen release is not a first run. Rebuilding it would
    # adopt whatever upstream serves now, overwrite the published bytes with it, and rewrite
    # both records to match: the substitution and the destruction of its own evidence, in one
    # cycle, exiting 0. A mutable release has no such promise to keep, so it is rebuilt and
    # reported rather than refused.
    damaged_metadata = False
    prov_path = vdir / PROVENANCE_NAME
    try:
        prior = _load_prior_provenance(prov_path)
    except ProvenanceUnavailable as exc:
        # Not damage: the bytes may be perfectly intact. Repair what we can repair, refuse to
        # conclude anything this cycle, and let the next one try again. Demanding a backup
        # restore for a mode this service fixes by itself would be an expensive lie.
        repaired = ensure_published_mode(prov_path) is ModeRepair.REPAIRED
        raise SyncError(
            f"{Marker.PROVENANCE_UNAVAILABLE}: {std.id} v{rel.version}\n"
            f"  {exc}\n"
            + (
                "  The file's mode has been repaired, so the next cycle should be able to read it.\n"
                if repaired
                else "  Nothing about this release can be established until that file can be read.\n"
            )
            + "  Nothing was written, and the next cycle will try again."
        ) from exc
    except ProvenanceUnreadable as exc:
        if rel.ever_published:
            raise SyncError(
                f"{Marker.PROVENANCE_UNREADABLE}: {std.id} v{rel.version}\n"
                f"  {exc}\n"
                f"  This version is published and frozen, so this file is the only record of what\n"
                f"  was published. Rebuilding it from upstream would silently adopt whatever is\n"
                f"  there now. Restore it from a backup, or re-publish the version deliberately\n"
                f"  once the bytes on disk have been confirmed against an independent copy."
            ) from exc
        log(f"  [FIX]  {std.id} v{rel.version}: provenance unreadable ({exc}); rebuilding from upstream")
        damaged_metadata = True
        prior = None

    # The manifest decides whether this release has published; provenance only witnesses it.
    # That direction matters, because provenance lives on the volume these guards protect,
    # while the manifest's history is append-only and gated by `cairn validate --baseline`.
    # Refused rather than defaulted, because both defaults are wrong in opposite directions and
    # neither is detectable afterwards: read as a draft it turns every guard off, read as
    # published it takes the no-fetch fast path and freezes the draft era as the release.
    #
    # Checked for drafts too. Gating it on `ever_published` left the draft case defaulting to
    # published, so a record cairn could not read raised PUBLISHED VERSION UNFROZEN and told the
    # operator to fix a manifest that was already correct.
    recorded_lifecycle = prior.get("lifecycle") if prior is not None else None
    if prior is not None and recorded_lifecycle not in tuple(Lifecycle):
        raise SyncError(
            f"{Marker.PROVENANCE_UNREADABLE}: {std.id} v{rel.version}\n"
            f"  the record parsed, but its lifecycle is {recorded_lifecycle!r} rather than one of\n"
            f"  {', '.join(repr(str(m)) for m in Lifecycle)}. Whether this record describes a\n"
            f"  published era decides whether the write-once checks run, and guessing it either\n"
            f"  way is unrecoverable: one adopts whatever upstream serves now, the other freezes\n"
            f"  the draft that preceded the release. Restore the record from a backup, or confirm\n"
            f"  the served bytes against an independent copy and re-publish the version.\n"
            f"  Records written before cairn had this field are not migrated: this service has\n"
            f"  never published a version, so its volumes were rebuilt rather than upgraded."
        )

    recorded_publication = prior is not None and recorded_lifecycle != Lifecycle.DRAFT

    # A release does not *have* a published lifecycle, it *becomes* published, and that cycle
    # is the publication: what the manifest names now is what gets published, so it must be
    # fetched rather than assumed to be what is already on disk.
    #
    # The record outranks the files, because files alone cannot say which era wrote them: a
    # promotion always finds the draft era's bytes in the directory.
    if prior is not None:
        already_published_here = recorded_publication
    else:
        # No record at all, so the files are the only witness left. Bytes present under a
        # published version mean the record was lost and its artifacts were not; an empty
        # directory means there is nothing here to contradict, and the release is rebuilt.
        already_published_here = any((vdir / a.name).exists() for a in rel.artifacts)

    # A release whose whole directory was lost therefore republishes: nothing here contradicts
    # anything. That is a restoration rather than a silent adoption of drift only because the
    # schema requires a published release to pin its own `ref`, so "what upstream serves now"
    # and "what was published" are the same bytes by construction. It is reported either way.
    publishing_now = rel.ever_published and not already_published_here

    # The reverse. `cairn validate --baseline` refuses this on a pull request, but the syncer is
    # the last line and every other write-once violation is refused here too: without it, a
    # lifecycle reverted by any route that skipped the gate - a direct push to main, a manifest
    # edited in place on the deployment - would have the next sync overwrite published bytes and
    # report a clean cycle.
    if recorded_publication and mutable:
        raise SyncError(
            f"{Marker.PUBLISHED_VERSION_UNFROZEN}: {std.id} v{rel.version}\n"
            f"  provenance records this version as published; the manifest now says\n"
            f"  lifecycle '{rel.lifecycle}'. That un-freezes bytes that have already been handed\n"
            f"  out, and the next sync would overwrite them in place. Restore the lifecycle, or\n"
            f"  set served: false to stop serving it without un-publishing it. `cairn validate\n"
            f"  --baseline` catches this on a pull request, before it can reach a deployment."
        )

    # The guards below protect what a release has already published. Every one of them is off on
    # the cycle that publishes it, because on that cycle the manifest is not contradicting a
    # promise, it is making one. Reported all the same, because a publication nobody intended is
    # worth a line in the log, and it happens once per release.
    frozen_promise = rel.ever_published and not publishing_now

    prior_arts = {a["name"]: a for a in prior["artifacts"]} if prior else {}
    commit_cache: dict[tuple[str, str], str | None] = {}

    def commit_for(resolved: Resolved) -> str | None:
        if not (resolved.repo and resolved.ref):
            return None
        key = (resolved.repo, resolved.ref)
        if key not in commit_cache:
            commit_cache[key] = _resolve_commit(resolved.repo, resolved.ref, client)
        return commit_cache[key]

    # A published release that is not served is dormant: not fetched, compared, or written.
    # Nothing reads these URLs, and probing them anyway fails the standard forever when the
    # upstream tag has moved, which is the usual reason a release was withdrawn.
    #
    # Stated once here rather than as an `is_served` clause on each guard, and skipping rather
    # than exempting: an exempted guard still lets the fetched bytes be written.
    #
    # `not publishing_now`, because publishing a version that is not served still has to write
    # what it promises, or the draft's bytes wait to be served the moment it is restored.
    dormant = rel.ever_published and not rel.served and not publishing_now

    # Checked before the artifact loop so that the precise refusal wins: a manifest that drops
    # one artifact and adds another would otherwise be refused for the replacement, which is
    # true but names the wrong file.
    #
    # Dormant releases are not reaped: "left alone" has to mean the whole directory, or
    # un-serving a release would quietly delete artifacts it is still promising.
    current_names = {art.name for art in rel.artifacts}
    orphans = [] if dormant else _orphan_names(vdir, current_names, prior, damaged_metadata)

    # Deleting a file from a frozen release turns a published 200 into a 404, which breaks
    # the same promise as changing its bytes. Withdrawn releases are exempt: that status
    # unpublishes the whole release deliberately and already answers 410.
    if orphans and frozen_promise:
        raise SyncError(
            f"{Marker.FROZEN_VERSION_LOST_AN_ARTIFACT}: {std.id} v{rel.version}\n"
            f"  no longer in the manifest: {', '.join(orphans)}\n"
            f"  lifecycle is '{rel.lifecycle}', so these URLs are published and must keep resolving.\n"
            f"  Restore the artifact entries, or publish the change as a new version.\n"
            f"  `cairn validate --baseline` catches this on a pull request, before it reaches\n"
            f"  a deployment; if it has already been deployed, restoring the entries is the\n"
            f"  only fix that keeps the published URLs working."
        )

    planned: list[PlannedArtifact] = []
    for art in rel.artifacts:
        resolved = resolve(std, rel, art, client)
        dest = vdir / art.name
        frozen = prior_arts.get(art.name)

        evidence = Evidence(
            mutable=mutable,
            dormant=dormant,
            publishing=publishing_now,
            promised=frozen_promise,
            verify=verify,
            recorded=frozen is not None,
            recorded_sha=(frozen or {}).get("sha256"),
            moved=_source_state(frozen or {}, art, resolved) is SourceState.MOVED,
            # UNRECORDED is deliberately not `moved`: a record with no coordinates is not
            # evidence of a repoint. It still has to gain them, which the caller handles.
            on_disk=dest.exists(),
            served_sha=None,
            upstream_sha=None,
        )

        verdict = _decide(evidence)
        data = digest = None
        if verdict is Verdict.FETCH:
            data = _fetch(resolved.url, client)
            digest = sha256_hex(data)
            evidence = replace(evidence, upstream_sha=digest, served_sha=_served_digest(dest))
            verdict = _decide(evidence)
            assert verdict is not Verdict.FETCH, "the second decision may not ask for the bytes again"

        if verdict is Verdict.REFUSE_CHANGED:
            raise SyncError(
                f"{Marker.FROZEN_VERSION_CHANGED}: {std.id} v{rel.version}/{art.name}\n"
                f"  recorded sha256 {evidence.recorded_sha}\n"
                f"  upstream sha256 {digest}\n"
                f"  A released version's bytes must never change. Cut a new version instead."
            )
        if verdict is Verdict.REFUSE_REPOINTED:
            raise SyncError(
                f"{Marker.FROZEN_VERSION_REPOINTED}: {std.id} v{rel.version}/{art.name}\n"
                f"  recorded source {frozen['source'].get('url')}\n"
                f"  manifest now    {resolved.url}\n"
                f"  A released version's recorded origin is part of what was published, so it\n"
                f"  cannot be amended to follow the manifest. Restore the coordinates, or\n"
                f"  publish the new source as a new version. `cairn validate --baseline` catches\n"
                f"  this on a pull request, before it can reach a deployment."
            )
        if verdict is Verdict.REFUSE_UNVERIFIABLE:
            raise SyncError(
                f"{Marker.UNVERIFIABLE_PUBLISHED_FILE}: {std.id} v{rel.version}/{art.name}\n"
                f"  on disk  {evidence.served_sha or 'missing, or could not be read'}\n"
                f"  upstream sha256 {digest}\n"
                f"  This version is published, but no checksum was ever recorded for it, so\n"
                f"  there is nothing here that can say whether the served copy was lost or\n"
                f"  changed, or whether upstream has been re-tagged. Writing upstream's bytes\n"
                f"  would settle that by assumption and destroy the evidence. Confirm against\n"
                f"  an independent copy, then restore the file or publish a new version."
            )

        if verdict is Verdict.SKIP_DORMANT:
            # No record means nothing to carry into provenance.json. A published release cannot
            # gain artifacts, so this is only reachable from an edit that predates that gate;
            # leaving it unplanned keeps "dormant writes nothing" true.
            if frozen is not None:
                planned.append(PlannedArtifact(art.name, dest, Action.SKIP, frozen))
            continue
        if verdict is Verdict.SKIP_FROZEN:
            planned.append(PlannedArtifact(art.name, dest, Action.SKIP, frozen))
            continue

        # The recorded origin is part of what a published release published, so it is never
        # rewritten to follow the manifest - a repoint was refused above. A draft may move, and
        # a record that never carried coordinates should gain them.
        # A record with no checksum gains one here: upstream corroborated the served bytes, so
        # this is the first cycle able to state what they are. A repoint on a published release
        # was refused above, so rebuilding cannot quietly adopt new coordinates.
        record = frozen
        source_state = _source_state(frozen or {}, art, resolved)
        if (
            record is None
            or not record.get("sha256")
            or ((mutable or publishing_now) and source_state is not SourceState.SAME)
        ):
            record = _artifact_record(art, resolved, data, digest, commit_for(resolved))

        if verdict is Verdict.RESTORE:
            planned.append(PlannedArtifact(art.name, dest, Action.RESTORE, record, data))
        elif verdict is Verdict.VERIFY:
            planned.append(PlannedArtifact(art.name, dest, Action.VERIFY, record))
        elif verdict is Verdict.SKIP_CORROBORATED:
            # Keeping the file exactly as it is. Rewriting installs a new inode, moving the
            # mtime and cache validators of content that did not change, under URLs nginx
            # serves as immutable.
            planned.append(PlannedArtifact(art.name, dest, Action.SKIP, record))
        else:
            planned.append(
                PlannedArtifact(
                    art.name, dest, Action.WRITE,
                    _artifact_record(art, resolved, data, digest, commit_for(resolved)), data,
                )
            )

    # Asserted before the commit phase writes anything: raising from the commit loop would
    # leave the release half written. No path above can produce a record without a checksum
    # today, so this is a backstop, kept honest by a test that forces the condition.
    #
    # Skipped entries are excluded: a dormant release carries its prior record forward verbatim
    # and writes no metadata, so a checksum-less record has nothing here to protect.
    missing = [
        p.name for p in planned if p.action is not Action.SKIP and not p.record.get("sha256")
    ]
    if missing:
        raise SyncError(
            f"{Marker.NO_CHECKSUM_RECORDED}: {std.id} v{rel.version}\n"
            f"  {', '.join(missing)}\n"
            f"  SHA256SUMS cannot be written without one, and a record carrying no checksum\n"
            f"  silently disables every later integrity comparison for that file."
        )

    return ReleasePlan(rel, vdir, planned, orphans, damaged_metadata, prior, publishing_now,
                       dormant)


def _commit_release(std: Standard, plan: ReleasePlan, stats: SyncStats, *, log) -> None:
    """Apply a checked plan, counting into the caller's *stats*.

    Every failure mode the plan can rule out has been ruled out, but the writes themselves can
    still fail on the volume, and allocating a local SyncStats and returning it meant such a
    failure discarded everything this release had already done. The counters are the only
    source of the operator markers, so a file restored just before a full disk was rewritten
    on the volume while the log said nothing - the same discard already fixed one level up,
    left in place one level down.
    """
    rel = plan.release
    plan.vdir.mkdir(parents=True, exist_ok=True)

    # Clear any temp file stranded by a previous run that was killed mid-write. Done here
    # rather than in the plan phase so a refused plan cannot delete a file another process
    # is in the middle of writing. The render sweeps the whole document root as well, which
    # covers this directory too when a build follows; this call is what keeps a standalone
    # `cairn sync` from leaving litter behind for a build that may never come.
    strays = reap_temp_files(plan.vdir)
    if strays:
        log(f"  [tidy] {std.id} v{rel.version}: removed {strays} stranded temp file(s)")

    for item in plan.artifacts:
        if item.action is Action.WRITE:
            atomic_write(item.dest, item.data)
            stats.fetched += 1
            log(
                f"  [get]  {std.id} v{rel.version}/{item.name} "
                f"({item.record['bytes']} bytes, sha256 {item.record['sha256'][:12]}…)"
            )
            continue

        if item.action is Action.RESTORE:
            lost = not item.dest.exists()
            atomic_write(item.dest, item.data)
            stats.restored += 1
            what = "was missing from the volume" if lost else (
                f"did not match its recorded sha256 {item.record['sha256'][:12]}…")
            log(f"  [FIX]  {std.id} v{rel.version}/{item.name} {what}; restored from upstream")
            continue

        # Nothing is rewritten for these, so a file left unreadable by an earlier bug would
        # stay a 403 indefinitely. Repair the mode in place instead; it needs no re-fetch.
        repair = ensure_published_mode(item.dest)
        if repair is ModeRepair.REPAIRED:
            stats.repaired += 1
            log(f"  [mode] {std.id} v{rel.version}/{item.name} -> 0644")
        elif repair is ModeRepair.FAILED:
            stats.unreadable += 1
            log(
                f"  [WARN] {std.id} v{rel.version}/{item.name} is not readable by the web "
                f"server and the mode could not be changed; this URL will answer 403"
            )

        # WRITE and RESTORE both continued above, so only these two remain. There is no
        # else: raising from the middle of the commit loop would leave the release half
        # written with its metadata never updated, which is worse than a miscount.
        if item.action is Action.VERIFY:
            stats.verified += 1
        elif item.action is Action.SKIP:
            stats.skipped += 1

    # Only reachable for mutable or withdrawn releases; the frozen case raised in the plan.
    for orphan_name in plan.orphans:
        orphan = plan.vdir / orphan_name
        if orphan.exists():
            orphan.unlink()
            log(f"  [del]  {std.id} v{rel.version}/{orphan_name}")

    _write_release_metadata(std, plan, stats, log=log)

    # Counted here, after the write it describes, like fetched and restored above it. Counted
    # on entry it reported DAMAGED RECORD(S) REBUILT and the INTEGRITY CHECK FAILED block for a
    # record still rotted on disk whenever the metadata write then failed - and these counters
    # are the only source of those markers, so the operator was told a repair had happened that
    # had not.
    if plan.damaged_metadata:
        stats.recovered += 1
    if plan.publishing:
        stats.published += 1
        log(f"  [pub]  {std.id} v{rel.version} published; write-once now applies to it")


def _keyed_by_name(pairs: list[tuple[str, object]] | None) -> dict | None:
    """Key (name, value) pairs by name, or None if the set is not wholly intact.

    One implementation, because both callers need the same rule and stating it twice is how they
    came to disagree: the record side rejected a duplicated entry and the SHA256SUMS side did
    not, so a file listing one artifact twice - with a wrong digest on the first line - compared
    equal to the correct set and stayed published, while `sha256sum -c` reported it FAILED.

    The rule is that ordering must not read as a change but damage must. A duplicate is damage:
    there is no reading of two entries for one name that a single mapping preserves.
    """
    if pairs is None:
        return None
    keyed = {name: value for name, value in pairs}
    return keyed if len(keyed) == len(pairs) else None


def _parse_sums(data: bytes | None) -> list[tuple[str, str]] | None:
    """Split a SHA256SUMS body into (name, digest) pairs, or None if any line does not parse.

    Bytes in, because decoding a file whose whole job is to detect damage is a way to crash on
    damage. Skipping unreadable lines made a corrupted file compare equal to a correct one.
    """
    if not data:
        return None
    pairs: list[tuple[str, str]] = []
    for line in data.split(b"\n"):
        if not line:
            continue
        digest, separator, name = line.partition(b"  ")
        if not (digest and separator and name):
            return None
        try:
            pairs.append((name.decode("utf-8"), digest.decode("ascii")))
        except UnicodeDecodeError:
            return None
    return pairs


def _write_release_metadata(std: Standard, plan: ReleasePlan, stats: SyncStats, *, log) -> None:
    """Rewrite provenance.json and SHA256SUMS only when the recorded set actually changed.

    These live beside write-once artifacts and are documented as permanent. Rewriting them
    every cycle purely to move `updated_at` churns the mtime of files nothing has changed
    and invalidates their validators for no reason.
    """
    rel = plan.release
    records = plan.records
    prior = plan.prior
    prov_path = plan.vdir / PROVENANCE_NAME
    sums_path = plan.vdir / SUMS_NAME

    # A dormant release was read and not written, and that has to include its metadata: its
    # records are the prior ones carried forward verbatim, so there is nothing to restate. It
    # also may not be restatable - a legacy or hand-restored record with no checksum cannot be
    # rendered into SHA256SUMS at all, and building it anyway raised KeyError from inside the
    # metadata writer for a release nothing had touched.
    if plan.dormant:
        # One exception to "writes nothing": the record has to stop claiming the release is
        # served, or provenance contradicts nginx for as long as it stays withdrawn - and
        # dormancy is sticky, so no later cycle could ever repair it. Only that field is
        # touched, and SHA256SUMS is left alone, because the artifact records are the prior
        # ones carried forward and may predate checksum recording entirely.
        if prior is not None and prior.get("served") != rel.served:
            atomic_write(prov_path, (json.dumps({**prior, "served": rel.served}, indent=2) + "\n").encode())
            log(f"  [meta] {std.id} v{rel.version}/{PROVENANCE_NAME} (served: {rel.served})")
        return

    provenance = {
        "standard": std.id,
        "version": rel.version,
        "lifecycle": str(rel.lifecycle),
        "served": rel.served,
        "generated_by": "cairn sync",
        "updated_at": _now(),
        "artifacts": records,
    }

    expected_sums = "".join(f"{r['sha256']}  {r['name']}\n" for r in records).encode()

    # SHA256SUMS is compared by content, not merely by existence. The two files are written
    # by separate calls, so a kill between them leaves one stale; checking only that the file
    # is present would then let the early return below make that divergence permanent, in the
    # file users are told to run `sha256sum -c` against. Compared as bytes, because decoding
    # a file whose whole purpose is to detect damage is a way to crash on damage.
    try:
        sums_current = sums_path.read_bytes()
    except OSError:
        sums_current = None

    # Compared as a mapping by name, which is how both readers index these, not as a list.
    # Ordering alone made this False, so reordering two artifact entries in a manifest rewrote
    # both files with a fresh updated_at - installing new inodes and moving the cache validators
    # of the two files this function exists to leave alone.
    def entries(value) -> list[tuple[str, object]] | None:
        return [(a["name"], a) for a in value] if isinstance(value, list) else None

    recorded = _keyed_by_name(entries(prior.get("artifacts"))) if prior is not None else None
    on_disk_sums = _keyed_by_name(_parse_sums(sums_current))
    expected_sums_by_name = _keyed_by_name([(r["name"], r["sha256"]) for r in records])

    if (
        prior is not None
        and prior.get("lifecycle") == rel.lifecycle
        and prior.get("served") == rel.served
        and recorded is not None
        and recorded == _keyed_by_name(entries(records))
        and on_disk_sums is not None
        and on_disk_sums == expected_sums_by_name
    ):
        for meta_path in (prov_path, sums_path):
            # Counted, not just logged. A warning nothing tallies produces no marker and no
            # exit code, which is the same silence the ModeRepair outcomes exist to end.
            if ensure_published_mode(meta_path) is ModeRepair.FAILED:
                stats.unreadable += 1
                log(f"  [WARN] {std.id} v{rel.version}/{meta_path.name} will answer 403; mode not repairable")
        return

    # SHA256SUMS first, provenance last. provenance.json is what the next run compares
    # against, so writing it last means an interruption leaves the pair looking out of date
    # rather than falsely up to date, and the next cycle rewrites both.
    atomic_write(sums_path, expected_sums)
    atomic_write(prov_path, (json.dumps(provenance, indent=2) + "\n").encode())
    log(f"  [meta] {std.id} v{rel.version}/{PROVENANCE_NAME}, {SUMS_NAME}")


def _plan_dry_run(std: Standard, rel: Release, client: httpx.Client, stats: SyncStats, *, log) -> None:
    """Resolve and probe without writing, counting into the caller's stats like every other
    step here. Allocating a throwaway and merging it was a second convention for one job.

    A dry run exists to answer whether the upstreams are reachable, and CI has a step named
    for exactly that. Logging UNREACHABLE and exiting 0 made it a gate that cannot fail, which
    is worse than no gate because it reports green.

    Raised per release rather than summed per standard, so it lands in the same isolation
    every other release failure does: the release is the unit that failed, the marker opens
    the message the way the runbooks expect, and one unreachable release is not counted as
    the whole standard having established nothing.
    """
    missed = 0
    for art in rel.artifacts:
        resolved = resolve(std, rel, art, client)
        ok = _reachable(resolved.url, client)
        log(f"  [plan] {std.id} v{rel.version}/{art.name} <- {resolved.url} {'OK' if ok else 'UNREACHABLE'}")
        stats.planned += 1
        if not ok:
            stats.unreachable += 1
            missed += 1

    if missed:
        raise SyncError(
            f"{Marker.UPSTREAM_UNREACHABLE}: {std.id} v{rel.version}\n"
            f"  {missed} artifact(s) could not be reached; see the [plan] lines above.\n"
            f"  A sync would fail on each of them."
        )


def sync_standard(
    std: Standard,
    root: Path,
    client: httpx.Client,
    *,
    verify: bool = False,
    dry_run: bool = False,
    log=print,
    stats: SyncStats | None = None,
) -> SyncStats:
    """Replicate every release of one standard, accumulating into *stats* if given.

    The caller passes its own stats so that a release which raises cannot discard the
    counters of the releases already committed before it. Returning them instead meant a
    restored file was written to disk and then reported as `restored == 0`, because the
    return value never happened - and those counters are the only source of the operator
    markers, so a published file was silently rewritten and the log said nothing.

    Releases are isolated from one another for the same reason standards are. They are
    independent units - separate directories, separate provenance, separate promises - but a
    raise used to abandon every release after the failing one *and still let the run count as
    complete*, because the standard was recorded as failed and `sync_all` returned an exit
    code meaning "ran to the end". The loop then wrote the verify stamp, recording a
    verification of releases it had never read and suppressing the next attempt for a full
    interval. That is precisely the pathology the exit-code split exists to prevent, one level
    below where it was fixed.

    Every failure is still reported: they are collected and raised together, so a standard
    with a bad release remains one failed standard rather than becoming several. The counters
    stay release-granular, because that is the unit the run either established something about
    or did not.
    """
    stats = SyncStats() if stats is None else stats
    failures: list[str] = []

    for rel in std.releases:
        stats.releases_attempted += 1
        try:
            if dry_run:
                _plan_dry_run(std, rel, client, stats, log=log)
                continue
            plan = _plan_release(std, rel, root, client, verify=verify, log=log)
            _commit_release(std, plan, stats, log=log)
        except Exception as exc:
            # As broad as the per-standard handler, and for the same reason: the faults worth
            # naming are a refused manifest edit, a network fault and a failing volume, but
            # the one that actually escaped was a KeyError from a malformed record. A bug in
            # one release's path must not cost the others.
            message = _describe(exc)
            stats.releases_failed += 1
            failures.append(message)
            # Logged here, at the version that failed, and deliberately not again by
            # sync_all: this line opens with the marker the runbooks name, which a
            # standard-level summary of several failures cannot.
            log(f"  [FAIL] {std.id} v{rel.version}: {message.splitlines()[0]}")

    if failures:
        raise StandardFailed(_combined_failure(std.id, failures, len(std.releases)))
    return stats


def _combined_failure(std_id: str, failures: list[str], attempted: int) -> str:
    """One message for one standard, however many of its releases failed.

    A single failure is passed through untouched, so `cairn sync`'s stderr block still opens
    with the marker rather than with a count of one.

    Every entry is now one failed release - the dry run's refusal included, which used to be
    summed per standard and appended afterwards - so the ratio is honest and cannot claim more
    failures than the standard has releases.
    """
    if len(failures) == 1:
        return failures[0]
    return (
        f"{len(failures)} of {attempted} release(s) of {std_id} failed:\n\n"
        + "\n\n".join(failures)
    )


# A dry run is what CI runs to gate a pull request, so an unreachable upstream reddens the
# branch. That is the point, but it also means one reset connection to raw.githubusercontent
# .com can fail a change that has nothing to do with the manifests, so a fault that might be
# transient is retried before it is believed.
REACHABILITY_ATTEMPTS = 3
REACHABILITY_BACKOFF_SECONDS = 1.0

# Only a *fast* failure is retried. util.http_client() allows 30s, so a host that accepts the
# connection and then hangs costs the full timeout, and two more of those turn a gate that
# fails in seconds into one that takes minutes to fail anyway - across every artifact of every
# release, serially, during exactly the upstream incident that made it slow. A reset or a
# refused port comes back in milliseconds, and that is the case retrying was added for.
REACHABILITY_RETRY_BELOW_SECONDS = 5.0

# Client errors that are not a statement about the resource. raw.githubusercontent.com answers
# 429 to an unauthenticated client over its rate limit, which is exactly what a fork's pull
# request is - secrets.GITHUB_TOKEN is unavailable there - so treating every 4xx as definitive
# reddened branches that had nothing to do with the manifests, the very thing the retry is for.
RETRYABLE_CLIENT_STATUS = frozenset({408, 429})

# Bound here so tests can replace them without patching the stdlib for the whole interpreter.
# `mock.patch("cairn.sync.time.sleep")` resolves to the time module itself and sets the
# attribute there, so the stub reaches pytest's own timing, the watchdog thread in the shell
# tests, and anything else consulting the clock while the test runs.
_sleep = time.sleep
_monotonic = time.monotonic


def _reachable(url: str, client: httpx.Client) -> bool:
    """Whether *url* answers. Fast transient faults are retried; anything else is believed.

    Most 4xx answers are the upstream telling us something true - a repo made private, a
    deleted tag, a renamed branch - and asking again neither changes the answer nor makes CI
    any more informative. A transport error, a 5xx, or one of the few 4xx codes that describe
    the request rather than the resource is worth one more look, provided it came back quickly
    enough that looking again is cheap.
    """
    def probe() -> httpx.Response:
        resp = client.head(url)
        if resp.status_code in (405, 501):  # server dislikes HEAD; try a lightweight GET
            resp = client.get(url, headers={"Range": "bytes=0-0"})
        return resp

    try:
        return _with_retry(probe).status_code < 400
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
                sync_standard(std, root, client, verify=verify, dry_run=dry_run, log=log, stats=total)
            except StandardFailed as exc:
                # Recorded, not re-logged. Every release inside already logged a line naming
                # its version and opening with the marker. A second line here duplicated a
                # lone failure verbatim, and for several printed a summary with no marker in
                # it at all - in the log operators are told to alert on marker strings.
                total.failures.append((std.id, str(exc)))
            except Exception as exc:
                # Deliberately broad. The named failures are a refused manifest edit
                # (SyncError), a network fault (httpx) and a full or read-only volume
                # (OSError), but listing them meant any unforeseen fault - a KeyError from one
                # malformed record was the real case - escaped the loop, skipped every
                # remaining standard and stopped the render. For a daemon whose whole purpose
                # is that one bad standard cannot take the registry down, a bug in one
                # standard's path is exactly the thing that must be isolated and reported
                # rather than propagated. KeyboardInterrupt and SystemExit are BaseException
                # and still stop the run.
                message = _describe(exc)
                total.failures.append((std.id, message))
                log(f"  [FAIL] {std.id}: {message.splitlines()[0]}")
    return total
