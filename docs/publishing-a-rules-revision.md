# Publishing a validation-rules revision

Schematron rules are published on their own track, separately from the schema versions they
validate. This is the step-by-step for adding a revision, together with the reasoning behind
the shape, because the shape is what makes the rest of it straightforward.

If you only remember one thing: **a rules revision is not part of a release.** It hangs off
the major line, it has its own dates, and it can be added to a standard whose releases are
frozen for ever.

---

## Why they are not inside the release

A published release is write-once, and that includes gaining a file: `compare_to_baseline` in
`src/cairn/manifest.py` refuses adding an artifact to a published release, because what a
version publishes is fixed once it is published.

So rules living inside a release could never be revised. Every rules change would need a new
schema version number for a schema that had not changed. Rules move on their own cadence, so
that is the wrong shape.

They also belong to the major line rather than to a version because that is what the files
themselves say. Every `.sch` declares the namespace it applies to:

```xml
<ns prefix="ead" uri="https://standards.openpreservation.org/ead/v4"/>
```

That is `v4`, the namespace, not `v4.0.0`. Publishing the rules under a concrete version would
contradict the file, and would mean republishing identical rules under every later patch
release.

## The URLs

For a standard `ead`, major line `v4`, rules revision `2026-07`:

| URL | What it is | Cache |
| --- | --- | --- |
| `/ead/v4/schematron/2026-07` | Page for that revision: files, checksums, provenance | short (300s) |
| `/ead/v4/schematron/2026-07/ead.sch` | The rules themselves. Frozen permanently | 1 year, immutable |
| `/ead/v4/schematron/latest` | `303` to the newest frozen revision | short (300s) |
| `/ead/v4/schematron/latest/ead.sch` | `303` to that revision's file | short (300s) |

`latest` is a generated redirect, never a directory in the document root. That is what keeps
the store write-once: making `latest` mean something new never rewrites a published file.

Which URL to hand out depends on what the reader means:

- **`latest`** wherever you mean "the current rules": documentation, a tag library, a web page.
- **The dated form** wherever the exact rules matter: audit records, batch-processing logs,
  anything that has to state precisely what was validated against. Old revisions never
  disappear.

## What goes in the manifest

Under `rules:` at the top level of `standards/<id>/standard.yaml`, beside `releases:` rather
than inside it:

```yaml
rules:
  - revision: "2026-07"          # dated label; becomes the URL segment
    applies_to: 4                # the major line, matching the .sch namespace declaration
    tested_against: 4.0.0        # the exact release its author tested it against
    lifecycle: published         # write-once from here on
    ref: EAS-2026-07             # REQUIRED once published: a tag or commit SHA, never a branch
    released: "2026-07-26"       # optional, good provenance
    artifacts:
      - name: ead.sch
        role: schematron
        from: repo
        repo: SAA-SDT/eas-schematron-validator
        path: schematron/ead.sch
        title: "EAD 4 Schematron rules"
```

Every field means exactly what it means on a release, and the same rules apply: `lifecycle` is
one-way, `served: false` withdraws without un-publishing, a published revision must pin its own
`ref`, and per-artifact `ref` overrides the revision's.

Three fields are specific to this track:

- **`revision`** is a dated label, `YYYY-MM` or `YYYY-MM-DD`, and it is checked as a real
  calendar date rather than merely a shape. It is not free-form because "newest" has to be
  decidable by sorting: that is what the `latest` pointer resolves by, and `2026-13` would sort
  above every real revision of that year and hold the pointer until 2027. The rule also keeps
  the word `latest` out of the revision space, so a revision can never shadow the pointer.
- **`applies_to`** is the major line, and must match the namespace the `.sch` declares. Cairn
  cannot read that out of the file: each `.sch` declares three namespaces and nothing in the
  file marks which is the subject, so the manifest states it and the file corroborates it.
- **`tested_against`** is the exact schema version the rules' author wrote and tested the
  revision against. Only they know it, so it is recorded from what they tell us and displayed
  beside the checksums. Nothing is gated on it. It must name a release of this standard in the
  same major line, so a typo is refused rather than published as a fact - and once this revision
  is `published`, that release must be published too. A draft is re-fetched from a branch every
  cycle, so its version number does not identify any particular bytes; publishing a permanent
  claim about it would be publishing a claim about nothing in particular. A draft revision may
  cite a draft release, because it is exactly as provisional.

And one that is usually absent:

