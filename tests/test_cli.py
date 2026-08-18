"""What the command line promises to the things that run it.

Two callers read these results and act on them without a human in the loop. CI decides
whether a pull request may change published URLs from `cairn validate --baseline`, and
deploy/sync-loop.sh decides whether a verification actually happened from `cairn sync`'s exit
code. Both have had failure modes that reported success: a baseline compared against itself
passed unconditionally, and a sync killed halfway recorded itself as having verified
everything. A test on the layer below cannot see either, because in both cases the layer
below did exactly what it was asked.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import cairn.cli as cli_module
import cairn.sync as sync_module
from fakes import MANIFEST, FakeClient, workspace
from cairn.cli import (
    EXIT_ATTENTION,
    EXIT_INCOMPLETE,
    EXIT_NOTHING_SUCCEEDED,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_STANDARD_FAILED,
    main,
)
from cairn.config import SITE_DIRNAME, find_root, site_dir
from cairn.manifest import Lifecycle
from cairn.util import PUBLISHED_MODE, sha256_hex

ROOT = find_root(Path(__file__).resolve().parent)

UPSTREAM = b"<xs:schema/>\n"


def _workspace(
    tmp_path: Path, name: str, *, lifecycle: str = "published", manifest: str | None = None,
    also_draft: bool = False
) -> Path:
    body = manifest if manifest is not None else MANIFEST.replace("lifecycle: published", f"lifecycle: {lifecycle}")
    manifests = {"demo": body}
    if also_draft:
        # A draft tracks its branch, so it survives an upstream change that fails the frozen
        # one. That is what makes a partial failure reachable with a single fake upstream.
        # A published release must pin its own ref, so dropping to draft drops the pin too.
        manifests["other"] = (
            MANIFEST.replace("id: demo", "id: other")
            .replace("lifecycle: published\n    ref: v1.0.0", "lifecycle: draft")
        )
    return workspace(tmp_path / name, manifests)


def _sync(root: Path, *args, upstream: bytes = UPSTREAM) -> int:
    with mock.patch.object(sync_module, "http_client", lambda: FakeClient(upstream)):
        return main(["sync", "--root", str(root), *args])


def _publish(root: Path, **kwargs) -> None:
    """The first sync of a published release is its publication. It is reported and exits 0.

    Reported, because a release that has never written to this volume has no record and no
    bytes to contradict, so the write-once guards do not apply to that one cycle - and that is
    indistinguishable from a release whose directory was deleted.

    Exit 0, because the manifest asked for it. Raising the exit code broke the image build
    stage, which runs `cairn validate && cairn all` against an empty /work/site where every
    published release publishes: the moment any standard went to `lifecycle: published`, no
    image could be built again.
    """
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = _sync(root, **kwargs)
    assert rc == EXIT_OK, f"a publication must not raise the exit code (got {rc})"
    assert "VERSION PUBLISHED" in err.getvalue(), "a publication must still be reported"


def _served(root: Path) -> Path:
    return site_dir(root) / "demo" / "v1.0.0" / "demo.xsd"


# --- the write-once gate -----------------------------------------------------------------

def test_baseline_pointing_at_the_workspace_itself_is_rejected(tmp_path):
    """Comparing manifests against themselves can never fail, so it must not be allowed."""
    root = _workspace(tmp_path, "current")
    assert main(["validate", "--root", str(root), "--baseline", str(root)]) == EXIT_REFUSED


def test_baseline_inside_the_workspace_is_rejected(tmp_path):
    """find_root walks upward, so a directory under the repo resolves back to the repo.

    This is the shape a CI checkout takes when its output lands somewhere unexpected, and it
    turned the whole gate into an unconditional pass.
    """
    root = _workspace(tmp_path, "current")
    inside = root / "baseline-checkout"
    inside.mkdir()
    assert main(["validate", "--root", str(root), "--baseline", str(inside)]) == EXIT_REFUSED


def test_baseline_with_no_standards_is_rejected(tmp_path):
    """An empty baseline compares clean against everything, which is the same failure."""
    root = _workspace(tmp_path, "current")
    empty = _workspace(tmp_path, "empty")
    for manifest in (empty / "standards").glob("*/standard.yaml"):
        manifest.unlink()
    assert main(["validate", "--root", str(root), "--baseline", str(empty)]) == EXIT_REFUSED


def test_baseline_from_a_separate_checkout_passes(tmp_path):
    """The shape CI actually uses: a git worktree of the base branch beside the workspace."""
    root = _workspace(tmp_path, "current")
    baseline = _workspace(tmp_path, "baseline")
    assert main(["validate", "--root", str(root), "--baseline", str(baseline)]) == EXIT_OK


def test_baseline_still_catches_a_real_violation(tmp_path):
    """The guards above must not have made the gate unreachable."""
    root = _workspace(tmp_path, "current", lifecycle=Lifecycle.DRAFT)
    baseline = _workspace(tmp_path, "baseline", lifecycle=Lifecycle.PUBLISHED)
    assert main(["validate", "--root", str(root), "--baseline", str(baseline)]) == EXIT_REFUSED


# --- sync exit codes ---------------------------------------------------------------------

def test_sync_exits_zero_when_everything_is_current(tmp_path):
    root = _workspace(tmp_path, "ws")
    _publish(root)
    assert _sync(root) == EXIT_OK  # second pass: frozen, nothing to do


def test_restored_corruption_does_not_exit_zero(tmp_path, capsys):
    """Silent repair is the failure here: the loop would log a clean cycle and alert nobody."""
    root = _workspace(tmp_path, "ws")
    _publish(root)
    _served(root).write_bytes(b"CORRUPT")

    assert _sync(root, "--verify") == EXIT_ATTENTION
    assert _served(root).read_bytes() == UPSTREAM
    assert "INTEGRITY CHECK FAILED" in capsys.readouterr().err


def test_a_permission_repair_that_fails_does_not_exit_zero(tmp_path, capsys):
    """A published file stuck unreadable answers 403 forever, so it cannot pass silently."""
    root = _workspace(tmp_path, "ws")
    _publish(root)
    _served(root).chmod(0o600)

    real_chmod = Path.chmod

    def refuse(self, mode, **kwargs):
        if self.name == "demo.xsd":
            raise PermissionError(1, "Operation not permitted")
        return real_chmod(self, mode, **kwargs)

    with mock.patch.object(Path, "chmod", refuse):
        assert _sync(root) == EXIT_ATTENTION
    assert "PERMISSION REPAIR FAILED" in capsys.readouterr().err


def test_a_repairable_permission_exits_zero(tmp_path):
    """The counterpart: a mode we can fix is housekeeping, not an operator event."""
    root = _workspace(tmp_path, "ws")
    _publish(root)
    _served(root).chmod(0o600)

    assert _sync(root) == EXIT_OK
    assert _served(root).stat().st_mode & 0o777 == PUBLISHED_MODE


def test_a_failed_standard_has_its_own_exit_code(tmp_path, capsys):
    """Distinct from EXIT_ATTENTION, because the site is not current in this one."""
    root = _workspace(tmp_path, "ws", also_draft=True)
    _publish(root)

    assert _sync(root, "--verify", upstream=b"RETAGGED") == EXIT_STANDARD_FAILED
    err = capsys.readouterr().err
    assert "FROZEN VERSION CHANGED" in err
    assert "SyncError" not in err  # the marker is the message, not its exception class


def test_a_repair_does_not_downgrade_a_failed_standard(tmp_path, capsys):
    """The runbook said either repair marker "means the run exits 3". That is false whenever a
    standard also failed: the failure branch is reached first and returns 4. An operator
    following that row would conclude such a run contained no standard failure at all.
    """
    root = _workspace(tmp_path, "ws", also_draft=True)
    _publish(root)
    # `other` is a draft, so its damaged record is rebuilt and counted; `demo` is frozen and
    # refuses the changed upstream. Both happen in the one run.
    (site_dir(root) / "other" / "v1.0.0" / "provenance.json").write_bytes(b"\xff\xfe rotted\n")

    assert _sync(root, "--verify", upstream=b"RETAGGED") == EXIT_STANDARD_FAILED

    captured = capsys.readouterr()
    assert "DAMAGED RECORD(S) REBUILT" in captured.out, captured.out
    assert "FROZEN VERSION CHANGED" in captured.err


def test_a_run_where_nothing_succeeded_is_not_a_verification(tmp_path):
    """The stamp records that a verify pass covered the corpus. If every standard failed it
    covered none, and stamping it would suppress the next attempt for a full interval - the
    original stamp bug arriving from the other direction."""
    root = _workspace(tmp_path, "ws")
    _publish(root)

    assert _sync(root, "--verify", upstream=b"RETAGGED") == EXIT_NOTHING_SUCCEEDED


def test_exit_codes_are_distinct():
    """sync-loop.sh branches on these, and mirrors the numbers. They cannot collide."""
    assert len({EXIT_OK, EXIT_INCOMPLETE, EXIT_ATTENTION, EXIT_STANDARD_FAILED, EXIT_NOTHING_SUCCEEDED}) == 5
    assert 2 not in {EXIT_ATTENTION, EXIT_STANDARD_FAILED}  # argparse owns 2


def test_sync_loop_reads_the_exit_codes_rather_than_restating_them():
    """The loop branches on cairn's codes. It used to hold its own copy of the numbers, and a
    third copy in the docs had already drifted; nothing keeps two restatements honest."""
    script = (ROOT / "deploy" / "sync-loop.sh").read_text(encoding="utf-8")

    assert "cairn exit-codes" in script
    hardcoded = re.findall(r"^SYNC_RC_\w+=\d+", script, re.MULTILINE)
    assert not hardcoded, f"the shell is restating exit codes instead of sourcing them: {hardcoded}"


def test_exit_codes_subcommand_emits_valid_shell():
    """It is eval'd by /bin/sh at container start, so a malformed line stops the syncer."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["exit-codes"]) == EXIT_OK

    emitted = dict(line.split("=") for line in out.getvalue().split())
    assert emitted == {
        "SYNC_RC_ATTENTION": str(EXIT_ATTENTION),
        "SYNC_RC_FAILED": str(EXIT_STANDARD_FAILED),
        "SYNC_RC_NOTHING_SUCCEEDED": str(EXIT_NOTHING_SUCCEEDED),
        # Same number as its sync counterpart, emitted separately because the loop consumes
        # the two at different call sites. Without it the shell had no way to tell a build
        # that reported something from a build that produced nothing, and called both a
        # failure.
        "BUILD_RC_ATTENTION": str(EXIT_ATTENTION),
    }


