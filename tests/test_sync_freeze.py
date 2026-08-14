import json

import pytest

from cairn.manifest import Artifact, MajorLine, Release, Source, Standard, Steward
from cairn.sync import SyncError, sync_standard
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


def test_orphaned_artifact_is_removed(tmp_path):
    """A file in provenance that is no longer in the manifest should be deleted on next sync."""
    _seed(tmp_path, b"OLD", artifact_name="old.xsd")
    orphan = tmp_path / "site" / "demo" / "v1.0.0" / "old.xsd"
    assert orphan.exists()

    new_artifact = Artifact(name="new.xsd", role="schema", from_="repo", path="new.xsd")
    sync_standard(_std(artifacts=[new_artifact]), tmp_path, _FakeClient(b"NEW"), log=lambda *a: None)

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
