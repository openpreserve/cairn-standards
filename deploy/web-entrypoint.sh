#!/bin/sh
# Cairn web entrypoint: seed the shared volumes on first boot (so the site works immediately,
# before the syncer has run), then hand the container over to nginx.
#
# nginx runs in the foreground as PID 1 via exec, which matters for two reasons:
#
#   Supervision. Previously nginx was started as a background daemon and PID 1 was a sleep
#   loop, so if the nginx master died the container stayed "running" with nothing serving.
#   The healthcheck noticed, but Docker's restart policy reacts to exit codes, not health, so
#   nothing restarted it. As PID 1, nginx dying is the container exiting, which `restart:
#   unless-stopped` acts on.
#
#   Shutdown. The kernel does not deliver default-action signals to PID 1, so a shell loop
#   there had to trap them, and a foreground `sleep` delayed that trap by up to the sleep
#   duration. nginx handles its own signals: the base image sets STOPSIGNAL SIGQUIT, which is
#   nginx's graceful shutdown, so `docker compose stop` drains connections instead of timing
#   out and being killed.
#
# Reloads are handled by a background watcher, because only nginx itself can hold PID 1.
set -eu

HTML_DIR=/usr/share/nginx/html
CONF_DIR=/etc/nginx/cairn
RELOAD_POLL="${RELOAD_POLL:-60}"

# Seed from the image-baked snapshot if a freshly-created volume is empty.
if [ -d /seed ] && [ -z "$(ls -A "$HTML_DIR" 2>/dev/null || true)" ]; then
  echo "[web] seeding $HTML_DIR from baked snapshot"
  cp -a /seed/. "$HTML_DIR"/
fi
if [ -d /seed-conf ] && [ -z "$(ls -A "$CONF_DIR" 2>/dev/null || true)" ]; then
  echo "[web] seeding $CONF_DIR from baked snapshot"
  cp -a /seed-conf/. "$CONF_DIR"/
fi

nginx -t

# Content needs no reload; nginx picks up new files as they are served. Only the generated
# routes do, so watch those specifically and reload when they actually change. The previous
# fixed six-hour timer meant a newly merged standard could sit unreachable for most of a day
# while its files were already on disk, and reloaded pointlessly the rest of the time.
routes_digest() {
  cat "$CONF_DIR"/*.conf 2>/dev/null | md5sum | cut -d' ' -f1
}

watch_routes() {
  known="$(routes_digest)"
  while sleep "$RELOAD_POLL"; do
    current="$(routes_digest)"
    [ "$current" = "$known" ] && continue
    if nginx -t 2>/dev/null; then
      if nginx -s reload; then
        known="$current"
        echo "[web] routes changed, reloaded at $(date -u +%FT%TZ)"
      fi
    else
      # Left unrecorded on purpose, so a corrected routes file is picked up on the next poll
      # rather than being treated as already handled.
      echo "[web] routes changed but config test failed; still serving the previous config"
    fi
  done
}

watch_routes &

exec nginx -g 'daemon off;'
