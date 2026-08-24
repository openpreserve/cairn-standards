"""The rules line: revisions that move independently of the schema versions beside them.

The point of this whole feature is one sentence, and it is the first thing asserted here: a
frozen release must be able to gain new validation rules without the release moving. Every
other test in this file guards a way that could be true in appearance while being false in
substance - the rules published somewhere a reader cannot find them, a revision that can be
edited after publication, a moving pointer aimed at bytes that are still following a branch,
or a URL that resolves to a redirect into a 404.

The routing tests earn their place separately. `nginx` takes the first regex location that
matches, and the pin-to-latest redirect matches everything under `/<id>/vN/` - so a rules
route emitted after it is not merely lower priority, it is dead. That is the same property
that already required `test_stranded_temp_files_are_never_served`, and being bitten by it
twice is why it is pinned rather than remembered.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest import mock

import pytest
from fakes import FakeClient, workspace

from cairn.config import site_dir
from cairn.manifest import ManifestError, compare_to_baseline, load_all
from cairn.nginx import render_routes
from cairn.render import render_site
from cairn.sync import sync_all

RELEASES = """
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

ONE_REVISION = RELEASES + """
rules:
  - revision: "2026-07"
    applies_to: 1
    tested_against: 1.0.0
    lifecycle: published
    ref: RULES-2026-07
    artifacts:
      - { name: demo.sch, role: schematron, from: repo, path: schematron/demo.sch }
"""


def _load(tmp_path: Path, name: str, body: str):
    """A standalone workspace, so two revisions of one manifest can be compared."""
    return load_all(workspace(tmp_path / name, {"demo": body}))


def _one(tmp_path: Path, body: str):
    return _load(tmp_path, "only", body)[0]


# --- the promise this feature exists to keep ------------------------------------------------

def test_rules_can_be_added_to_a_standard_whose_releases_are_all_frozen(tmp_path):
    """The whole reason the rules line is not inside `releases:`.

    A published release may never gain an artifact, so rules living inside one could only ever
    be revised by minting a new schema version for a schema that did not change. Out here the
    same edit is ordinary: the release is untouched, and the gate says so.
    """
    before = _load(tmp_path, "before", RELEASES)
    after = _load(tmp_path, "after", ONE_REVISION)

    assert compare_to_baseline(after, before) == []
    assert after[0].release("1.0.0") == before[0].release("1.0.0"), "the release moved"


def test_a_second_revision_leaves_the_first_one_exactly_as_it_was(tmp_path):
    """Revising the rules must cost the earlier revision nothing at all."""
    second = ONE_REVISION + """
  - revision: "2027-03"
    applies_to: 1
    tested_against: 1.0.0
    lifecycle: published
    ref: RULES-2027-03
    artifacts:
      - { name: demo.sch, role: schematron, from: repo, path: schematron/demo.sch }
"""
    before = _load(tmp_path, "before", ONE_REVISION)
    after = _load(tmp_path, "after", second)

    assert compare_to_baseline(after, before) == []
    assert after[0].rule_set(1, "2026-07") == before[0].rule_set(1, "2026-07")


def test_a_revision_is_published_under_the_major_line_not_under_a_version(tmp_path):
    """Where the rules go is not a formatting choice: the `.sch` declares the namespace URI,
    which is major-only, so publishing them under a concrete version would contradict the file
    and force the same rules to be republished under every later patch release."""
    rules = _one(tmp_path, ONE_REVISION).rule_set(1, "2026-07")
    assert rules.slug == "v1/schematron/2026-07"


# --- write-once, which is the same promise a release makes ----------------------------------

def test_the_gate_refuses_removing_a_published_revision(tmp_path):
    before = _load(tmp_path, "before", ONE_REVISION)
    after = _load(tmp_path, "after", RELEASES)

    errors = compare_to_baseline(after, before)

    assert len(errors) == 1, errors
    assert "v1/schematron/2026-07" in errors[0] and "is now gone" in errors[0]


