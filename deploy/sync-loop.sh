#!/bin/sh
# Cairn syncer loop: keep the shared volumes current from the manifests repo.
#
# Every SYNC_INTERVAL seconds it (re)acquires the repo, then runs
# validate -> sync -> build, writing into the shared volumes:
#   CAIRN_SITE_DIR    -> served by nginx as the document root
#   CAIRN_ROUTES_FILE -> included by nginx (picked up on its next reload)
#
# Manifest source (supports both):
#   - bind-mount a working copy at $REPO_DIR (git pull if it is a git repo), or
#   - set REPO_URL to have this container clone it on first run.
set -eu

REPO_DIR="${REPO_DIR:-/repo}"
SYNC_INTERVAL="${SYNC_INTERVAL:-21600}"          # 6 hours
export CAIRN_SITE_DIR="${CAIRN_SITE_DIR:-/data/site}"
export CAIRN_ROUTES_FILE="${CAIRN_ROUTES_FILE:-/data/conf/cairn-routes.conf}"

log() { echo "[sync-loop] $(date -u +%FT%TZ) $*"; }

acquire_repo() {
  if [ -d "$REPO_DIR/.git" ]; then
    log "git pull in $REPO_DIR"
    git -C "$REPO_DIR" pull --ff-only || log "git pull failed; using existing checkout"
  elif [ -n "${REPO_URL:-}" ] && [ ! -e "$REPO_DIR/pyproject.toml" ]; then
    log "cloning $REPO_URL into $REPO_DIR"
    git clone --depth 1 ${REPO_BRANCH:+-b "$REPO_BRANCH"} "$REPO_URL" "$REPO_DIR"
  else
    log "using repo at $REPO_DIR (no git remote configured)"
  fi
}

run_once() {
  acquire_repo
  cd "$REPO_DIR"
  cairn validate
  cairn sync
  cairn build
  log "cycle complete -> $CAIRN_SITE_DIR"
}

while true; do
  run_once || log "cycle FAILED (will retry next interval)"
  log "sleeping ${SYNC_INTERVAL}s"
  sleep "$SYNC_INTERVAL"
done
