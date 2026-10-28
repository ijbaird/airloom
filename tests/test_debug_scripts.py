import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent


FAKE_APP = r'''#!/usr/bin/env python3
import json
import os
import socket
from pathlib import Path

record = Path(os.environ["AIRLOOM_TEST_ENV_RECORD"])
record.write_text(json.dumps({
    "pid": os.getpid(),
    "XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME"),
    "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME"),
}), encoding="utf-8")
for variable in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
    app_dir = Path(os.environ[variable]) / "airloom"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "child-write").write_text(variable, encoding="utf-8")

sock_path = os.environ["AIRLOOM_DEBUG_SOCKET"]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(sock_path)
server.listen(5)
zoom = 14
center = {"lat": 45.5152, "lon": -122.6784}
sensor_payloads = 0
quitting = False
while not quitting:
    conn, _ = server.accept()
    data = b""
    while b"\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    message = json.loads(data.split(b"\n", 1)[0])
    cmd = message.get("cmd")
    result = {}
    error = None
    if cmd == "ping":
        result = {"pong": True}
    elif cmd == "state":
        result = {
            "sensorCount": 3,
            "viewportKm": 20 if zoom == 14 else 80,
            "heatmapActive": zoom == 8,
            "center": center,
        }
    elif cmd == "screenshot":
        Path(message["path"]).write_bytes(b"fake png")
        result = {"path": message["path"]}
    elif cmd == "quit":
        quitting = True
    elif cmd == "eval":
        js = message["js"]
        if "debugZoomTo(14)" in js:
            zoom = 14
            result = None
        elif "debugZoomTo(8)" in js:
            zoom = 8
            result = None
        elif "getImageData" in js:
            result = 0 if zoom == 14 else 100
        elif "querySelectorAll('.map-marker')" in js:
            result = 3 if zoom == 14 else 0
        elif "querySelector('#map-popup')" in js:
            result = True
        elif 'querySelector("#map-panel")' in js:
            result = 3
        elif "window.__origReceive = window.Airloom.receive" in js:
            result = "installed"
        elif js.strip() == "window.__sensorsPayloads":
            result = sensor_payloads
        elif "form.elements.home_mode" in js:
            center = {"lat": 43.615, "lon": -116.2023}
            result = "submitted"
        elif 'window.Airloom.receive("location"' in js:
            center = {"lat": 47.6062, "lon": -122.3321}
            result = "glided"
        elif "form.requestSubmit" in js:
            sensor_payloads += 1
            result = "submitted"
        else:
            error = "unexpected eval: " + js
    else:
        error = "unexpected command: " + repr(cmd)
    response = {"id": message.get("id"), "ok": error is None}
    if error is None:
        response["result"] = result
    else:
        response["error"] = error
    conn.sendall(json.dumps(response).encode("utf-8") + b"\n")
    conn.close()
server.close()
'''


FAKE_PKILL = r'''#!/usr/bin/env python3
import os
import signal
import sys

# Confine the reproduced historical kill to this test's own disposable
# process, even when running the unfixed launcher on a developer machine.
if sys.argv[1:] != ["-f", "python3 -m airloom"]:
    raise SystemExit(1)
os.kill(int(os.environ["AIRLOOM_TEST_SENTINEL_PID"]), signal.SIGTERM)
'''


def write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


def tree_snapshot(root: Path) -> dict:
    snapshot = {}
    for path in root.rglob("*"):
        relative = str(path.relative_to(root))
        if path.is_dir():
            snapshot[relative] = ("directory", "")
        else:
            snapshot[relative] = ("file", path.read_bytes())
    return snapshot


class DebugScriptIsolationTest(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.root = Path(self._tempdir.name)

    def test_heatmap_child_is_isolated_and_unrelated_sentinels_survive(self):
        self._exercise_gui_script("test-heatmap")

    def test_save_refresh_child_is_isolated_and_unrelated_sentinels_survive(self):
        self._exercise_gui_script("test-save-refresh")

    def _exercise_gui_script(self, script_name: str) -> None:
        project = self.root / "project"
        scripts = project / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(PROJECT / "scripts" / "debug-run", scripts / "debug-run")
        shutil.copy2(PROJECT / "scripts" / script_name, scripts / script_name)
        write_executable(project / "run", FAKE_APP)

        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        write_executable(fake_bin / "pkill", FAKE_PKILL)
        write_executable(fake_bin / "flatpak", "#!/bin/sh\nexit 0\n")

        sentinel_config = self.root / "real-config"
        sentinel_cache = self.root / "real-cache"
        (sentinel_config / "airloom").mkdir(parents=True)
        (sentinel_cache / "airloom").mkdir(parents=True)
        (sentinel_config / "airloom" / "config.json").write_bytes(b"real config sentinel")
        (sentinel_cache / "airloom" / "cache.db").write_bytes(b"real cache sentinel")
        before = (tree_snapshot(sentinel_config), tree_snapshot(sentinel_cache))

        unrelated_project = self.root / "unrelated-app"
        (unrelated_project / "airloom").mkdir(parents=True)
        (unrelated_project / "airloom" / "__init__.py").write_text("", encoding="utf-8")
        (unrelated_project / "airloom" / "__main__.py").write_text(
            "import time\nwhile True:\n    time.sleep(1)\n",
            encoding="utf-8",
        )
        unrelated = subprocess.Popen(
            [sys.executable, "-m", "airloom"],
            cwd=unrelated_project,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._stop_owned_process, unrelated)

        tmpdir = self.root / "tmp"
        tmpdir.mkdir()
        record = self.root / f"{script_name}-child-env.json"
        env = {
            **os.environ,
            "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            "TMPDIR": str(tmpdir),
            "XDG_CONFIG_HOME": str(sentinel_config),
            "XDG_CACHE_HOME": str(sentinel_cache),
            "AIRLOOM_TEST_ENV_RECORD": str(record),
            "AIRLOOM_TEST_SENTINEL_PID": str(unrelated.pid),
        }
        completed = subprocess.run(
            [sys.executable, str(scripts / script_name)],
            cwd=project,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )

        child = json.loads(record.read_text(encoding="utf-8"))
        for variable, sentinel in (
            ("XDG_CONFIG_HOME", sentinel_config),
            ("XDG_CACHE_HOME", sentinel_cache),
        ):
            private = Path(child[variable])
            self.assertNotEqual(private, sentinel, f"{variable} leaked into the caller's directory")
            self.assertTrue(
                private.is_relative_to(tmpdir),
                f"{variable} was not private to the GUI test: {private}",
            )
        self.assertEqual(tree_snapshot(sentinel_config), before[0])
        self.assertEqual(tree_snapshot(sentinel_cache), before[1])

        with self.assertRaises(ProcessLookupError, msg="the launched GUI child was not cleaned up"):
            os.kill(child["pid"], 0)
        self.assertIsNone(unrelated.poll(), "the GUI launcher terminated an unrelated Airloom process")

    def _stop_owned_process(self, process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
