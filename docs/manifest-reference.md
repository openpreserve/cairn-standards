# Manifest reference

Everything Cairn serves is described by one file per standard:

```text
standards/<id>/
├─ standard.yaml         # the manifest
└─ content/
   ├─ overview.md        # landing-page copy
   ├─ <version>.md       # optional notes for one release
   └─ schematron-v<major>-<revision>.md   # optional notes for one rules revision
```

The authoritative field list is the JSON Schema at `schemas/standard.schema.json`, and most
editors will give you completion and inline validation from it. This page explains what the
fields mean and which combinations matter.

---

## Identity

```yaml
id: eaf
title: "Encoded Archival Context - Functions (EAC-F)"
summary: >-
  One or two sentences for the landing page and the catalog.
based_on: "International Standard for Describing Functions (ISDF), 2007"   # optional
```

`id` is the URL segment: `standards.openpreservation.org/<id>`. It must match the folder name,
and it must **not** encode the responsible organisation. Organisations change; the identifier
should not.

## Steward and links

```yaml
steward:
  org: "Technical Subcommittee on Encoded Archival Standards (TS-EAS)"
  homepage: "https://www.loc.gov/ead/"
  github: "https://github.com/SAA-SDT/eas-schemas"
  contacts: ["someone@example.org"]        # optional

links:                                     # optional, shown on the landing page
  - label: "Tag Library (browse online)"
    url: "https://saa-sdt.github.io/EAS-TagLibraries/"
```

The steward is displayed and recorded. It never appears in a URL.

## Where the bytes come from

```yaml
source:
  type: github
  repo: SAA-SDT/eas-schemas
  ref: release_2026_07        # the default git ref
```

Cairn fetches from here. It does not store schema bytes in this repository. `source.ref` is
only a default: a release, a rules revision, or an individual artifact can override it.

## Major lines

```yaml
major_lines:
  - major: 1
    latest: 1.0.0
    namespace: "https://example.org/some/other/uri"   # optional
```

One entry per major version line. `latest` is the concrete release that `/<id>/vN` resolves to,
and it must name a release that exists, is in that major line, and is currently served. It may
name a draft; that is the normal state before a release is published.

`namespace` overrides the declared namespace URI for display and for `catalog.json` only. It
does not move where the document is served. See [The URL contract](url-contract.md).

## Releases

```yaml
releases:
  - version: 1.0.0
    lifecycle: published      # draft | published
    maturity: stable          # optional: alpha | beta | stable | deprecated
    served: true              # optional, default true
    ref: EAS-2026-07          # required once published
    released: "2026-07-31"    # optional, quoted
    notes: "…"                # optional, Markdown
    artifacts:
      - name: eaf.xsd
        role: schema
        from: repo
        path: xml-schemas/eaf/eaf.xsd
        title: "EAC-F 1 W3C XML Schema"
```

Versions are strict three-part semver: `MAJOR.MINOR.PATCH`, all integers. There is no support
for pre-release or build suffixes. `1.0.0-rc1` is invalid, and would also break version
sorting, which parses three integers. To express pre-release maturity use `maturity`.

`lifecycle`, `served` and `maturity` are three separate fields doing three separate jobs; what
each decides is in [Lifecycle and freezing](lifecycle-and-freezing.md). The short version:
`lifecycle` decides whether the bytes may still change, `served` decides whether the URLs
answer, and `maturity` is a label with no behaviour at all.

**Quote the dates.** YAML parses a bare `2026-07-31` as a date object, and the schema requires
a string, so the unquoted form fails validation.

## Rules revisions

Validation rules live beside the releases, not inside them:

```yaml
rules:
  - revision: "2026-07"          # dated label; becomes the URL segment
    applies_to: 1                # the major line
    tested_against: 1.0.0        # optional but wanted; from the rules' author
    minimum_version: 1.0.1       # optional; usually absent
    lifecycle: published
    ref: EAS-Schematron-2026-07
    artifacts:
      - name: eaf.sch
        role: schematron
        from: repo
        repo: SAA-SDT/eas-schematron-validator
        path: schematron/eaf.sch
```

They share every field a release has, and mean the same by them. The whole reasoning, the URL
shapes and the procedure are in
[Publishing a validation-rules revision](publishing-a-rules-revision.md).

## Artifacts

Each artifact is one file to replicate and serve.

```yaml
- name: eaf.xsd            # the filename to serve as, under the version folder
  role: schema             # what it is
  from: repo               # where it comes from
  path: xml-schemas/eaf/eaf.xsd
  title: "EAC-F 1 W3C XML Schema"     # optional, shown on the page
  media_type: application/xml         # optional, overrides the extension
```

