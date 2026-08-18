# Cairn concepts and gotchas

The things about Cairn that are not obvious from a first read, and that bite people when
they are missed. If you maintain manifests or operate the deployment, read this once.

For the specific task of freezing a release, see
[Promoting a release from draft to stable](promoting-a-draft-release.md).

---

## The URL contract has three layers, and they behave differently

For a standard `eaf`, major line `v1`, latest release `v1.0.0`:

| URL | What it is | Cache |
| --- | --- | --- |
| `/eaf` | Human landing page for the standard | short (300s) |
| `/eaf/v1` | Namespace document (major line only). RDDL page, or content-negotiated to the schema | short (300s) |
| `/eaf/v1/eaf.xsd` | `303` redirect to the latest concrete `v1.x.y` file | short (300s) |
| `/eaf/v1.0.0` | Landing page for that exact release | short (300s) |
| `/eaf/v1.0.0/eaf.xsd` | The actual file. Immutable, CORS `*` | 1 year, immutable |

The trap is the difference between `/eaf/v1/...` (major line, a redirect that follows the
latest release) and `/eaf/v1.0.0/...` (an exact, frozen file). They look almost identical
but do completely different things. The nginx rule that does the pin-to-latest redirect
matches `/eaf/v1/<anything>` but deliberately does **not** match `/eaf/v1.0.0/<anything>`,
because after `v1` the exact path has a `.` where the major-line path has a `/`. Keep that
distinction in mind whenever you quote a URL to someone: the major-line form is a moving
pointer, the dotted form is permanent.

## The namespace is major-only, and it content-negotiates

`/eaf/v1` is a single URL that serves two different things depending on who asks:

- A browser (which sends `Accept: text/html,...`) gets the human-readable RDDL namespace
  page.
- A bare XML tool (which sends `Accept: application/xml` with no `text/html`) is
  redirected straight to the current schema file.
- Anything else, including `*/*`, gets the human page.

This is driven by an nginx `Accept`-header map, not by application logic. The reason
`text/html` is checked first is that browsers send both `text/html` and `application/xml`
in the same header; matching `text/html` first ensures a person gets the page and only a
dedicated XML client gets the raw schema.

Two rules follow from this and must never be broken:

- **Never put a minor or patch number in a namespace.** The namespace is the stable
  identity of the schema. It is major-only (`/eaf/v1`) so that a minor release never forces
  consumers to change their namespace. The exact version lives in the schema's own
  `schema-version` attribute and in the `/vX.Y.Z/` release folders, not in the namespace.
- The namespace document is regenerated from whichever release is the major line's
  `latest`, so promoting a new latest release automatically updates what `/eaf/v1` points
  at.

### Gotcha: the `namespace:` override changes the label, not the routing

A major line can carry an optional `namespace:` field to declare a canonical namespace URI
that differs from the default `https://standards.openpreservation.org/<id>/vN`. This only
changes what is **displayed and recorded** (the namespace document text and `catalog.json`).
It does **not** move where the document is served. The RDDL page is still served at
`/<id>/vN`. Use the override only when a standard's real, externally-fixed namespace URI
must be declared, and expect the served path and the declared URI to differ.

## Status values do more than label a release

Two independent fields control freezing and serving, plus a third that controls neither:

| field | values | what it decides |
| --- | --- | --- |
| `lifecycle` | `draft`, `published` | whether the bytes may still change. Moves in one direction only |
| `served` | `true` (default), `false` | whether the URLs answer 200 or 410 |
| `maturity` | `alpha`, `beta`, `stable`, `deprecated` | the badge on the page. No behaviour at all |

Which gives four states:

| `lifecycle` | `served` | meaning | mutable? | URLs |
| --- | --- | --- | --- | --- |
| `draft` | `true` | work in progress, publicly visible | yes | 200 |
| `draft` | `false` | abandoned draft | yes | 410 |
| `published` | `true` | published | no | 200 |
| `published` | `false` | withdrawn from service, promise intact | no | 410 |

Gotchas:

- **Un-serving is not un-publishing.** `served: false` stops the URLs answering; it does not
  make the bytes editable, and it does not let the release be dropped from the manifest. To
  stop serving something, that is the field you want. There is no way back to `draft`.
