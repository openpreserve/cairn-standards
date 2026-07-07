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
    """Returns fixed bytes for any GET — stands in for the network."""

    def __init__(self, content: bytes):
        self._c = content

    def get(self, url, headers=None):
        return _FakeResp(200, self._c)

    def head(self, url):
        return _FakeResp(200, b"")


def _std(status="stable"):
    art = Artifact(name="demo.xsd", role="schema", from_="repo", path="demo.xsd")
    rel = Release(version="1.0.0", status=status, artifacts=[art], ref="main")
    return Standard(
        id="demo",
        title="Demo",
        summary="s",
        steward=Steward(org="x"),
        source=Source(type="github", repo="o/r", ref="main"),
        major_lines=[MajorLine(major=1, latest="1.0.0")],
        releases=[rel],
    )


def _seed(root, content: bytes, status="stable"):
    vdir = root / "site" / "demo" / "v1.0.0"
    vdir.mkdir(parents=True)
    (vdir / "demo.xsd").write_bytes(content)
    prov = {
        "standard": "demo",
        "version": "1.0.0",
        "status": status,
        "artifacts": [
            {
                "name": "demo.xsd",
                "role": "schema",
                "media_type": "application/xml",
                "bytes": len(content),
                "sha256": sha256_hex(content),
                "source": {},
                "fetched_at": "2026-01-01T00:00:00+00:00",
            }
        ],
    }
    (vdir / "provenance.json").write_text(json.dumps(prov), encoding="utf-8")


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
