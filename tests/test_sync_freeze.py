import json
import os
import stat
from pathlib import Path
from unittest import mock

import httpx
import pytest

from fakes import FakeClient as _FakeClient
from cairn.config import site_dir
from cairn.manifest import Lifecycle, Artifact, MajorLine, Release, Source, Standard, Steward
from cairn.sync import SyncError, SyncStats, sync_all, sync_standard
from cairn.sync import _artifact_record as S_artifact_record
from cairn.sync import resolve as S_resolve
from cairn.nginx import write_routes
from cairn.render import render_site
from cairn.util import (
    TEMP_PREFIX,
    is_provenance_record_set,
    ModeRepair,
    atomic_write,
    ensure_published_mode,
    reap_temp_tree,
    sha256_hex,
)


def _std(lifecycle=Lifecycle.PUBLISHED, artifacts=None, served=True):
    if artifacts is None:
        artifacts = [Artifact(name="demo.xsd", role="schema", from_="repo", path="demo.xsd")]
    rel = Release(version="1.0.0", lifecycle=lifecycle, served=served, artifacts=artifacts, ref="main")
    return Standard(
        id="demo",
        title="Demo",
        summary="s",
        steward=Steward(org="x"),
        source=Source(type="github", repo="o/r", ref="main"),
        major_lines=[MajorLine(major=1, latest="1.0.0")],
        releases=[rel],
    )


def _seed(root, content: bytes, lifecycle=Lifecycle.PUBLISHED, served=True, artifact_name="demo.xsd", extra_sha256=True):
    # site_dir(), never root/"site": the code resolves it through that function, and a test
    # that assumes otherwise silently stops testing the code when CAIRN_SITE_DIR is set.
    vdir = site_dir(root) / "demo" / "v1.0.0"
    vdir.mkdir(parents=True)
    (vdir / artifact_name).write_bytes(content)
    art_record = {
        "name": artifact_name,
        "role": "schema",
        "media_type": "application/xml",
        "bytes": len(content),
        "source": {},
        "fetched_at": "2026-01-01T00:00:00+00:00",
    }
    if extra_sha256:
        art_record["sha256"] = sha256_hex(content)
    prov = {
        "standard": "demo",
        "version": "1.0.0",
        "lifecycle": str(lifecycle),
        "served": served,
        "artifacts": [art_record],
    }
    (vdir / "provenance.json").write_text(json.dumps(prov), encoding="utf-8")


# --- existing tests ---

def test_frozen_change_is_rejected(tmp_path):
    _seed(tmp_path, b"OLD")
    with pytest.raises(SyncError, match="FROZEN VERSION CHANGED"):
        sync_standard(_std(), tmp_path, _FakeClient(b"NEW"), verify=True, log=lambda *a: None)


def test_frozen_match_verifies(tmp_path):
    _seed(tmp_path, b"SAME")
    stats = sync_standard(_std(), tmp_path, _FakeClient(b"SAME"), verify=True, log=lambda *a: None)
    assert stats.verified == 1


def test_draft_is_mutable(tmp_path):
    # A draft tracks its branch: changed upstream bytes overwrite, no error.
    _seed(tmp_path, b"OLD", lifecycle=Lifecycle.DRAFT)
    stats = sync_standard(_std(lifecycle=Lifecycle.DRAFT), tmp_path, _FakeClient(b"NEW"), log=lambda *a: None)
    assert stats.fetched == 1
    assert (site_dir(tmp_path) / "demo" / "v1.0.0" / "demo.xsd").read_bytes() == b"NEW"


# --- new robustness tests ---

def test_damaged_provenance_on_a_draft_is_rebuilt(tmp_path):
    """A mutable release has no promise to keep, so the record is rebuilt - and reported."""
    vdir = site_dir(tmp_path) / "demo" / "v1.0.0"
    vdir.mkdir(parents=True)
    (vdir / "provenance.json").write_text("{not valid json", encoding="utf-8")

    stats = sync_standard(_std(lifecycle=Lifecycle.DRAFT), tmp_path, _FakeClient(b"BYTES"), log=lambda *a: None)

    assert stats.fetched == 1
    assert stats.recovered == 1, "a rebuilt record must be counted, not absorbed"
    prov = json.loads((vdir / "provenance.json").read_text())
    assert prov["artifacts"][0]["sha256"] == sha256_hex(b"BYTES")


def test_damaged_provenance_on_a_published_release_is_refused(tmp_path):
    """Rebuilding it would adopt whatever upstream serves now and destroy the only evidence
    that the published bytes were ever anything else. This test previously asserted the
    opposite, which is how the substitution stayed invisible."""
    vdir = site_dir(tmp_path) / "demo" / "v1.0.0"
    vdir.mkdir(parents=True)
    (vdir / "demo.xsd").write_bytes(b"PUBLISHED")
    (vdir / "provenance.json").write_bytes(b"\xff\xfe rotted\n")

    with pytest.raises(SyncError, match="PROVENANCE UNREADABLE"):
        sync_standard(_std(), tmp_path, _FakeClient(b"RETAGGED"), verify=True, log=lambda *a: None)

    assert (vdir / "demo.xsd").read_bytes() == b"PUBLISHED"


def test_structurally_invalid_provenance_is_refused_when_published(tmp_path):
    """Valid JSON of the wrong shape is damage too, and must not read as a first run."""
    vdir = site_dir(tmp_path) / "demo" / "v1.0.0"
    vdir.mkdir(parents=True)
    (vdir / "demo.xsd").write_bytes(b"PUBLISHED")
    (vdir / "provenance.json").write_text(json.dumps({"standard": "demo"}), encoding="utf-8")

    with pytest.raises(SyncError, match="PROVENANCE UNREADABLE"):
        sync_standard(_std(), tmp_path, _FakeClient(b"RETAGGED"), log=lambda *a: None)

    assert (vdir / "demo.xsd").read_bytes() == b"PUBLISHED"


def test_frozen_record_without_sha256_does_not_raise_false_positive(tmp_path):
    """An old provenance record that predates sha256 recording must not trigger FROZEN VERSION
    CHANGED. The missing checksum is computed and stored so later runs can compare for real.

    Upstream corroborates the served copy, so there is nothing to write: reinstating the
    identical bytes moves the inode and mtime, and therefore the ETag, of a URL nginx serves
    with immutable cache headers.
    """
    _seed(tmp_path, b"DATA", extra_sha256=False)
    served = site_dir(tmp_path) / "demo" / "v1.0.0" / "demo.xsd"
    before = served.stat().st_ino

    sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), verify=True, log=lambda *a: None)

    prov = json.loads((site_dir(tmp_path) / "demo" / "v1.0.0" / "provenance.json").read_text())
    assert prov["artifacts"][0]["sha256"] == sha256_hex(b"DATA")
    assert served.stat().st_ino == before, "identical bytes were reinstalled under a cached URL"


def test_orphaned_artifact_is_removed_from_a_draft(tmp_path):
    """A file in provenance that is no longer in the manifest should be deleted on next sync.

    Only while the release is still mutable - a draft's bytes are not published promises.
    """
    _seed(tmp_path, b"OLD", lifecycle=Lifecycle.DRAFT, artifact_name="old.xsd")
    orphan = site_dir(tmp_path) / "demo" / "v1.0.0" / "old.xsd"
    assert orphan.exists()

    new_artifact = Artifact(name="new.xsd", role="schema", from_="repo", path="new.xsd")
    sync_standard(
        _std(lifecycle=Lifecycle.DRAFT, artifacts=[new_artifact]), tmp_path, _FakeClient(b"NEW"), log=lambda *a: None
    )

    assert not orphan.exists(), "orphaned artifact should have been removed"
    assert (site_dir(tmp_path) / "demo" / "v1.0.0" / "new.xsd").exists()


def test_provenance_written_atomically(tmp_path):
    """provenance.json should not leave a partial file if interrupted (temp+rename pattern)."""
    # This is structural: verify the file appears complete and parseable after sync.
    stats = sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), log=lambda *a: None)
    assert stats.fetched == 1
    prov_path = site_dir(tmp_path) / "demo" / "v1.0.0" / "provenance.json"
    prov = json.loads(prov_path.read_text())
    assert prov["artifacts"][0]["name"] == "demo.xsd"


# --- write-once guards ---

def test_dropping_an_artifact_from_a_frozen_release_is_rejected(tmp_path):
    """Unpublishing a frozen artifact breaks the URL contract as surely as changing its bytes.

    `/demo/v1.0.0/old.xsd` has been handed out and may be cited in a schema import. Deleting
    the file answers 404 where it used to answer 200, so the manifest edit is refused and the
    published file is left alone.
    """
    _seed(tmp_path, b"PUBLISHED", lifecycle=Lifecycle.PUBLISHED, artifact_name="old.xsd")
    published = site_dir(tmp_path) / "demo" / "v1.0.0" / "old.xsd"

    kept = Artifact(name="new.xsd", role="schema", from_="repo", path="new.xsd")
    with pytest.raises(SyncError, match="FROZEN VERSION LOST AN ARTIFACT"):
        sync_standard(
            _std(lifecycle=Lifecycle.PUBLISHED, artifacts=[kept]), tmp_path, _FakeClient(b"NEW"), log=lambda *a: None
        )

    assert published.exists(), "a frozen release's published artifact must survive"


def test_refused_manifest_edit_writes_nothing(tmp_path):
    """The refusal must happen before anything is written, not after.

    Checking after the fetch loop left the replacement artifact published under a frozen
    version: absent from provenance, absent from SHA256SUMS, served with a one-year immutable
    cache, and permanently beyond the orphan reaper, which only considers names the previous
    provenance recorded. Every later cycle repeated the same partial write.
    """
    _seed(tmp_path, b"PUBLISHED", lifecycle=Lifecycle.PUBLISHED, artifact_name="old.xsd")
    vdir = site_dir(tmp_path) / "demo" / "v1.0.0"

    replacement = Artifact(name="new.xsd", role="schema", from_="repo", path="new.xsd")
    with pytest.raises(SyncError, match="FROZEN VERSION LOST AN ARTIFACT"):
        sync_standard(
            _std(lifecycle=Lifecycle.PUBLISHED, artifacts=[replacement]), tmp_path, _FakeClient(b"NEW"), log=lambda *a: None
        )

    assert not (vdir / "new.xsd").exists(), "an unrecorded file was published under a frozen version"
    assert sorted(p.name for p in vdir.iterdir()) == ["old.xsd", "provenance.json"]
    prov = json.loads((vdir / "provenance.json").read_text())
    assert [a["name"] for a in prov["artifacts"]] == ["old.xsd"]


