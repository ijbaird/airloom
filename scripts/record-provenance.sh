#!/usr/bin/env bash
# Record resolved release inputs; this is evidence, not a reproducibility claim.
set -euo pipefail

exec python3 - "$@" <<'PY'
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path


class ProvenanceError(Exception):
    pass


class ProvenanceArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ProvenanceError(message)


def keyed_values(values: list[str], option: str) -> dict[str, str]:
    result = {}
    for value in values:
        key, separator, recorded = value.partition("=")
        if not separator or not key or not recorded or key in result:
            raise ProvenanceError(f"{option} requires unique NAME=VALUE entries")
        result[key] = recorded
    return result


def checksums(paths: list[str]) -> dict[str, str]:
    result = {}
    for value in paths:
        path = Path(value)
        if not path.is_file():
            raise ProvenanceError(f"bundle is not a regular file: {path}")
        if path.name in result:
            raise ProvenanceError(f"duplicate bundle name: {path.name}")
        digest = hashlib.sha256()
        with path.open("rb") as bundle:
            for block in iter(lambda: bundle.read(1024 * 1024), b""):
                digest.update(block)
        result[path.name] = digest.hexdigest()
    return result


def record(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_sha):
        raise ProvenanceError("--source-sha must be a full lowercase Git commit SHA")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.image_digest):
        raise ProvenanceError("--image-digest must be a full sha256 digest")
    if not args.image_ref.endswith("@" + args.image_digest):
        raise ProvenanceError("--image-ref must be pinned to --image-digest")
    for option, value in (("--runtime-commit", args.runtime_commit), ("--sdk-commit", args.sdk_commit)):
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ProvenanceError(f"{option} must be a full lowercase OSTree commit")
    for option, value in (("--image-ref", args.image_ref), ("--runtime-ref", args.runtime_ref), ("--sdk-ref", args.sdk_ref)):
        if not value or "\n" in value or "\r" in value:
            raise ProvenanceError(f"{option} must be a non-empty single-line ref")

    tool_refs = keyed_values(args.tool_ref, "--tool-ref")
    for name, commit in tool_refs.items():
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ProvenanceError(f"tool ref {name!r} must use a full immutable commit SHA")

    provenance = {
        "schema_version": 1,
        "source_sha": args.source_sha,
        "image_ref": args.image_ref,
        "image_digest": args.image_digest,
        "runtime_ref": args.runtime_ref,
        "runtime_commit": args.runtime_commit,
        "sdk_ref": args.sdk_ref,
        "sdk_commit": args.sdk_commit,
        "tool_versions": keyed_values(args.tool_version, "--tool-version"),
        "tool_refs": tool_refs,
        "checksums": checksums(args.bundle),
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent, prefix=output.name + ".", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(provenance, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = ProvenanceArgumentParser(description="Record resolved Flatpak release inputs")
    parser.add_argument("--output", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--runtime-ref", required=True)
    parser.add_argument("--runtime-commit", required=True)
    parser.add_argument("--sdk-ref", required=True)
    parser.add_argument("--sdk-commit", required=True)
    parser.add_argument("--tool-version", action="append", required=True)
    parser.add_argument("--tool-ref", action="append", required=True)
    parser.add_argument("--bundle", action="append", required=True)
    try:
        record(parser.parse_args())
    except (ProvenanceError, OSError) as error:
        print(f"provenance denied: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PY