`role` drives the RDDL nature on the namespace document and the grouping on the landing page.
Valid values: `schema`, `relaxng`, `nvdl`, `schematron`, `taglibrary-html`, `taglibrary-pdf`,
`documentation`, `license`, `other`.

`name` must be a bare filename. It may not begin with a dot, and it may not collide with a file
Cairn generates (`index.html`, `provenance.json`, `SHA256SUMS`) - the sync would write the
artifact and then overwrite it with its own metadata.

### The four source types

| `from` | Also needs | Fetched from |
| --- | --- | --- |
| `repo` | `path`, and a ref from somewhere | `raw.githubusercontent.com/<repo>/<ref>/<path>` |
| `github-pages` | `path` | `https://<owner>.github.io/<name>/<path>` |
| `release-asset` | `asset` | a file attached to a GitHub Release |
| `url` | `url` | that absolute URL, verbatim |

- `repo` needs a ref from somewhere. If none of `artifact.ref`, the publication's `ref`, or
  `source.ref` is set, the sync fails with a clear error. There is no default-branch
  assumption.
- `release-asset` needs a release tag. It uses `release_tag`, falling back to the publication's
  `ref` or `source.ref`. `asset` is matched against the release's asset names and supports
  globs; matching nothing, or more than one, fails rather than guessing.
- `github-pages` derives the host from the repo, so `repo` must be the Pages repo and `path`
  is the path within the published site.

### DTDs are not supported yet

Cairn serves XSD, RELAX NG, NVDL, Schematron, tag libraries and documentation. It does **not**
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
   or nginx answers with `default_type` regardless of what the manifest says. Add them to
   `gzip_types` as well; DTDs are text and compress well.
3. `schemas/standard.schema.json`'s `role` enum has no `dtd`, so a DTD has to be declared as
   `other` today. That is what drives the RDDL nature and the grouping on the release page.
4. `render.RDDL_NATURE` and `RDDL_PURPOSE` need a `dtd` entry. Without one it falls back to
   `http://rddl.org/natures#resource`, which tells an XML toolchain nothing about what the
   resource is; the nature for a DTD is its media type.

The relative-reference point in (1) is the one to settle before publishing anything: the
modules have to sit beside the DTD under the same version directory and be declared as their
own artifacts, or the DTD resolves at the top and fails one level down. Write-once then applies
to each of those URLs individually.

Nothing here is blocked; it has simply not been needed, because every standard hosted so far is
schema-based.

### Ref and repo precedence

Most specific wins:

```text
artifact.ref  >  the publication's ref  >  source.ref
```

The same order applies to `repo`, which is how a tag-library PDF is pulled from a different
repository than the schemas.

This precedence is defined once, in `artifact_locator` (`src/cairn/manifest.py`), and both the
fetch and the write-once check read it from there. That is deliberate: comparing the literal
manifest fields instead would miss a `source.ref` move that a frozen release silently
inherits, which repoints published bytes without the release block changing at all.

**When you pin a release, scan the whole block for the word `ref`** - at `source`, at the
release, and at every artifact - and make sure none of them still names a branch. An artifact
that keeps its own `ref: master` leaves that file floating while everything around it looks
frozen.

## Provenance

For each artifact, Cairn additionally resolves the branch or tag to an exact commit SHA and
records it. That call is best-effort: if GitHub is unreachable or rate-limits it, the sync
still succeeds and records the ref without a commit. Setting a `GITHUB_TOKEN` makes it more
reliable and raises the rate limit, but it is not required.

## What validation checks

`cairn validate` runs the JSON Schema first, then consistency checks the schema cannot express.
A manifest can be perfectly schema-valid and still fail these:

- `id` matches the folder name
- no duplicate release versions
- every `major_lines[].latest` names a release that exists, is in that major line, and is served
- every release's major has a `major_lines` entry
- no duplicate artifact names within one release or revision
- each artifact carries the locator field its `from` requires
- each artifact name is a bare filename and is not one Cairn generates
- a rules revision's `applies_to` has a `major_lines` entry, and no two revisions share a major
  line and a label
- a revision's `tested_against` and `minimum_version` each name a release of this standard in
  that same major line, and `tested_against` is not below `minimum_version`

If `validate` complains about something that looks fine against the schema, it is one of these.

There is a third layer, run on pull requests, that compares your manifests against the branch
you are merging into and refuses edits that would break an already-published URL. It needs a
second checkout, and it is described in [Lifecycle and freezing](lifecycle-and-freezing.md).

## Prose beside the manifest

`content/overview.md` is rendered into the standard's landing page. `content/<version>.md` and
`content/schematron-v<major>-<revision>.md` are optional notes for one publication; the `notes:`
field in the manifest does the same job for shorter text.

All of it is optional. A file that cannot be read does not fail the build: the page falls back
to the one-line summary, and the run reports `CONTENT UNREADABLE` so the omission is not silent.