def test_frozen_byte_change_writes_nothing(tmp_path):
    """Same guarantee for the other refusal: a changed frozen artifact is not written."""
    _seed(tmp_path, b"OLD", lifecycle=Lifecycle.PUBLISHED)
    artifact = site_dir(tmp_path) / "demo" / "v1.0.0" / "demo.xsd"

    with pytest.raises(SyncError, match="FROZEN VERSION CHANGED"):
        sync_standard(_std(), tmp_path, _FakeClient(b"TAMPERED"), verify=True, log=lambda *a: None)

    assert artifact.read_bytes() == b"OLD", "frozen bytes were overwritten before the check"


def test_an_unserved_release_keeps_the_artifacts_it_published(tmp_path):
    """Un-serving a release does not un-publish it, so the bytes behind its URLs stay put.

    The old model had no way to say that: `withdrawn` meant both "stop answering" and "this
    was never a promise", so reaping here was allowed and a release could be emptied out by
    one word. Restoring it to service would then have served 404s for artifacts the manifest
    had already published.
    """
    _seed(tmp_path, b"GONE", lifecycle=Lifecycle.PUBLISHED, served=False, artifact_name="old.xsd")
    published = site_dir(tmp_path) / "demo" / "v1.0.0" / "old.xsd"

    kept = Artifact(name="new.xsd", role="schema", from_="repo", path="new.xsd")
    sync_standard(
        _std(lifecycle=Lifecycle.PUBLISHED, served=False, artifacts=[kept]), tmp_path,
        _FakeClient(b"NEW"), log=lambda *a: None
    )

    assert published.exists(), "an un-served release was emptied of what it had published"
    assert published.read_bytes() == b"GONE"


@pytest.mark.parametrize("writer", ["sync", "render", "routes"])
def test_the_directories_it_creates_are_traversable_under_the_deployed_umask(tmp_path, monkeypatch, writer):
    """nginx runs unprivileged and cannot enter a directory without +x, so every directory this
    service creates has to be traversable. That is now the umask's job, set once in
    deploy/sync-loop.sh, rather than a walk that chmods each path.

    The guarantee this pins is the other half of that bargain: cairn must not create these with
    an explicit restrictive mode, or narrow them afterwards, or the umask cannot deliver it.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CAIRN_SITE_DIR", str(workspace / "doc-root"))
    monkeypatch.setenv("CAIRN_ROUTES_FILE", str(workspace / "conf" / "cairn-routes.conf"))

    old_umask = os.umask(0o022)
    try:
        if writer == "sync":
            sync_standard(_std(lifecycle=Lifecycle.DRAFT), workspace, _FakeClient(b"DATA"), log=lambda *a: None)
            created = [site_dir(workspace), site_dir(workspace) / "demo" / "v1.0.0"]
        elif writer == "render":
            render_site([_std(lifecycle=Lifecycle.DRAFT)], workspace, log=lambda *a: None)
            created = [site_dir(workspace), site_dir(workspace) / "demo", site_dir(workspace) / "assets"]
        else:
            created = [write_routes([_std(lifecycle=Lifecycle.DRAFT)], workspace).parent]
    finally:
        os.umask(old_umask)

    for directory in created:
        mode = directory.stat().st_mode & 0o777
        assert mode & stat.S_IROTH and mode & stat.S_IXOTH, (
            f"{directory} is {oct(mode)}; the web server cannot traverse it, so every URL "
            f"beneath it answers 403"
        )


def test_nothing_changes_the_mode_of_a_directory_it_did_not_create(tmp_path, monkeypatch):
    """A directory cairn did not create belongs to whoever mounted it. The version of this that
    set modes explicitly walked a path and widened what it found, which chmod'd a developer's
    home directory from 0750 to 0755 because a path's parents do not stop anywhere."""
    private = tmp_path / "restricted"
    private.mkdir(mode=0o700)
    workspace = private / "workspace"
    workspace.mkdir(mode=0o700)
    monkeypatch.setenv("CAIRN_SITE_DIR", str(workspace / "doc-root"))
    monkeypatch.setenv("CAIRN_ROUTES_FILE", str(workspace / "conf" / "cairn-routes.conf"))

    sync_standard(_std(lifecycle=Lifecycle.DRAFT), workspace, _FakeClient(b"DATA"), log=lambda *a: None)
    render_site([_std(lifecycle=Lifecycle.DRAFT)], workspace, log=lambda *a: None)
    write_routes([_std(lifecycle=Lifecycle.DRAFT)], workspace)

    assert private.stat().st_mode & 0o777 == 0o700
    assert workspace.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("name", ["demo.xsd", "provenance.json", "SHA256SUMS"])
def test_synced_files_are_readable_by_the_web_server(tmp_path, name):
    """nginx workers run unprivileged and answer 403 for anything they cannot open.

    mkstemp creates 0600 and os.replace preserves it, so without an explicit chmod every
    file sync writes becomes a 403 the moment the syncer image is rebuilt.
    """
    sync_standard(_std(lifecycle=Lifecycle.DRAFT), tmp_path, _FakeClient(b"BYTES"), log=lambda *a: None)
    mode = (site_dir(tmp_path) / "demo" / "v1.0.0" / name).stat().st_mode
    assert mode & stat.S_IROTH, f"{name} is {oct(mode & 0o777)} - the web server cannot read it"


@pytest.mark.parametrize("verify", [False, True], ids=["sync", "verify"])
def test_unreadable_frozen_artifact_is_repaired(tmp_path, verify):
    """A frozen artifact is never rewritten, so a bad mode would otherwise be permanent.

    Anything left 0600 by an earlier bug is skipped by a plain sync and `continue`d past by
    --verify, so neither path would ever restore it and the URL would 403 forever.
    """
    _seed(tmp_path, b"DATA", lifecycle=Lifecycle.PUBLISHED)
    artifact = site_dir(tmp_path) / "demo" / "v1.0.0" / "demo.xsd"
    artifact.chmod(0o600)

    stats = sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), verify=verify, log=lambda *a: None)

    assert artifact.stat().st_mode & stat.S_IROTH, "the web server still cannot read it"
    assert stats.repaired == 1


def test_unchanged_frozen_release_does_not_rewrite_metadata(tmp_path):
    """provenance.json and SHA256SUMS sit beside write-once artifacts and are documented as
    permanent. Rewriting them every cycle only to move `updated_at` churns the mtime of files
    nothing has changed and needlessly invalidates their cache validators."""
    _seed(tmp_path, b"DATA", lifecycle=Lifecycle.PUBLISHED)
    sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), log=lambda *a: None)
    prov_path = site_dir(tmp_path) / "demo" / "v1.0.0" / "provenance.json"
    before = prov_path.stat().st_mtime_ns

    sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), log=lambda *a: None)

    assert prov_path.stat().st_mtime_ns == before, "provenance.json was rewritten with no change"


def test_one_standard_failing_does_not_stop_the_others(tmp_path):
    """A failed integrity check on one standard must not stop the rest of the registry.

    sync_all previously let the SyncError propagate, so a single re-tagged artifact stopped
    every other standard replicating and stopped the render entirely, freezing the whole site
    at its last good state until someone intervened.
    """
    _seed(tmp_path, b"OLD", lifecycle=Lifecycle.PUBLISHED)  # demo v1.0.0, will fail on changed bytes

    healthy = Standard(
        id="other",
        title="Other",
        summary="s",
        steward=Steward(org="x"),
        source=Source(type="github", repo="o/r", ref="main"),
        major_lines=[MajorLine(major=1, latest="1.0.0")],
        releases=[
            Release(
                version="1.0.0",
                lifecycle=Lifecycle.DRAFT,
                ref="main",
                artifacts=[Artifact(name="other.xsd", role="schema", from_="repo", path="other.xsd")],
            )
        ],
    )

    with mock.patch("cairn.sync.http_client", return_value=_FakeClient(b"NEW")):
        stats = sync_all([_std(), healthy], tmp_path, verify=True, log=lambda *a: None)

    assert [std_id for std_id, _ in stats.failures] == ["demo"]
    assert not stats.ok
    assert (site_dir(tmp_path) / "other" / "v1.0.0" / "other.xsd").exists(), \
        "the healthy standard was not replicated"


# --- review follow-ups: integrity, isolation, and churn ---

def test_verify_detects_and_restores_local_corruption(tmp_path):
    """Upstream matching the record is only half the check; the served copy must be read too.

    Bit rot, a truncated file from an interrupted write, or a bad restore all leave upstream
    and provenance agreeing while the bytes actually served are wrong. Reporting that as
    verified is backwards for a preservation registry.
    """
    _seed(tmp_path, b"GENUINE", lifecycle=Lifecycle.PUBLISHED)
    served = site_dir(tmp_path) / "demo" / "v1.0.0" / "demo.xsd"
    served.write_bytes(b"CORRUPTED ON DISK")

    stats = sync_standard(_std(), tmp_path, _FakeClient(b"GENUINE"), verify=True, log=lambda *a: None)

    assert served.read_bytes() == b"GENUINE", "the corrupted served copy was not restored"
    assert stats.restored == 1
    assert stats.verified == 0, "corruption must not be reported as a clean verify"


def test_verify_reports_an_intact_copy_as_verified(tmp_path):
    _seed(tmp_path, b"GENUINE", lifecycle=Lifecycle.PUBLISHED)
    stats = sync_standard(_std(), tmp_path, _FakeClient(b"GENUINE"), verify=True, log=lambda *a: None)
    assert (stats.verified, stats.restored) == (1, 0)


def test_unchanged_draft_bytes_are_not_rewritten(tmp_path):
    """os.replace installs a new inode, moving mtime and cache validators on content that did
    not change - four times a day, for every draft, for nothing."""
    root = tmp_path
    sync_standard(_std(lifecycle=Lifecycle.DRAFT), root, _FakeClient(b"SAME"), log=lambda *a: None)
    artifact = site_dir(root) / "demo" / "v1.0.0" / "demo.xsd"
    before = artifact.stat().st_mtime_ns

    stats = sync_standard(_std(lifecycle=Lifecycle.DRAFT), root, _FakeClient(b"SAME"), log=lambda *a: None)

    assert artifact.stat().st_mtime_ns == before, "identical bytes were rewritten"
    assert stats.fetched == 0 and stats.skipped == 1


