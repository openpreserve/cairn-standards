"""Command-line entry point: `cairn {validate,sync,build,all}`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import STANDARDS_DIRNAME, find_root, site_dir
from .manifest import ManifestError, compare_to_baseline, load_all
from .markers import Marker
from .render import render_site
from .sync import SyncError, sync_all


# Exit codes for `cairn sync`, mirrored in deploy/sync-loop.sh. That loop has to tell a run
# which completed and found something from a run that died partway, because it cannot
# conclude anything about the artifacts a dead run never reached: it records a verification
# as done on the strength of this, and recording one that crashed suppresses the next for a
# full interval. 1 stays "did not complete", which is what an unhandled fault exits with too;
# 2 is argparse's usage error and is deliberately skipped.
EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_ATTENTION = 3       # ran to the end; something happened an operator must see
EXIT_STANDARD_FAILED = 4  # ran to the end; one or more standards failed
EXIT_NOTHING_SUCCEEDED = 5  # ran to the end; every release failed, so nothing was checked

# `cairn validate` is a gate rather than a daemon step: it passes or it refuses, and a
# refusal is a finding, not a crash. It shares 1 because both mean "do not proceed" and
# nothing branches on the difference, but the distinction belongs in the name: reading the
# sync contract onto validate would classify every genuine write-once violation as a crash.
EXIT_REFUSED = 1


def _add_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Workspace root (defaults to the nearest dir containing standards/ and schemas/).",
    )


def _resolve_root(args) -> Path:
    return find_root(args.root)


# An empty manifest set is never "nothing to do", but what it costs depends on which set it
# is, so the consequence travels with the call rather than being restated at each site.
_EMPTY_WORKSPACE = (
    "  Refusing to continue: building from an empty set would unpublish every URL\n"
    "  this service serves. If the workspace is genuinely empty, that is the bug;\n"
    "  check the checkout completed and that --root points at the right directory."
)

_EMPTY_BASELINE = (
    "  Nothing could be compared, so this is a failure rather than a pass: an empty\n"
    "  baseline compares clean against everything, which turns the write-once gate into\n"
    "  an unconditional pass. A mistyped path, or a checkout step that silently produced\n"
    "  nothing, both look exactly like this."
)


def _load(root: Path, consequence: str = _EMPTY_WORKSPACE):
    """Load every manifest, refusing to proceed with none.

    An empty set is not "nothing to do", it is a registry with no standards in it, and the
    build renders exactly that: an index listing nothing and an nginx routes file containing
    only its header, which unpublishes every namespace document, pin-to-latest redirect and
    410 the moment nginx reloads. A partial checkout or a pull interrupted midway looks
    precisely like this, so it has to fail where it is noticed rather than where it is served.

    The baseline of a write-once check goes through here too, for the same reason phrased the
    other way round. main() maps the refusal to the same code cmd_validate returns for one of
    its own, which is what makes routing it through here rather than a second hand-rolled copy
    behaviour-preserving.
    """
    standards = load_all(root)
    if not standards:
        raise ManifestError(f"no standards found under {root}/{STANDARDS_DIRNAME}.\n{consequence}")
    return standards


def cmd_validate(args) -> int:
    root = _resolve_root(args)
    standards = _load(root)
    for std in standards:
        n_rel = len(std.releases)
        n_art = sum(len(r.artifacts) for r in std.releases)
        print(f"  ✓ {std.id}: {std.title} - {n_rel} release(s), {n_art} artifact(s)")

    if args.baseline:
        baseline_root = find_root(args.baseline)
        # find_root walks *upward* looking for a workspace, so a baseline path that is not one
        # itself resolves to the nearest enclosing workspace - which, for anything checked out
        # under the repo, is the repo. The manifests would then be compared against themselves
        # and pass unconditionally. A gate that cannot fail is worse than no gate, because CI
        # reports it green.
        if baseline_root == root:
            sys.stdout.flush()
            print(
                f"ERROR: the baseline at {args.baseline} resolves to the workspace being checked ({root}).\n"
                f"  Comparing the manifests against themselves can never report a violation.\n"
                f"  Point --baseline at a separate checkout containing standards/ and schemas/.",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        sys.stdout.flush()  # a refusal below goes to stderr; keep it under the lines above
        breaks = compare_to_baseline(standards, _load(baseline_root, _EMPTY_BASELINE))
        if breaks:
            print(
                f"\n{Marker.WRITE_ONCE_VIOLATION} - {len(breaks)} problem(s) against {baseline_root}:",
                file=sys.stderr,
            )
            for line in breaks:
                print(f"  - {line}", file=sys.stderr)
            print(
                "\nThese edits would change or remove URLs that are already published.",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        print(f"  ✓ no write-once violations against {baseline_root}")

    print(f"OK - {len(standards)} manifest(s) valid.")
    return EXIT_OK


def cmd_sync(args) -> int:
    root = _resolve_root(args)
    standards = _load(root)

    # Checked before any work, and per id rather than in aggregate. Asking only whether
    # *anything* matched let a mistyped id alongside a valid one be dropped in silence, which
    # is exactly the renamed-standard case this is here to catch: a runbook step naming three
    # standards quietly stops covering one of them and still exits 0.
    if args.standard:
        unknown = sorted(set(args.standard) - {std.id for std in standards})
        if unknown:
            print(
                f"ERROR: no standard with id {', '.join(unknown)}.\n"
                f"  Nothing was synced. Known ids: {', '.join(sorted(std.id for std in standards))}.",
                file=sys.stderr,
            )
            return EXIT_INCOMPLETE

    stats = sync_all(
        standards,
        root,
        only=args.standard or None,
        verify=args.verify,
        dry_run=args.dry_run,
        log=print,
    )
    if args.dry_run:
        print(f"Dry run: {stats.planned} artifact(s) resolved, {stats.unreachable} unreachable.")
    else:
        summary = f"Done: {stats.fetched} fetched, {stats.verified} verified, {stats.skipped} frozen/skipped"
        if stats.repaired:
            summary += f", {stats.repaired} permission(s) repaired"
        if stats.restored:
            summary += f", {stats.restored} {Marker.CORRUPTED_FILES_RESTORED}"
        if stats.recovered:
            summary += f", {stats.recovered} {Marker.DAMAGED_RECORDS_REBUILT}"
        print(summary + ".")

    # Every operator-facing marker is emitted here rather than by the caller, so the strings
    # the runbooks tell people to alert on have one source. deploy/sync-loop.sh relays them.
    if stats.restored:
        print(
            f"\n{Marker.INTEGRITY_CHECK_FAILED}: {stats.restored} served file(s) were missing, or did not match\n"
            f"  their recorded checksum, and were restored from upstream. The published bytes are\n"
            f"  correct again, but nothing on that volume should be trusted until you know why.",
            file=sys.stderr,
        )
    if stats.recovered:
        print(
            f"\n{Marker.INTEGRITY_CHECK_FAILED}: {stats.recovered} release(s) had unreadable provenance and were\n"
            f"  rebuilt from upstream. Only drafts are rebuilt this way, so no published URL was\n"
            f"  affected, but that metadata was damaged on the volume.",
            file=sys.stderr,
        )
    # A publication is the one cycle on which a release's write-once guards do not apply.
    # Reported rather than silent because the same line covers a published release whose
    # directory was lost and has just been rebuilt from its pinned ref, and an operator who
    # promoted nothing this cycle needs to know the volume lost a release.
    if stats.published:
        print(
            f"\n{Marker.VERSION_PUBLISHED}: {stats.published} release(s) published bytes for the first\n"
            f"  time this cycle, so the write-once checks did not apply to them. If you promoted\n"
            f"  nothing, the release directory was lost and has been rebuilt from its pinned ref.",
            file=sys.stderr,
        )
    if stats.unreadable:
        print(
            f"\n{Marker.PERMISSION_REPAIR_FAILED}: {stats.unreadable} published file(s) cannot be read by the\n"
            f"  web server and the mode could not be changed. Those URLs answer 403 until it is fixed.",
            file=sys.stderr,
        )

    if not stats.ok:
        print(f"\n{len(stats.failures)} standard(s) FAILED:", file=sys.stderr)
        for std_id, message in stats.failures:
            print(f"\n--- {std_id} ---\n{message}", file=sys.stderr)
        if args.verify:
            print(
                f"\n{Marker.INTEGRITY_CHECK_FAILED}: {len(stats.failures)} standard(s) could not be verified.",
                file=sys.stderr,
            )
        return EXIT_NOTHING_SUCCEEDED if stats.nothing_succeeded else EXIT_STANDARD_FAILED
    if stats.restored or stats.recovered or stats.unreadable or stats.published:
        return EXIT_ATTENTION
    return EXIT_OK


def cmd_exit_codes(args) -> int:
    """Print the exit codes as shell assignments for deploy/sync-loop.sh to source.

    The loop branches on these, so it used to restate the numbers, and a third copy in the
    docs had drifted by the time it was written. Sourcing them means the shell cannot hold a
    different opinion from the code it is running.

    BUILD_RC_ATTENTION is the same number as its sync counterpart and is emitted separately
    anyway, because the loop consumes the two at different call sites and treating "the build
    reported something" as "the build failed" is not a distinction it can make from the number
    alone. It did exactly that, and logged Marker.BUILD_FAILED - which the runbook defines as
    the site serving its previous state - for a page that had merely fallen back to its
    summary.
    """
    print(f"SYNC_RC_ATTENTION={EXIT_ATTENTION}")
    print(f"SYNC_RC_FAILED={EXIT_STANDARD_FAILED}")
    print(f"SYNC_RC_NOTHING_SUCCEEDED={EXIT_NOTHING_SUCCEEDED}")
    print(f"BUILD_RC_ATTENTION={EXIT_ATTENTION}")
    return EXIT_OK


def cmd_build(args) -> int:
    root = _resolve_root(args)
    standards = _load(root)
    # I/O on the shared volumes is the likely build failure - full disk, read-only mount -
    # and OSError is not what main() catches, so it escaped as a traceback and took the
    # sync's exit code with it when the two were composed by `cairn all`.
    try:
        degraded = render_site(standards, root, log=print)
    except OSError as exc:
        print(f"\nERROR: the site could not be rendered: {exc}", file=sys.stderr)
        return EXIT_INCOMPLETE
    # site_dir(), not root/"site". The container sets CAIRN_SITE_DIR, so restating the layout
    # here logged /app/site on every cycle for a render that had written to /data/site.
    print(f"Built site into {site_dir(root)}")
    if degraded:
        print(
            f"\n{Marker.CONTENT_UNREADABLE}: {degraded} page(s) fell back to their one-line summary because\n"
            f"  prose beside the manifest could not be read. The pages are live but incomplete.",
            file=sys.stderr,
        )
        return EXIT_ATTENTION
    return EXIT_OK


def cmd_all(args) -> int:
    # Render even when a standard failed to sync. The standards that did sync are on disk and
    # should reach the site; withholding the render because of an unrelated failure would
    # freeze the whole site at its last good state. The non-zero exit still propagates.
    sync_rc = cmd_sync(args)
    # --dry-run is documented as "resolve + check reachability without writing", and the render
    # is the step that writes most: every page, the catalog, the sitemap, the error pages, the
    # assets and the generated nginx routes file. Running it anyway meant the one flag an
    # operator reaches for to inspect a live volume safely rewrote the whole document root.
    if args.dry_run:
        return sync_rc
    build_rc = cmd_build(args)
    # A build that produced nothing to serve outranks whatever the sync found, because the
    # sync's codes all describe a run that finished. This is now a real ranking: cmd_build
    # returns EXIT_INCOMPLETE for an I/O failure rather than letting it escape as a traceback.
    if build_rc == EXIT_INCOMPLETE:
        return build_rc
    return sync_rc or build_rc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cairn", description="Durable hosting for preservation standards.")
    parser.add_argument("--version", action="version", version=f"cairn {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_val = sub.add_parser("validate", help="Validate all manifests.")
    _add_root(p_val)
    p_val.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Workspace to compare against (e.g. a checkout of main). Fails if the current "
        "manifests remove or repoint anything already published.",
    )
    p_val.set_defaults(func=cmd_validate)

    p_sync = sub.add_parser("sync", help="Replicate + checksum upstream artifacts.")
    _add_root(p_sync)
    p_sync.add_argument("--standard", action="append", help="Only this standard id (repeatable).")
    p_sync.add_argument("--verify", action="store_true", help="Re-fetch frozen versions and fail on checksum drift.")
    p_sync.add_argument("--dry-run", action="store_true", help="Resolve + check reachability without writing.")
    p_sync.set_defaults(func=cmd_sync)

    sub.add_parser(
        "exit-codes", help="Print the sync exit codes as shell assignments (used by deploy/sync-loop.sh)."
    ).set_defaults(func=cmd_exit_codes)

    p_build = sub.add_parser("build", help="Render pages, RDDL, catalog, and nginx routes.")
    _add_root(p_build)
    p_build.set_defaults(func=cmd_build)

    p_all = sub.add_parser("all", help="sync then build.")
    _add_root(p_all)
    p_all.add_argument("--standard", action="append", help="Only this standard id (repeatable).")
    p_all.add_argument("--verify", action="store_true")
    p_all.add_argument("--dry-run", action="store_true")
    p_all.set_defaults(func=cmd_all)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ManifestError, SyncError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return EXIT_INCOMPLETE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
