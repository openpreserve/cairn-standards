# Cairn

**Durable, resolvable hosting for preservation standards.**
Powers [`standards.openpreservation.org`](https://standards.openpreservation.org).

---

## Why this exists

The Open Preservation Foundation (OPF) has agreed to host the [TS-EAS](https://www2.archivists.org/groups/technical-subcommittee-on-encoded-archival-standards-ts-eas)
Encoded Archival Standards: **EAF**, **EAD**, & **EAC-CPF**, under a stable domain it controls,
with more standards (E-ARK, for instance) to follow. The schemas' source of truth
stays upstream (GitHub); Cairn pulls each release into a locally-controlled, integrity-checked,
write-once replica and serves it under a permanent URL scheme.

Design principles:

- **Preserve, don't redirect.** We serve our own verified copies so URLs keep resolving even
  if upstream disappears. Every artifact carries a SHA-256 and provenance record.
- **Static-first, dumb serving layer.** All the logic is in an offline build step; nginx just
  serves files. Schema URLs resolve forever even when the tooling is idle.
- **Manifest-driven.** Adding or updating a standard is a small YAML edit reviewed as a pull
  request - not bespoke engineering. This is how the platform scales without scaling OPF's effort.

## The URL contract

For a standard `eaf`, major line `v1`, latest release `v1.0.0`:

| URL | Serves |
| --- | --- |
| `/` | Registry index of all hosted standards |
| `/eaf` | Human landing page for the standard (all versions, stewards, links, downloads) |
| `/eaf/v1` | **Namespace document** - readable XHTML that is also a machine-readable [RDDL](https://www.w3.org/2001/tag/doc/nsDocuments/) directory pointing at the current v1 schema files. XML clients that ask for `application/xml` are content-negotiated to the schema. |
| `/eaf/v1/eaf.xsd` | `303` → the latest concrete `v1.x.y` file (pin-to-latest-minor) |
| `/eaf/v1.0.0` | Landing page for that exact release (downloads + checksums + provenance) |
| `/eaf/v1.0.0/eaf.xsd` | The actual schema file - `application/xml`, immutable cache, CORS `*` |

**Namespace = major version only** (`/eaf/v1`). Minor/patch numbers never appear in a namespace,
so a minor release never forces a namespace change. The exact version lives in the schema's
`schema-version` attribute and in the concrete `/vX.Y.Z/` release folders.

## How it works

The registry is a set of YAML manifests. Three commands turn them into a served site:

```text
  standards/<id>/standard.yaml    the registry: identity, upstream source, releases, artifacts

  1. cairn validate      check every manifest against schemas/standard.schema.json
                         (--baseline <checkout> also refuses edits that would break a
                          URL already published, which is what CI runs on a PR)
  2. cairn sync          fetch artifacts, verify SHA-256, record provenance, freeze
                         (writes the write-once replica to site/<id>/vX.Y.Z/;
                          --verify re-checks frozen versions against upstream)
  3. cairn build         render landing pages, RDDL, catalog.json, sitemap, nginx routes
                         (writes site/ and build/nginx/cairn-routes.conf)

  nginx (:8080, plain HTTP)       serves site/ behind the ingress layer
```

Each release is planned in full before anything is written: every artifact is resolved,
fetched and checked, and only a plan that breaks no promises is committed to disk. A refused
change leaves the served directory exactly as it was.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .

cairn validate            # lint every manifest
cairn sync                # replicate + checksum upstream artifacts into site/
cairn build               # render pages + routing into site/ and build/nginx/

# serve it locally (publishes 127.0.0.1:8080)
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml up --build
curl -i http://localhost:8080/eaf/v1.0.0/eaf.xsd
```

See [CONTRIBUTING.md](CONTRIBUTING.md) to add or update a standard.

## Deployment

The stack is three small services around two shared volumes:

- **syncer** replicates + renders into the volumes on a loop (every 6h by default), so a newly
  merged standard appears without a manual rebuild. It reads manifests from the image-baked
  copy, a bind-mounted checkout, or a `git clone` (`REPO_URL`) - see `deploy/docker-compose.yml`.
  Every 24h (`VERIFY_INTERVAL`) that cycle runs as `cairn sync --verify`, re-reading the bytes
  behind frozen versions so an upstream re-tag is caught rather than going unnoticed. A
  standard that fails is reported and skipped; the others still sync and the site still
  re-renders.
- **web** (nginx) serves the volumes on `:8080` (plain HTTP), seeds them on first boot from a
  baked snapshot, and reloads periodically to pick up new routes.
- **cloudflared** (opt-in) provides ingress via a Cloudflare Tunnel: no inbound ports, TLS at
  the edge.

Ingress modes:

```bash
# Local testing (publishes 127.0.0.1:8080):
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml up -d --build

# Cloudflare Tunnel (no host ports; set CLOUDFLARE_TUNNEL_TOKEN in deploy/.env first):
docker compose --profile tunnel up -d --build
```

If you would rather sit behind OPF's own TLS-terminating reverse proxy, point it at the
internal `web:8080` (see `deploy/reverse-proxy.example.conf`) and skip the tunnel profile.
See [deploy/](deploy/) for all of the above plus `.env.example`.

## Licence

Tooling: Apache-2.0 (see [LICENSE](LICENSE)). Hosted standards remain under their upstream
licences; each release records its provenance.