def test_changed_draft_bytes_are_still_written(tmp_path):
    """The churn fix must not stop a draft tracking its branch."""
    sync_standard(_std(lifecycle=Lifecycle.DRAFT), tmp_path, _FakeClient(b"OLD"), log=lambda *a: None)
    stats = sync_standard(_std(lifecycle=Lifecycle.DRAFT), tmp_path, _FakeClient(b"NEW"), log=lambda *a: None)
    assert stats.fetched == 1
    assert (site_dir(tmp_path) / "demo" / "v1.0.0" / "demo.xsd").read_bytes() == b"NEW"


@pytest.mark.parametrize(
    "exc",
    [httpx.ReadTimeout(""), httpx.ConnectError(""), OSError(28, "No space left on device")],
    ids=["empty-timeout", "empty-connect-error", "oserror"],
)
def test_one_standard_failing_never_escapes_isolation(tmp_path, exc):
    """The handler must survive the faults it exists for.

    httpx gives an empty message when wrapping a bare socket timeout, so taking the first
    line of str(exc) raised IndexError from inside the handler itself, skipping every
    remaining standard and the render: the whole-site freeze the isolation prevents.
    """
    class Failing(_FakeClient):
        def get(self, url, headers=None):
            raise exc

    with mock.patch("cairn.sync.http_client", return_value=Failing(b"")):
        stats = sync_all([_std(lifecycle=Lifecycle.DRAFT)], tmp_path, log=lambda *a: None)

    assert [s for s, _ in stats.failures] == ["demo"]
    assert stats.failures[0][1], "the recorded failure message must not be empty"


def test_stranded_temp_files_are_reaped(tmp_path):
    """A process killed between creating a temp file and renaming it leaves it in the
    document root, and nothing else would ever remove it."""
    _seed(tmp_path, b"DATA", lifecycle=Lifecycle.DRAFT)
    vdir = site_dir(tmp_path) / "demo" / "v1.0.0"
    stray = vdir / f"{TEMP_PREFIX}leftover"
    stray.write_bytes(b"partial")

    sync_standard(_std(lifecycle=Lifecycle.DRAFT), tmp_path, _FakeClient(b"DATA"), log=lambda *a: None)

    assert not stray.exists(), "a stranded temp file survived a sync"


def test_reordering_artifacts_does_not_rewrite_the_metadata(tmp_path):
    """These two files sit beside write-once artifacts and are documented as permanent. The
    early return compared record lists by order, so swapping two artifact entries in a manifest
    - no other change, nothing on disk different - installed new inodes and moved the mtime and
    cache validators of exactly the files this guard exists to leave alone."""
    first = Artifact(name="a.xsd", role="schema", from_="repo", path="a.xsd")
    second = Artifact(name="b.xsd", role="schema", from_="repo", path="b.xsd")
    sync_standard(_std(artifacts=[first, second]), tmp_path, _FakeClient(b"DATA"),
                  log=lambda *a: None)
    vdir = site_dir(tmp_path) / "demo" / "v1.0.0"
    before = {p.name: p.stat().st_mtime_ns for p in (vdir / "provenance.json", vdir / "SHA256SUMS")}

    sync_standard(_std(artifacts=[second, first]), tmp_path, _FakeClient(b"DATA"),
                  log=lambda *a: None)

    after = {p.name: p.stat().st_mtime_ns for p in (vdir / "provenance.json", vdir / "SHA256SUMS")}
    assert after == before, "reordering a manifest rewrote metadata documented as permanent"


def test_a_real_metadata_change_is_still_written(tmp_path):
    """The order-insensitive comparison must not have made the rewrite unreachable."""
    _seed(tmp_path, b"DATA", lifecycle=Lifecycle.DRAFT)
    sync_standard(_std(lifecycle=Lifecycle.DRAFT), tmp_path, _FakeClient(b"DATA"), log=lambda *a: None)
    vdir = site_dir(tmp_path) / "demo" / "v1.0.0"

    sync_standard(_std(lifecycle=Lifecycle.DRAFT), tmp_path, _FakeClient(b"CHANGED"), log=lambda *a: None)

    prov = json.loads((vdir / "provenance.json").read_text())
    assert prov["artifacts"][0]["sha256"] == sha256_hex(b"CHANGED")
    assert (vdir / "SHA256SUMS").read_bytes() == f"{sha256_hex(b'CHANGED')}  demo.xsd\n".encode()


def test_sha256sums_is_rebuilt_when_it_disagrees_with_provenance(tmp_path):
    """The pair is written by two calls, so a kill between them leaves one stale. Checking
    only that SHA256SUMS exists would make that divergence permanent."""
    _seed(tmp_path, b"DATA", lifecycle=Lifecycle.PUBLISHED)
    sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), log=lambda *a: None)
    sums = site_dir(tmp_path) / "demo" / "v1.0.0" / "SHA256SUMS"
    sums.write_text("0000000000  demo.xsd\n", encoding="utf-8")

    sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), log=lambda *a: None)

    assert sums.read_text().startswith(sha256_hex(b"DATA")), "stale SHA256SUMS was left in place"


def test_a_corrupted_draft_is_repaired(tmp_path):
    """Not rewriting unchanged bytes must not mean never looking at them.

    Every release published here is currently a draft, and a draft is the one case where
    nothing else ever reads the file: the frozen path has --verify, and the write path has
    the fetch. Rewriting every cycle used to repair this by accident.
    """
    _seed(tmp_path, b"GENUINE", lifecycle=Lifecycle.DRAFT)
    served = site_dir(tmp_path) / "demo" / "v1.0.0" / "demo.xsd"
    served.write_bytes(b"CORRUPT")

    stats = sync_standard(_std(lifecycle=Lifecycle.DRAFT), tmp_path, _FakeClient(b"GENUINE"), log=lambda *a: None)

    assert stats.restored == 1
    assert stats.skipped == 0
    assert served.read_bytes() == b"GENUINE"


def test_an_unchanged_draft_is_not_rewritten(tmp_path):
    """The counterpart: reading the file must not bring the four-times-a-day churn back."""
    _seed(tmp_path, b"GENUINE", lifecycle=Lifecycle.DRAFT)
    served = site_dir(tmp_path) / "demo" / "v1.0.0" / "demo.xsd"
    before = served.stat().st_mtime_ns

    stats = sync_standard(_std(lifecycle=Lifecycle.DRAFT), tmp_path, _FakeClient(b"GENUINE"), log=lambda *a: None)

    assert stats.skipped == 1
    assert stats.restored == 0
    assert served.stat().st_mtime_ns == before


def test_an_unreadable_served_copy_is_rewritten(tmp_path):
    """A bad sector used to propagate out of the plan phase and fail the whole standard,
    skipping the branch that was holding the bytes to repair it."""
    _seed(tmp_path, b"GENUINE", lifecycle=Lifecycle.PUBLISHED)
    served = site_dir(tmp_path) / "demo" / "v1.0.0" / "demo.xsd"

    with mock.patch.object(Path, "read_bytes", side_effect=OSError(5, "Input/output error")):
        stats = sync_standard(_std(), tmp_path, _FakeClient(b"GENUINE"), verify=True, log=lambda *a: None)

    assert stats.restored == 1
    assert served.read_bytes() == b"GENUINE"


def test_repointing_a_draft_updates_its_provenance(tmp_path):
    """Identical bytes from a new place are still from a new place, and provenance is the
    product. The skip path reused the old record and the metadata write saw no change."""
    _seed(tmp_path, b"SAME", lifecycle=Lifecycle.DRAFT)
    client = _FakeClient(b"SAME")
    sync_standard(_std(lifecycle=Lifecycle.DRAFT), tmp_path, client, log=lambda *a: None)

    moved = _std(lifecycle=Lifecycle.DRAFT)
    moved.source = Source(type="github", repo="other/repo", ref="v2-branch")
    moved.releases[0].ref = "v2-branch"
    stats = sync_standard(moved, tmp_path, client, log=lambda *a: None)

    prov = json.loads((site_dir(tmp_path) / "demo" / "v1.0.0" / "provenance.json").read_text())
    source = prov["artifacts"][0]["source"]
    assert source["repo"] == "other/repo"
    assert source["ref"] == "v2-branch"
    assert stats.fetched == 0, "the bytes were identical; only the record should have moved"


def test_damaged_metadata_does_not_end_the_run(tmp_path):
    """UnicodeDecodeError is a ValueError, so it escaped both the guard and the per-standard
    isolation. Detecting damage on the volume is what these two files are for."""
    _seed(tmp_path, b"DATA", lifecycle=Lifecycle.DRAFT)
    vdir = site_dir(tmp_path) / "demo" / "v1.0.0"
    (vdir / "SHA256SUMS").write_bytes(b"\xff\xfe not utf-8\n")
    (vdir / "provenance.json").write_bytes(b"\xff\xfe not utf-8\n")

    with mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"DATA")):
        stats = sync_all([_std(lifecycle=Lifecycle.DRAFT)], tmp_path, log=lambda *a: None)

    assert stats.failures == []
    assert (vdir / "SHA256SUMS").read_bytes() == f"{sha256_hex(b'DATA')}  demo.xsd\n".encode()
    assert json.loads((vdir / "provenance.json").read_text())["artifacts"][0]["name"] == "demo.xsd"


def test_temp_files_are_reaped_below_the_release_directories(tmp_path):
    """The sync only reaps release directories it planned, which leaves out everything the
    render writes and any standard that failed before its commit phase."""
    site = site_dir(tmp_path)
    spots = [site, site / "assets", site / "demo", site / "demo" / "_ns", site / "demo" / "v9.9.9"]
    strays = []
    for directory in spots:
        directory.mkdir(parents=True, exist_ok=True)
        stray = directory / f"{TEMP_PREFIX}leftover"
        stray.write_bytes(b"partial")
        strays.append(stray)

    assert reap_temp_tree(site) == len(strays)
    assert not any(stray.exists() for stray in strays)


def test_a_failed_permission_repair_is_distinguishable(tmp_path):
    """Returning the same value for "fixed nothing" and "could not fix" made a permanent 403
    invisible: nothing counted it and nothing logged it."""
    target = tmp_path / "artifact.xsd"
    target.write_bytes(b"DATA")

    target.chmod(0o644)
    assert ensure_published_mode(target) is ModeRepair.UNCHANGED

    target.chmod(0o600)
    assert ensure_published_mode(target) is ModeRepair.REPAIRED
    assert target.stat().st_mode & 0o777 == 0o644

    target.chmod(0o600)
    with mock.patch.object(Path, "chmod", side_effect=PermissionError(1, "Operation not permitted")):
        assert ensure_published_mode(target) is ModeRepair.FAILED

    assert ensure_published_mode(tmp_path / "gone.xsd") is ModeRepair.UNCHANGED


