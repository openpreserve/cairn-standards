"""Load, validate, and model standard manifests (standards/<id>/standard.yaml)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from . import BASE_URL
from .config import schema_path, standards_dir
from .util import media_type_for, semver_key


class ManifestError(Exception):
    """Raised when a manifest is structurally or semantically invalid."""


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
    status: str
    artifacts: list[Artifact]
    ref: str | None = None
    released: str | None = None
    notes: str | None = None

    @property
    def major(self) -> int:
        return int(self.version.split(".")[0])

    @property
    def is_served(self) -> bool:
        """Withdrawn releases are listed but served as 410."""
        return self.status != "withdrawn"


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
    schema = json.loads(schema_path(root).read_text(encoding="utf-8"))
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
                status=r["status"],
                artifacts=artifacts,
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
        data = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - passthrough
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
