# Contributing to Cairn

There are two very different kinds of contribution:

1. **Adding or updating a hosted standard** — the common case. No code required; you edit a
   YAML manifest and (optionally) some Markdown. This guide focuses on that.
2. **Changing the tooling** — Python in `src/cairn/`. See the bottom of this file.

---

## Add or update a standard

Everything about a standard lives in one folder:

```
standards/<id>/
├─ standard.yaml         # the manifest (identity, upstream source, releases, artifacts)
└─ content/
   └─ overview.md        # landing-page copy (Markdown)
```

`<id>` is the URL segment (`standards.openpreservation.org/<id>`). Use a short, lowercase,
stable identifier. It must **not** encode the responsible organisation — organisations change;
the identifier should not.

### 1. Write the manifest

Copy an existing `standards/*/standard.yaml` and edit it. The full field reference is the JSON
Schema at [`schemas/standard.schema.json`](schemas/standard.schema.json); most editors will give
you autocompletion and inline validation from it.

Key ideas:

- **`source`** points at where the artifacts really live (a GitHub repo + ref). Cairn fetches
  from there — it does not store schema bytes in this repo.
- **`major_lines[].latest`** is the concrete version that `/<id>/vN` redirects to.
- **`releases[].artifacts[]`** lists the files to replicate for that version. Each artifact has a
  `role` (`schema`, `relaxng`, `nvdl`, `schematron`, `taglibrary-html`, `taglibrary-pdf`, …) that
  drives the RDDL namespace document and the landing-page download list.
- Artifacts can come `from:` `repo` (a path at a git ref), `release-asset` (a GitHub Release
  asset), `github-pages`, or a direct `url`.

### 2. Add the landing-page copy

Put human-readable overview text in `standards/<id>/content/overview.md`. This is rendered into
the standard's landing page. Per-release notes can go in `content/<version>.md`.

### 3. Validate locally

```bash
cairn validate                 # all manifests
cairn sync --standard <id>     # fetch + checksum this standard's artifacts
cairn build                    # render everything
```

`cairn sync` is **write-once**: once a version's artifacts are fetched and checksummed, they are
frozen. If upstream later changes the bytes at a released version, sync will **refuse** and flag
it (re-tagging/tampering). Cut a new version instead.

### 4. Open a pull request

CI validates every manifest and does a dry-run sync (reachability) on your PR. Once merged, the
publish workflow syncs, builds, and deploys. That's the whole burden-reducing point: a reviewed
YAML change ships a standard.

## Versioning rules

- Versions are semantic (`MAJOR.MINOR.PATCH`) and served at `/<id>/vMAJOR.MINOR.PATCH/`.
- `/<id>/vMAJOR` always tracks the latest release in that major line (minor releases are assumed
  backward-compatible).
- The **namespace** URL is major-only (`/<id>/vMAJOR`) and must stay stable across minor/patch
  releases. Never put minor/patch numbers in a namespace.
- Withdrawn releases stay listed but are served as `410 Gone`. We do not delete history.

## Changing the tooling

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Keep the dependency set small and prefer the standard library — this is preservation
infrastructure and needs to keep building for a long time.
