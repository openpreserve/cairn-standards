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

from cairn.manifest import Artifact, MajorLine, Release, Source, Standard, Steward
from cairn.nginx import render_routes

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
    """One standard, one major line, one release - enough to generate every route shape."""
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
                status="stable",
                ref="v1.0.0",
                artifacts=[Artifact(name="demo.xsd", role="schema", from_="repo", path="demo.xsd")],
            )
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

    ns = root / "demo" / "_ns"
    ns.mkdir(parents=True)
    (ns / "v1.xhtml").write_text('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"/>', encoding="utf-8")

    (root / "demo" / "index.html").write_text("<html><body>standard</body></html>", encoding="utf-8")
    (root / "index.html").write_text("<html><body>registry</body></html>", encoding="utf-8")
    (root / "404.html").write_text("<html><body>not found</body></html>", encoding="utf-8")


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
