"""Every operator-facing marker string, in one place.

A marker is the shouted phrase that opens a message an operator may have to act on. The
runbooks tell people to alert on these strings, so a marker that reaches a deployment log
without a runbook entry is an alert nobody can answer.

Keeping them as an enum rather than as literals at each print site is not tidiness. Three
successive versions of the guard that checks markers against the runbook were a regex over
the source, and each one was blind to markers written in a shape it had not anticipated:
first a marker with no word boundary before it, then one with a colon it did not expect, then
`CORRUPTED FILE(S) RESTORED`, `DAMAGED RECORD(S) REBUILT` and `WRITE-ONCE VIOLATION`, whose
parentheses and hyphen defeated it. All three had already shipped undocumented while the
guard reported green.

A registry inverts that. The check is no longer "did the regex find everything?" but "is
every member of this enum documented?", which cannot be blind, and the accompanying test
refuses to let a marker be spelled out as a literal anywhere else. Adding a marker is
therefore a two-line change that fails the suite until the runbook mentions it.

deploy/sync-loop.sh prints BUILD_FAILED and cannot import this module, so that one is pinned
to the script by a test instead.
"""

from __future__ import annotations

from enum import StrEnum


class Marker(StrEnum):
    """Operator-facing markers. The value is the exact string that reaches the log."""

    # A manifest edit would have broken a published URL. Nothing is wrong with the volume.
    FROZEN_VERSION_CHANGED = "FROZEN VERSION CHANGED"
    FROZEN_VERSION_LOST_AN_ARTIFACT = "FROZEN VERSION LOST AN ARTIFACT"
    FROZEN_VERSION_REPOINTED = "FROZEN VERSION REPOINTED"
    WRITE_ONCE_VIOLATION = "WRITE-ONCE VIOLATION"
    PUBLISHED_VERSION_UNFROZEN = "PUBLISHED VERSION UNFROZEN"

    # Not a failure: the one cycle on which a release is published and the write-once guards
    # are therefore off. Reported because the volume cannot prove that cycle was intended.
    VERSION_PUBLISHED = "VERSION PUBLISHED"
    UPSTREAM_UNREACHABLE = "UPSTREAM UNREACHABLE"

    # The sync cannot tell what is true and will not guess. These need a person.
    PROVENANCE_UNREADABLE = "PROVENANCE UNREADABLE"
    PROVENANCE_UNAVAILABLE = "PROVENANCE UNAVAILABLE"
    UNVERIFIABLE_PUBLISHED_FILE = "UNVERIFIABLE PUBLISHED FILE"
    NO_CHECKSUM_RECORDED = "NO CHECKSUM RECORDED"

    # Something on the volume was wrong. Service is restored where it can be.
    INTEGRITY_CHECK_FAILED = "INTEGRITY CHECK FAILED"
    CORRUPTED_FILES_RESTORED = "CORRUPTED FILE(S) RESTORED"
    DAMAGED_RECORDS_REBUILT = "DAMAGED RECORD(S) REBUILT"
    PERMISSION_REPAIR_FAILED = "PERMISSION REPAIR FAILED"
    CONTENT_UNREADABLE = "CONTENT UNREADABLE"

    # Printed by deploy/sync-loop.sh, which relays the rest but owns this one.
    BUILD_FAILED = "BUILD FAILED"