- **`published` is one-way, and the PR gate enforces it.** `cairn validate --baseline` refuses
  `published -> draft`. This replaced a six-value `status` enum in which `withdrawn` was
  neither mutable nor served, so `stable -> withdrawn -> draft` came back mutable with
  published bytes still on disk. Two orthogonal facts in one enum is what made that reachable.
- **`maturity` is a label and nothing else.** Under the old enum `status: beta` was frozen and
  served, identical to `stable` in every behavioural respect, so an author writing it to mean
  "still moving" got a release the syncer refused to update. Freezing is `lifecycle` now.
- **A published release must pin its own `ref`, and it should be a tag or commit SHA.** The
  schema requires the field; it cannot check what the value points at, because a tag and a
  branch are not distinguishable from the string. Pinning a tag is what makes a published
  release rebuildable from the manifest alone if its directory is ever lost. Pin a branch and
  that property is gone: a lost or restored volume republishes branch-tip bytes under a
  write-once URL and records them as what was published, reporting only `VERSION PUBLISHED`.
  Nothing in cairn can catch that, so it is on review.
- **Withdrawn history is not deleted.** An un-served release stays in the manifest and in the
  listings, and its URLs return `410 Gone` (a deliberate "this existed and is gone" signal),
  not `404`.
- **An un-served published release is dormant.** The syncer does not fetch it, compare it, or
  write to it. Withdrawing is exactly what an operator does when an upstream tag has moved or
  gone, so probing it anyway would fail that whole standard on every verify pass forever, and
  the manifest cannot drop a published release either.
- **A major line's `latest` cannot be an un-served release.** Validation rejects it, because
  `latest` is what the major-line URL resolves to.
- A `draft` release **can** be a major line's `latest` (that is the normal pre-release
  state), so `/eaf/v1` can legitimately resolve to a draft that is still tracking a branch.

## Write-once freezing (and the re-sync trap)

Once a non-draft release is synced, its bytes, SHA-256, and provenance are recorded and the
version is frozen. Later syncs skip it. `cairn sync --verify` re-fetches frozen versions and
fails loudly with `FROZEN VERSION CHANGED` if the upstream bytes behind that ref ever move
(re-tagging or tampering). The fix is always to cut a new version, never to overwrite. In a
deployment this runs on its own every `VERIFY_INTERVAL`; you do not have to remember it.

Freezing is enforced in two places, on purpose:

- **On the pull request**, by `cairn validate --baseline <checkout>`, which compares your
  manifests against the branch you are merging into and refuses anything that would break a
  published URL: removing an artifact or a release, reverting a release to `draft`, or
  repointing where an artifact comes from. This is where a violation should be caught, because
  the person who can fix it is looking at it.
- **In the syncer**, as the last line of defence, in case something reached a deployment
  anyway.

A refusal in the syncer never leaves a half-applied change. Each release is resolved, fetched
and checked in full before a single byte is written, so a rejected plan leaves the served
directory exactly as it was. (Earlier versions checked after writing, which published files
that no provenance record ever mentioned.)

The non-obvious part is what happens when you promote a version that was previously a draft:
the old draft replica is already on disk, so a naive re-sync freezes the stale bytes and the
wrong provenance. This has its own runbook:
[Promoting a release from draft to stable](promoting-a-draft-release.md).

## Ref precedence, and how provenance is pinned

When Cairn resolves where to fetch an artifact from, the git ref is chosen most-specific-first:

```text
artifact.ref  >  release.ref  >  source.ref
```

The same precedence applies to `repo` (an artifact can override the repo entirely, which is
how a tag-library PDF is pulled from a different repository than the schemas).

For provenance, Cairn additionally makes a best-effort call to resolve a branch or tag ref
to the exact commit SHA and records it. That call is never fatal: if GitHub is unreachable
or rate-limits it, the sync still succeeds and just records the ref without a commit. This is
why a `GITHUB_TOKEN` is worth setting (see below), but not strictly required.

## The four artifact source types, and their required fields

Each artifact declares where it comes `from:`. Each form needs different fields, and
`cairn validate` enforces this on top of the JSON Schema:

| `from` | Needs | Fetched from |
| --- | --- | --- |
| `repo` | `path` (and a ref from somewhere) | `raw.githubusercontent.com/<repo>/<ref>/<path>` |
| `github-pages` | `path` | `https://<owner>.github.io/<name>/<path>` |
| `release-asset` | `asset` | a file attached to a GitHub Release |
| `url` | `url` | that absolute URL, verbatim |