- **`minimum_version`** - the earliest release in the line these rules make sense against.
  Omit it unless a revision genuinely depends on something introduced part-way through the
  major line. Rules target the namespace, and the namespace is major-only, so the normal
  answer is "the whole line". When it is set it must name a release in the same major line and
  must not be above `tested_against`.

## Two ways to get a revision onto the site

The same choice a release has:

1. **Following a branch.** `lifecycle: draft`, no `ref` needed. Re-fetched and overwritten
   every sync, so the site tracks the branch tip on its own. Good while a set of rules is still
   settling.
2. **Pinned to a tag.** `lifecycle: published` with `ref:` naming a tag or commit SHA. Frozen
   permanently, safe to cite.

A revision can start as the first and be promoted to the second, which is an ordinary manifest
edit: repointing a draft is allowed, and the cycle that publishes it re-fetches at the pin.

**A draft revision never becomes `latest`.** The pointer resolves only to a revision that is
published and served. A major line's `latest` for releases may point at a draft, but that
target is written down by a person in `major_lines`; this one is derived by sorting, so a draft
added to track a branch would capture the pointer with nobody deciding that it should. Until a
line has frozen its first revision there are no current rules to cite, `latest` answers 404,
and the drafts stay reachable at their own dated URLs.

## Checklist

- [ ] the `.sch` declares the namespace of the major line you are putting it under
- [ ] `revision:` is a real date and does not already exist for that major line
- [ ] `applies_to:` has a matching `major_lines` entry
- [ ] `tested_against:` confirmed with the rules' author, not guessed, and already published
- [ ] `minimum_version:` set only if the rules genuinely do not apply to the whole line
- [ ] `lifecycle: published` and `ref:` pinned to a tag or commit, not a branch
- [ ] every per-artifact `ref:` pinned too
- [ ] `cairn validate` clean
- [ ] `cairn validate --baseline <checkout of main>` clean
- [ ] `cairn sync --standard <id>` fetched the revision at the pinned ref
- [ ] `provenance.json` under the revision names the tag, not a branch
- [ ] `cairn build` run, and `/<id>/vN` lists the revision
- [ ] the deployment has been redeployed if `deploy/nginx.conf` changed, not merely re-synced

## What happens on the cycle that publishes it

Exactly what happens for a release, because it is the same code: `cairn sync` logs
`VERSION PUBLISHED` once and exits 0. From the next cycle on the revision is frozen and every
write-once guard applies to it. Seeing that marker when you published nothing means a
directory was lost and has been rebuilt from its pinned ref, which is worth looking into.

## How the routing is put together

You do not need this to publish a revision. It is here because the ordering is a real
constraint on anyone changing the route generator.

The rules locations are generated **before** the pin-to-latest redirect for their major line.
nginx tries regex locations in declaration order and takes the first that matches, and

```nginx
location ~ "^/ead/v4/(?<cairn_rest>.+)$" { return 303 /ead/v4.0.0/$cairn_rest; }
```

matches `/ead/v4/schematron/2026-07/ead.sch` too. Emitted after it, every rules URL answers a
redirect into a version directory that has never held rules - a `303` to a `404`, which is not
the kind of failure anyone spots in a log.

`src/cairn/nginx.py` emits them in the right order and
`test_rules_routes_are_declared_before_the_pin_to_latest_redirect` holds them there. This is
the second place in the codebase to depend on nginx's declaration order; `deploy/nginx.conf`
carries the first, for the dotfile guard, and both are pinned by tests rather than by care.

The cache map in `deploy/nginx.conf` needs the same care in the same direction. A rules path
carries a bare major (`v4`), not a dotted version, so it matches neither release entry and has
its own; `latest` sits at the same depth as a revision, so it is matched *before* the immutable
rule, or the pointer every citation uses would be cached for a year. A draft revision is kept
short-lived by the generated rule described in
[The URL contract](url-contract.md#caching).

## Reference

- `RULES_SEGMENT` and `LATEST_SEGMENT` in `src/cairn/config.py` - the two path segments, named
  once because the manifest, the routes, the redirect and the cache map all have to agree.
- `RuleSet` and `Publication` in `src/cairn/manifest.py` - a revision and a release share one
  base, so the syncer, the write-once gate and the render treat them identically.
- `Standard.latest_rules` in `src/cairn/manifest.py` - what the moving pointer resolves to.
- `compare_to_baseline` in `src/cairn/manifest.py` - the write-once gate, which runs over
  revisions and releases alike.
- General freezing behaviour is in [Lifecycle and freezing](lifecycle-and-freezing.md), and it
  applies here unchanged. The schema-version equivalent of this page is
  [Publishing a release](publishing-a-release.md).