def test_the_shell_loop_uses_every_name_the_exit_codes_command_emits():
    """A name emitted and never consumed is dead weight; a name consumed and never emitted is
    an unbound variable under `set -u`, which aborts the cycle midway rather than at start."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["exit-codes"])
    emitted = [line.split("=")[0] for line in out.getvalue().split()]

    script = (ROOT / "deploy" / "sync-loop.sh").read_text(encoding="utf-8")
    unused = [name for name in emitted if script.count(name) < 2]
    assert not unused, f"cairn emits {unused}, which deploy/sync-loop.sh never branches on"


def test_the_documented_tag_is_one_that_exists():
    """CI copies .env.example, which proves compose can parse the value and nothing more. A
    version pinned here that has no matching git tag has no images in GHCR either, so the
    documented deploy path fails with "manifest unknown" - which is the state this file was
    in when it pinned the in-development version rather than a released one."""
    tag = re.search(r"^CAIRN_TAG=(.+)$", (ROOT / "deploy" / ".env.example").read_text(), re.MULTILINE)
    assert tag, "deploy/.env.example must set CAIRN_TAG; the ghcr overlay cannot parse without it"
    value = tag.group(1).strip()
    if value == "latest":
        return  # always published from the default branch

    tags = subprocess.run(
        ["git", "tag", "--list"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if tags.returncode != 0:
        pytest.skip("not a git checkout")
    released = {line.strip().lstrip("v") for line in tags.stdout.splitlines() if line.strip()}
    assert value in released or value in {t.rsplit(".", 1)[0] for t in released}, (
        f"deploy/.env.example points operators at {value}, which has no matching git tag and "
        f"therefore no images in GHCR. Use `latest` until v{value} is tagged and published."
    )


# --- build ------------------------------------------------------------------------------

def test_build_records_what_sync_replicated(tmp_path):
    """`cairn all` renders from provenance, so a sync failure must not erase the pages."""
    root = _workspace(tmp_path, "ws")
    _publish(root)
    assert main(["build", "--root", str(root)]) == EXIT_OK

    prov = json.loads((site_dir(root) / "demo" / "v1.0.0" / "provenance.json").read_text())
    assert prov["artifacts"][0]["sha256"] == sha256_hex(UPSTREAM)
    assert (site_dir(root) / "demo" / "index.html").is_file()


def test_the_build_reports_the_directory_it_actually_wrote_to(tmp_path, monkeypatch, capsys):
    """The container sets CAIRN_SITE_DIR, and this line restated the layout instead of asking
    site_dir(). Every cycle logged "Built site into /app/site" for a render that had written to
    /data/site, which is exactly the wrong thing to be reading when a deployment looks stale."""
    root = _workspace(tmp_path, "ws")
    elsewhere = tmp_path / "mounted-volume"
    monkeypatch.setenv("CAIRN_SITE_DIR", str(elsewhere))

    assert main(["build", "--root", str(root)]) == EXIT_OK

    out = capsys.readouterr().out
    assert (elsewhere / "index.html").is_file(), "the render did not honour CAIRN_SITE_DIR"
    assert str(elsewhere) in out
    assert str(root / SITE_DIRNAME) not in out, "the log named a directory nothing was written to"


def test_an_empty_workspace_does_not_unpublish_the_registry(tmp_path):
    """A partial checkout or an interrupted pull looks exactly like an empty workspace, and
    the build renders that faithfully: an index listing nothing and a routes file holding
    only its header, which drops every namespace document, 303 and 410 on nginx's reload."""
    root = _workspace(tmp_path, "ws")
    for manifest in (root / "standards").glob("*/standard.yaml"):
        manifest.unlink()

    routes = root / "build" / "nginx" / "cairn-routes.conf"
    routes.parent.mkdir(parents=True)
    routes.write_text("location = /demo/v1 { return 303 /demo/v1.0.0; }\n")
    site = site_dir(root)
    site.mkdir()
    (site / "index.html").write_text("<h1>the registry</h1>")

    assert main(["validate", "--root", str(root)]) == EXIT_INCOMPLETE
    assert main(["build", "--root", str(root)]) == EXIT_INCOMPLETE

    assert "303" in routes.read_text(), "the routes file was replaced from an empty registry"
    assert "the registry" in (site / "index.html").read_text()