def test_the_gate_refuses_unfreezing_a_published_revision(tmp_path):
    """The single forbidden edit, reachable on this track exactly as on the other."""
    before = _load(tmp_path, "before", ONE_REVISION)
    after = _load(tmp_path, "after", ONE_REVISION.replace(
        '    lifecycle: published\n    ref: RULES-2026-07', "    lifecycle: draft"))

    errors = compare_to_baseline(after, before)

    assert any("un-freezes a published rules revision" in e for e in errors), errors


def test_the_gate_refuses_adding_an_artifact_to_a_published_revision(tmp_path):
    """Adding is refused for the same reason removing is: it changes, retroactively, what that
    revision published. A reader who recorded "validated against 2026-07" would find it now
    contains a file that was not there."""
    before = _load(tmp_path, "before", ONE_REVISION)
    after = _load(tmp_path, "after", ONE_REVISION.replace(
        "      - { name: demo.sch, role: schematron, from: repo, path: schematron/demo.sch }",
        "      - { name: demo.sch, role: schematron, from: repo, path: schematron/demo.sch }\n"
        "      - { name: extra.sch, role: schematron, from: repo, path: schematron/extra.sch }"))

    errors = compare_to_baseline(after, before)

    assert len(errors) == 1, errors
    assert "extra.sch" in errors[0] and "added to a published rules revision" in errors[0]


def test_the_gate_refuses_repointing_a_published_revision(tmp_path):
    before = _load(tmp_path, "before", ONE_REVISION)
    after = _load(tmp_path, "after", ONE_REVISION.replace("ref: RULES-2026-07", "ref: RULES-2026-08"))

    errors = compare_to_baseline(after, before)

    assert len(errors) == 1, errors
    assert "source repointed" in errors[0] and "'RULES-2026-07' -> 'RULES-2026-08'" in errors[0]


def test_moving_a_published_revision_to_another_major_line_reads_as_a_removal(tmp_path):
    """Its URL is its identity, so re-parenting it is not an edit to the same thing - the old
    address stops resolving. Matching on the revision label alone would have called this an
    ordinary field change and let it through."""
    two_lines = ONE_REVISION.replace(
        "major_lines: [ { major: 1, latest: 1.0.0 } ]",
        "major_lines: [ { major: 1, latest: 1.0.0 }, { major: 2, latest: 2.0.0 } ]",
    ).replace(
        "releases:\n",
        "releases:\n"
        "  - version: 2.0.0\n    lifecycle: published\n    ref: v2.0.0\n"
        "    artifacts:\n      - { name: demo.xsd, role: schema, from: repo, path: demo.xsd }\n",
    )
    before = _load(tmp_path, "before", two_lines)
    after = _load(tmp_path, "after", two_lines.replace(
        "    applies_to: 1", "    applies_to: 2").replace(
        "    tested_against: 1.0.0", "    tested_against: 2.0.0"))

    errors = compare_to_baseline(after, before)

    assert len(errors) == 1, errors
    assert "v1/schematron/2026-07" in errors[0] and "is now gone" in errors[0]


def test_the_gate_allows_a_draft_revision_to_change_freely(tmp_path):
    """Following a branch is an explicitly offered mode, so churn under it is not a violation."""
    draft = ONE_REVISION.replace('    lifecycle: published\n    ref: RULES-2026-07', "    lifecycle: draft")
    before = _load(tmp_path, "before", draft)
    after = _load(tmp_path, "after", draft.replace("path: schematron/demo.sch", "path: rules/demo.sch"))

    assert compare_to_baseline(after, before) == []


def test_a_published_revision_must_pin_its_own_ref(tmp_path):
    """Same rule as a release, and for the same reason: a revision whose directory is lost is
    rebuilt from the manifest alone, which is only the same bytes if the ref cannot move."""
    with pytest.raises(ManifestError, match="'ref' is a required property"):
        _load(tmp_path, "unpinned", ONE_REVISION.replace("    ref: RULES-2026-07\n", ""))


# --- validation of what only this track can get wrong ----------------------------------------

