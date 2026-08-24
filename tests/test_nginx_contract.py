"""Serving-layer contract tests: the URL promises kept by nginx rather than by Python.

The other tests cover manifests and the freeze logic. But most of what README calls "the URL
contract" is enforced by deploy/nginx.conf and the generated routes: which URLs resolve at
all, what media type they carry, and which of them a client may cache forever. None of that
is reachable from a unit test, which is how a redirect loop on every release page reached
production unnoticed.

So this boots the real nginx config over a synthetic site tree. It needs docker and skips
without it. The tree is built here rather than taken from site/, so the test needs no network
and does not depend on a prior `cairn sync`.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from cairn.manifest import Lifecycle, Artifact, MajorLine, Release, RuleSet, Source, Standard, Steward
from cairn.nginx import render_routes
from cairn.util import TEMP_PREFIX

REPO_ROOT = Path(__file__).resolve().parents[1]
NGINX_IMAGE = "nginx:1.27-alpine"
CONTAINER_NAME = "cairn-contract-test"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _docker_available(), reason="docker is required to run nginx")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _standard() -> Standard:
    """One standard, one major line, one release and two rules revisions.

    The second revision is withdrawn, which is the case only declaration order gets right: its
    files are still on disk, because withdrawing does not delete what was published.
    """
    rules = [Artifact(name="demo.sch", role="schematron", from_="repo", path="demo.sch")]
    return Standard(
        id="demo",
        title="Demo Standard",
        summary="s",
        steward=Steward(org="x"),
        source=Source(type="github", repo="o/r", ref="main"),
        major_lines=[MajorLine(major=1, latest="1.0.0")],
        releases=[
            Release(
                version="1.0.0",
                lifecycle=Lifecycle.PUBLISHED,
                ref="v1.0.0",
                artifacts=[Artifact(name="demo.xsd", role="schema", from_="repo", path="demo.xsd")],
            ),
            # A draft, which has the same URL shape as the frozen release above and must not be
            # cached the same way: its bytes are re-fetched from a branch every cycle.
            Release(
                version="1.1.0",
                lifecycle=Lifecycle.DRAFT,
                artifacts=[Artifact(name="demo.xsd", role="schema", from_="repo", path="demo.xsd")],
            ),
        ],
        rules=[
            RuleSet(revision="2026-07", applies_to=1, lifecycle=Lifecycle.PUBLISHED,
                    ref="RULES-2026-07", artifacts=rules),
            RuleSet(revision="2026-09", applies_to=1, lifecycle=Lifecycle.PUBLISHED,
                    ref="RULES-2026-09", served=False, artifacts=rules),
            RuleSet(revision="2026-11", applies_to=1, lifecycle=Lifecycle.DRAFT, artifacts=rules),
        ],
    )


def _write_site(root: Path) -> None:
    """The file layout `cairn build` produces, reduced to what the routes touch."""
    release = root / "demo" / "v1.0.0"
    release.mkdir(parents=True)
    (release / "index.html").write_text("<html><body>release</body></html>", encoding="utf-8")
    (release / "demo.xsd").write_text('<?xml version="1.0"?><schema/>', encoding="utf-8")
    (release / "provenance.json").write_text(json.dumps({"standard": "demo"}), encoding="utf-8")
    (release / "SHA256SUMS").write_text("0  demo.xsd\n", encoding="utf-8")

    draft = root / "demo" / "v1.1.0"
    draft.mkdir(parents=True)
    (draft / "index.html").write_text("<html><body>draft release</body></html>", encoding="utf-8")
    (draft / "demo.xsd").write_text('<?xml version="1.0"?><schema/>', encoding="utf-8")

    for revision in ("2026-07", "2026-09", "2026-11"):
        rules = root / "demo" / "v1" / "schematron" / revision
        rules.mkdir(parents=True)
        (rules / "index.html").write_text("<html><body>rules</body></html>", encoding="utf-8")
        (rules / "demo.sch").write_text('<?xml version="1.0"?><schema/>', encoding="utf-8")
        (rules / "provenance.json").write_text(json.dumps({"standard": "demo"}), encoding="utf-8")
        (rules / "SHA256SUMS").write_text("0  demo.sch\n", encoding="utf-8")

    ns = root / "demo" / "_ns"
    ns.mkdir(parents=True)
    (ns / "v1.xhtml").write_text('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"/>', encoding="utf-8")

    (root / "demo" / "index.html").write_text("<html><body>standard</body></html>", encoding="utf-8")
    (root / "index.html").write_text("<html><body>registry</body></html>", encoding="utf-8")
    (root / "404.html").write_text("<html><body>not found</body></html>", encoding="utf-8")
    # Both error pages, because nginx answers 404 for a 410 whose error_page target is missing.
    # Without this file a withdrawn publication looks like an unknown URL, which is the one
    # distinction the 410 exists to make.
    (root / "410.html").write_text("<html><body>gone</body></html>", encoding="utf-8")

    # What a syncer killed between creating a temp file and renaming it leaves behind. It is
    # reaped on the next run, but it must not be reachable in the meantime - and one of these
    # sits under a generated route's prefix, which is where the guard's ordering matters.
    (root / f"{TEMP_PREFIX}leftover").write_text("partial", encoding="utf-8")
    (release / f"{TEMP_PREFIX}leftover").write_text("partial", encoding="utf-8")


@pytest.fixture(scope="module")
def base_url(tmp_path_factory) -> str:
    workspace = tmp_path_factory.mktemp("serving")
    site, conf = workspace / "site", workspace / "conf"
    site.mkdir()
    conf.mkdir()
    _write_site(site)
    (conf / "cairn-routes.conf").write_text(render_routes([_standard()]), encoding="utf-8")

    port = _free_port()
    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)
    started = subprocess.run(
        [
            "docker", "run", "-d", "--name", CONTAINER_NAME,
            "-p", f"127.0.0.1:{port}:8080",
            "-v", f"{site}:/usr/share/nginx/html:ro",
            "-v", f"{conf}:/etc/nginx/cairn:ro",
            "-v", f"{REPO_ROOT / 'deploy' / 'nginx.conf'}:/etc/nginx/nginx.conf:ro",
            NGINX_IMAGE,
        ],
        capture_output=True,
        text=True,
    )
    assert started.returncode == 0, f"could not start nginx: {started.stderr}"

    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                if httpx.get(f"{url}/healthz", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            logs = subprocess.run(["docker", "logs", CONTAINER_NAME], capture_output=True, text=True)
            pytest.fail(f"nginx never became healthy: {logs.stdout}\n{logs.stderr}")
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)


@pytest.fixture(scope="module")
def client(base_url):
    with httpx.Client(base_url=base_url, follow_redirects=False, timeout=5.0) as c:
        yield c


@pytest.mark.parametrize(
    "path",
    ["/demo/v1.0.0", "/demo/v1.0.0/", "/demo/v1.0.0/index.html", "/demo", "/demo/", "/"],
)
def test_pages_resolve_without_redirecting(client, path):
    """Regression: a directory URI used to be answered with a 301 to itself.

    nginx's append-a-slash redirect is relative here (`absolute_redirect off`), so once any
    layer in front normalises the trailing slash away, client and origin bounce the same URL
    between them forever. Neither spelling may redirect.
    """
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} answered {resp.status_code} -> {resp.headers.get('location')}"
    assert "location" not in resp.headers


def test_replicated_artifacts_are_immutable(client):
    """Write-once bytes at a version URL can be cached forever - that is the whole point."""
    resp = client.get("/demo/v1.0.0/demo.xsd")
    assert resp.status_code == 200
    assert "immutable" in resp.headers["cache-control"]
    assert resp.headers["content-type"].startswith("application/xml")


@pytest.mark.parametrize(
    "path", ["/demo/v1.0.0", "/demo/v1.0.0/", "/demo/v1.0.0/index.html", "/demo/v1.0.0/provenance.json", "/demo/v1.0.0/SHA256SUMS"]
)
def test_generated_files_stay_revalidatable(client, path):
    """These live under a version directory but are re-rendered by build and sync.

    An immutable year-long TTL on them cannot be recalled: a client that has cached one never
    asks again, so a corrected page or a re-synced provenance record never reaches it.
    """
    assert "immutable" not in client.get(path).headers["cache-control"]


def test_namespace_document_is_xhtml_for_browsers(client):
    resp = client.get("/demo/v1", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xhtml+xml")


def test_namespace_document_negotiates_to_the_schema_for_xml_clients(client):
    resp = client.get("/demo/v1", headers={"Accept": "application/xml"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/demo/v1.0.0/demo.xsd"


def test_major_line_pins_to_the_latest_release(client):
    resp = client.get("/demo/v1/demo.xsd")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/demo/v1.0.0/demo.xsd"


def test_redirects_stay_relative(client):
    """Absolute redirects would leak the internal http://host:8080 origin to clients."""
    location = client.get("/demo/v1/demo.xsd").headers["location"]
    assert location.startswith("/")


