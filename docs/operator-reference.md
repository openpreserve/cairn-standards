# Operator reference

Look-up tables for someone reading a log or a CI failure. Every marker Cairn can print is
listed, with what it means and what to do about it.

Markers are printed by `cairn` itself, so this page and the strings in the log have one source.
They are the strings to alert on.

---

## Exit codes

`cairn sync`'s exit code answers whether the run *finished*, which is a different question from
whether it found anything.

| code | meaning |
| ---- | ------- |
| 0 | ran to the end, nothing to report |
| 1 | did not finish: unloadable manifest, unhandled fault, killed |
| 3 | ran to the end, and something needs an operator |
| 4 | ran to the end, and one or more standards failed |
| 5 | ran to the end, and *every publication it attempted* failed, so nothing was checked |

That distinction is what the verify stamp is written from. A pass that finished counts as a
verification even if it reported problems, so one persistently failing standard cannot make
every cycle re-verify and re-download the whole registry for ever. A pass that did not finish
is not stamped, because nothing can be concluded about the artifacts it never reached.

Nor is 5. A pass in which every publication failed re-read nothing, so recording it as a
verification would suppress the next attempt for a full interval. The unit is the publication -
a release or a rules revision - not the standard: a standard with one rotted release and two
good ones did read two of them, and counting standards reported that as having checked nothing.

The table is `cairn sync` only. `cairn validate` is a gate rather than a step in a loop: it
exits 0 or refuses with 1, and a refusal there is a finding, not a crash.

`cairn build` has three outcomes, and they are not the same question: 0 rendered cleanly, 3
rendered but reported something, 1 produced nothing. Its two consumers treat 3 differently on
purpose. The syncer loop does not call it a failed render, because there the site is live and
current and calling it one sends an operator to check a disk that is fine. CI and the image
build both treat it as fatal, because there it is a mis-encoded file arriving on a branch and
that is the cheapest place to fix it. Same code, opposite correct response - which is why the
loop reads the number from `cairn exit-codes` rather than holding an opinion about it.

---

## Markers

They fall into three groups, and the group tells you how urgent it is.

Markers naming a "version" cover both kinds of publication, because both make the same promise
and go through the same guard. The path in the message says which you are looking at:
`ead v4.0.0/ead.xsd` is a release, `ead v4/schematron/2026-07/ead.sch` is a rules revision. They
are not split into two sets, because operators alert on these strings and a second set would
mean writing every alert twice.

### A manifest edit would have broken a published URL

The sync refuses and leaves the published files untouched. Fix the manifest; nothing is wrong
with the volume.

**`FROZEN VERSION CHANGED`** - the bytes upstream no longer match what was recorded for a
frozen publication. Usually a moved tag. The fix is upstream: cut a new tag, and publish it
here as a new version or revision.

**`FROZEN VERSION LOST AN ARTIFACT`** - the manifest no longer declares a file that is already
published at a frozen URL. Restore the artifact entry, or publish the change as a new one.

**`FROZEN VERSION REPOINTED`** - a published publication now names a different repo or ref. The
bytes may well be identical, but a published thing's recorded origin is part of what it
published, so the sync will not amend it to follow the manifest.

All three are caught by `cairn validate --baseline <checkout>` on a pull request, which is
where they should be caught. Reaching the syncer means the change was merged anyway.

**`WRITE-ONCE VIOLATION`** - the same problems seen from that gate rather than from the syncer,
and the only one of these you should normally meet. It is what `cairn validate --baseline`
prints on a pull request, listing every edit that would change or remove an already-published
URL, and it refuses with exit 1 so the branch cannot merge. Nothing has been written anywhere.

**`PUBLISHED VERSION UNFROZEN`** - `provenance.json` records this publication as published, and
the manifest now says `lifecycle: draft`. That un-freezes bytes already handed out, and the next
sync would overwrite them in place. Restore the lifecycle, or set `served: false`. The
pull-request gate refuses the same edit, so seeing this from the syncer means the change arrived
by a route that skipped it.

