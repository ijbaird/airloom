import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from airloom.debugport import DebugPort, FakePurpleAirClient, debug_purpleair_factory, validate_command
from airloom.geography import Bounds
from airloom.purpleair import PurpleAirError


def _echo_dispatcher(message: dict, reply) -> None:
    """Fake dispatcher standing in for app.py's real command handlers.

    Answers "ping" synchronously and reports everything else as an
    unknown command, which is enough to exercise the framing layer
    (id echo, ok/error shape, malformed-input handling) without any
    GTK/WebKit dependency.
    """
    if message.get("cmd") == "ping":
        reply({"ok": True, "result": {"pong": True}})
    else:
        reply({"ok": False, "error": f"unknown cmd: {message.get('cmd')!r}"})


class _ClientSession:
    """Thin newline-JSON client over a connected unix socket, for tests."""

    def __init__(self, path: str):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(path)
        self._buf = b""

    def send(self, raw: bytes) -> None:
        self.sock.sendall(raw if raw.endswith(b"\n") else raw + b"\n")

    def request(self, obj: dict) -> dict:
        self.send(json.dumps(obj).encode("utf-8"))
        return self.read_response()

    def read_response(self) -> dict:
        while b"\n" not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed before a full response arrived")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))

    def close(self) -> None:
        self.sock.close()