def test_schemas_are_readable_cross_origin(client):
    assert client.get("/demo/v1.0.0/demo.xsd").headers["access-control-allow-origin"] == "*"


def test_unknown_paths_are_not_found(client):
    assert client.get("/demo/v9.9.9/demo.xsd").status_code == 404


@pytest.mark.parametrize(
    "path",
    [f"/{TEMP_PREFIX}leftover", f"/demo/v1.0.0/{TEMP_PREFIX}leftover"],
    ids=["document-root", "under-a-generated-route"],
)
def test_stranded_temp_files_are_never_served(client, path):
    """A process killed between creating a temp file and renaming it strands a partial copy of
    a published artifact in the document root. The syncer reaps strays on its next run, which
    can be six hours away.

    The second case is the one that matters structurally: the dotfile guard is a regex
    location, nginx tries regex locations in declaration order, and the guard used to be
    declared *after* the generated routes include. Its coverage therefore depended on what
    cairn's route generator happened to emit, which is not a property a guard may have.
    """
    assert client.get(path).status_code == 404


# --- the rules line, whose whole routing depends on nginx's declaration order ----------------

@pytest.mark.parametrize(
    "path",
    ["/demo/v1/schematron/2026-07", "/demo/v1/schematron/2026-07/", "/demo/v1/schematron/2026-07/index.html"],
)
def test_a_rules_revision_page_resolves_without_redirecting(client, path):
    """The trap the pin-to-latest redirect sets. `location ~ "^/demo/v1/(.+)$"` matches these
    and sends them to `/demo/v1.0.0/schematron/...`, which has never existed - so getting this
    wrong produces a 303 to a 404 rather than an error anyone would see in a log."""
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} answered {resp.status_code} -> {resp.headers.get('location')}"
    assert "location" not in resp.headers


