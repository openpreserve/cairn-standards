import json
import stat
from unittest import mock

import pytest

from cairn.manifest import Artifact, MajorLine, Release, Source, Standard, Steward
from cairn.sync import SyncError, sync_all, sync_standard
from cairn.util import sha256_hex


class _FakeResp:
    def __init__(self, status, content):
        self.status_code = status
        self.content = content

    def json(self):
        return {}


class _FakeClient:
    """Returns fixed bytes for any GET - stands in for the network."""

    def __init__(self, content: bytes):
        self._c = content

    def get(self, url, headers=None):
        return _FakeResp(200, self._c)

    def head(self, url):
        return _FakeResp(200, b"")

    # sync_all owns its client via `with http_client() as client`, so standing in for it
    # means standing in for the context manager too.
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _std(status="stable", artifacts=None):
    if artifacts is None:
        artifacts = [Artifact(name="demo.xsd", role="schema", from_="repo", path="demo.xsd")]
    rel = Release(version="1.0.0", status=status, artifacts=artifacts, ref="main")
    return Standard(
        id="demo",
        title="Demo",
        summary="s",
        steward=Steward(org="x"),
        source=Source(type="github", repo="o/r", ref="main"),
        major_lines=[MajorLine(major=1, latest="1.0.0")],
        releases=[rel],
    )


def _seed(root, content: bytes, status="stable", artifact_name="demo.xsd", extra_sha256=True):
    vdir = root / "site" / "demo" / "v1.0.0"
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
        "status": status,
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
    _seed(tmp_path, b"OLD", status="draft")
    stats = sync_standard(_std(status="draft"), tmp_path, _FakeClient(b"NEW"), log=lambda *a: None)
    assert stats.fetched == 1
    assert (tmp_path / "site" / "demo" / "v1.0.0" / "demo.xsd").read_bytes() == b"NEW"


# --- new robustness tests ---

def test_corrupt_provenance_json_is_treated_as_fresh(tmp_path):
    """A truncated or invalid provenance.json should not crash sync; treat as first run."""
    vdir = tmp_path / "site" / "demo" / "v1.0.0"
    vdir.mkdir(parents=True)
    (vdir / "provenance.json").write_text("{not valid json", encoding="utf-8")

    stats = sync_standard(_std(), tmp_path, _FakeClient(b"BYTES"), log=lambda *a: None)
    assert stats.fetched == 1
    prov = json.loads((vdir / "provenance.json").read_text())
    assert prov["artifacts"][0]["sha256"] == sha256_hex(b"BYTES")


def test_provenance_missing_artifacts_key_is_treated_as_fresh(tmp_path):
    """provenance.json without an 'artifacts' list should not crash sync."""
    vdir = tmp_path / "site" / "demo" / "v1.0.0"
    vdir.mkdir(parents=True)
    (vdir / "provenance.json").write_text(json.dumps({"standard": "demo"}), encoding="utf-8")

    stats = sync_standard(_std(), tmp_path, _FakeClient(b"BYTES"), log=lambda *a: None)
    assert stats.fetched == 1


def test_frozen_record_without_sha256_does_not_raise_false_positive(tmp_path):
    """An old provenance record that predates sha256 recording must not trigger FROZEN VERSION CHANGED.

    On the next sync/verify run, the missing sha256 is computed and stored so future
    runs can do a real integrity check.
    """
    _seed(tmp_path, b"DATA", extra_sha256=False)

    stats = sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), verify=True, log=lambda *a: None)
    # Re-recorded (not verified) because there was nothing to compare against
    assert stats.fetched == 1
    prov = json.loads((tmp_path / "site" / "demo" / "v1.0.0" / "provenance.json").read_text())
    assert prov["artifacts"][0]["sha256"] == sha256_hex(b"DATA")


