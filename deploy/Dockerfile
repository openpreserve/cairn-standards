# syntax=docker/dockerfile:1

# ---------- build stage: replicate upstream artifacts + render the static site ----------.
FROM python:3.12-slim-bullseye AS builder
WORKDIR /work

# Install the tooling first (layer caches until deps change).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Then bring in the registry and produce site/ + build/nginx/cairn-routes.conf.
COPY schemas ./schemas
COPY standards ./standards
# GITHUB_TOKEN can be passed as a build secret to lift GitHub API rate limits.
#
# `cairn all` rather than `cairn sync && cairn build`, for one modest reason: the && chain
# stopped at the first non-zero, so a sync failure meant you never learned whether the render
# would also have failed, and it was a second copy of a composition cli.cmd_all already
# implements and has tests for.
#
# It buys no isolation, and an earlier version of this comment wrongly claimed it did.
# cmd_all returns `sync_rc or build_rc`, so any non-zero still fails this RUN and docker still
# discards the layer holding the seed snapshot - exactly as before. That is deliberate here
# and only here: this stage produces a release artifact rather than keeping a running service
# current, so an image whose registry is missing a standard, or whose pages quietly lost their
# prose, should not be published at all. The syncer loop makes the opposite call for the same
# codes, and does not use `cairn all` precisely because it needs to tolerate build rc 3.
RUN --mount=type=secret,id=github_token,required=false \
    sh -c 'umask 022; export GITHUB_TOKEN="$(cat /run/secrets/github_token 2>/dev/null || true)"; \
           cairn validate && cairn all'

# ---------- runtime stage: a small nginx that seeds volumes and self-reloads ----------
FROM nginx:1.27-alpine AS runtime
RUN rm -f /etc/nginx/conf.d/default.conf \
 && rm -rf /usr/share/nginx/html/* \
 && mkdir -p /etc/nginx/cairn
COPY deploy/nginx.conf /etc/nginx/nginx.conf
COPY deploy/web-entrypoint.sh /usr/local/bin/web-entrypoint.sh
RUN chmod +x /usr/local/bin/web-entrypoint.sh

# Image-baked snapshot: seeds empty volumes on first boot so the site works before the
# syncer's first run. The syncer later overwrites these with freshly-replicated content.
COPY --from=builder /work/site/ /seed/
COPY --from=builder /work/build/nginx/ /seed-conf/

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/healthz >/dev/null 2>&1 || exit 1

ENTRYPOINT ["/usr/local/bin/web-entrypoint.sh"]
