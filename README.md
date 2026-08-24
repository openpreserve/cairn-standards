# Cairn

**Durable, resolvable hosting for preservation standards.**
Powers [`standards.openpreservation.org`](https://standards.openpreservation.org).

---

## What it is

The Open Preservation Foundation hosts the [TS-EAS](https://www2.archivists.org/groups/technical-subcommittee-on-encoded-archival-standards-ts-eas)
Encoded Archival Standards - **EAD**, **EAC-CPF** and **EAC-F** - under a stable domain it
controls, with more standards (E-ARK, for instance) to follow. The schemas' source of truth
stays upstream on GitHub. Cairn pulls each release into a locally-controlled,
integrity-checked, write-once replica and serves it under a permanent URL scheme.

The problem it solves is that a schema URL is a promise. Anything that cites one - a
`targetNamespace`, a validation pipeline, a printed tag library - depends on it resolving to
the same bytes years later. Upstream repositories move, get renamed, and occasionally
disappear. Cairn makes that promise something OPF can keep.

Three principles decide most of the design:

- **Preserve, don't redirect.** We serve verified copies of our own, so URLs keep resolving
  even if upstream disappears. Every file carries a SHA-256 and a provenance record.
- **Static-first, dumb serving layer.** All the logic is in an offline build step; nginx just
  serves files. URLs resolve for as long as the files exist, whether or not the tooling runs.
- **Manifest-driven.** Adding or updating a standard is a small YAML edit reviewed as a pull
  request, not bespoke engineering. That is how the platform scales without scaling the effort.

## What the URLs look like

For a standard `eaf`, major line `v1`, latest release `v1.0.0`:

| URL | Serves |
| --- | --- |
| `/` | Registry index of every hosted standard |
| `/eaf` | Landing page for the standard |
| `/eaf/v1` | Namespace document: a readable page that is also a machine-readable [RDDL](https://www.w3.org/2001/tag/doc/nsDocuments/) directory. XML clients asking for `application/xml` are sent straight to the schema |
| `/eaf/v1/eaf.xsd` | `303` to the same file under the latest `v1.x.y` release |
| `/eaf/v1.0.0/eaf.xsd` | The schema itself. Permanent, immutable, CORS `*` |
| `/eaf/v1/schematron/2026-07/eaf.sch` | One dated revision of the validation rules. Permanent |
| `/eaf/v1/schematron/latest/eaf.sch` | `303` to the newest frozen revision |

Two things are worth knowing straight away.

**A namespace is major-only.** `/eaf/v1`, never `/eaf/v1.0.0`, so a minor release never forces
consumers to change their namespace. The exact version lives in the schema's own
`schema-version` attribute and in the `/vX.Y.Z/` release folders.

**Schemas and rules are separate tracks.** A schema version is frozen for ever once published.
Schematron rules are revised on a cadence of their own, so they are published beside the
versions rather than inside them - dated, and attached to the major line the `.sch` file
itself declares. Revising the rules moves no schema version, and no revision ever disappears.

The full contract is in [docs/url-contract.md](docs/url-contract.md).

## How it works

The registry is a set of YAML manifests. Three commands turn them into a served site:

```text
  standards/<id>/standard.yaml    identity, upstream source, releases, rules revisions,
                                 and the artifacts belonging to each

  1. cairn validate      check every manifest against schemas/standard.schema.json
                         (--baseline <checkout> also refuses edits that would break a
                          URL already published; this is what CI runs on a pull request)
  2. cairn sync          fetch artifacts, verify SHA-256, record provenance, freeze
                         (writes the write-once replica under site/;
                          --verify re-checks frozen bytes against upstream)
  3. cairn build         render pages, RDDL, catalog.json, sitemap, nginx routes
                         (writes site/ and build/nginx/cairn-routes.conf)

  nginx (:8080, plain HTTP)       serves site/ behind the ingress layer
```

Nothing is written until it has been checked. Every publication is resolved, fetched and
verified in full, and only a plan that breaks no existing promise is committed to disk - so a
refused change leaves the served directory exactly as it was.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .

cairn validate            # check every manifest
cairn sync                # replicate + checksum upstream artifacts into site/
cairn build               # render pages and routing

# serve it locally on 127.0.0.1:8080
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml up --build
curl -i http://localhost:8080/eaf/v1.0.0/eaf.xsd
```

## Deployment

Three small services around two shared volumes. The **syncer** replicates and renders on a loop
(six hours by default, with a deeper integrity pass every 24), so a merged manifest change
reaches the site without a manual rebuild. **web** (nginx) serves the volumes and reloads
itself within a minute of the generated routes changing. **cloudflared** is an optional ingress
via a Cloudflare Tunnel; alternatively point your own TLS-terminating reverse proxy at the
internal `web:8080` (see `deploy/reverse-proxy.example.conf`).

```bash
# Cloudflare Tunnel; set CLOUDFLARE_TUNNEL_TOKEN in deploy/.env first
docker compose --profile tunnel up -d --build
```

See [docs/running-the-service.md](docs/running-the-service.md) for what updates on its own and
what needs a rebuild, and [deploy/](deploy/) for the compose files and `.env.example`.

## Documentation

- **[Adding or updating a standard](CONTRIBUTING.md)** - the common task. A YAML edit and a
  pull request.
- **[docs/](docs/README.md)** - the URL contract, the manifest reference, how freezing works,
  the two publishing procedures, running the service, and an operator reference for every
  marker and exit code.

## Licence

Tooling: Apache-2.0 (see [LICENSE](LICENSE)). Hosted standards remain under their upstream
licences; each release records its provenance.
