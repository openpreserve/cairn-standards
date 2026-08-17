#!/bin/sh
# Cairn syncer loop: keep the shared volumes current from the manifests repo.
#
# Every SYNC_INTERVAL seconds it (re)acquires the repo, then runs validate -> sync -> build,
# writing into the shared volumes:
#   CAIRN_SITE_DIR    -> served by nginx as the document root
#   CAIRN_ROUTES_FILE -> included by nginx (picked up on its next reload)
#
# Every VERIFY_INTERVAL seconds the sync runs as `cairn sync --verify`. An ordinary sync
# skips a frozen release entirely, so without this nothing ever re-reads the bytes behind a
# published URL and an upstream re-tag would go unnoticed. Verify is a superset of sync:
# mutable releases still track their branch, and frozen ones are re-fetched and checksummed.
#
# This file holds the only defaults for both intervals. Override them in the environment
# (see deploy/docker-compose.yml); do not copy the numbers into another file, or the two
# will drift and the behaviour will depend on how the container was started.
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

# When the last verify completed. This has to outlive the container: with `restart:
# unless-stopped`, keeping it in memory means every crash or redeploy re-downloads every
# frozen artifact of every standard. It lives beside the routes file because that volume is
# persistent and, unlike the document root, is not served to the public.
VERIFY_STAMP="${VERIFY_STAMP:-$(dirname "$CAIRN_ROUTES_FILE")/.cairn-last-verify}"

log() { echo "[sync-loop] $(date -u +%FT%TZ) $*"; }

read_stamp() {
  [ -r "$VERIFY_STAMP" ] || { echo 0; return; }
  stamp="$(cat "$VERIFY_STAMP" 2>/dev/null || echo 0)"
  case "$stamp" in
    ''|*[!0-9]*) echo 0 ;;
    *)           echo "$stamp" ;;
  esac
}

write_stamp() {
  mkdir -p "$(dirname "$VERIFY_STAMP")"
  echo "$1" > "$VERIFY_STAMP"
}

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

# Every step is guarded explicitly rather than relying on `set -e`. POSIX suspends errexit
# for any command that is not the last in an AND-OR list or an `if` condition, and that
# suspension propagates into a function body, so a failed validate would otherwise fall
# through to sync and build.
#
# `cairn sync` reports a per-standard failure with a non-zero exit but still replicates
# every standard that succeeded, so the build runs regardless and the healthy standards
# reach the site. Only validate is fatal to the cycle, because a manifest that will not load
# means there is nothing meaningful to render.
run_once() {
  run_mode="$1"
  acquire_repo || return 1
  cd "$REPO_DIR" || return 1
  cairn validate || return 1

  sync_rc=0
  if [ "$run_mode" = verify ]; then
    cairn sync --verify || sync_rc=$?
  else
    cairn sync || sync_rc=$?
  fi

  cairn build || return 1
  [ "$sync_rc" -eq 0 ] || return "$sync_rc"
  log "cycle complete -> $CAIRN_SITE_DIR"
}

running=1
# The script is PID 1, and the kernel does not deliver default-action signals to PID 1, so
# without this trap `docker compose stop` waits out the grace period and then SIGKILLs the
# container mid-cycle. The sleep below runs in the background and is waited on, because
# `wait` is interruptible while a foreground `sleep` would block the trap for hours.
trap 'log "stopping"; running=0' TERM INT

while [ "$running" -eq 1 ]; do
  now="$(date -u +%s)"
  last_verify="$(read_stamp)"

  # A clock that has stepped backwards would otherwise suppress verification until it caught
  # up, so treat a stamp in the future as due.
  if [ "$last_verify" -gt "$now" ] || [ "$(( now - last_verify ))" -ge "$VERIFY_INTERVAL" ]; then
    mode=verify
  else
    mode=sync
  fi

  log "starting $mode cycle"
  if run_once "$mode"; then
    if [ "$mode" = verify ]; then
      write_stamp "$now"
    fi
  elif [ "$mode" = verify ]; then
    # Deliberately not stamped, so the next cycle retries rather than waiting a full
    # interval. Alert on this marker: it means either a transient upstream failure or a
    # published version whose bytes no longer match what we recorded.
    log "INTEGRITY CHECK FAILED - retrying next cycle"
  else
    log "cycle FAILED (will retry next interval)"
  fi

  [ "$running" -eq 1 ] || break
  log "sleeping ${SYNC_INTERVAL}s"
  sleep "$SYNC_INTERVAL" &
  wait $! || true
done

log "exited cleanly"
