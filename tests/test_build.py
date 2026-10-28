import subprocess
import unittest
from pathlib import Path


class BuildCheckTest(unittest.TestCase):
    def test_check_includes_javascript_tests(self):
        project_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            ["make", "-n", "check"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("node --test", result.stdout)
