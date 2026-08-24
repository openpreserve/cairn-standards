"""Every state `_decide` can be handed, and the promises that hold across all of them.

The invariant matrix drives whole releases through the real sync, which is what makes it
believable - but its dimensions are release-wide. "provenance valid" and "bytes on disk"
describe a one-artifact release, so a combination like *provenance lost and one of two
artifacts lost* cannot be expressed in it at all. That is precisely where a published release
was found writing whatever upstream now served into a write-once URL, with 483 tests green.

`_decide` is a pure function of named evidence, so the space can simply be enumerated. No
filesystem, no network, no manifest: the point is to check the branch selection itself rather
than to re-test the plumbing around it.

Dormancy is deliberately not one of the dimensions. It is a property of the release, not of an
artifact, and deciding it here still let `resolve()` run first - which for a `release-asset`
artifact is a GitHub API call that raises when the tag is gone. It is a single early return in
the planners now, covered by tests/test_sync_freeze.py.
"""

from __future__ import annotations

import itertools

import pytest

from cairn.sync import Evidence, Verdict, _decide

SHA_A = "a" * 64
SHA_B = "b" * 64

WRITE_ONCE_SAFE = {
    Verdict.SKIP_FROZEN,
    Verdict.SKIP_CORROBORATED,
    Verdict.VERIFY,
    Verdict.RESTORE,
    Verdict.REFUSE_CHANGED,
    Verdict.REFUSE_REPOINTED,
    Verdict.REFUSE_UNVERIFIABLE,
}


def _states():
    """Every coherent evidence combination, after the fetch."""
    for (mutable, publishing, promised, verify, recorded, has_sha, moved, on_disk,
         served, upstream) in itertools.product(
            (False, True), (False, True), (False, True), (False, True),
            (False, True), (False, True), (False, True), (False, True),
            (None, SHA_A, SHA_B), (SHA_A, SHA_B)):
        # Impossible by construction rather than by policy, so excluding them is not a way of
        # dodging a case: a checksum cannot be recorded without a record, coordinates cannot
        # have moved without one, and a file that is not there has no digest.
        if has_sha and not recorded:
            continue
        if moved and not recorded:
            continue
        if served is not None and not on_disk:
            continue
        # on_disk=True with served_sha=None is deliberately kept: that is exactly what
        # _plan_publication builds for a file that is present but cannot be read, since
        # _served_digest returns None on any OSError. Excluding it left the present-but
        # -unreadable case - EACCES from the 403 this service repairs elsewhere, EIO from the
        # bad sector RESTORE exists for - never fed to the decision at all.
        # The publication-level flags are derived from one another in _plan_publication.
        if promised and (mutable or publishing):
            continue
        yield Evidence(
            mutable=mutable, publishing=publishing, promised=promised,
            verify=verify, recorded=recorded, recorded_sha=SHA_A if has_sha else None,
            moved=moved, on_disk=on_disk, served_sha=served, upstream_sha=upstream,
        )


STATES = list(_states())


def test_the_enumeration_is_not_empty_or_trivial():
    """A filter that excluded everything would make every assertion below vacuous."""
    assert len(STATES) > 200, len(STATES)
    assert len({_decide(e) for e in STATES}) >= 6, "the enumeration reaches almost no branches"


@pytest.mark.parametrize("e", STATES, ids=lambda e: "")
def test_a_published_release_is_never_overwritten(e):
    """The whole promise, in one assertion, over the whole space.

    A release that has published and is not publishing on this cycle may be skipped, verified,
    restored from its own record, or refused. It may never be written from upstream, because
    that is what silently replacing published bytes looks like from in here.
    """
    if not e.promised:
        return
    verdict = _decide(e)
    assert verdict in WRITE_ONCE_SAFE, f"{verdict} on {e}"
    assert verdict is not Verdict.WRITE


@pytest.mark.parametrize("e", STATES, ids=lambda e: "")
def test_every_state_decides_something_and_asks_for_bytes_only_once(e):
    verdict = _decide(e)
    assert isinstance(verdict, Verdict)
    assert verdict is not Verdict.FETCH, "the bytes are already in evidence"


@pytest.mark.parametrize("e", STATES, ids=lambda e: "")
def test_the_accepting_verdicts_agree_with_what_is_on_disk(e):
    """Each accept path implies something concrete about the served copy. These are the
    statements the commit phase relies on without re-checking them."""
    verdict = _decide(e)
    if verdict is Verdict.SKIP_CORROBORATED:
        assert e.served_sha == e.upstream_sha, "skipped as corroborated while disagreeing"
    if verdict is Verdict.VERIFY:
        assert e.served_sha == e.upstream_sha == e.recorded_sha, "verified without agreement"
    if verdict is Verdict.RESTORE:
        assert e.served_sha != e.upstream_sha, "restored a file that was already correct"


def test_before_the_fetch_only_the_no_bytes_verdicts_are_reachable():
    """The first call may only skip or ask; anything else would decide without evidence."""
    allowed = {Verdict.FETCH, Verdict.SKIP_FROZEN}
    seen = set()
    for e in STATES:
        verdict = _decide(Evidence(**{**e.__dict__, "upstream_sha": None, "served_sha": None}))
        assert verdict in allowed, f"{verdict} decided without the bytes"
        seen.add(verdict)
    assert seen == allowed, f"unreached before the fetch: {allowed - seen}"
