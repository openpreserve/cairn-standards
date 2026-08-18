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
from .config import GENERATED_NAMES, schema_path, standards_dir
from .util import _SAFE_ARTIFACT_NAME, DecodeError, media_type_for, read_text, semver_key


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


@dataclass
class Release:
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
    def is_mutable(self) -> bool:
        """Whether a sync may overwrite these bytes in place."""
        return self.lifecycle is Lifecycle.DRAFT

    @property
    def ever_published(self) -> bool:
        """Whether this version has ever made a promise a URL is holding.

        True forever once set, regardless of whether it is currently served: un-serving a
        release stops answering for it, it does not un-promise it.
        """
        return self.lifecycle is not Lifecycle.DRAFT

    @property
    def is_served(self) -> bool:
        """Un-served releases stay listed but answer 410."""
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
    releases = []
    for r in data["releases"]:
        artifacts = [
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
            for a in r["artifacts"]
        ]
        releases.append(
            Release(
                version=r["version"],
                lifecycle=Lifecycle(r["lifecycle"]),
                artifacts=artifacts,
                served=r.get("served", True),
                maturity=r.get("maturity"),
                ref=r.get("ref"),
                released=r.get("released"),
                notes=r.get("notes"),
            )
        )
    return Standard(
        id=data["id"],
        title=data["title"],
        summary=data["summary"],
        steward=steward,
        source=source,
        major_lines=major_lines,
        releases=releases,
        based_on=data.get("based_on"),
        links=[Link(label=l["label"], url=l["url"]) for l in data.get("links", [])],
        directory=directory,
    )


_LOCATOR_REQUIREMENTS = {
    "repo": ("path",),
    "github-pages": ("path",),
    "release-asset": ("asset",),
    "url": ("url",),
}


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

    for rel in std.releases:
        names = [a.name for a in rel.artifacts]
        dupe_names = {n for n in names if names.count(n) > 1}
        if dupe_names:
            errors.append(f"release {rel.version}: duplicate artifact names: {', '.join(sorted(dupe_names))}")
        for art in rel.artifacts:
            # Checked here as well as in the schema. The schema's pattern has to be ECMA-262 so
            # that editors and other validators agree with us, and there `$` also matches before
            # a trailing newline; this anchor does not. The name is joined onto the release
            # directory and handed to atomic_write() and unlink().
            if not _SAFE_ARTIFACT_NAME.match(art.name):
                errors.append(
                    f"release {rel.version}: artifact name {art.name!r} is not a bare filename"
                )
            # A collision is not merely confusing: the sync writes the artifact and then
            # overwrites that same path with its own metadata, so SHA256SUMS records a checksum
            # for a file holding the provenance document and `sha256sum -c` fails forever. On a
            # published release the restore then leaves non-JSON in provenance.json, which the
            # next cycle refuses as unreadable.
            if art.name in GENERATED_NAMES:
                errors.append(
                    f"release {rel.version}: artifact name {art.name!r} is a file cairn generates "
                    f"({', '.join(sorted(GENERATED_NAMES))}); it would be overwritten by its own metadata"
                )
            required = _LOCATOR_REQUIREMENTS.get(art.from_, ())
            for field_name in required:
                if not getattr(art, field_name):
                    errors.append(
                        f"release {rel.version} artifact '{art.name}': from='{art.from_}' requires '{field_name}'"
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

    std = _build(data, directory)
    sem_errors = _semantic_checks(std, manifest_file)
    if sem_errors:
        raise ManifestError(
            f"{manifest_file}: consistency checks failed:\n" + "\n".join(f"  - {e}" for e in sem_errors)
        )
    return std


def artifact_locator(std: Standard, rel: Release, art: Artifact) -> dict[str, str | None]:
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

    A release is published once its baseline lifecycle is `published`, and stays published
    for every later comparison. Whether it is currently served is deliberately not consulted:
    an earlier version of this function skipped un-served releases entirely, which let a
    published release be un-served in one commit and reverted to `draft` in the next with
    every check below never running.
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
            published = sorted(
                (r.version for r in was.releases if r.ever_published),
                key=semver_key,
            )
            if published:
                errors.append(
                    f"{was.id}: the whole standard was removed, but v{', v'.join(published)} "
                    f"{'is' if len(published) == 1 else 'are'} published. Every URL under "
                    f"/{was.id}/ would stop resolving. If it is being renamed, the old id has to "
                    f"stay published."
                )
            continue
        for old_rel in was.releases:
            if not old_rel.ever_published:
                continue
            new_rel = std.release(old_rel.version)
            if new_rel is None:
                errors.append(
                    f"{std.id} v{old_rel.version}: release was published and is now gone. "
                    f"Published versions must stay in the manifest."
                )
                continue
            # The one forbidden transition. Reverting to draft un-freezes bytes that have
            # already been handed out, which defeats the guarantee just as surely as editing
            # them: the next sync would start overwriting the version in place.
            if not new_rel.ever_published:
                errors.append(
                    f"{std.id} v{old_rel.version}: lifecycle went from 'published' back to "
                    f"'{new_rel.lifecycle}'. That un-freezes a published version and lets later "
                    f"syncs overwrite it in place. To stop serving it, set served: false instead."
                )

            # Adding to a published release changes what that version publishes, retroactively,
            # exactly as removing does. It also has to be refused because the syncer relies on
            # it: an artifact with no record and no file on a published release is read as one
            # the volume lost, not one the manifest gained, and without this gate that edit
            # merged green and then failed the standard every cycle with UNVERIFIABLE PUBLISHED
            # FILE, telling the operator to restore a file that had never existed.
            added = sorted({a.name for a in new_rel.artifacts} - {a.name for a in old_rel.artifacts})
            if added:
                errors.append(
                    f"{std.id} v{old_rel.version}: artifact(s) added to a published release: "
                    f"{', '.join(added)}. What a version publishes is fixed once it is published; "
                    f"publish the addition as a new version."
                )

            lost = sorted({a.name for a in old_rel.artifacts} - {a.name for a in new_rel.artifacts})
            if lost:
                errors.append(
                    f"{std.id} v{old_rel.version}: artifact(s) removed from a published release: "
                    f"{', '.join(lost)}. Those URLs are live; publish the change as a new version."
                )

            # Same URL, different upstream bytes. Compared on the resolved locator rather than
            # the literal fields, so an inherited `source.ref` or `release.ref` move is caught
            # even when the artifact block itself is untouched.
            # Grouped by what moved, because one edit to a release or standard ref repoints
            # every artifact that inherits it, and N copies of the same sentence buries the
            # single change that caused them.
            new_by_name = {a.name: a for a in new_rel.artifacts}
            repoints: dict[str, list[str]] = {}
            for old_art in old_rel.artifacts:
                new_art = new_by_name.get(old_art.name)
                if new_art is None:
                    continue
                old_loc = artifact_locator(was, old_rel, old_art)
                new_loc = artifact_locator(std, new_rel, new_art)
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
                    f"{std.id} v{old_rel.version}: source repointed on a published release for "
                    f"{', '.join(names)} ({detail}). Those URLs are live and their bytes must "
                    f"not change; publish the new source as a new version."
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
