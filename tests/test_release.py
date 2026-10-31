from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import contextmanager
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHECK_RELEASE = PROJECT_ROOT / "scripts" / "check-release.py"
CHECK_PROVENANCE = PROJECT_ROOT / "scripts" / "check-release-provenance.py"
RECORD_PROVENANCE = PROJECT_ROOT / "scripts" / "record-provenance.sh"
IMPORT_SIGNING_KEY = PROJECT_ROOT / "scripts" / "import-signing-key.sh"
VERIFY_CLIENT = PROJECT_ROOT / "scripts" / "verify-flatpak-release.sh"
PUBLISH_REPO = PROJECT_ROOT / "scripts" / "publish-flatpak-repo.sh"
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "flatpak.yml"
PUBLISH_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "publish.yml"
RELEASE_GUIDE = PROJECT_ROOT / "RELEASE.md"
README = PROJECT_ROOT / "README.md"
SOURCE_SHA = "a" * 40
RUNTIME_COMMIT = "b" * 64
SDK_COMMIT = "c" * 64
IMAGE_DIGEST = "sha256:" + "d" * 64
IMAGE_REF = f"ghcr.io/flathub-infra/flatpak-github-actions@{IMAGE_DIGEST}"
TURNSTYLE_SHA = "3805450be63b8c80577dc253e8c17e9132036df4"


def run_git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def write_version_files(
    root: Path,
    *,
    package: str = "1.2.3",
    changelog: str = "1.2.3",
    appstream: str = "1.2.3",
) -> None:
    (root / "airloom").mkdir(parents=True, exist_ok=True)
    (root / "packaging").mkdir(parents=True, exist_ok=True)
    (root / "airloom" / "__init__.py").write_text(
        f'__version__ = "{package}"\n', encoding="utf-8"
    )
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## {changelog} — 2026-10-28\n\n- Fixture release.\n",
        encoding="utf-8",
    )
    (root / "packaging" / "ai.stealthvision.Airloom.metainfo.xml").write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<component type=\"desktop-application\">\n"
        "  <releases>\n"
        f"    <release version=\"{appstream}\" date=\"2026-10-28\" />\n"
        "  </releases>\n"
        "</component>\n",
        encoding="utf-8",
    )


@contextmanager
def release_checkout(*, tags=("v1.2.2", "v1.2.3"), **versions):
    with tempfile.TemporaryDirectory(prefix="airloom-release-test-") as temporary:
        root = Path(temporary)
        write_version_files(root, **versions)
        run_git(root, "init", "-q")
        run_git(root, "config", "user.name", "Airloom Test")
        run_git(root, "config", "user.email", "airloom@example.invalid")
        run_git(root, "add", ".")
        run_git(root, "commit", "-qm", "fixture")
        for tag in tags:
            run_git(root, "tag", tag)
        yield root


