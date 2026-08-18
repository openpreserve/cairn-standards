"""Render the static site: landing pages, RDDL namespace docs, catalog, sitemap, routes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from importlib import resources
from pathlib import Path

import markdown as md
from jinja2 import Environment, PackageLoader, select_autoescape

from . import BASE_URL, __version__
from .config import PROVENANCE_NAME, RELEASE_PAGE_NAME, site_dir
from .manifest import ManifestError, Release, Standard
from .nginx import write_routes
from .util import DecodeError, atomic_write, is_provenance_record_set, reap_temp_tree, read_text

ROLE_LABELS = {
    "schema": "W3C XML Schema (XSD)",
    "relaxng": "RELAX NG",
    "nvdl": "NVDL",
    "schematron": "Schematron",
    "taglibrary-html": "Tag Library (HTML)",
    "taglibrary-pdf": "Tag Library (PDF)",
    "documentation": "Documentation",
    "license": "Licence",
    "other": "File",
}

# RDDL "natures" and "purposes" - machine-readable labels for what each resource *is*
# and why it relates to the namespace. These use http://rddl.org/ (not www., not https).
#
# NOTE (2026-07): rddl.org's TLS certificate expired around early July 2026, so https to
# that host currently fails and www.rddl.org does not resolve. We use plain http://rddl.org/.
# TODO: recheck before a future release - if they renew the cert, switch to https://rddl.org/.
RDDL_NATURE = {
    "schema": "http://www.w3.org/2001/XMLSchema",
    "relaxng": "http://relaxng.org/ns/structure/1.0",
    "nvdl": "http://purl.oclc.org/dsdl/nvdl/ns/structure/1.0",
    "schematron": "http://purl.oclc.org/dsdl/schematron",
    "taglibrary-html": "http://www.w3.org/1999/xhtml",
    "taglibrary-pdf": "http://www.iana.org/assignments/media-types/application/pdf",
    "documentation": "http://www.w3.org/1999/xhtml",
    "license": "http://rddl.org/natures#resource",
    "other": "http://rddl.org/natures#resource",
}
RDDL_PURPOSE = {
    "schema": "http://rddl.org/purposes#normative-reference",
    "relaxng": "http://rddl.org/purposes#normative-reference",
    "nvdl": "http://rddl.org/purposes#normative-reference",
    "schematron": "http://rddl.org/purposes#normative-reference",
    "taglibrary-html": "http://rddl.org/purposes#reference",
    "taglibrary-pdf": "http://rddl.org/purposes#reference",
    "documentation": "http://rddl.org/purposes#reference",
    "license": "http://rddl.org/purposes#reference",
    "other": "http://rddl.org/purposes#reference",
}


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _env() -> Environment:
    env = Environment(
        loader=PackageLoader("cairn", "templates"),
        autoescape=select_autoescape(["html", "xhtml", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals.update(
        base_url=BASE_URL,
        cairn_version=__version__,
        generated_at=_now_iso(),
        role_label=role_label,
        rddl_nature=lambda r: RDDL_NATURE.get(r, RDDL_NATURE["other"]),
        rddl_purpose=lambda r: RDDL_PURPOSE.get(r, RDDL_PURPOSE["other"]),
    )
    env.filters["humansize"] = _humansize
    return env


def _humansize(n: int | None) -> str:
    if not n:
        return ""
    units = ["B", "KB", "MB", "GB"]
    size = float(n)
    for u in units:
        if size < 1024 or u == units[-1]:
            return f"{size:.0f} {u}" if u == "B" else f"{size:.1f} {u}"
        size /= 1024
    return f"{n} B"


@lru_cache(maxsize=None)
def _load_provenance(vdir: Path) -> dict | None:
    """Read a release's provenance for display, or None if it cannot be used.

    The render must survive anything the sync refuses. A damaged record on one release used
    to raise out of `cairn build`, which returns non-zero, which stops the whole cycle - so
    one rotted file froze every healthy standard's pages, routes and 410s at their last
    state. The sync reports the damage and refuses that release; the site keeps rendering
    with whatever it can show, which is the checksums missing from one release's page.

    Keyed on the release directory rather than on the workspace root, because that is the path
    it actually opens: the root is resolved through `site_dir`, which CAIRN_SITE_DIR overrides,
    so two builds under one root but different document roots shared a cache entry.

    Memoised for the duration of one build. Three call sites want the same record - the release
    page context, the namespace document of a major line's latest release, and the catalog - so
    every build was opening, decoding and JSON-parsing each file three times. The cache is
    cleared at the start of each render, because a build runs after a sync that has just
    rewritten these files and a process-lifetime cache would serve the previous cycle's.

    The shape check is shared with the sync rather than restated. The two had already drifted
    once - the sync rejected valid JSON of the wrong shape a release before this side learned
    to - and the whole point of a shape contract is that both ends hold the same one.
    """
    path = vdir / PROVENANCE_NAME
    if not path.exists():
        return None
    try:
        data = json.loads(read_text(path))
    except (json.JSONDecodeError, DecodeError, OSError):
        return None
    return data if is_provenance_record_set(data) else None


_RAW_PREFIX = "https://raw.githubusercontent.com/"


def _github_link(source: dict | None) -> str | None:
    """Turn recorded provenance into a browsable GitHub permalink (pinned to the commit)."""
    if not source:
        return None
    url = source.get("url") or ""
    repo = source.get("repo")
    ref = source.get("commit") or source.get("ref")
    if repo and ref and url.startswith(_RAW_PREFIX):
        parts = url[len(_RAW_PREFIX):].split("/", 3)  # owner/name/ref/path...
        if len(parts) == 4:
            return f"https://github.com/{repo}/blob/{ref}/{parts[3]}"
    return url or None


def _artifact_views(std: Standard, rel: Release, root: Path) -> list[dict]:
    prov = _load_provenance(site_dir(root) / std.id / f"v{rel.version}")
    prov_arts = {a["name"]: a for a in prov.get("artifacts", [])} if prov else {}
    views = []
    for art in rel.artifacts:
        p = prov_arts.get(art.name, {})
        source = p.get("source") or {}
        commit = source.get("commit")
        views.append(
            {
                "name": art.name,
                "role": art.role,
                "title": art.title or role_label(art.role),
                "url": f"/{std.id}/v{rel.version}/{art.name}",
                "media_type": art.content_type(),
                "bytes": p.get("bytes"),
                "sha256": p.get("sha256"),
                "source": source,
                "github": _github_link(source),
                "upstream_repo": source.get("repo"),
                "upstream_ref": source.get("ref"),
                "commit": commit,
                "commit_short": commit[:12] if commit else None,
            }
        )
    return views


def _read_content(path: Path, log, degraded: list[str]) -> str | None:
    """Optional prose beside a manifest. Unreadable prose must not stop a build.

    These files are decoration: the page has a summary to fall back on. Letting a damaged one
    raise would take down the render of every other standard along with it.
    """
    if not path.is_file():
        return None
    try:
        return read_text(path)
    except (OSError, DecodeError) as exc:
        log(f"  [WARN] {path} could not be read and was skipped: {exc}")
        degraded.append(str(path))
        return None


def _overview_html(std: Standard, log, degraded: list[str]) -> str:
    cd = std.content_dir
    src = _read_content(cd / "overview.md", log, degraded) if cd else None
    if not src:
        return f"<p>{std.summary}</p>"
    return md.markdown(src, extensions=["extra", "sane_lists", "toc"])


def _release_notes_html(std: Standard, rel: Release, log, degraded: list[str]) -> str | None:
    cd = std.content_dir
    src = _read_content(cd / f"{rel.version}.md", log, degraded) if cd else None
    if src:
        return md.markdown(src, extensions=["extra"])
    if rel.notes:
        return md.markdown(rel.notes, extensions=["extra"])
    return None


def _write(path: Path, content: str) -> None:
    # Atomic because nginx serves this directory while the render runs, and the syncer that
    # invokes the render can be killed mid-cycle. A plain write_text can leave a truncated
    # page in the document root, which then gets served until the next successful build.
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, content.encode("utf-8"))


def _copy_assets(site: Path) -> None:
    assets_dir = site / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    pkg_assets = resources.files("cairn") / "assets"
    for entry in pkg_assets.iterdir():
        if entry.is_file():
            atomic_write(assets_dir / entry.name, entry.read_bytes())


def render_site(standards: list[Standard], root: Path, log=print) -> int:
    # Guarded here as well as at the CLI boundary, because this is the call that actually
    # replaces the served registry and the nginx routes with whatever it was handed.
    if not standards:
        raise ManifestError("refusing to render an empty registry: it would unpublish every URL")

    # Counted, not merely logged. A page quietly losing its overview and falling back to a
    # one-line summary is the same silence the sync's repair counters exist to end.
    degraded: list[str] = []

    # The memo below is per build, not per process: the sync immediately before this one has
    # just rewritten the records this reads.
    _load_provenance.cache_clear()

    env = _env()
    site = site_dir(root)
    site.mkdir(parents=True, exist_ok=True)

    # The render is the last step of every cycle and the only one that looks at the whole
    # document root, so the sweep for temp files stranded by a kill belongs here. The sync
    # reaps only the release directories of plans it finished, which covers neither the pages
    # written below nor a standard that failed before its commit phase. Safe because a cycle
    # is one process doing one thing at a time; two concurrent cairn runs over one document
    # root would have larger problems than this.
    strays = reap_temp_tree(site)
    if strays:
        log(f"  [tidy] removed {strays} stranded temp file(s) under {site}")

    _copy_assets(site)

    # Root registry index
    _write(site / "index.html", env.get_template("index.html").render(standards=standards))
    log(f"  [page] /")

    for std in standards:
        # Precompute per-release artifact views (merges manifest + provenance checksums).
        release_ctx = []
        for rel in std.sorted_releases():
            release_ctx.append(
                {
                    "release": rel,
                    "artifacts": _artifact_views(std, rel, root),
                    "notes_html": _release_notes_html(std, rel, log, degraded),
                }
            )

        # Standard landing page
        _write(
            site / std.id / "index.html",
            env.get_template("standard.html").render(
                std=std, overview_html=_overview_html(std, log, degraded), release_ctx=release_ctx
            ),
        )
        log(f"  [page] /{std.id}")

        # Namespace (RDDL) document per major line
        for ml in std.sorted_major_lines():
            latest = std.release(ml.latest)
            if latest is None:
                continue
            _write(
                site / std.id / "_ns" / f"v{ml.major}.xhtml",
                env.get_template("namespace.xhtml").render(
                    std=std,
                    major=ml.major,
                    namespace=std.namespace_for(ml.major),
                    latest=latest,
                    artifacts=_artifact_views(std, latest, root),
                ),
            )
            log(f"  [ns]   /{std.id}/v{ml.major}")

        # Concrete release landing pages
        for ctx in release_ctx:
            rel = ctx["release"]
            _write(
                site / std.id / f"v{rel.version}" / RELEASE_PAGE_NAME,
                env.get_template("release.html").render(
                    std=std, rel=rel, artifacts=ctx["artifacts"], notes_html=ctx["notes_html"]
                ),
            )

    # Machine-readable catalog + sitemap + robots + error pages
    _write(site / "catalog.json", _render_catalog(standards, root))
    _write(site / "sitemap.xml", env.get_template("sitemap.xml").render(standards=standards))
    _write(site / "robots.txt", env.get_template("robots.txt").render())
    _write(site / "404.html", env.get_template("404.html").render())
    _write(site / "410.html", env.get_template("410.html").render())
    log("  [meta] catalog.json, sitemap.xml, robots.txt, 404, 410")

    # Generated nginx routing
    routes = write_routes(standards, root)
    log(f"  [conf] {routes}")

    return len(degraded)


def _render_catalog(standards: list[Standard], root: Path) -> str:
    def artifact_json(std, rel):
        prov = _load_provenance(site_dir(root) / std.id / f"v{rel.version}")
        prov_arts = {a["name"]: a for a in prov.get("artifacts", [])} if prov else {}
        out = []
        for art in rel.artifacts:
            p = prov_arts.get(art.name, {})
            out.append(
                {
                    "name": art.name,
                    "role": art.role,
                    "url": f"{BASE_URL}/{std.id}/v{rel.version}/{art.name}",
                    "media_type": art.content_type(),
                    "sha256": p.get("sha256"),
                    "bytes": p.get("bytes"),
                }
            )
        return out

    catalog = {
        "@context": {"cairn": "https://standards.openpreservation.org/schemas/"},
        "generated_at": _now_iso(),
        "generator": f"cairn/{__version__}",
        "site": BASE_URL,
        "standards": [
            {
                "id": std.id,
                "title": std.title,
                "summary": std.summary,
                "url": f"{BASE_URL}/{std.id}",
                "steward": {
                    "org": std.steward.org,
                    "homepage": std.steward.homepage,
                    "github": std.steward.github,
                },
                "major_lines": [
                    {
                        "major": ml.major,
                        "namespace": std.namespace_for(ml.major),
                        "latest": ml.latest,
                        "latest_url": f"{BASE_URL}/{std.id}/v{ml.latest}",
                    }
                    for ml in std.sorted_major_lines()
                ],
                "releases": [
                    {
                        "version": rel.version,
                        "status": rel.label,
                        "released": rel.released,
                        "url": f"{BASE_URL}/{std.id}/v{rel.version}",
                        "artifacts": artifact_json(std, rel),
                    }
                    for rel in std.sorted_releases()
                ],
            }
            for std in standards
        ],
    }
    return json.dumps(catalog, indent=2) + "\n"
