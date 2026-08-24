"""Small shared helpers: HTTP client, hashing, MIME inference, semver, durable writes."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from datetime import datetime
from enum import StrEnum
from pathlib import Path

import httpx

from . import __version__

# Everything written into the document root is read by nginx running as an unprivileged
# user, and nginx answers 403 for a file it cannot open. `tempfile.mkstemp` creates 0600
# and `os.replace` carries that mode onto the destination, so the mode is set explicitly
# rather than inherited from the temp file's default.
PUBLISHED_MODE = 0o644

# Files only. Directories are left to `umask 022`, set by the syncer before it runs (see
# deploy/sync-loop.sh), so every directory this service creates is 0755 by construction.
# Setting them explicitly here instead means walking a path and widening what it finds, and
# `path.parents` does not stop at anything in particular.

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


class DecodeError(ValueError):
    """A file's bytes are not valid UTF-8.

    Separate from being unable to read the file at all, because the two mean different things
    about the volume and want different responses. Named rather than left as UnicodeDecodeError
    because that is a ValueError, so it slips past every `except OSError` in the codebase and
    arrives at the top level as a traceback - which took the whole registry down when it
    happened during a render.
    """


def read_text(path: Path) -> str:
    """Read *path* as UTF-8, raising DecodeError instead of UnicodeDecodeError.

    Every caller has to handle both "cannot read it" and "read it, and it is not text". Only
    the first was ever caught, in five separate places.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        # No path in the message. Every caller has one and adds it; including it here put it
        # in the operator-facing text twice, and the class name made three.
        raise DecodeError(f"not valid UTF-8 (byte {exc.start}: {exc.reason})") from exc


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# The manifest schema constrains artifact names to this, but provenance.json is written by
# cairn and read back without a schema, so nothing checked it on the way in. Every name in it
# is resolved against a release directory, and one of them is passed to unlink() by the orphan
# reaper: a record holding "../../../something" deleted a file outside the document root and
# the run reported success.
# The negative lookahead is the whole point: "." and ".." match the character class, so without
# it a record naming ".." was accepted, listed as an orphan, and reached (vdir / "..").unlink()
# - which raises EISDIR, failing that release on every cycle with an OSError carrying no marker.
# `\Z`, not `$`: `$` also matches before a trailing newline, so "demo.xsd\n" passed a check
# whose whole job is to insist on a bare filename - and that name is joined onto the release
# directory and handed to unlink() and atomic_write().
#
# No leading dot, which rules out more than `.` and `..`. `.cairn-tmp-*` is what the stranded
# temp-file reaper globs for, so an artifact named that way was published, recorded, and then
# deleted by the tidy step with nothing to rewrite it. And deploy/nginx.conf answers 404 for
# any path segment starting with a dot, so every such artifact would be recorded as published
# under a URL that can never resolve.
_SAFE_ARTIFACT_NAME = re.compile(r"\A[A-Za-z0-9_-][A-Za-z0-9._-]*\Z")

# A rules revision label is a dated `YYYY-MM` or `YYYY-MM-DD`, and it is checked here for the
# same two reasons the artifact name above is. It becomes a path segment that is joined onto
# the document root and handed to mkdir(), so `..` in it escapes the tree; and the schema's
# copy of this pattern has to be ECMA-262, where `$` also matches before a trailing newline.
#
# Dated rather than free-form so that "newest" is decidable by sorting the labels. The moving
# `latest` pointer resolves by that order, and a label nobody can order leaves the pointer's
# target depending on manifest sequence. It also keeps the word `latest` out of the revision
# space, so a revision can never shadow the pointer that resolves to it.
_SAFE_REVISION = re.compile(r"\A\d{4}-(0[1-9]|1[0-2])(-(0[1-9]|[12][0-9]|3[01]))?\Z")


def is_dated_revision(label: str) -> bool:
    """Whether *label* is a real calendar date, `YYYY-MM` or `YYYY-MM-DD`.

    The shape is not enough, because sorting is what the moving `latest` pointer resolves by
    and a label that sorts high wins it. `2026-13` is not a month, but it sorts above every
    real revision of 2026 and would take the pointer from them; so would `2026-99`. A published
    revision can never be removed from the manifest either, so the typo and the URL it reserves
    are permanent - `served: false` hides it and hands the pointer back, but the segment stays
    spent.

    The regex rules out the months and the day numbers that can never exist; the parse rules out
    the ones that exist only in some months, which no pattern can express.
    """
    if not _SAFE_REVISION.match(label):
        return False
    try:
        datetime.strptime(label, "%Y-%m-%d" if len(label) == 10 else "%Y-%m")
    except ValueError:
        return False
    return True


