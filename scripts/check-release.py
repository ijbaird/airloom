#!/usr/bin/env python3
"""Deny publication unless a stable tag and its source versions are eligible."""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


VERSION = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
TAG_PATTERN = re.compile(rf"v{VERSION}")
VERSION_PATTERN = re.compile(VERSION)


class ReleaseError(Exception):
    pass


class ReleaseArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ReleaseError(message)


def package_version(root: Path) -> str:
    source = (root / "airloom" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(root / "airloom" / "__init__.py"))
    assignments = []
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if any(isinstance(target, ast.Name) and target.id == "__version__" for target in targets):
            if statement.value is None or not isinstance(statement.value, ast.Constant):
                raise ReleaseError("airloom/__init__.py __version__ must be a string literal")
            assignments.append(statement.value.value)
    if len(assignments) != 1 or not isinstance(assignments[0], str):
        raise ReleaseError("airloom/__init__.py must define __version__ exactly once")
    return assignments[0]


def changelog_version(root: Path) -> str:
    source = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(r"^##[ \t]+([^ \t]+)", source, flags=re.MULTILINE)
    if not match:
        raise ReleaseError("CHANGELOG.md has no release heading")
    return match.group(1)


def appstream_version(root: Path) -> str:
    path = root / "packaging" / "ai.stealthvision.Airloom.metainfo.xml"
    release = ET.parse(path).find("./releases/release")
    if release is None or not release.attrib.get("version"):
        raise ReleaseError("AppStream metadata has no current release version")
    return release.attrib["version"]


def repository_tags(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "tag", "--list", "v*"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [tag for tag in result.stdout.splitlines() if TAG_PATTERN.fullmatch(tag)]


def version_key(version: str) -> tuple[int, int, int]:
    match = VERSION_PATTERN.fullmatch(version)
    if not match:
        raise ReleaseError(f"invalid semantic version: {version!r}")
    return tuple(int(part) for part in match.groups())


def validate(args: argparse.Namespace) -> tuple[str, str]:
    ref = args.ref
    tag = ref.removeprefix("refs/tags/")
    if not TAG_PATTERN.fullmatch(tag):
        raise ReleaseError(f"ref is not a stable vX.Y.Z tag: {ref!r}")
    version = tag[1:]

    root = Path(args.version_root)
    versions = {
        "airloom/__init__.py": package_version(root),
        "CHANGELOG.md": changelog_version(root),
        "AppStream metadata": appstream_version(root),
    }
    for name, recorded in versions.items():
        if not VERSION_PATTERN.fullmatch(recorded):
            raise ReleaseError(f"{name} has malformed semantic version {recorded!r}")
        if recorded != version:
            raise ReleaseError(
                f"{name} version {recorded!r} does not match tag {tag!r}"
            )

    tags = repository_tags(root)
    if tag not in tags:
        raise ReleaseError(f"tag {tag!r} does not exist in repository history")
    if not tags:
        raise ReleaseError("no stable release tags found in repository history")
    latest = max(tags, key=lambda candidate: version_key(candidate[1:]))
    if version_key(version) < version_key(latest[1:]):
        if not args.allow_older:
            raise ReleaseError(
                f"tag {tag} is older than latest tag {latest}; --allow-older is required"
            )
        if args.confirm_tag != tag:
            raise ReleaseError(
                f"older tag {tag} requires exact --confirm-tag {tag} confirmation"
            )
    elif args.confirm_tag is not None:
        if not args.allow_older:
            raise ReleaseError("--confirm-tag requires --allow-older")
        if args.confirm_tag != tag:
            raise ReleaseError("--confirm-tag must exactly match the release tag")

    return tag, version


def main() -> int:
    parser = ReleaseArgumentParser(description=__doc__)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--version-root", required=True)
    parser.add_argument("--allow-older", action="store_true")
    parser.add_argument("--confirm-tag")
    try:
        args = parser.parse_args()
        tag, version = validate(args)
    except (ReleaseError, OSError, SyntaxError, ET.ParseError, subprocess.SubprocessError) as error:
        print(f"release eligibility denied: {error}", file=sys.stderr)
        return 1

    print("release_eligible=true")
    print(f"release_tag={tag}")
    print(f"version={version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
