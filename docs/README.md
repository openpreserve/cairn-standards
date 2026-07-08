# Cairn documentation

Guides and reference for running and maintaining Cairn, the durable hosting platform for
preservation standards behind `standards.openpreservation.org`.

New to the project? Start with the [README](../README.md) for what Cairn is and the
[CONTRIBUTING guide](../CONTRIBUTING.md) for adding or updating a standard. The documents
here go deeper on the parts that are easy to get wrong.

## Contents

- **[Concepts and gotchas](concepts-and-gotchas.md)** - the non-obvious behaviour of the URL
  contract, namespaces and content negotiation, statuses, freezing, artifact sources,
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