def test_a_legacy_record_gains_a_checksum_when_upstream_agrees(tmp_path):
    """A record predating checksum recording used to raise KeyError when SHA256SUMS was built,
    and KeyError is not what sync_all isolates on, so one legacy release ended the whole run.

    Upstream still serving the published bytes is what makes adopting them safe."""
    _seed(tmp_path, b"PUBLISHED", lifecycle=Lifecycle.PUBLISHED, extra_sha256=False)
    vdir = site_dir(tmp_path) / "demo" / "v1.0.0"
    (vdir / "SHA256SUMS").write_bytes(b"legacy\n")

    with mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"PUBLISHED")):
        stats = sync_all([_std()], tmp_path, log=lambda *a: None)

    assert stats.failures == []
    assert (vdir / "demo.xsd").read_bytes() == b"PUBLISHED"
    prov = json.loads((vdir / "provenance.json").read_text())
    assert prov["artifacts"][0]["sha256"] == sha256_hex(b"PUBLISHED")
    assert (vdir / "SHA256SUMS").read_bytes() == f"{sha256_hex(b'PUBLISHED')}  demo.xsd\n".encode()


def test_a_legacy_record_is_refused_when_upstream_disagrees(tmp_path):
    """With no recorded checksum there is nothing that can say whether the served copy rotted
    or upstream was re-tagged, and the two need opposite responses. Adopting either silently
    picks one and destroys the evidence for the other."""
    _seed(tmp_path, b"PUBLISHED", lifecycle=Lifecycle.PUBLISHED, extra_sha256=False)
    vdir = site_dir(tmp_path) / "demo" / "v1.0.0"

    with mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"UPSTREAM-MOVED-ON")):
        stats = sync_all([_std()], tmp_path, log=lambda *a: None)

    assert [std_id for std_id, _ in stats.failures] == ["demo"]
    assert "UNVERIFIABLE PUBLISHED FILE" in stats.failures[0][1]
    assert (vdir / "demo.xsd").read_bytes() == b"PUBLISHED"


def test_an_unexpected_fault_is_isolated_to_one_standard(tmp_path):
    """The isolation exists so one bad standard cannot take the registry down, which has to
    include faults nobody enumerated - the real case was a KeyError from a malformed record."""
    good = _std(lifecycle=Lifecycle.DRAFT)
    bad = _std(lifecycle=Lifecycle.DRAFT)
    bad.id = "boom"

    real_resolve = S_resolve

    def explode(std, rel, art, client):
        if std.id == "boom":
            raise RuntimeError("something nobody thought of")
        return real_resolve(std, rel, art, client)

    with mock.patch("cairn.sync.resolve", explode), \
         mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"DATA")):
        stats = sync_all([bad, good], tmp_path, log=lambda *a: None)

    assert [std_id for std_id, _ in stats.failures] == ["boom"]
    assert "RuntimeError" in stats.failures[0][1]
    assert stats.fetched == 1, "the healthy standard must still have synced"


def test_repointing_a_frozen_release_is_refused(tmp_path):
    """Identical bytes from a new origin still change what was published, because the recorded
    origin is part of it. The record used to follow the manifest silently and report verified."""
    client = _FakeClient(b"SAME")
    sync_standard(_std(), tmp_path, client, log=lambda *a: None)
    prov_path = site_dir(tmp_path) / "demo" / "v1.0.0" / "provenance.json"
    before = prov_path.read_text()

    moved = _std()
    moved.source = Source(type="github", repo="other/repo", ref="v2-tag")
    moved.releases[0].ref = "v2-tag"

    with pytest.raises(SyncError, match="FROZEN VERSION REPOINTED"):
        sync_standard(moved, tmp_path, client, verify=True, log=lambda *a: None)

    assert prov_path.read_text() == before, "a published record must not follow the manifest"


def test_a_frozen_record_without_a_recorded_source_is_left_alone(tmp_path):
    """Legacy records have no coordinates to compare, which is not evidence of a repoint."""
    _seed(tmp_path, b"DATA", lifecycle=Lifecycle.PUBLISHED)  # _seed records source: {}

    stats = sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), verify=True, log=lambda *a: None)

    assert stats.verified == 1
    prov = json.loads((site_dir(tmp_path) / "demo" / "v1.0.0" / "provenance.json").read_text())
    assert prov["artifacts"][0]["source"] == {}


def test_a_metadata_file_stuck_unreadable_is_counted(tmp_path):
    """A [WARN] nothing tallies produces no marker and no exit code, which is the silence the
    three-way repair outcome was introduced to end."""
    sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), log=lambda *a: None)
    prov = site_dir(tmp_path) / "demo" / "v1.0.0" / "provenance.json"
    prov.chmod(0o600)

    real_chmod = Path.chmod

    def refuse(self, mode, **kwargs):
        if self.name == "provenance.json":
            raise PermissionError(1, "Operation not permitted")
        return real_chmod(self, mode, **kwargs)

    with mock.patch.object(Path, "chmod", refuse):
        stats = sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), log=lambda *a: None)

    assert stats.unreadable == 1


def test_the_routes_file_is_written_atomically(tmp_path, monkeypatch):
    """The one generated file that was not. nginx reloads on any change to it and refuses a
    config it cannot parse, so a truncated include survives until the next restart and then
    stops the server from starting at all."""
    routes = tmp_path / "conf" / "cairn-routes.conf"
    monkeypatch.setenv("CAIRN_ROUTES_FILE", str(routes))

    written = write_routes([_std()], tmp_path)

    assert written == routes
    assert "location" in routes.read_text()
    assert list(routes.parent.glob(f"{TEMP_PREFIX}*")) == []


def test_a_write_that_fails_leaves_no_temp_file(tmp_path):
    """The fsync additions must not have changed which failures clean up after themselves."""
    target = tmp_path / "artifact.xsd"

    with mock.patch("os.fsync", side_effect=OSError(28, "No space left on device")):
        with pytest.raises(OSError):
            atomic_write(target, b"DATA")

    assert not target.exists()
    assert list(tmp_path.glob(f"{TEMP_PREFIX}*")) == []


def test_a_repoint_is_caught_when_the_artifact_is_missing(tmp_path):
    """The guard used to sit inside the branch that requires the file to exist, so a published
    release whose artifact had vanished adopted the new upstream and rewrote its provenance."""
    client = _FakeClient(b"PUBLISHED")
    sync_standard(_std(), tmp_path, client, log=lambda *a: None)
    vdir = site_dir(tmp_path) / "demo" / "v1.0.0"
    (vdir / "demo.xsd").unlink()

    moved = _std()
    moved.source = Source(type="github", repo="other/repo", ref="v2-tag")
    moved.releases[0].ref = "v2-tag"

    with pytest.raises(SyncError, match="FROZEN VERSION REPOINTED"):
        sync_standard(moved, tmp_path, client, verify=True, log=lambda *a: None)

    source = json.loads((vdir / "provenance.json").read_text())["artifacts"][0]["source"]
    assert source["repo"] == "o/r"


def test_repointing_a_withdrawn_release_is_allowed(tmp_path):
    """A withdrawn release publishes nothing - the URL answers 410 - so repointing it breaks
    no promise. Every sibling guard checks is_served; this one did not, and failed the whole
    standard every verify pass."""
    client = _FakeClient(b"BYTES")
    sync_standard(_std(lifecycle=Lifecycle.PUBLISHED, served=False), tmp_path, client, log=lambda *a: None)

    moved = _std(lifecycle=Lifecycle.PUBLISHED, served=False)
    moved.source = Source(type="github", repo="other/repo", ref="v2-tag")
    moved.releases[0].ref = "v2-tag"

    stats = sync_standard(moved, tmp_path, client, verify=True, log=lambda *a: None)
    assert stats is not None  # no raise is the assertion


def test_a_vanished_published_artifact_is_restored_and_reported(tmp_path):
    """A write-once URL answering 404 is at least as serious as byte drift, and was counted
    as an ordinary fetch: exit 0, no marker, "cycle complete" in the log."""
    sync_standard(_std(), tmp_path, _FakeClient(b"PUBLISHED"), log=lambda *a: None)
    served = site_dir(tmp_path) / "demo" / "v1.0.0" / "demo.xsd"
    served.unlink()

    stats = sync_standard(_std(), tmp_path, _FakeClient(b"PUBLISHED"), log=lambda *a: None)

    assert stats.restored == 1
    assert stats.fetched == 0
    assert served.read_bytes() == b"PUBLISHED"


def test_the_routes_directory_is_reaped(tmp_path, monkeypatch):
    """It sits under neither reaper: the sync sweeps release directories, the render sweeps
    the document root, and the routes file lives outside both."""
    routes = tmp_path / "conf" / "cairn-routes.conf"
    routes.parent.mkdir(parents=True)
    monkeypatch.setenv("CAIRN_ROUTES_FILE", str(routes))
    stray = routes.parent / f"{TEMP_PREFIX}leftover"
    stray.write_bytes(b"partial")

    write_routes([_std()], tmp_path)

    assert not stray.exists()


def test_committed_releases_keep_their_counters_when_a_later_one_fails(tmp_path):
    """Stats used to be returned rather than accumulated, so a raise in release two discarded
    everything release one had already done - including the restore counter that is the only
    source of the operator marker. The file was rewritten and the log said nothing."""
    from cairn.manifest import Release as _Release

    std = _std()
    std.releases.append(_Release(version="1.1.0", lifecycle=Lifecycle.PUBLISHED, ref="main",
                                 artifacts=[Artifact(name="demo.xsd", role="schema", from_="repo", path="demo.xsd")]))
    sync_standard(std, tmp_path, _FakeClient(b"ORIGINAL"), log=lambda *a: None)

    (site_dir(tmp_path) / "demo" / "v1.0.0" / "demo.xsd").write_bytes(b"CORRUPTED")
    later = site_dir(tmp_path) / "demo" / "v1.1.0" / "provenance.json"
    record = json.loads(later.read_text())
    record["artifacts"][0]["sha256"] = "0" * 64  # make the second release fail
    later.write_text(json.dumps(record))

    with mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"ORIGINAL")):
        stats = sync_all([std], tmp_path, verify=True, log=lambda *a: None)

    assert stats.failures, "the second release should still have failed"
    assert stats.restored == 1, "the first release's repair was discarded with the exception"
    assert (site_dir(tmp_path) / "demo" / "v1.0.0" / "demo.xsd").read_bytes() == b"ORIGINAL"


def _release(version: str, lifecycle: Lifecycle = Lifecycle.PUBLISHED, name: str = "demo.xsd") -> Release:
    return Release(
        version=version,
        lifecycle=lifecycle,
        ref="main",
        artifacts=[Artifact(name=name, role="schema", from_="repo", path=name)],
    )


