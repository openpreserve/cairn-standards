"""One stand-in for the network, shared by every test module that needs one.

Three near-identical copies of this existed, differing only in constructor arity, which is
how the environment-isolation fixture came to protect one module out of three.
"""

from __future__ import annotations

from pathlib import Path

from cairn.config import find_root


class FakeResponse:
    def __init__(self, content: bytes, status: int = 200):
        self.status_code = status
        self.content = content

    def json(self):
        return {}


class FakeClient:
    """Answers every GET with fixed bytes, and stands in for the client's context manager.

    sync_all owns its client through `with http_client() as client`, so replacing the network
    means replacing that too.
    """

    def __init__(self, content: bytes = b""):
        self._content = content

    def get(self, url, headers=None):
        return FakeResponse(self._content)

    def head(self, url):
        return FakeResponse(b"")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


MANIFEST = """
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


def workspace(root: Path, manifests: dict[str, str] | None = None) -> Path:
    """Build a Cairn workspace at *root*: standards/<id>/standard.yaml plus the real schema.

    Four near-identical versions of this existed across the test modules, which is the same
    divergence that let one of them resolve the document root differently from the code and
    pass 96 cases while testing nothing.
    """
    for standard_id, body in (manifests or {"demo": MANIFEST}).items():
        directory = root / "standards" / standard_id
        directory.mkdir(parents=True)
        (directory / "standard.yaml").write_text(body, encoding="utf-8")

    schemas = root / "schemas"
    schemas.mkdir(exist_ok=True)
    real_schema = find_root(Path(__file__).resolve().parent) / "schemas" / "standard.schema.json"
    (schemas / "standard.schema.json").write_text(real_schema.read_text(encoding="utf-8"), encoding="utf-8")
    return root