def test_a_frozen_rules_file_is_served_immutably_as_xml(client):
    resp = client.get("/demo/v1/schematron/2026-07/demo.sch")
    assert resp.status_code == 200
    assert "immutable" in resp.headers["cache-control"]
    assert resp.headers["content-type"].startswith("application/xml")


@pytest.mark.parametrize(
    "path",
    ["/demo/v1/schematron/2026-07/index.html", "/demo/v1/schematron/2026-07/provenance.json",
     "/demo/v1/schematron/2026-07/SHA256SUMS"],
)
def test_generated_files_beside_the_rules_stay_revalidatable(client, path):
    assert "immutable" not in client.get(path).headers["cache-control"]


def test_the_latest_pointer_redirects_to_the_newest_frozen_revision(client):
    resp = client.get("/demo/v1/schematron/latest/demo.sch")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/demo/v1/schematron/2026-07/demo.sch"


@pytest.mark.parametrize("path", ["/demo/v1/schematron/latest", "/demo/v1/schematron/latest/"])
def test_the_bare_latest_pointer_resolves_to_the_revision_page(client, path):
    """Including the trailing-slash spelling, which no `location =` can match: it goes through
    the subtree rule's rewrite and back out, and a rewrite that did not terminate would loop
    inside nginx rather than answering."""
    resp = client.get(path)
    assert resp.status_code == 303, resp.status_code
    assert resp.headers["location"] == "/demo/v1/schematron/2026-07"


