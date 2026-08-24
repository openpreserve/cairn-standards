# Contributing to Cairn

There are two very different kinds of contribution:

1. **Adding or updating a hosted standard** - the common case. No code required; you edit a
   YAML manifest and (optionally) some Markdown. This guide focuses on that.
2. **Changing the tooling** - Python in `src/cairn/`. See the bottom of this file.

---

## Add or update a standard

Everything about a standard lives in one folder:

```text
standards/<id>/
├─ standard.yaml         # the manifest (identity, upstream source, releases, rules, artifacts)
└─ content/
   └─ overview.md        # landing-page copy (Markdown)
```

`<id>` is the URL segment (`standards.openpreservation.org/<id>`). Use a short, lowercase,
stable identifier. It must **not** encode the responsible organisation - organisations change;
the identifier should not.

### 1. Write the manifest

Copy an existing `standards/*/standard.yaml` and edit it. The full field reference is the JSON
Schema at [`schemas/standard.schema.json`](schemas/standard.schema.json); most editors will give
you autocompletion and inline validation from it.

Key ideas:

- **`source`** points at where the artifacts really live (a GitHub repo + ref). Cairn fetches
  from there - it does not store schema bytes in this repo.
- **`major_lines[].latest`** is the concrete version that `/<id>/vN` redirects to.
- **`releases[].artifacts[]`** lists the files to replicate for that version. Each artifact has a
  `role` (`schema`, `relaxng`, `nvdl`, `schematron`, `taglibrary-html`, `taglibrary-pdf`, …) that
  drives the RDDL namespace document and the landing-page download list.
- **`rules[]`** is the separate track for validation rules: dated Schematron revisions served
  under a major line at `/<id>/vN/schematron/<revision>/`, on their own cadence. They are not
  inside `releases[]` on purpose - a published release may never gain an artifact, so rules
  stored there could not be revised without minting a new schema version for a schema that had
  not changed. See [docs/publishing-a-rules-revision.md](docs/publishing-a-rules-revision.md).
- Artifacts can come `from:` `repo` (a path at a git ref), `release-asset` (a GitHub Release
  asset), `github-pages`, or a direct `url`.

### 2. Add the landing-page copy

Put human-readable overview text in `standards/<id>/content/overview.md`. This is rendered into
the standard's landing page. Per-release notes can go in `content/<version>.md`, and notes for a
rules revision in `content/schematron-v<major>-<revision>.md`.

### 3. Validate locally

```bash
cairn validate                 # all manifests
cairn sync --standard <id>     # fetch + checksum this standard's artifacts
cairn build                    # render everything
```

`cairn sync` is **write-once**: once a version's artifacts are fetched and checksummed, they are
frozen. If upstream later changes the bytes at a released version, sync will **refuse** and flag
it (re-tagging/tampering). Cut a new version instead.

A refusal never leaves the served directory half-changed. Each release is planned in full and
checked before anything is written, so a rejected edit changes nothing on disk.

### 4. Open a pull request

CI validates every manifest, checks your changes against the base branch for anything that
would break an already-published URL, runs the unit tests, and does a dry-run sync
(reachability). Once merged, the publish workflow builds and pushes both deployment images.
That's the whole burden-reducing point: a reviewed YAML change ships a standard.

The write-once check is the one most likely to stop you, and it is deliberate. Against the base
branch, these are refused on anything already published (any release or rules revision that is
not `draft`):

- removing an artifact, or removing the release or revision itself - those URLs are live
- adding an artifact to one - that changes what it published, retroactively
- reverting `lifecycle` to `draft` - that would let later syncs overwrite published bytes
- repointing where an artifact comes from (`path`, `repo`, `ref`, or an inherited `source.ref`)
  - same URL, different bytes

All of them have the same fix: leave the published one alone and add a new one. For rules, that
is cheap by design: a new revision costs the schema versions nothing. Run it
yourself before pushing:

```bash
git worktree add /tmp/baseline origin/main
cairn validate --baseline /tmp/baseline
```

Put the baseline outside the working copy, as above. A path inside it resolves back to the
workspace itself, which would compare the manifests against themselves and pass no matter
what; `cairn validate` refuses that rather than reporting a check it did not perform.

## Versioning rules

- Versions are semantic (`MAJOR.MINOR.PATCH`) and served at `/<id>/vMAJOR.MINOR.PATCH/`.
- `/<id>/vMAJOR` always tracks the latest release in that major line (minor releases are assumed
  backward-compatible).
- The **namespace** URL is major-only (`/<id>/vMAJOR`) and must stay stable across minor/patch
  releases. Never put minor/patch numbers in a namespace.
- Withdrawn releases stay listed but are served as `410 Gone`. We do not delete history.
- Rules revisions are dated (`YYYY-MM`), not semantic, and are attached to a major line rather
  than to a version. `/<id>/vMAJOR/schematron/latest` always resolves to the newest frozen one.

Promoting a release from `draft` (tracking a branch) to `stable` (frozen) has a re-sync
trap that can silently freeze the wrong bytes. Follow the runbook:
[docs/promoting-a-draft-release.md](docs/promoting-a-draft-release.md).

## Further reading

More detailed guides live in [docs/](docs/README.md):

- [Concepts and gotchas](docs/concepts-and-gotchas.md) - the non-obvious behaviour of the
  URL contract, namespaces and content negotiation, lifecycle and serving, freezing, artifact sources,
  validation, caching, and deployment.
- [Promoting a release from draft to stable](docs/promoting-a-draft-release.md) - freezing a
  release safely.
- [Publishing a validation-rules revision](docs/publishing-a-rules-revision.md) - the separate
  track for Schematron, which moves without the schemas moving.

## Changing the tooling

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Keep the dependency set small and prefer the standard library - this is preservation
infrastructure and needs to keep building for a long time.
