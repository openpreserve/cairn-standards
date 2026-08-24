# Lifecycle and freezing

Cairn's central promise is that a published URL keeps returning the same bytes for ever. This
page is how that promise is expressed in a manifest, where it is enforced, and what it means
for the edits you are allowed to make.

It applies to both kinds of thing Cairn publishes.

---

## A publication is a release or a rules revision

A **release** is a schema version, served at `/<id>/vX.Y.Z/`. A **rules revision** is one dated
set of Schematron rules for a whole major line, served at `/<id>/vN/schematron/<revision>/`.

They are one type in the code (`Publication` in `src/cairn/manifest.py`) travelling one code
path, and everything below applies to both. Where this page says "a publication", read
"a release or a rules revision".

They exist as separate tracks for one reason: a published release may never gain an artifact,
so rules stored inside a release could not be revised without minting a new schema version for
a schema that had not changed. See
[Publishing a validation-rules revision](publishing-a-rules-revision.md).

## Three fields, three separate jobs

| field | values | what it decides |
| --- | --- | --- |
| `lifecycle` | `draft`, `published` | whether the bytes may still change. Moves in one direction only |
| `served` | `true` (default), `false` | whether the URLs answer `200` or `410` |
| `maturity` | `alpha`, `beta`, `stable`, `deprecated` | the badge on the page. No behaviour at all |

Which gives four states:

| `lifecycle` | `served` | meaning | bytes may change? | URLs |
| --- | --- | --- | --- | --- |
| `draft` | `true` | work in progress, publicly visible | yes | 200 |
| `draft` | `false` | abandoned draft | yes | 410 |
| `published` | `true` | published | no | 200 |
| `published` | `false` | withdrawn from service, promise intact | no | 410 |

These are two fields rather than one because they answer two independent questions. They were
one six-value `status` enum once, in which `withdrawn` was neither mutable nor served, so every
predicate written as "may this change?" or "does this answer?" got that value wrong - and a
release routed `stable → withdrawn → draft` came back mutable with published bytes still on
disk.

## What each state does

**`draft`, served.** The publication follows a ref, usually a branch. Every sync re-fetches it
and overwrites the replica, so the site tracks the branch tip on its own with no action from
anyone. This is the normal state before an official release. It is a preview: the bytes change
under it, so it is not an address to hand out.

**`published`, served.** Frozen. The bytes, a SHA-256 and the provenance are recorded once, and
from then on an ordinary sync skips the publication entirely - which is what makes syncing
cheap. If the upstream bytes behind that ref ever change, the periodic `--verify` pass fails
loudly rather than silently following.

**`served: false`.** The URLs answer `410 Gone`. The publication stays in the manifest and stays
listed. Withdrawing is what you do when an upstream tag has moved or gone.

## The rules that follow

- **Un-serving is not un-publishing.** `served: false` stops the URLs answering. It does not
  make the bytes editable and it does not allow the publication to be dropped from the
  manifest. There is no route back to `draft`.
- **`published` is one-way, and the pull-request gate enforces it.** `cairn validate --baseline`
  refuses `published → draft`.
- **`maturity` is a label and nothing else.** Under the old enum, `status: beta` was frozen and
  served, behaviourally identical to `stable`, so an author writing it to mean "still moving"
  got a publication the syncer refused to update. Freezing is `lifecycle` now.
- **A published publication must pin its own `ref`, and it should be a tag or a commit SHA.**
  The schema requires the field. It cannot check what the value points at, because a tag and a
  branch are indistinguishable as strings. Pinning a tag is what makes a publication rebuildable
  from the manifest alone if its directory is ever lost. Pin a branch and that property is
  gone: a restored volume republishes branch-tip bytes under a write-once URL and records them
  as what was published. Nothing in Cairn can catch that, so it is a review responsibility.
- **A withdrawn publication is dormant.** The syncer does not fetch it, compare it, or write to
  it. Withdrawing is exactly what an operator does when an upstream ref has gone, so probing it
  anyway would fail that whole standard on every verify pass for ever, with no way out - the
  manifest cannot drop a published release either.
- **A major line's `latest` cannot be a withdrawn release.** Validation rejects it, because
  `latest` is what the major-line URL resolves to. It *can* be a draft; that is the normal
  pre-release state, so `/eaf/v1` may legitimately resolve to a draft that is tracking a branch.

## Where the promise is enforced

In two places, on purpose.

**On the pull request**, by `cairn validate --baseline <checkout>`. It compares your manifests
against the branch you are merging into and refuses any edit that would break a published URL.
This is where a violation should be caught, because the person who can fix it is looking at it.

```bash
git worktree add /tmp/baseline origin/main
cairn validate --baseline /tmp/baseline
```

Put the baseline outside the working copy. A path inside it resolves back to the workspace
itself, which would compare the manifests against themselves and pass no matter what; Cairn
refuses that rather than reporting a check it did not perform.

**In the syncer**, as the last line of defence, in case something reached a deployment by a
route that skipped the gate - a direct push, or a manifest edited in place on the server.

A refusal in the syncer never leaves a half-applied change. Every publication is resolved,
fetched and checked in full before a single byte is written, so a rejected plan leaves the
served directory exactly as it was.

## What the gate refuses

On any publication whose baseline `lifecycle` is `published`:

- **removing it**, or removing the whole standard. Those URLs are live.
- **removing an artifact from it.** Same thing, one file down.
- **adding an artifact to it.** This changes what that publication published, retroactively,
  exactly as removing does. The syncer also depends on the refusal: an artifact with no record
  and no file on a published publication is read as one the volume *lost*, not one the manifest
  gained.
- **reverting `lifecycle` to `draft`.** That un-freezes bytes already handed out, and the next
  sync would overwrite them in place.
- **repointing where an artifact comes from** - `path`, `repo`, `ref`, or an inherited
  `source.ref`. Same URL, different bytes.

All of them have the same fix: leave the published thing alone and publish a new one. For
rules that is cheap by design - a new revision costs the schema versions nothing.

Whether the publication is currently served is deliberately not consulted. An earlier version
skipped un-served publications entirely, which let one be un-served in one commit and reverted
to `draft` in the next with every check above never running.

## The cycle that publishes something

A publication does not simply *have* a frozen lifecycle. It *becomes* published, and one sync
cycle is that publication.

On that cycle the recorded lifecycle says `draft` and the manifest says `published`, so the
fast path is skipped, the artifacts are re-fetched at the pinned `ref`, and the provenance is
rewritten to cite it. Every write-once guard is off, because on that cycle the manifest is not
contradicting a promise, it is making one.

You do not need to clear an old draft replica by hand, and you should not: on a deployment the
volume *is* the published record, and `rm -rf` on a directory is indistinguishable from losing
it.

The cycle is reported. `cairn sync` logs `VERSION PUBLISHED` and exits **0**. Expect it once
when you publish something. If you see it when you published nothing, a directory was lost and
has just been rebuilt from its pinned ref - which is worth looking into, because nothing on the
volume can prove the rebuild was intended.

## Verification after the fact

An ordinary sync skips a frozen publication without reading anything, which is what makes it
cheap. So drift under a published URL is caught by the periodic `--verify` pass, which
re-fetches frozen bytes and compares them against the record.

That means the detection window for the frozen corpus is the verify interval, not the sync
interval. On the deployment those default to 24 hours and 6 hours. See
[Running the service](running-the-service.md).

## Procedures

- [Publishing a release](publishing-a-release.md)
- [Publishing a validation-rules revision](publishing-a-rules-revision.md)
