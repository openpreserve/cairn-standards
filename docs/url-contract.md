# The URL contract

Cairn exists to make a small number of URLs resolve, correctly, for a very long time. This
page is what those URLs are, what each one does, and what a reader may assume about it.

Everything here is served by nginx from static files. There is no application logic at request
time, so a URL keeps behaving the way this page describes even when the tooling is idle.

---

## The shapes

For a standard `eaf`, major line `v1`, latest release `v1.0.0`, rules revision `2026-07`:

| URL | What it is | Cache |
| --- | --- | --- |
| `/` | Registry index of every hosted standard | 300s |
| `/eaf` | Landing page for the standard: versions, steward, links, downloads | 300s |
| `/eaf/v1` | Namespace document for the major line | 300s |
| `/eaf/v1/eaf.xsd` | `303` to the same file under the latest `v1.x.y` release | 300s |
| `/eaf/v1.0.0` | Page for that exact release: files, checksums, provenance | 300s |
| `/eaf/v1.0.0/eaf.xsd` | The schema itself | 1 year, immutable |
| `/eaf/v1.0.0/SHA256SUMS` | Checksums for that release, `sha256sum -c` compatible | 300s |
| `/eaf/v1.0.0/provenance.json` | Where each file came from, and when | 300s |
| `/eaf/v1/schematron/2026-07` | Page for that rules revision | 300s |
| `/eaf/v1/schematron/2026-07/eaf.sch` | The rules themselves | 1 year, immutable |
| `/eaf/v1/schematron/latest/eaf.sch` | `303` to the newest frozen revision's file | 300s |
| `/catalog.json` | The whole registry as machine-readable JSON | 300s |
| `/sitemap.xml` | Every page above | 300s |

Every response carries `Access-Control-Allow-Origin: *` and `X-Content-Type-Options: nosniff`.
`OPTIONS` short-circuits to `204`. Schema serialisations are served as `application/xml`.

## Two kinds of URL, and the difference matters

Read the table again and there are only two kinds of thing in it.

**Moving pointers.** `/eaf/v1`, `/eaf/v1/eaf.xsd`, `/eaf/v1/schematron/latest/eaf.sch`. These
resolve to whatever is current. They are cached for five minutes, so a change reaches readers
within about that. Use them in documentation, in a tag library, anywhere you mean "the current
one".

**Permanent addresses.** `/eaf/v1.0.0/eaf.xsd`, `/eaf/v1/schematron/2026-07/eaf.sch`. The bytes
at these never change. They are cached for a year and marked `immutable`, so a client that has
fetched one may never ask again. Use them in audit records, in batch-processing logs, anywhere
you need to state exactly what something was validated against.

The two are easy to confuse because they look alike. `/eaf/v1/…` is a pointer and `/eaf/v1.0.0/…`
is permanent, and the only visible difference is a dot where the other has a slash. When you
quote a URL to someone, be deliberate about which you are handing over.

## The namespace document

`/eaf/v1` is one URL that serves two things depending on who asks.

- A browser sends `Accept: text/html,…` and gets a readable page describing the namespace.
- A bare XML tool sends `Accept: application/xml` with no `text/html`, and is redirected
  straight to the current schema.
- Anything else, `*/*` included, gets the readable page.

That page is also a machine-readable [RDDL](https://www.w3.org/2001/tag/doc/nsDocuments/)
directory: each entry is an `rddl:resource` naming what the resource is and why it relates to
the namespace. It is generated from whichever release the major line names as `latest`, so
publishing a new latest release updates it automatically.

Two consequences follow, and both are firm:

- **A namespace is major-only.** `/eaf/v1`, never `/eaf/v1.0.0`. The namespace is the stable
  identity of the schema; a minor release must not force every consumer to change it. The
  exact version lives in the schema's own `schema-version` attribute and in the `/vX.Y.Z/`
  release folders.
- **The negotiation is nginx's, not the application's.** It is an `Accept`-header map.
  `text/html` is tested first because browsers send both `text/html` and `application/xml` in
  one header, and a person should get the page.

A major line may declare an explicit `namespace:` in the manifest when the standard's real,
externally-fixed namespace URI differs from the default. That changes what is **displayed and
recorded** only. The document is still served at `/<id>/vN`, and expect the two to differ.

### rddl.org is referenced over http on purpose

The namespace documents use `http://rddl.org/` - not `www.`, not `https://` - for their nature
and purpose identifiers. As of mid-2026 the rddl.org TLS certificate is expired and
`www.rddl.org` does not resolve, so plain `http://rddl.org/` is the working, stable form. These
are identifiers rather than resources fetched at serve time, so the scheme here is about
consistency, not liveness. Do not change them to https without confirming the certificate has
been renewed first.

## Schemas and rules are two separate tracks

A schema version is frozen for ever once published. Validation rules are revised on a cadence
of their own, so they are published beside the versions rather than inside them: dated,
attached to the major line the `.sch` file itself declares, each revision frozen once
published, with a moving `latest` pointer beside them.

Revising the rules moves no schema version, and no revision ever disappears. The full picture
is in [Publishing a validation-rules revision](publishing-a-rules-revision.md).

## Withdrawn URLs answer 410, not 404

A release or revision that is no longer served stays in the manifest and stays listed, and its
URLs return `410 Gone`. That is a deliberate "this existed and has been withdrawn" signal,
which `404` is not. Withdrawing does not delete history and does not un-publish anything; see
[Lifecycle and freezing](lifecycle-and-freezing.md).

## Caching

nginx assigns cache headers mostly by URL shape. A concrete file under a version or a revision
is immutable for a year; everything else is five minutes.

Shape alone is not quite enough, because a **draft** publication has the same URL shape as a
frozen one while its bytes still follow a branch. So `cairn build` generates an explicit rule
for each draft, and the cache map is keyed on that rule's marker as well as on the URL: a draft
is served at five minutes, like the pointers.

The practical consequence for you is unchanged by that. A draft is a preview, not an address to
hand out: its bytes change under it, and the page badge says `draft` for that reason. Advertise
a release once it is published.

## Redirects are relative

Cairn sits behind a TLS-terminating proxy and speaks plain HTTP to it. Every redirect it emits
has a relative `Location`, so the client resolves it against the public origin it actually
asked for, rather than being sent to an internal scheme and port.

For the same reason a directory URL is served directly rather than being answered with nginx's
append-a-slash redirect: with relative redirects, any layer in front that normalises the
trailing slash away would bounce the two spellings between itself and the origin for ever.
Both `/eaf/v1.0.0` and `/eaf/v1.0.0/` answer `200`.
