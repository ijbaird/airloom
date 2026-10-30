from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from airloom import __version__


PROJECT_ROOT = Path(__file__).resolve().parent.parent
METADATA = ET.parse(
    PROJECT_ROOT / "packaging" / "ai.stealthvision.Airloom.metainfo.xml"
)


class StagedInstallTest(unittest.TestCase):
    def assert_staged_launcher_resolves_installed_package(
        self, prefix: str | None, expected_prefix: str
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="airloom-install-test-") as temporary:
            root = Path(temporary)
            stage = root / "stage"
            command = ["make", "--no-print-directory", "install", f"DESTDIR={stage}"]
            if prefix is not None:
                command.append(f"PREFIX={prefix}")
            install_environment = os.environ.copy()
            for variable in ("PREFIX", "DESTDIR", "AIRLOOM_PREFIX"):
                install_environment.pop(variable, None)
            subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                env=install_environment,
                check=True,
                capture_output=True,
                text=True,
            )

            launcher = stage / expected_prefix.lstrip("/") / "bin" / "airloom"
            package = (
                stage
                / expected_prefix.lstrip("/")
                / "share"
                / "airloom"
                / "airloom"
                / "__init__.py"
            )
            self.assertTrue(launcher.is_file())
            self.assertTrue(package.is_file())

            outside = root / "outside"
            decoy = outside / "decoy"
            (decoy / "airloom").mkdir(parents=True)
            (decoy / "airloom" / "__init__.py").write_text(
                'raise RuntimeError("the decoy package must not be imported")\n'
            )
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            python_shim = fake_bin / "python3"
            python_shim.write_text(
                f"#!{sys.executable}\n"
                "import airloom\n"
                "print(airloom.__file__)\n"
            )
            python_shim.chmod(python_shim.stat().st_mode | stat.S_IXUSR)

            environment = os.environ.copy()
            environment.pop("AIRLOOM_PREFIX", None)
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["PYTHONPATH"] = str(decoy)
            result = subprocess.run(
                [str(launcher)],
                cwd=outside,
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(Path(result.stdout.strip()).resolve(), package.resolve())

    def test_default_prefix_resolves_outside_the_checkout(self):
        self.assert_staged_launcher_resolves_installed_package(None, "/usr/local")

    def test_usr_prefix_resolves_outside_the_checkout(self):
        self.assert_staged_launcher_resolves_installed_package("/usr", "/usr")

    def test_custom_prefix_resolves_outside_the_checkout(self):
        self.assert_staged_launcher_resolves_installed_package(
            "/opt/airloom", "/opt/airloom"
        )


class BuildVersionTest(unittest.TestCase):
    def test_bundle_filename_uses_committed_version_from_dirty_checkout(self):
        with tempfile.TemporaryDirectory(prefix="airloom-version-test-") as temporary:
            root = Path(temporary)
            fixture = root / "checkout"
            (fixture / "scripts").mkdir(parents=True)
            (fixture / "packaging").mkdir()
            (fixture / "airloom").mkdir()
            (fixture / "tests").mkdir()
            shutil.copy2(
                PROJECT_ROOT / "scripts" / "build-flatpak.sh",
                fixture / "scripts" / "build-flatpak.sh",
            )
            shutil.copy2(
                PROJECT_ROOT
                / "packaging"
                / "ai.stealthvision.Airloom.yml",
                fixture / "packaging" / "ai.stealthvision.Airloom.yml",
            )
            (fixture / "tests" / "__init__.py").write_text("")
            (fixture / "tests" / "test_smoke.py").write_text(
                "import unittest\n\n"
                "class SmokeTest(unittest.TestCase):\n"
                "    def test_fixture(self):\n"
                "        self.assertTrue(True)\n"
            )
            version_file = fixture / "airloom" / "__init__.py"
            version_file.write_text('__version__ = "1.2.3"\n')

            subprocess.run(["git", "init", "-q"], cwd=fixture, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Airloom Test"],
                cwd=fixture,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "airloom@example.invalid"],
                cwd=fixture,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=fixture, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "fixture"], cwd=fixture, check=True
            )
            version_file.write_text('__version__ = "9.9.9"\n')

            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            flatpak_shim = fake_bin / "flatpak"
            flatpak_shim.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  --default-arch) printf '%s\\n' x86_64 ;;\n"
                "  build-bundle)\n"
                "    mkdir -p \"$(dirname \"$3\")\"\n"
                "    printf 'disposable fixture\\n' > \"$3\"\n"
                "    ;;\n"
                "  *) printf 'unexpected flatpak command: %s\\n' \"$*\" >&2; exit 2 ;;\n"
                "esac\n"
            )
            flatpak_shim.chmod(flatpak_shim.stat().st_mode | stat.S_IXUSR)
            (root / "tmp").mkdir()
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["FLATPAK_BUILDER"] = "/bin/true"
            environment["TMPDIR"] = str(root / "tmp")
            environment["AIRLOOM_DIST_DIR"] = str(fixture / "dist")
            environment["AIRLOOM_BUILD_DIR"] = str(root / "build-dir")
            environment["AIRLOOM_REPO_DIR"] = str(root / "flatpak-repo")
            result = subprocess.run(
                [str(fixture / "scripts" / "build-flatpak.sh")],
                cwd=fixture,
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            expected = fixture / "dist" / "Airloom-1.2.3-x86_64.flatpak"
            dirty = fixture / "dist" / "Airloom-9.9.9-x86_64.flatpak"
            self.assertTrue(expected.is_file(), result.stdout + result.stderr)
            self.assertFalse(dirty.exists())
            self.assertIn(f"Built {expected}", result.stdout)


class ReleaseMetadataTest(unittest.TestCase):
    def test_appstream_provides_project_and_help_links(self):
        metadata = ET.parse(
            PROJECT_ROOT / "packaging" / "ai.stealthvision.Airloom.metainfo.xml"
        )
        links = {
            element.attrib["type"]: element.text
            for element in metadata.findall("./url")
        }

        expected = {
            "homepage": "https://github.com/ijbaird/airloom",
            "bugtracker": "https://github.com/ijbaird/airloom/issues",
            "vcs-browser": "https://github.com/ijbaird/airloom",
            "help": "https://github.com/ijbaird/airloom#readme",
        }
        for link_type, url in expected.items():
            with self.subTest(link_type=link_type):
                self.assertEqual(links.get(link_type), url)

    def test_current_version_matches_changelog_and_appstream(self):
        changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text()
        changelog_match = re.search(r"^## (.+)$", changelog, flags=re.MULTILINE)
        self.assertIsNotNone(changelog_match)
        heading = re.fullmatch(
            r"(\d+\.\d+\.\d+) — (\d{4}-\d{2}-\d{2})", changelog_match.group(1)
        )
        self.assertIsNotNone(
            heading, f"malformed CHANGELOG heading {changelog_match.group(0)!r}"
        )
        release = METADATA.find("./releases/release")
        self.assertIsNotNone(release)

        self.assertEqual(heading.group(1), __version__)
        self.assertEqual(release.attrib["version"], __version__)
        self.assertEqual(release.attrib.get("date"), heading.group(2))

    def test_appstream_releases_are_listed_newest_first(self):
        releases = METADATA.findall("./releases/release")
        versions = [
            tuple(int(part) for part in release.attrib["version"].split("."))
            for release in releases
        ]
        dates = [release.attrib["date"] for release in releases]

        self.assertEqual(versions, sorted(versions, reverse=True))
        self.assertEqual(len(set(versions)), len(versions))
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_screenshots_are_pinned_to_the_current_tag_and_shipped(self):
        screenshots = METADATA.findall("./screenshots/screenshot")
        self.assertTrue(screenshots, "AppStream metadata has no screenshots")
        self.assertEqual(screenshots[0].attrib.get("type"), "default")
        self.assertEqual(
            [shot.attrib.get("type") for shot in screenshots].count("default"), 1
        )
        prefix = f"https://raw.githubusercontent.com/ijbaird/airloom/v{__version__}/"
        for shot in screenshots:
            image = shot.find("image")
            with self.subTest(image=image is not None and image.text):
                caption = shot.find("caption")
                self.assertIsNotNone(caption)
                self.assertTrue((caption.text or "").strip())
                self.assertIsNotNone(image)
                self.assertEqual(image.attrib.get("type"), "source")
                self.assertTrue(image.text.startswith(prefix), image.text)
                path = PROJECT_ROOT / image.text.removeprefix(prefix)
                self.assertTrue(path.resolve().is_relative_to(PROJECT_ROOT))
                self.assertTrue(path.is_file(), f"{path} is missing")
                header = path.read_bytes()[:24]
                self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
                width = int.from_bytes(header[16:20], "big")
                height = int.from_bytes(header[20:24], "big")
                self.assertEqual(
                    (image.attrib.get("width"), image.attrib.get("height")),
                    (str(width), str(height)),
                )
                self.assertGreaterEqual(min(width, height), 760)


if __name__ == "__main__":
    unittest.main()
