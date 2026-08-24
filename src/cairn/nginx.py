"""Generate the per-standard nginx routing include (cairn-routes.conf).

The static base config (deploy/nginx.conf) provides MIME types, CORS, caching, content
negotiation maps, and error pages. This module generates only the parts that depend on the
manifests: the namespace documents, the pin-to-latest 303 redirects, the rules-revision
subtree with its moving `latest` pointer, and 410s for withdrawn publications. The result is
``include``-d inside the server block.

Order inside a major line's block is load-bearing. nginx tries regex locations in declaration
order and takes the first that matches, and the pin-to-latest regex matches *everything*
under `/<id>/vN/`, the rules subtree included - so a rules URL emitted after it is redirected
to `/<id>/vX.Y.Z/schematron/...`, which nothing serves. This module is the second place in
the codebase to depend on that property; deploy/nginx.conf carries the first, and both are
held in place by tests rather than by care.
"""

from __future__ import annotations

from pathlib import Path

from .config import LATEST_SEGMENT, RELEASE_PAGE_NAME, RULES_SEGMENT, nginx_routes_path
from .manifest import Standard
from .util import atomic_write, reap_temp_files


def _escaped(version: str) -> str:
    """A version as a regex literal. The dots are what a location pattern must not treat as
    wildcards: `v1.0.0` would otherwise also match `v1x0y0`."""
    return version.replace(".", r"\.")


def _serve_directory() -> list[str]:
    """The directives that serve a tree of generated pages and replicated files.

    Restated from `location /` in the base config because a regex location that matches has to
    handle its own content, and stated once here because two generated blocks need the pair.
    The rewrite is the load-bearing half: without it a directory URI is answered by nginx's
    append-a-slash redirect, which `absolute_redirect off` makes relative, which any layer in
    front that normalises the slash away bounces back for ever. That loop reached production
    once already.
    """
    return [
        f"    rewrite ^(.+)/$ $1 last;",
        f"    try_files $uri $uri/{RELEASE_PAGE_NAME} =404;",
    ]


def _mutable_location(path_pattern: str, why: str) -> list[str]:
    """Serve a draft publication's directory, marking it as not cacheable for a year.

    nginx assigns cache headers by URL shape, and a draft has the same shape as a frozen
    release while its bytes still follow a branch: same dotted version, same directory, but
    re-fetched and overwritten every cycle. Without this, every file of every draft went out
    with `immutable, max-age=31536000`, and a client that has cached one never asks again - so
    the correction a draft exists to allow could never reach anyone who had already looked.

    It sets a variable rather than restating `Cache-Control`, because an `add_header` here
    would replace the whole inherited set and silently drop CORS and `nosniff` along with it.
    `$cairn_mutable` prefixes the cache map's key, and every rule in that map is anchored at
    `^/`, so a prefixed key matches none of them and takes the short default.

    Setting a variable is not a content handler, so the serving directives have to be restated
    too: without them nginx falls back to the static module and answers a directory URI with
    the append-a-slash redirect that `absolute_redirect off` turns into a loop.
    """
    return [
        f"# {why}",
        f'location ~ "{path_pattern}" {{',
        f"    set $cairn_mutable mutable;",
        *_serve_directory(),
        f"}}",
    ]


def _standard_block(std: Standard) -> str:
    lines: list[str] = [f"# ===================== {std.id} =====================",
                        f"location = /{std.id} {{ try_files /{std.id}/index.html =404; }}"]

    # Before every other rule for this standard. A draft that is not served answers 410 and is
    # skipped here entirely, so this can never shadow the 410s emitted at the end of the block.
    for rel in std.releases:
        if rel.is_mutable and rel.is_served:
            lines += _mutable_location(
                f"^/{std.id}/v{_escaped(rel.version)}/",
                f"v{rel.version} is a draft: its bytes still follow a branch, so they may not "
                f"be cached as immutable",
            )

    for ml in std.sorted_major_lines():
        v = ml.major
        latest = ml.latest
        # Namespace document for the major line. Browsers (and anything not explicitly asking
        # for XML) get the human-readable RDDL page; XML tools that send Accept: application/xml
        # are content-negotiated straight to the current schema.
        lines += [
            f"location = /{std.id}/v{v} {{",
            f"    if ($cairn_wants_xml) {{ return 303 /{std.id}/v{latest}/{_schema_name(std, latest)}; }}",
            f"    default_type application/xhtml+xml;",
            f"    try_files /{std.id}/_ns/v{v}.xhtml =404;",
            f"}}",
        ]
        # Before the pin-to-latest regex below, which would otherwise swallow every one of
        # these. See the module docstring.
        lines += _rules_locations(std, v)
        lines += [
            # Pin-to-latest: any file requested under the major line redirects to the concrete
            # latest release. Note /{id}/v{major}.y.z/... does NOT match this (no '/' after vN).
            f'location ~ "^/{std.id}/v{v}/(?<cairn_rest>.+)$" {{ return 303 /{std.id}/v{latest}/$cairn_rest; }}',
        ]

    for rel in std.releases:
        if not rel.is_served:
            lines.append(
                f'location ~ "^/{std.id}/v{_escaped(rel.version)}(/.*)?$" {{ return 410; }}'
            )

    lines.append("")
    return "\n".join(lines)