def check_release(root: Path, ref: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(CHECK_RELEASE),
            "--ref",
            ref,
            "--version-root",
            str(root),
            *args,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def provenance_command(
    output: Path, bundle: Path, *, repository_tar: Path | None = None
) -> list[str]:
    command = [
        str(RECORD_PROVENANCE),
        "--output",
        str(output),
        "--source-sha",
        SOURCE_SHA,
        "--image-ref",
        IMAGE_REF,
        "--image-digest",
        IMAGE_DIGEST,
        "--runtime-ref",
        "runtime/org.gnome.Platform/x86_64/50",
        "--runtime-commit",
        RUNTIME_COMMIT,
        "--sdk-ref",
        "runtime/org.gnome.Sdk/x86_64/50",
        "--sdk-commit",
        SDK_COMMIT,
        "--tool-version",
        "flatpak=Flatpak 1.16.1",
        "--tool-version",
        "flatpak-builder=1.4.4",
        "--tool-ref",
        "actions/checkout=3d3c42e5aac5ba805825da76410c181273ba90b1",
        "--tool-ref",
        "flatpak/flatpak-github-actions/flatpak-builder=79327416609af08178ad73b352877e51450790b3",
        "--bundle",
        str(bundle),
    ]
    if repository_tar is not None:
        command.extend(("--repository-tar", str(repository_tar)))
    return command


def job_block(source: str, job_name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(job_name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        source,
    )
    return match.group(1) if match else ""


def step_block(job: str, step_name: str) -> str:
    match = re.search(
        rf"(?ms)^      - name: {re.escape(step_name)}\n(.*?)(?=^      - |\Z)",
        job,
    )
    return match.group(1) if match else ""


def run_block(step: str) -> str:
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


class ReleaseEligibilityTest(unittest.TestCase):
    def assert_denied(self, result: subprocess.CompletedProcess) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(
            result.stderr.startswith("release eligibility denied:"),
            result.stdout + result.stderr,
        )

    def assert_eligible(self, result: subprocess.CompletedProcess) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("release_eligible=true", result.stdout)

    def test_branch_and_pull_request_refs_are_denied_even_with_older_override(self):
        with release_checkout() as root:
            for ref in ("refs/heads/main", "main", "refs/pull/7/merge"):
                with self.subTest(ref=ref):
                    result = check_release(
                        root,
                        ref,
                        "--allow-older",
                        "--confirm-tag",
                        "v1.2.3",
                    )
                    self.assert_denied(result)

    def test_malformed_stable_tags_are_denied(self):
        with release_checkout() as root:
            for ref in (
                "refs/tags/1.2.3",
                "refs/tags/v1.2",
                "refs/tags/v01.2.3",
                "refs/tags/v1.2.3.4",
                "refs/tags/v1.2.3-rc1",
            ):
                with self.subTest(ref=ref):
                    self.assert_denied(check_release(root, ref))

    def test_all_three_version_files_must_match_the_tag(self):
        scenarios = (
            {"package": "1.2.4"},
            {"changelog": "1.2.2"},
            {"appstream": "1.2.2"},
            {"package": "1.2.3-rc1"},
        )
        for versions in scenarios:
            with self.subTest(versions=versions), release_checkout(**versions) as root:
                self.assert_denied(check_release(root, "refs/tags/v1.2.3"))

    def test_latest_tag_and_current_tag_rerun_are_eligible(self):
        with release_checkout() as root:
            first = check_release(root, "refs/tags/v1.2.3")
            rerun = check_release(root, "refs/tags/v1.2.3")
            self.assert_eligible(first)
            self.assert_eligible(rerun)
            self.assertEqual(first.stdout, rerun.stdout)

    def test_latest_tag_comparison_is_numeric(self):
        with release_checkout(
            tags=("v1.9.0", "v1.10.0"),
            package="1.10.0",
            changelog="1.10.0",
            appstream="1.10.0",
        ) as root:
            self.assert_eligible(check_release(root, "refs/tags/v1.10.0"))

    def test_older_tag_requires_allow_older_and_exact_typed_confirmation(self):
        scenarios = (
            (),
            ("--allow-older",),
            ("--confirm-tag", "v1.2.2"),
            ("--allow-older", "--confirm-tag", "v1.2.3"),
            ("--allow-older", "--confirm-tag", "refs/tags/v1.2.2"),
        )
        for args in scenarios:
            with self.subTest(args=args), release_checkout(
                package="1.2.2", changelog="1.2.2", appstream="1.2.2"
            ) as root:
                self.assert_denied(check_release(root, "refs/tags/v1.2.2", *args))

    def test_confirmed_older_tag_is_eligible(self):
        with release_checkout(
            package="1.2.2", changelog="1.2.2", appstream="1.2.2"
        ) as root:
            result = check_release(
                root,
                "refs/tags/v1.2.2",
                "--allow-older",
                "--confirm-tag",
                "v1.2.2",
            )
            self.assert_eligible(result)

    def test_tag_must_exist_in_repository_history(self):
        with release_checkout(tags=("v1.2.2",)) as root:
            self.assert_denied(check_release(root, "refs/tags/v1.2.3"))

    def test_moved_tag_is_denied_for_the_captured_source_commit(self):
        with release_checkout() as root:
            intended = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            initial = check_release(
                root, "refs/tags/v1.2.3", "--source-sha", intended
            )
            self.assert_eligible(initial)
            self.assertIn(f"source_sha={intended}", initial.stdout)

            run_git(root, "commit", "--allow-empty", "-qm", "same version, new source")
            moved = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            run_git(root, "tag", "-f", "v1.2.3", moved)
            run_git(root, "reset", "--hard", intended)

            publication = check_release(
                root, "refs/tags/v1.2.3", "--source-sha", intended
            )
            self.assert_denied(publication)
            self.assertIn("tag v1.2.3 peeled commit", publication.stderr)
            self.assertIn("does not match intended source", publication.stderr)

    def test_annotated_tag_is_peeled_to_the_intended_source_commit(self):
        with release_checkout(tags=("v1.2.2",)) as root:
            run_git(root, "tag", "-a", "v1.2.3", "-m", "release v1.2.3")
            intended = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            tag_object = subprocess.check_output(
                ["git", "rev-parse", "refs/tags/v1.2.3"], cwd=root, text=True
            ).strip()
            self.assertNotEqual(tag_object, intended)

            result = check_release(
                root, "refs/tags/v1.2.3", "--source-sha", intended
            )
            self.assert_eligible(result)


class ProvenanceTest(unittest.TestCase):
    def assert_denied(self, result: subprocess.CompletedProcess) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(
            result.stderr.startswith("provenance denied:"),
            result.stdout + result.stderr,
        )

    def test_records_source_image_runtime_tools_and_real_bundle_checksum(self):
        with tempfile.TemporaryDirectory(prefix="airloom-provenance-test-") as temporary:
            root = Path(temporary)
            output = root / "provenance.json"
            bundle = root / "Airloom-x86_64.flatpak"
            bundle.write_bytes(b"disposable fixture bundle\n")
            result = subprocess.run(
                provenance_command(output, bundle),
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            provenance = json.loads(output.read_text(encoding="utf-8"))

            self.assertEqual(provenance["source_sha"], SOURCE_SHA)
            self.assertEqual(provenance["image_ref"], IMAGE_REF)
            self.assertEqual(provenance["image_digest"], IMAGE_DIGEST)
            self.assertEqual(
                provenance["runtime_ref"], "runtime/org.gnome.Platform/x86_64/50"
            )
            self.assertEqual(provenance["runtime_commit"], RUNTIME_COMMIT)
            self.assertEqual(provenance["sdk_ref"], "runtime/org.gnome.Sdk/x86_64/50")
            self.assertEqual(provenance["sdk_commit"], SDK_COMMIT)
            self.assertEqual(
                provenance["tool_versions"],
                {"flatpak": "Flatpak 1.16.1", "flatpak-builder": "1.4.4"},
            )
            self.assertEqual(
                provenance["tool_refs"]["actions/checkout"],
                "3d3c42e5aac5ba805825da76410c181273ba90b1",
            )
            self.assertEqual(
                provenance["checksums"],
                {
                    bundle.name: hashlib.sha256(
                        b"disposable fixture bundle\n"
                    ).hexdigest()
                },
            )

    def test_records_update_repository_tar_payload_checksum(self):
        with tempfile.TemporaryDirectory(prefix="airloom-provenance-test-") as temporary:
            root = Path(temporary)
            output = root / "provenance.json"
            bundle = root / "Airloom-x86_64.flatpak"
            bundle.write_bytes(b"disposable fixture bundle\n")
            repository_tar = root / "airloom-repo-x86_64.tar"
            repository_tar.write_bytes(b"disposable repository tar payload\n")

            result = subprocess.run(
                provenance_command(output, bundle, repository_tar=repository_tar),
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            provenance = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                provenance["repository_checksums"],
                {
                    repository_tar.name: hashlib.sha256(
                        b"disposable repository tar payload\n"
                    ).hexdigest()
                },
            )

    def test_required_fields_fail_closed(self):
        required_options = (
            "--output",
            "--source-sha",
            "--image-ref",
            "--image-digest",
            "--runtime-ref",
            "--runtime-commit",
            "--sdk-ref",
            "--sdk-commit",
            "--bundle",
        )
        with tempfile.TemporaryDirectory(prefix="airloom-provenance-test-") as temporary:
            root = Path(temporary)
            output = root / "provenance.json"
            bundle = root / "Airloom-x86_64.flatpak"
            bundle.write_bytes(b"fixture\n")
            for option in required_options:
                with self.subTest(option=option):
                    command = provenance_command(output, bundle)
                    index = command.index(option)
                    del command[index : index + 2]
                    result = subprocess.run(
                        command,
                        cwd=PROJECT_ROOT,
                        capture_output=True,
                        text=True,
                    )
                    self.assert_denied(result)

            for option in ("--tool-version", "--tool-ref"):
                with self.subTest(option=option):
                    command = provenance_command(output, bundle)
                    command = [
                        value
                        for index, value in enumerate(command)
                        if command[index - 1] != option and value != option
                    ]
                    result = subprocess.run(
                        command,
                        cwd=PROJECT_ROOT,
                        capture_output=True,
                        text=True,
                    )
                    self.assert_denied(result)

    def test_image_ref_must_contain_the_recorded_digest(self):
        with tempfile.TemporaryDirectory(prefix="airloom-provenance-test-") as temporary:
            root = Path(temporary)
            output = root / "provenance.json"
            bundle = root / "Airloom-x86_64.flatpak"
            bundle.write_bytes(b"fixture\n")
            command = provenance_command(output, bundle)
            command[command.index(IMAGE_REF)] = (
                "ghcr.io/flathub-infra/flatpak-github-actions:gnome-50"
            )
            result = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
            )
            self.assert_denied(result)
            self.assertFalse(output.exists())


class ProvenanceValidationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="airloom-provenance-check-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "Airloom-x86_64.flatpak"
        self.bundle.write_bytes(b"same-version fixture bundle\n")
        self.provenance = self.root / "Airloom-x86_64.provenance.json"
        result = subprocess.run(
            provenance_command(self.provenance, self.bundle),
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def check(self, source_sha=SOURCE_SHA, *, bundle=True, provenance=None):
        command = [
            sys.executable,
            str(CHECK_PROVENANCE),
            "--source-sha",
            source_sha,
            "--provenance",
            str(provenance or self.provenance),
        ]
        if bundle:
            command.extend(("--bundle", str(self.bundle)))
        return subprocess.run(
            command, cwd=PROJECT_ROOT, capture_output=True, text=True
        )

    def test_matching_source_and_bundle_checksum_are_accepted(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_repository_provenance_is_bound_without_a_sibling_bundle(self):
        result = self.check(bundle=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_same_version_artifact_from_a_different_commit_is_rejected(self):
        result = self.check("e" * 40)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("source_sha", result.stderr)

    def test_stale_bundle_does_not_match_recorded_checksum(self):
        self.bundle.write_bytes(b"replacement bytes under the old tag\n")
        result = self.check()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("checksum", result.stderr)


class RepositoryTarProvenanceValidationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="airloom-repo-tar-check-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "Airloom-x86_64.flatpak"
        self.bundle.write_bytes(b"fixture bundle\n")
        self.repository_tar = self.root / "airloom-repo-x86_64.tar"
        self.repository_tar.write_bytes(b"original repository tar payload\n")
        self.provenance = self.root / "Airloom-x86_64.provenance.json"
        result = subprocess.run(
            provenance_command(
                self.provenance, self.bundle, repository_tar=self.repository_tar
            ),
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def check(self, *, source_sha=SOURCE_SHA, provenance=None):
        return subprocess.run(
            [
                sys.executable,
                str(CHECK_PROVENANCE),
                "--source-sha",
                source_sha,
                "--provenance",
                str(provenance or self.provenance),
                "--repository-tar",
                str(self.repository_tar),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )

    def test_matching_repository_tar_checksum_is_accepted(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_complete_client_arguments_accept_identical_adjacent_provenance_copy(self):
        client = self.root / "client"
        dist = client / "dist"
        dist.mkdir(parents=True)
        repository_dirs = {}
        for arch in ("x86_64", "aarch64"):
            repository_dir = client / "repos" / f"airloom-repo-{arch}"
            repository_dir.mkdir(parents=True)
            repository_dirs[arch] = repository_dir
            bundle = repository_dir / f"Airloom-{arch}.flatpak"
            bundle.write_bytes(f"{arch} client bundle\n".encode())
            repository_tar = repository_dir / f"airloom-repo-{arch}.tar"
            repository_tar.write_bytes(f"{arch} repository payload\n".encode())
            provenance = repository_dir / f"Airloom-{arch}.provenance.json"
            result = subprocess.run(
                provenance_command(
                    provenance, bundle, repository_tar=repository_tar
                ),
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        shutil.copyfile(
            repository_dirs["x86_64"] / "Airloom-x86_64.flatpak",
            dist / "Airloom-x86_64.flatpak",
        )
        shutil.copyfile(
            repository_dirs["x86_64"] / "Airloom-x86_64.provenance.json",
            dist / "Airloom-x86_64.provenance.json",
        )
        command = [
            sys.executable,
            str(CHECK_PROVENANCE),
            "--source-sha",
            SOURCE_SHA,
            "--provenance",
            str(dist / "Airloom-x86_64.provenance.json"),
            "--bundle",
            str(dist / "Airloom-x86_64.flatpak"),
        ]
        for arch in ("x86_64", "aarch64"):
            repository_dir = repository_dirs[arch]
            command.extend(
                (
                    "--provenance",
                    str(repository_dir / f"Airloom-{arch}.provenance.json"),
                    "--repository-tar",
                    str(repository_dir / f"airloom-repo-{arch}.tar"),
                )
            )

        result = subprocess.run(
            command, cwd=PROJECT_ROOT, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_replaced_repository_tar_is_rejected_before_it_can_be_used(self):
        self.repository_tar.write_bytes(b"replacement repository tar payload\n")
        result = self.check()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("repository tar checksum mismatch", result.stderr)

    def test_repository_tar_cannot_use_provenance_for_a_different_payload(self):
        other = self.root / "other"
        other.mkdir()
        other_tar = other / self.repository_tar.name
        other_tar.write_bytes(b"different repository tar payload\n")
        other_provenance = other / self.provenance.name
        result = subprocess.run(
            provenance_command(
                other_provenance, self.bundle, repository_tar=other_tar
            ),
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        result = self.check(provenance=other_provenance)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("repository tar checksum mismatch", result.stderr)

    def test_genuinely_ambiguous_repository_provenance_is_rejected(self):
        other = self.root / "ambiguous"
        other.mkdir()
        other_tar = other / self.repository_tar.name
        other_tar.write_bytes(b"conflicting repository tar payload\n")
        other_provenance = other / self.provenance.name
        result = subprocess.run(
            provenance_command(
                other_provenance, self.bundle, repository_tar=other_tar
            ),
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        isolated = self.root / "isolated"
        isolated.mkdir()
        isolated_tar = isolated / self.repository_tar.name
        shutil.copyfile(self.repository_tar, isolated_tar)

        result = subprocess.run(
            [
                sys.executable,
                str(CHECK_PROVENANCE),
                "--source-sha",
                SOURCE_SHA,
                "--provenance",
                str(self.provenance),
                "--provenance",
                str(other_provenance),
                "--repository-tar",
                str(isolated_tar),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("missing or ambiguous", result.stderr)

    def test_repository_tar_provenance_is_bound_to_the_source_commit(self):
        result = self.check(source_sha="e" * 40)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("source_sha", result.stderr)


# Stand-ins for ostree/gpg/flatpak that model just enough of commit
# signatures for publish-flatpak-repo.sh: per-commit signer records, ostree's
# "already signed" refusal, and gpg-verified metadata pulls against a keyring.
FAKE_OSTREE = r"""#!/usr/bin/env python3
import os
import sys
from pathlib import Path

env = os.environ
state = Path(env["FAKE_STATE"])
with open(state / "log", "a") as stream:
    stream.write(" ".join(sys.argv[1:]) + "\n")
args = sys.argv[1:]
options = {a.split("=", 1)[0]: (a.split("=", 1)[1] if "=" in a else "") for a in args if a.startswith("--")}
positional = [a for a in args[1:] if not a.startswith("--")]
command = args[0]


def signers(commit):
    path = state / "signatures" / commit
    return path.read_text().split() if path.exists() else []


if command == "init":
    Path(options["--repo"]).mkdir(parents=True, exist_ok=True)
elif command == "refs":
    refs = Path(options["--repo"]) / "refs.list"
    print(refs.read_text(), end="") if refs.exists() else None
elif command == "rev-parse":
    print("commit-" + positional[0].split("/")[2])
elif command == "pull-local":
    source, ref = positional
    if "--remote" in options:
        keyring = (state / "remotes" / options["--remote"]).read_text().strip()
        commit = "commit-" + ref.split("/")[2]
        assert "--gpg-verify" in options and "--commit-metadata-only" in options, args
        if not signers(commit):
            sys.exit(f"error: Commit {commit}: GPG verification enabled, but no signatures found")
        if keyring.removeprefix("PUBKEY:") not in signers(commit):
            sys.exit(f"error: Commit {commit}: Can't check signature: public key not found")
    else:
        with open(Path(options["--repo"]) / "refs.list", "a") as stream:
            stream.write(ref + "\n")
elif command == "remote":
    assert positional[0] == "add", args
    (state / "remotes").mkdir(exist_ok=True)
    (state / "remotes" / positional[1]).write_text(Path(options["--gpg-import"]).read_text())
elif command == "gpg-sign":
    commit, key = positional
    existing = signers(commit)
    if existing:
        sys.exit(f"error: Commit is already signed with GPG key {existing[0]}")
    if env.get("FAKE_SIGN_ERROR"):
        sys.exit(env["FAKE_SIGN_ERROR"])
    if env.get("FAKE_SIGN_DROPS_SIGNATURE") != "1":
        (state / "signatures").mkdir(exist_ok=True)
        (state / "signatures" / commit).write_text(key + "\n")
else:
    sys.exit(f"unsupported fake ostree command: {args}")
"""

FAKE_GPG = """#!/bin/sh
# Only `gpg --batch --export KEY` is used.
[ "$2" = "--export" ] || { echo "unsupported fake gpg: $*" >&2; exit 1; }
echo "PUBKEY:$3"
"""

FAKE_FLATPAK = """#!/bin/sh
echo "flatpak $*" >> "$FAKE_STATE/log"
"""


class PublishRepoSigningTest(unittest.TestCase):
    KEY = "A69439FA8798E7F3D470CB0AB0CB70F4240BF9B6"
    OTHER_KEY = "0123456789ABCDEF0123456789ABCDEF01234567"

    def run_publish(self, presigned=None, **env_overrides):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            for name, body in (("ostree", FAKE_OSTREE), ("gpg", FAKE_GPG), ("flatpak", FAKE_FLATPAK)):
                (bin_dir / name).write_text(body, encoding="utf-8")
                (bin_dir / name).chmod(0o755)
            state = root / "state"
            (state / "signatures").mkdir(parents=True)
            for commit, key in (presigned or {}).items():
                (state / "signatures" / commit).write_text(key + "\n")
            sources = []
            for arch in ("x86_64", "aarch64"):
                source = root / f"airloom-repo-{arch}"
                source.mkdir()
                (source / "refs.list").write_text(f"app/ai.stealthvision.Airloom/{arch}/stable\n")
                sources.append(str(source))
            pubkey = root / "airloom-flatpak.gpg"
            pubkey.write_text("public key\n")
            env = {
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "FAKE_STATE": str(state),
                "GPG_KEY_ID": self.KEY,
                "GNUPGHOME": str(root / "gnupg"),
                "PUBKEY_FILE": str(pubkey),
                **env_overrides,
            }
            result = subprocess.run(
                ["bash", str(PUBLISH_REPO), str(root / "_site"), *sources],
                env=env, capture_output=True, text=True, timeout=60,
            )
            log = (state / "log").read_text() if (state / "log").exists() else ""
            signatures = {
                path.name: path.read_text().split() for path in (state / "signatures").iterdir()
            }
            return result, log, signatures

    def test_unsigned_merged_commits_are_signed_and_verified(self):
        result, log, signatures = self.run_publish()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(signatures, {"commit-x86_64": [self.KEY], "commit-aarch64": [self.KEY]})
        for arch in ("x86_64", "aarch64"):
            self.assertRegex(
                log,
                rf"(?m)^pull-local --repo=\S+ --remote=\S+ --gpg-verify --commit-metadata-only "
                rf"\S+/_site/repo app/ai\.stealthvision\.Airloom/{arch}/stable$",
            )
        self.assertIn("flatpak build-update-repo", log)

    def test_commits_already_signed_by_the_same_key_are_accepted(self):
        # Production build jobs and dev fixtures sign every arch's commit
        # before assembly; ostree refuses to sign them again.
        result, _log, signatures = self.run_publish(
            presigned={"commit-x86_64": self.KEY, "commit-aarch64": self.KEY}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(signatures, {"commit-x86_64": [self.KEY], "commit-aarch64": [self.KEY]})

    def test_already_signed_by_a_different_key_is_fatal(self):
        result, log, _signatures = self.run_publish(presigned={"commit-x86_64": self.OTHER_KEY})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"already signed with GPG key {self.OTHER_KEY}", result.stderr)
        self.assertNotIn("build-update-repo", log)

    def test_other_signing_errors_are_fatal(self):
        result, log, _signatures = self.run_publish(FAKE_SIGN_ERROR="error: gpg: signing failed: No secret key")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No secret key", result.stderr)
        self.assertNotIn("build-update-repo", log)

    def test_a_commit_left_without_our_signature_is_fatal(self):
        result, log, _signatures = self.run_publish(FAKE_SIGN_DROPS_SIGNATURE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"is not signed by {self.KEY}", result.stderr)
        self.assertNotIn("build-update-repo", log)


class SigningKeyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="airloom-signing-test-")
        cls.root = Path(cls.temporary.name)
        cls.fixtures = {}
        for name in ("matching", "wrong"):
            home = cls.root / name
            home.mkdir(mode=0o700)
            env = dict(os.environ, GNUPGHOME=str(home))
            subprocess.run(
                [
                    "gpg",
                    "--batch",
                    "--pinentry-mode",
                    "loopback",
                    "--passphrase",
                    "",
                    "--quick-generate-key",
                    f"Airloom Test {name} <{name}@example.invalid>",
                    "ed25519",
                    "sign",
                    "1d",
                ],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            listing = subprocess.run(
                ["gpg", "--batch", "--with-colons", "--list-secret-keys"],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            fingerprint = next(
                line.split(":")[9]
                for line in listing.splitlines()
                if line.startswith("fpr:")
            )
            public = cls.root / f"{name}.gpg"
            private = cls.root / f"{name}.asc"
            with public.open("wb") as stream:
                subprocess.run(
                    ["gpg", "--batch", "--export", fingerprint],
                    env=env,
                    check=True,
                    stdout=stream,
                )
            with private.open("wb") as stream:
                subprocess.run(
                    [
                        "gpg",
                        "--batch",
                        "--pinentry-mode",
                        "loopback",
                        "--passphrase",
                        "",
                        "--armor",
                        "--export-secret-keys",
                        fingerprint,
                    ],
                    env=env,
                    check=True,
                    stdout=stream,
                )
            cls.fixtures[name] = {
                "fingerprint": fingerprint,
                "public": public,
                "private": private,
            }

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def import_key(self, **overrides):
        work = Path(tempfile.mkdtemp(prefix="airloom-import-test-"))
        self.addCleanup(shutil.rmtree, work)
        github_env = work / "github-env"
        github_env.touch()
        env = dict(
            os.environ,
            GITHUB_ENV=str(github_env),
            TMPDIR=str(work),
            AIRLOOM_REQUIRE_PRODUCTION_KEY="1",
            AIRLOOM_PUBLIC_KEY_FILE=str(self.fixtures["matching"]["public"]),
            FLATPAK_GPG_PRIVATE_KEY=self.fixtures["matching"]["private"].read_text(
                encoding="utf-8"
            ),
        )
        env.pop("AIRLOOM_EPHEMERAL_SIGNING_KEY", None)
        env.update(overrides)
        result = subprocess.run(
            [str(IMPORT_SIGNING_KEY)],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        return result, github_env.read_text(encoding="utf-8"), work

    def test_production_import_must_match_the_committed_public_key(self):
        result, github_env, work = self.import_key()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        fingerprint = self.fixtures["matching"]["fingerprint"]
        self.assertIn(f"GPG_KEY_ID={fingerprint}", github_env)
        public_line = next(
            line for line in github_env.splitlines() if line.startswith("FLATPAK_GPG_PUBLIC_KEY=")
        )
        exported = Path(public_line.split("=", 1)[1])
        self.assertTrue(exported.is_file())
        self.assertTrue(exported.is_relative_to(work))

        result, github_env, _ = self.import_key(
            AIRLOOM_PUBLIC_KEY_FILE=str(self.fixtures["wrong"]["public"])
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("does not match", result.stderr)
        self.assertNotIn("GPG_KEY_ID=", github_env)

    def test_ordinary_ci_generates_a_disposable_ephemeral_key(self):
        result, github_env, work = self.import_key(
            AIRLOOM_EPHEMERAL_SIGNING_KEY="1",
            AIRLOOM_REQUIRE_PRODUCTION_KEY="0",
            FLATPAK_GPG_PRIVATE_KEY="",
            AIRLOOM_PUBLIC_KEY_FILE=str(self.root / "does-not-exist.gpg"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        values = dict(
            line.split("=", 1) for line in github_env.splitlines() if "=" in line
        )
        self.assertIn("GNUPGHOME", values)
        self.assertIn("GPG_KEY_ID", values)
        self.assertIn("FLATPAK_GPG_PUBLIC_KEY", values)
        self.assertNotEqual(
            values["GPG_KEY_ID"], self.fixtures["matching"]["fingerprint"]
        )
        self.assertTrue(Path(values["GNUPGHOME"]).is_relative_to(work))
        self.assertTrue(Path(values["FLATPAK_GPG_PUBLIC_KEY"]).is_file())


class ClientVerificationTest(unittest.TestCase):
    def run_client_gate(self, *extra_args, committed_key="production", **fake_overrides):
        with tempfile.TemporaryDirectory(prefix="airloom-client-test-") as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            log = root / "flatpak.jsonl"
            current = root / "Current.flatpak"
            previous = root / "Previous.flatpak"
            current.write_bytes(b"current fixture bundle")
            previous.write_bytes(b"previous fixture bundle")
            repo = root / "repo"
            repo.mkdir()
            correct_key = root / "correct.gpg"
            correct_key.write_bytes(b"correct-test-key")
            production_key = root / "production.gpg"
            production_key.write_bytes(b"production-test-key")
            summary = root / "summary.md"
            summary.touch()
            (root / "correct-home").mkdir(mode=0o700)

            flatpak = bin_dir / "flatpak"
            flatpak.write_text(
                textwrap.dedent(
                    r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
env = os.environ
client = Path(env["FLATPAK_USER_DIR"])
client.mkdir(parents=True, exist_ok=True)
state_path = client / "state.json"
remotes_path = client / "remotes.json"
state = json.loads(state_path.read_text()) if state_path.exists() else {}
remotes = json.loads(remotes_path.read_text()) if remotes_path.exists() else {}
with open(env["FAKE_FLATPAK_LOG"], "a") as stream:
    stream.write(json.dumps({"args": args, "client": str(client)}) + "\n")
command = next((arg for arg in args if not arg.startswith("-")), "")
current_version = env["FAKE_CURRENT_VERSION"]
previous_version = env["FAKE_PREVIOUS_VERSION"]
FPRS = {b"correct-test-key": "CORRECTFPR", b"wrong-test-key": "WRONGFPR", b"production-test-key": "PRODUCTIONFPR"}
# The key that signs the repository's commits; a remote accepts them only
# when it trusts that key or does not verify signatures.
repo_signer = env.get("FAKE_REPO_SIGNER", "CORRECTFPR")

def save():
    state_path.write_text(json.dumps(state))
    remotes_path.write_text(json.dumps(remotes))

def option(name):
    return next((arg.split("=", 1)[1] for arg in args if arg.startswith(name + "=")), None)

def fingerprint(path):
    return FPRS.get(Path(path).read_bytes(), "UNKNOWNFPR")

def accepts(remote):
    return not remote["gpg_verify"] or repo_signer in remote["keys"] or env.get("FAKE_ACCEPT_WRONG_KEY") == "1"

if command == "remote-add":
    name, url = args[-2:]
    imported = option("--gpg-import")
    keys = [fingerprint(imported)] if imported is not None else []
    if repo_signer not in keys and env.get("FAKE_WRONG_KEY_REMOTE_ADD_SIGNATURE") == "1":
        print("OSTree GPG signature verification failed: wrong key", file=sys.stderr)
        sys.exit(1)
    if repo_signer not in keys and env.get("FAKE_WRONG_KEY_REMOTE_ADD_UNRELATED") == "1":
        print("network unreachable while fetching metadata", file=sys.stderr)
        sys.exit(1)
    remotes[name] = {"url": url, "keys": keys, "gpg_verify": True}
    save()
elif command == "remote-modify":
    name = args[-1]
    remote = remotes[name]
    remote["url"] = option("--url") or remote["url"]
    imported = option("--gpg-import")
    if imported is not None:
        remote["keys"] = sorted(set(remote["keys"]) | {fingerprint(imported)})
    if "--gpg-verify" in args:
        remote["gpg_verify"] = True
    save()
elif command == "install":
    bundle = next((arg for arg in args if arg.endswith(".flatpak")), None)
    if bundle is not None:
        if option("--gpg-file") is not None:
            # flatpak 1.18.1 frees --gpg-file key bytes as a GObject at
            # transaction teardown and segfaults (flatpak#6842).
            print("Segmentation fault (core dumped)", file=sys.stderr)
            sys.exit(139)
        previous = bundle == env["FAKE_PREVIOUS_BUNDLE"]
        # A bundle carries the keys build-bundle --gpg-keys embedded; its
        # origin remote trusts exactly those and requires signatures only
        # when there are any.
        if previous:
            trusted = env.get("FAKE_PREVIOUS_TRUSTED_KEY", "CORRECTFPR")
            keys, signer = [trusted], trusted
            gpg_verify = env.get("FAKE_PREVIOUS_GPG_VERIFY", "1") == "1"
            url = env.get("FAKE_PREVIOUS_BUNDLE_URL", "https://ijbaird.github.io/airloom/repo")
        else:
            signature = env.get("FAKE_CURRENT_BUNDLE_SIGNATURE", "selected")
            # (trusted keys, commit signer, origin gpg-verify, install enforces
            # signatures). The last two let each gate check be exercised alone,
            # e.g. a correct key with verification off, or an install that
            # succeeded although the commit is not signed by our key.
            keys, signer, gpg_verify, enforced = {
                "selected": (["CORRECTFPR"], "CORRECTFPR", True, True),
                "unsigned": ([], None, False, True),
                "independent": (["OTHERFPR"], "OTHERFPR", True, True),
                "extra-key": (["CORRECTFPR", "OTHERFPR"], "CORRECTFPR", True, True),
                "embedded-but-signed-by-other": (["CORRECTFPR"], "OTHERFPR", True, True),
                "verification-disabled": (["CORRECTFPR"], "CORRECTFPR", False, True),
                "installed-but-signed-by-other": (["CORRECTFPR"], "OTHERFPR", True, False),
            }[signature]
            url = env.get("FAKE_BUNDLE_URL", "https://ijbaird.github.io/airloom/repo")
            if enforced and keys and signer not in keys:
                print("GPG signatures found, but none are in trusted keyring", file=sys.stderr)
                sys.exit(1)
        if previous:
            version = env.get("FAKE_PREVIOUS_REPORTED_VERSION", previous_version)
        else:
            version = env.get("FAKE_BUNDLE_REPORTED_VERSION", current_version)
        origin = env.get("FAKE_ORIGIN", "airloom-origin")
        commit = "previous-commit" if previous else "current-commit"
        state = {"version": version, "commit": commit, "origin": origin}
        remotes[origin] = {"url": url, "keys": keys, "gpg_verify": gpg_verify}
        signers = json.loads((client / "signers.json").read_text()) if (client / "signers.json").exists() else {}
        signers[commit] = signer
        (client / "signers.json").write_text(json.dumps(signers))
    else:
        remote_name = args[-2]
        remote = remotes[remote_name]
        if not accepts(remote):
            if env.get("FAKE_WRONG_KEY_INSTALL_UNRELATED") == "1":
                print("network unreachable while pulling commit", file=sys.stderr)
            else:
                print("GPG signatures found, but none are in trusted keyring", file=sys.stderr)
            sys.exit(1)
        state = {
            "version": current_version,
            "commit": "current-commit",
            "origin": remote_name,
        }
    save()
elif command == "update":
    remote = remotes[state["origin"]]
    if not accepts(remote):
        print("GPG signatures found, but none are in trusted keyring", file=sys.stderr)
        sys.exit(1)
    if env.get("FAKE_UPDATE_ADDS_KEY"):
        remote["keys"].append(env["FAKE_UPDATE_ADDS_KEY"])
    if env.get("FAKE_UPDATE_DISABLES_GPG_VERIFY") == "1":
        remote["gpg_verify"] = False
    # FAKE_UPGRADE_RESULT models partial upgrades so the version and commit
    # checks are each exercised alone.
    result = "stale" if env.get("FAKE_STALE_UPGRADE") == "1" else env.get("FAKE_UPGRADE_RESULT", "complete")
    if result in ("complete", "same-commit"):
        state["version"] = current_version
    if result in ("complete", "old-version"):
        state["commit"] = "current-commit"
    save()
elif command == "info":
    if "--show-origin" in args:
        print(state["origin"])
    elif "--show-commit" in args:
        print(state["commit"])
    else:
        print(f"Version: {env.get('FAKE_INFO_VERSION', state['version'])}")
elif command == "remotes":
    for name, remote in remotes.items():
        print(f"{name} {remote['url']}")
else:
    print(f"unsupported fake flatpak command: {args}", file=sys.stderr)
    sys.exit(2)
'''
                ),
                encoding="utf-8",
            )
            flatpak.chmod(0o755)
            gpg = bin_dir / "gpg"
            gpg.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "from pathlib import Path\n"
                "if '--export' in sys.argv:\n"
                "    sys.stdout.buffer.write(b'wrong-test-key')\n"
                "elif '--show-keys' in sys.argv:\n"
                "    data = Path(sys.argv[-1]).read_bytes()\n"
                "    fpr = {b'correct-test-key': 'CORRECTFPR', b'wrong-test-key': 'WRONGFPR',\n"
                "           b'production-test-key': 'PRODUCTIONFPR'}[data]\n"
                "    print(f'pub:u:255:22:X:1::::::scESC:\\nfpr:::::::::{fpr}:')\n",
                encoding="utf-8",
            )
            gpg.chmod(0o755)
            ostree = bin_dir / "ostree"
            ostree.write_text(
                textwrap.dedent(
                    r'''#!/usr/bin/env python3
import json
import sys
from pathlib import Path

args = sys.argv[1:]
options = {a.split("=", 1)[0]: (a.split("=", 1)[1] if "=" in a else "") for a in args if a.startswith("--")}
positional = [a for a in args if not a.startswith("--")]
repo = Path(options["--repo"])
client = repo.parent
KEYS = {b"correct-test-key": "CORRECTFPR", b"wrong-test-key": "WRONGFPR"}
if positional[:3] == ["remote", "gpg-list-keys", positional[2] if len(positional) > 2 else ""]:
    remotes = json.loads((client / "remotes.json").read_text())
    for key in remotes[positional[2]]["keys"]:
        print(f"Key: {key}")
elif positional[:2] == ["config", "get"]:
    name = positional[2].split('"')[1]
    remotes = json.loads((client / "remotes.json").read_text())
    print("true" if remotes[name]["gpg_verify"] else "false")
elif positional[0] == "init":
    repo.mkdir(parents=True, exist_ok=True)
elif positional[:2] == ["remote", "add"]:
    (repo / "keyring").write_text(KEYS.get(Path(options["--gpg-import"]).read_bytes(), "UNKNOWN"))
elif positional[0] == "pull-local":
    assert "--gpg-verify" in options and "--commit-metadata-only" in options, args
    source, commit = positional[1:3]
    signers = json.loads((Path(source).parent / "signers.json").read_text())
    if signers.get(commit) is None:
        sys.exit(f"error: Commit {commit}: GPG verification enabled, but no signatures found")
    if signers[commit] != (repo / "keyring").read_text():
        sys.exit(f"error: Commit {commit}: Can't check signature: public key not found")
else:
    sys.exit(f"unsupported fake ostree command: {args}")
'''
                ),
                encoding="utf-8",
            )
            ostree.chmod(0o755)

            env = dict(
                os.environ,
                PATH=f"{bin_dir}:{os.environ['PATH']}",
                TMPDIR=str(root),
                GITHUB_STEP_SUMMARY=str(summary),
                FLATPAK_GPG_PUBLIC_KEY=str(correct_key),
                GNUPGHOME=str(root / "correct-home"),
                AIRLOOM_PUBLIC_KEY_FILE=str(
                    correct_key if committed_key == "candidate" else production_key
                ),
                FAKE_FLATPAK_LOG=str(log),
                FAKE_PREVIOUS_BUNDLE=str(previous),
                FAKE_CURRENT_VERSION="1.2.3",
                FAKE_PREVIOUS_VERSION="1.2.2",
            )
            env.update(fake_overrides)
            result = subprocess.run(
                [
                    str(VERIFY_CLIENT),
                    "--bundle",
                    str(current),
                    "--previous-bundle",
                    str(previous),
                    "--repo",
                    str(repo),
                    "--arch",
                    "x86_64",
                    "--version",
                    "1.2.3",
                    "--previous-version",
                    "1.2.2",
                    *extra_args,
                ],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            entries = (
                [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
                if log.exists()
                else []
            )
            return result, entries, summary.read_text(encoding="utf-8")

    def test_installs_signed_bundle_and_repository_and_upgrades_previous_release(self):
        result, entries, summary = self.run_client_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = [entry["args"] for entry in entries]
        current_bundle_install = next(
            command
            for command in commands
            if "install" in command
            and any(argument.endswith("Current.flatpak") for argument in command)
        )
        # Installed as a user installs a .flatpak: trusting the embedded key,
        # which the gate then proves is ours (--gpg-file crashes 1.18.1).
        self.assertFalse(
            any(argument.startswith("--gpg-file") for command in commands for argument in command)
        )
        self.assertTrue(any("remote-add" in command for command in commands))
        self.assertTrue(any("remote-modify" in command for command in commands))
        self.assertTrue(any("update" in command for command in commands))
        self.assertFalse(
            any("--no-gpg-verify" in argument for command in commands for argument in command)
        )
        clients = {entry["client"] for entry in entries}
        self.assertEqual(len(clients), 4)
        self.assertTrue(all("airloom-client-verify." in client for client in clients))
        for evidence in (
            "bundle_verified origin=airloom-origin "
            "url=https://ijbaird.github.io/airloom/repo key=CORRECTFPR version=1.2.3",
            "repository_install_verified origin=airloom-verify-current version=1.2.3",
            "upgrade_verified previous_version=1.2.2 version=1.2.3",
            "wrong_key_rejected remote=airloom-verify-wrong-key stage=install",
        ):
            with self.subTest(evidence=evidence):
                self.assertIn(evidence, summary)

    def test_unsigned_bundle_is_rejected(self):
        result, _, summary = self.run_client_gate(
            FAKE_CURRENT_BUNDLE_SIGNATURE="unsigned"
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stderr, r"(?i)gpg|signature")
        self.assertNotIn("bundle_verified", summary)

    def test_independently_signed_bundle_is_rejected(self):
        result, _, summary = self.run_client_gate(
            FAKE_CURRENT_BUNDLE_SIGNATURE="independent"
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stderr, r"(?i)gpg|signature")
        self.assertNotIn("bundle_verified", summary)

    def test_bundle_origin_trusting_an_extra_key_is_rejected(self):
        result, _, summary = self.run_client_gate(FAKE_CURRENT_BUNDLE_SIGNATURE="extra-key")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("trusts GPG keys CORRECTFPR OTHERFPR, not CORRECTFPR", result.stderr)
        self.assertNotIn("bundle_verified", summary)

    def test_bundle_commit_not_signed_by_the_expected_key_is_rejected(self):
        result, _, summary = self.run_client_gate(
            FAKE_CURRENT_BUNDLE_SIGNATURE="embedded-but-signed-by-other"
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stderr, r"(?i)gpg|signature")
        self.assertNotIn("bundle_verified", summary)

    def test_origin_name_is_whatever_flatpak_assigns(self):
        result, _, summary = self.run_client_gate(FAKE_ORIGIN="ai.stealthvision.Airloom-origin")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("bundle_verified origin=ai.stealthvision.Airloom-origin ", summary)

    def test_bundle_origin_must_require_signatures_even_with_the_right_key(self):
        result, _, summary = self.run_client_gate(FAKE_CURRENT_BUNDLE_SIGNATURE="verification-disabled")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("does not require GPG signatures", result.stderr)
        self.assertNotIn("bundle_verified", summary)

    def test_installed_bundle_commit_must_carry_our_signature(self):
        # Installation and the origin's key policy look right, but the
        # installed commit itself is not signed by our key.
        result, _, summary = self.run_client_gate(FAKE_CURRENT_BUNDLE_SIGNATURE="installed-but-signed-by-other")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("bundle commit current-commit has no good GPG signature by CORRECTFPR", result.stderr)
        self.assertNotIn("bundle_verified", summary)

    def test_bundle_installation_must_report_the_current_version(self):
        # Only the bundle client reports a wrong version; later stages are fine.
        result, _, summary = self.run_client_gate(FAKE_BUNDLE_REPORTED_VERSION="9.9.9")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("bundle version 9.9.9 does not match expected 1.2.3", result.stderr)
        self.assertNotIn("bundle_verified", summary)

    def test_previous_installation_must_report_the_previous_version(self):
        result, _, summary = self.run_client_gate(FAKE_PREVIOUS_REPORTED_VERSION="1.2.1")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("previous installation version 1.2.1 does not match expected 1.2.2", result.stderr)
        self.assertNotIn("upgrade_verified", summary)

    def test_upgrade_must_report_the_current_version(self):
        result, _, summary = self.run_client_gate(FAKE_UPGRADE_RESULT="old-version")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("upgrade version 1.2.2 does not match expected 1.2.3", result.stderr)
        self.assertNotIn("upgrade_verified", summary)

    def test_upgrade_must_reach_a_new_commit(self):
        result, _, summary = self.run_client_gate(FAKE_UPGRADE_RESULT="same-commit")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("upgrade did not reach a new commit", result.stderr)
        self.assertNotIn("upgrade_verified", summary)

    def test_previous_release_must_update_from_the_published_repository(self):
        result, _, _ = self.run_client_gate(
            FAKE_PREVIOUS_BUNDLE_URL="https://example.invalid/other/repo"
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("previous installation origin URL", result.stderr)

    def test_wrong_bundle_origin_url_is_rejected(self):
        result, _, _ = self.run_client_gate(
            FAKE_BUNDLE_URL="https://ijbaird.github.io/airloom/repo/repo"
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("origin URL", result.stderr)

    def test_wrong_installed_version_is_rejected(self):
        result, _, _ = self.run_client_gate(FAKE_INFO_VERSION="9.9.9")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("version", result.stderr.lower())

    def test_previous_installation_must_reach_the_current_commit(self):
        result, _, _ = self.run_client_gate(FAKE_STALE_UPGRADE="1")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("upgrade", result.stderr.lower())

    def test_accepting_a_wrong_signing_key_fails_the_gate(self):
        result, _, _ = self.run_client_gate(FAKE_ACCEPT_WRONG_KEY="1")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("wrong signing key", result.stderr)

    def test_wrong_key_rejection_during_remote_creation_is_classified(self):
        result, _, summary = self.run_client_gate(
            FAKE_WRONG_KEY_REMOTE_ADD_SIGNATURE="1"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "wrong_key_rejected remote=airloom-verify-wrong-key stage=remote-add",
            summary,
        )

    def test_unrelated_remote_creation_error_is_not_wrong_key_rejection(self):
        result, _, summary = self.run_client_gate(
            FAKE_WRONG_KEY_REMOTE_ADD_UNRELATED="1"
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("unrelated reason", result.stderr)
        self.assertNotIn("wrong_key_rejected", summary)

    def test_unrelated_install_error_is_not_wrong_key_rejection(self):
        result, _, summary = self.run_client_gate(
            FAKE_WRONG_KEY_INSTALL_UNRELATED="1"
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("unrelated reason", result.stderr)
        self.assertNotIn("wrong_key_rejected", summary)

    def remote_modifications(self, entries):
        return [e["args"] for e in entries if "remote-modify" in e["args"]]

    def test_production_upgrade_redirects_only_the_previous_origin_url(self):
        result, entries, summary = self.run_client_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (modify,) = self.remote_modifications(entries)
        self.assertEqual(modify[:2], ["remote-modify", "--user"])
        self.assertEqual(modify[-1], "airloom-origin")
        self.assertEqual([a for a in modify[2:-1] if a.startswith("--")], [modify[2]])
        self.assertRegex(modify[2], r"^--url=file:///.+/repo$")
        self.assertIn("upgrade_verified previous_version=1.2.2 version=1.2.3", summary)
        self.assertIn("trust=preserved previous_keys=CORRECTFPR", summary)

    def test_production_upgrade_rejects_a_candidate_the_previous_install_does_not_trust(self):
        result, entries, summary = self.run_client_gate(FAKE_PREVIOUS_TRUSTED_KEY="PREVIOUSFPR")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("previous installation (trusting PREVIOUSFPR) rejected the candidate release; "
                      "a signing-key rotation needs a reviewed client migration", result.stderr)
        self.assertIn("bundle_verified", summary)
        self.assertIn("repository_install_verified", summary)
        self.assertNotIn("upgrade_verified", summary)
        self.assertFalse(any(a.startswith("--gpg-import") for m in self.remote_modifications(entries) for a in m))

    def test_previous_origin_must_require_signatures_before_the_upgrade(self):
        result, entries, summary = self.run_client_gate(FAKE_PREVIOUS_GPG_VERIFY="0")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("previous installation origin airloom-origin does not require GPG signatures", result.stderr)
        self.assertFalse(any("update" in e["args"] for e in entries))

    def test_production_upgrade_must_leave_previous_trust_unchanged(self):
        result, _, summary = self.run_client_gate(FAKE_UPDATE_ADDS_KEY="OTHERFPR")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("previous installation trust changed from CORRECTFPR to CORRECTFPR OTHERFPR", result.stderr)
        self.assertNotIn("upgrade_verified", summary)

    def test_production_upgrade_must_leave_previous_origin_requiring_signatures(self):
        # Trusted keys stay the same; only the signature policy is dropped.
        result, _, summary = self.run_client_gate(FAKE_UPDATE_DISABLES_GPG_VERIFY="1")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("previous installation origin airloom-origin stopped requiring GPG signatures", result.stderr)
        self.assertNotIn("upgrade_verified", result.stdout)
        self.assertNotIn("upgrade_verified", summary)

    def test_development_option_imports_the_candidate_key_into_the_previous_install(self):
        result, entries, summary = self.run_client_gate(
            "--import-candidate-key", FAKE_PREVIOUS_TRUSTED_KEY="PREVIOUSFPR")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (modify,) = self.remote_modifications(entries)
        self.assertTrue(any(a.startswith("--gpg-import=") and a.endswith("correct.gpg") for a in modify))
        self.assertTrue(any(a.startswith("--url=file://") for a in modify))
        self.assertIn("trust=candidate-key-imported previous_keys=PREVIOUSFPR", summary)

    def test_development_option_refuses_the_committed_production_key(self):
        result, entries, summary = self.run_client_gate("--import-candidate-key", committed_key="candidate")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--import-candidate-key is development-only and refuses the committed "
                      "production key CORRECTFPR", result.stderr)
        self.assertEqual(entries, [])
        self.assertEqual(summary, "")


class ReleaseWorkflowContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKFLOW.read_text(encoding="utf-8")
        cls.publish_source = (
            PUBLISH_WORKFLOW.read_text(encoding="utf-8")
            if PUBLISH_WORKFLOW.exists()
            else ""
        )
        cls.eligibility = job_block(cls.source, "release-eligibility")
        cls.flatpak = job_block(cls.source, "flatpak")
        cls.verify = job_block(cls.source, "verify-client")
        cls.coordinator = job_block(cls.source, "publication")
        cls.release = job_block(cls.publish_source, "release")
        cls.publish = job_block(cls.publish_source, "publish-repo")

    def test_dispatch_exposes_typed_older_release_confirmation(self):
        self.assertRegex(
            self.source,
            r"(?m)^      allow_older:\n(?:^        .+\n)*?^        type: boolean$",
        )
        self.assertRegex(
            self.source,
            r"(?m)^      confirm_tag:\n(?:^        .+\n)*?^        type: string$",
        )
        self.assertIn("scripts/check-release.py", self.eligibility)
        self.assertIn("--allow-older", self.eligibility)
        self.assertIn('--confirm-tag "${CONFIRM_TAG}"', self.eligibility)

    def test_eligibility_checks_full_tag_history_before_building(self):
        self.assertTrue(self.eligibility, "release-eligibility job is missing")
        checkout = step_block(self.eligibility, "Checkout release history")
        self.assertIn("fetch-depth: 0", checkout)
        self.assertIn("ref: ${{ github.sha }}", checkout)
        self.assertIn("--ref", self.eligibility)
        self.assertIn("--version-root", self.eligibility)
        self.assertIn('--source-sha "${EXPECTED_SOURCE_SHA}"', self.eligibility)
        self.assertIn('echo "source_sha=${EXPECTED_SOURCE_SHA}"', self.eligibility)
        self.assertIn("needs: [test, webkit-ui, release-eligibility]", self.flatpak)

    def test_production_and_ephemeral_signing_keys_are_explicitly_separated(self):
        eligible = "needs.release-eligibility.outputs.eligible == 'true'"
        for job_name, job in (
            ("flatpak", self.flatpak),
            ("verify-client", self.verify),
        ):
            with self.subTest(job=job_name):
                production = step_block(job, "Import production signing key")
                self.assertIn(eligible, production)
                self.assertIn("AIRLOOM_REQUIRE_PRODUCTION_KEY", production)
                self.assertIn("secrets.FLATPAK_GPG_PRIVATE_KEY", production)
                self.assertIn("scripts/import-signing-key.sh", production)
        publication_key = step_block(self.publish, "Import production signing key")
        self.assertIn("AIRLOOM_REQUIRE_PRODUCTION_KEY", publication_key)
        self.assertIn("secrets.FLATPAK_GPG_PRIVATE_KEY", publication_key)
        self.assertIn("scripts/import-signing-key.sh", publication_key)
        self.assertNotIn("needs.release-eligibility", publication_key)
        ephemeral = step_block(self.verify, "Generate ephemeral signing key")
        self.assertIn("needs.release-eligibility.outputs.eligible != 'true'", ephemeral)
        self.assertIn("AIRLOOM_EPHEMERAL_SIGNING_KEY", ephemeral)
        self.assertIn("scripts/import-signing-key.sh", ephemeral)
        self.assertNotIn("secrets.", ephemeral)

    def test_only_eligible_releases_can_publish_after_client_verification(self):
        eligible = "if: needs.release-eligibility.outputs.eligible == 'true'"
        self.assertIn(eligible, self.coordinator)
        self.assertTrue(self.verify, "verify-client job is missing")
        self.assertIn("needs: [flatpak, release-eligibility]", self.verify)
        self.assertIn(
            "needs: [flatpak, verify-client, release-eligibility, webkit-ui]",
            self.coordinator,
        )
        self.assertIn("uses: ./.github/workflows/publish.yml", self.coordinator)
        self.assertIn("needs: [release]", self.publish)
        for job in (self.release, self.publish):
            self.assertIn("scripts/check-release.py", job)
            self.assertIn('--source-sha "${EXPECTED_SOURCE_SHA}"', job)

    def test_client_gate_assembles_signed_test_repo_and_verifies_every_architecture(self):
        for architecture in ("x86_64", "aarch64"):
            self.assertIn(f"arch: {architecture}", self.verify)
        self.assertIn("fetch-depth: 0", self.verify)
        assemble = step_block(self.verify, "Assemble verification repository")
        self.assertIn("scripts/publish-flatpak-repo.sh _site", assemble)
        verify = step_block(self.verify, "Verify signed client installation and upgrade")
        self.assertIn("scripts/verify-flatpak-release.sh", verify)
        for argument in (
            "--bundle",
            "--previous-bundle",
            "--previous-version",
            "--repo",
            "--arch",
            "--version",
        ):
            self.assertIn(argument, verify)
        self.assertLess(self.verify.index("name: Assemble verification repository"), self.verify.index("name: Verify signed client installation and upgrade"))
        development = step_block(self.verify, "Sign disposable development fixtures")
        self.assertIn("needs.release-eligibility.outputs.eligible != 'true'", development)
        # Like the production build jobs, every arch's source commit is
        # signed before assembly; only the matrix arch gets a fixture bundle.
        self.assertRegex(development, r"(?m)^\s*for repo_arch in x86_64 aarch64; do$")
        self.assertRegex(
            development,
            r'(?m)^\s*flatpak build-sign "repos/airloom-repo-\$\{repo_arch\}" ai\.stealthvision\.Airloom stable \\$',
        )
        self.assertIn('--arch="${repo_arch}"', development)
        self.assertEqual(development.count("flatpak build-sign"), 1)
        self.assertRegex(development, r'(?m)^\s*flatpak build-bundle "repos/airloom-repo-\$\{arch\}" "dist/Airloom-\$\{arch\}\.flatpak" \\$')
        self.assertIn("FLATPAK_GPG_PUBLIC_KEY", development)
        for step_name in ("Archive update-repo artifact", "Upload update-repo artifact"):
            self.assertNotIn("needs.release-eligibility.outputs.eligible", step_block(self.flatpak, step_name))
        self.assertNotIn("--no-gpg-verify", self.source)

    def test_only_development_runs_import_the_candidate_key_into_the_previous_install(self):
        verify = step_block(self.verify, "Verify signed client installation and upgrade")
        self.assertIn("RELEASE_ELIGIBLE: ${{ needs.release-eligibility.outputs.eligible }}", verify)
        self.assertEqual(self.source.count("--import-candidate-key"), 1)
        self.assertNotIn("--import-candidate-key", self.publish_source)
        script = run_block(verify).replace("${{ matrix.arch }}", "x86_64")
        for eligible, expected in (("true", False), ("false", True), ("", True)):
            with self.subTest(eligible=eligible), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stub = root / "scripts" / "verify-flatpak-release.sh"
                stub.parent.mkdir()
                stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGS_OUT"\n', encoding="utf-8")
                stub.chmod(0o755)
                env = dict(os.environ, RELEASE_ELIGIBLE=eligible, EXPECTED_VERSION="1.2.3",
                           PREVIOUS_VERSION="1.2.2", ARGS_OUT=str(root / "args"))
                subprocess.run(["bash", "-c", script], cwd=root, env=env, check=True)
                args = (root / "args").read_text(encoding="utf-8").splitlines()
                self.assertEqual("--import-candidate-key" in args, expected)
                self.assertEqual(args[args.index("--version") + 1], "1.2.3")
                self.assertEqual(args[args.index("--previous-bundle") + 1], "previous/Airloom-x86_64.flatpak")

    def test_newer_tag_after_initial_eligibility_is_rejected_at_publication(self):
        with release_checkout() as root:
            initial = check_release(root, "refs/tags/v1.2.3")
            self.assertEqual(initial.returncode, 0, initial.stdout + initial.stderr)

            write_version_files(
                root,
                package="1.2.4",
                changelog="1.2.4",
                appstream="1.2.4",
            )
            run_git(root, "add", ".")
            run_git(root, "commit", "-qm", "newer release")
            run_git(root, "tag", "v1.2.4")
            run_git(root, "reset", "--hard", "HEAD~1")

            publication = check_release(root, "refs/tags/v1.2.3")
            self.assertNotEqual(
                publication.returncode, 0, publication.stdout + publication.stderr
            )
            self.assertTrue(
                publication.stderr.startswith("release eligibility denied:"),
                publication.stdout + publication.stderr,
            )

        for job, first_publication_step in (
            (self.release, "Create release and upload bundles"),
            (self.publish, "Import production signing key"),
        ):
            with self.subTest(publication_step=first_publication_step):
                checkout = step_block(job, "Checkout release history")
                self.assertIn("fetch-depth: 0", checkout)
                revalidation = step_block(job, "Revalidate release eligibility")
                for expected in (
                    "git fetch --force --tags origin",
                    "scripts/check-release.py",
                    "ALLOW_OLDER: ${{ inputs.allow_older }}",
                    "CONFIRM_TAG: ${{ inputs.confirm_tag }}",
                    '--confirm-tag "${CONFIRM_TAG}"',
                    '--source-sha "${EXPECTED_SOURCE_SHA}"',
                ):
                    self.assertIn(expected, revalidation)
                self.assertLess(
                    job.index("name: Revalidate release eligibility"),
                    job.index(f"name: {first_publication_step}"),
                )

    def test_turnstyle_preserves_pending_queue_order_across_publication_runs(self):
        self.assertIn("uses: ./.github/workflows/publish.yml", self.coordinator)
        for job in (self.coordinator, self.release, self.publish):
            self.assertNotIn("concurrency:", job)

        queue = step_block(self.release, "Wait for prior stable publications")
        self.assertIn(f"uses: softprops/turnstyle@{TURNSTYLE_SHA}", queue)
        self.assertIn("actions: read", self.release)
        self.assertIn("actions: read", self.coordinator)
        self.assertNotIn("actions: read", self.publish)
        self.assertIn("same-branch-only: false", queue)
        self.assertNotIn("continue-after-seconds", queue)
        self.assertNotIn("abort-after-seconds", queue)
        first_step = re.search(r"(?m)^      - (.+)$", self.release)
        self.assertIsNotNone(first_step)
        self.assertEqual(first_step.group(1), "name: Wait for prior stable publications")
        self.assertLess(
            self.release.index("name: Wait for prior stable publications"),
            self.release.index("name: Revalidate release eligibility"),
        )
        self.assertIn("needs: [release]", self.publish)

        # Exercise Turnstyle's oldest-run-first predecessor rule with A holding
        # the publication critical section, B pending, then C arriving. Unlike
        # native concurrency, no pending contender is cancelled or bypassed.
        statuses = {"A": "in_progress", "B": "queued", "C": "queued"}
        run_ids = {"A": 101, "B": 102, "C": 103}
        channels = []

        def enter_critical_section(run):
            predecessors = [
                candidate
                for candidate, status in statuses.items()
                if status in {"queued", "waiting", "in_progress"}
                and run_ids[candidate] < run_ids[run]
            ]
            if predecessors:
                return False
            channels.extend((f"{run}:release", f"{run}:pages"))
            statuses[run] = "in_progress"
            return True

        statuses["A"] = "waiting"
        self.assertTrue(enter_critical_section("A"))
        self.assertFalse(enter_critical_section("B"))
        self.assertFalse(enter_critical_section("C"))
        statuses["A"] = "completed"
        self.assertTrue(enter_critical_section("B"))
        self.assertFalse(enter_critical_section("C"))
        statuses["B"] = "completed"
        self.assertTrue(enter_critical_section("C"))
        self.assertEqual(
            channels,
            ["A:release", "A:pages", "B:release", "B:pages", "C:release", "C:pages"],
        )
        self.assertNotIn("cancelled", statuses.values())

    def test_release_provenance_is_recorded_uploaded_and_attached(self):
        record = step_block(self.flatpak, "Record release provenance")
        for argument in (
            "--source-sha",
            "--image-ref",
            "--image-digest",
            "--runtime-ref",
            "--runtime-commit",
            "--sdk-ref",
            "--sdk-commit",
            "--tool-version",
            "--tool-ref",
            "--bundle",
            "--repository-tar",
        ):
            with self.subTest(argument=argument):
                self.assertIn(argument, record)
        self.assertIn("flatpak info", record)
        self.assertIn('--source-sha "${EXPECTED_SOURCE_SHA}"', record)
        self.assertNotIn('--source-sha "${GITHUB_SHA}"', record)
        self.assertIn('airloom-repo-${arch}.tar', record)
        archive = step_block(self.flatpak, "Archive update-repo artifact")
        self.assertIn('tar -cf "airloom-repo-${{ matrix.arch }}.tar" repo', archive)
        self.assertLess(
            self.flatpak.index("name: Archive update-repo artifact"),
            self.flatpak.index("name: Record release provenance"),
        )
        self.assertIn("provenance.json", step_block(self.flatpak, "Upload workflow artifact"))
        self.assertIn(
            "provenance.json",
            step_block(self.flatpak, "Upload update-repo artifact"),
        )
        self.assertIn("dist/*.provenance.json", self.release)

    def test_downloaded_artifacts_are_bound_before_signing_or_publication(self):
        for job, later_step in (
            (self.verify, "Import production signing key"),
            (self.release, "Create release and upload bundles"),
            (self.publish, "Import production signing key"),
        ):
            with self.subTest(step=later_step):
                self.assertIn("scripts/check-release-provenance.py", job)
                self.assertIn('--source-sha "${EXPECTED_SOURCE_SHA}"', job)
                self.assertLess(
                    job.index("name: Verify downloaded artifact provenance"),
                    job.index(f"name: {later_step}"),
                )
        release_check = step_block(self.release, "Verify downloaded artifact provenance")
        self.assertIn("--bundle", release_check)
        self.assertIn("for arch in x86_64 aarch64", release_check)
        self.assertIn('dist/Airloom-${arch}.flatpak', release_check)
        self.assertIn('dist/Airloom-${arch}.provenance.json', release_check)

    def test_repository_tars_are_verified_before_extraction_or_key_use(self):
        for job in (self.verify, self.publish):
            with self.subTest(job=job):
                check = step_block(job, "Verify downloaded artifact provenance")
                self.assertIn("--repository-tar", check)
                self.assertIn(
                    'repos/airloom-repo-${repo_arch}/airloom-repo-${repo_arch}.tar',
                    check,
                )
                self.assertLess(
                    job.index("name: Verify downloaded artifact provenance"),
                    job.index("name: Extract update-repo artifacts"),
                )
                key_step = (
                    "Import production signing key"
                    if job is self.publish
                    else "Generate ephemeral signing key"
                )
                self.assertLess(
                    job.index("name: Verify downloaded artifact provenance"),
                    job.index(f"name: {key_step}"),
                )

    def test_every_checkout_is_pinned_and_verified_against_one_source_commit(self):
        for job_name, job in (
            ("test", job_block(self.source, "test")),
            ("webkit-ui", job_block(self.source, "webkit-ui")),
            ("release-eligibility", self.eligibility),
            ("flatpak", self.flatpak),
            ("verify-client", self.verify),
            ("release", self.release),
            ("publish-repo", self.publish),
        ):
            with self.subTest(job=job_name):
                self.assertIn("actions/checkout@", job)
                self.assertRegex(job, r"(?m)^          ref: \$\{\{")
                self.assertIn("git rev-parse HEAD", job)
                self.assertIn("EXPECTED_SOURCE_SHA", job)
        self.assertIn("outputs:\n      eligible:", self.eligibility)
        self.assertIn("source_sha: ${{ steps.check.outputs.source_sha }}", self.eligibility)

    def test_publication_permissions_and_secrets_remain_channel_scoped(self):
        self.assertIn("      contents: write", self.release)
        self.assertNotIn("pages: write", self.release)
        self.assertNotIn("id-token: write", self.release)
        self.assertNotIn("FLATPAK_GPG_PRIVATE_KEY", self.release)
        self.assertIn("GH_TOKEN: ${{ github.token }}", self.release)

        self.assertIn("pages: write", self.publish)
        self.assertIn("id-token: write", self.publish)
        self.assertIn("contents: read", self.publish)
        self.assertNotIn("contents: write", self.publish)
        self.assertIn("secrets.FLATPAK_GPG_PRIVATE_KEY", self.publish)
        self.assertNotIn("GH_TOKEN", self.publish)
        self.assertIn(
            "FLATPAK_GPG_PRIVATE_KEY: ${{ secrets.FLATPAK_GPG_PRIVATE_KEY }}",
            self.coordinator,
        )
        for permission in ("contents: write", "pages: write", "id-token: write"):
            with self.subTest(coordinator_permission=permission):
                self.assertIn(permission, self.coordinator)

    def test_release_container_is_pinned_to_a_resolved_image_digest(self):
        self.assertRegex(
            self.source,
            r"ghcr\.io/flathub-infra/flatpak-github-actions@sha256:[0-9a-f]{64}",
        )


class ReleaseDocumentationTest(unittest.TestCase):
    def test_downloaded_bundle_examples_use_published_asset_names(self):
        self.assertIn("bundle: Airloom-${{ matrix.arch }}.flatpak", WORKFLOW.read_text(encoding="utf-8"))
        for path in (RELEASE_GUIDE, README):
            with self.subTest(doc=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("./Airloom-ARCH.flatpak", text)
                for match in re.finditer(r"(\S*)Airloom-VERSION-ARCH\.flatpak", text):
                    self.assertTrue(match.group(1).endswith("dist/"), match.group(0))

    def test_release_guide_documents_failed_tag_recovery(self):
        text = RELEASE_GUIDE.read_text(encoding="utf-8")
        section = text[text.index("## If a tag run fails"):]
        for expected in (
            "gh release view vX.Y.Z --repo ijbaird/airloom",
            "deployments?environment=github-pages&sha=",
            "git push --delete origin vX.Y.Z",
            "git tag -d vX.Y.Z",
            "never move or delete the tag",
            "next patch version",
        ):
            self.assertIn(expected, section)

    def test_release_guide_states_the_signing_key_rotation_policy(self):
        text = RELEASE_GUIDE.read_text(encoding="utf-8")
        self.assertIn("--import-candidate-key", text)
        self.assertIn("only its origin URL is redirected", text)
        self.assertIn("fails the protected upgrade gate until a reviewed client migration", text)


if __name__ == "__main__":
    unittest.main()