@pytest.mark.parametrize(
    "label",
    ["latest", "2026-7", "spring", "2026-07-1", "../etc", "2026-13", "2026-00", "2026-02-31"],
)
def test_a_revision_label_must_be_a_real_date(tmp_path, label):
    """Dated labels are what make "newest" decidable, which is what the moving pointer resolves
    by. `latest` is refused by the same rule, so a revision can never shadow the pointer, and so
    is anything that would escape the document root when joined onto it.

    The last three are the reason a shape check is not enough. `2026-13` is not a month, but it
    sorts above every real revision of 2026, so it would take the pointer and keep it until
    2027 - and a published revision can never be removed from the manifest, so neither the typo
    nor the URL it reserves can be taken back.
    """
    with pytest.raises(ManifestError):
        _load(tmp_path, "label", ONE_REVISION.replace('revision: "2026-07"', f'revision: "{label}"'))


def test_a_revision_must_belong_to_a_declared_major_line(tmp_path):
    with pytest.raises(ManifestError, match="no major_lines entry"):
        _load(tmp_path, "orphan", ONE_REVISION.replace("applies_to: 1", "applies_to: 9"))


def test_duplicate_revisions_are_refused(tmp_path):
    """Two entries for one URL. The second would overwrite the first's directory on every sync
    while both claimed to be frozen."""
    duplicated = ONE_REVISION + """
  - revision: "2026-07"
    applies_to: 1
    lifecycle: published
    ref: RULES-2026-07
    artifacts:
      - { name: other.sch, role: schematron, from: repo, path: schematron/other.sch }
"""
    with pytest.raises(ManifestError, match="duplicate rules revision"):
        _load(tmp_path, "dupe", duplicated)


def test_tested_against_must_name_a_release_of_this_standard(tmp_path):
    """It is displayed beside a checksum as a statement of fact about what was verified, and
    once the revision is published nothing may correct the page it is displayed on."""
    with pytest.raises(ManifestError, match="tested_against 9.9.9 is not a release"):
        _load(tmp_path, "typo", ONE_REVISION.replace("tested_against: 1.0.0", "tested_against: 9.9.9"))


def test_tested_against_must_be_in_the_line_the_revision_applies_to(tmp_path):
    two_lines = ONE_REVISION.replace(
        "major_lines: [ { major: 1, latest: 1.0.0 } ]",
        "major_lines: [ { major: 1, latest: 1.0.0 }, { major: 2, latest: 2.0.0 } ]",
    ).replace(
        "releases:\n",
        "releases:\n"
        "  - version: 2.0.0\n    lifecycle: published\n    ref: v2.0.0\n"
        "    artifacts:\n      - { name: demo.xsd, role: schema, from: repo, path: demo.xsd }\n",
    ).replace("tested_against: 1.0.0", "tested_against: 2.0.0")

    with pytest.raises(ManifestError, match="is in major line v2"):
        _load(tmp_path, "crossed", two_lines)


def test_a_published_revision_cannot_cite_a_draft_release(tmp_path):
    """A version number identifies bytes only once that version is frozen.

    A draft is re-fetched from a branch every cycle, so "tested against 1.1.0" names whatever
    1.1.0 happened to be that day. Published, the revision carries that claim permanently, on a
    page the write-once gate then refuses to let anyone correct.
    """
    with_draft = ONE_REVISION.replace(
        "releases:\n",
        "releases:\n"
        "  - version: 1.1.0\n    lifecycle: draft\n"
        "    artifacts:\n      - { name: demo.xsd, role: schema, from: repo, path: demo.xsd }\n",
    ).replace("tested_against: 1.0.0", "tested_against: 1.1.0")

    with pytest.raises(ManifestError, match="which is still a draft"):
        _load(tmp_path, "cites-draft", with_draft)