def is_provenance_record_set(data: object) -> bool:
    """Whether *data* is a provenance document the rest of this codebase can index into.

    Both readers key every record by name, so a structurally malformed document surfaces as a
    TypeError or KeyError from deep inside a loop: the sync's plan phase, the render's page
    loop. Two copies of this check drifted apart once already - render learned to reject valid
    JSON of the wrong shape a release after the sync did.

    The name is checked, not merely its type. Callers join it onto a release directory and one
    of them unlinks the result, so a record is only usable if every name in it is a bare
    filename. Rejecting it here makes a record holding a traversal name damage, which is what
    it is, rather than an instruction.

    It lives here rather than in sync because it is a pure predicate over a parsed document,
    and importing it from sync gave the renderer a dependency on the one module in the package
    that does network I/O, pointing the arrow from render to sync for four lines of structure.
    """
    return (
        isinstance(data, dict)
        and isinstance(data.get("artifacts"), list)
        and all(
            isinstance(a, dict)
            and isinstance(a.get("name"), str)
            and _SAFE_ARTIFACT_NAME.match(a["name"]) is not None
            for a in data["artifacts"]
        )
    )


# Temp files are created in the destination directory, which for most callers is inside the
# public document root, so they need a name that is both recognisable for cleanup and not
# served. nginx denies dotfiles (deploy/nginx.conf); a random mkstemp name was neither.
TEMP_PREFIX = ".cairn-tmp-"


def _fsync_dir(directory: Path) -> None:
    """Persist a rename itself, not just the bytes it points at.

    Best-effort: some filesystems refuse to fsync a directory, and failing to harden a
    rename is not a reason to fail a write that has already succeeded.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    """Write bytes via a temp file in the same directory, then rename over the target.

    Every write into the document root goes through here, so a reader never observes a
    partial file: the syncer can be killed mid-cycle and the render step rewrites pages in
    place while nginx is serving them.

    `os.fdopen` takes ownership of the descriptor so it is closed exactly once, including
    when the write itself fails. Closing it twice by hand would raise EBADF from the second
    attempt, masking the real error and skipping the temp-file cleanup.

    The data is fsynced before the rename and the directory after it. Without that, rename is
    atomic against other readers but not against power loss: on a crash the metadata can land
    while the blocks behind it do not, leaving a published artifact that is zero-length or
    partly zeroed while its name and its recorded checksum both insist it is intact. That is
    the one failure this service is least able to detect after the fact.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=TEMP_PREFIX)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, PUBLISHED_MODE)
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _reap(paths) -> int:
    """Delete each path, counting successes. Best-effort, and never raises."""
    removed = 0
    try:
        candidates = list(paths)
    except OSError:
        return 0
    for stray in candidates:
        try:
            stray.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def reap_temp_files(directory: Path) -> int:
    """Delete leftover temp files in *directory*, returning how many were removed.

    A process killed between creating a temp file and renaming it leaves the file behind.
    Nothing else would ever remove it: the orphan reaper only considers names recorded in
    provenance, so a stray would accumulate permanently in a directory documented as
    write-once. Best-effort, and never a reason to fail a sync.
    """
    return _reap(directory.glob(f"{TEMP_PREFIX}*"))


def reap_temp_tree(directory: Path) -> int:
    """Same, for every directory under *directory*.

    The sync only reaps release directories belonging to a plan it finished, which leaves out
    everything the render writes (the registry index, the per-standard pages, the namespace
    documents, the assets) and the release directories of any standard whose plan raised.
    """
    return _reap(directory.rglob(f"{TEMP_PREFIX}*"))


class ModeRepair(StrEnum):
    """Outcome of a permission repair. FAILED and UNCHANGED must not look alike."""

    UNCHANGED = "unchanged"  # already readable, or not there to repair
    REPAIRED = "repaired"    # widened to PUBLISHED_MODE
    FAILED = "failed"        # still unreadable, and we cannot fix it


def ensure_published_mode(path: Path) -> ModeRepair:
    """Widen *path* to PUBLISHED_MODE if it is not already readable by the web server.

    Frozen artifacts are never rewritten, so a file left unreadable by an earlier bug would
    stay a 403 forever. Repairing it here costs one stat per sync and needs no re-fetch.

    Failing is not a reason to fail the sync - a read-only mount or a file owned by another
    user is out of our hands - but it must be distinguishable from success. Returning a bare
    False for both meant a permanent 403 was reported identically to "nothing to do", so
    nothing counted it and nothing logged it.
    """
    try:
        current = path.stat().st_mode & 0o777
    except FileNotFoundError:
        return ModeRepair.UNCHANGED
    except OSError:
        return ModeRepair.FAILED
    if current == PUBLISHED_MODE:
        return ModeRepair.UNCHANGED
    try:
        path.chmod(PUBLISHED_MODE)
    except OSError:
        return ModeRepair.FAILED
    return ModeRepair.REPAIRED


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
