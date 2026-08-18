"""The runbook is part of the contract, so drift from it is a defect like any other.

cli.py states that every operator-facing marker is printed by cairn itself "so the runbooks
have one source for them". That only holds if the runbook actually lists them. Four markers
were added in one session and none reached the docs, and an exit code was added to both the
CLI and the shell loop while the documented table kept saying there were four. Nothing
noticed, because prose is the one part of the system nothing executes.

Three successive versions of this file tried to find the markers by pattern-matching the
source, and each was blind to a shape it had not anticipated - most recently to
`CORRUPTED FILE(S) RESTORED`, `DAMAGED RECORD(S) REBUILT` and `WRITE-ONCE VIOLATION`, all
three of which had already shipped undocumented while this suite reported green.

So the direction is reversed. cairn.markers.Marker is the registry, the code can only print
its members, and the first test below asks whether every member is documented - a question
no regex can be blind to. The pattern-matching is kept, demoted to a backstop against a
marker written as a literal instead of added to the registry, which is the only way one can
still escape.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from cairn import cli
from cairn.config import find_root
from cairn.markers import Marker
from cairn.config import RELEASE_PAGE_NAME
from cairn.sync import GENERATED_NAMES

ROOT = find_root(Path(__file__).resolve().parent)
SOURCES = sorted((ROOT / "src" / "cairn").glob("*.py"))
REGISTRY = ROOT / "src" / "cairn" / "markers.py"
SYNC_LOOP = ROOT / "deploy" / "sync-loop.sh"
RUNBOOKS = [ROOT / "docs" / "concepts-and-gotchas.md", ROOT / "docs" / "README.md", ROOT / "README.md"]

MARKERS = {m.value for m in Marker}

# A marker-shaped string: two or more shouted words, where a word may carry the parentheses,
# hyphens and digits that successive versions of this scan were blind to. Deliberately wider
# than the markers themselves, because its job is to catch a marker nobody registered.
_SHOUTED = r"[A-Z][A-Z0-9()-]+"
MARKER_SHAPED = re.compile(rf"{_SHOUTED}(?: {_SHOUTED})+")

# Shouted phrases that are not markers. Listed rather than pattern-excluded so that adding one
# is a deliberate act with a reason attached, which is what the scan is for.
NOT_MARKERS = {
    "RELAX NG",  # render.ROLE_LABELS: the human name of a schema language, shown on a page.
    "W3C XML",   # render.ROLE_LABELS: "W3C XML Schema (XSD)", the same, and the reason digits
                 # are allowed above - without them "SHA256 MISMATCH" would slip past entirely.
}


# The loop prints markers of its own and an operator greps one log, not two. Everything it
# shows a human goes through its log() helper, so that is what is scanned: outside it the
# script is full of shouted words that are shell (`trap ... TERM INT`, the exit-code names).
SHELL_LOG_CALL = re.compile(r'log "([^"]*)"')


def _emitted_strings(source: Path) -> set[str]:
    """Every string literal a module could print, excluding its docstrings.

    Scanning the raw file text meant ordinary prose failed the suite: "the HTTP GET path" or
    "the JSON API returns" in any comment is two shouted words, and the author's only way out
    was to add it to NOT_MARKERS, a set reserved for deliberate exceptions with a reason.
    Comments and docstrings cannot reach a log, so the scan has no business reading them.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    }

def _runbook_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in RUNBOOKS)