def test_a_build_reads_each_provenance_record_once(tmp_path):
    """Three call sites want the same record - the release page context, the namespace document
    of a major line's latest release, and the catalog - so every build opened, decoded and
    JSON-parsed each file three times over."""
    import cairn.render as render_module

    root = _workspace(tmp_path, "ws")
    _publish(root)

    real_read = render_module.read_text
    reads = []

    def counting_read(path):
        if path.name == "provenance.json":
            reads.append(path)
        return real_read(path)

    with mock.patch.object(render_module, "read_text", counting_read):
        assert main(["build", "--root", str(root)]) == EXIT_OK

    assert len(reads) == 1, f"provenance.json was read {len(reads)} times for one release"


def test_a_second_build_sees_records_the_sync_rewrote(tmp_path):
    """The memo is per build, not per process. A build runs immediately after a sync that has
    just rewritten these records, so a process-lifetime cache would render the previous
    cycle's checksums onto the pages."""
    root = _workspace(tmp_path, "ws", lifecycle=Lifecycle.DRAFT)
    assert _sync(root) == EXIT_OK
    assert main(["build", "--root", str(root)]) == EXIT_OK

    moved = b"<xs:schema>upstream moved on</xs:schema>\n"
    assert _sync(root, upstream=moved) == EXIT_OK
    assert main(["build", "--root", str(root)]) == EXIT_OK

    catalog = json.loads((site_dir(root) / "catalog.json").read_text())
    published = catalog["standards"][0]["releases"][0]["artifacts"][0]["sha256"]
    assert published == sha256_hex(moved), "the build served a cached record from the previous cycle"