def _rules_locations(std: Standard, major: int) -> list[str]:
    """Routes for one major line's rules revisions, most specific first.

    Four shapes, and the order between them is the contract:

    1. `410` for a withdrawn revision, so it wins over everything below. Without it the serve
       rule would keep answering 200 from files that are still on disk, because withdrawing a
       publication does not delete what it published.
    2. A draft revision's own directory, so that its short cache wins over the shared serve
       rule beneath it. Same reasoning as a draft release, and reachable the same way: a
       revision may follow a branch while its rules are still settling.
    3. The `latest` pointer, a redirect to the newest published-and-served revision. It is
       generated rather than being a directory in the document root, which is what keeps the
       store write-once: nothing is ever rewritten in place to make `latest` mean something
       new.
    4. The revisions themselves. This exists only because the pin-to-latest regex would
       otherwise claim them; it restates what `location /` in the base config already does,
       including the trailing-slash rewrite that stops a directory URI redirecting to itself.
    """
    rules = std.sorted_rules(major)
    if not rules:
        return []

    prefix = f"/{std.id}/v{major}/{RULES_SEGMENT}"
    lines: list[str] = []

    for rule_set in rules:
        if not rule_set.is_served:
            lines.append(f'location ~ "^{prefix}/{rule_set.revision}(/.*)?$" {{ return 410; }}')

    for rule_set in rules:
        if rule_set.is_mutable and rule_set.is_served:
            lines += _mutable_location(
                f"^{prefix}/{rule_set.revision}/",
                f"rules revision {rule_set.revision} is a draft: its bytes still follow a "
                f"branch, so they may not be cached as immutable",
            )

    current = std.latest_rules(major)
    if current:
        lines += [
            f'location = {prefix}/{LATEST_SEGMENT} {{ return 303 {prefix}/{current.revision}; }}',
            f'location ~ "^{prefix}/{LATEST_SEGMENT}/(?<cairn_rules_rest>.+)$" '
            f'{{ return 303 {prefix}/{current.revision}/$cairn_rules_rest; }}',
        ]

    # The whole subtree, the bare `/schematron` segment included, so that a path with no
    # revision on it answers 404 here rather than being redirected by the pin-to-latest rule
    # into a version directory that has never held rules.
    lines += [
        f'location ~ "^{prefix}(/.*)?$" {{',
        *_serve_directory(),
        f"}}",
    ]
    return lines


def _schema_name(std: Standard, version: str) -> str:
    """The primary schema artifact name for a release (used as the content-neg target)."""
    rel = std.release(version)
    if rel:
        for art in rel.artifacts:
            if art.role == "schema":
                return art.name
        if rel.artifacts:
            return rel.artifacts[0].name
    return f"{std.id}.xsd"


def render_routes(standards: list[Standard]) -> str:
    header = (
        "# Generated by `cairn build` - do not edit by hand.\n"
        "# Included inside the server{} block of deploy/nginx.conf.\n\n"
    )
    return header + "\n".join(_standard_block(s) for s in standards)


def write_routes(standards: list[Standard], root: Path) -> Path:
    # Atomic like every other generated file. A truncated include is worse than a stale one:
    # the web container reloads on any change to this file, and nginx refuses a config it
    # cannot parse, so a kill mid-write leaves the running worker on the old config and the
    # next restart unable to start at all.
    out = nginx_routes_path(root)
    # Same argument as the document root: this volume is written by the syncer container and
    # read by the web one, so a directory left at the writer's umask is a config the reader
    # cannot open when the two run as different uids.
    out.parent.mkdir(parents=True, exist_ok=True)
    # This directory is under neither reaper: the sync only sweeps release directories and
    # the render only sweeps the document root, while the routes file lives outside both.
    # Writing it is the last thing a build does, so a kill here is the likeliest one.
    reap_temp_files(out.parent)
    atomic_write(out, render_routes(standards).encode("utf-8"))
    return out