# --- promotion: the one path the manifests document as the way to cut a version ------------

def _pinned(lifecycle: Lifecycle, ref: str) -> Standard:
    """The shape standards/eaf/standard.yaml instructs: a draft tracking a branch, then the
    same version with lifecycle published and `ref:` pinned to the tag."""
    std = _std(lifecycle=lifecycle)
    std.source = Source(type="github", repo="o/r", ref=ref)
    std.releases[0].ref = ref
    return std


BRANCH_HEAD = b"<schema>branch head, still under review</schema>"
TAGGED = b"<schema>the bytes that were actually released</schema>"


def test_promoting_a_draft_publishes_the_bytes_the_manifest_now_names(tmp_path):
    """The release procedure every manifest here documents, and the one path no test covered.

    `is_frozen` was computed from the new status while the record it consulted came from the
    draft era, so the first frozen cycle took the no-fetch fast path: the branch-head bytes
    stayed on disk as the new stable version, a checksum matching *them* was recorded, and
    SHA256SUMS was written to agree. The release was then internally consistent and
    self-certifying while serving something that was never the release, and every later check
    compared against the record that cycle had just written. Exit 0, no marker.
    """
    sync_standard(_pinned(Lifecycle.DRAFT, "release-branch"), tmp_path, _FakeClient(BRANCH_HEAD),
                  log=lambda *a: None)
    vdir = site_dir(tmp_path) / "demo" / "v1.0.0"

    stats = sync_standard(_pinned(Lifecycle.PUBLISHED, "v1.0.0"), tmp_path, _FakeClient(TAGGED),
                          log=lambda *a: None)

    assert stats.fetched == 1, "the publication cycle must read what the manifest now names"
    assert (vdir / "demo.xsd").read_bytes() == TAGGED, "the draft-era bytes were frozen as the release"
    record = json.loads((vdir / "provenance.json").read_text())["artifacts"][0]
    assert record["source"]["ref"] == "v1.0.0", "the published record still names the draft's branch"
    assert record["sha256"] == sha256_hex(TAGGED)
    assert (vdir / "SHA256SUMS").read_bytes() == f"{sha256_hex(TAGGED)}  demo.xsd\n".encode()


def test_a_promoted_release_verifies_cleanly_afterwards(tmp_path):
    """The lasting half. Freezing the draft's coordinates made every later --verify raise
    FROZEN VERSION REPOINTED, which never self-heals: the standard fails every cycle, the run
    exits 5, and the loop never stamps, so the whole frozen corpus is re-verified and
    re-downloaded forever."""
    sync_standard(_pinned(Lifecycle.DRAFT, "release-branch"), tmp_path, _FakeClient(TAGGED),
                  log=lambda *a: None)
    sync_standard(_pinned(Lifecycle.PUBLISHED, "v1.0.0"), tmp_path, _FakeClient(TAGGED), log=lambda *a: None)

    with mock.patch("cairn.sync.http_client", lambda: _FakeClient(TAGGED)):
        stats = sync_all([_pinned(Lifecycle.PUBLISHED, "v1.0.0")], tmp_path, verify=True, log=lambda *a: None)

    assert stats.failures == []
    assert stats.verified == 1


def test_promotion_does_not_weaken_the_guards_on_later_cycles(tmp_path):
    """The publication cycle turns the write-once guards off, so they have to come back on for
    the cycle after it. Otherwise the fix would be a permanent hole rather than a one-cycle
    window."""
    sync_standard(_pinned(Lifecycle.DRAFT, "release-branch"), tmp_path, _FakeClient(BRANCH_HEAD),
                  log=lambda *a: None)
    sync_standard(_pinned(Lifecycle.PUBLISHED, "v1.0.0"), tmp_path, _FakeClient(TAGGED), log=lambda *a: None)

    with pytest.raises(SyncError, match="FROZEN VERSION CHANGED"):
        sync_standard(_pinned(Lifecycle.PUBLISHED, "v1.0.0"), tmp_path, _FakeClient(b"RETAGGED LATER"),
                      verify=True, log=lambda *a: None)
    with pytest.raises(SyncError, match="FROZEN VERSION REPOINTED"):
        sync_standard(_pinned(Lifecycle.PUBLISHED, "v2.0.0-tag"), tmp_path, _FakeClient(TAGGED),
                      verify=True, log=lambda *a: None)


def test_unfreezing_a_published_version_is_refused(tmp_path):
    """The reverse transition. `cairn validate --baseline` refuses it on a pull request, but the
    syncer is the last line: without this, a status reverted by any route that skipped the gate
    has the next sync overwrite published bytes in place and report a clean cycle."""
    sync_standard(_std(lifecycle=Lifecycle.PUBLISHED), tmp_path, _FakeClient(TAGGED), log=lambda *a: None)
    served = site_dir(tmp_path) / "demo" / "v1.0.0" / "demo.xsd"

    with pytest.raises(SyncError, match="PUBLISHED VERSION UNFROZEN"):
        sync_standard(_std(lifecycle=Lifecycle.DRAFT), tmp_path, _FakeClient(b"SOMETHING ELSE"),
                      log=lambda *a: None)

    assert served.read_bytes() == TAGGED, "published bytes were overwritten in place"


def test_withdrawing_a_release_is_not_treated_as_unfreezing(tmp_path):
    """Withdrawing is the deliberate way to unpublish and stays allowed; the refusal above must
    not catch it, and the baseline gate exempts it for the same reason."""
    sync_standard(_std(lifecycle=Lifecycle.PUBLISHED), tmp_path, _FakeClient(TAGGED), log=lambda *a: None)
    stats = sync_standard(_std(lifecycle=Lifecycle.PUBLISHED, served=False), tmp_path, _FakeClient(TAGGED),
                          log=lambda *a: None)
    assert stats is not None


def test_a_failing_release_does_not_abandon_the_ones_after_it(tmp_path):
    """Releases are independent units, and a raise used to skip every one after the failure
    while the run still counted as having finished - so the loop wrote the verify stamp for
    releases it had never read and suppressed the next attempt for a full interval. That is
    the pathology the exit-code split exists to prevent, one level below where it was fixed."""
    std = _std()
    std.releases = [_release("1.0.0"), _release("1.1.0"), _release("2.0.0")]
    sync_standard(std, tmp_path, _FakeClient(b"PUBLISHED"), log=lambda *a: None)

    rotted = site_dir(tmp_path) / "demo" / "v1.0.0" / "provenance.json"
    rotted.write_bytes(b"\xff\xfe rotted\n")

    with mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"PUBLISHED")):
        stats = sync_all([std], tmp_path, verify=True, log=lambda *a: None)

    assert "PROVENANCE UNREADABLE" in stats.failures[0][1]
    assert stats.verified == 2, "the releases after the failing one were never read"


def test_several_failing_releases_are_reported_as_one_failed_standard(tmp_path):
    """Per-release isolation must not turn one bad standard into several: `nothing_succeeded`
    compares the failure count against the number of standards attempted, so a standard that
    contributed two failures would make a total wipe-out look like a partial one."""
    std = _std()
    std.releases = [_release("1.0.0"), _release("1.1.0")]
    sync_standard(std, tmp_path, _FakeClient(b"PUBLISHED"), log=lambda *a: None)

    with mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"RETAGGED")):
        stats = sync_all([std], tmp_path, verify=True, log=lambda *a: None)

    assert len(stats.failures) == 1, "one standard failed, however many of its releases did"
    assert stats.nothing_succeeded, "every standard attempted failed; nothing was verified"
    message = stats.failures[0][1]
    assert message.count("FROZEN VERSION CHANGED") == 2, "a release's refusal was dropped"
    assert message.splitlines()[0] == "2 of 2 release(s) of demo failed:"


def test_a_release_contributes_one_failure_however_many_ways_it_went_wrong(tmp_path):
    """The dry run's refusal used to be summed per standard and appended after the loop, so a
    single release that both raised and left an artifact unreachable produced two entries -
    and the aggregate could then claim more failures than the standard had releases. Reporting
    it per release, like every other refusal here, makes the count structurally honest."""
    std = _std()
    std.releases[0].artifacts = [
        # Probed, and answers 404: one unreachable artifact.
        Artifact(name="probed.xsd", role="schema", from_="repo", path="a.xsd"),
        # Never gets as far as a probe: the release it names holds no matching asset, so
        # resolve() raises and the release itself fails. One release, two problems.
        Artifact(name="unresolvable.xsd", role="schema", from_="release-asset", asset="nothing.zip"),
    ]

    class Missing(_FakeClient):
        def head(self, url):
            response = super().head(url)
            response.status_code = 404
            return response

    stats = SyncStats()
    with pytest.raises(SyncError) as raised:
        sync_standard(std, tmp_path, Missing(b""), dry_run=True, log=lambda *a: None, stats=stats)

    # One release, one entry, and it opens with the marker rather than with a count.
    assert stats.releases_attempted == 1 and stats.releases_failed == 1
    assert str(raised.value).splitlines()[0].startswith("demo 1.0.0 unresolvable.xsd:")


def test_a_single_failing_release_keeps_its_marker_on_the_first_line(tmp_path):
    """sync_all logs only the first line, and the runbooks name markers. Wrapping a lone
    failure in a summary would demote the documented string to line three."""
    std = _std()
    std.releases = [_release("1.0.0"), _release("1.1.0")]
    sync_standard(std, tmp_path, _FakeClient(b"PUBLISHED"), log=lambda *a: None)
    (site_dir(tmp_path) / "demo" / "v1.1.0" / "demo.xsd").write_bytes(b"CORRUPT")
    (site_dir(tmp_path) / "demo" / "v1.0.0" / "provenance.json").write_bytes(b"\xff\xfe rotted\n")

    with mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"PUBLISHED")):
        stats = sync_all([std], tmp_path, verify=True, log=lambda *a: None)

    assert stats.failures[0][1].startswith("PROVENANCE UNREADABLE:")
    assert stats.restored == 1, "the healthy release's repair was not counted"


