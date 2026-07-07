#!/bin/sh
# Cairn web entrypoint: seed the shared volumes on first boot (so the site works
# immediately, before the syncer has run), start nginx, then reload periodically so
# newly-synced routes are picked up. Content files need no reload; only routes do.
set -eu

HTML_DIR=/usr/share/nginx/html
CONF_DIR=/etc/nginx/cairn
RELOAD_INTERVAL="${RELOAD_INTERVAL:-21600}"      # 6 hours

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
nginx                                             # start as background daemon

trap 'echo "[web] stopping"; nginx -s quit; exit 0' TERM INT

while true; do
  sleep "$RELOAD_INTERVAL"
  if nginx -t 2>/dev/null; then
    nginx -s reload && echo "[web] reloaded at $(date -u +%FT%TZ)"
  else
    echo "[web] config test failed; skipping reload"
  fi
done