def test_a_build_survives_damage_to_one_release(tmp_path):
    """The render must survive anything the sync refuses. A damaged record used to raise out
    of `cairn build`, which stops the cycle, so one rotted file froze every healthy standard's
    pages, routes and 410s at their last state."""
    root = _workspace(tmp_path, "ws")
    _publish(root)
    (site_dir(root) / "demo" / "v1.0.0" / "provenance.json").write_bytes(b"\xff\xfe rotted\n")

    assert main(["build", "--root", str(root)]) == EXIT_OK
    assert (site_dir(root) / "demo" / "index.html").is_file()



def test_a_build_survives_unreadable_prose_but_reports_it(tmp_path, capsys):
    """Optional prose is decoration and must not stop the build, but a page quietly losing
    its overview is the same silence the sync's repair counters exist to end."""
    root = _workspace(tmp_path, "ws")
    content = root / "standards" / "demo" / "content"
    content.mkdir()
    (content / "overview.md").write_bytes(b"\xff\xfe not utf-8\n")

    assert main(["build", "--root", str(root)]) == EXIT_ATTENTION
    captured = capsys.readouterr()
    assert "[WARN]" in captured.out
    assert "CONTENT UNREADABLE" in captured.err
    assert (site_dir(root) / "demo" / "index.html").is_file()


def test_a_build_survives_a_malformed_provenance_shape(tmp_path):
    """Valid JSON of the wrong shape raised TypeError from inside the page loop."""
    root = _workspace(tmp_path, "ws")
    _publish(root)
    (site_dir(root) / "demo" / "v1.0.0" / "provenance.json").write_text('{"artifacts": [1, 2, 3]}')

    assert main(["build", "--root", str(root)]) == EXIT_OK


