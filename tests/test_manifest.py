import json
import shutil
from pathlib import Path

import pytest
from fakes import workspace

from cairn.config import find_root
from cairn.manifest import (
    Lifecycle,
    ManifestError,
    compare_to_baseline,
    load_all,
    load_standard,
)
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
    workspace(tmp_path, {"demo": body})
    return tmp_path / "standards" / "demo"


VALID = """
id: demo
title: Demo
summary: A demo standard.
steward: { org: Someone }
source: { type: github, repo: owner/repo, ref: main }
major_lines: [ { major: 1, latest: 1.0.0 } ]
releases:
  - version: 1.0.0
    lifecycle: published
    ref: v1.0.0
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
    return workspace(tmp_path / name, {"demo": body})


PUBLISHED = VALID.replace(
    "      - { name: demo.xsd, role: schema, from: repo, path: demo.xsd }",
    "      - { name: demo.xsd, role: schema, from: repo, path: demo.xsd }\n"
    "      - { name: demo.sch, role: schematron, from: repo, path: demo.sch }",
)


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
    """One ref change repoints every artifact that inherits it, reported as a single error."""
    before = load_all(_workspace(tmp_path, "before", PUBLISHED))
    after = load_all(_workspace(tmp_path, "after", PUBLISHED.replace("ref: v1.0.0", "ref: v1.0.1")))
    errors = compare_to_baseline(after, before)
    assert len(errors) == 1, errors
    assert "source repointed" in errors[0]
    assert "demo.xsd" in errors[0] and "demo.sch" in errors[0]


def test_baseline_rejects_repointing_one_artifact_path(tmp_path):
    """Same URL, different upstream file. The release block itself is untouched."""
    before = load_all(_workspace(tmp_path, "before", PUBLISHED))
    after = load_all(
        _workspace(tmp_path, "after", PUBLISHED.replace("path: demo.sch }", "path: rules/demo.sch }"))
    )
    errors = compare_to_baseline(after, before)
    assert len(errors) == 1, errors
    assert "demo.sch" in errors[0] and "demo.xsd" not in errors[0]


def test_a_published_release_cannot_inherit_its_ref(tmp_path):
    """A release with no ref of its own inherits source.ref, so moving source.ref repoints the
    bytes without the release block changing at all. That was caught by comparing resolved
    locators rather than literal fields; it is now unreachable, because a published release has
    to pin its own ref and the schema refuses one that does not. Asserted here so that relaxing
    the schema cannot quietly restore the hole."""
    inheriting = PUBLISHED.replace("    lifecycle: published\n    ref: v1.0.0", "    lifecycle: published")
    with pytest.raises(ManifestError, match="'ref' is a required property"):
        load_all(_workspace(tmp_path, "before_inh", inheriting))


def test_baseline_rejects_repointing_an_inherited_ref(tmp_path):
    """A release with no `ref` of its own inherits `source.ref`, so moving that repoints the
    bytes without the release block changing at all. compare_to_baseline compares resolved
    locators rather than literal fields, which is the only reason this is caught.

    Exercised through a draft, because a published release must pin its own ref. Written with
    an explicit release-level ref for a while, which quietly stopped covering
    `artifact_locator`'s `art.ref or rel.ref or std.source.ref` fallback: had the fallback
    regressed to ignore `source.ref` entirely, that version still passed.
    """
    inheriting = PUBLISHED.replace("    lifecycle: published\n    ref: v1.0.0", "    lifecycle: draft")
    before = load_all(_workspace(tmp_path, "before_inh", inheriting))
    # Published in the baseline, so the write-once comparison applies; the manifest under test
    # keeps it published and moves only the standard-level ref it inherits from.
    for std in before:
        for rel in std.releases:
            rel.lifecycle = Lifecycle.PUBLISHED
    after = load_all(_workspace(tmp_path, "after_inh", inheriting.replace("ref: main", "ref: moved")))
    for std in after:
        for rel in std.releases:
            rel.lifecycle = Lifecycle.PUBLISHED

    errors = compare_to_baseline(after, before)

    assert len(errors) == 1, errors
    assert "'main' -> 'moved'" in errors[0]


def test_baseline_rejects_unfreezing_a_published_release(tmp_path):
    """Reverting stable to draft would let later syncs overwrite published bytes in place."""
    before = load_all(_workspace(tmp_path, "before_uf", PUBLISHED))
    after = load_all(_workspace(tmp_path, "after_uf", PUBLISHED.replace("lifecycle: published", "lifecycle: draft")))
    errors = compare_to_baseline(after, before)
    assert any("un-freezes" in e for e in errors), errors


def test_baseline_rejects_deleting_a_published_release(tmp_path):
    before = load_all(_workspace(tmp_path, "before", PUBLISHED))
    replacement = PUBLISHED.replace("version: 1.0.0\n    lifecycle: published", "version: 2.0.0\n    lifecycle: published")
    replacement = replacement.replace("major: 1, latest: 1.0.0", "major: 2, latest: 2.0.0")
    after = load_all(_workspace(tmp_path, "after", replacement))
    errors = compare_to_baseline(after, before)
    assert any("is now gone" in e for e in errors)


def test_baseline_allows_a_draft_to_change_freely(tmp_path):
    """A draft is explicitly not a published promise, so artifact churn is fine."""
    draft = PUBLISHED.replace("lifecycle: published", "lifecycle: draft")
    before = load_all(_workspace(tmp_path, "before", draft))
    after = load_all(_workspace(tmp_path, "after", draft.replace(
        "      - { name: demo.sch, role: schematron, from: repo, path: demo.sch }\n", "")))
    assert compare_to_baseline(after, before) == []


def test_baseline_rejects_deleting_a_whole_published_standard(tmp_path):
    """The cheapest possible edit, and the one the gate most needs to stop.

    compare_to_baseline was driven from the current set, so a standard that no longer exists
    was never visited and the check passed clean while every URL under it went away.
    """
    before = load_all(_workspace(tmp_path, "before_del", PUBLISHED))
    empty = _workspace(tmp_path, "after_del", PUBLISHED)
    shutil.rmtree(empty / "standards" / "demo")

    errors = compare_to_baseline(load_all(empty), before)
    assert len(errors) == 1, errors
    assert "whole standard was removed" in errors[0] and "1.0.0" in errors[0]


def test_baseline_allows_deleting_a_standard_that_was_only_draft(tmp_path):
    """Nothing was ever published, so there is no promise to break."""
    draft = PUBLISHED.replace("lifecycle: published", "lifecycle: draft")
    before = load_all(_workspace(tmp_path, "before_dd", draft))
    empty = _workspace(tmp_path, "after_dd", draft)
    shutil.rmtree(empty / "standards" / "demo")
    assert compare_to_baseline(load_all(empty), before) == []


def test_missing_schema_gives_a_clear_error_not_a_traceback(tmp_path):
    """find_root falls back to its argument, so a mistyped --baseline path arrives here."""
    with pytest.raises(ManifestError, match="Is this a Cairn workspace"):
        load_all(tmp_path / "nowhere")


def test_removed_standard_lists_versions_in_semver_order(tmp_path):
    """Sorted as text, v10.0.0 lands between v1.0.0 and v2.0.0. semver_key was already
    imported in this module and every other ordering in the codebase uses it."""
    artifacts = "    artifacts:\n      - { name: demo.xsd, role: schema, from: repo, path: demo.xsd }\n"
    body = (
        "id: demo\n"
        "title: Demo\n"
        "summary: A demo standard.\n"
        "steward: { org: Someone }\n"
        "source: { type: github, repo: owner/repo, ref: main }\n"
        "major_lines: [ { major: 1, latest: 1.0.0 }, { major: 2, latest: 2.0.0 }, "
        "{ major: 10, latest: 10.0.0 } ]\n"
        "releases:\n"
        + "".join(f"  - version: {v}\n    lifecycle: published\n    ref: v{v}\n{artifacts}" for v in ("1.0.0", "2.0.0", "10.0.0"))
    )
    baseline = load_all(_workspace(tmp_path, "before", body))

    errors = compare_to_baseline([], baseline)

    assert len(errors) == 1, errors
    assert "v1.0.0, v2.0.0, v10.0.0" in errors[0]


def test_a_manifest_that_is_not_utf8_is_a_named_error(tmp_path):
    """UnicodeDecodeError is a ValueError, so it slipped past the YAMLError guard and reached
    the top level as a traceback rather than a message naming the file."""
    d = _write_manifest(tmp_path, VALID)
    (d / "standard.yaml").write_bytes(b"\xff\xfe id: demo\n")

    with pytest.raises(ManifestError, match="cannot be read"):
        load_standard(d, root=tmp_path)


def test_a_schema_that_is_not_utf8_is_a_named_error(tmp_path):
    """Same class, one file away: the schema is read the same way and was guarded by nothing."""
    d = _write_manifest(tmp_path, VALID)
    (tmp_path / "schemas" / "standard.schema.json").write_bytes(b"\xff\xfe{}")

    with pytest.raises(ManifestError, match="manifest schema"):
        load_standard(d, root=tmp_path)
