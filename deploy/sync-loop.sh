#!/bin/sh
# Cairn syncer loop: keep the shared volumes current from the manifests repo.
#
# Every SYNC_INTERVAL seconds it (re)acquires the repo, then runs
# validate -> sync -> build, writing into the shared volumes:
#   CAIRN_SITE_DIR    -> served by nginx as the document root
#   CAIRN_ROUTES_FILE -> included by nginx (picked up on its next reload)
#
# Every VERIFY_INTERVAL seconds that sync runs as `cairn sync --verify` instead. An
# ordinary sync skips a frozen release entirely, so without this nothing ever re-reads the
# bytes behind a published URL and an upstream re-tag would go unnoticed indefinitely.
# Verify is a superset of sync: it still fetches mutable releases, and additionally
# re-fetches frozen ones and fails on checksum drift.
#
# Manifest source (supports both):
#   - bind-mount a working copy at $REPO_DIR (git pull if it is a git repo), or
#   - set REPO_URL to have this container clone it on first run.
set -eu

REPO_DIR="${REPO_DIR:-/repo}"
SYNC_INTERVAL="${SYNC_INTERVAL:-21600}"          # 6 hours
VERIFY_INTERVAL="${VERIFY_INTERVAL:-86400}"      # 24 hours
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

# Each step is guarded explicitly rather than left to `set -e`: this function is called from
# an AND-OR list, and POSIX suspends errexit for every command of such a list bar the last,
# which propagates into the function body. Relying on errexit here would let `cairn build`
# run after a failed sync or a failed integrity check.
run_once() {
  mode="$1"
  acquire_repo || return 1
  cd "$REPO_DIR" || return 1
  cairn validate || return 1
  if [ "$mode" = verify ]; then
    cairn sync --verify || return 1
  else
    cairn sync || return 1
  fi
  cairn build || return 1
  log "cycle complete -> $CAIRN_SITE_DIR"
}

# Zero means "never verified", so the first cycle after a restart is a verify.
last_verify=0

while true; do
  now="$(date -u +%s)"
  if [ "$(( now - last_verify ))" -ge "$VERIFY_INTERVAL" ]; then
    mode=verify
  else
    mode=sync
  fi

  log "starting $mode cycle"
  if run_once "$mode"; then
    if [ "$mode" = verify ]; then
      last_verify="$now"
    fi
  elif [ "$mode" = verify ]; then
    # Left unstamped on purpose so the next cycle retries the check rather than waiting a
    # full VERIFY_INTERVAL. The marker is here to be alerted on: it means either a
    # transient upstream failure or a published version whose bytes no longer match.
    log "INTEGRITY CHECK FAILED - retrying next cycle"
  else
    log "cycle FAILED (will retry next interval)"
  fi

  log "sleeping ${SYNC_INTERVAL}s"
  sleep "$SYNC_INTERVAL"
done
