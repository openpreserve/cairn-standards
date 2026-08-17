import json
from pathlib import Path

import pytest

from cairn.config import find_root
from cairn.manifest import ManifestError, compare_to_baseline, load_all, load_standard
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


# --- write-once checks against a baseline (what CI runs on a pull request) ---

def _workspace(tmp_path: Path, name: str, body: str) -> Path:
    """A standalone workspace root, so two revisions of a manifest can be compared."""
    root = tmp_path / name
    d = root / "standards" / "demo"
    d.mkdir(parents=True)
    (d / "standard.yaml").write_text(body, encoding="utf-8")
    schemas = root / "schemas"
    schemas.mkdir()
    (schemas / "standard.schema.json").write_text(
        (ROOT / "schemas" / "standard.schema.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return root


PUBLISHED = VALID.replace(
    "      - { name: demo.xsd, role: schema, from: repo, path: demo.xsd }",
    "      - { name: demo.xsd, role: schema, from: repo, path: demo.xsd }\n"
    "      - { name: demo.sch, role: schematron, from: repo, path: demo.sch }",
).replace("status: stable", "status: stable\n    ref: v1.0.0")


def test_baseline_accepts_an_unchanged_manifest(tmp_path):
    before = load_all(_workspace(tmp_path, "before", PUBLISHED))
    after = load_all(_workspace(tmp_path, "after", PUBLISHED))
    assert compare_to_baseline(after, before) == []


def test_baseline_rejects_removing_a_published_artifact(tmp_path):
    """This is the failure that previously merged green and broke the deployment instead."""
    before = load_all(_workspace(tmp_path, "before", PUBLISHED))
    after = load_all(_workspace(tmp_path, "after", PUBLISHED.replace(
        "      - { name: demo.sch, role: schematron, from: repo, path: demo.sch }\n", "")))
    errors = compare_to_baseline(after, before)
    assert len(errors) == 1
    assert "demo.sch" in errors[0]


def test_baseline_rejects_repointing_a_published_ref(tmp_path):
    before = load_all(_workspace(tmp_path, "before", PUBLISHED))
    after = load_all(_workspace(tmp_path, "after", PUBLISHED.replace("ref: v1.0.0", "ref: v1.0.1")))
    errors = compare_to_baseline(after, before)
    assert len(errors) == 1
    assert "ref changed" in errors[0]


def test_baseline_rejects_deleting_a_published_release(tmp_path):
    before = load_all(_workspace(tmp_path, "before", PUBLISHED))
    replacement = PUBLISHED.replace("version: 1.0.0\n    status: stable", "version: 2.0.0\n    status: stable")
    replacement = replacement.replace("major: 1, latest: 1.0.0", "major: 2, latest: 2.0.0")
    after = load_all(_workspace(tmp_path, "after", replacement))
    errors = compare_to_baseline(after, before)
    assert any("is now gone" in e for e in errors)


def test_baseline_allows_a_draft_to_change_freely(tmp_path):
    """A draft is explicitly not a published promise, so artifact churn is fine."""
    draft = PUBLISHED.replace("status: stable", "status: draft")
    before = load_all(_workspace(tmp_path, "before", draft))
    after = load_all(_workspace(tmp_path, "after", draft.replace(
        "      - { name: demo.sch, role: schematron, from: repo, path: demo.sch }\n", "")))
    assert compare_to_baseline(after, before) == []