def test_a_draft_revision_may_cite_a_draft_release(tmp_path):
    """The other direction. Both are provisional and both move, so the claim is as good as the
    thing it describes - and refusing it would block the ordinary pre-release state, where the
    schema and its rules are being settled together."""
    both_draft = ONE_REVISION.replace(
        "releases:\n",
        "releases:\n"
        "  - version: 1.1.0\n    lifecycle: draft\n"
        "    artifacts:\n      - { name: demo.xsd, role: schema, from: repo, path: demo.xsd }\n",
    ).replace("tested_against: 1.0.0", "tested_against: 1.1.0").replace(
        "    lifecycle: published\n    ref: RULES-2026-07", "    lifecycle: draft")

    assert _one(tmp_path, both_draft).rule_set(1, "2026-07").tested_against == "1.1.0"


def test_a_revision_cannot_be_tested_below_its_own_stated_minimum(tmp_path):
    """Two fields that can only both be right in one order. Nothing else would notice, and the
    contradiction would be published permanently on the revision's page."""
    with_minimum = ONE_REVISION.replace(
        "releases:\n",
        "releases:\n"
        "  - version: 1.1.0\n    lifecycle: published\n    ref: v1.1.0\n"
        "    artifacts:\n      - { name: demo.xsd, role: schema, from: repo, path: demo.xsd }\n",
    ).replace("    tested_against: 1.0.0", "    tested_against: 1.0.0\n    minimum_version: 1.1.0")

    with pytest.raises(ManifestError, match="below its own stated minimum"):
        _load(tmp_path, "backwards", with_minimum)


def test_a_revision_artifact_may_not_collide_with_a_generated_file(tmp_path):
    """The artifact checks are shared with releases rather than restated, and this is what
    proves the sharing is real: a rules-only copy of the loop would have missed this."""
    with pytest.raises(ManifestError, match="cairn generates"):
        _load(tmp_path, "collide", ONE_REVISION.replace("name: demo.sch", "name: provenance.json"))


# --- the moving pointer ----------------------------------------------------------------------

def test_latest_resolves_to_the_newest_frozen_revision(tmp_path):
    newer = ONE_REVISION + """
  - revision: "2027-03"
    applies_to: 1
    lifecycle: published
    ref: RULES-2027-03
    artifacts:
      - { name: demo.sch, role: schematron, from: repo, path: schematron/demo.sch }
"""
    assert _one(tmp_path, newer).latest_rules(1).revision == "2027-03"


def test_latest_skips_a_draft_revision(tmp_path):
    """A draft follows a branch, and what `latest` resolves to is served under a dated path
    with a year-long immutable cache. Aiming the pointer documentation cites at moving bytes
    would have readers cache a draft as though it were frozen."""
    with_draft = ONE_REVISION + """
  - revision: "2027-03"
    applies_to: 1
    lifecycle: draft
    artifacts:
      - { name: demo.sch, role: schematron, from: repo, path: schematron/demo.sch }
"""
    assert _one(tmp_path, with_draft).latest_rules(1).revision == "2026-07"


def test_latest_skips_a_withdrawn_revision(tmp_path):
    """A withdrawn revision answers 410, so pointing the current-rules URL at it would resolve
    a live citation into a gone."""
    withdrawn = ONE_REVISION + """
  - revision: "2027-03"
    applies_to: 1
    lifecycle: published
    ref: RULES-2027-03
    served: false
    artifacts:
      - { name: demo.sch, role: schematron, from: repo, path: schematron/demo.sch }
"""
    assert _one(tmp_path, withdrawn).latest_rules(1).revision == "2026-07"


def test_a_line_whose_only_revision_is_a_draft_has_no_current_rules(tmp_path):
    """None is the right answer, not the oldest thing lying around: until a revision is frozen
    there is nothing anyone should be citing, and the draft is still reachable by its own date.
    """
    draft = ONE_REVISION.replace('    lifecycle: published\n    ref: RULES-2026-07', "    lifecycle: draft")
    assert _one(tmp_path, draft).latest_rules(1) is None


# --- routing -----------------------------------------------------------------------------------

def _routes(tmp_path: Path, body: str) -> str:
    return render_routes(_load(tmp_path, "routes", body))