def test_orphaned_artifact_is_removed_from_a_draft(tmp_path):
    """A file in provenance that is no longer in the manifest should be deleted on next sync.

    Only while the release is still mutable - a draft's bytes are not published promises.
    """
    _seed(tmp_path, b"OLD", status="draft", artifact_name="old.xsd")
    orphan = tmp_path / "site" / "demo" / "v1.0.0" / "old.xsd"
    assert orphan.exists()

    new_artifact = Artifact(name="new.xsd", role="schema", from_="repo", path="new.xsd")
    sync_standard(
        _std(status="draft", artifacts=[new_artifact]), tmp_path, _FakeClient(b"NEW"), log=lambda *a: None
    )

    assert not orphan.exists(), "orphaned artifact should have been removed"
    assert (tmp_path / "site" / "demo" / "v1.0.0" / "new.xsd").exists()


def test_provenance_written_atomically(tmp_path):
    """provenance.json should not leave a partial file if interrupted (temp+rename pattern)."""
    # This is structural: verify the file appears complete and parseable after sync.
    stats = sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), log=lambda *a: None)
    assert stats.fetched == 1
    prov_path = tmp_path / "site" / "demo" / "v1.0.0" / "provenance.json"
    prov = json.loads(prov_path.read_text())
    assert prov["artifacts"][0]["name"] == "demo.xsd"


# --- write-once guards ---

def test_dropping_an_artifact_from_a_frozen_release_is_rejected(tmp_path):
    """Unpublishing a frozen artifact breaks the URL contract as surely as changing its bytes.

    `/demo/v1.0.0/old.xsd` has been handed out and may be cited in a schema import. Deleting
    the file answers 404 where it used to answer 200, so the manifest edit is refused and the
    published file is left alone.
    """
    _seed(tmp_path, b"PUBLISHED", status="stable", artifact_name="old.xsd")
    published = tmp_path / "site" / "demo" / "v1.0.0" / "old.xsd"

    kept = Artifact(name="new.xsd", role="schema", from_="repo", path="new.xsd")
    with pytest.raises(SyncError, match="FROZEN VERSION LOST AN ARTIFACT"):
        sync_standard(
            _std(status="stable", artifacts=[kept]), tmp_path, _FakeClient(b"NEW"), log=lambda *a: None
        )

    assert published.exists(), "a frozen release's published artifact must survive"


def test_refused_manifest_edit_writes_nothing(tmp_path):
    """The refusal must happen before anything is written, not after.

    Checking after the fetch loop left the replacement artifact published under a frozen
    version: absent from provenance, absent from SHA256SUMS, served with a one-year immutable
    cache, and permanently beyond the orphan reaper, which only considers names the previous
    provenance recorded. Every later cycle repeated the same partial write.
    """
    _seed(tmp_path, b"PUBLISHED", status="stable", artifact_name="old.xsd")
    vdir = tmp_path / "site" / "demo" / "v1.0.0"

    replacement = Artifact(name="new.xsd", role="schema", from_="repo", path="new.xsd")
    with pytest.raises(SyncError, match="FROZEN VERSION LOST AN ARTIFACT"):
        sync_standard(
            _std(status="stable", artifacts=[replacement]), tmp_path, _FakeClient(b"NEW"), log=lambda *a: None
        )

    assert not (vdir / "new.xsd").exists(), "an unrecorded file was published under a frozen version"
    assert sorted(p.name for p in vdir.iterdir()) == ["old.xsd", "provenance.json"]
    prov = json.loads((vdir / "provenance.json").read_text())
    assert [a["name"] for a in prov["artifacts"]] == ["old.xsd"]


def test_frozen_byte_change_writes_nothing(tmp_path):
    """Same guarantee for the other refusal: a changed frozen artifact is not written."""
    _seed(tmp_path, b"OLD", status="stable")
    artifact = tmp_path / "site" / "demo" / "v1.0.0" / "demo.xsd"

    with pytest.raises(SyncError, match="FROZEN VERSION CHANGED"):
        sync_standard(_std(), tmp_path, _FakeClient(b"TAMPERED"), verify=True, log=lambda *a: None)

    assert artifact.read_bytes() == b"OLD", "frozen bytes were overwritten before the check"


