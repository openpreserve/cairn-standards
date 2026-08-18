"""Shared fixtures and stand-ins for the whole suite.

The environment isolation here is not housekeeping. CAIRN_SITE_DIR is set by
deploy/Dockerfile.syncer and exported by deploy/sync-loop.sh, so it is exactly what a
maintainer debugging the deployment has in their shell. Without this, `site_dir()` inside the
code under test resolved somewhere else entirely while the tests kept asserting against
tmp_path, and the 96-case write-once suite reported 97 passed while exercising nothing at all.
A test that cannot fail is worse than no test, and that one existed specifically to stop that
happening to the sync.

It lives in conftest.py rather than in one file because a file-scoped version already existed
in test_cli.py and protected one of the three modules that needed it.
"""

from __future__ import annotations

import pytest

@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    """Run every test in the configuration production runs in, not a convenient one.

    CAIRN_SITE_DIR is *set*, to a directory no test can arrive at by rebuilding the default
    layout from its workspace root. Deleting it instead only removed the maintainer's shell
    from the picture: it left `site_dir(root)` equal to `root / "site"`, so a test could go on
    hard-coding that and still pass, and the convention that it must not was enforced by a
    comment and, later, by a regex that caught one bypass shape in four.

    Set, the convention is enforced by construction. Any test that resolves the document root
    itself now reads a directory nothing wrote to and fails on the spot, and every test
    exercises the same override the container runs with.

    CAIRN_ROUTES_FILE stays deleted: its default is under `build/`, which is not served and has
    never been the source of this confusion, and tests that care set it themselves.
    """
    monkeypatch.setenv("CAIRN_SITE_DIR", str(tmp_path / "document-root"))
    monkeypatch.delenv("CAIRN_ROUTES_FILE", raising=False)