def test_a_filter_matching_nothing_is_not_success(tmp_path):
    """`--standard <typo>` did no work and reported "Done: 0 fetched", which a script reads
    as success. A mistyped or renamed id is the likely cause."""
    root = _workspace(tmp_path, "ws")
    assert _sync(root, "--standard", "does-not-exist") == EXIT_INCOMPLETE


def test_a_dry_run_fails_when_an_upstream_is_unreachable(tmp_path):
    """CI has a step named 'Dry-run sync (upstream reachability)'. Logging UNREACHABLE and
    exiting 0 made it a gate that cannot fail, which reports green while every source is dead."""

    class Dead(FakeClient):
        def get(self, url, headers=None):
            response = super().get(url)
            response.status_code = 404
            return response

        def head(self, url):
            response = super().head(url)
            response.status_code = 404
            return response

    root = _workspace(tmp_path, "ws")
    with mock.patch.object(sync_module, "http_client", lambda: Dead(b"")):
        assert main(["sync", "--root", str(root), "--dry-run"]) != EXIT_OK


def test_an_io_failure_in_the_render_is_not_a_traceback(tmp_path, capsys):
    """A full or read-only shared volume is the likeliest build failure, and OSError is not
    what main() catches, so it escaped as a traceback and took the sync's exit code with it."""
    root = _workspace(tmp_path, "ws")

    with mock.patch("cairn.render.write_routes", side_effect=OSError(28, "No space left on device")):
        assert main(["build", "--root", str(root)]) == EXIT_INCOMPLETE

    assert "could not be rendered" in capsys.readouterr().err


def test_a_dry_run_writes_nothing_even_through_cairn_all(tmp_path):
    """--dry-run is documented as "without writing", and it is the flag an operator reaches for
    to inspect a live volume safely. `cairn all` passed it to the sync and then ran the render
    unconditionally, rewriting every page, the catalog, the error pages and the generated nginx
    routes file - and exiting 0."""
    root = _workspace(tmp_path, "ws")

    with mock.patch.object(sync_module, "http_client", lambda: FakeClient(UPSTREAM)):
        assert main(["all", "--dry-run", "--root", str(root)]) == EXIT_OK

    assert not site_dir(root).exists(), sorted(p.name for p in site_dir(root).iterdir())
    assert not (root / "build").exists(), "the generated nginx routes file was written"


def test_a_dry_run_still_reports_an_unreachable_upstream_through_cairn_all(tmp_path):
    """Skipping the render must not have skipped the gate."""
    class Dead(FakeClient):
        def head(self, url):
            response = super().head(url)
            response.status_code = 404
            return response

    root = _workspace(tmp_path, "ws")
    with mock.patch.object(sync_module, "http_client", lambda: Dead(b"")):
        assert main(["all", "--dry-run", "--root", str(root)]) != EXIT_OK


def test_a_failed_build_outranks_the_sync_result(tmp_path):
    """`cairn all` composes the two, and a cycle that produced nothing to serve outranks
    whatever the sync found, because the sync's codes all describe a run that finished."""
    root = _workspace(tmp_path, "ws")
    _publish(root)
    _served(root).write_bytes(b"CORRUPT")  # would make the sync return EXIT_ATTENTION

    with mock.patch.object(sync_module, "http_client", lambda: FakeClient(UPSTREAM)), \
         mock.patch("cairn.render.write_routes", side_effect=OSError(28, "No space left on device")):
        assert main(["all", "--root", str(root), "--verify"]) == EXIT_INCOMPLETE


def test_cairn_all_parses_each_manifest_once(tmp_path):
    """cmd_sync and cmd_build each loaded and JSON-Schema-validated every manifest for
    themselves, so a syncer cycle did the whole job twice. The cost is the smaller half: if the
    repo were pulled between the two, the render would describe a different registry from the
    one that had just been replicated."""
    root = _workspace(tmp_path, "ws")
    loads = []
    real_load = cli_module._load

    def counting_load(*a, **kw):
        loads.append(a[0])
        return real_load(*a, **kw)

    with mock.patch.object(cli_module, "_load", counting_load):
        with mock.patch.object(sync_module, "http_client", lambda: FakeClient(UPSTREAM)):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                main(["all", "--root", str(root)])

    assert len(loads) == 1, f"the manifests were loaded {len(loads)} times in one cycle"
