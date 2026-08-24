# Publishing a release

The procedure for taking a release from `draft`, where it tracks a branch, to `published`,
where its bytes are frozen permanently. This is what you do when an upstream body announces an
official release.

It is a manifest edit, a pull request, and one sync cycle. The care goes into the refs.

If you only remember one thing: **flipping `lifecycle` is not enough.** Every ref in that
release has to be pinned as well, including the ones it inherits and the ones an individual
artifact overrides.

For what `draft` and `published` actually mean, see
[Lifecycle and freezing](lifecycle-and-freezing.md).

---

## 1. Edit the manifest

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

Four things to get right:

1. **`lifecycle: draft` → `lifecycle: published`.** This is what makes the release write-once,
   and it is one-way. If you need to stop serving it later, set `served: false`, which leaves
   the promise intact.

2. **Pin `ref` to a tag.** The schema requires this as soon as `lifecycle` is `published`, so a
   promotion without it fails validation rather than shipping. Before promotion the release may
   inherit `source.ref`, which points at a branch. Freezing against a branch is meaningless,
   because the branch keeps moving. The tag is whatever upstream actually cut, which is rarely
   `v<version>`.

3. **Quote the date.** YAML parses a bare `2026-07-31` as a date object and the schema requires
   a string, so the unquoted form fails validation.

4. **`maturity` is optional and cosmetic.** It sets the badge and has no effect on freezing or
   serving.

### Check the per-artifact refs

An individual artifact can carry its own `ref`, which overrides everything else. In the EAD
manifest the tag-library PDF does exactly this:

```yaml
      - name: ead-taglibrary.pdf
        role: taglibrary-pdf
        repo: SAA-SDT/EAS-TagLibraries
        ref: master           # <- another moving branch
        path: pdf/EAD4-TL-eng.pdf
```

`ref: master` is a branch too. Freeze the schemas and leave this pointing at `master`, and the
PDF source is still floating while everything around it looks frozen. Pin it to a tag or a
commit SHA as well.

**Rule of thumb:** after editing, scan the whole release for the word `ref` - at `source`, at
the release, and at every artifact - and make sure none of them names a branch.

## 2. Check it locally

```bash
cairn validate
cairn sync --standard ead        # re-fetches at the pinned ref
cairn build
```

Then read `site/ead/v4.0.0/provenance.json` and confirm that every recorded `ref` and `url`
names the tag rather than a branch. That file is what the release will be citing permanently.

Run the write-once gate the way CI will:

```bash
git worktree add /tmp/baseline origin/main
cairn validate --baseline /tmp/baseline
```

## 3. Open a pull request

Manifest changes only. CI validates every manifest, runs the write-once check against the base
branch, runs the tests, and does a dry-run sync for reachability.

Worth stating in the pull request, because a reviewer cannot see it otherwise: that you
compared the bytes at the pinned tag against what the site is serving from the branch today,
and whether anything anyone has already downloaded changes. Usually nothing does, and only the
provenance moves.

## 4. What happens after the merge

A release that ran as a `draft` has already been synced, and its replica is on disk with a
`provenance.json` recording bytes fetched from a branch. The syncer handles that: the record
says `draft`, the manifest says `published`, so the fast path is skipped, the artifacts are
re-fetched at the pinned `ref`, and the provenance is rewritten to cite the tag.

You do not need to clear the old replica by hand, and you should not: on a deployment the
volume *is* the published record, and `rm -rf` on a release directory is indistinguishable from
losing it.

Expect `VERSION PUBLISHED` once in the log, and exit 0. Then confirm the deployment agrees:

```bash
curl -s https://standards.openpreservation.org/ead/v4.0.0/provenance.json | grep -E '"ref"|"lifecycle"'
```

From then on ordinary syncs skip the release entirely, and the periodic `--verify` pass is what
would catch an upstream re-tag.

## Checklist

- [ ] `lifecycle:` changed from `draft` to `published`
- [ ] release-level `ref:` pinned to a tag or commit, not a branch
- [ ] every per-artifact `ref:` in the release pinned too
- [ ] `maturity: stable` set if you want the badge to say so
- [ ] `released:` set, in quotes
- [ ] `cairn validate` clean
- [ ] `cairn validate --baseline <checkout of main>` clean
- [ ] `cairn sync --standard <id>` re-fetched from the tag
- [ ] `provenance.json` names the tag, not a branch
- [ ] `cairn build` run
- [ ] pull request contains the manifest change and nothing else

## Reference

- Lifecycle values, serving, and what the write-once gate refuses:
  [Lifecycle and freezing](lifecycle-and-freezing.md).
- Ref precedence and the artifact source types: [Manifest reference](manifest-reference.md).
- Freezing is checked in the plan phase (`_plan_publication` in `src/cairn/sync.py`) before
  anything is written, and again on pull requests via `cairn validate --baseline`.
- Publishing Schematron rules follows the same pattern on its own track:
  [Publishing a validation-rules revision](publishing-a-rules-revision.md).
