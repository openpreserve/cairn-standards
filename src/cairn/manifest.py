"""Load, validate, and model standard manifests (standards/<id>/standard.yaml)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from . import BASE_URL
from .config import GENERATED_NAMES, RULES_SEGMENT, schema_path, standards_dir
from .util import (
    _SAFE_ARTIFACT_NAME,
    _SAFE_REVISION,
    DecodeError,
    media_type_for,
    read_text,
    semver_key,
)


class ManifestError(Exception):
    """Raised when a manifest is structurally or semantically invalid."""


class Lifecycle(StrEnum):
    """Whether a release's bytes may still change.

    This carries the write-once promise, and it is the only field that does. It moves in one
    direction only, draft -> published, and `compare_to_baseline` refuses the reverse, so
    "has this version ever been published?" is answerable from the manifest alone and cannot
    be laundered by any sequence of edits.

    It replaced a six-value `status` enum that conflated this question with "does the URL
    answer 200?". `withdrawn` was the value that was neither mutable nor served, so every
    predicate written as "is it mutable?" or "is it served?" got it wrong, and a release
    routed stable -> withdrawn -> draft came back mutable with published bytes still on disk.
    Two orthogonal facts in one enum is what made that reachable; they are separate fields now.
    """

    DRAFT = "draft"
    PUBLISHED = "published"


# --------------------------------------------------------------------------- models


@dataclass
class Artifact:
    name: str
    role: str
    from_: str
    path: str | None = None
    ref: str | None = None
    repo: str | None = None
    asset: str | None = None
    release_tag: str | None = None
    url: str | None = None
    media_type: str | None = None
    title: str | None = None

    def content_type(self) -> str:
        return media_type_for(self.name, self.media_type)


class Publication:
    """Anything cairn replicates as one write-once unit: a release, or a rules revision.

    The two differ in what names them and where their bytes are served, and in nothing else.
    Both hold artifacts, both cross draft -> published exactly once, both can be withdrawn
    without being un-published, and both wear the same badge. So the syncer, the write-once
    gate and the render all work on this type, and a rules revision inherits every guard that
    was written for a release rather than getting a second, thinner copy of them. Every bug
    those guards exist for is reachable here too: the rules line is write-once for the same
    reason and by the same mechanism.

    Deliberately not a dataclass. Making it one fixes the field order of every subclass's
    constructor, so `Release(version=...)` would have to be spelled with `lifecycle` first.
    The subclasses declare their own fields; this declares what they must have in common.
    """

    lifecycle: Lifecycle
    artifacts: list[Artifact]
    served: bool
    maturity: str | None
    ref: str | None
    released: str | None
    notes: str | None

    @property
    def slug(self) -> str:
        """Path under ``/<id>/`` holding these bytes, and the name every log line uses.

        One string rather than a path plus a label, because the two must not be able to
        disagree: an operator reads a marker naming a unit and then goes looking for the
        directory it names.
        """
        raise NotImplementedError

    @property
    def noun(self) -> str:
        """What to call this in an operator-facing sentence."""
        raise NotImplementedError

    def record_identity(self) -> dict[str, object]:
        """The fields naming this unit in its provenance record.

        Kept on the model so the syncer writes both shapes without asking what it is holding.
        """
        raise NotImplementedError

    @property
    def content_name(self) -> str:
        """Filename of the optional prose beside the manifest, under ``content/``.

        Flat rather than mirroring `slug`, which has slashes in it for a rules revision and
        would otherwise ask an author to nest directories to write one paragraph.
        """
        raise NotImplementedError

    @property
    def order_key(self) -> tuple:
        """Sort order across a mixed list of releases and rules revisions.

        The leading discriminator keeps the two kinds apart, so nothing ever compares a semver
        triple against a dated label. Within the releases it is semver: sorted as text,
        v10.0.0 lands between v1.0.0 and v2.0.0, which is a mis-ordering a reader takes as a
        statement about which version is newer.
        """
        raise NotImplementedError

    @property
    def is_mutable(self) -> bool:
        """Whether a sync may overwrite these bytes in place."""
        return self.lifecycle is Lifecycle.DRAFT

    @property
    def ever_published(self) -> bool:
        """Whether this unit has ever made a promise a URL is holding.

        True forever once set, regardless of whether it is currently served: un-serving
        stops answering for it, it does not un-promise it.
        """
        return self.lifecycle is not Lifecycle.DRAFT

    @property
    def is_served(self) -> bool:
        """Un-served units stay listed but answer 410."""
        return self.served

    @property
    def label(self) -> str:
        """The single word shown on the page badge. Display only, never a guard.

        Derived rather than stored so that the word on the page cannot disagree with the
        behaviour: the old `status` field was both at once, which is how `beta` came to mean
        a frozen release that an author had every reason to read as a moving one.
        """
        if not self.served:
            return "withdrawn"
        if self.maturity:
            return self.maturity
        return "stable" if self.ever_published else "draft"


@dataclass
class Release(Publication):
    version: str
    lifecycle: Lifecycle
    artifacts: list[Artifact]
    served: bool = True
    maturity: str | None = None
    ref: str | None = None
    released: str | None = None
    notes: str | None = None

    @property
    def major(self) -> int:
        return int(self.version.split(".")[0])

    @property
    def slug(self) -> str:
        return f"v{self.version}"

    @property
    def noun(self) -> str:
        return "release"

    @property
    def content_name(self) -> str:
        return f"{self.version}.md"

    @property
    def order_key(self) -> tuple:
        return (0, semver_key(self.version))

    def record_identity(self) -> dict[str, object]:
        return {"version": self.version}


@dataclass
class RuleSet(Publication):
    """One revision of the validation rules for a whole major line.

    It hangs off the major line rather than off a release because that is what the rules
    themselves say: every `.sch` declares the namespace URI, `.../ead/v4`, and not a version.
    Publishing them under `/ead/v4/schematron/<revision>/` therefore matches the file's own
    statement, and an EAD 4.0.1 does not force the same rules to be republished beneath it.

    It is outside `releases:` for a harder reason. A published release may not gain an
    artifact - `compare_to_baseline` refuses it, because what a version publishes is fixed
    once it is published - so rules living inside a release could never be revised without
    minting a new schema version for rules that did not change the schema. Out here, a
    revision is its own write-once unit on its own cadence: EAD 4.0.0 stays frozen, byte for
    byte, while revisions appear and are frozen alongside it.
    """

    revision: str
    applies_to: int
    lifecycle: Lifecycle
    artifacts: list[Artifact]
    served: bool = True
    maturity: str | None = None
    ref: str | None = None
    released: str | None = None
    notes: str | None = None
    # The schema version this revision was written and tested against. Only the rules' author
    # knows it, so it is recorded from what they tell us and displayed; nothing is gated on it.
    tested_against: str | None = None
    # The earliest version in the line these rules make sense against, when that is not the
    # whole line. Optional because the usual answer is "the whole line" - rules target the
    # namespace, and the namespace is major-only - but a revision written for behaviour
    # introduced part-way through a line would be wrong applied to the versions before it, and
    # a reader has no way to know that unless we record it.
    minimum_version: str | None = None

    @property
    def slug(self) -> str:
        return f"v{self.applies_to}/{RULES_SEGMENT}/{self.revision}"

    @property
    def noun(self) -> str:
        return "rules revision"

    @property
    def content_name(self) -> str:
        return f"{RULES_SEGMENT}-v{self.applies_to}-{self.revision}.md"

    @property
    def order_key(self) -> tuple:
        return (1, self.applies_to, self.revision)

    def record_identity(self) -> dict[str, object]:
        return {
            "revision": self.revision,
            "applies_to": self.applies_to,
            "tested_against": self.tested_against,
            "minimum_version": self.minimum_version,
        }


@dataclass
class MajorLine:
    major: int
    latest: str
    namespace: str | None = None


@dataclass
class Steward:
    org: str
    homepage: str | None = None
    github: str | None = None
    contacts: list[str] = field(default_factory=list)


@dataclass
class Source:
    type: str
    repo: str
    ref: str | None = None


@dataclass
class Link:
    label: str
    url: str


@dataclass
class Standard:
    id: str
    title: str
    summary: str
    steward: Steward
    source: Source
    major_lines: list[MajorLine]
    releases: list[Release]
    rules: list[RuleSet] = field(default_factory=list)
    based_on: str | None = None
    links: list[Link] = field(default_factory=list)
    directory: Path | None = None

    # -- lookups ----------------------------------------------------------------

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.id}"

    @property
    def content_dir(self) -> Path | None:
        return self.directory / "content" if self.directory else None

    def release(self, version: str) -> Release | None:
        return next((r for r in self.releases if r.version == version), None)

    def major_line(self, major: int) -> MajorLine | None:
        return next((m for m in self.major_lines if m.major == major), None)

    def namespace_for(self, major: int) -> str:
        ml = self.major_line(major)
        if ml and ml.namespace:
            return ml.namespace
        return f"{BASE_URL}/{self.id}/v{major}"

    def sorted_releases(self) -> list[Release]:
        """Newest first."""
        return sorted(self.releases, key=lambda r: semver_key(r.version), reverse=True)

    def sorted_major_lines(self) -> list[MajorLine]:
        return sorted(self.major_lines, key=lambda m: m.major, reverse=True)

    def rule_set(self, major: int, revision: str) -> RuleSet | None:
        return next(
            (r for r in self.rules if r.applies_to == major and r.revision == revision), None
        )

    def sorted_rules(self, major: int) -> list[RuleSet]:
        """Every revision for one major line, newest first.

        Sorted as plain strings, which is total and correct because the schema constrains a
        revision to `YYYY-MM` or `YYYY-MM-DD`. That constraint is what earns the `latest`
        pointer below the right to exist: a free-form label would leave "newest" undefined,
        and a moving pointer whose target depends on how someone spelled a revision is worse
        than no pointer at all.
        """
        return sorted(
            (r for r in self.rules if r.applies_to == major), key=lambda r: r.revision, reverse=True
        )

    def latest_rules(self, major: int) -> RuleSet | None:
        """What ``/<id>/v<major>/schematron/latest/`` resolves to, or None if nothing does.

        The newest revision that is both published and served, and deliberately not merely the
        newest served one. A major line's `latest` for releases may point at a draft, but that
        target is written down by a person in `major_lines`; this one is derived by sorting, so
        a draft revision added to track a branch would capture the pointer with nobody
        deciding that it should. The pointer is the URL documentation is told to cite, and what
        it resolves to is served under a dated path with a year-long immutable cache, so aiming
        it at bytes that still follow a branch would have readers cache a moving draft as
        though it were frozen.

        Withdrawn revisions are skipped for the matching reason: their URLs answer 410, so
        pointing the current-rules pointer at one would resolve a live citation into a gone.

        None is a legitimate answer. Until a line has frozen its first revision there are no
        current rules to cite, and the drafts remain reachable at their own dated URLs.
        """
        return next(
            (r for r in self.sorted_rules(major) if r.ever_published and r.is_served), None
        )

    def publications(self) -> list[Publication]:
        """Everything the syncer replicates for this standard, in the order it does it.

        Releases first, because a rules revision's `tested_against` names one and a log read
        top to bottom should meet the version before the rules that cite it.
        """
        return [*self.releases, *self.rules]

    def publication(self, slug: str) -> Publication | None:
        """Look a release or a rules revision up by the path it is served at.

        The slug is the identity the write-once gate compares on, because it is exactly what
        a reader has: `/ead/v4.0.0` and `/ead/v4/schematron/2026-07` cannot collide, both
        halves are already refused a duplicate by validation, and matching on the URL means
        the gate is asking the same question a person would.
        """
        return next((p for p in self.publications() if p.slug == slug), None)


# --------------------------------------------------------------------------- loading


def _validator(root: Path) -> Draft202012Validator:
    path = schema_path(root)
    # find_root falls back to whatever it was given when it finds no marker, so a mistyped
    # path arrives here intact and would otherwise surface as a bare FileNotFoundError
    # traceback from read_text, which main() does not catch.
    if not path.is_file():
        raise ManifestError(
            f"{root}: no manifest schema at {path}. Is this a Cairn workspace? "
            f"It needs both standards/ and schemas/standard.schema.json."
        )
    try:
        schema = json.loads(read_text(path))
    except (OSError, DecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{path}: cannot be read as the manifest schema: {exc}") from exc
    return Draft202012Validator(schema)


def _build(data: dict[str, Any], directory: Path) -> Standard:
    st = data["steward"]
    steward = Steward(
        org=st["org"],
        homepage=st.get("homepage"),
        github=st.get("github"),
        contacts=list(st.get("contacts", [])),
    )
    src = data["source"]
    source = Source(type=src["type"], repo=src["repo"], ref=src.get("ref"))
    major_lines = [
        MajorLine(major=m["major"], latest=m["latest"], namespace=m.get("namespace"))
        for m in data["major_lines"]
    ]
    releases = [
        Release(
            version=r["version"],
            lifecycle=Lifecycle(r["lifecycle"]),
            artifacts=_artifacts(r),
            served=r.get("served", True),
            maturity=r.get("maturity"),
            ref=r.get("ref"),
            released=r.get("released"),
            notes=r.get("notes"),
        )
        for r in data["releases"]
    ]
    rules = [
        RuleSet(
            revision=r["revision"],
            applies_to=r["applies_to"],
            lifecycle=Lifecycle(r["lifecycle"]),
            artifacts=_artifacts(r),
            served=r.get("served", True),
            maturity=r.get("maturity"),
            ref=r.get("ref"),
            released=r.get("released"),
            notes=r.get("notes"),
            tested_against=r.get("tested_against"),
            minimum_version=r.get("minimum_version"),
        )
        for r in data.get("rules", [])
    ]
    return Standard(
        id=data["id"],
        title=data["title"],
        summary=data["summary"],
        steward=steward,
        source=source,
        major_lines=major_lines,
        releases=releases,
        rules=rules,
        based_on=data.get("based_on"),
        links=[Link(label=l["label"], url=l["url"]) for l in data.get("links", [])],
        directory=directory,
    )


def _artifacts(block: dict[str, Any]) -> list[Artifact]:
    """The artifact list of a release or of a rules revision. One shape, one reader."""
    return [
        Artifact(
            name=a["name"],
            role=a["role"],
            from_=a["from"],
            path=a.get("path"),
            ref=a.get("ref"),
            repo=a.get("repo"),
            asset=a.get("asset"),
            release_tag=a.get("release_tag"),
            url=a.get("url"),
            media_type=a.get("media_type"),
            title=a.get("title"),
        )
        for a in block["artifacts"]
    ]


_LOCATOR_REQUIREMENTS = {
    "repo": ("path",),
    "github-pages": ("path",),
    "release-asset": ("asset",),
    "url": ("url",),
}


def _rules_checks(std: Standard) -> list[str]:
    """Consistency the schema cannot express, for the rules line only.

    Every one of these guards a claim that ends up on a permanent page. A revision's URL and
    its stated compatibility are both frozen the moment it is published, so a typo here is not
    a validation nuisance, it is a false statement about what was tested that a later edit is
    then refused permission to correct.
    """
    errors: list[str] = []

    seen: set[tuple[int, str]] = set()
    for rules in std.rules:
        # Anchored here as well as in the schema, for the same reason as an artifact name: the
        # schema's pattern is ECMA-262, where `$` also matches before a trailing newline, and
        # this label is joined onto the document root and handed to mkdir().
        if not _SAFE_REVISION.match(rules.revision):
            errors.append(
                f"rules revision {rules.revision!r} is not a dated label (YYYY-MM or YYYY-MM-DD)"
            )
        key = (rules.applies_to, rules.revision)
        if key in seen:
            errors.append(
                f"duplicate rules revision {rules.revision} for major line v{rules.applies_to}"
            )
        seen.add(key)

        if std.major_line(rules.applies_to) is None:
            errors.append(
                f"rules revision {rules.revision} applies to major line v{rules.applies_to}, "
                f"which has no major_lines entry"
            )

        # Both fields name a version of *this* standard, so both are checkable, and checking
        # them is worth more here than elsewhere: a published release can never be dropped from
        # the manifest, so a reference that resolves today resolves forever. One that does not
        # resolve is a typo, and it would be displayed beside a checksum as though it were a
        # fact somebody had established.
        for field_name, version in (
            ("tested_against", rules.tested_against),
            ("minimum_version", rules.minimum_version),
        ):
            if version is None:
                continue
            target = std.release(version)
            if target is None:
                errors.append(
                    f"rules revision {rules.revision}: {field_name} {version} is not a release "
                    f"of this standard"
                )
            elif target.major != rules.applies_to:
                errors.append(
                    f"rules revision {rules.revision}: {field_name} {version} is in major line "
                    f"v{target.major}, but the revision applies to v{rules.applies_to}"
                )

        if (
            rules.tested_against
            and rules.minimum_version
            and semver_key(rules.tested_against) < semver_key(rules.minimum_version)
        ):
            errors.append(
                f"rules revision {rules.revision}: tested against {rules.tested_against}, which is "
                f"below its own stated minimum of {rules.minimum_version}"
            )

    return errors


def _semantic_checks(std: Standard, manifest_file: Path) -> list[str]:
    errors: list[str] = []
    if std.directory and std.id != std.directory.name:
        errors.append(f"id '{std.id}' does not match folder name '{std.directory.name}'")

    versions = [r.version for r in std.releases]
    dupes = {v for v in versions if versions.count(v) > 1}
    if dupes:
        errors.append(f"duplicate release versions: {', '.join(sorted(dupes))}")

    release_majors = {r.major for r in std.releases}
    for ml in std.major_lines:
        target = std.release(ml.latest)
        if target is None:
            errors.append(f"major_line v{ml.major} latest {ml.latest} has no matching release")
        elif target.major != ml.major:
            errors.append(
                f"major_line v{ml.major} latest {ml.latest} is in major line {target.major}"
            )
        elif not target.is_served:
            errors.append(f"major_line v{ml.major} latest {ml.latest} is withdrawn")

    for major in release_majors:
        if std.major_line(major) is None:
            errors.append(f"release major {major} has no major_lines entry")

    errors.extend(_rules_checks(std))

    for rel in std.publications():
        names = [a.name for a in rel.artifacts]
        dupe_names = {n for n in names if names.count(n) > 1}
        if dupe_names:
            errors.append(f"{rel.noun} {rel.slug}: duplicate artifact names: {', '.join(sorted(dupe_names))}")
        for art in rel.artifacts:
            # Checked here as well as in the schema. The schema's pattern has to be ECMA-262 so
            # that editors and other validators agree with us, and there `$` also matches before
            # a trailing newline; this anchor does not. The name is joined onto the unit's
            # directory and handed to atomic_write() and unlink().
            if not _SAFE_ARTIFACT_NAME.match(art.name):
                errors.append(
                    f"{rel.noun} {rel.slug}: artifact name {art.name!r} is not a bare filename"
                )
            # A collision is not merely confusing: the sync writes the artifact and then
            # overwrites that same path with its own metadata, so SHA256SUMS records a checksum
            # for a file holding the provenance document and `sha256sum -c` fails forever. On a
            # published release the restore then leaves non-JSON in provenance.json, which the
            # next cycle refuses as unreadable.
            if art.name in GENERATED_NAMES:
                errors.append(
                    f"{rel.noun} {rel.slug}: artifact name {art.name!r} is a file cairn generates "
                    f"({', '.join(sorted(GENERATED_NAMES))}); it would be overwritten by its own metadata"
                )
            required = _LOCATOR_REQUIREMENTS.get(art.from_, ())
            for field_name in required:
                if not getattr(art, field_name):
                    errors.append(
                        f"{rel.noun} {rel.slug} artifact '{art.name}': from='{art.from_}' requires '{field_name}'"
                    )
    return errors


def load_standard(directory: Path, validator: Draft202012Validator | None = None, root: Path | None = None) -> Standard:
    """Load and fully validate a single standard folder."""
    manifest_file = directory / "standard.yaml"
    if not manifest_file.is_file():
        raise ManifestError(f"{directory}: missing standard.yaml")
    try:
        data = yaml.safe_load(read_text(manifest_file))
    except (OSError, DecodeError) as exc:
        raise ManifestError(f"{manifest_file}: cannot be read: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ManifestError(f"{manifest_file}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{manifest_file}: top level must be a mapping")

    if validator is None:
        if root is None:
            raise ValueError("load_standard needs a validator or a root")
        validator = _validator(root)

    schema_errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if schema_errors:
        lines = [
            f"  - {'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
            for e in schema_errors
        ]
        raise ManifestError(f"{manifest_file}: schema validation failed:\n" + "\n".join(lines))

    # _build reads keys the schema has just guaranteed - but the schema it was checked against
    # is the one in *that* workspace, and a baseline worktree carries its own. So a manifest
    # from before a schema change passes its own validator and then meets model code that
    # expects the new shape. A KeyError there is a traceback with no marker and no file name;
    # this says which field and which revision.
    try:
        std = _build(data, directory)
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestError(
            f"{manifest_file}: valid against the schema beside it, but this build of cairn cannot "
            f"read it ({exc!r}). That normally means the manifest predates a schema change - the "
            f"write-once baseline check compares against another revision, and it cannot span one."
        ) from exc
    sem_errors = _semantic_checks(std, manifest_file)
    if sem_errors:
        raise ManifestError(
            f"{manifest_file}: consistency checks failed:\n" + "\n".join(f"  - {e}" for e in sem_errors)
        )
    return std


def artifact_locator(std: Standard, rel: Publication, art: Artifact) -> dict[str, str | None]:
    """The concrete upstream coordinates an artifact resolves to.

    Precedence is artifact, then release, then standard. It lives here rather than in the
    syncer so the write-once check and the fetch agree by construction. Comparing the literal
    manifest fields instead would miss a `source.ref` move that a frozen release silently
    inherits, which repoints published bytes without touching the release block at all.
    """
    return {
        "from": art.from_,
        "repo": art.repo or std.source.repo,
        "ref": art.ref or rel.ref or std.source.ref,
        "path": art.path,
        "url": art.url,
        "asset": art.asset,
        "release_tag": art.release_tag,
    }


def compare_to_baseline(current: list[Standard], baseline: list[Standard]) -> list[str]:
    """Report edits that would break an already-published URL.

    Write-once is a property of a manifest *edit*, so it is checkable the moment the edit is
    proposed, against the manifests as they were before it. Enforcing it only in the syncer
    means the offending change merges green and fails on the deployment instead, where the
    person who can fix it is not looking and the site has already stopped updating.

    A unit is published once its baseline lifecycle is `published`, and stays published for
    every later comparison. Whether it is currently served is deliberately not consulted: an
    earlier version of this function skipped un-served releases entirely, which let a
    published release be un-served in one commit and reverted to `draft` in the next with
    every check below never running.

    Releases and rules revisions go through the identical comparison, because they make the
    identical promise. Writing a second, rules-shaped copy of these checks would leave the
    newer line protected by whichever of them someone remembered to update.
    """
    errors: list[str] = []
    current_by_id = {s.id: s for s in current}

    # Driven from the baseline, because the question is "what was published before, and is it
    # still there?". Iterating the current set instead asks only about standards that still
    # exist, so deleting one outright, or renaming its id (a delete plus an add), was never
    # examined at all and passed the gate clean.
    for was in baseline:
        std = current_by_id.get(was.id)
        if std is None:
            published = [
                p.slug for p in sorted(was.publications(), key=lambda p: p.order_key)
                if p.ever_published
            ]
            if published:
                errors.append(
                    f"{was.id}: the whole standard was removed, but {', '.join(published)} "
                    f"{'is' if len(published) == 1 else 'are'} published. Every URL under "
                    f"/{was.id}/ would stop resolving. If it is being renamed, the old id has to "
                    f"stay published."
                )
            continue
        for old_unit in was.publications():
            if not old_unit.ever_published:
                continue
            errors.extend(_compare_publication(was, old_unit, std))
    return errors


def _compare_publication(was: Standard, old: Publication, std: Standard) -> list[str]:
    """What a manifest edit may not do to one already-published release or rules revision."""
    errors: list[str] = []
    # Matched on the served path, so moving a rules revision to another major line reads as
    # what it is - the old URL is gone - rather than as an edit to the same unit.
    new = std.publication(old.slug)
    if new is None:
        return [
            f"{std.id} {old.slug}: this {old.noun} was published and is now gone. "
            f"Published {old.noun}s must stay in the manifest."
        ]

    # The one forbidden transition. Reverting to draft un-freezes bytes that have already been
    # handed out, which defeats the guarantee just as surely as editing them: the next sync
    # would start overwriting it in place.
    if not new.ever_published:
        errors.append(
            f"{std.id} {old.slug}: lifecycle went from 'published' back to "
            f"'{new.lifecycle}'. That un-freezes a published {old.noun} and lets later "
            f"syncs overwrite it in place. To stop serving it, set served: false instead."
        )

    # Adding to a published unit changes what it publishes, retroactively, exactly as removing
    # does. It also has to be refused because the syncer relies on it: an artifact with no
    # record and no file on a published unit is read as one the volume lost, not one the
    # manifest gained, and without this gate that edit merged green and then failed the
    # standard every cycle with UNVERIFIABLE PUBLISHED FILE, telling the operator to restore a
    # file that had never existed.
    added = sorted({a.name for a in new.artifacts} - {a.name for a in old.artifacts})
    if added:
        errors.append(
            f"{std.id} {old.slug}: artifact(s) added to a published {old.noun}: "
            f"{', '.join(added)}. What a {old.noun} publishes is fixed once it is published; "
            f"publish the addition as a new one."
        )

    lost = sorted({a.name for a in old.artifacts} - {a.name for a in new.artifacts})
    if lost:
        errors.append(
            f"{std.id} {old.slug}: artifact(s) removed from a published {old.noun}: "
            f"{', '.join(lost)}. Those URLs are live; publish the change as a new one."
        )

    # Same URL, different upstream bytes. Compared on the resolved locator rather than the
    # literal fields, so an inherited `source.ref` or unit-level `ref` move is caught even when
    # the artifact block itself is untouched.
    # Grouped by what moved, because one edit to a unit or standard ref repoints every artifact
    # that inherits it, and N copies of the same sentence buries the single change that caused
    # them.
    new_by_name = {a.name: a for a in new.artifacts}
    repoints: dict[str, list[str]] = {}
    for old_art in old.artifacts:
        new_art = new_by_name.get(old_art.name)
        if new_art is None:
            continue
        old_loc = artifact_locator(was, old, old_art)
        new_loc = artifact_locator(std, new, new_art)
        if old_loc == new_loc:
            continue
        detail = "; ".join(
            f"{k}: {old_loc[k]!r} -> {new_loc[k]!r}"
            for k in sorted(old_loc)
            if old_loc[k] != new_loc[k]
        )
        repoints.setdefault(detail, []).append(old_art.name)

    for detail, names in repoints.items():
        errors.append(
            f"{std.id} {old.slug}: source repointed on a published {old.noun} for "
            f"{', '.join(names)} ({detail}). Those URLs are live and their bytes must "
            f"not change; publish the new source as a new one."
        )
    return errors


def load_all(root: Path) -> list[Standard]:
    """Load every standard in the workspace, sorted by id. Raises on the first invalid one."""
    validator = _validator(root)
    base = standards_dir(root)
    if not base.is_dir():
        return []
    standards: list[Standard] = []
    for directory in sorted(p for p in base.iterdir() if p.is_dir()):
        if not (directory / "standard.yaml").is_file():
            continue
        standards.append(load_standard(directory, validator=validator))
    ids = [s.id for s in standards]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ManifestError(f"duplicate standard ids across manifests: {', '.join(sorted(dupes))}")
    return standards
