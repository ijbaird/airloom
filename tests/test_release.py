from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHECK_RELEASE = PROJECT_ROOT / "scripts" / "check-release.py"
RECORD_PROVENANCE = PROJECT_ROOT / "scripts" / "record-provenance.sh"
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "flatpak.yml"
SOURCE_SHA = "a" * 40
RUNTIME_COMMIT = "b" * 64
SDK_COMMIT = "c" * 64
IMAGE_DIGEST = "sha256:" + "d" * 64
IMAGE_REF = f"ghcr.io/flathub-infra/flatpak-github-actions@{IMAGE_DIGEST}"


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


def provenance_command(output: Path, bundle: Path) -> list[str]:
    return [
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


class ReleaseWorkflowContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKFLOW.read_text(encoding="utf-8")
        cls.eligibility = job_block(cls.source, "release-eligibility")
        cls.flatpak = job_block(cls.source, "flatpak")
        cls.release = job_block(cls.source, "release")
        cls.publish = job_block(cls.source, "publish-repo")

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
        self.assertIn("--ref", self.eligibility)
        self.assertIn("--version-root", self.eligibility)
        self.assertIn("needs: [test, release-eligibility]", self.flatpak)

    def test_only_eligible_releases_can_sign_or_archive_stable_repository(self):
        eligible = "needs.release-eligibility.outputs.eligible == 'true'"
        for job in (self.flatpak, self.publish):
            with self.subTest(step="Import signing key"):
                self.assertIn(eligible, step_block(job, "Import signing key"))
        for step_name in ("Archive update-repo artifact", "Upload update-repo artifact"):
            with self.subTest(step=step_name):
                self.assertIn(eligible, step_block(self.flatpak, step_name))

    def test_only_eligible_releases_can_publish(self):
        eligible = "if: needs.release-eligibility.outputs.eligible == 'true'"
        self.assertIn(eligible, self.release)
        self.assertIn(eligible, self.publish)
        self.assertIn("needs: [flatpak, release-eligibility]", self.release)
        self.assertIn("needs: [flatpak, release-eligibility]", self.publish)

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
        ):
            with self.subTest(argument=argument):
                self.assertIn(argument, record)
        self.assertIn("flatpak info", record)
        self.assertIn("provenance.json", step_block(self.flatpak, "Upload workflow artifact"))
        self.assertIn("dist/*.provenance.json", self.release)

    def test_release_container_is_pinned_to_a_resolved_image_digest(self):
        self.assertRegex(
            self.source,
            r"ghcr\.io/flathub-infra/flatpak-github-actions@sha256:[0-9a-f]{64}",
        )


if __name__ == "__main__":
    unittest.main()