**`UPSTREAM UNREACHABLE`** - only from `cairn sync --dry-run`, which is what CI runs to check
that every declared source still answers. The manifest points somewhere that did not respond: a
repo made private, a renamed branch, a deleted tag, or an outage. Nothing has been written, and
a real sync would fail on the same artifacts.

**`VERSION PUBLISHED`** - not a failure. A publication became frozen on this cycle, so the
write-once checks did not apply to it: on that cycle the manifest is not contradicting a
promise, it is making one. Expect it exactly once per publication, when you publish one.

The run exits **0**. `cairn all` runs in the image build stage against an empty document root,
where everything published publishes, so counting a publication as attention meant no image
could be built once anything was published. Alert on the marker in the log, not on the exit
code.

It is reported because nothing on the volume can prove it was intended. Whether the guards run
is decided by the lifecycle recorded in `provenance.json`, which lives on the volume those
guards protect - so a record that has rotted, or been edited to `draft`, produces this line too,
and in that case the bytes just adopted are whatever upstream served, with no check against what
was published. **If you see this and you published nothing, treat that publication as suspect
and compare it against an independent copy.**

### The sync cannot tell what is true, and will not guess

These need a person, do not self-heal, and repeat every cycle until resolved. They are the most
important markers here.

**`PROVENANCE UNREADABLE`** - a published publication's `provenance.json` is present but cannot
be parsed. That file is the only record of what was published, so rebuilding it would adopt
whatever upstream serves now and destroy the evidence in the same run. Restore it from a
backup, or confirm the bytes on disk against an independent copy and re-publish deliberately.
Drafts are rebuilt instead of refused, and reported under `INTEGRITY CHECK FAILED`.

**`UNVERIFIABLE PUBLISHED FILE`** - a published file has no recorded checksum and no longer
matches upstream. Nothing available can say whether the served copy rotted or upstream was
re-tagged, and the two need opposite responses, so the sync refuses to pick one. Compare
against an independent copy before doing anything.

**`NO CHECKSUM RECORDED`** - a publication was about to be written with an artifact carrying no
checksum, which would put a claim in `SHA256SUMS` that nothing could later verify. It is refused
before anything is written.

`PROVENANCE UNAVAILABLE` is the near neighbour of the first of these and is deliberately not in
this group: the record could not be *read at all*, which is usually a mode or a mount rather
than damage to the bytes. The sync repairs the mode if it can, writes nothing, and retries next
cycle rather than asking anyone to restore a backup. If it persists, check ownership on the
volume.

### Something on the volume was wrong

Service is restored automatically where it can be.

**`INTEGRITY CHECK FAILED`** - one of: a standard could not be verified; a served file did not
match its recorded checksum and was rewritten from upstream; a published file had vanished and
was restored; or a draft's damaged provenance was rebuilt. Where a repair happened the published
bytes are correct again and service needs no action - but something wrote to files nothing
should be writing to, and that is worth understanding before it reaches a file whose upstream is
gone.

**`CORRUPTED FILE(S) RESTORED`** and **`DAMAGED RECORD(S) REBUILT`** - the same two repairs,
counted on the one-line summary `cairn sync` prints on stdout, where the `INTEGRITY CHECK
FAILED` block says the same thing at length on stderr. They carry no information the block does
not; they exist so the count is visible in a log tailing stdout alone. Either one appearing
means something was repaired, which on its own exits 3 - but a standard failing in the same run
outranks it, so seeing one of these alongside exit 4 (or 5) is correct and means both things
happened.

**`PERMISSION REPAIR FAILED`** - a published file cannot be read by the web server and its mode
could not be changed, so that URL answers 403 until someone with the right ownership fixes it.
Usually a volume written by a different uid.

**`CONTENT UNREADABLE`** - the optional prose beside a manifest (`content/overview.md`, or a
publication's notes) could not be read, so those pages fell back to their one-line summary. The
site is live and every URL resolves; the pages are just missing their descriptions. Fix the
file's encoding or its permissions.

**`BUILD FAILED`** - printed by the syncer loop, not by `cairn`. The replication succeeded but
the render did not, so the site is serving its previous state: correct, just not current.
