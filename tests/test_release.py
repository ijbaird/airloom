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
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "flatpak.yml"
PUBLISH_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "publish.yml"
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

    def test_repository_tar_provenance_is_bound_to_the_source_commit(self):
        result = self.check(source_sha="e" * 40)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("source_sha", result.stderr)


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
    def run_client_gate(self, **fake_overrides):
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
correct_key = Path(env["FAKE_CORRECT_KEY"]).read_bytes()

def save():
    state_path.write_text(json.dumps(state))
    remotes_path.write_text(json.dumps(remotes))

def option(name):
    return next((arg.split("=", 1)[1] for arg in args if arg.startswith(name + "=")), None)

def key_matches(path):
    return path is not None and Path(path).read_bytes() == correct_key

if command == "remote-add":
    name, url = args[-2:]
    key_ok = key_matches(option("--gpg-import"))
    if not key_ok and env.get("FAKE_WRONG_KEY_REMOTE_ADD_SIGNATURE") == "1":
        print("OSTree GPG signature verification failed: wrong key", file=sys.stderr)
        sys.exit(1)
    if not key_ok and env.get("FAKE_WRONG_KEY_REMOTE_ADD_UNRELATED") == "1":
        print("network unreachable while fetching metadata", file=sys.stderr)
        sys.exit(1)
    remotes[name] = {"url": url, "key_ok": key_ok}
    save()
elif command == "remote-modify":
    name = args[-1]
    remote = remotes[name]
    remote["url"] = option("--url") or remote["url"]
    remote["key_ok"] = remote["key_ok"] or key_matches(option("--gpg-import"))
    save()
elif command == "install":
    bundle = next((arg for arg in args if arg.endswith(".flatpak")), None)
    if bundle is not None:
        previous = bundle == env["FAKE_PREVIOUS_BUNDLE"]
        if not previous:
            signature = env.get("FAKE_CURRENT_BUNDLE_SIGNATURE", "selected")
            selected_key = option("--gpg-file")
            if selected_key is not None:
                if signature == "unsigned":
                    print("GPG signature missing for current bundle", file=sys.stderr)
                    sys.exit(1)
                if signature != "selected" or not key_matches(selected_key):
                    print("GPG signature verification failed: bundle was not signed by the selected key", file=sys.stderr)
                    sys.exit(1)
        version = previous_version if previous else current_version
        origin = env.get("FAKE_ORIGIN", "ai.stealthvision.Airloom-origin")
        state = {
            "version": version,
            "commit": "previous-commit" if previous else "current-commit",
            "origin": origin,
        }
        remotes[origin] = {
            "url": env.get("FAKE_BUNDLE_URL", "https://ijbaird.github.io/airloom/repo"),
            "key_ok": True,
        }
    else:
        remote_name = args[-2]
        remote = remotes[remote_name]
        if not remote["key_ok"] and env.get("FAKE_ACCEPT_WRONG_KEY") != "1":
            if env.get("FAKE_WRONG_KEY_INSTALL_UNRELATED") == "1":
                print("network unreachable while pulling commit", file=sys.stderr)
            else:
                print("GPG signature verification failed: wrong key", file=sys.stderr)
            sys.exit(1)
        state = {
            "version": current_version,
            "commit": "current-commit",
            "origin": remote_name,
        }
    save()
elif command == "update":
    remote = remotes[state["origin"]]
    if not remote["key_ok"] and env.get("FAKE_ACCEPT_WRONG_KEY") != "1":
        print("GPG signature verification failed: wrong key", file=sys.stderr)
        sys.exit(1)
    if env.get("FAKE_STALE_UPGRADE") != "1":
        state["version"] = current_version
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
                "if '--export' in sys.argv:\n"
                "    sys.stdout.buffer.write(b'wrong-test-key')\n",
                encoding="utf-8",
            )
            gpg.chmod(0o755)

            env = dict(
                os.environ,
                PATH=f"{bin_dir}:{os.environ['PATH']}",
                TMPDIR=str(root),
                GITHUB_STEP_SUMMARY=str(summary),
                FLATPAK_GPG_PUBLIC_KEY=str(correct_key),
                GNUPGHOME=str(root / "correct-home"),
                FAKE_FLATPAK_LOG=str(log),
                FAKE_CORRECT_KEY=str(correct_key),
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
        self.assertTrue(
            any(argument.startswith("--gpg-file=") for argument in current_bundle_install)
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
            "bundle_verified origin=ai.stealthvision.Airloom-origin "
            "url=https://ijbaird.github.io/airloom/repo version=1.2.3",
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

    def test_wrong_bundle_origin_is_rejected(self):
        result, _, _ = self.run_client_gate(FAKE_ORIGIN="untrusted-origin")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("origin", result.stderr)

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
        self.assertIn("flatpak build-sign", development)
        self.assertIn("flatpak build-bundle", development)
        self.assertIn("FLATPAK_GPG_PUBLIC_KEY", development)
        for step_name in ("Archive update-repo artifact", "Upload update-repo artifact"):
            self.assertNotIn("needs.release-eligibility.outputs.eligible", step_block(self.flatpak, step_name))
        self.assertNotIn("--no-gpg-verify", self.source)

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


if __name__ == "__main__":
    unittest.main()
