"""Display-free coverage for scripts/test-ui-regression and cross-layer
request-ordering guarantees it depends on.

The GUI scenarios themselves need GTK, WebKit and a display; these tests pin
the script's isolation contract (private config/cache, loopback geocoder,
child-only cleanup, stripped release tree) so `make check` catches
regressions without launching Airloom.
"""

import importlib.machinery
import importlib.util
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

Path(os.environ["AIRLOOM_TEST_ENV_RECORD"]).write_text(json.dumps({
    "pid": os.getpid(),
    "env": {key: os.environ.get(key) for key in (
        "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "AIRLOOM_DEBUG_SOCKET", "AIRLOOM_NOMINATIM_URL")},
    "config": json.loads((Path(os.environ["XDG_CONFIG_HOME"]) / "airloom" / "config.json").read_text()),
}), encoding="utf-8")
# A grandchild in the same session, like WebKit's helper processes.
helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path(os.environ["AIRLOOM_TEST_ENV_RECORD"] + ".helper").write_text(str(helper.pid))
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


def process_gone(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2]
            if state == "Z":
                return True
        except FileNotFoundError:
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
        app = self.script.DebugApp(
            apps_root, "main", "http://127.0.0.1:1",
            extra_env={"AIRLOOM_TEST_ENV_RECORD": str(record)}, project=project,
        )
        app.start()
        try:
            app.wait_ready()
        finally:
            app.stop()

        child = json.loads(record.read_text(encoding="utf-8"))
        for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "AIRLOOM_DEBUG_SOCKET"):
            self.assertTrue(Path(child["env"][key]).is_relative_to(apps_root / "main"), key)
        self.assertEqual(child["env"]["AIRLOOM_NOMINATIM_URL"], "http://127.0.0.1:1")
        self.assertNotIn("api_key", child["config"], "scenarios must run on demo data")
        self.assertEqual(child["config"]["home_mode"], "fixed")
        self.assertTrue(process_gone(child["pid"]), "the debug child survived cleanup")
        helper = int(Path(str(record) + ".helper").read_text())
        self.assertTrue(process_gone(helper), "a helper in the child's session survived cleanup")
        self.assertIsNone(unrelated.poll(), "cleanup touched an unrelated process")

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
        self.assertTrue(process_gone(json.loads(record.read_text())["pid"]))

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
            "from pathlib import Path\n"
            "Path(os.environ['AIRLOOM_TEST_ENV_RECORD']).write_text(os.environ.get('DBUS_SESSION_BUS_ADDRESS', ''))\n"
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
            deadline = time.monotonic() + 10
            while not (record.exists() and record.read_text()) and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            app.stop()
        address = record.read_text()
        self.assertTrue(address, "the child had no session bus")
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