class DebugPortTest(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.path = str(Path(self._tempdir.name) / "airloom-debug.sock")
        self.port = DebugPort(self.path, _echo_dispatcher, reply_timeout=2.0)
        self.port.start()
        self.addCleanup(self.port.stop)

    def _client(self) -> _ClientSession:
        client = _ClientSession(self.path)
        self.addCleanup(client.close)
        return client

    def test_ping_round_trip(self):
        client = self._client()
        response = client.request({"id": 1, "cmd": "ping"})
        self.assertEqual(response, {"id": 1, "ok": True, "result": {"pong": True}})

    def test_unknown_cmd_error(self):
        client = self._client()
        response = client.request({"id": 2, "cmd": "not-a-real-command"})
        self.assertEqual(response["id"], 2)
        self.assertFalse(response["ok"])
        self.assertIn("unknown cmd", response["error"])

    def test_malformed_json_error(self):
        client = self._client()
        client.send(b"{not valid json")
        response = client.read_response()
        self.assertFalse(response["ok"])
        self.assertIn("malformed", response["error"])
        self.assertIsNone(response["id"])
        # The connection must survive a malformed line: a well-formed
        # request afterward still gets answered normally.
        response = client.request({"id": 3, "cmd": "ping"})
        self.assertEqual(response, {"id": 3, "ok": True, "result": {"pong": True}})

    def test_non_object_json_error(self):
        client = self._client()
        response = client.request(["not", "an", "object"])
        self.assertFalse(response["ok"])
        self.assertIn("malformed", response["error"])

    def test_request_id_echo(self):
        client = self._client()
        for request_id in (0, 1, 999, -5):
            response = client.request({"id": request_id, "cmd": "ping"})
            self.assertEqual(response["id"], request_id)

    def test_socket_created_with_owner_only_permissions(self):
        mode = os.stat(self.path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_stale_socket_file_is_replaced(self):
        # Simulate a leftover socket at the path (e.g. from a prior crash)
        # from *before* start() is called on a fresh DebugPort instance.
        stale_path = str(Path(self._tempdir.name) / "stale.sock")
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(stale_path)
        stale.close()
        port = DebugPort(stale_path, _echo_dispatcher)
        port.start()
        try:
            mode = os.stat(stale_path).st_mode & 0o777
            self.assertEqual(mode, 0o600)
            client = _ClientSession(stale_path)
            try:
                self.assertEqual(
                    client.request({"id": 1, "cmd": "ping"}),
                    {"id": 1, "ok": True, "result": {"pong": True}},
                )
            finally:
                client.close()
        finally:
            port.stop()

    def test_dispatcher_reply_timeout_becomes_error(self):
        def never_replies(_message, _reply):
            pass  # simulates a dead main loop that never calls reply()

        port = DebugPort(str(Path(self._tempdir.name) / "hang.sock"), never_replies, reply_timeout=0.2)
        port.start()
        try:
            client = _ClientSession(port._path)
            try:
                response = client.request({"id": 1, "cmd": "ping"})
                self.assertFalse(response["ok"])
                self.assertIn("timed out", response["error"])
            finally:
                client.close()
        finally:
            port.stop()

    def test_dispatcher_exception_becomes_error_not_crash(self):
        def raises(_message, _reply):
            raise RuntimeError("boom")

        port = DebugPort(str(Path(self._tempdir.name) / "boom.sock"), raises)
        port.start()
        try:
            client = _ClientSession(port._path)
            try:
                response = client.request({"id": 1, "cmd": "ping"})
                self.assertFalse(response["ok"])
                self.assertIn("boom", response["error"])
            finally:
                client.close()
        finally:
            port.stop()

    def test_multiple_requests_over_one_connection(self):
        client = self._client()
        for request_id in range(5):
            response = client.request({"id": request_id, "cmd": "ping"})
            self.assertEqual(response["id"], request_id)
            self.assertTrue(response["ok"])


class DebugPortSafetyTest(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.path = str(Path(self._tempdir.name) / "airloom-debug.sock")

    def test_regular_file_is_preserved(self):
        Path(self.path).write_bytes(b"do not delete")
        port = DebugPort(self.path, _echo_dispatcher)
        self.addCleanup(port.stop)

        with self.assertRaises(OSError):
            port.start()

        self.assertEqual(Path(self.path).read_bytes(), b"do not delete")

    def test_live_socket_is_refused_and_preserved(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(self.path)
        listener.listen(1)
        original = os.lstat(self.path)
        port = DebugPort(self.path, _echo_dispatcher)
        self.addCleanup(port.stop)

        with self.assertRaises(OSError):
            port.start()

        current = os.lstat(self.path)
        self.assertEqual((current.st_dev, current.st_ino), (original.st_dev, original.st_ino))

    def test_live_socket_with_full_backlog_is_refused_without_hanging(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(self.path)
        listener.listen(1)
        original = os.lstat(self.path)

        # Fill the listener queue without accepting. On Linux, the next
        # non-blocking connect fails with EAGAIN while a blocking connect
        # waits for queue space.
        for _attempt in range(32):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.setblocking(False)
            try:
                client.connect(self.path)
            except BlockingIOError:
                client.close()
                break
            self.addCleanup(client.close)
        else:
            self.fail("could not saturate the disposable listener backlog")

        port = DebugPort(self.path, _echo_dispatcher)
        self.addCleanup(port.stop)
        outcome = []

        def start_port():
            try:
                port.start()
            except OSError as exc:
                outcome.append(exc)

        starter = threading.Thread(target=start_port, daemon=True)
        starter.start()
        starter.join(timeout=2)
        if starter.is_alive():
            # Release the blocked pre-fix probe before failing so the test
            # leaves no running startup thread behind.
            accepted, _ = listener.accept()
            accepted.close()
            starter.join(timeout=2)
            self.fail("start() blocked on a live listener with a full connection backlog")

        self.assertEqual(len(outcome), 1, "start() did not refuse the congested live listener")
        self.assertIsInstance(outcome[0], OSError)
        current = os.lstat(self.path)
        self.assertEqual((current.st_dev, current.st_ino), (original.st_dev, original.st_ino))

    def test_stale_socket_is_replaced(self):
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(self.path)
        stale.close()
        original = os.lstat(self.path)
        port = DebugPort(self.path, _echo_dispatcher)
        self.addCleanup(port.stop)

        port.start()

        current = os.lstat(self.path)
        self.assertNotEqual((current.st_dev, current.st_ino), (original.st_dev, original.st_ino))
        client = _ClientSession(self.path)
        self.addCleanup(client.close)
        self.assertTrue(client.request({"id": 1, "cmd": "ping"})["ok"])

    def test_stop_preserves_a_replacement_socket(self):
        port = DebugPort(self.path, _echo_dispatcher)
        port.start()
        os.unlink(self.path)
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(replacement.close)
        replacement.bind(self.path)
        replacement.listen(1)
        replacement_identity = os.lstat(self.path)

        port.stop()

        self.assertTrue(os.path.lexists(self.path), "stop() removed a socket it did not create")
        current = os.lstat(self.path)
        self.assertEqual(
            (current.st_dev, current.st_ino),
            (replacement_identity.st_dev, replacement_identity.st_ino),
        )


class ValidateCommandTest(unittest.TestCase):
    """GTK-free coverage of every command's payload validation — see the
    table in airloom/debugport.py. app.py's dispatcher trusts these results
    completely (including for unknown `cmd`s), so this is the load-bearing
    test for "malformed input never reaches a GTK/WebKit call".
    """

    # -- No-parameter commands --------------------------------------------

    def test_no_param_commands_normalize_to_empty_dict(self):
        for cmd in ("ping", "version", "state", "quit", "notifications"):
            with self.subTest(cmd=cmd):
                normalized, error = validate_command({"id": 1, "cmd": cmd})
                self.assertIsNone(error)
                self.assertEqual(normalized, {})

    def test_no_param_commands_ignore_extra_fields(self):
        normalized, error = validate_command({"id": 1, "cmd": "ping", "junk": "ignored"})
        self.assertIsNone(error)
        self.assertEqual(normalized, {})

    # -- Unknown / malformed cmd --------------------------------------------

    def test_unknown_cmd_is_an_error(self):
        normalized, error = validate_command({"id": 1, "cmd": "not-a-real-command"})
        self.assertIsNone(normalized)
        self.assertIn("unknown cmd", error)

    def test_missing_cmd_is_an_error(self):
        normalized, error = validate_command({"id": 1})
        self.assertIsNone(normalized)
        self.assertIn("unknown cmd", error)

    def test_non_string_cmd_is_an_error_not_an_exception(self):
        for cmd in (None, 17, ["ping"], {"name": "ping"}):
            with self.subTest(cmd=cmd):
                try:
                    normalized, error = validate_command({"id": 1, "cmd": cmd})
                except TypeError as exc:
                    self.fail(f"non-string cmd raised TypeError instead of returning an error: {exc}")
                self.assertIsNone(normalized)
                self.assertIn("unknown cmd", error)

    def test_eval_and_pinch_are_not_in_the_table(self):
        # app.py routes these to their own (pre-existing) validation before
        # ever calling validate_command(); this table must not shadow that.
        for cmd in ("eval", "pinch"):
            with self.subTest(cmd=cmd):
                normalized, error = validate_command({"id": 1, "cmd": cmd})
                self.assertIsNone(normalized)
                self.assertIn("unknown cmd", error)

    # -- tap -----------------------------------------------------------------

    def test_tap_valid(self):
        normalized, error = validate_command({"cmd": "tap", "x": 12, "y": 34.5})
        self.assertIsNone(error)
        self.assertEqual(normalized, {"x": 12.0, "y": 34.5})

    def test_tap_accepts_numeric_strings(self):
        # float() coerces "12" the same way JSON would if a client sent
        # numbers as strings; no reason to reject that.
        normalized, error = validate_command({"cmd": "tap", "x": "12", "y": "34"})
        self.assertIsNone(error)
        self.assertEqual(normalized, {"x": 12.0, "y": 34.0})

    def test_tap_missing_field(self):
        normalized, error = validate_command({"cmd": "tap", "x": 1})
        self.assertIsNone(normalized)
        self.assertIn("'x' and 'y'", error)

    def test_tap_non_numeric_field(self):
        normalized, error = validate_command({"cmd": "tap", "x": "abc", "y": 1})
        self.assertIsNone(normalized)
        self.assertIn("'x' and 'y'", error)

    def test_tap_rejects_non_finite(self):
        normalized, error = validate_command({"cmd": "tap", "x": float("nan"), "y": 1})
        self.assertIsNone(normalized)
        self.assertIn("finite", error)

    # -- search ----------------------------------------------------------

    def test_search_valid(self):
        normalized, error = validate_command({"cmd": "search", "query": "park"})
        self.assertIsNone(error)
        self.assertEqual(normalized, {"query": "park"})

    def test_search_allows_empty_string(self):
        # An empty query is how a client clears the search box; must not be
        # rejected as "missing".
        normalized, error = validate_command({"cmd": "search", "query": ""})
        self.assertIsNone(error)
        self.assertEqual(normalized, {"query": ""})

    def test_search_requires_string(self):
        normalized, error = validate_command({"cmd": "search", "query": 5})
        self.assertIsNone(normalized)
        self.assertIn("string 'query'", error)

    def test_search_missing_query(self):
        normalized, error = validate_command({"cmd": "search"})
        self.assertIsNone(normalized)
        self.assertIn("string 'query'", error)

    # -- key -------------------------------------------------------------

    def test_key_valid(self):
        normalized, error = validate_command({"cmd": "key", "key": "Escape"})
        self.assertIsNone(error)
        self.assertEqual(normalized, {"key": "Escape"})

    def test_key_rejects_empty_string(self):
        normalized, error = validate_command({"cmd": "key", "key": ""})
        self.assertIsNone(normalized)
        self.assertIn("non-empty", error)

    def test_key_requires_string(self):
        normalized, error = validate_command({"cmd": "key", "key": 27})
        self.assertIsNone(normalized)
        self.assertIn("non-empty", error)

    # -- screenshot --------------------------------------------------------

    def test_screenshot_no_path(self):
        normalized, error = validate_command({"cmd": "screenshot"})
        self.assertIsNone(error)
        self.assertEqual(normalized, {"path": None})

    def test_screenshot_absolute_path(self):
        normalized, error = validate_command({"cmd": "screenshot", "path": "/tmp/shot.png"})
        self.assertIsNone(error)
        self.assertEqual(normalized, {"path": "/tmp/shot.png"})

    def test_screenshot_relative_path_rejected(self):
        normalized, error = validate_command({"cmd": "screenshot", "path": "shot.png"})
        self.assertIsNone(normalized)
        self.assertIn("absolute", error)

    def test_screenshot_non_string_path_rejected(self):
        normalized, error = validate_command({"cmd": "screenshot", "path": 5})
        self.assertIsNone(normalized)
        self.assertIn("non-empty string", error)

    def test_screenshot_empty_path_rejected(self):
        normalized, error = validate_command({"cmd": "screenshot", "path": ""})
        self.assertIsNone(normalized)
        self.assertIn("non-empty string", error)


class ProtocolRegressionTest(unittest.TestCase):
    """Extra protocol-layer regressions beyond DebugPortTest's coverage,
    exercised against the real (validate_command-aware) shape of requests
    the new commands will actually send.
    """

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.path = str(Path(self._tempdir.name) / "airloom-debug.sock")

    def _dispatcher(self, message: dict, reply) -> None:
        normalized, error = validate_command(message)
        if error is not None:
            reply({"ok": False, "error": error})
            return
        reply({"ok": True, "result": normalized})

    def _start(self) -> DebugPort:
        port = DebugPort(self.path, self._dispatcher, reply_timeout=2.0)
        port.start()
        self.addCleanup(port.stop)
        return port

    def _client(self) -> _ClientSession:
        client = _ClientSession(self.path)
        self.addCleanup(client.close)
        return client

    def test_tap_round_trip_through_the_wire(self):
        self._start()
        client = self._client()
        response = client.request({"id": 1, "cmd": "tap", "x": 10, "y": 20})
        self.assertEqual(response, {"id": 1, "ok": True, "result": {"x": 10.0, "y": 20.0}})

    def test_invalid_tap_round_trip_reports_error_not_crash(self):
        self._start()
        client = self._client()
        response = client.request({"id": 1, "cmd": "tap", "x": "nope"})
        self.assertFalse(response["ok"])
        self.assertIn("error", response)
        # The connection survives a validation error, same as any other.
        response = client.request({"id": 2, "cmd": "ping"})
        self.assertEqual(response, {"id": 2, "ok": True, "result": {}})

    def test_quit_and_version_and_state_accept_no_params_over_the_wire(self):
        self._start()
        client = self._client()
        for cmd in ("quit", "version", "state"):
            with self.subTest(cmd=cmd):
                response = client.request({"id": 1, "cmd": cmd})
                self.assertEqual(response, {"id": 1, "ok": True, "result": {}})


class FakePurpleAirClientTest(unittest.TestCase):
    """Dev-only fixture client; ships only with the debug module."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.fixture = Path(self._tempdir.name) / "purpleair.json"

    def write(self, **data):
        self.fixture.write_text(json.dumps(data), encoding="utf-8")

    def row(self, sensor_id, *, latitude=1.0, longitude=1.0, location_type=0, age_s=0):
        return {"sensor_index": sensor_id, "name": f"Fixture {sensor_id}", "latitude": latitude,
                "longitude": longitude, "location_type": location_type, "age_s": age_s,
                "humidity": 50, "temperature": 70, "pm1.0": 5, "pm2.5_cf_1": 150, "pm10.0": 9,
                "confidence": 100}

    def test_filters_like_the_api_and_stamps_last_seen_from_age(self):
        self.write(rows=[self.row(1), self.row(2, location_type=1), self.row(3, latitude=40.0),
                         self.row(4, age_s=600)])
        client = FakePurpleAirClient(str(self.fixture), clock=lambda: 10_000)
        bounds = Bounds(2.0, 0.0, 0.0, 2.0)

        outdoor = client.fetch_rows(bounds=bounds, location_filter="outdoor")
        indoor = client.fetch_rows(bounds=bounds, location_filter="indoor")
        both = client.fetch_rows(bounds=bounds, location_filter="both")
        favorites = client.fetch_rows(show_only=[2, 3])

        self.assertEqual([r["sensor_index"] for r in outdoor.rows], [1, 4])
        self.assertEqual([r["sensor_index"] for r in indoor.rows], [2])
        self.assertEqual([r["sensor_index"] for r in both.rows], [1, 2, 4])
        self.assertEqual([r["sensor_index"] for r in favorites.rows], [2, 3])
        self.assertEqual(outdoor.rows[1]["last_seen"], 10_000 - 600)
        self.assertNotIn("age_s", outdoor.rows[0])
        self.assertEqual(outdoor.time_stamp, 10_000)

    def test_fixture_is_reread_so_a_test_can_switch_to_an_outage(self):
        self.write(rows=[self.row(1)])
        client = FakePurpleAirClient(str(self.fixture))
        self.assertEqual(len(client.fetch_rows(show_only=[1]).rows), 1)
        self.write(fail=True, rows=[])
        with self.assertRaisesRegex(PurpleAirError, "Fake PurpleAir outage"):
            client.fetch_rows(show_only=[1])

    def test_factory_is_inert_unless_the_fixture_variable_is_set(self):
        self.assertIsNone(debug_purpleair_factory({}))
        self.write(rows=[])
        factory = debug_purpleair_factory({"AIRLOOM_DEBUG_FAKE_PURPLEAIR": str(self.fixture)})
        client = factory("any-key")
        self.assertIsInstance(client, FakePurpleAirClient)
        self.assertEqual(client.fetch_rows(show_only=[1]).rows, [])


if __name__ == "__main__":
    unittest.main()