Gotchas:

- **`from: repo` needs a ref.** If none of `artifact.ref`, `release.ref`, or `source.ref` is
  set, sync fails with a clear error. There is no default branch assumption.
- **`release-asset` needs a release tag.** It uses `release_tag`, falling back to `release.ref`
  or `source.ref`. The `asset` field is matched against the release's asset names and supports
  globs. If the glob matches nothing, or matches more than one asset, sync fails rather than
  guessing.
- **`github-pages` derives the host from the repo**, so the `repo` (owner/name) must be the
  Pages repo, and `path` is the path within the published site.

## Validation is two layers, and the second one is where surprises live

`cairn validate` runs the JSON Schema first, then a set of semantic consistency checks that
the schema cannot express. A manifest can be perfectly schema-valid and still fail these:

- the `id` must equal the folder name (`standards/<id>/`)
- no duplicate release versions
- every `major_lines[].latest` must name a release that exists, is in that major line, and
  is not withdrawn
- every release's major must have a `major_lines` entry
- no duplicate artifact names within a release
- each artifact must carry the locator field its `from` requires (see the table above)

If `validate` complains about something that "looks fine" against the schema, it is almost
certainly one of these.

There is a third layer that only runs when you ask for it, and it needs a second checkout to
compare against:

```bash
git worktree add /tmp/baseline origin/main
cairn validate --baseline /tmp/baseline
```

This is the write-once check described above. It is not part of a plain `cairn validate`
because it is a property of a *change*, not of a manifest: the same file can be perfectly
valid and still be an illegal edit. CI runs it on every pull request against the base branch.

## Versions are strict three-part semver

A version must be exactly `MAJOR.MINOR.PATCH`, all integers. There is no support for
pre-release or build suffixes: `1.0.0-rc1` is invalid and would also break version sorting,
which parses three integers. To express pre-release maturity, use the `maturity` field
(`alpha`, `beta`, `draft`), not a version suffix.

## Caching: concrete files are effectively permanent

nginx assigns cache headers by URL shape:

- concrete `/<id>/vX.Y.Z/...` files get `max-age=31536000, immutable` (one year)
- everything else, including the namespace document and the pin-to-latest redirects, gets
  `max-age=300` (five minutes)

The implication: a change to a moving pointer (the namespace, or where `/eaf/v1/eaf.xsd`
redirects) propagates to clients within about five minutes, while a concrete versioned file
can be cached for a year. That is safe precisely because concrete files are write-once, so
their bytes never change. All of these are served with `Access-Control-Allow-Origin: *` and
`X-Content-Type-Options: nosniff`, and `OPTIONS` requests short-circuit to `204`.

## Deployment: what updates on its own, and what does not

The stack is three services around two shared volumes. The syncer runs
`validate` then `sync` then `build` on a loop (every six hours by default) and writes into
the volumes; nginx serves those volumes. What that means in practice:

- **Updated content appears on its own.** New or re-synced files, re-rendered landing pages,
  and an updated `catalog.json` are served as soon as the syncer writes them. nginx needs no
  reload for content.
- **New routes need an nginx reload, which happens on change.** A brand-new standard, a new
  major line, or a new `410` for a withdrawal are *routing* changes, and nginx only picks
  those up on reload. The web container watches the generated routes file and reloads within
  `RELOAD_POLL` seconds (60 by default) of it changing, so a new standard is reachable
  shortly after the syncer writes it rather than at the next fixed interval. A routes file
  that fails `nginx -t` is not loaded: the previous config keeps serving and the failure is
  logged, then retried on the next poll once corrected.
- **If nginx dies, the container exits on purpose.** nginx runs as PID 1 via `exec`, so its
  death is the container's exit and `restart: unless-stopped` restarts it. It also handles
  its own signals, and the base image's `STOPSIGNAL SIGQUIT` means `docker compose stop`
  drains connections gracefully instead of timing out and being killed. (Running it as a
  background daemon under a shell loop, as this once did, left a container that Docker
  considered up while nothing was serving, because restart policies react to exit codes, not
  to the healthcheck.)
- **Whether a new standard is even picked up depends on the syncer's manifest source.** In
  git-pull or bind-mount mode the syncer sees new manifests automatically. In the default
  image-baked mode it only knows the standards baked into the image, so a genuinely new
  standard requires a new image (via the publish workflow) and a redeploy.