def test_withdrawn_release_may_drop_artifacts(tmp_path):
    """`withdrawn` is the deliberate way to unpublish, so reaping is allowed there."""
    _seed(tmp_path, b"GONE", status="withdrawn", artifact_name="old.xsd")
    orphan = tmp_path / "site" / "demo" / "v1.0.0" / "old.xsd"

    kept = Artifact(name="new.xsd", role="schema", from_="repo", path="new.xsd")
    sync_standard(
        _std(status="withdrawn", artifacts=[kept]), tmp_path, _FakeClient(b"NEW"), log=lambda *a: None
    )

    assert not orphan.exists()


@pytest.mark.parametrize("name", ["demo.xsd", "provenance.json", "SHA256SUMS"])
def test_synced_files_are_readable_by_the_web_server(tmp_path, name):
    """nginx workers run unprivileged and answer 403 for anything they cannot open.

    mkstemp creates 0600 and os.replace preserves it, so without an explicit chmod every
    file sync writes becomes a 403 the moment the syncer image is rebuilt.
    """
    sync_standard(_std(status="draft"), tmp_path, _FakeClient(b"BYTES"), log=lambda *a: None)
    mode = (tmp_path / "site" / "demo" / "v1.0.0" / name).stat().st_mode
    assert mode & stat.S_IROTH, f"{name} is {oct(mode & 0o777)} - the web server cannot read it"


@pytest.mark.parametrize("verify", [False, True], ids=["sync", "verify"])
def test_unreadable_frozen_artifact_is_repaired(tmp_path, verify):
    """A frozen artifact is never rewritten, so a bad mode would otherwise be permanent.

    Anything left 0600 by an earlier bug is skipped by a plain sync and `continue`d past by
    --verify, so neither path would ever restore it and the URL would 403 forever.
    """
    _seed(tmp_path, b"DATA", status="stable")
    artifact = tmp_path / "site" / "demo" / "v1.0.0" / "demo.xsd"
    artifact.chmod(0o600)

    stats = sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), verify=verify, log=lambda *a: None)

    assert artifact.stat().st_mode & stat.S_IROTH, "the web server still cannot read it"
    assert stats.repaired == 1


def test_unchanged_frozen_release_does_not_rewrite_metadata(tmp_path):
    """provenance.json and SHA256SUMS sit beside write-once artifacts and are documented as
    permanent. Rewriting them every cycle only to move `updated_at` churns the mtime of files
    nothing has changed and needlessly invalidates their cache validators."""
    _seed(tmp_path, b"DATA", status="stable")
    sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), log=lambda *a: None)
    prov_path = tmp_path / "site" / "demo" / "v1.0.0" / "provenance.json"
    before = prov_path.stat().st_mtime_ns

    sync_standard(_std(), tmp_path, _FakeClient(b"DATA"), log=lambda *a: None)

    assert prov_path.stat().st_mtime_ns == before, "provenance.json was rewritten with no change"


def test_one_standard_failing_does_not_stop_the_others(tmp_path):
    """A failed integrity check on one standard must not stop the rest of the registry.

    sync_all previously let the SyncError propagate, so a single re-tagged artifact stopped
    every other standard replicating and stopped the render entirely, freezing the whole site
    at its last good state until someone intervened.
    """
    _seed(tmp_path, b"OLD", status="stable")  # demo v1.0.0, will fail on changed bytes

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
                status="draft",
                ref="main",
                artifacts=[Artifact(name="other.xsd", role="schema", from_="repo", path="other.xsd")],
            )
        ],
    )

    with mock.patch("cairn.sync.http_client", return_value=_FakeClient(b"NEW")):
        stats = sync_all([_std(), healthy], tmp_path, verify=True, log=lambda *a: None)

    assert [std_id for std_id, _ in stats.failures] == ["demo"]
    assert not stats.ok
    assert (tmp_path / "site" / "other" / "v1.0.0" / "other.xsd").exists(), \
        "the healthy standard was not replicated"
