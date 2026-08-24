# Cairn documentation

Cairn is the tooling behind [`standards.openpreservation.org`](https://standards.openpreservation.org):
it replicates preservation standards from upstream repositories into an integrity-checked,
write-once store and serves them under permanent URLs.

New here? The [project README](../README.md) is the overview. The
[contributing guide](../CONTRIBUTING.md) is how to add or update a standard. These pages are
the detail.

## Reference

How the thing behaves. Read these once if you maintain manifests or operate the deployment.

- **[The URL contract](url-contract.md)** - every URL Cairn serves, what each one does, the
  namespace document and its content negotiation, and which addresses are permanent.
- **[Manifest reference](manifest-reference.md)** - every field in `standard.yaml`, the four
  artifact source types, how refs are resolved, and what validation checks.
- **[Lifecycle and freezing](lifecycle-and-freezing.md)** - `draft` against `published`, what
  withdrawing does, where the write-once promise is enforced and which edits it refuses.
- **[Running the service](running-the-service.md)** - the deployment: what the services are,
  what updates on its own, and what needs a rebuild.

## Procedures

Step by step, with a checklist at the end.

- **[Publishing a release](publishing-a-release.md)** - taking a schema version from `draft`,
  tracking a branch, to frozen permanently.
- **[Publishing a validation-rules revision](publishing-a-rules-revision.md)** - Schematron
  moves on its own cadence, so it is published on its own track beside the schema versions.

## Look-up

- **[Operator reference](operator-reference.md)** - `cairn sync`'s exit codes, and every marker
  it can print with what it means and what to do about it.

## Where to start, by task

| I want to… | Go to |
| --- | --- |
| add a standard to the site | [contributing guide](../CONTRIBUTING.md) |
| freeze a release at an official version | [Publishing a release](publishing-a-release.md) |
| publish or revise Schematron rules | [Publishing a validation-rules revision](publishing-a-rules-revision.md) |
| understand why a URL behaves as it does | [The URL contract](url-contract.md) |
| know what a field in `standard.yaml` means | [Manifest reference](manifest-reference.md) |
| find out why my pull request was refused | [Lifecycle and freezing](lifecycle-and-freezing.md#what-the-gate-refuses) |
| work out what a marker in the log means | [Operator reference](operator-reference.md#markers) |
| deploy, or change how the service runs | [Running the service](running-the-service.md) |

## Two situations worth knowing before you meet them

**A pull request failed the write-once check.** The edit would change or remove a URL that is
already published. Nothing has been written anywhere. What is refused and why is in
[Lifecycle and freezing](lifecycle-and-freezing.md#what-the-gate-refuses); the fix is always to
leave the published thing alone and publish a new one.

**A deployment logged `PROVENANCE UNREADABLE` or `UNVERIFIABLE PUBLISHED FILE`.** These two do
not self-heal and repeat every cycle. The sync has found something published that it cannot
establish the truth about, and refuses to guess rather than overwrite the evidence. Both need a
person and an independent copy of the bytes. See the
[Operator reference](operator-reference.md#the-sync-cannot-tell-what-is-true-and-will-not-guess).

## About this folder

These documents read well both as files on GitHub and as a published site. Two conventions keep
that true:

- Links between documents are relative, so they work in the GitHub file view and in a rendered
  site without changes.
- References to source files are written as inline code (`src/cairn/sync.py`) rather than
  links, so nothing breaks when only `docs/` is published.

To turn this folder into a GitHub Pages site, enable Pages with the source set to the `main`
branch and the `/docs` folder. This page becomes the site home and no links need rewriting.
