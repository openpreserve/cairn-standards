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
# Every directory this service creates lands in the document root or beside the routes file,
# and nginx runs unprivileged: a directory it cannot traverse makes every URL beneath it a 403.
# Setting the umask once here is the whole of that guarantee. cairn chmods the *files* it
# writes, because those go through a temp file that mkstemp creates 0600 regardless, but it
# does not touch directory modes at all - an earlier version that walked each path and widened
# what it found produced seven defects in two review rounds, including chmod'ing a developer's
# home directory, because a path's parents do not stop anywhere in particular.
umask 022

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
# Exit codes are three-way on purpose:
#   0  everything succeeded
#   1  the cycle did not complete - nothing can be concluded about what it never reached
#   2  the cycle ran to the end, and reported something an operator must see
#
# The distinction matters twice. Operators alerting on "cycle FAILED" should not see it for
# a single flaky upstream while the site is perfectly current. And the verify stamp records
# that the verify pass *ran*, not that every standard passed: collapsing the two meant one
# persistently failing standard suppressed the stamp forever, so every cycle re-verified and
# re-downloaded every frozen artifact of every healthy standard, indefinitely.
#
# That fix needs cairn's own codes to distinguish the same two things, or a verify killed
# partway through would stamp itself as having run and suppress the next one for a full
# interval, which is the same bug pointing the other way.
# Read from cairn rather than restated here. Two copies of a contract drift, and this one
# already had a third in the docs that was wrong on the day it was written.
sync_rc_defs="$(cairn exit-codes)" || { log "cannot read exit codes from cairn"; exit 1; }
eval "$sync_rc_defs"

# Under `set -u` a name this script expects but cairn no longer emits would abort the run at
# the point of use, i.e. midway through a cycle, hours after the container started and with
# the failure reading as a crash. Check them once, here, where the message can say what is
# actually wrong: this script and the cairn on its PATH are different versions.
for rc_name in SYNC_RC_ATTENTION SYNC_RC_FAILED SYNC_RC_NOTHING_SUCCEEDED BUILD_RC_ATTENTION; do
  eval "rc_value=\${$rc_name:-}"
  [ -n "$rc_value" ] || { log "cairn exit-codes did not define $rc_name; version mismatch"; exit 1; }
done

# run_once's own result, distinct from cairn's. It answers two questions at once: did the pass
# reach the end (which is the only thing the verify stamp asks), and did the render produce a
# site. Enumerated rather than computed - the arithmetic version of this had CYCLE_RAN plus the
# build penalty collide with CYCLE_INCOMPLETE, so an unfinished sync was read as a completed one
# and stamped.
CYCLE_RAN=0
CYCLE_INCOMPLETE=1
CYCLE_ATTENTION=2
CYCLE_RAN_BUILD_FAILED=4
CYCLE_INCOMPLETE_BUILD_FAILED=5
CYCLE_ATTENTION_BUILD_FAILED=6

