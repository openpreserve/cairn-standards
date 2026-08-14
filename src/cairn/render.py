"""Render the static site: landing pages, RDDL namespace docs, catalog, sitemap, routes."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

import markdown as md
from jinja2 import Environment, PackageLoader, select_autoescape

from . import BASE_URL, __version__
from .config import site_dir
from .manifest import Release, Standard
from .nginx import write_routes

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


def _load_provenance(root: Path, std_id: str, version: str) -> dict | None:
    path = site_dir(root) / std_id / f"v{version}" / "provenance.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


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
    prov = _load_provenance(root, std.id, rel.version)
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


def _overview_html(std: Standard) -> str:
    cd = std.content_dir
    src = None
    if cd and (cd / "overview.md").is_file():
        src = (cd / "overview.md").read_text(encoding="utf-8")
    if not src:
        return f"<p>{std.summary}</p>"
    return md.markdown(src, extensions=["extra", "sane_lists", "toc"])


def _release_notes_html(std: Standard, rel: Release) -> str | None:
    cd = std.content_dir
    if cd and (cd / f"{rel.version}.md").is_file():
        return md.markdown((cd / f"{rel.version}.md").read_text(encoding="utf-8"), extensions=["extra"])
    if rel.notes:
        return md.markdown(rel.notes, extensions=["extra"])
    return None


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _copy_assets(site: Path) -> None:
    assets_dir = site / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    pkg_assets = resources.files("cairn") / "assets"
    for entry in pkg_assets.iterdir():
        if entry.is_file():
            (assets_dir / entry.name).write_bytes(entry.read_bytes())


def render_site(standards: list[Standard], root: Path, log=print) -> None:
    env = _env()
    site = site_dir(root)
    site.mkdir(parents=True, exist_ok=True)
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
                    "notes_html": _release_notes_html(std, rel),
                }
            )

        # Standard landing page
        _write(
            site / std.id / "index.html",
            env.get_template("standard.html").render(
                std=std, overview_html=_overview_html(std), release_ctx=release_ctx
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
                site / std.id / f"v{rel.version}" / "index.html",
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


def _render_catalog(standards: list[Standard], root: Path) -> str:
    def artifact_json(std, rel):
        prov = _load_provenance(root, std.id, rel.version)
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
                        "status": rel.status,
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