def test_rules_routes_are_declared_before_the_pin_to_latest_redirect(tmp_path):
    """The trap this feature walked into, held open by a test rather than by memory.

    `location ~ "^/demo/v1/(.+)$"` matches `/demo/v1/schematron/2026-07/demo.sch` and sends it
    to `/demo/v1.0.0/schematron/...`, which nothing has ever served. nginx takes the first
    matching regex location in declaration order, so being emitted after it is not a lower
    priority, it is dead code.
    """
    routes = _routes(tmp_path, ONE_REVISION)

    rules_at = routes.index('location ~ "^/demo/v1/schematron')
    pin_at = routes.index('location ~ "^/demo/v1/(?<cairn_rest>')
    assert rules_at < pin_at, routes


def test_the_latest_pointer_is_a_redirect_and_not_a_directory(tmp_path):
    """Generated as a 303 rather than written into the document root, which is what keeps the
    store write-once: making `latest` mean something new never rewrites a published file."""
    routes = _routes(tmp_path, ONE_REVISION)

    assert "location = /demo/v1/schematron/latest { return 303 /demo/v1/schematron/2026-07; }" in routes
    assert 'return 303 /demo/v1/schematron/2026-07/$cairn_rules_rest' in routes


def test_a_withdrawn_revision_answers_410_before_anything_serves_it(tmp_path):
    """Withdrawing does not delete what was published, so the files are still on disk. Only
    declaration order stops them being served."""
    withdrawn = ONE_REVISION.replace("    ref: RULES-2026-07", "    ref: RULES-2026-07\n    served: false")
    routes = _routes(tmp_path, withdrawn)

    gone_at = routes.index('location ~ "^/demo/v1/schematron/2026-07(/.*)?$" { return 410; }')
    serve_at = routes.index('location ~ "^/demo/v1/schematron(/.*)?$"')
    assert gone_at < serve_at, routes


def test_a_standard_with_no_rules_generates_no_rules_routes(tmp_path):
    assert "schematron" not in _routes(tmp_path, RELEASES)


# --- replication and rendering -----------------------------------------------------------------

def _sync(tmp_path: Path, body: str, content: bytes, **kwargs):
    """Sync one workspace, re-reading an edited manifest in place on later calls.

    The same root every time, deliberately: a second cycle over a fresh document root would
    have nothing to contradict, which is the one situation in which every write-once guard is
    legitimately off.
    """
    root = tmp_path / "ws"
    manifest = root / "standards" / "demo" / "standard.yaml"
    if manifest.exists():
        manifest.write_text(body, encoding="utf-8")
        standards = load_all(root)
    else:
        standards = _load(tmp_path, "ws", body)
    with mock.patch("cairn.sync.http_client", lambda: FakeClient(content)):
        stats = sync_all(standards, root, log=lambda *a: None, **kwargs)
    return standards, stats


RULES_BYTES = b'<schema xmlns="http://purl.oclc.org/dsdl/schematron"/>\n'

# One live revision, one withdrawn, one draft: the three states a page has to tell apart.
WITHDRAWN_AND_LIVE = ONE_REVISION + """
  - revision: "2026-08"
    applies_to: 1
    lifecycle: published
    ref: RULES-2026-08
    served: false
    artifacts:
      - { name: demo.sch, role: schematron, from: repo, path: schematron/demo.sch }
  - revision: "2026-09"
    applies_to: 1
    lifecycle: draft
    artifacts:
      - { name: demo.sch, role: schematron, from: repo, path: schematron/demo.sch }
"""


def test_a_revision_is_replicated_and_frozen_like_a_release(tmp_path):
    standards, stats = _sync(tmp_path, ONE_REVISION, RULES_BYTES)
    served = site_dir(tmp_path / "ws") / "demo" / "v1" / "schematron" / "2026-07" / "demo.sch"

    assert served.read_bytes() == RULES_BYTES
    assert stats.published == 2, "the release and the revision each published once"

    record = json.loads((served.parent / "provenance.json").read_text())
    assert record["revision"] == "2026-07" and record["applies_to"] == 1
    assert record["tested_against"] == "1.0.0"
    assert record["artifacts"][0]["source"]["ref"] == "RULES-2026-07", "the record cites the pin"
    assert (served.parent / "SHA256SUMS").read_text().endswith("  demo.sch\n")


