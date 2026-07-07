# Cairn

**Durable, resolvable hosting for preservation standards.**
Powers [`standards.openpreservation.org`](https://standards.openpreservation.org).

A *cairn* is a stack of stones that endures and marks the way. This project does both:
it **preserves** a controlled replica of each standard's release artifacts, and it makes
their namespace/schema URLs **resolve** — for people in a browser and for XML tools alike.

---

## Why this exists

The Open Preservation Foundation (OPF) has agreed to host the [TS-EAS](https://www2.archivists.org/groups/technical-subcommittee-on-encoded-archival-standards-ts-eas)
Encoded Archival Standards — **EAF**, **EAD**, **EAC-CPF** — under a stable domain it controls,
with more standards (E-ARK, and other organisations') to follow. The schemas' source of truth
stays upstream (GitHub); Cairn pulls each release into an OPF-controlled, integrity-checked,
write-once replica and serves it under a permanent URL scheme.

Design principles:

- **Preserve, don't redirect.** We serve our own verified copies so URLs keep resolving even
  if upstream disappears. Every artifact carries a SHA-256 and provenance record.
- **Static-first, dumb serving layer.** All the logic is in an offline build step; nginx just
  serves files. Schema URLs resolve forever even when the tooling is idle.
- **Manifest-driven.** Adding or updating a standard is a small YAML edit reviewed as a pull
  request — not bespoke engineering. This is how the platform scales without scaling OPF's effort.

## The URL contract

For a standard `eaf`, major line `v1`, latest release `v1.0.0`:

| URL | Serves |
| --- | --- |
| `/` | Registry index of all hosted standards |
| `/eaf` | Human landing page for the standard (all versions, stewards, links, downloads) |
| `/eaf/v1` | **Namespace document** — readable XHTML that is also a machine-readable [RDDL](https://www.w3.org/2001/tag/doc/nsDocuments/) directory pointing at the current v1 schema files. XML clients that ask for `application/xml` are content-negotiated to the schema. |
| `/eaf/v1/eaf.xsd` | `303` → the latest concrete `v1.x.y` file (pin-to-latest-minor) |
| `/eaf/v1.0.0` | Landing page for that exact release (downloads + checksums + provenance) |
| `/eaf/v1.0.0/eaf.xsd` | The actual schema file — `application/xml`, immutable cache, CORS `*` |

**Namespace = major version only** (`/eaf/v1`). Minor/patch numbers never appear in a namespace,
so a minor release never forces a namespace change. The exact version lives in the schema's
`schema-version` attribute and in the concrete `/vX.Y.Z/` release folders.

## How it works

```
standards/<id>/standard.yaml   ─ the registry (identity, upstream source, releases, artifacts)
        │  cairn validate       ─ check manifests against schemas/standard.schema.json
        ▼
     cairn sync                 ─ fetch artifacts, verify SHA-256, write provenance, FREEZE
        │                          → site/<id>/vX.Y.Z/…  (write-once replica)
        ▼
     cairn build                ─ render landing pages, RDDL, catalog.json, sitemap,
        │                          and the generated nginx routing → site/ + build/nginx/
        ▼
   nginx (:8080, plain HTTP)    ─ serves site/ behind OPF's TLS-terminating reverse proxy
```

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .

cairn validate            # lint every manifest
cairn sync                # replicate + checksum upstream artifacts into site/
cairn build               # render pages + routing into site/ and build/nginx/

# serve it
docker compose -f deploy/docker-compose.yml up --build
curl -i http://localhost:8080/eaf/v1.0.0/eaf.xsd
```

See [CONTRIBUTING.md](CONTRIBUTING.md) to add or update a standard.

## Deployment

The container serves **plain HTTP on `:8080`** and expects OPF's existing reverse proxy to
terminate TLS and forward to it. See [deploy/](deploy/) for the `Dockerfile`, `docker-compose.yml`,
and an example upstream vhost.

## Licence

Tooling: Apache-2.0 (see [LICENSE](LICENSE)). Hosted standards remain under their upstream
licences; each release records its provenance.