def test_a_run_that_verified_most_of_a_standard_is_not_nothing_succeeded(tmp_path):
    """`nothing_succeeded` decides whether the loop records a verification as having happened.

    It counted standards, which was right while a standard was all-or-nothing. Once a failing
    release stopped abandoning its siblings, one rotted record in a three-release standard was
    a run that read and checksummed the other two, and still reported that nothing had been
    checked: the loop refused the stamp and re-verified and re-downloaded the whole frozen
    corpus every cycle, forever. That is the pathology per-release isolation removed, surviving
    in the accounting that consumes it.
    """
    std = _std()
    std.releases = [_release("1.0.0"), _release("1.1.0"), _release("2.0.0")]
    sync_standard(std, tmp_path, _FakeClient(b"PUBLISHED"), log=lambda *a: None)
    (site_dir(tmp_path) / "demo" / "v1.0.0" / "provenance.json").write_bytes(b"\xff\xfe rotted\n")

    with mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"PUBLISHED")):
        stats = sync_all([std], tmp_path, verify=True, log=lambda *a: None)

    assert stats.verified == 2, "two releases really were re-read"
    assert not stats.nothing_succeeded, (
        "a run that verified two of three releases reported that it verified nothing, which "
        "makes the loop re-verify the entire frozen corpus every cycle indefinitely"
    )


def test_a_run_where_every_release_failed_is_still_nothing_succeeded(tmp_path):
    """The counterpart. Loosening the definition must not make the stamp unrefusable: a pass
    that re-read nothing and stamped itself anyway is the original bug pointing the other way.
    """
    std = _std()
    std.releases = [_release("1.0.0"), _release("1.1.0")]
    sync_standard(std, tmp_path, _FakeClient(b"PUBLISHED"), log=lambda *a: None)

    with mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"RETAGGED")):
        stats = sync_all([std], tmp_path, verify=True, log=lambda *a: None)

    assert stats.releases_attempted == 2 and stats.releases_failed == 2
    assert stats.nothing_succeeded


def test_every_failure_reaches_the_log_opening_with_its_marker(tmp_path, capsys):
    """Operators are told to alert on the marker strings, so a `[FAIL]` line without one is a
    failure their tooling cannot see. The standard-level summary of several failures opens with
    a count, so it is the per-release lines that have to carry the markers - and the
    standard-level line must not repeat a lone failure verbatim and double every alert."""
    std = _std()
    std.releases = [_release("1.0.0"), _release("1.1.0")]
    sync_standard(std, tmp_path, _FakeClient(b"PUBLISHED"), log=lambda *a: None)

    with mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"RETAGGED")):
        sync_all([std], tmp_path, verify=True, log=print)

    fail_lines = [line for line in capsys.readouterr().out.splitlines() if "[FAIL]" in line]
    assert len(fail_lines) == 2, f"one line per failed release, and no summary repeat: {fail_lines}"
    for line in fail_lines:
        assert "FROZEN VERSION CHANGED" in line, f"a [FAIL] line carries no marker: {line}"


def test_a_lone_failure_is_not_logged_twice(tmp_path, capsys):
    """The duplicate carried nothing the first line lacked and double-counted every alert."""
    _seed(tmp_path, b"OLD", lifecycle=Lifecycle.PUBLISHED)

    with mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"RETAGGED")):
        sync_all([_std()], tmp_path, verify=True, log=print)

    fail_lines = [line for line in capsys.readouterr().out.splitlines() if "[FAIL]" in line]
    assert len(fail_lines) == 1, fail_lines


def test_a_write_failure_mid_commit_keeps_what_the_release_already_did(tmp_path):
    """The commit phase used to allocate its own counters and return them, so a raise anywhere
    in it discarded everything that release had already done - and those counters are the only
    source of the operator markers. A file was restored on the volume and the log said nothing:
    the same discard fixed one level up, left in place one level down."""
    _seed(tmp_path, b"GENUINE", lifecycle=Lifecycle.PUBLISHED)
    served = site_dir(tmp_path) / "demo" / "v1.0.0" / "demo.xsd"
    served.write_bytes(b"CORRUPTED ON DISK")

    def fail_on_metadata(path, data):
        # cairn.sync.atomic_write is what gets patched; this name still points at the real one.
        if path.name == "SHA256SUMS":
            raise OSError(28, "No space left on device")
        return atomic_write(path, data)

    with mock.patch("cairn.sync.atomic_write", fail_on_metadata), \
         mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"GENUINE")):
        stats = sync_all([_std()], tmp_path, verify=True, log=lambda *a: None)

    assert served.read_bytes() == b"GENUINE", "the restore itself did not happen"
    assert stats.restored == 1, "a published file was rewritten and nothing counted it"
    assert stats.failures, "the failed metadata write must still be reported"


def test_a_rebuild_that_failed_is_not_reported_as_a_rebuild(tmp_path):
    """`recovered` is the only source of DAMAGED RECORD(S) REBUILT and its INTEGRITY CHECK
    FAILED block. Counted on entry to the commit phase rather than after the write it
    describes, it told the operator a record had been rebuilt while that record was still
    rotted on disk - every sibling counter is incremented after its write succeeds."""
    _seed(tmp_path, b"DATA", lifecycle=Lifecycle.DRAFT)
    vdir = site_dir(tmp_path) / "demo" / "v1.0.0"
    (vdir / "provenance.json").write_bytes(b"\xff\xfe rotted\n")

    def fail_on_metadata(path, data):
        if path.name == "SHA256SUMS":
            raise OSError(28, "No space left on device")
        return atomic_write(path, data)

    with mock.patch("cairn.sync.atomic_write", fail_on_metadata), \
         mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"DATA")):
        stats = sync_all([_std(lifecycle=Lifecycle.DRAFT)], tmp_path, log=lambda *a: None)

    assert stats.failures, "the failed metadata write must be reported"
    assert (vdir / "provenance.json").read_bytes().startswith(b"\xff\xfe"), "premise: still rotted"
    assert stats.recovered == 0, "a rebuild that did not happen was reported as one"


def test_a_rebuilt_record_still_reaps_what_the_manifest_dropped(tmp_path):
    """Orphans were computed only from a prior record, so a release whose provenance had been
    damaged and rebuilt reaped nothing - and from the next cycle on the rebuilt record does not
    name those files either, putting them permanently beyond every reaper while they keep
    resolving."""
    _seed(tmp_path, b"OLD", lifecycle=Lifecycle.DRAFT, artifact_name="dropped.xsd")
    vdir = site_dir(tmp_path) / "demo" / "v1.0.0"
    (vdir / "provenance.json").write_bytes(b"\xff\xfe rotted\n")
    (vdir / "index.html").write_text("the release page", encoding="utf-8")
    stray_temp = vdir / f"{TEMP_PREFIX}half-written"
    stray_temp.write_bytes(b"partial")

    kept = Artifact(name="kept.xsd", role="schema", from_="repo", path="kept.xsd")
    stats = sync_standard(
        _std(lifecycle=Lifecycle.DRAFT, artifacts=[kept]), tmp_path, _FakeClient(b"NEW"), log=lambda *a: None
    )

    assert stats.recovered == 1
    assert not (vdir / "dropped.xsd").exists(), "a file the manifest no longer declares kept serving"
    assert (vdir / "kept.xsd").exists()
    assert (vdir / "index.html").is_file(), "the render's own output is not the sync's to reap"
    assert not stray_temp.exists(), "a stranded temp file survived"


def test_the_reaper_never_deletes_anything_the_render_wrote(tmp_path):
    """The directory-scan reaper holds back the names the render owns, which was a list copied
    into the sync. A copy of another module's decision goes stale silently, and this one deletes
    files on the strength of it: a second per-release render output would be removed on every
    cycle that rebuilt a damaged record, with the older test still green because it named
    index.html specifically.

    So this asks the render what it writes rather than restating it. A new per-release output
    is covered the day it is added.
    """

    _seed(tmp_path, b"DATA", lifecycle=Lifecycle.DRAFT)
    render_site([_std(lifecycle=Lifecycle.DRAFT)], tmp_path, log=lambda *a: None)
    vdir = site_dir(tmp_path) / "demo" / "v1.0.0"
    rendered = {p.name for p in vdir.iterdir() if p.is_file()} - {"demo.xsd", "provenance.json", "SHA256SUMS"}
    assert rendered, "the render wrote nothing into the release directory; this test proves nothing"

    (vdir / "provenance.json").write_bytes(b"\xff\xfe rotted\n")
    sync_standard(_std(lifecycle=Lifecycle.DRAFT), tmp_path, _FakeClient(b"DATA"), log=lambda *a: None)

    survived = {p.name for p in vdir.iterdir() if p.is_file()}
    assert rendered <= survived, f"the reaper deleted render output: {sorted(rendered - survived)}"


def test_publishing_a_version_is_reported(tmp_path):
    """On the cycle that publishes a release the write-once guards do not apply, and whether
    they apply is decided by a status recorded on the volume they protect. Nothing there can
    prove the promotion was intended, so it is reported rather than guessed at."""
    sync_standard(_pinned(Lifecycle.DRAFT, "release-branch"), tmp_path, _FakeClient(BRANCH_HEAD),
                  log=lambda *a: None)

    stats = sync_standard(_pinned(Lifecycle.PUBLISHED, "v1.0.0"), tmp_path, _FakeClient(TAGGED),
                          log=lambda *a: None)

    assert stats.published == 1
    # And not on the cycles either side of it.
    again = sync_standard(_pinned(Lifecycle.PUBLISHED, "v1.0.0"), tmp_path, _FakeClient(TAGGED),
                          log=lambda *a: None)
    assert again.published == 0


def test_un_serving_and_restoring_a_release_cannot_launder_its_bytes(tmp_path):
    """The laundering path, now unreachable by construction rather than by predicate.

    Under the previous model `withdrawn` was neither mutable nor served, so its guards were
    off: a verify pass adopted whatever upstream had drifted to, and restoring the release to
    `stable` counted as already-frozen, skipped the fetch, and certified the withdrawn era's
    bytes as the published version with a checksum and SHA256SUMS to agree - exit 0, no marker.
    Three successive fixes closed three routes to this and a fourth was still open, because
    every one of them tried to reconstruct a path through a six-value enum from one word that
    the sync itself rewrote every cycle.

    `lifecycle` never leaves `published`, so there is no path to close.
    """
    published, drift = b"PUBLISHED", b"UPSTREAM DRIFTED HERE"
    sync_standard(_std(), tmp_path, _FakeClient(published), log=lambda *a: None)

    sync_standard(_std(served=False), tmp_path, _FakeClient(drift), verify=True, log=lambda *a: None)
    vdir = site_dir(tmp_path) / "demo" / "v1.0.0"
    assert (vdir / "demo.xsd").read_bytes() == published, "the un-served era adopted upstream"

    stats = sync_standard(_std(), tmp_path, _FakeClient(published), log=lambda *a: None)

    assert (vdir / "demo.xsd").read_bytes() == published
    assert not stats.failures
    record = json.loads((vdir / "provenance.json").read_text())["artifacts"][0]
    assert record["sha256"] == sha256_hex(published)


