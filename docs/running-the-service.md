# Running the service

What the deployment is made of, what it does on its own, and what needs a person. If you are
looking up a marker or an exit code from a log, go straight to the
[Operator reference](operator-reference.md).

---

## The shape of it

Three small services around two shared volumes.

**syncer** runs `validate`, then `sync`, then `build`, on a loop - every six hours by default.
It writes into the volumes. It reads manifests from one of three places, set in
`deploy/docker-compose.yml`: the copy baked into its image, a bind-mounted checkout, or a
`git clone` of `REPO_URL`.

**web** (nginx) serves the volumes on `:8080` over plain HTTP, behind whatever terminates TLS.
On first boot it seeds an empty volume from a snapshot baked into the image, so the site works
before the syncer's first cycle. It watches the generated routes file and reloads within
`RELOAD_POLL` seconds (60 by default) of it changing.

nginx runs as PID 1 via `exec`, so if it dies the container exits and `restart: unless-stopped`
brings it back. It also handles its own signals, and the base image's `STOPSIGNAL SIGQUIT`
means `docker compose stop` drains connections rather than timing out and being killed. Run as
a background daemon under a shell loop, as this once was, a dead server left a container Docker
considered healthy while nothing was being served - restart policies react to exit codes, not
to what is listening.

**cloudflared** (opt-in) provides ingress through a Cloudflare Tunnel: no inbound ports, TLS at
the edge. Alternatively point OPF's own reverse proxy at the internal `web:8080`; see
`deploy/reverse-proxy.example.conf`.

## Ingress modes

```bash
# Local testing, publishes 127.0.0.1:8080
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml up -d --build

# Cloudflare Tunnel; set CLOUDFLARE_TUNNEL_TOKEN in deploy/.env first
docker compose --profile tunnel up -d --build
```

`deploy/.env.example` lists every variable. Copy it to `deploy/.env` before the first run.

## What updates on its own

**Content.** New or re-synced files, re-rendered pages and an updated `catalog.json` are served
as soon as the syncer writes them. nginx needs no reload for content.

**Routes, within about a minute.** A brand-new standard, a new major line, a new rules revision
or a new `410` are *routing* changes, and nginx picks those up on reload. The web container
watches the generated routes file and reloads shortly after the syncer writes it. A routes file
that fails `nginx -t` is not loaded: the previous config keeps serving, the failure is logged,
and it is retried on the next poll once corrected.

**Frozen bytes, every 24 hours.** Every `VERIFY_INTERVAL`, and on the first cycle after any
restart, the cycle runs `cairn sync --verify` instead of a plain sync. This re-fetches frozen
artifacts and compares them against the recorded SHA-256, because an ordinary sync skips them
entirely and would never notice an upstream re-tag. The stamp for this lives beside the routes
file so it survives restarts.

**Drifted files.** Once a sync has fetched an artifact and found upstream still agrees with the
record, it hashes the served copy too and rewrites it if that has drifted. For a draft that
happens every cycle; for a frozen publication, on the verify pass. So the detection window for
the frozen corpus is `VERIFY_INTERVAL` (24h), not `SYNC_INTERVAL` (6h).

## What does not update on its own

**A genuinely new standard, in the default image-baked mode.** The syncer only knows the
manifests baked into its image, so a new standard needs a new image via the publish workflow
and a redeploy. In git-pull or bind-mount mode it is picked up automatically.

**Anything in `deploy/nginx.conf`.** That file ships inside the web image. A change to the
cache map or the base config needs an image rebuild and a redeploy, not merely a scheduled
sync. The generated routes file is the part that updates by itself; the static config is not.

**Anything in `src/cairn/`.** Same reasoning: it is the syncer image.

## A `GITHUB_TOKEN` is optional but recommended

Public `raw.githubusercontent.com` fetches need no authentication, but the GitHub API calls do
- release-asset lookups, and the best-effort commit pinning that turns a tag into a permanent
SHA in the provenance record - and all GitHub requests share a rate limit. Setting
`GITHUB_TOKEN` (or `GH_TOKEN`) lifts that limit. CI and the syncer both pass it through.

Without it, nothing breaks: the commit pinning is best-effort and a sync that cannot reach the
API still succeeds, recording the ref without a commit. But frequent or high-volume syncs can
be throttled, and the provenance is less complete than it could be.

## When a cycle fails

A failure is reported per standard, not per run. `cairn sync` replicates every standard it can,
records the ones that failed, and exits non-zero. The loop still runs `cairn build`, so one
broken upstream does not stop the rest of the registry reaching the site.

The same holds one level down: a publication that fails does not abandon the others belonging
to its standard. That matters most on a verify pass, where the point of the run is to have
*read* every published artifact. Abandoning the rest after one failure while still exiting with
a code meaning "ran to the end" had the loop stamp a verification of bytes it never looked at,
suppressing the next attempt for a full interval.

Failures within one standard are collected and reported together, so it counts as one failed
standard however many of its publications were involved.

The exit codes and every marker are in the [Operator reference](operator-reference.md).

## Backups, and what they are for

The volume is the published record. Two of the failure modes in the operator reference -
`PROVENANCE UNREADABLE` and `UNVERIFIABLE PUBLISHED FILE` - are resolved by comparing the bytes
on the volume against an independent copy. If you have no independent copy, they are resolved
by deciding, without evidence, what was published.

Keep one.