- **First boot is seeded.** The web container seeds an empty volume from a snapshot baked
  into the image, so the site works immediately, before the syncer's first cycle.
- **Frozen versions are re-checked on their own timer.** Every `VERIFY_INTERVAL` (24h by
  default), and on the first cycle after any restart, the syncer runs `cairn sync --verify`
  instead of a plain sync. This re-fetches frozen artifacts and compares them against the
  recorded SHA-256, because an ordinary sync skips them entirely and would never notice an
  upstream re-tag. The stamp for this lives next to the routes file so it survives restarts.
- **A sync reads the copy it serves before trusting it.** Upstream matching the record says
  nothing about the bytes on the volume, and those are the ones anyone actually downloads. So
  once a sync has fetched an artifact and found upstream still agrees with the record, it
  hashes the served copy too and rewrites the recorded bytes if that has drifted. Note what
  that does and does not cover: a draft is checked on every cycle, but an ordinary sync skips
  a frozen release without reading anything at all, which is exactly what makes it cheap.
  Drift under a published URL is therefore caught on the `--verify` pass, so the detection
  window for the frozen corpus is `VERIFY_INTERVAL` (24h), not `SYNC_INTERVAL` (6h).

### When a cycle fails

A failure is reported per standard, not per run. `cairn sync` replicates every standard it
can, records the ones that failed, and exits non-zero; the loop still runs `cairn build`, so
one broken upstream does not stop the rest of the registry reaching the site.

The same holds one level down: a release that fails does not abandon the other releases of its
standard. That matters most on a `--verify` pass, where the point of the run is to have *read*
every published artifact - abandoning the releases after a failure and still exiting with a
code meaning "ran to the end" had the loop stamp a verification of bytes it never looked at,
suppressing the next attempt for a full interval. Failures within one standard are still
collected and reported together, so it counts as one failed standard however many of its
releases were involved.

`cairn sync`'s exit code answers whether the run *finished*, which is a different question
from whether it found anything:

| code | meaning |
| ---- | ------- |
| 0 | ran to the end, nothing to report |
| 1 | did not finish: unloadable manifest, unhandled fault, killed |
| 3 | ran to the end, and something needs an operator |
| 4 | ran to the end, and one or more standards failed |
| 5 | ran to the end, and *every release it attempted* failed, so nothing was checked |

That distinction is what the verify stamp is written from. A pass that finished counts as a
verification even if it reported problems, so one persistently failing standard cannot make
every cycle re-verify and re-download every other standard forever. A pass that did not
finish is not stamped, because nothing can be concluded about the artifacts it never reached.
Nor is 5: a pass in which every release failed re-read nothing, so recording it as a
verification would suppress the next attempt for a full interval. The unit is the release, not
the standard - a standard with one rotted release and two good ones did read two of them, and
counting standards reported that as having checked nothing, which is what suppressed the stamp
forever.

The table is `cairn sync` only. `cairn validate` is a gate rather than a step in a loop: it
exits 0 or refuses with 1, and a refusal there is a finding, not a crash.

`cairn build` has three outcomes and they are not the same question: 0 rendered cleanly, 3
rendered but reported something (today, only `CONTENT UNREADABLE`), and 1 produced nothing.
Its two consumers treat 3 differently on purpose. The syncer loop reads it through
`BUILD_RC_ATTENTION` and does not call it a failed render, because there the site is live and
current and calling it one sends an operator to check the disk. CI and the image build both
treat it as fatal, because there it is a mis-encoded file arriving on a branch and that is the
cheapest place to fix it. Same code, opposite correct response, which is why the loop reads
the number from `cairn exit-codes` rather than holding an opinion about it.

Every marker is printed by `cairn` itself, so this list and the strings in the log have one
source. They fall into three groups, and the group tells you how urgent it is.

**A manifest edit would have broken a published URL.** The sync refuses and leaves the
published files untouched. Fix the manifest; nothing is wrong with the volume.

- `FROZEN VERSION CHANGED` - the bytes upstream no longer match what was recorded for a
  frozen version. Usually a moved tag. The fix is upstream: cut a new tag, and publish it as
  a new version here.
- `FROZEN VERSION LOST AN ARTIFACT` - the manifest no longer declares a file that is already
  published at a frozen URL. Restore the artifact entry, or publish the change as a new
  version.