def test_a_release_with_no_record_at_all_keeps_its_guards(tmp_path):
    """The counterpart to the rule above. With no provenance there is nothing saying the
    previous era published nothing, and bytes already in the release directory are evidence
    that it did - a record can be lost while the files it described stay served. Treating that
    as a first publication would disable exactly the guard written for it."""
    vdir = site_dir(tmp_path) / "demo" / "v1.0.0"
    vdir.mkdir(parents=True)
    (vdir / "demo.xsd").write_bytes(b"PUBLISHED")

    with mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"SOMETHING ELSE")):
        stats = sync_all([_std()], tmp_path, log=lambda *a: None)

    assert "UNVERIFIABLE PUBLISHED FILE" in stats.failures[0][1]
    assert (vdir / "demo.xsd").read_bytes() == b"PUBLISHED"


def test_a_dot_name_in_provenance_is_refused(tmp_path):
    """"." and ".." match a bare-filename character class but are not filenames. A record naming
    ".." was accepted, listed as an orphan and reached (vdir / "..").unlink(), which raises
    EISDIR - failing that release on every cycle with an OSError carrying no marker, invisible
    to the alerting the marker registry exists to serve."""
    for name in ("..", "."):
        assert not is_provenance_record_set({"artifacts": [{"name": name}]}), name
    assert is_provenance_record_set({"artifacts": [{"name": "demo.xsd"}]})


def test_a_damaged_sha256sums_with_a_duplicate_name_is_rebuilt(tmp_path):
    """The rule that a duplicate is damage lived on the record side and not on this one, so a
    SHA256SUMS listing one artifact twice - a wrong digest on the first line - compared equal to
    the correct set and stayed published, while `sha256sum -c` reported it FAILED."""
    _seed(tmp_path, b"DATA", lifecycle=Lifecycle.PUBLISHED)
    sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), log=lambda *a: None)
    sums = site_dir(tmp_path) / "demo" / "v1.0.0" / "SHA256SUMS"
    intact = sums.read_bytes()
    sums.write_bytes(b"0" * 64 + b"  demo.xsd\n" + intact)

    sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), verify=True, log=lambda *a: None)

    assert sums.read_bytes() == intact


def test_a_real_fetch_retries_what_the_gate_retries(tmp_path):
    """The retry began life in the dry run only, so the gate was strictly more tolerant than the
    sync it gates: an upstream answering 429 once and 200 on retry passed `cairn sync --dry-run`
    and then failed the standard on the deployment with a single un-retried GET."""
    attempts = []

    class Limited(_FakeClient):
        def get(self, url, headers=None):
            response = super().get(url)
            # Only the artifact fetch: resolve() also calls the API to pin a commit, which is
            # best-effort and would otherwise be counted here.
            if not url.endswith("demo.xsd"):
                return response
            attempts.append(url)
            if len(attempts) < 3:
                response.status_code = 429
            return response

    with mock.patch("cairn.sync._sleep"):
        stats = sync_standard(_std(lifecycle=Lifecycle.DRAFT), tmp_path, Limited(b"DATA"), log=lambda *a: None)

    assert len(attempts) == 3
    assert stats.fetched == 1


def test_a_rotted_lifecycle_cannot_publish_a_release_in_silence(tmp_path):
    """One word in provenance.json still decides whether the guards run on the publication
    cycle, and that file lives on the volume they protect. A record rotted to 'draft' is
    indistinguishable from the promotion it describes, so it is reported rather than guessed at.

    What has changed is the blast radius. The schema requires a published release to pin its
    own ref, so the bytes this cycle writes are the ones the tag names either way; the report
    is what tells an operator who promoted nothing that the volume damaged a record.
    """
    sync_standard(_std(), tmp_path, _FakeClient(b"PUBLISHED"), log=lambda *a: None)
    prov = site_dir(tmp_path) / "demo" / "v1.0.0" / "provenance.json"
    record = json.loads(prov.read_text())
    record["lifecycle"] = "draft"
    prov.write_text(json.dumps(record), encoding="utf-8")

    with mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"RETAGGED")):
        stats = sync_all([_std()], tmp_path, verify=True, log=lambda *a: None)

    assert stats.published == 1, "a release was re-published with no check and nothing said so"


def test_a_non_string_status_is_classified_not_crashed_on(tmp_path):
    """Set membership was tested before the type was, so a record holding a non-string status
    raised TypeError from inside the guard added to classify it - and a TypeError carries no
    marker, so an operator's alerting could not see it."""
    sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), log=lambda *a: None)
    prov = site_dir(tmp_path) / "demo" / "v1.0.0" / "provenance.json"
    record = json.loads(prov.read_text())
    record["status"] = ["stable"]
    prov.write_text(json.dumps(record), encoding="utf-8")

    with mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"DATA")):
        stats = sync_all([_std()], tmp_path, verify=True, log=lambda *a: None)

    for _, message in stats.failures:
        assert "TypeError" not in message, message


def test_a_duplicated_record_is_damage_not_a_no_change(tmp_path):
    """Keying artifacts by name to stop ordering reading as a change also collapsed a
    duplicated entry onto one key, so a record holding the same artifact twice compared equal
    to the correct one and was never rewritten. Its sibling, _by_name_sums, deliberately treats
    anything not wholly intact as a change; this side did not."""
    _seed(tmp_path, b"DATA", lifecycle=Lifecycle.PUBLISHED)
    sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), log=lambda *a: None)
    prov = site_dir(tmp_path) / "demo" / "v1.0.0" / "provenance.json"
    record = json.loads(prov.read_text())
    record["artifacts"] = record["artifacts"] * 2
    prov.write_text(json.dumps(record), encoding="utf-8")

    sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), verify=True, log=lambda *a: None)

    rewritten = json.loads(prov.read_text())
    assert len(rewritten["artifacts"]) == 1, "a duplicated record stayed published"


def test_a_provenance_name_that_escapes_the_release_is_refused(tmp_path):
    """Every name in provenance.json is joined onto a release directory, and the orphan reaper
    passes one of them to unlink(). A record naming '../../../something' deleted a file outside
    the document root and the run reported success. The name is checked when the record is
    read, so such a record is damage rather than an instruction."""
    _seed(tmp_path, b"DATA", lifecycle=Lifecycle.DRAFT)
    outside = tmp_path / "IMPORTANT.conf"
    outside.write_text("do not delete", encoding="utf-8")
    prov = site_dir(tmp_path) / "demo" / "v1.0.0" / "provenance.json"
    record = json.loads(prov.read_text())
    record["artifacts"].append({"name": "../../../IMPORTANT.conf", "sha256": "0" * 64, "source": {}})
    prov.write_text(json.dumps(record), encoding="utf-8")

    with mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"DATA")):
        sync_all([_std(lifecycle=Lifecycle.DRAFT)], tmp_path, log=lambda *a: None)

    assert outside.exists(), "a file outside the document root was deleted"


def test_a_record_that_does_not_say_what_it_was_is_not_trusted(tmp_path):
    """Both defaults are wrong here, in opposite and unrecoverable directions: reading a record
    with no lifecycle as a draft turns every guard off and adopts upstream, and reading it as
    published takes the no-fetch fast path and freezes the draft era as the release. So it is
    refused.

    Reachable from an older cairn's record, and from the hand-restored backup the PROVENANCE
    UNREADABLE runbook entry tells operators to produce.
    """
    sync_standard(_pinned(Lifecycle.DRAFT, "release-branch"), tmp_path, _FakeClient(BRANCH_HEAD),
                  log=lambda *a: None)
    vdir = site_dir(tmp_path) / "demo" / "v1.0.0"
    record = json.loads((vdir / "provenance.json").read_text())
    del record["lifecycle"]
    (vdir / "provenance.json").write_text(json.dumps(record), encoding="utf-8")

    # A plain sync, not --verify: the no-fetch fast path this guards is only reachable without
    # it, and asserting through --verify would have passed with the guard removed.
    with mock.patch("cairn.sync.http_client", lambda: _FakeClient(TAGGED)):
        stats = sync_all([_pinned(Lifecycle.PUBLISHED, "v1.0.0")], tmp_path, log=lambda *a: None)

    assert stats.failures, "a record that cannot say what was published was trusted anyway"
    assert "PROVENANCE UNREADABLE" in stats.failures[0][1]
    assert (vdir / "demo.xsd").read_bytes() == BRANCH_HEAD, "the draft era's bytes were replaced"


def test_a_damaged_sha256sums_is_rebuilt_even_when_it_still_parses(tmp_path):
    """Comparing the parsed mapping rather than the bytes stopped ordering reading as a change,
    and made damage read as no change too: a garbage line appended to SHA256SUMS was skipped by
    the parser, the early return fired, and the damaged file stayed published on every later
    cycle - while `sha256sum -c`, the whole point of the file, errors on that line."""
    _seed(tmp_path, b"DATA", lifecycle=Lifecycle.PUBLISHED)
    sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), log=lambda *a: None)
    sums = site_dir(tmp_path) / "demo" / "v1.0.0" / "SHA256SUMS"
    intact = sums.read_bytes()
    sums.write_bytes(intact + b"\xde\xad a line with no separator\n")

    sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), verify=True, log=lambda *a: None)

    assert sums.read_bytes() == intact, "a damaged SHA256SUMS was left published"


def test_a_rate_limited_probe_is_retried(tmp_path):
    """raw.githubusercontent.com answers 429 to an unauthenticated client over its rate limit,
    which is what a fork's pull request is. Treating every 4xx as definitive reddened branches
    that had nothing to do with the manifests - the false red the retry exists to prevent."""
    attempts = []

    class Limited(_FakeClient):
        def head(self, url):
            attempts.append(url)
            response = super().head(url)
            if len(attempts) < 3:
                response.status_code = 429
            return response

    with mock.patch("cairn.sync._sleep"):
        stats = sync_standard(_std(), tmp_path, Limited(b""), dry_run=True, log=lambda *a: None)

    assert len(attempts) == 3
    assert stats.unreachable == 0


