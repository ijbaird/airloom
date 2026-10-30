import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent


FAKE_APP = r'''#!/usr/bin/env python3
import json
import os
import re
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
import re
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
        unrelated = self._launch_owned_process(
            [sys.executable, "-m", "airloom"],
            cwd=unrelated_project,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

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
        launched = self._launch_owned_process(
            [sys.executable, str(scripts / script_name)],
            cwd=project,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = launched.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            self._stop_owned_process(launched)
            raise
        completed = subprocess.CompletedProcess(
            launched.args, launched.returncode, stdout, stderr
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

    def test_screenshot_session_gets_a_private_compositor_bus_and_directories(self):
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        record = self.root / "session.json"
        write_executable(
            fake_bin / "dbus-run-session",
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            f"open({str(record)!r}, 'w').write(json.dumps("
            "{'argv': sys.argv[1:], 'env': dict(os.environ)}))\n",
        )
        write_executable(fake_bin / "mutter", "#!/bin/sh\nexit 1\n")
        sentinel_config = self.root / "real-config"
        (sentinel_config / "airloom").mkdir(parents=True)
        (sentinel_config / "airloom" / "config.json").write_bytes(b"real config sentinel")
        before = tree_snapshot(sentinel_config)
        tmpdir = self.root / "tmp"
        tmpdir.mkdir()
        output = self.root / "shots"
        env = {
            **os.environ,
            "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            "TMPDIR": str(tmpdir),
            "XDG_CONFIG_HOME": str(sentinel_config),
            "XDG_RUNTIME_DIR": str(self.root / "real-runtime"),
            "DISPLAY": ":99",
            "WAYLAND_DISPLAY": "wayland-real",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/real-bus",
            "AIRLOOM_DEBUG_FAKE_PURPLEAIR": "/nonexistent/fixture.json",
        }

        completed = subprocess.run(
            [sys.executable, str(PROJECT / "scripts" / "make-screenshots"), "--output", str(output)],
            env=env, capture_output=True, text=True, timeout=30,
        )

        # The fake session never runs the capture, so the script must not
        # report success or leave images behind.
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("FAIL", completed.stderr)
        self.assertFalse(output.exists() and any(output.iterdir()))
        session = json.loads(record.read_text(encoding="utf-8"))
        argv, child_env = session["argv"], session["env"]
        self.assertEqual(argv[:2], ["--", "mutter"])
        for flag in ("--headless", "--wayland", "--no-x11"):
            self.assertIn(flag, argv)
        self.assertEqual(child_env["WAYLAND_DISPLAY"], argv[argv.index("--wayland-display") + 1])
        self.assertNotEqual(child_env["WAYLAND_DISPLAY"], "wayland-real")
        for variable in ("DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "AIRLOOM_DEBUG_FAKE_PURPLEAIR"):
            self.assertFalse(variable in child_env, f"{variable} leaked into the session")
        for variable in ("XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                         "XDG_DATA_HOME", "XDG_STATE_HOME"):
            self.assertTrue(Path(child_env[variable]).is_relative_to(tmpdir), variable)
        self.assertEqual(child_env["GSETTINGS_BACKEND"], "memory")
        self.assertEqual(tree_snapshot(sentinel_config), before)
        self.assertEqual(list(tmpdir.iterdir()), [], "the private session directory was not removed")

    def test_screenshot_timeout_stops_the_separately_sessioned_app(self):
        self._exercise_screenshot_abort(timeout_s=3, cancel_signal=None)

    def test_screenshot_sigterm_stops_the_separately_sessioned_app(self):
        self._exercise_screenshot_abort(timeout_s=120, cancel_signal=signal.SIGTERM)

    def test_screenshot_sigint_stops_the_separately_sessioned_app(self):
        self._exercise_screenshot_abort(timeout_s=120, cancel_signal=signal.SIGINT)

    def test_screenshot_signal_during_timeout_cleanup_still_sweeps(self):
        self._exercise_screenshot_abort(
            timeout_s=3, cancel_signal=None, signal_during_cleanup=signal.SIGTERM
        )

    def test_screenshot_second_sigint_during_cleanup_is_ignored(self):
        self._exercise_screenshot_abort(
            timeout_s=120, cancel_signal=signal.SIGINT, signal_during_cleanup=signal.SIGINT
        )

    def test_mountinfo_paths_decode_octal_escapes_without_mangling_unicode(self):
        from importlib.machinery import SourceFileLoader
        from importlib.util import module_from_spec, spec_from_loader
        from unittest import mock

        loader = SourceFileLoader("make_screenshots", str(PROJECT / "scripts" / "make-screenshots"))
        module = module_from_spec(spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        root = Path("/tmp/café/airloom-screenshots-x")
        mountinfo = (
            "22 1 0:21 / / rw - ext4 /dev/root rw\n"
            "40 22 0:50 / /tmp/café/airloom-screenshots-x/runtime/doc rw - fuse.portal portal rw\n"
            "41 22 0:51 / /tmp/café/airloom-screenshots-x/runtime/a\\040b\\011c\\012d\\134e rw - fuse f rw\n"
            "42 22 0:52 / /tmp/café/other rw - fuse g rw\n"
        )
        original_text, original_bytes = Path.read_text, Path.read_bytes

        def read_text(path, *args, **kwargs):
            return mountinfo if str(path) == "/proc/self/mountinfo" else original_text(path, *args, **kwargs)

        def read_bytes(path):
            return mountinfo.encode("utf-8") if str(path) == "/proc/self/mountinfo" else original_bytes(path)

        calls = []
        with mock.patch.object(Path, "read_text", read_text), \
                mock.patch.object(Path, "read_bytes", read_bytes), \
                mock.patch.object(module.shutil, "which", lambda tool: f"/usr/bin/{tool}"), \
                mock.patch.object(module, "run_bounded",
                                  lambda command, *_args: calls.append(command[-1])):
            module.detach_mounts(root)

        self.assertEqual(sorted(calls), sorted([
            "/tmp/café/airloom-screenshots-x/runtime/doc",
            "/tmp/café/airloom-screenshots-x/runtime/a b\tc\nd\\e",
        ]))

    def test_screenshot_stuck_unmount_is_bounded_reaped_and_fails_the_run(self):
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        # The session "succeeds" at once: mutter's stand-in writes the runner's
        # success marker instead of running it.
        write_executable(fake_bin / "dbus-run-session", '#!/bin/sh\n[ "$1" = "--" ] && shift\nexec "$@"\n')
        write_executable(
            fake_bin / "mutter",
            '#!/bin/sh\nwhile [ "$#" -gt 0 ] && [ "$1" != "--inside-compositor" ]; do shift; done\n'
            'echo ok > "$2/done"\n',
        )
        unmounts = self.root / "unmounts.jsonl"
        # A stuck unmount: records itself, ignores SIGTERM, and never returns.
        stuck = (
            f"#!{sys.executable}\n"
            "import json, os, signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"with open({str(unmounts)!r}, 'a') as record:\n"
            "    record.write(json.dumps({'pid': os.getpid(), 'args': sys.argv[1:]}) + '\\n')\n"
            "time.sleep(300)\n"
        )
        for name in ("fusermount3", "fusermount"):
            write_executable(fake_bin / name, stuck)
        tmpdir = self.root / "tmp"
        tmpdir.mkdir()
        # Runs the real owner in-process with /proc/self/mountinfo listing two
        # FUSE mounts inside its private directory, and a 1 s unmount bound
        # (ignored by a script without one).
        driver = self.root / "driver.py"
        driver.write_text(
            "import glob, json, os, sys, time\n"
            "from importlib.machinery import SourceFileLoader\n"
            "from importlib.util import module_from_spec, spec_from_loader\n"
            "from pathlib import Path\n"
            "script, tmpdir, output, unmounts = sys.argv[1:5]\n"
            "loader = SourceFileLoader('make_screenshots', script)\n"
            "module = module_from_spec(spec_from_loader(loader.name, loader))\n"
            "loader.exec_module(module)\n"
            "module.UNMOUNT_TIMEOUT_S = 1\n"
            "original = Path.read_bytes\n"
            "def read_bytes(path):\n"
            "    if str(path) != '/proc/self/mountinfo':\n"
            "        return original(path)\n"
            "    lines = [f'90 1 0:90 / {root}/{name} rw - fuse.fake fake rw'\n"
            "             for root in glob.glob(os.path.join(tmpdir, 'airloom-screenshots-*'))\n"
            "             for name in ('runtime', 'state')]\n"
            "    return ''.join(line + '\\n' for line in lines).encode()\n"
            "Path.read_bytes = read_bytes\n"
            "start = time.monotonic()\n"
            "try:\n"
            "    module.main(['--output', output])\n"
            "    outcome = {'result': 'success', 'message': ''}\n"
            "except BaseException as exc:\n"
            "    outcome = {'result': type(exc).__name__, 'message': str(exc)}\n"
            "outcome['elapsed'] = time.monotonic() - start\n"
            "pids = [json.loads(line)['pid'] for line in open(unmounts)] if os.path.exists(unmounts) else []\n"
            "outcome['unreaped'] = [pid for pid in pids if os.path.exists(f'/proc/{pid}')]\n"
            "print('OUTCOME ' + json.dumps(outcome))\n",
            encoding="utf-8",
        )
        env = {
            **os.environ,
            "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            "TMPDIR": str(tmpdir),
        }
        process = self._launch_owned_process(
            [sys.executable, str(driver), str(PROJECT / "scripts" / "make-screenshots"),
             str(tmpdir), str(self.root / "shots"), str(unmounts)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self._kill_recorded_unmounts, unmounts)
        guard_s = 25
        try:
            stdout, stderr = process.communicate(timeout=guard_s)
        except subprocess.TimeoutExpired:
            self._stop_owned_process(process)
            self.fail(f"cleanup did not finish within the {guard_s}s guard (stuck unmount is unbounded)")

        outcome = json.loads(next(line for line in stdout.splitlines()
                                  if line.startswith("OUTCOME "))[len("OUTCOME "):])
        # A leftover mount must not pass as success even though capture succeeded.
        self.assertEqual(outcome["result"], "AssertionError", stdout + stderr)
        self.assertIn("unmount", outcome["message"])
        self.assertIn("warning", stderr.lower())
        # Each of the two unmounts is bounded (1 s plus the kill), so the
        # whole cleanup finishes far inside the guard.
        self.assertLess(outcome["elapsed"], 10, outcome)
        calls = [json.loads(line) for line in unmounts.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(calls), 2, "a stuck unmount skipped the next mount")
        for call in calls:
            self.assertEqual(call["args"][:2], ["-u", "-z"])
        self.assertEqual(outcome["unreaped"], [], "a timed-out unmount was left unreaped")
        # Removal was still attempted: everything but the still-mounted
        # directories is gone.
        (root,) = list(tmpdir.iterdir())
        self.assertEqual(sorted(path.name for path in root.iterdir()), ["runtime", "state"])

    def _kill_recorded_unmounts(self, record: Path) -> None:
        if record.exists():
            for line in record.read_text(encoding="utf-8").splitlines():
                self._kill_owned_pid(json.loads(line)["pid"])

    def _exercise_screenshot_abort(self, timeout_s: int, cancel_signal, signal_during_cleanup=None) -> None:
        # A copy of the real script in a fake project whose debug-run never
        # opens a debug socket, so generation stalls until it is aborted.
        project = self.root / "project"
        (project / "scripts").mkdir(parents=True)
        script = (PROJECT / "scripts" / "make-screenshots").read_text(encoding="utf-8")
        shortened = re.sub(r"^TIMEOUT_S = \d+$", f"TIMEOUT_S = {timeout_s}", script, flags=re.MULTILINE)
        self.assertNotEqual(shortened, script, "make-screenshots has no TIMEOUT_S constant to shorten")
        write_executable(project / "scripts" / "make-screenshots", shortened)
        record = self.root / "fake-app.json"
        write_executable(
            project / "scripts" / "debug-run",
            f"#!{sys.executable}\n"
            "import json, os, subprocess, sys, time\n"
            "helper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
            f"open({str(record) + '.tmp'!r}, 'w').write(json.dumps("
            "{'app': os.getpid(), 'helper': helper.pid, 'runner': os.getppid()}))\n"
            f"os.replace({str(record) + '.tmp'!r}, {str(record)!r})\n"
            "time.sleep(300)\n",
        )
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        # Stand-ins that just run the wrapped command in place, as the real
        # bus and compositor do, without a display.
        forwarded = self.root / "forwarded"
        if signal_during_cleanup is None:
            write_executable(
                fake_bin / "dbus-run-session",
                '#!/bin/sh\n[ "$1" = "--" ] && shift\nexec "$@"\n',
            )
        else:
            # The runner gets its own session, so only this stand-in receives
            # the owner's SIGTERM to the session group, which by construction
            # happens inside the owner's cleanup. It kills the runner outright
            # (no chance to clean up the app), records the handshake, and
            # signals the owner mid-cleanup.
            write_executable(
                fake_bin / "dbus-run-session",
                f"#!{sys.executable}\n"
                "import os, signal, subprocess, sys\n"
                "args = sys.argv[1:]\n"
                "if args[:1] == ['--']:\n    args = args[1:]\n"
                "runner = subprocess.Popen(args, start_new_session=True)\n"
                "def on_term(_signum, _frame):\n"
                # Raw calls: Popen.wait() here would deadlock on the lock
                # held by the interrupted wait() below.
                "    os.kill(runner.pid, signal.SIGKILL)\n"
                "    os.waitpid(runner.pid, 0)\n"
                f"    open({str(forwarded)!r}, 'w').write('sent')\n"
                f"    os.kill(os.getppid(), {int(signal_during_cleanup)})\n"
                "    os._exit(0)\n"
                "signal.signal(signal.SIGTERM, on_term)\n"
                "sys.exit(runner.wait())\n",
            )
        write_executable(
            fake_bin / "mutter",
            '#!/bin/sh\nwhile [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n',
        )
        sentinel_project = self.root / "unrelated-app"
        (sentinel_project / "airloom").mkdir(parents=True)
        (sentinel_project / "airloom" / "__init__.py").write_text("", encoding="utf-8")
        (sentinel_project / "airloom" / "__main__.py").write_text(
            "import time\nwhile True:\n    time.sleep(1)\n", encoding="utf-8"
        )
        sentinel = self._launch_owned_process(
            [sys.executable, "-m", "airloom"], cwd=sentinel_project,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        tmpdir = self.root / "tmp"
        tmpdir.mkdir()
        env = {
            **os.environ,
            "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            "TMPDIR": str(tmpdir),
        }

        # A file, not a pipe: descendants inheriting a pipe would make
        # waiting for EOF also wait for them.
        log_path = self.root / "make-screenshots.log"
        log = open(log_path, "wb")
        self.addCleanup(log.close)
        outer = self._launch_owned_process(
            [sys.executable, str(project / "scripts" / "make-screenshots"),
             "--output", str(self.root / "shots")],
            env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 20
        while not record.exists() and outer.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(record.exists(), "the fake app never started")
        owned = json.loads(record.read_text(encoding="utf-8"))
        for pid in owned.values():
            self.addCleanup(self._kill_owned_pid, pid)
        if cancel_signal is not None:
            outer.send_signal(cancel_signal)
        outer.wait(timeout=timeout_s + 30)
        output = log_path.read_text(encoding="utf-8", errors="replace")

        self.assertNotEqual(outer.returncode, 0, output)
        if signal_during_cleanup is not None:
            self.assertTrue(forwarded.exists(), "no signal was delivered during cleanup")
        for role, pid in owned.items():
            self.assertTrue(self._process_is_stopped(pid),
                            f"the owned {role} process {pid} outlived make-screenshots")
        self.assertIsNone(sentinel.poll(), "make-screenshots terminated an unrelated Airloom process")
        self.assertEqual(list(tmpdir.iterdir()), [], "the private session directory was not removed")

    def test_owned_process_cleanup_terminates_the_entire_process_group(self):
        record = self.root / "owned-child.pid"
        parent_program = (
            "import subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(60)']); "
            "open(sys.argv[1], 'w').write(str(child.pid)); "
            "time.sleep(60)"
        )
        parent = subprocess.Popen(
            [sys.executable, "-c", parent_program, str(record)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._stop_owned_process, parent)
        deadline = time.monotonic() + 2
        while not record.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        child_pid = int(record.read_text(encoding="utf-8"))
        self.addCleanup(self._kill_owned_pid, child_pid)

        self._stop_owned_process(parent)

        self.assertIsNotNone(parent.poll())
        self.assertTrue(self._process_is_stopped(child_pid), "owned child survived launcher cleanup")

    def _process_is_stopped(self, pid: int) -> bool:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2]
                if state == "Z":
                    return True
            except FileNotFoundError:
                return True
            time.sleep(0.01)
        return False

    def _kill_owned_pid(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _launch_owned_process(self, *args, **kwargs) -> subprocess.Popen:
        process = subprocess.Popen(*args, start_new_session=True, **kwargs)
        self.addCleanup(self._stop_owned_process, process)
        return process

    def _stop_owned_process(self, process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            process.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
