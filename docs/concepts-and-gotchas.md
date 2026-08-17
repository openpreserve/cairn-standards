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

`status` controls both freezing and serving:

| status | Frozen? | Served as |
| --- | --- | --- |
| `draft` | No, re-synced every run (tracks a branch) | Normal (200) |
| `stable`, `beta`, `alpha` | Yes, write-once | Normal (200) |
| `deprecated` | Yes, write-once | Normal (200), just labelled |
| `withdrawn` | Yes, write-once | `410 Gone`, but still listed |

Gotchas:

- **`deprecated` is still served.** Only `withdrawn` returns `410`. Deprecating something
  does not take it offline; it is a label.
- **Withdrawn history is not deleted.** A withdrawn release stays in the manifest and in the
  listings, and its URLs return `410 Gone` (a deliberate "this existed and is gone" signal),
  not `404`.
- **A major line's `latest` cannot be a withdrawn release.** Validation rejects it, because
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
which parses three integers. To express pre-release maturity, use the `status` field
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

### When a cycle fails

A failure is reported per standard, not per run. `cairn sync` replicates every standard it
can, records the ones that failed, and exits non-zero; the loop still runs `cairn build`, so
one broken upstream does not stop the rest of the registry reaching the site.

Two errors mean a write-once promise was about to be broken, and both leave the published
files untouched:

- `FROZEN VERSION CHANGED` - the bytes upstream no longer match what was recorded for a
  frozen version. Usually a moved tag. The fix is upstream: cut a new tag, and publish it as
  a new version here.
- `FROZEN VERSION LOST AN ARTIFACT` - the manifest no longer declares a file that is already
  published at a frozen URL. Restore the artifact entry, or publish the change as a new
  version. `cairn validate --baseline <checkout>` catches this on a pull request, which is
  where it should be caught; reaching the syncer means it was merged.

A failing verify is logged with an `INTEGRITY CHECK FAILED` marker and is deliberately not
stamped, so the next cycle retries rather than waiting a full interval. That marker is the
one worth alerting on.

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
