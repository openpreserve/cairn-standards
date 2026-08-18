"""The one promise, checked across every state the sync can meet.

Every other test here is an example: a situation someone thought of, asserted, and moved on
from. That is precisely the method that kept failing. Three rounds of review each found their
worst bug in a guard added for the previous round, always in a combination nobody enumerated:
a draft whose upstream had not moved, a frozen release whose record had rotted, a legacy
record with no checksum reached through the one branch that skips the fetch.

So this enumerates instead. Five dimensions the sync actually branches on, crossed:

    release state  x  provenance state  x  bytes on disk  x  upstream state  x  --verify

where a release state is the pair (lifecycle, served), which used to be a single six-value
`status`. Squashing two independent facts into one enum is what made `withdrawn` - neither
mutable nor served - a hole that four successive fixes each failed to close.

and asserts one thing about all of them, phrased without reference to how the code works:

    for a published release, a sync leaves the served bytes holding what was published, or
    leaves them exactly as it found them, or reports the standard as failed.

and, in the other direction, which matters just as much:

    a state with nothing wrong in it is never reported as a failure.

The middle clause is not a loophole, it is the cheap-sync bargain: an ordinary cycle skips a
frozen release without reading it, so corruption that was already there survives until the
`--verify` pass. What the sync must never do is *introduce* a difference and say nothing.

The second assertion exists because a guard that fires when it should not is just as much a
bug, and passes a one-directional check silently: a withdrawn release whose upstream had
moved failed its entire standard every cycle, and 96 cases had nothing to say about it.

Silence is the failure mode. Every bug this file was written after ended with the published
bytes replaced, or the check skipped, and exit 0.
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from unittest import mock

import pytest

from fakes import FakeClient
from cairn.manifest import (
    Artifact,
    Lifecycle,
    MajorLine,
    Release,
    Source,
    Standard,
    Steward,
)
from cairn.config import SITE_DIRNAME, site_dir
from cairn.sync import SyncStats, sync_all
from cairn.util import sha256_hex

PUBLISHED = b"<xs:schema>the bytes that were published</xs:schema>\n"
UPSTREAM_MOVED = b"<xs:schema>something else entirely</xs:schema>\n"
CORRUPTED = b"\x00\x00 rot \x00\x00"

# resolve() builds this from the standard below; matching it keeps every case from also
# looking like a repoint, which is a different dimension with its own tests.
RECORDED_URL = "https://raw.githubusercontent.com/o/r/main/demo.xsd"

# The full state space, which is now a product rather than an enum: two lifecycles times two
# serving states. `(PUBLISHED, served=False)` is the old `withdrawn`, and `(DRAFT, served=False)`
# is an abandoned draft - two situations the old enum could not tell apart, which is why
# un-serving a release used to un-promise it.
STATES = [
    (Lifecycle.DRAFT, True),
    (Lifecycle.DRAFT, False),
    (Lifecycle.PUBLISHED, True),
    (Lifecycle.PUBLISHED, False),
]


def _name(state) -> str:
    lifecycle, served = state
    return f"{lifecycle}{'' if served else '/unserved'}"
PROVENANCE = ["valid", "no_checksum", "damaged", "absent"]
ON_DISK = ["published", "corrupted", "missing"]
UPSTREAM = ["unchanged", "moved"]
VERIFY = [False, True]


def _standard(state) -> Standard:
    lifecycle, served = state
    artifact = Artifact(name="demo.xsd", role="schema", from_="repo", path="demo.xsd")
    return Standard(
        id="demo",
        title="Demo",
        summary="s",
        steward=Steward(org="x"),
        source=Source(type="github", repo="o/r", ref="main"),
        major_lines=[MajorLine(major=1, latest="1.0.0")],
        releases=[Release(version="1.0.0", lifecycle=lifecycle, served=served,
                          artifacts=[artifact], ref="main")],
    )


def _seed(root: Path, state, provenance: str, on_disk: str) -> Path:
    lifecycle, served_flag = state
    # site_dir(), not root/"site". The sync resolves its document root through that
    # function, which honours CAIRN_SITE_DIR; hard-coding the path here meant that with
    # the variable set the sync wrote elsewhere, every assertion compared a file nothing
    # had touched, and all 96 cases passed while testing nothing.
    vdir = site_dir(root) / "demo" / "v1.0.0"
    vdir.mkdir(parents=True)
    served = vdir / "demo.xsd"

    if on_disk == "published":
        served.write_bytes(PUBLISHED)
    elif on_disk == "corrupted":
        served.write_bytes(CORRUPTED)

    if provenance == "damaged":
        (vdir / "provenance.json").write_bytes(b"\xff\xfe not utf-8 any more\n")
    elif provenance != "absent":
        record = {
            "name": "demo.xsd",
            "role": "schema",
            "media_type": "application/xml",
            "bytes": len(PUBLISHED),
            "source": {"from": "repo", "url": RECORDED_URL, "repo": "o/r", "ref": "main", "commit": None},
            "fetched_at": "2026-01-01T00:00:00+00:00",
        }
        if provenance == "valid":
            record["sha256"] = sha256_hex(PUBLISHED)
            (vdir / "SHA256SUMS").write_bytes(f"{record['sha256']}  demo.xsd\n".encode())
        (vdir / "provenance.json").write_text(
            json.dumps({"standard": "demo", "version": "1.0.0", "lifecycle": str(lifecycle),
                        "served": served_flag, "artifacts": [record]}),
            encoding="utf-8",
        )
    return served


CASES = list(itertools.product(STATES, PROVENANCE, ON_DISK, UPSTREAM, VERIFY))


def _check_invariant(state, provenance, on_disk, upstream, served: Path, before, stats, case: str) -> None:
    """The oracle, kept out of the test body so it can itself be driven and shown to fail."""
    lifecycle, is_served = state
    failed = bool(stats.failures)
    if failed:
        assert stats.failures[0][1].strip(), f"{case}: a failure was recorded with no message"

    # A release that is internally consistent and matches upstream has nothing wrong with it.
    # Failing that is a guard firing when it should not, which a one-directional check misses.
    if provenance == "valid" and on_disk == "published" and upstream == "unchanged":
        assert not failed, (
            f"{case}: a healthy release was reported as failed: {stats.failures[0][1].splitlines()[0]}"
        )

    # A draft tracks its branch, whether or not anyone can currently read it.
    if lifecycle is Lifecycle.DRAFT:
        return

    # Published but not served: dormant. Not fetched, not compared, not written. The bytes are
    # still promised - the manifest cannot drop them - so what must hold is that nothing touches
    # them, which is strictly stronger than the old rule that a withdrawn release simply must
    # not fail. That weaker rule is what let a withdrawn release adopt upstream drift.
    if not is_served and provenance in ("valid", "no_checksum") and on_disk != "missing":
        assert served.read_bytes() == before, f"{case}: a dormant release's bytes were rewritten"
        if provenance == "valid":
            assert not failed, f"{case}: a dormant release was reported as failed"
        return

    if on_disk == "missing":
        # This third of the matrix used to return here, asserting nothing - and it is exactly
        # where a published artifact with no recorded checksum was being silently replaced by
        # whatever upstream served. Nothing is being served while the file is absent, so a
        # first publication may write freely; but a record that says this version was
        # published before means the bytes restored have to be the ones it published.
        if provenance in ("valid", "no_checksum"):
            after = served.read_bytes() if served.exists() else None
            if after != PUBLISHED:
                assert failed, (
                    f"{case}: a published artifact was replaced with {str(after)[:40]} "
                    f"and the run reported success"
                )
        return

    assert served.exists(), f"{case}: a published artifact disappeared"
    after = served.read_bytes()
    if after not in (PUBLISHED, before):
        assert failed, f"{case}: served bytes became {after[:40]!r} and the run reported success"


@pytest.mark.parametrize("state,provenance,on_disk,upstream,verify", CASES)
def test_published_bytes_survive_every_state(tmp_path, state, provenance, on_disk, upstream, verify):
    served = _seed(tmp_path, state, provenance, on_disk)
    before = served.read_bytes() if served.exists() else None
    upstream_bytes = PUBLISHED if upstream == "unchanged" else UPSTREAM_MOVED

    with mock.patch("cairn.sync.http_client", lambda: FakeClient(upstream_bytes)):
        stats = sync_all([_standard(state)], tmp_path, verify=verify, log=lambda *a: None)

    case = f"{_name(state)}/{provenance}/{on_disk}/upstream {upstream}/verify {verify}"
    _check_invariant(state, provenance, on_disk, upstream, served, before, stats, case)


def test_the_table_covers_what_it_claims():
    """A miscounted product silently shrinks the cross product to a handful of cases."""
    assert len(CASES) == 4 * 4 * 3 * 2 * 2 == 192


PUBLISHED_SERVED = (Lifecycle.PUBLISHED, True)


def test_the_oracle_can_actually_fail(tmp_path):
    """The check on the check.

    This suite once could not fail at all: with CAIRN_SITE_DIR set the sync wrote to one
    directory while every assertion read another, and 96 cases reported success while
    exercising nothing. So drive the oracle with a substitution that was reported as clean,
    and require it to object.
    """
    served = _seed(tmp_path, PUBLISHED_SERVED, "valid", "published")
    before = served.read_bytes()
    served.write_bytes(UPSTREAM_MOVED)  # a sync that replaced the published bytes...

    with pytest.raises(AssertionError, match="reported success"):
        _check_invariant(PUBLISHED_SERVED, "valid", "published", "moved", served, before,
                         SyncStats(), "meta")  # ...and said nothing


def test_the_oracle_accepts_a_reported_failure(tmp_path):
    """The same substitution, reported. That is refusal, not silence, and must pass."""
    served = _seed(tmp_path, PUBLISHED_SERVED, "valid", "published")
    before = served.read_bytes()
    served.write_bytes(UPSTREAM_MOVED)

    loud = SyncStats(failures=[("demo", "FROZEN VERSION CHANGED: demo v1.0.0/demo.xsd")])
    _check_invariant(PUBLISHED_SERVED, "valid", "published", "moved", served, before, loud, "meta")


def test_the_oracle_objects_to_a_dormant_release_being_written_to(tmp_path):
    """The arm that is new in this model, and the one the old oracle had backwards: it exempted
    withdrawn releases from failing and said nothing about their bytes, so a verify pass that
    overwrote them passed all 36 transition cases."""
    dormant = (Lifecycle.PUBLISHED, False)
    served = _seed(tmp_path, dormant, "valid", "published")
    before = served.read_bytes()
    served.write_bytes(UPSTREAM_MOVED)

    with pytest.raises(AssertionError, match="dormant release's bytes were rewritten"):
        _check_invariant(dormant, "valid", "published", "moved", served, before, SyncStats(), "meta")


# --- the second matrix: what a release's status changing between cycles may do -------------
#
# The first matrix crosses five dimensions, and every one of them is a property of a single
# cycle. A release's status is not: it is edited between cycles, and the edit is the whole
# release procedure this project documents. Nothing enumerated that, and the cost was a sync
# that froze the draft era's bytes as the published release, recorded a checksum matching them,
# wrote SHA256SUMS to agree, and exited 0 - self-certifying, and wrong.

TRANSITIONS = list(itertools.product(STATES, STATES, UPSTREAM, VERIFY))

# The one edit the manifest may not make. Everything else in the 4x4 is legal, including both
# directions of `served`, which is the point: un-serving a release and restoring it is now an
# ordinary round trip rather than a laundering route.
FORBIDDEN = [(was, now) for was, now in itertools.product(STATES, STATES)
             if was[0] is Lifecycle.PUBLISHED and now[0] is Lifecycle.DRAFT]


def _check_transition(was, now, upstream: str, served: Path, stats, case: str) -> None:
    """What a manifest edit between cycles is allowed to leave behind.

    Phrased without reference to how the sync branches, like the first oracle - and this time
    without restating the predicate under test. The previous version of this function opened
    with `was not in MUTABLE_STATUSES and status_is_served(was) and now in MUTABLE_STATUSES`,
    which is the production predicate retyped, so all 36 cases agreed with the code while a
    published release routed through `withdrawn` came back mutable. A test that reimplements
    the logic under test cannot find a logic error in it.

    The rule below reads off the manifest instead: published never becomes draft.
    """
    was_lifecycle, was_served = was
    now_lifecycle, now_served = now
    failed = bool(stats.failures)
    after = served.read_bytes() if served.exists() else None
    upstream_bytes = PUBLISHED if upstream == "unchanged" else UPSTREAM_MOVED

    # Un-freezing a published version lets the next sync overwrite bytes already handed out.
    # No sequence of `served` edits can reach this, which is the whole design: it is the single
    # forbidden edge in the state machine rather than a path through a six-value enum.
    if (was, now) in FORBIDDEN:
        assert failed, f"{case}: a published version was un-frozen and the run reported success"
        assert after == PUBLISHED, f"{case}: published bytes were overwritten in place"
        return

    if now_lifecycle is Lifecycle.DRAFT:
        # A draft tracks its branch, whatever it was before, and being un-served does not stop
        # it: nothing is promised either way.
        assert not failed, f"{case}: a mutable release was reported as failed"
        assert after == upstream_bytes, f"{case}: a draft did not track its upstream"
        return

    # Published now. If it was a draft before, this cycle is the publication: what the manifest
    # names now is what gets published, and the record has to describe those exact bytes.
    if was_lifecycle is Lifecycle.DRAFT:
        assert not failed, f"{case}: publishing a version was reported as failed"
        assert after == upstream_bytes, (
            f"{case}: the version published {str(after)[:40]} rather than what the manifest names"
        )
        record = json.loads((served.parent / "provenance.json").read_text())["artifacts"][0]
        assert record["sha256"] == sha256_hex(after), (
            f"{case}: the recorded checksum describes bytes other than the ones published"
        )
        assert record["source"]["ref"] == "main", f"{case}: the record kept the draft's coordinates"
        return

    # Published before and published now. The bytes are the promise, and un-serving does not
    # release it: the old model let exactly this pair rewrite them and say nothing.
    if after != PUBLISHED:
        assert failed, f"{case}: published bytes became {str(after)[:40]} and the run said nothing"
    elif was == now and upstream == "unchanged":
        # The arm that asserted nothing at all. Every other branch of this oracle checks that a
        # guard did not fire when it should not have; without this one, a regression making
        # every ordinary published sync raise passed all cases.
        assert not failed, (
            f"{case}: nothing was wrong with this release and the standard was reported failed: "
            f"{stats.failures[0][1].splitlines()[0]}"
        )


@pytest.mark.parametrize("was,now,upstream,verify", TRANSITIONS)
def test_a_manifest_edit_between_cycles(tmp_path, was, now, upstream, verify):
    with mock.patch("cairn.sync.http_client", lambda: FakeClient(PUBLISHED)):
        sync_all([_standard(was)], tmp_path, log=lambda *a: None)

    served = site_dir(tmp_path) / "demo" / "v1.0.0" / "demo.xsd"
    upstream_bytes = PUBLISHED if upstream == "unchanged" else UPSTREAM_MOVED
    with mock.patch("cairn.sync.http_client", lambda: FakeClient(upstream_bytes)):
        stats = sync_all([_standard(now)], tmp_path, verify=verify, log=lambda *a: None)

    _check_transition(was, now, upstream, served, stats,
                      f"{_name(was)} -> {_name(now)}/upstream {upstream}/verify {verify}")


def test_the_transition_table_covers_what_it_claims():
    assert len(TRANSITIONS) == 4 * 4 * 2 * 2 == 64


def test_only_un_publishing_is_forbidden():
    """Pins the size of the forbidden set. An edit that widened it to cover un-serving would
    make the suite green while breaking the operation withdrawal exists for."""
    assert len(FORBIDDEN) == 4, FORBIDDEN
    assert all(was[0] is Lifecycle.PUBLISHED and now[0] is Lifecycle.DRAFT for was, now in FORBIDDEN)
