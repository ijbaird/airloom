"""Display-free coverage for scripts/test-ui-regression and cross-layer
request-ordering guarantees it depends on.

The GUI scenarios themselves need GTK, WebKit and a display; these tests pin
the script's isolation contract (private config/cache, loopback geocoder,
child-only cleanup, stripped release tree) so `make check` catches
regressions without launching Airloom.
"""

import errno
import importlib.machinery
import importlib.util
import inspect
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from airloom import geocode
from airloom.geocode import Place
from airloom.requests import RequestRegistry

try:
    from tests.record_handoff import WRITE_RECORD_SOURCE, nonempty, wait_for_record
except ImportError:  # run from inside tests/ (unittest discover -s tests)
    from record_handoff import WRITE_RECORD_SOURCE, nonempty, wait_for_record


PROJECT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT / "scripts" / "test-ui-regression"

# Every live scenario the integration plan and its reviews require, mapped to
# the script scenario that covers it. The script must list exactly these.
REQUIRED_COVERAGE = {
    "demo/limited-coverage/stale labels": "labels",
    "away-from-home refresh": "away-refresh",
    "save success and failure": "save",
    "delayed, out-of-order, and canceled search": "search",
    "interrupted fly/zoom/pinch": "motion",
    "keyboard focus restoration": "focus",
    "filter persistence": "filters",
    "trend retry": "trend-retry",
    "renderer crash recovery": "shell-recovery",
    "system reduced-motion preference": "reduced-motion",
    "demo/expired/fresh alert behavior": "alerts",
    "filtered cached fallback with provenance": "cache-fallback",
    "popup/search/narrow layouts at all scales and preview actions": "layouts",
    "stripped debug module on a private bus": "stripped-debug",
    "live window resize across the narrow breakpoint": "resize",
    "keyboard map traversal and panning": "keyboard-map",
}
REQUIRED_SCENARIOS = set(REQUIRED_COVERAGE.values())