@pytest.mark.parametrize("marker", sorted(MARKERS))
def test_every_marker_is_documented(marker):
    """Every marker cairn can print is one an operator may have to act on.

    Parametrised rather than aggregated so a failure names the marker rather than a list.
    """
    assert marker in _runbook_text(), (
        f"{marker!r} can reach a deployment log with no runbook entry. "
        f"Add it to docs/concepts-and-gotchas.md under 'When a cycle fails'."
    )


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_no_source_spells_out_a_marker_it_could_import(source):
    """The registry is only authoritative if nothing routes around it.

    A marker written as a literal at the print site is exactly what shipped undocumented three
    times: it reaches the log, and nothing that checks the docs has any way to know it exists.

    Emitted strings only, like its sibling below. Comments and docstrings cannot reach a log,
    and prose explaining why a guard exists naturally wants to name the marker it produces;
    reading the raw text forced that prose to spell the name as `Marker.X`, which reads as an
    implementation detail in a sentence written for a human.
    """
    if source == REGISTRY:
        return
    emitted = _emitted_strings(source)
    restated = sorted(m for m in MARKERS if any(m in s for s in emitted))
    assert not restated, (
        f"{source.name} spells out {restated} instead of using cairn.markers.Marker. "
        f"The docs check reads the registry, so a literal here is invisible to it."
    )


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_no_source_prints_an_unregistered_marker(source):
    """The backstop: a marker-shaped string that is not in the registry is either a new marker
    nobody registered, or a shouted phrase that needs listing in NOT_MARKERS with a reason."""
    found = {m.group(0) for s in _emitted_strings(source) for m in MARKER_SHAPED.finditer(s)}
    unknown = sorted(found - MARKERS - NOT_MARKERS)
    assert not unknown, (
        f"{source.name} contains marker-shaped strings that are not in cairn.markers.Marker: "
        f"{unknown}. Register them, or add them to NOT_MARKERS with a reason."
    )


def test_the_scan_ignores_prose_and_still_sees_what_gets_printed(tmp_path):
    """The scan reads what a module can print, not what it says about itself.

    Reading the raw file text made any comment containing two shouted words fail the suite,
    with NOT_MARKERS - a set for deliberate exceptions - as the only way out.
    """
    module = tmp_path / "sample.py"
    module.write_text(
        '''"""A docstring mentioning the HTTP GET path and the JSON API."""
# A comment about TLS SNI termination.
VALUE = "SOMETHING SHOUTED"


def f():
    """Another docstring naming the XML SCHEMA rules."""
    return "AND ONE MORE"
''',
        encoding="utf-8",
    )

    emitted = _emitted_strings(module)
    assert emitted == {"SOMETHING SHOUTED", "AND ONE MORE"}, emitted


def test_the_shell_loop_only_logs_registered_markers():
    """sync-loop.sh cannot import the registry, so it is pinned to it from this side."""
    logged = SHELL_LOG_CALL.findall(SYNC_LOOP.read_text(encoding="utf-8"))
    found = {m.group(0) for line in logged for m in MARKER_SHAPED.finditer(line)}
    unknown = sorted(found - MARKERS - NOT_MARKERS)
    assert not unknown, f"deploy/sync-loop.sh logs unregistered markers: {unknown}"
    assert Marker.BUILD_FAILED in found, (
        "the scan found no known marker in sync-loop.sh's log() calls; if that is empty the "
        "guard is blind to the one file here that cannot be checked by import"
    )


def test_the_exit_code_table_describes_the_unit_the_code_counts():
    """Code 5 means the run established nothing, and what "nothing" is counted in moved from
    standards to releases when a failing release stopped abandoning its siblings. The table,
    the constant's comment and the shell loop all went on saying "every standard failed", which
    is now false for a standard with one bad release and two good ones - the drift this module
    exists to catch, in the row it is most expensive to misread."""
    table = (ROOT / "docs" / "concepts-and-gotchas.md").read_text(encoding="utf-8")
    row = re.search(rf"^\| {cli.EXIT_NOTHING_SUCCEEDED} \| (.+)\|$", table, re.MULTILINE)
    assert row, "exit code 5 has no row"
    assert "release" in row.group(1), (
        f"the row for 5 says {row.group(1)!r}; nothing_succeeded counts releases, not standards"
    )


