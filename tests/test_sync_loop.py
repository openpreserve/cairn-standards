"""What deploy/sync-loop.sh does with the exit codes cairn hands it.

The loop is the only consumer of those codes, and it is the layer where every stamp bug so far
has lived: a build failure suppressing a completed verification, a verify killed partway
stamping itself as done, and most recently a build that merely *reported* something being
logged as `BUILD FAILED` - a marker the runbook defines as the site serving stale content and
points at a full or read-only volume. None of that is reachable from a Python test, because in
every case cairn did exactly what it was asked.

So this runs the real script under /bin/sh with a stubbed `cairn` on PATH. The stub's exit
codes come from the real `cairn exit-codes`, so the test cannot pass by restating a number the
code no longer uses.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import threading
from pathlib import Path

import pytest

from cairn.cli import main
from cairn.config import find_root

ROOT = find_root(Path(__file__).resolve().parent)
SYNC_LOOP = ROOT / "deploy" / "sync-loop.sh"
READ_TIMEOUT_SECONDS = 60.0

pytestmark = pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs a POSIX shell")


def _exit_code_defs() -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["exit-codes"])
    return out.getvalue()


def _run_one_cycle(tmp_path: Path, *, build_rc: int, sync_rc: int = 0) -> str:
    """Start the loop, let it finish one cycle, stop it, and return everything it logged."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "cairn"
    stub.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        f"  exit-codes) cat <<'EOF'\n{_exit_code_defs()}EOF\n"
        "    ;;\n"
        "  validate) exit 0 ;;\n"
        f"  sync) exit {sync_rc} ;;\n"
        f"  build) exit {build_rc} ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    repo = tmp_path / "repo"
    repo.mkdir()

    # Built from nothing, not layered over os.environ. The script reads REPO_URL and clones
    # from it when the repo directory has no .git, so a developer with REPO_URL exported would
    # have this test attempt a network clone; SYNC_INTERVAL and VERIFY_INTERVAL would likewise
    # be taken from whatever the shell happened to hold.
    env = {
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "REPO_DIR": str(repo),
        "CAIRN_SITE_DIR": str(tmp_path / "site"),
        "CAIRN_ROUTES_FILE": str(tmp_path / "conf" / "cairn-routes.conf"),
        # Long enough that the process is asleep, not mid-cycle, when it is asked to stop.
        "SYNC_INTERVAL": "3600",
        "VERIFY_INTERVAL": "3600",
    }

    proc = subprocess.Popen(
        ["/bin/sh", str(SYNC_LOOP)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    # Reading a pipe blocks with no timeout of its own, and the cleanup below cannot run while
    # it does, so a script or stub that never reaches its "sleeping" line would hang the whole
    # test run rather than fail it. The watchdog closes the pipe by killing the writer, which
    # ends the read and lets the assertion report what was logged before the hang.
    watchdog = threading.Timer(READ_TIMEOUT_SECONDS, proc.kill)
    watchdog.start()
    lines = []
    try:
        for line in proc.stdout:
            lines.append(line)
            if "sleeping" in line:
                break
        else:
            pytest.fail(
                f"the loop never completed a cycle within {READ_TIMEOUT_SECONDS}s:\n{''.join(lines)}"
            )
        return "".join(lines)
    finally:
        watchdog.cancel()
        proc.terminate()  # the script traps TERM and exits its wait
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        proc.kill()
        proc.wait()  # reap, so no zombie and no ResourceWarning
        proc.stdout.close()


def test_a_build_that_reports_something_is_not_a_failed_build(tmp_path):
    """`cairn build` exits 3 when the site was rendered but a page fell back to its one-line
    summary, having already printed CONTENT UNREADABLE naming the file. The site is current
    and every URL resolves. Reading any non-zero as a failed render sent an operator to check
    the disk over an encoding problem in one markdown file."""
    from cairn.cli import EXIT_ATTENTION

    log = _run_one_cycle(tmp_path, build_rc=EXIT_ATTENTION)

    assert "BUILD FAILED" not in log, "a degraded page was reported as a render that produced nothing"
    assert "cycle complete" in log


def test_a_build_that_produced_nothing_is_still_reported(tmp_path):
    """The counterpart: the fix above must not have made the marker unreachable."""
    from cairn.cli import EXIT_INCOMPLETE

    log = _run_one_cycle(tmp_path, build_rc=EXIT_INCOMPLETE)

    assert "BUILD FAILED" in log


def test_a_failed_build_does_not_suppress_the_verify_stamp(tmp_path):
    """Whether the render succeeded says nothing about whether the artifacts were checked, and
    letting it suppress the stamp is how one failure became permanent: every cycle re-verified
    and re-downloaded the whole frozen corpus, indefinitely."""
    from cairn.cli import EXIT_INCOMPLETE

    _run_one_cycle(tmp_path, build_rc=EXIT_INCOMPLETE)

    stamp = tmp_path / "conf" / ".cairn-last-verify"
    assert stamp.is_file() and stamp.read_text().strip().isdigit()


def test_a_sync_that_did_not_finish_is_not_stamped(tmp_path):
    """Nothing can be concluded about the artifacts a dead run never reached."""
    from cairn.cli import EXIT_INCOMPLETE

    log = _run_one_cycle(tmp_path, build_rc=0, sync_rc=EXIT_INCOMPLETE)

    assert "cycle FAILED" in log
    assert not (tmp_path / "conf" / ".cairn-last-verify").exists()


def test_an_unfinished_sync_and_a_failed_build_are_both_reported(tmp_path):
    """run_once answers two questions in one value, and a first attempt at that encoded them
    arithmetically: CYCLE_RAN plus the build penalty collided with CYCLE_INCOMPLETE, so an
    unfinished sync was read as a completed one and stamped. Both answers have to survive."""
    from cairn.cli import EXIT_INCOMPLETE

    log = _run_one_cycle(tmp_path, build_rc=EXIT_INCOMPLETE, sync_rc=EXIT_INCOMPLETE)

    assert "cycle FAILED" in log, "an unfinished sync was reported as a completed cycle"
    assert "BUILD FAILED" in log, "the failed render was lost"
    assert not (tmp_path / "conf" / ".cairn-last-verify").exists(), "an unfinished pass was stamped"


def test_a_cycle_needing_attention_still_stamps_when_the_build_failed(tmp_path):
    """The other combination. Whether the render succeeded says nothing about whether the
    artifacts were checked, so it must not change the stamp in either direction."""
    from cairn.cli import EXIT_ATTENTION, EXIT_INCOMPLETE

    log = _run_one_cycle(tmp_path, build_rc=EXIT_INCOMPLETE, sync_rc=EXIT_ATTENTION)

    assert "needs attention" in log
    assert "BUILD FAILED" in log
    stamp = tmp_path / "conf" / ".cairn-last-verify"
    assert stamp.is_file(), "a pass that ran end to end was not stamped"


def test_the_build_result_survives_run_once_being_piped(tmp_path):
    """The reason the outcome moved out of a global: reporting it that way worked only because
    run_once happens to be called in the current shell. A pipeline or subshell would discard the
    assignment silently, and BUILD FAILED would simply stop being logged."""
    script = SYNC_LOOP.read_text(encoding="utf-8")
    assert "build_failed=1" not in script.split("run_once() {")[1].split("\n}")[0], (
        "run_once assigns the build outcome to a global instead of returning it"
    )


def test_the_loop_sets_the_umask_before_it_runs_cairn():
    """cairn does not set directory modes at all: nginx being able to traverse the document
    root is delivered by this one line. An earlier version walked each path and widened what it
    found, which produced seven defects across two review rounds - including chmod'ing a
    developer's home directory - because a path's parents do not stop anywhere in particular.

    Checked from here because it is a property of the deployment, not of the code, and nothing
    else would notice it going missing.
    """
    script = SYNC_LOOP.read_text(encoding="utf-8")
    assert "umask 022" in script, "the syncer no longer sets a umask; every URL may answer 403"
    # Against the first cairn *invocation*, not the first mention: the header comment above
    # names several of them, so a textual search matches long before any command runs.
    lines = script.splitlines()
    umask_at = next(i for i, line in enumerate(lines) if line.strip().startswith("umask "))
    runs_at = next(i for i, line in enumerate(lines) if line.strip().startswith("cairn "))
    assert umask_at < runs_at, "the umask must be set before cairn runs"

    dockerfile = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    build_step = dockerfile[dockerfile.index("cairn validate"):]
    assert "umask 022" in dockerfile[:dockerfile.index("cairn validate")] or "umask 022" in build_step, (
        "the image build stage bakes a seed snapshot into the runtime image, so it needs the "
        "same umask; without it the baked directories are whatever the builder's umask was"
    )


def test_the_loop_refuses_to_start_against_a_cairn_it_disagrees_with(tmp_path):
    """Under `set -u` a name the script expects but cairn no longer emits aborts at the point
    of use - midway through a cycle, hours after start, reading as a crash."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "cairn"
    stub.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  exit-codes) echo SYNC_RC_ATTENTION=3; echo SYNC_RC_FAILED=4; echo SYNC_RC_NOTHING_SUCCEEDED=5 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    result = subprocess.run(
        ["/bin/sh", str(SYNC_LOOP)],
        env={"PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}", "REPO_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert "BUILD_RC_ATTENTION" in result.stdout
