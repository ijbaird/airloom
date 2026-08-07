import json
import os
import socket
import tempfile
import unittest
from pathlib import Path

from airloom.debugport import DebugPort


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
        # Simulate a leftover file at the path (e.g. from a prior crash)
        # from *before* start() is called on a fresh DebugPort instance.
        stale_path = str(Path(self._tempdir.name) / "stale.sock")
        Path(stale_path).write_text("stale", encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
