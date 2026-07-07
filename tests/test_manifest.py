import json
from pathlib import Path

import pytest

from cairn.config import find_root
from cairn.manifest import ManifestError, load_all, load_standard
from cairn.util import media_type_for, semver_key

ROOT = find_root(Path(__file__).resolve().parent)


def test_seeded_manifests_all_valid():
    standards = load_all(ROOT)
    ids = {s.id for s in standards}
    assert {"eaf", "ead", "eac"} <= ids
    eaf = next(s for s in standards if s.id == "eaf")
    assert eaf.namespace_for(1) == "https://standards.openpreservation.org/eaf/v1"
    assert eaf.release("1.0.0") is not None


def test_util_helpers():
    assert semver_key("1.10.2") == (1, 10, 2)
    assert media_type_for("eaf.xsd") == "application/xml"
    assert media_type_for("thing.pdf") == "application/pdf"
    assert media_type_for("x", override="text/foo") == "text/foo"


def _write_manifest(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "standards" / "demo"
    d.mkdir(parents=True)
    (d / "standard.yaml").write_text(body, encoding="utf-8")
    # a JSON schema copy so find_root/validator resolves against tmp
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    (schemas / "standard.schema.json").write_text(
        (ROOT / "schemas" / "standard.schema.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return d


VALID = """
id: demo
title: Demo
summary: A demo standard.
steward: { org: Someone }
source: { type: github, repo: owner/repo, ref: main }
major_lines: [ { major: 1, latest: 1.0.0 } ]
releases:
  - version: 1.0.0
    status: stable
    artifacts:
      - { name: demo.xsd, role: schema, from: repo, path: demo.xsd }
"""


def test_valid_manifest_loads(tmp_path):
    d = _write_manifest(tmp_path, VALID)
    std = load_standard(d, root=tmp_path)
    assert std.id == "demo"


def test_bad_id_rejected(tmp_path):
    d = _write_manifest(tmp_path, VALID.replace("id: demo", "id: Demo_Bad"))
    with pytest.raises(ManifestError):
        load_standard(d, root=tmp_path)


def test_latest_must_exist(tmp_path):
    d = _write_manifest(tmp_path, VALID.replace("latest: 1.0.0", "latest: 9.9.9"))
    with pytest.raises(ManifestError, match="no matching release"):
        load_standard(d, root=tmp_path)


def test_missing_locator_rejected(tmp_path):
    # from: repo without a path
    broken = VALID.replace("{ name: demo.xsd, role: schema, from: repo, path: demo.xsd }",
                           "{ name: demo.xsd, role: schema, from: repo }")
    d = _write_manifest(tmp_path, broken)
    with pytest.raises(ManifestError):
        load_standard(d, root=tmp_path)
