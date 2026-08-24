# Promoting a release from draft to stable (freezing it)

This is the step-by-step for taking a standard's release from `draft` (auto-tracking a
branch) to a frozen, permanent release. It captures the gotchas that are easy to miss,
because getting this wrong silently freezes the wrong bytes or the wrong provenance.

If you only remember one thing: **flipping `lifecycle` is not enough.** You must also
pin the git ref to a tag, and because the version was already synced as a draft you must
clear its old replica so it re-freezes against that tag.

---

## Background: what "frozen" actually means

Every release has a `lifecycle`, and it has exactly two values:

```text
draft      -> mutable: re-fetched and overwritten on every sync (tracks a branch)
published  -> frozen, write-once, forever
```

This is defined by `Lifecycle` in `src/cairn/manifest.py`. Serving is a separate field
(`served`), so withdrawing a release later does not un-publish it and cannot return it to
`draft`; `cairn validate --baseline` refuses that edit on the pull request. A `draft`
release pointed at a branch is pulled fresh every sync (the deployment syncs every 6h by
default), so the served bytes follow the branch tip. A frozen release records its bytes,
a SHA-256, and provenance once, and from then on sync skips it. If the upstream bytes at
that ref ever change, a `--verify` sync fails loudly instead of silently following.

"Fixed" is not a value. What you want at an official release is `lifecycle: published`, with
`maturity: stable` as the label on the page and `ref:` pinned to the tag.

---

## The two edits that matter

Take EAD 4.0.0 as the worked example. In `standards/ead/standard.yaml`:

```yaml
releases:
  - version: 4.0.0
    lifecycle: published      # was: draft
    maturity: stable          # the badge on the page; no effect on freezing
    ref: EAS-2026-07          # REQUIRED once published: the real upstream tag, never a branch
    released: "2026-07-31"    # optional, good provenance
    artifacts:
      ...
```

Both the tag and the quotes matter. The tag is whatever upstream actually cut, which is rarely
`v<version>`. The quotes are load-bearing: YAML parses a bare `2026-07-31` as a date object,
the manifest schema requires a string, and the unquoted form fails validation.

1. **`lifecycle: draft` -> `lifecycle: published`.** This is what makes the release
   write-once, and it is one-way: `cairn validate --baseline` refuses the reverse on a pull
   request. If you need to stop serving it later, set `served: false`, which leaves the
   promise intact.

2. **Pin `ref` to a tag.** The schema requires this as soon as `lifecycle` is `published`, so
   a promotion without it fails validation rather than shipping. Before promotion the release
   may inherit `source.ref`, which points at a branch (for example `release_2026_07`).
   Freezing against a branch is meaningless, because the branch keeps moving. Add a `ref:` on
   the release; it overrides `source.ref` for that release only.

3. **`maturity` is optional and cosmetic.** It sets the badge. It has no effect on freezing
   or serving, so nothing breaks if you leave it out.

`released:` is optional but worth setting for provenance. It uses `YYYY-MM-DD` format, in
quotes.

### Watch the per-artifact ref overrides

An individual artifact can carry its own `ref`, which overrides everything else. In the
EAD manifest the tag-library PDF does exactly this:

```yaml
      - name: ead-taglibrary.pdf
        role: taglibrary-pdf
        repo: SAA-SDT/EAS-TagLibraries
        ref: master           # <- another moving branch
        path: pdf/EAD4-TL-eng.pdf
```

That `ref: master` is a branch too. If you freeze the schemas but leave this pointing at
`master`, the PDF source is still floating and the release is not fully frozen. Pin this
artifact's `ref` to a tag or a specific commit SHA as well.

Rule of thumb: after editing, scan the whole release for the word `ref` (at
`source`, release, and artifact level) and make sure none of them name a branch.

---

## What happens on the publication cycle

A version that has been running as a `draft` has **already been synced**. Its replica exists on
disk at `site/<id>/v<version>/`, including a `provenance.json` recording bytes fetched from the
branch, which is the wrong source for a published release.

The syncer handles this. A release does not simply *have* a frozen lifecycle, it *becomes*
published, and that cycle is the publication: the record says `draft`, the manifest says
`published`, so the fast path is skipped, the artifacts are re-fetched at the pinned `ref`, and
provenance is rewritten to cite the tag. From the next cycle on the release is frozen and the
write-once guards apply.

You do not need to clear the replica by hand, and you should not: on a deployment the volume is
the published record, and `rm -rf` on a release directory is indistinguishable from losing it.

The publication is reported. `cairn sync` logs `VERSION PUBLISHED` and exits 0, because that is
the one cycle on which the write-once checks do not apply. Expect it when you promote. If you
see it when you promoted nothing, a release directory was lost and has just been rebuilt from
its pinned ref, which is worth looking into.

---

## Quick checklist

- [ ] `lifecycle:` changed from `draft` to `published`
- [ ] release-level `ref:` pinned to a tag (or commit), not a branch (the schema enforces this)
- [ ] every per-artifact `ref:` in the release pinned to a tag or commit, not a branch
- [ ] `maturity: stable` set if you want the badge to say so
- [ ] `released:` date set (optional but recommended)
- [ ] `cairn validate` clean
- [ ] `cairn sync --standard <id>` re-fetched from the tag
- [ ] `provenance.json` checked: `ref`/`url` name the tag, not a branch
- [ ] `cairn build` run
- [ ] PR opened with only the manifest change
- [ ] production volume replica cleared or `--verify` run after merge

---

## Reference

- Lifecycle values: `draft`, `published`. Serving: `served: true|false`. Both belong to
  `Publication` (`src/cairn/manifest.py`), the base a release and a rules revision share, so
  the two cannot drift apart on what freezing means.
- Maturity labels (display only): `alpha`, `beta`, `stable`, `deprecated`
  (`schemas/standard.schema.json`).
- Only `lifecycle: draft` is mutable; `published` is write-once and one-way
  (`src/cairn/manifest.py`, `Lifecycle` and `Publication.is_mutable`). Un-serving a release
  with `served: false` does not un-publish it.
- Ref precedence (most specific wins): artifact `ref` > release `ref` > `source.ref`. Defined
  once in `artifact_locator` (`src/cairn/manifest.py`) and used by both the fetch and the
  write-once check, so the two cannot disagree.
- Freezing is checked in the plan phase (`_plan_publication` in `src/cairn/sync.py`) before
  anything is written, and again on pull requests via `cairn validate --baseline`.
- Everything on this page applies unchanged to a validation-rules revision, which is the other
  kind of publication cairn freezes. Its own runbook is
  [Publishing a validation-rules revision](publishing-a-rules-revision.md).
- General "add or update a standard" guidance is in `CONTRIBUTING.md`.