def test_the_latest_pointer_is_never_cached_as_immutable(client):
    """It is the URL documentation is told to cite. Cached for a year it would pin every reader
    to whichever revision was current the day they first asked, with no way to recall it."""
    resp = client.get("/demo/v1/schematron/latest/demo.sch")
    assert "immutable" not in resp.headers["cache-control"]


def test_a_withdrawn_revision_answers_gone_although_its_files_remain(client):
    """Withdrawing does not delete what was published, so the bytes are still in the document
    root. Only the 410 being declared before the serve rule stops them being handed out."""
    assert client.get("/demo/v1/schematron/2026-09/demo.sch").status_code == 410
    assert client.get("/demo/v1/schematron/2026-09").status_code == 410


def test_a_rules_path_with_no_revision_is_not_found_rather_than_redirected(client):
    """It must not fall through to the pin-to-latest rule, which would answer a redirect into a
    version directory that has never held rules."""
    resp = client.get("/demo/v1/schematron")
    assert resp.status_code == 404, resp.headers.get("location")


def test_the_release_track_still_pins_to_latest_around_the_rules(client):
    """The rules locations are declared first, so this is the one that could have been shadowed
    by them rather than the other way round."""
    resp = client.get("/demo/v1/demo.xsd")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/demo/v1.0.0/demo.xsd"


# --- drafts have the shape of a frozen publication and must not be cached like one ----------

@pytest.mark.parametrize(
    "path",
    ["/demo/v1.1.0/demo.xsd", "/demo/v1/schematron/2026-11/demo.sch"],
    ids=["draft-release", "draft-rules-revision"],
)
def test_a_draft_is_never_cached_as_immutable(client, path):
    """The bug this pair exists to hold closed.

    A draft is re-fetched from a branch on every cycle, and nginx decides cacheability from the
    URL alone - where a draft is indistinguishable from a frozen release. Every draft file was
    therefore handed out with `immutable, max-age=31536000`, and a client that has cached one
    never asks again, so the correction a draft exists to allow could never reach it.
    """
    resp = client.get(path)
    assert resp.status_code == 200, resp.status_code
    assert "immutable" not in resp.headers["cache-control"], resp.headers["cache-control"]
    assert "max-age=300" in resp.headers["cache-control"]


@pytest.mark.parametrize(
    "path",
    ["/demo/v1.1.0/demo.xsd", "/demo/v1/schematron/2026-11/demo.sch"],
    ids=["draft-release", "draft-rules-revision"],
)
def test_a_draft_keeps_every_other_header(client, path):
    """The reason the draft rule sets a variable instead of restating `Cache-Control`.

    An `add_header` inside a location replaces the whole inherited set, so writing the cache
    header there would have silently dropped CORS and `nosniff` from exactly these URLs - and
    nothing would have reported it, because the response still arrives.
    """
    resp = client.get(path)
    assert resp.headers["access-control-allow-origin"] == "*"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-type"].startswith("application/xml")


@pytest.mark.parametrize(
    "path", ["/demo/v1.1.0", "/demo/v1.1.0/", "/demo/v1.1.0/index.html"]
)
def test_a_draft_page_resolves_without_redirecting(client, path):
    """Marking a directory mutable takes over content handling for it, so the serving rules
    have to be restated - and a restatement that forgot the trailing-slash rewrite would answer
    a directory URI with a relative redirect to itself."""
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} answered {resp.status_code} -> {resp.headers.get('location')}"
    assert "location" not in resp.headers


def test_a_frozen_publication_beside_a_draft_is_still_immutable(client):
    """The other direction: the draft rule must not leak onto its neighbours."""
    assert "immutable" in client.get("/demo/v1.0.0/demo.xsd").headers["cache-control"]
    assert "immutable" in client.get("/demo/v1/schematron/2026-07/demo.sch").headers["cache-control"]