def load_script():
    loader = importlib.machinery.SourceFileLoader("airloom_ui_regression", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


# A stand-in for ./run: records its environment and answers just enough of
# the debug protocol for DebugApp.start()/wait_ready()/stop().
FAKE_APP = r'''#!/usr/bin/env python3
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
''' + WRITE_RECORD_SOURCE + r'''
write_record(os.environ["AIRLOOM_TEST_ENV_RECORD"], json.dumps({
    "pid": os.getpid(),
    "env": {key: os.environ.get(key) for key in (
        "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "AIRLOOM_DEBUG_SOCKET", "AIRLOOM_NOMINATIM_URL",
        "WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS")},
    "config": json.loads((Path(os.environ["XDG_CONFIG_HOME"]) / "airloom" / "config.json").read_text()),
}))
# A grandchild in the same session, like WebKit's helper processes.
helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
write_record(os.environ["AIRLOOM_TEST_ENV_RECORD"] + ".helper", str(helper.pid))
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(os.environ["AIRLOOM_DEBUG_SOCKET"])
server.listen(5)
while True:
    conn, _ = server.accept()
    data = b""
    while b"\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    message = json.loads(data.split(b"\n", 1)[0])
    cmd = message.get("cmd")
    result = {"pong": True} if cmd == "ping" else {"sensorCount": 3} if cmd == "state" else None
    conn.sendall(json.dumps({"id": message.get("id"), "ok": True, "result": result}).encode() + b"\n")
    conn.close()
    if cmd == "quit" and os.environ.get("AIRLOOM_TEST_IGNORE_QUIT") != "1":
        break
server.close()
'''


class FakeX11:
    """Stands in for libX11 and libXtst behind the script's X11_LIBRARY seam.

    Records the display calls and injected events, and can simulate a server
    without XTEST, an unopenable display, or a rejected fake event.
    """

    DISPLAY = 0x5EED
    KEYCODES = {"Shift_L": 50, "Tab": 23, "x": 53}

    def __init__(self, *, xtest=True, open_display=True, reject=None, reject_keycode=None):
        self.xtest = xtest
        self.open_display = open_display
        self.reject = reject
        self.reject_keycode = reject_keycode
        self.events = []
        self.closed = []

    def load(self, name):
        fake = self

        class Library:
            pass

        library = Library()
        if name == "libX11.so.6":
            library.XOpenDisplay = lambda _name: fake.DISPLAY if fake.open_display else None
            library.XCloseDisplay = lambda display: fake.closed.append(display) or 0
            library.XStringToKeysym = lambda name: 0xFF00 + fake.KEYCODES[name.decode()] if name.decode() in fake.KEYCODES else 0
            library.XKeysymToKeycode = lambda _display, keysym: keysym - 0xFF00
            library.XFlush = lambda _display: fake.events.append(("flush",)) or 1
        elif name == "libXtst.so.6":
            library.XTestQueryExtension = lambda *_args: 1 if fake.xtest else 0
            library.XTestFakeKeyEvent = lambda _d, keycode, press, _delay: fake._event(("key", keycode, press))
            library.XTestFakeMotionEvent = lambda _d, _screen, x, y, _delay: fake._event(("motion", x, y))
            library.XTestFakeButtonEvent = lambda _d, button, press, _delay: fake._event(("button", button, press))
        else:
            raise OSError(f"{name}: not faked")
        return library

    def _event(self, event):
        if event[0] == self.reject and self.reject_keycode in (None, event[1]):
            return 0
        self.events.append(event)
        return 1


def process_gone(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2]
            if state == "Z":
                return True
        except (FileNotFoundError, ProcessLookupError):  # gone, possibly mid-read
            return True
        time.sleep(0.02)
    return False


class UiRegressionScriptContractTest(unittest.TestCase):
    def setUp(self):
        self.script = load_script()
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.root = Path(self._tempdir.name)

    def test_lists_every_required_scenario(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(self.script.main(["--list"]), 0)
        self.assertEqual(set(output.getvalue().split()), REQUIRED_SCENARIOS)

    def test_keyboard_map_scenario_requires_x11_rather_than_skipping(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISPLAY", None)
            with self.assertRaisesRegex(AssertionError, "X11 display with XTEST"):
                self.script.X11Input()

    def test_keyboard_map_scenario_fails_on_a_display_without_xtest_and_closes_it(self):
        fake = FakeX11(xtest=False)
        with mock.patch.dict(os.environ, {"DISPLAY": ":99"}), \
                mock.patch.object(self.script, "X11_LIBRARY", fake.load):
            with self.assertRaisesRegex(AssertionError, r"XTEST.*Xvfb"):
                self.script.X11Input()
        self.assertEqual(fake.closed, [FakeX11.DISPLAY])

    def test_keyboard_map_scenario_fails_when_the_display_cannot_open(self):
        fake = FakeX11(open_display=False)
        with mock.patch.dict(os.environ, {"DISPLAY": ":99"}), \
                mock.patch.object(self.script, "X11_LIBRARY", fake.load):
            with self.assertRaisesRegex(AssertionError, "X11 display with XTEST.*cannot open :99"):
                self.script.X11Input()
        self.assertEqual(fake.closed, [])

    def test_keyboard_map_scenario_fails_when_libxtst_is_missing(self):
        def load(name):
            raise OSError(f"{name}: cannot open shared object file")
        with mock.patch.dict(os.environ, {"DISPLAY": ":99"}), \
                mock.patch.object(self.script, "X11_LIBRARY", load):
            with self.assertRaisesRegex(AssertionError, "X11 display with XTEST"):
                self.script.X11Input()

    def test_x11_input_sends_exactly_the_requested_real_events(self):
        fake = FakeX11()
        with mock.patch.dict(os.environ, {"DISPLAY": ":99"}), \
                mock.patch.object(self.script, "X11_LIBRARY", fake.load):
            x11 = self.script.X11Input()
        x11.key("Tab", shift=True)
        x11.key("x")
        x11.click(10, 20)
        x11.close()
        x11.close()
        shift, tab, x = FakeX11.KEYCODES["Shift_L"], FakeX11.KEYCODES["Tab"], FakeX11.KEYCODES["x"]
        self.assertEqual(fake.events, [
            ("key", shift, 1), ("key", tab, 1), ("key", tab, 0), ("key", shift, 0), ("flush",),
            ("key", x, 1), ("key", x, 0), ("flush",),
            ("motion", 10, 20), ("flush",), ("button", 1, 1), ("button", 1, 0), ("flush",),
        ])
        self.assertEqual(fake.closed, [FakeX11.DISPLAY])

    def test_x11_input_raises_when_the_server_rejects_an_injected_event(self):
        for kind, act in (("key", lambda x11: x11.key("Tab")),
                          ("motion", lambda x11: x11.move(1, 2)),
                          ("button", lambda x11: x11.click(1, 2))):
            with self.subTest(kind=kind):
                fake = FakeX11(reject=kind)
                with mock.patch.dict(os.environ, {"DISPLAY": ":99"}), \
                        mock.patch.object(self.script, "X11_LIBRARY", fake.load):
                    x11 = self.script.X11Input()
                with self.assertRaisesRegex(AssertionError, f"XTEST rejected the fake {kind} event"):
                    act(x11)
                x11.close()

    def test_x11_input_releases_shift_when_a_shifted_key_is_rejected(self):
        fake = FakeX11(reject="key", reject_keycode=FakeX11.KEYCODES["Tab"])
        with mock.patch.dict(os.environ, {"DISPLAY": ":99"}), \
                mock.patch.object(self.script, "X11_LIBRARY", fake.load):
            x11 = self.script.X11Input()
        with self.assertRaisesRegex(AssertionError, "XTEST rejected the fake key event"):
            x11.key("Tab", shift=True)
        x11.close()
        shift = FakeX11.KEYCODES["Shift_L"]
        self.assertEqual(fake.events[-2:], [("key", shift, 0), ("flush",)])

    def test_keyboard_map_scenario_closes_the_display_when_it_fails_midway(self):
        fake = FakeX11()
        stopped = []

        class StubApp:
            sock_path = "unused.sock"

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                return self

            def wait_ready(self):
                pass

            def stop(self):
                stopped.append(True)

        def page_origin(_sock, _x11):
            raise AssertionError("pointer never arrived")

        with mock.patch.dict(os.environ, {"DISPLAY": ":99"}), \
                mock.patch.object(self.script, "X11_LIBRARY", fake.load), \
                mock.patch.object(self.script, "DebugApp", StubApp), \
                mock.patch.object(self.script, "page_origin", page_origin), \
                redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(AssertionError, "pointer never arrived"):
                self.script.scenario_keyboard_map(self.root, mock.Mock(url="http://127.0.0.1:9"))
        self.assertEqual(fake.closed, [FakeX11.DISPLAY])
        self.assertEqual(stopped, [True])

    def test_layouts_cover_the_reported_scaled_widths_at_every_scale(self):
        self.assertEqual(self.script.SCALES, (1.0, 1.25, 1.5))
        widths = {width for width, _height in self.script.LAYOUT_SIZES}
        self.assertTrue({320, 375} <= widths)
        for state in ("search", "popup", "settings", "shell"):
            self.assertIn(state, self.script.LAYOUT_STATES_JS)

    def test_script_never_kills_by_name(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("pkill", "killall", "pgrep"):
            self.assertNotIn(forbidden, source)

    def test_debug_app_is_isolated_demo_only_and_cleans_up_only_its_own_group(self):
        project = self.root / "project"
        (project / "scripts").mkdir(parents=True)
        (project / "scripts" / "debug-run").write_bytes((PROJECT / "scripts" / "debug-run").read_bytes())
        (project / "scripts" / "debug-run").chmod(0o755)
        (project / "run").write_text(FAKE_APP, encoding="utf-8")
        (project / "run").chmod(0o755)
        record = self.root / "child.json"

        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._kill_group, unrelated)

        apps_root = self.root / "apps"
        apps_root.mkdir()
        # CI disables WebKit's sandbox for these scripts; it must reach the app.
        with mock.patch.dict(os.environ, {"WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS": "1"}):
            app = self.script.DebugApp(
                apps_root, "main", "http://127.0.0.1:1",
                extra_env={"AIRLOOM_TEST_ENV_RECORD": str(record)}, project=project,
            )
        app.start()
        try:
            app.wait_ready()
        finally:
            app.stop()

        child = wait_for_record(record, json.loads, what="the fake app's environment record")
        for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "AIRLOOM_DEBUG_SOCKET"):
            self.assertTrue(Path(child["env"][key]).is_relative_to(apps_root / "main"), key)
        self.assertEqual(child["env"]["AIRLOOM_NOMINATIM_URL"], "http://127.0.0.1:1")
        self.assertEqual(child["env"]["WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS"], "1", "the CI sandbox setting did not reach the app")
        self.assertNotIn("api_key", child["config"], "scenarios must run on demo data")
        self.assertEqual(child["config"]["home_mode"], "fixed")
        self.assertTrue(process_gone(child["pid"]), "the debug child survived cleanup")
        helper = wait_for_record(str(record) + ".helper", int, what="the fake app's helper pid")
        self.assertTrue(process_gone(helper), "a helper in the child's session survived cleanup")
        self.assertIsNone(unrelated.poll(), "cleanup touched an unrelated process")

    def test_wait_ready_fails_fast_when_the_child_has_exited(self):
        project = self.root / "project"
        (project / "scripts").mkdir(parents=True)
        (project / "scripts" / "debug-run").write_bytes((PROJECT / "scripts" / "debug-run").read_bytes())
        (project / "scripts" / "debug-run").chmod(0o755)
        (project / "run").write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
        (project / "run").chmod(0o755)
        apps_root = self.root / "apps"
        apps_root.mkdir()
        app = self.script.DebugApp(apps_root, "dies", "http://127.0.0.1:1", project=project)
        app.start()
        try:
            started = time.monotonic()
            with self.assertRaisesRegex(AssertionError, r"exited before opening its debug socket \(status 7\)"):
                app.wait_ready()
            self.assertLess(time.monotonic() - started, 10)
        finally:
            app.stop()

    def test_stop_escalates_when_the_child_ignores_quit(self):
        project = self.root / "project"
        (project / "scripts").mkdir(parents=True)
        (project / "scripts" / "debug-run").write_bytes((PROJECT / "scripts" / "debug-run").read_bytes())
        (project / "scripts" / "debug-run").chmod(0o755)
        (project / "run").write_text(FAKE_APP, encoding="utf-8")
        (project / "run").chmod(0o755)
        record = self.root / "child.json"
        apps_root = self.root / "apps"
        apps_root.mkdir()
        self.script.QUIT_TIMEOUT_S = 0.5
        app = self.script.DebugApp(
            apps_root, "stubborn", "http://127.0.0.1:1", project=project,
            extra_env={"AIRLOOM_TEST_ENV_RECORD": str(record), "AIRLOOM_TEST_IGNORE_QUIT": "1"},
        )
        app.start()
        app.wait_ready()
        app.stop()

        self.assertIsNotNone(app.process.poll())
        self.assertTrue(process_gone(wait_for_record(record, json.loads, what="the fake app's record")["pid"]))

    def test_geocoder_stub_is_loopback_and_records_queries(self):
        with self.script.GeocoderStub() as stub:
            self.assertTrue(stub.url.startswith("http://127.0.0.1:"))
            with urllib.request.urlopen(f"{stub.url}/search?q=fasttown&format=jsonv2", timeout=5) as response:
                rows = json.load(response)
            with urllib.request.urlopen(f"{stub.url}/search?q=unknown", timeout=5) as response:
                unknown = json.load(response)
            with urllib.request.urlopen(f"{stub.url}/reverse?lat=1&lon=2", timeout=5) as response:
                reverse = json.load(response)
        self.assertEqual(rows[0]["display_name"], "Fasttown, Oregon")
        self.assertEqual(unknown, [])
        self.assertIn("address", reverse)
        self.assertTrue(stub.received("fasttown"))
        self.assertFalse(stub.received("queuedhome"))

    def test_stripped_tree_omits_only_the_debug_module(self):
        source = self.root / "source"
        (source / "airloom" / "__pycache__").mkdir(parents=True)
        for name in ("__init__.py", "app.py", "debugport.py"):
            (source / "airloom" / name).write_text("# fixture\n", encoding="utf-8")
        (source / "airloom" / "__pycache__" / "app.cpython.pyc").write_bytes(b"")
        (source / "run").write_text("#!/bin/sh\n", encoding="utf-8")

        stripped = self.script.make_stripped_tree(self.root / "stripped", source)

        self.assertEqual(sorted(path.name for path in (stripped / "airloom").iterdir()),
                         ["__init__.py", "app.py"])
        self.assertTrue((stripped / "run").exists())
        self.assertTrue((PROJECT / "airloom" / "debugport.py").exists(),
                        "the real tree must keep its debug module")

    def _bus_recording_project(self):
        project = self.root / "bus-project"
        project.mkdir()
        (project / "run").write_text(
            "#!/usr/bin/env python3\n"
            "import os, time\n"
            + WRITE_RECORD_SOURCE
            + "write_record(os.environ['AIRLOOM_TEST_ENV_RECORD'], os.environ.get('DBUS_SESSION_BUS_ADDRESS', ''))\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        (project / "run").chmod(0o755)
        return project

    @unittest.skipUnless(shutil.which("dbus-run-session"), "dbus-run-session is not installed")
    def test_private_bus_launch_never_shares_the_callers_session_bus(self):
        record = self.root / "bus.txt"
        apps_root = self.root / "apps"
        apps_root.mkdir()
        app = self.script.DebugApp(
            apps_root, "stripped", "http://127.0.0.1:1", project=self._bus_recording_project(),
            debug_socket=False, private_bus=True,
            extra_env={"AIRLOOM_TEST_ENV_RECORD": str(record),
                       "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/caller-bus"},
        )
        app.start()
        try:
            address = wait_for_record(record, nonempty, what="the child's session bus address")
        finally:
            app.stop()
        self.assertNotEqual(address, "unix:path=/nonexistent/caller-bus")
        self.assertIsNotNone(app.process.poll())

    def test_private_bus_is_required_rather_than_silently_skipped(self):
        apps_root = self.root / "apps"
        apps_root.mkdir()
        app = self.script.DebugApp(apps_root, "stripped", "http://127.0.0.1:1",
                                   project=self._bus_recording_project(),
                                   debug_socket=False, private_bus=True)
        with mock.patch.object(self.script.shutil, "which", return_value=None):
            with self.assertRaisesRegex(AssertionError, "dbus-run-session"):
                app.start()
        self.assertIsNone(app.process)

    def test_stripped_scenario_launches_on_a_private_bus(self):
        launched = {}

        class Recorder:
            def __init__(self, *args, **kwargs):
                launched.update(kwargs)
                raise RuntimeError("recorded")

        with mock.patch.object(self.script, "DebugApp", Recorder), \
                mock.patch.object(self.script, "make_stripped_tree", return_value=self.root):
            with self.assertRaisesRegex(RuntimeError, "recorded"), redirect_stdout(io.StringIO()):
                self.script.scenario_stripped_debug(self.root, mock.Mock(url="http://127.0.0.1:1"))
        self.assertIs(launched.get("private_bus"), True)
        self.assertIs(launched.get("debug_socket"), False)

    def test_save_scenario_fails_rather_than_skips_as_root(self):
        app = mock.Mock(sock_path="sock")
        with mock.patch.object(self.script.os, "geteuid", return_value=0), \
                mock.patch.object(self.script, "eval_js") as eval_js, \
                redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(AssertionError, "regular user"):
                self.script.scenario_save(app)
        eval_js.assert_not_called()
        self.assertNotIn("skip", output.getvalue())

    def test_layout_probe_children_inherit_the_ci_sandbox_setting(self):
        launched = {}

        def record_popen(args, **kwargs):
            launched.update(kwargs)
            raise RuntimeError("recorded")

        with mock.patch.dict(os.environ, {"WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS": "1"}), \
                mock.patch.object(self.script.subprocess, "Popen", record_popen), \
                redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "recorded"):
                self.script.scenario_layouts(self.root)
        self.assertEqual(launched["env"].get("WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS"), "1")

    def _fake_clock(self):
        """Injectable clock for the script's waits: sleeping only advances
        fake time, so the waiting logic is tested without real delays."""
        clock = {"now": 0.0, "sleeps": []}

        def sleep(seconds):
            clock["sleeps"].append(seconds)
            clock["now"] += seconds

        patches = (mock.patch.object(self.script, "CLOCK", lambda: clock["now"]),
                   mock.patch.object(self.script, "SLEEP", sleep))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return clock

    def _fake_page(self, marker_polls, popup_opens_after=0, popup_sensor=None, settled_after=0):
        """A scripted page for the resize helpers: each JS probe is answered by
        identity, so the tests pin which conditions the helpers wait on.
        popup_opens_after: popup-state polls before the popup shows (None =
        never); popup_sensor: the id the open popup reports (default: the
        clicked marker's)."""
        page = {"marker_polls": list(marker_polls), "clicked": [], "probes": 0, "popup_polls": 0}
        script = self.script

        def eval_js(_sock, js):
            if js == script.FIND_POPUP_MARKER_JS:
                return page["marker_polls"].pop(0) if page["marker_polls"] else None
            if js.startswith(script.CLICK_MARKER_JS_PREFIX):
                page["clicked"].append(js.split('"')[1])
                return "clicked"
            if js == script.POPUP_OPEN_JS:
                page["popup_polls"] += 1
                if not page["clicked"] or popup_opens_after is None or page["popup_polls"] <= popup_opens_after:
                    return None
                return popup_sensor or page["clicked"][-1]
            if js == script.POPUP_DIAGNOSTICS_JS:
                return {"markers": [], "map": [0, 0, 313, 469], "viewport": [313, 469], "popupHidden": False,
                        "popupId": popup_sensor, "selectedId": 900000, "loading": False}
            if js == script.RESIZE_PROBE_JS:
                page["probes"] += 1
                settled = settled_after is not None and page["probes"] > settled_after
                return {"width": 313, "layoutSettled": settled, "viewport": [313, 469], "panel": [313, 425],
                        "markerLayer": "translate(-167032.04px, -93104.5px)" if settled else "translate(-166576.04px, -93104.5px)",
                        "expectedLayer": [-167032.04, -93104.5]}
            raise AssertionError(f"unexpected probe: {js[:80]}")

        return page, eval_js

    def test_popup_helper_waits_for_a_qualifying_marker_then_for_the_popup(self):
        # The first probes see no marker inside the safe area yet (layout still
        # catching up after a resize); the helper must keep waiting.
        self._fake_clock()
        page, eval_js = self._fake_page([None, None, "900004"])
        with mock.patch.object(self.script, "eval_js", eval_js), redirect_stdout(io.StringIO()):
            self.assertEqual(self.script.open_visible_popup("sock", timeout_s=5), "900004")
        self.assertEqual(page["clicked"], ["900004"])

    def test_popup_helper_keeps_polling_until_a_delayed_popup_opens(self):
        clock = self._fake_clock()
        page, eval_js = self._fake_page(["900004"], popup_opens_after=4)
        with mock.patch.object(self.script, "eval_js", eval_js), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.script.open_visible_popup("sock", timeout_s=10), "900004")
        self.assertEqual(page["popup_polls"], 5)
        self.assertEqual(page["clicked"], ["900004"])
        self.assertLess(clock["now"], 10)
        self.assertNotIn("no marker popup", output.getvalue())

    def test_popup_helper_does_not_accept_a_popup_for_another_sensor(self):
        clock = self._fake_clock()
        page, eval_js = self._fake_page(["900004"], popup_sensor="900001")
        output = io.StringIO()
        with mock.patch.object(self.script, "eval_js", eval_js), redirect_stdout(output):
            self.assertIsNone(self.script.open_visible_popup("sock", timeout_s=10))
        # It kept polling for the clicked marker's popup until the deadline.
        self.assertGreaterEqual(clock["now"], 10)
        self.assertGreater(page["popup_polls"], 10)
        self.assertIn("marker 900004's popup to open", output.getvalue())
        self.assertIn('"popupId": "900001"', output.getvalue())

    def test_popup_helper_times_out_with_diagnostics_when_no_marker_qualifies(self):
        self._fake_clock()
        _page, eval_js = self._fake_page([])
        output = io.StringIO()
        with mock.patch.object(self.script, "eval_js", eval_js), redirect_stdout(output):
            self.assertIsNone(self.script.open_visible_popup("sock", timeout_s=10))
        self.assertIn("no marker popup", output.getvalue())
        for field in ('"markers"', '"map"', '"viewport"', '"selectedId"', '"loading"'):
            self.assertIn(field, output.getvalue())

    def test_popup_helper_requires_the_clicked_markers_popup_to_open(self):
        self._fake_clock()
        page, eval_js = self._fake_page(["900004"], popup_opens_after=None)
        output = io.StringIO()
        with mock.patch.object(self.script, "eval_js", eval_js), redirect_stdout(output):
            self.assertIsNone(self.script.open_visible_popup("sock", timeout_s=10))
        self.assertEqual(page["clicked"], ["900004"])
        self.assertIn("no marker popup", output.getvalue())

    def test_width_wait_returns_only_after_the_map_layout_matches_the_viewport(self):
        clock = self._fake_clock()
        page, eval_js = self._fake_page([], settled_after=3)
        with mock.patch.object(self.script, "eval_js", eval_js):
            probe = self.script.wait_for_width("sock", 480, 1.5, 10)
        self.assertTrue(probe["layoutSettled"])
        self.assertEqual(page["probes"], 4)
        # Polling interval only: no fixed settle delay.
        self.assertTrue(all(seconds <= 0.2 for seconds in clock["sleeps"]), clock["sleeps"])

    def test_width_wait_timeout_reports_the_last_probe(self):
        clock = self._fake_clock()
        _page, eval_js = self._fake_page([], settled_after=None)
        with mock.patch.object(self.script, "eval_js", eval_js):
            with self.assertRaises(AssertionError) as raised:
                self.script.wait_for_width("sock", 480, 1.5, 10)
        message = str(raised.exception)
        self.assertGreaterEqual(clock["now"], 15)
        self.assertIn("a 480px window (313 CSS px) with the map laid out for it", message)
        for detail in ('"viewport": [313, 469]', '"panel": [313, 425]',
                       '"markerLayer": "translate(-166576.04px, -93104.5px)"', '"expectedLayer": [-167032.04, -93104.5]'):
            self.assertIn(detail, message)

    def test_proc_reads_treat_a_vanished_process_as_empty(self):
        # Descendants can exit between being listed and being read; that is
        # "not a match", not a crash of the scenario.
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        self.assertEqual(self.script.proc_bytes(gone.pid, "cmdline"), b"")
        self.assertIn(b"python", self.script.proc_bytes(os.getpid(), "cmdline").lower())

    def test_proc_reads_treat_a_process_reaped_mid_read_as_gone(self):
        reaped = ProcessLookupError(errno.ESRCH, "No such process")
        with mock.patch.object(self.script.Path, "read_bytes", side_effect=reaped):
            self.assertEqual(self.script.proc_bytes(12345, "cmdline"), b"")

    def test_proc_read_errors_other_than_a_vanished_process_propagate(self):
        # A live owned descendant that cannot be read must not silently
        # drop out of ownership selection.
        for error in (PermissionError(errno.EACCES, "Permission denied"), OSError(errno.EIO, "I/O error")):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(self.script.Path, "read_bytes", side_effect=error):
                with self.assertRaises(type(error)) as raised:
                    self.script.proc_bytes(12345, "cmdline")
                self.assertEqual(raised.exception.errno, error.errno)

    def test_viewport_width_excludes_the_window_frame_before_scaling(self):
        # Under a compositor (mutter) the webview spans the whole window; under
        # Xvfb GTK draws a solid CSD frame inside the requested size.
        self.assertEqual(self.script.viewport_width(1240), 1240)
        self.assertEqual(self.script.viewport_width(1240, 1.5), 826)
        self.assertEqual(self.script.viewport_width(1240, frame=10), 1230)
        self.assertEqual(self.script.viewport_width(480, 1.5, 10), 313)

    def test_window_frame_is_measured_from_the_default_window_and_bounded(self):
        def probe_once(probe, _timeout, what):
            value = probe()
            if value is None or value is False:
                raise AssertionError(f"timed out waiting for {what}")
            return value

        for inner, frame in ((1240, 0), (1230, 10)):
            with self.subTest(inner=inner), \
                    mock.patch.object(self.script, "wait_for", probe_once), \
                    mock.patch.object(self.script, "eval_js", return_value={"width": inner}):
                self.assertEqual(self.script.measure_window_frame("sock"), frame)
        for inner in (1241, 1240 - self.script.WINDOW_FRAME_MAX_PX - 1, 360):
            with self.subTest(inner=inner), \
                    mock.patch.object(self.script, "wait_for", probe_once), \
                    mock.patch.object(self.script, "eval_js", return_value={"width": inner}):
                with self.assertRaises(AssertionError):
                    self.script.measure_window_frame("sock")

    def test_trend_terminal_state_rejects_loading_and_pending_retry(self):
        terminal = self.script.terminal_trend
        self.assertIsNone(terminal({"retryHidden": True, "retryDisabled": False,
                                    "chartText": "Loading trend…", "rows": []}))
        self.assertIsNone(terminal({"retryHidden": False, "retryDisabled": False,
                                    "chartText": "Trend unavailable", "rows": []}))
        loaded = {"retryHidden": True, "retryDisabled": False, "chartText": "", "rows": [["Now", 21]]}
        self.assertIs(terminal(loaded), loaded)

    def test_trend_retry_uses_a_real_fixture_failure(self):
        self.assertIn("trend-retry", self.script.STANDALONE_SCENARIOS)
        self.assertNotIn("trend-retry", self.script.APP_SCENARIOS)
        self.assertNotIn("1000000", inspect.getsource(self.script.scenario_trend_retry))

    def test_caller_snapshot_detects_any_change(self):
        target = self.root / "airloom"
        target.mkdir()
        (target / "config.json").write_bytes(b"real")
        before = self.script.tree_snapshot(target)
        (target / "config.json").write_bytes(b"changed")
        self.assertNotEqual(before, self.script.tree_snapshot(target))
        self.assertEqual(self.script.tree_snapshot(self.root / "missing"), {})

    def _kill_group(self, process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


class RequestReplacementTest(unittest.TestCase):
    def test_replacement_after_invalidation_is_newer_and_isolated_from_the_old_token(self):
        registry = RequestRegistry()
        old = registry.begin("place-search:home")
        registry.invalidate("place-search:home")

        replacement = registry.begin("place-search:home")

        self.assertGreater(replacement.request_id, old.request_id)
        self.assertFalse(registry.is_current(old))
        self.assertTrue(registry.is_current(replacement))
        registry.finish(old)  # a stale completion arriving late
        self.assertTrue(registry.is_current(replacement))
        self.assertFalse(registry.is_current(old))


class SignalingLock:
    """Wrap a lock and report how many threads have tried to enter it."""

    def __init__(self, lock):
        self._lock = lock
        self._count = 0
        self._guard = threading.Lock()
        self.second_waiting = threading.Event()

    def __enter__(self):
        with self._guard:
            self._count += 1
            if self._count == 2:
                self.second_waiting.set()
        return self._lock.__enter__()

    def __exit__(self, *exc):
        return self._lock.__exit__(*exc)


def fake_response(payload):
    return mock.MagicMock(
        __enter__=lambda _self: io.StringIO(json.dumps(payload)),
        __exit__=lambda _self, *_args: False,
    )


class GeocoderReadinessTest(unittest.TestCase):
    """Release the first search only once the second is really waiting."""

    def _client(self):
        client = geocode.Geocoder(clock=lambda: 0.0, sleep=lambda _seconds: None)
        lock = SignalingLock(client._search_lock)
        client._search_lock = lock
        return client, lock

    def _run_pair(self, client, lock, urlopen, query):
        first_started = threading.Event()
        release_first = threading.Event()
        outcomes = {"first": [], "second": []}

        def controlled(request, timeout=None):
            return urlopen(request, first_started, release_first)

        def run(name):
            try:
                outcomes[name].append(client.search(query))
            except Exception as error:  # noqa: BLE001 - recorded for assertions
                outcomes[name].append(error)

        first = threading.Thread(target=run, args=("first",))
        second = threading.Thread(target=run, args=("second",))
        with mock.patch.object(geocode.urllib.request, "urlopen", side_effect=controlled):
            first.start()
            self.assertTrue(first_started.wait(2), "first search never reached the network")
            second.start()
            self.assertTrue(lock.second_waiting.wait(2), "second search never reached the in-flight lock")
            release_first.set()
            first.join(5)
            second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        return outcomes

    def test_waiting_identical_search_reuses_the_completed_result(self):
        client, lock = self._client()
        calls = []

        def urlopen(request, first_started, release_first):
            calls.append(request.full_url)
            first_started.set()
            self.assertTrue(release_first.wait(5))
            return fake_response([{"lat": "1", "lon": "2", "display_name": "Shared place"}])

        outcomes = self._run_pair(client, lock, urlopen, "shared readiness")

        expected = [Place("Shared place", 1.0, 2.0)]
        self.assertEqual(outcomes, {"first": [expected], "second": [expected]})
        self.assertEqual(len(calls), 1)

    def test_waiting_search_retries_after_the_leader_fails(self):
        client, lock = self._client()
        calls = []

        def urlopen(request, first_started, release_first):
            calls.append(request.full_url)
            if len(calls) == 1:
                first_started.set()
                self.assertTrue(release_first.wait(5))
                raise OSError("leader failed")
            return fake_response([{"lat": "1", "lon": "2", "display_name": "Recovered place"}])

        outcomes = self._run_pair(client, lock, urlopen, "retry readiness")

        self.assertIsInstance(outcomes["first"][0], geocode.GeocodeError)
        self.assertEqual(outcomes["second"], [[Place("Recovered place", 1.0, 2.0)]])
        self.assertEqual(len(calls), 2)


class CachedFallbackAlertIntegrationTest(unittest.TestCase):
    """Fixture PurpleAir through the real fetch, cache, filter, publication
    and alert path: demo and expired observations never alert, a fresh
    favorite alerts once, and stale cached fallback keeps filtering and
    provenance without re-alerting."""

    def setUp(self):
        from airloom import app as app_module
        from airloom.cache import SensorCache
        from airloom.debugport import FakePurpleAirClient
        from airloom.store import Store
        from tests.test_app_state import ApplicationAdapterTestCase

        self.app_module = app_module
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        root = Path(self._tempdir.name)
        self.fixture = root / "purpleair.json"
        self.store = Store(root / "config.json")
        self.store.data.update({
            "api_key": "fake-key", "latitude": 45.0, "longitude": -122.0,
            "favorites": [1, 2, 3, 900000], "alert_threshold": 101,
            "alert_states": {"900000": False}, "location_filter": "outdoor",
        })
        self.store.save()
        self.cache = SensorCache(root / "cache.db")
        self.addCleanup(self.cache.close)
        original = app_module._purpleair_client_factory
        self.addCleanup(setattr, app_module, "_purpleair_client_factory", original)
        app_module._purpleair_client_factory = lambda _key: FakePurpleAirClient(str(self.fixture))
        helper = ApplicationAdapterTestCase()
        self.app = helper.make_app(store=self.store, cache=self.cache)
        self.app._captured_notifications = []

    def write_fixture(self, *, fail=False):
        def row(sensor_id, location_type, age_s, pm):
            return {"sensor_index": sensor_id, "name": f"Fixture {sensor_id}", "latitude": 45.001,
                    "longitude": -122.001, "location_type": location_type, "age_s": age_s,
                    "humidity": 50, "temperature": 70, "pm1.0": 5, "pm2.5_cf_1": pm,
                    "pm10.0": 9, "confidence": 100}
        rows = [row(1, 0, 30, 150),         # fresh outdoor favorite above threshold
                row(2, 0, 3 * 3600, 150),   # expired outdoor favorite above threshold
                row(3, 1, 30, 150),         # fresh indoor favorite above threshold
                row(4, 0, 30, 5)]           # ordinary clean outdoor sensor
        self.fixture.write_text(json.dumps({"fail": fail, "rows": rows}), encoding="utf-8")

    def set_filter(self, value):
        candidate = dict(self.store.data, location_filter=value)
        self.store.apply(candidate)
        self.app.controller.set_config(self.store.data)

    def refresh(self):
        request = self.app.controller.begin_refresh(force=True)
        outcome = self.app_module._fetch_readings(
            dict(self.store.data), self.cache, request,
            self.app_module.cap_bounds(request.bounds, request.center), 120.0)
        self.app._finish_refresh(outcome)
        return [p for e, p in self.app.sent if e == "sensors"][-1]

    def alerted(self):
        return [item["sensor_id"] for item in self.app._captured_notifications]

    def persisted_states(self):
        return json.loads((Path(self._tempdir.name) / "config.json").read_text())["alert_states"]

    def test_fixture_fetch_cache_fallback_filters_and_alerts(self):
        # Outage with an empty cache: demo fallback must neither alert nor
        # touch alert state, even for demo favorites above the threshold.
        self.write_fixture(fail=True)
        demo = self.refresh()
        self.assertEqual(demo["source"], "Demo data")
        self.assertTrue(any(s["aqi"] >= 101 for s in demo["items"]))
        self.assertEqual(self.alerted(), [])
        self.assertEqual(self.persisted_states(), {"900000": False})

        # Live: only the fresh favorite alerts; the expired one does not.
        self.write_fixture()
        live = self.refresh()
        self.assertEqual(live["source"], "PurpleAir live")
        self.assertEqual(sorted(s["id"] for s in live["items"]), [1, 2, 4])
        self.assertEqual(self.alerted(), [1])
        self.set_filter("both")
        self.refresh()
        self.assertEqual(self.alerted(), [1, 3])
        self.assertEqual(self.persisted_states(), {"900000": False, "1": True, "3": True})

        # Outage after a populated cache: stale cached readings, still
        # filtered per the preference, labeled stale, and no repeat alerts.
        self.write_fixture(fail=True)
        self.set_filter("outdoor")
        stale = self.refresh()
        self.assertEqual(stale["source"], "PurpleAir · cached")
        self.assertIs(stale["stale"], True)
        self.assertEqual(sorted(s["id"] for s in stale["items"]), [1, 2, 4])
        self.assertTrue(all(s["delivery"] == "cache" and s["origin"] == "live" for s in stale["items"]))
        self.set_filter("indoor")
        indoor = self.refresh()
        self.assertEqual([s["id"] for s in indoor["items"]], [3])
        self.assertIs(indoor["stale"], True)
        self.assertEqual(self.alerted(), [1, 3])
        self.assertEqual(self.persisted_states(), {"900000": False, "1": True, "3": True})
        errors = [p["message"] for e, p in self.app.sent if e == "error"]
        self.assertTrue(any("Showing cached readings" in message for message in errors))


if __name__ == "__main__":
    unittest.main()
