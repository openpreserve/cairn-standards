"""Command-line entry point: `cairn {validate,sync,build,all}`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import find_root
from .manifest import ManifestError, load_all
from .render import render_site
from .sync import SyncError, sync_all


def _add_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Workspace root (defaults to the nearest dir containing standards/ and schemas/).",
    )


def _resolve_root(args) -> Path:
    return find_root(args.root)


def _load(root: Path):
    standards = load_all(root)
    if not standards:
        print(f"No standards found under {root}/standards", file=sys.stderr)
    return standards


def cmd_validate(args) -> int:
    root = _resolve_root(args)
    standards = load_all(root)
    for std in standards:
        n_rel = len(std.releases)
        n_art = sum(len(r.artifacts) for r in std.releases)
        print(f"  ✓ {std.id}: {std.title} — {n_rel} release(s), {n_art} artifact(s)")
    print(f"OK — {len(standards)} manifest(s) valid.")
    return 0


def cmd_sync(args) -> int:
    root = _resolve_root(args)
    standards = _load(root)
    stats = sync_all(
        standards,
        root,
        only=args.standard or None,
        verify=args.verify,
        dry_run=args.dry_run,
        log=print,
    )
    if args.dry_run:
        print(f"Dry run: {stats.planned} artifact(s) resolved.")
    else:
        print(f"Done: {stats.fetched} fetched, {stats.verified} verified, {stats.skipped} frozen/skipped.")
    return 0


def cmd_build(args) -> int:
    root = _resolve_root(args)
    standards = _load(root)
    render_site(standards, root, log=print)
    print(f"Built site into {root / 'site'}")
    return 0


def cmd_all(args) -> int:
    rc = cmd_sync(args)
    if rc:
        return rc
    return cmd_build(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cairn", description="Durable hosting for preservation standards.")
    parser.add_argument("--version", action="version", version=f"cairn {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_val = sub.add_parser("validate", help="Validate all manifests.")
    _add_root(p_val)
    p_val.set_defaults(func=cmd_validate)

    p_sync = sub.add_parser("sync", help="Replicate + checksum upstream artifacts.")
    _add_root(p_sync)
    p_sync.add_argument("--standard", action="append", help="Only this standard id (repeatable).")
    p_sync.add_argument("--verify", action="store_true", help="Re-fetch frozen versions and fail on checksum drift.")
    p_sync.add_argument("--dry-run", action="store_true", help="Resolve + check reachability without writing.")
    p_sync.set_defaults(func=cmd_sync)

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
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