def test_a_frozen_revision_refuses_upstream_bytes_that_moved(tmp_path):
    """The guard that matters, reached through the real sync rather than asserted about it."""
    _sync(tmp_path, ONE_REVISION, RULES_BYTES)
    standards, stats = _sync(tmp_path, ONE_REVISION, b"different rules entirely\n", verify=True)

    served = site_dir(tmp_path / "ws") / "demo" / "v1" / "schematron" / "2026-07" / "demo.sch"
    assert served.read_bytes() == RULES_BYTES, "published rules were overwritten"
    assert any("FROZEN VERSION CHANGED" in message for _, message in stats.failures), stats.failures


def test_correcting_tested_against_rewrites_the_record(tmp_path):
    """A metadata field the comparison does not look at is one that stays wrong on disk for
    ever: nothing else would rewrite provenance.json for a frozen revision.
    """
    corrected = ONE_REVISION.replace(
        "releases:\n",
        "releases:\n"
        "  - version: 1.1.0\n    lifecycle: published\n    ref: v1.1.0\n"
        "    artifacts:\n      - { name: demo.xsd, role: schema, from: repo, path: demo.xsd }\n",
    )
    _sync(tmp_path, corrected, RULES_BYTES)
    _sync(tmp_path, corrected.replace("tested_against: 1.0.0", "tested_against: 1.1.0"), RULES_BYTES)

    record = json.loads(
        (site_dir(tmp_path / "ws") / "demo" / "v1" / "schematron" / "2026-07" / "provenance.json").read_text()
    )
    assert record["tested_against"] == "1.1.0"


def test_both_tracks_reach_the_pages_and_the_catalog(tmp_path):
    standards, _ = _sync(tmp_path, ONE_REVISION, RULES_BYTES)
    root = tmp_path / "ws"
    render_site(standards, root, log=lambda *a: None)
    site = site_dir(root)

    assert (site / "demo" / "v1" / "schematron" / "2026-07" / "index.html").is_file()

    namespace = (site / "demo" / "_ns" / "v1.xhtml").read_text()
    assert "/demo/v1/schematron/2026-07/demo.sch" in namespace, "the namespace doc indexes both tracks"

    catalog = json.loads((site / "catalog.json").read_text())["standards"][0]
    assert [r["revision"] for r in catalog["rules"]] == ["2026-07"]
    assert catalog["rules"][0]["tested_against"] == "1.0.0"
    assert catalog["major_lines"][0]["rules_latest"] == "2026-07"
    assert catalog["rules"][0]["artifacts"][0]["sha256"], "the catalog carries the checksum"

    sitemap = (site / "sitemap.xml").read_text()
    assert "/demo/v1/schematron/2026-07</loc>" in sitemap


def test_a_standard_with_no_rules_says_so_by_omission(tmp_path):
    """An absent key rather than a null one: a client cannot then read "there is a current
    rules URL, and it is null"."""
    standards, _ = _sync(tmp_path, RELEASES, b"<xs:schema/>\n")
    root = tmp_path / "ws"
    render_site(standards, root, log=lambda *a: None)

    catalog = json.loads((site_dir(root) / "catalog.json").read_text())["standards"][0]
    assert catalog["rules"] == []
    assert "rules_latest" not in catalog["major_lines"][0]


# --- what the pages may claim ------------------------------------------------------------------