- `FROZEN VERSION REPOINTED` - a published release now names a different repo or ref. The
  bytes may be identical, but a released version's recorded origin is part of what was
  published, so the sync will not amend it to follow the manifest.

All three are caught by `cairn validate --baseline <checkout>` on a pull request, which is
where they should be caught; reaching the syncer means the change was merged anyway.

- `VERSION PUBLISHED` - a release became frozen on this cycle, so the write-once checks did
  not apply to it: on the cycle that publishes a version, the manifest is not contradicting a
  promise, it is making one. Expected exactly once per version, when you promote a draft.

  The run exits **0**. `cairn all` runs in the image build stage against an empty document
  root, where every published release publishes, so counting a publication as attention meant
  no image could be built once anything was published. Alert on the marker in the log, not on
  the exit code.

  It is reported because nothing on the volume can prove it was intended. Whether the guards
  run is decided by the lifecycle recorded in `provenance.json`, which lives on the volume those
  guards protect, so a record that has rotted or been edited to `draft` produces this line too
  - and in that case the bytes the release just adopted are whatever upstream served, with no
  check against what was published. If you see this and you did not promote a version, treat
  the release as suspect and compare it against an independent copy.

- `PUBLISHED VERSION UNFROZEN` - `provenance.json` records this version as published, and the
  manifest now says `lifecycle: draft`. That un-freezes bytes already handed out, and the
  next sync would overwrite them in place. Restore the lifecycle, or set `served: false` and
  publish the change as a new one. `cairn validate --baseline` refuses the same edit on a pull
  request; seeing it from the syncer means the change arrived by some route that skipped the
  gate.

- `WRITE-ONCE VIOLATION` - the same three problems seen from that gate rather than from the
  syncer, and the only one of these you should normally meet. It is what `cairn validate
  --baseline` prints on a pull request, listing every edit that would change or remove an
  already-published URL, and it refuses with exit 1 so the branch cannot merge. Nothing has
  been written anywhere; fix the manifest.

- `UPSTREAM UNREACHABLE` - only from `cairn sync --dry-run`, which is what CI runs to check
  that every declared source still answers. The manifest points somewhere that did not
  respond: a repo made private, a renamed branch, a deleted tag, or an outage. Nothing has
  been written, and a real sync would fail on the same artifacts.

**The sync cannot tell what is true and will not guess.** These need a person, do not
self-heal, and repeat every cycle until resolved. They are the most important markers here.

- `PROVENANCE UNREADABLE` - a published release's `provenance.json` is present but cannot be
  parsed. That file is the only record of what was published, so rebuilding it would adopt
  whatever upstream serves now and destroy the evidence in the same run. Restore it from a
  backup, or confirm the bytes on disk against an independent copy and re-publish the version
  deliberately. Drafts are rebuilt instead of refused, and reported under `INTEGRITY CHECK
  FAILED`.
- `UNVERIFIABLE PUBLISHED FILE` - a published file has no recorded checksum and no longer
  matches upstream. Nothing available can say whether the served copy rotted or upstream was
  re-tagged, and the two need opposite responses, so the sync refuses to pick one. Compare
  against an independent copy before doing anything.
- `NO CHECKSUM RECORDED` - a release was about to be written with an artifact carrying no
  checksum, which would put a claim in `SHA256SUMS` that nothing could later verify. The
  release is refused before anything is written.

`PROVENANCE UNAVAILABLE` is the near neighbour of the first of these and is deliberately not
in this group: the record could not be *read at all*, which is usually a mode or a mount
rather than damage to the bytes. The sync repairs the mode if it can, writes nothing, and
retries on the next cycle rather than asking anyone to restore a backup. If it persists,
check ownership on the volume.

**Something on the volume was wrong.** Service is restored automatically where it can be.

- `INTEGRITY CHECK FAILED` - one of: a standard could not be verified; a served file did not
  match its recorded checksum and was rewritten from upstream; a published file had vanished
  and was restored; or a draft's damaged provenance was rebuilt. Where a repair happened the
  published bytes are correct again and service needs no action, but something wrote to files
  nothing should be writing to, and that is worth understanding before it reaches a file
  whose upstream is gone.