def test_a_release_directory_that_cannot_be_listed_is_reported(tmp_path):
    """Returning "no orphans" for "could not look" is indistinguishable from having nothing to
    do: the files this reaper exists to remove would keep serving forever, nothing logged, no
    counter moved, exit 0. That is the silence the three-way repair outcome was added to end."""
    _seed(tmp_path, b"DATA", lifecycle=Lifecycle.DRAFT, artifact_name="dropped.xsd")
    vdir = site_dir(tmp_path) / "demo" / "v1.0.0"
    (vdir / "provenance.json").write_bytes(b"\xff\xfe rotted\n")

    real_iterdir = Path.iterdir

    def denied(self):
        if self.name == "v1.0.0":
            raise PermissionError(13, "Permission denied")
        return real_iterdir(self)

    kept = Artifact(name="kept.xsd", role="schema", from_="repo", path="kept.xsd")
    with mock.patch.object(Path, "iterdir", denied), \
         mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"NEW")):
        stats = sync_all([_std(lifecycle=Lifecycle.DRAFT, artifacts=[kept])], tmp_path, log=lambda *a: None)

    assert stats.failures, "an unlistable release directory passed silently"
    assert "PermissionError" in stats.failures[0][1]


def test_a_slow_probe_is_not_retried(tmp_path):
    """The client allows 30s, so retrying a hang costs the full timeout again, per artifact, in
    exactly the upstream incident that made it slow. A gate that failed in seconds would take
    minutes to fail anyway. Only a fast failure is worth asking about twice."""
    import cairn.sync as sync_module

    clock = [0.0]
    attempts = []

    class Hanging(_FakeClient):
        def head(self, url):
            attempts.append(url)
            clock[0] += 30.0  # the client's timeout elapsed before it gave up
            raise httpx.ReadTimeout("")

    # The module's own seam, not the stdlib clock: patching time.monotonic sets the attribute
    # on the time module itself, so pytest's timing, the watchdog thread in the shell tests and
    # httpx internals all get the fake for the duration.
    with mock.patch.object(sync_module, "_monotonic", lambda: clock[0]), \
         mock.patch.object(sync_module, "_sleep") as slept:
        with pytest.raises(SyncError, match="UPSTREAM UNREACHABLE"):
            sync_standard(_std(), tmp_path, Hanging(b""), dry_run=True, log=lambda *a: None)

    assert len(attempts) == 1, "a 30s hang was retried, tripling the time to fail"
    assert slept.call_count == 0


def test_a_healthy_release_reaps_only_what_its_record_names(tmp_path):
    """The counterpart. With an intact record the directory is not authoritative, so a file
    that arrived by some other route must not be deleted on the strength of a scan."""
    _seed(tmp_path, b"DATA", lifecycle=Lifecycle.DRAFT)
    vdir = site_dir(tmp_path) / "demo" / "v1.0.0"
    unrelated = vdir / "left-by-something-else.txt"
    unrelated.write_text("not ours", encoding="utf-8")

    sync_standard(_std(lifecycle=Lifecycle.DRAFT), tmp_path, _FakeClient(b"DATA"), log=lambda *a: None)

    assert unrelated.exists()


def test_a_transient_upstream_fault_is_retried_before_it_reddens_ci(tmp_path):
    """A dry run gates pull requests, so one reset connection to raw.githubusercontent.com
    would fail a change that has nothing to do with the manifests."""
    attempts = []

    class Flaky(_FakeClient):
        def head(self, url):
            attempts.append(url)
            if len(attempts) < 3:
                raise httpx.ConnectError("connection reset by peer")
            return super().head(url)

    with mock.patch("cairn.sync._sleep") as slept:
        stats = sync_standard(
            _std(), tmp_path, Flaky(b""), dry_run=True, log=lambda *a: None
        )

    assert len(attempts) == 3
    assert stats.unreachable == 0
    assert slept.call_count == 2, "the retries must back off rather than hammer the host"


def test_a_definite_upstream_answer_is_not_retried(tmp_path):
    """A 404 is upstream telling us something true - a deleted tag, a repo made private - and
    asking twice more neither changes the answer nor makes CI any more informative."""
    attempts = []

    class Gone(_FakeClient):
        def head(self, url):
            attempts.append(url)
            response = super().head(url)
            response.status_code = 404
            return response

    with mock.patch("cairn.sync._sleep") as slept:
        with pytest.raises(SyncError, match="UPSTREAM UNREACHABLE"):
            sync_standard(_std(), tmp_path, Gone(b""), dry_run=True, log=lambda *a: None)

    assert len(attempts) == 1
    assert slept.call_count == 0


def test_an_unserved_release_is_not_probed_at_all(tmp_path):
    """Withdrawing a release is exactly what you do when upstream has moved or gone, so probing
    it anyway is a guard firing where there is nothing left to protect: no URL answers, and the
    manifest cannot drop a published release either, so a re-tagged upstream would fail that
    standard on every verify pass forever with no way out.

    Skipped rather than exempted. An exempted guard still lets the fetched bytes be written,
    which is how the previous model had a withdrawn release adopt upstream drift and then
    certify it on the way back to service.
    """
    sync_standard(_std(lifecycle=Lifecycle.PUBLISHED, served=False), tmp_path,
                  _FakeClient(b"OLD"), log=lambda *a: None)

    stats = sync_standard(
        _std(lifecycle=Lifecycle.PUBLISHED, served=False), tmp_path,
        _FakeClient(b"COMPLETELY-DIFFERENT"), verify=True, log=lambda *a: None
    )

    assert stats.fetched == 0, "an un-served release was put on the wire"
    assert not stats.failures, "an un-served release failed its standard over upstream drift"
    served = site_dir(tmp_path) / "demo" / "v1.0.0" / "demo.xsd"
    assert served.read_bytes() == b"OLD", "an un-served release adopted upstream's bytes"


def test_an_unreadable_published_file_is_not_overwritten_on_trust(tmp_path):
    """A copy that cannot be read corroborates nothing, so it takes the same path as one that
    disagrees. Falling through overwrote a published file from upstream with no comparison."""
    _seed(tmp_path, b"PUBLISHED", lifecycle=Lifecycle.PUBLISHED, extra_sha256=False)
    served = site_dir(tmp_path) / "demo" / "v1.0.0" / "demo.xsd"

    with mock.patch.object(Path, "read_bytes", side_effect=OSError(5, "Input/output error")):
        with mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"RETAGGED")):
            stats = sync_all([_std()], tmp_path, log=lambda *a: None)

    assert "UNVERIFIABLE PUBLISHED FILE" in stats.failures[0][1]
    assert served.read_bytes() == b"PUBLISHED"


def test_a_provenance_file_that_cannot_be_read_is_not_called_damage(tmp_path):
    """Damage on a published release is permanent and needs a person with an independent
    copy. Being unable to read the file right now is usually a mode or a mount, and telling
    an operator to restore from backup for that is both wrong and expensive."""
    sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), log=lambda *a: None)

    real_read_text = Path.read_text

    def denied(self, *args, **kwargs):
        if self.name == "provenance.json":
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    with mock.patch.object(Path, "read_text", denied):
        with mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"DATA")):
            stats = sync_all([_std()], tmp_path, log=lambda *a: None)

    message = stats.failures[0][1]
    assert "PROVENANCE UNAVAILABLE" in message
    assert "backup" not in message, "a read failure must not be reported as data loss"


def test_the_no_checksum_backstop_refuses_before_writing(tmp_path):
    """No path can produce a record without a checksum today, which is why this guard needs a
    test that forces the condition: it is a backstop for a regression that shipped once, where
    such a record put a claim in SHA256SUMS for bytes nothing had written.

    The refusal has to happen in the plan phase. Raising from the commit loop would leave the
    release half written with its metadata describing the previous state.
    """
    def record_without_checksum(art, resolved, data, digest, commit):
        record = S_artifact_record(art, resolved, data, digest, commit)
        del record["sha256"]
        return record

    with mock.patch("cairn.sync._artifact_record", record_without_checksum):
        with pytest.raises(SyncError, match="NO CHECKSUM RECORDED"):
            sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), log=lambda *a: None)

    assert not (site_dir(tmp_path) / "demo" / "v1.0.0" / "demo.xsd").exists(), "nothing may be written"


def test_a_run_survives_stdout_dying_while_it_reports_a_failure(tmp_path):
    """A closed pipe - `docker logs` killed, a full disk - while the per-release handler is
    writing its [FAIL] line. The release is already counted by then, so the accounting stays
    honest; what matters is that the standard is isolated rather than taking the run down."""
    _seed(tmp_path, b"PUBLISHED")

    def log_that_dies_reporting_a_failure(message=""):
        if "[FAIL]" in message and "v1.0.0" in message:
            raise BrokenPipeError(32, "Broken pipe")

    with mock.patch("cairn.sync.http_client", lambda: _FakeClient(b"RETAGGED")):
        stats = sync_all([_std()], tmp_path, verify=True, log=log_that_dies_reporting_a_failure)

    assert stats.failures, "the fault was not reported at all"
    assert stats.nothing_succeeded, (
        f"a run that established nothing reported otherwise: "
        f"attempted={stats.releases_attempted} failed={stats.releases_failed}"
    )


def test_a_dormant_release_is_not_resolved_probed_or_fetched(tmp_path):
    """Dormancy is a property of the release, so it has to hold before anything reaches the
    network - not inside the per-artifact decision, which runs after resolve().

    resolve() is not free: a `release-asset` artifact resolves through a GitHub API call that
    raises when the tag is gone, and a retired tag is the usual reason a release was withdrawn.
    Deciding dormancy per artifact therefore still failed the standard on every cycle forever,
    and the manifest cannot drop a published release either.
    """
    _seed(tmp_path, b"PUBLISHED", served=False)

    class RefusesEverything:
        def get(self, *a, **kw):
            raise AssertionError("a dormant release reached the network")
        head = get
        def __enter__(self): return self
        def __exit__(self, *a): return False

    std = _std(served=False, artifacts=[
        Artifact(name="demo.xsd", role="schema", from_="release-asset",
                 asset="demo.xsd", release_tag="v1.0.0"),
    ])
    with mock.patch("cairn.sync.http_client", RefusesEverything):
        stats = sync_all([std], tmp_path, verify=True, log=lambda *a: None)

    assert not stats.failures, stats.failures
    assert stats.fetched == 0 and stats.planned == 0


def test_the_dry_run_does_not_probe_a_release_it_would_not_sync(tmp_path):
    """The gate must not reject what the real thing accepts. A release withdrawn because its
    upstream was retired probes UNREACHABLE, so this failed every pull request from then on,
    with no manifest edit able to fix it."""
    class Gone:
        def get(self, *a, **kw):
            raise AssertionError("the dry run probed a release that is not served")
        head = get
        def __enter__(self): return self
        def __exit__(self, *a): return False

    with mock.patch("cairn.sync.http_client", Gone):
        stats = sync_all([_std(served=False)], tmp_path, dry_run=True, log=lambda *a: None)

    assert not stats.failures, stats.failures
    assert stats.unreachable == 0