def test_a_line_with_no_frozen_revision_does_not_advertise_the_latest_pointer(tmp_path):
    """The page must not hand a reader a URL that does not answer.

    `latest` resolves only to a published, served revision, and no route is generated until one
    exists - so on a line whose only revision is a draft, printing the pointer in the "how to
    reference these rules" panel offers a 404 to anyone who copies it.
    """
    draft = ONE_REVISION.replace('    lifecycle: published\n    ref: RULES-2026-07', "    lifecycle: draft")
    standards, _ = _sync(tmp_path, draft, RULES_BYTES)
    root = tmp_path / "ws"
    render_site(standards, root, log=lambda *a: None)

    page = (site_dir(root) / "demo" / "v1" / "schematron" / "2026-07" / "index.html").read_text()
    panels = re.findall(r'<div class="url-panel">([^<]*)</div>', page)

    assert panels, "the page stopped offering any URL at all"
    assert not any("/schematron/latest/" in p for p in panels), panels
    assert "schematron/latest" not in render_routes(standards), "no route backs that pointer"


def test_a_withdrawn_publication_is_not_advertised_to_crawlers(tmp_path):
    """A sitemap says a URL is worth indexing. A withdrawn publication answers 410, for as long
    as the history stays in the manifest - which is for ever - so listing it is a crawl error on
    every pass. It stays listed for a reader on the landing page, which is the right place for
    "this existed and is gone"."""
    standards, _ = _sync(tmp_path, WITHDRAWN_AND_LIVE, RULES_BYTES)
    root = tmp_path / "ws"
    render_site(standards, root, log=lambda *a: None)

    sitemap = (site_dir(root) / "sitemap.xml").read_text()

    assert "/demo/v1/schematron/2026-07<" in sitemap, "the live revision should be indexed"
    assert "/demo/v1/schematron/2026-08" not in sitemap, "a 410 was advertised to crawlers"


def test_the_namespace_document_only_asserts_resources_that_are_current(tmp_path):
    """The namespace document is machine-readable, and an `rddl:resource` with a
    normative-reference arcrole is a statement that the thing at that URL is a current,
    citable part of this namespace.

    A withdrawn revision answers 410 and a draft is re-fetched from a branch every cycle.
    Both stay in the human table, with their badge; neither may carry the assertion.
    """
    standards, _ = _sync(tmp_path, WITHDRAWN_AND_LIVE, RULES_BYTES)
    root = tmp_path / "ws"
    render_site(standards, root, log=lambda *a: None)

    namespace = (site_dir(root) / "demo" / "_ns" / "v1.xhtml").read_text()
    asserted = re.findall(r'<rddl:resource[^>]*xlink:href="([^"]*)"', namespace, re.S)

    assert "/demo/v1/schematron/2026-07/demo.sch" in asserted
    assert not any("2026-08" in href for href in asserted), "a 410 was asserted as a resource"
    assert not any("2026-09" in href for href in asserted), "a draft was asserted as a resource"
    # Still visible to a person, which is what the table is for.
    assert "/demo/v1/schematron/2026-08/demo.sch" in namespace
    assert "/demo/v1/schematron/2026-09/demo.sch" in namespace


def test_a_metadata_only_edit_does_not_reinstall_sha256sums(tmp_path):
    """`tested_against` is recorded but is not a checksum, so correcting it has to rewrite the
    record and has nothing to say about the sums.

    Rewriting both installs a new inode and moves the mtime of the file users are told to run
    `sha256sum -c` against, on a publication documented as permanent - the churn this writer
    exists to avoid, reached by the one edit that is allowed on a frozen publication.
    """
    with_versions = ONE_REVISION.replace(
        "releases:\n",
        "releases:\n"
        "  - version: 1.1.0\n    lifecycle: published\n    ref: v1.1.0\n"
        "    artifacts:\n      - { name: demo.xsd, role: schema, from: repo, path: demo.xsd }\n",
    )
    _sync(tmp_path, with_versions, RULES_BYTES)
    sums = site_dir(tmp_path / "ws") / "demo" / "v1" / "schematron" / "2026-07" / "SHA256SUMS"
    before = sums.stat().st_ino

    _sync(tmp_path, with_versions.replace("tested_against: 1.0.0", "tested_against: 1.1.0"), RULES_BYTES)

    record = json.loads((sums.parent / "provenance.json").read_text())
    assert record["tested_against"] == "1.1.0", "the record was not corrected"
    assert sums.stat().st_ino == before, "SHA256SUMS was reinstalled with identical bytes"