- `CORRUPTED FILE(S) RESTORED` and `DAMAGED RECORD(S) REBUILT` - the same two repairs, counted
  on the one-line summary that `cairn sync` prints on stdout, where the `INTEGRITY CHECK
  FAILED` block above says the same thing at length on stderr. They carry no information the
  block does not; they exist so that the count is visible in a log tailing stdout alone.
  Either one appearing means something was repaired, which on its own exits 3 - but a
  standard failing in the same run outranks it, so seeing one of these alongside exit 4 (or 5
  if every release failed) is correct and means both things happened.
- `PERMISSION REPAIR FAILED` - a published file cannot be read by the web server and its mode
  could not be changed, so that URL answers 403 until someone with the right ownership fixes
  it. Usually a volume written by a different uid.
- `CONTENT UNREADABLE` - the optional prose beside a manifest (`content/overview.md`, or a
  release's notes) could not be read, so those pages fell back to their one-line summary. The
  site is live and the URLs all resolve; the pages are just missing their descriptions. Fix
  the file's encoding or its permissions.
- `BUILD FAILED` - printed by the syncer loop, not by `cairn`. The replication succeeded but
  the render did not, so the site is serving its previous state: correct, just not current.
  Almost always the volume being full or mounted read-only. Note that this deliberately does
  not suppress the verification stamp, because whether the render succeeded says nothing
  about whether the artifacts were checked.

  It means the render produced nothing, and only that. A build that finished but reported
  `CONTENT UNREADABLE` exits 3, which the loop reads through `BUILD_RC_ATTENTION` and does not
  treat as a failure - the site is current in that case, and sending an operator to check the
  disk over an encoding problem in one markdown file is a false alarm the loop used to raise.

## DTDs are not supported yet

Cairn serves XSD, RelaxNG, NVDL, Schematron, tag libraries and documentation. It does **not**
yet serve DTDs correctly, which matters because a `DOCTYPE` in an XML header is exactly the
kind of fixed URI this service exists to keep resolving:

```xml
<!DOCTYPE ead SYSTEM "https://standards.openpreservation.org/ead/v2.0.2/ead.dtd">
```

Publish a `.dtd` today and it is served as `application/octet-stream`, because nothing maps the
extension. Some parsers accept that and some refuse it, which is the worst of both.

Four things need changing, and they are small:

1. `util._EXT_MEDIA_TYPES` needs `.dtd`, and almost certainly `.mod` and `.ent` too. A real DTD
   is normally split into modules and entity files that it pulls in by relative reference, so
   serving the DTD without them publishes a document that cannot be resolved. The registered
   media type is `application/xml-dtd` (RFC 3023).
2. `deploy/nginx.conf`'s `types` block maps `xsd rng nvdl sch` and needs the same extensions,
   or nginx will answer with `default_type` regardless of what the manifest says. Add them to
   `gzip_types` as well; DTDs are text and compress well.
3. `schemas/standard.schema.json`'s `role` enum has no `dtd`, so a DTD has to be declared as
   `other` today. That is what drives the RDDL nature and the grouping on the release page.
4. `render.RDDL_NATURE` and `RDDL_PURPOSE` need a `dtd` entry. Without one it falls back to
   `http://rddl.org/natures#resource`, which tells an XML toolchain nothing about what the
   resource is; the nature for a DTD is its media type.

The relative-reference point in (1) is the one to think about before publishing anything: the
modules have to sit beside the DTD under the same version directory and be declared as their
own artifacts, or the DTD resolves at the top and fails one level down. Write-once applies to
each of those URLs individually once the version is not a draft.

Nothing here is blocked; it has simply not been needed. All three standards currently hosted
are schema-based.

## A `GITHUB_TOKEN` is optional but recommended

Public `raw.githubusercontent.com` fetches do not need authentication, but the GitHub API
calls do (release-asset lookups and the best-effort commit pinning), and all GitHub requests
share a rate limit. Setting `GITHUB_TOKEN` (or `GH_TOKEN`) lifts that limit. CI and the
syncer both pass it through. Without it, high-volume or frequent syncs can be throttled.

## rddl.org is referenced over http on purpose

The RDDL namespace documents use `http://rddl.org/` (not `www.`, not `https://`) for their
nature and purpose identifiers. As of mid-2026 the rddl.org TLS certificate is expired and
`www.rddl.org` does not resolve, so plain `http://rddl.org/` is the working, stable form.
These are identifiers, not resources that get fetched at serve time, so http here is about
consistency, not liveness. Do not "fix" them to https without confirming the certificate has
been renewed first.
