#!/usr/bin/env python3
"""Bind downloaded release artifacts to the intended source commit and bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


class ProvenanceError(Exception):
    pass


class ProvenanceArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ProvenanceError(message)


def load_provenance(path: Path, source_sha: str) -> dict:
    if not path.is_file():
        raise ProvenanceError(f"provenance is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ProvenanceError(f"invalid provenance JSON in {path}: {error}") from error
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ProvenanceError(f"unsupported provenance schema: {path}")
    if value.get("source_sha") != source_sha:
        raise ProvenanceError(
            f"{path} source_sha {value.get('source_sha')!r} does not match {source_sha}"
        )
    checksums = value.get("checksums")
    if not isinstance(checksums, dict) or not checksums:
        raise ProvenanceError(f"provenance has no bundle checksums: {path}")
    for name, digest in checksums.items():
        if not isinstance(name, str) or not name.endswith(".flatpak"):
            raise ProvenanceError(f"invalid bundle name in {path}: {name!r}")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ProvenanceError(f"invalid checksum for {name!r} in {path}")
    return value


def bundle_digest(path: Path) -> str:
    if not path.is_file():
        raise ProvenanceError(f"bundle is not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_sha):
        raise ProvenanceError("--source-sha must be a full lowercase Git commit SHA")

    provenance_paths = [Path(value) for value in args.provenance]
    if len(set(provenance_paths)) != len(provenance_paths):
        raise ProvenanceError("duplicate --provenance path")
    records = {
        path.resolve(): load_provenance(path, args.source_sha)
        for path in provenance_paths
    }

    bundle_paths = [Path(value) for value in args.bundle]
    if len(set(bundle_paths)) != len(bundle_paths):
        raise ProvenanceError("duplicate --bundle path")
    for bundle in bundle_paths:
        if not bundle.name.endswith(".flatpak"):
            raise ProvenanceError(f"bundle name must end in .flatpak: {bundle}")
        provenance = bundle.with_name(
            bundle.name.removesuffix(".flatpak") + ".provenance.json"
        ).resolve()
        if provenance not in records:
            raise ProvenanceError(f"missing matching --provenance for bundle: {bundle}")
        expected = records[provenance]["checksums"].get(bundle.name)
        actual = bundle_digest(bundle)
        if expected != actual:
            raise ProvenanceError(
                f"bundle checksum mismatch for {bundle.name}: expected {expected!r}, got {actual}"
            )


def main() -> int:
    parser = ProvenanceArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--provenance", action="append", required=True)
    parser.add_argument("--bundle", action="append", default=[])
    try:
        validate(parser.parse_args())
    except (ProvenanceError, OSError) as error:
        print(f"release provenance denied: {error}", file=sys.stderr)
        return 1
    print("release_provenance_verified=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