run_once() {
  run_mode="$1"
  # $CYCLE_INCOMPLETE, not a literal 1. These three run before the build, so no build outcome
  # is folded in; restating the number is what the enumeration above exists to stop.
  acquire_repo || return "$CYCLE_INCOMPLETE"
  cd "$REPO_DIR" || return "$CYCLE_INCOMPLETE"
  cairn validate || return "$CYCLE_INCOMPLETE"

  sync_rc=0
  if [ "$run_mode" = verify ]; then
    cairn sync --verify || sync_rc=$?
  else
    cairn sync || sync_rc=$?
  fi

  # Whether the render succeeded is a separate question from whether the sync ran, and
  # ordering it before this decision threw away a completed verify pass: a build that failed
  # on a full or read-only volume made run_once return 1, the stamp was never written, and
  # every 6h cycle re-verified and re-downloaded the whole frozen corpus indefinitely. That
  # is the pathology the exit-code split exists to prevent, one step later in the same
  # function. The build result is reported by the caller instead.
  #
  # `cairn build` has three outcomes, not two. It exits BUILD_RC_ATTENTION when the site was
  # rendered but a page fell back to its one-line summary because prose beside a manifest
  # could not be read: the site is current, every URL resolves, and cairn has already printed
  # CONTENT UNREADABLE naming the file. Reading any non-zero as a failed render logged BUILD
  # FAILED for that, which the runbook defines as "the site is serving its previous state" and
  # points at a full or read-only volume - an operator sent to check the disk over an encoding
  # problem in one markdown file.
  build_rc=0
  cairn build || build_rc=$?

  # The build outcome travels in the return value, not in a global. Reporting it out of band
  # worked only because run_once happens to be called in the current shell: putting it in a
  # pipeline or a subshell - `run_once "$mode" | tee -a log`, a natural thing to add - silently
  # loses the assignment, and BUILD FAILED is then never logged, with nothing to say so.
  if [ "$build_rc" -eq 0 ] || [ "$build_rc" -eq "$BUILD_RC_ATTENTION" ]; then
    ran=$CYCLE_RAN
    incomplete=$CYCLE_INCOMPLETE
    attention=$CYCLE_ATTENTION
  else
    ran=$CYCLE_RAN_BUILD_FAILED
    incomplete=$CYCLE_INCOMPLETE_BUILD_FAILED
    attention=$CYCLE_ATTENTION_BUILD_FAILED
  fi

  case "$sync_rc" in
    0)
      log "cycle complete -> $CAIRN_SITE_DIR"
      return "$ran"
      ;;
    "$SYNC_RC_ATTENTION"|"$SYNC_RC_FAILED")
      # cairn has already printed the specific marker and named what it applies to; the
      # marker strings live there so the runbooks have one source for them.
      log "cycle ran and needs attention (cairn sync exit $sync_rc) -> $CAIRN_SITE_DIR"
      return "$attention"
      ;;
    "$SYNC_RC_NOTHING_SUCCEEDED")
      # Every release the pass attempted failed, so it re-read nothing. Stamping it would
      # record a verification that did not happen and suppress the next attempt for a full
      # interval, which is the original stamp bug arriving from the other direction.
      log "no release synced successfully; nothing was verified"
      return "$incomplete"
      ;;
    *)
      log "cairn sync did not complete (exit $sync_rc)"
      return "$incomplete"
      ;;
  esac
}

running=1
# The script is PID 1, and the kernel does not deliver default-action signals to PID 1, so
# without this trap `docker compose stop` waits out the grace period and then SIGKILLs the
# container. The sleep below runs in the background and is waited on, because `wait` is
# interruptible while a foreground `sleep` would block the trap for hours.
#
# Scope, stated honestly: POSIX defers a trap until the running foreground command returns,
# so a signal arriving *during* a cycle is not handled until that step finishes, and a long
# verify pass can still outlast docker's grace period and be killed. That is tolerable
# rather than fixed, and only because of how the writes below behave: every file is written
# to a temp file and renamed, SHA256SUMS is written before provenance.json so an interrupted
# pair looks stale rather than current, and stranded temp files are reaped on the next run.
# A kill mid-cycle therefore loses work, never corrupts what is already published.
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
  run_once "$mode" && cycle_rc=0 || cycle_rc=$?

  # Split the two answers back apart. Whether the pass reached the end is the only question the
  # stamp asks, so a failed render must not change it in either direction.
  build_failed=0
  case "$cycle_rc" in
    "$CYCLE_RAN_BUILD_FAILED")        build_failed=1; cycle_rc=$CYCLE_RAN ;;
    "$CYCLE_INCOMPLETE_BUILD_FAILED") build_failed=1; cycle_rc=$CYCLE_INCOMPLETE ;;
    "$CYCLE_ATTENTION_BUILD_FAILED")  build_failed=1; cycle_rc=$CYCLE_ATTENTION ;;
  esac

  case "$cycle_rc" in
    "$CYCLE_RAN"|"$CYCLE_ATTENTION")
      # Both mean the pass ran end to end, so a verify is recorded as done and the next one
      # happens on the normal schedule. rc 2 says an operator must look at what cairn
      # reported, not that verification was skipped.
      if [ "$mode" = verify ]; then
        write_stamp "$now"
      fi
      ;;
    *)
      # No stamp: the run stopped partway, so the artifacts it never reached are unverified
      # and the next cycle should try again rather than wait out the interval.
      log "cycle FAILED (will retry next interval)"
      ;;
  esac

  # Reported after the stamp decision, never before it. A failed render leaves the site at
  # its last good state, which is bad, but it says nothing about whether the artifacts were
  # verified - and letting it suppress the stamp is how a single failure became permanent.
  if [ "$build_failed" -ne 0 ]; then
    log "BUILD FAILED: the site was not re-rendered; it is serving its previous state"
  fi

  [ "$running" -eq 1 ] || break
  log "sleeping ${SYNC_INTERVAL}s"
  sleep "$SYNC_INTERVAL" &
  wait $! || true
done

log "exited cleanly"
