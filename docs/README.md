# Cairn documentation

Guides and reference for running and maintaining Cairn, the durable hosting platform for
preservation standards behind `standards.openpreservation.org`.

New to the project? Start with the [README](../README.md) for what Cairn is and the
[CONTRIBUTING guide](../CONTRIBUTING.md) for adding or updating a standard. The documents
here go deeper on the parts that are easy to get wrong.

## Contents

- **[Concepts and gotchas](concepts-and-gotchas.md)** - the non-obvious behaviour of the URL
  contract, namespaces and content negotiation, lifecycle and serving, freezing, artifact sources,
  validation, caching, and deployment. Read this once if you touch manifests or operate the
  stack.
- **[Promoting a release from draft to stable](promoting-a-draft-release.md)** - the
  step-by-step for freezing a release, including the re-sync trap that silently freezes the
  wrong bytes if you miss it.

## Common tasks

- **Add a new standard** - see the [CONTRIBUTING guide](../CONTRIBUTING.md). It is a small
  YAML edit reviewed as a pull request, no code required.
- **Freeze a release (draft to stable)** - see
  [Promoting a release from draft to stable](promoting-a-draft-release.md).
- **Understand why a URL behaves the way it does** - see
  [Concepts and gotchas](concepts-and-gotchas.md).
- **A pull request failed the write-once check** - the edit would change or remove a URL that
  is already published. See
  [Write-once freezing](concepts-and-gotchas.md#write-once-freezing-and-the-re-sync-trap) for
  what is refused and why, and the [CONTRIBUTING guide](../CONTRIBUTING.md) for how to run the
  same check locally before pushing.
- **A deployment logged `INTEGRITY CHECK FAILED`** - either the upstream bytes behind a frozen
  version no longer match what was recorded, or a file on the volume drifted, vanished or had
  its record damaged and was put back from upstream. Nothing published has been left altered
  either way; see [When a cycle fails](concepts-and-gotchas.md#when-a-cycle-fails), which
  lists every marker and what `cairn sync`'s exit codes mean.
- **A deployment logged `PROVENANCE UNREADABLE` or `UNVERIFIABLE PUBLISHED FILE`** - these two
  do not self-heal and will repeat every cycle. The sync has found a published release it
  cannot establish the truth about, and refuses to guess rather than overwrite the evidence.
  Both need a person and an independent copy of the bytes; see
  [When a cycle fails](concepts-and-gotchas.md#when-a-cycle-fails).

## About this folder

These documents are written to read well both as plain files on GitHub and as a published
site. Two conventions keep that transition easy and natural:

- Links between documents are relative (for example `concepts-and-gotchas.md`), so they work
  in the GitHub file view and in a rendered site without changes.
- References to source files are written as inline code (for example `src/cairn/sync.py`)
  rather than links, so nothing breaks when only the `docs/` folder is published.

To turn this folder into a GitHub Pages site later, enable Pages with the source set to the
`main` branch and the `/docs` folder. This page becomes the site home, and no rewriting of
links is needed. Adding a Jekyll theme or a `_config.yml` is optional and can come later; the
plain Markdown renders fine on its own.