def test_the_cache_map_knows_every_generated_file_in_a_release_directory():
    """nginx.conf decides which files under a version URL may be cached for a year.

    A release directory holds write-once artifacts, which are immutable and should be, beside
    files the sync and the render rewrite - so the cache map names the second group to keep
    them short-lived. That list is a third copy of a decision config.RELEASE_PAGE_NAME and
    sync.GENERATED_NAMES were introduced to hold once, and nothing compared them. Changing the
    constant - the change it exists to make safe - would drop the release page into the
    `immutable` arm one line below: a year-long, unrecallable cache on a page re-rendered
    whenever a template changes.
    """
    conf = (ROOT / "deploy" / "nginx.conf").read_text(encoding="utf-8")
    short_lived = re.search(r"\(([^)]*)\)\?\$", conf)
    assert short_lived, "the cache map no longer has a group naming the re-rendered files"
    named = set(short_lived.group(1).replace("\\", "").split("|"))
    assert named == set(GENERATED_NAMES), (
        f"nginx.conf caches {sorted(named)} as short-lived, but the sync treats "
        f"{sorted(GENERATED_NAMES)} as generated. A file in one list and not the other is "
        f"either cached for a year by mistake or reaped by mistake."
    )


@pytest.mark.parametrize(
    "code",
    sorted({cli.EXIT_OK, cli.EXIT_INCOMPLETE, cli.EXIT_ATTENTION, cli.EXIT_STANDARD_FAILED,
            cli.EXIT_NOTHING_SUCCEEDED}),
)
def test_every_exit_code_has_a_documented_row(code):
    """The contract lives in three places. Two are pinned to each other by a test in
    test_cli.py; this pins the third, which had already drifted by one row."""
    table = (ROOT / "docs" / "concepts-and-gotchas.md").read_text(encoding="utf-8")
    assert re.search(rf"^\| {code} \| .+\|$", table, re.MULTILINE), (
        f"exit code {code} is returned by cairn but has no row in the exit-code table"
    )


def test_nginx_serves_the_release_page_the_render_actually_writes():
    """config.RELEASE_PAGE_NAME exists so the render and the sync's orphan reaper cannot
    disagree about the filename, and its docstring names a copy elsewhere as the hazard. Two
    copies live in deploy/nginx.conf, which is static and cannot import the constant, so the
    comparison has to happen here.

    The cache-map test above does not cover this: RELEASE_PAGE_NAME flows into GENERATED_NAMES,
    so renaming the constant keeps that test green while `index` and `try_files` still name the
    old file - and every release directory answers 404.
    """
    conf = (ROOT / "deploy" / "nginx.conf").read_text(encoding="utf-8")
    directives = [
        line.strip() for line in conf.splitlines()
        if line.strip().startswith(("index ", "try_files "))
    ]
    assert directives, "nginx.conf no longer resolves a directory to a page at all"
    for directive in directives:
        assert RELEASE_PAGE_NAME in directive, (
            f"nginx.conf serves '{directive}', but the render writes {RELEASE_PAGE_NAME!r}. "
            f"Every release directory would answer 404."
        )


def test_the_docs_do_not_cite_symbols_the_code_no_longer_has():
    """The runbook's reference section names the symbols a maintainer is told to go and read.
    `MUTABLE_STATUSES` outlived its deletion there by a whole commit titled 'pin the docs to
    the code', because nothing compared the two."""
    import re

    source = "".join(
        (ROOT / "src" / "cairn" / name).read_text(encoding="utf-8")
        for name in ("manifest.py", "sync.py", "config.py", "util.py", "cli.py", "markers.py")
    )
    for doc in sorted((ROOT / "docs").glob("*.md")) + [ROOT / "CONTRIBUTING.md"]:
        text = doc.read_text(encoding="utf-8")
        # Backticked ALL_CAPS or CamelCase names attributed to a src/cairn file on the same line.
        for line in text.splitlines():
            if "src/cairn/" not in line:
                continue
            for symbol in re.findall(r"`([A-Z][A-Za-z0-9_]{3,})`", line):
                assert symbol in source, (
                    f"{doc.name} sends a reader to src/cairn for `{symbol}`, which no longer exists"
                )
