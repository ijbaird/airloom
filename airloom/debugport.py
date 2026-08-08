"""Debug port: a stdlib-only control channel for driving Airloom from an
external test harness (e.g. an agent synthesizing native gestures or
polling map state).

SECURITY: this port is only ever opened when the ``AIRLOOM_DEBUG_SOCKET``
environment variable names a socket path — Airloom never starts it on its
own, and there is no default path. By convention the path should live
under ``$XDG_RUNTIME_DIR`` (a per-user, 0700 tmpfs directory on
GNOME/systemd sessions), e.g.::

    AIRLOOM_DEBUG_SOCKET="$XDG_RUNTIME_DIR/airloom-debug.sock" ./run

The socket file is always created 0600 regardless of the process umask,
and any stale file already at the path is unlinked first. This module
does not itself refuse a world-writable parent directory (e.g. /tmp) —
callers are responsible for choosing a safe path; XDG_RUNTIME_DIR is
safe, /tmp is not.

Protocol: newline-delimited JSON over an ``AF_UNIX``/``SOCK_STREAM``
socket, one client connection at a time. Each line the client sends is a
request object ``{"id": N, "cmd": ...}`` and gets exactly one reply line
back: ``{"id": N, "ok": true, "result": ...}`` on success or
``{"id": N, "ok": false, "error": "..."}`` on failure. Malformed input
(non-JSON, or JSON that isn't an object) always gets an error reply, never
a crash or a dropped connection.

This module implements only the framing/transport, so it can be unit
tested without importing GTK (see tests/test_debugport.py). The actual
command handlers (``ping``, ``eval``, ``pinch``, ``version``, ``tap``,
``search``, ``key``, ``state``, ``screenshot``, ``quit``, ...) are
supplied by the caller as a
``dispatcher(command: dict, reply: Callable[[dict], None])`` callable —
see ``airloom/app.py`` for the real dispatcher, which marshals onto the
GTK main loop via ``GLib.idle_add`` because GTK/WebKit must never be
touched from this module's own accept/serve thread.

Payload validation for every command (beyond ``eval``'s own ad hoc check
and ``pinch``'s, which predate this module's validator table) lives in
``validate_command`` below, table-driven and GTK-free, so its rules are
exercised by plain ``unittest`` without a display.
"""

from __future__ import annotations

import json
import math
import os
import socket
import threading
from typing import Callable

Reply = Callable[[dict], None]
Dispatcher = Callable[[dict, Reply], None]

# How long the port thread will wait for the dispatcher to call `reply`
# before giving up and returning a timeout error to the client. This is a
# backstop distinct from any per-command timeout (e.g. `eval`'s 5s JS
# timeout) — it exists so a dispatcher that never calls back (or a dead
# GTK main loop) can't hang the socket forever.
DEFAULT_REPLY_TIMEOUT = 10.0


class DebugPort:
    def __init__(self, path: str, dispatcher: Dispatcher, reply_timeout: float = DEFAULT_REPLY_TIMEOUT) -> None:
        self._path = str(path)
        self._dispatcher = dispatcher
        self._reply_timeout = reply_timeout
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    def start(self) -> None:
        """Bind, chmod 0600, and start accepting connections on a daemon thread."""
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)
        try:
            sock.bind(self._path)
        finally:
            os.umask(old_umask)
        # Belt-and-braces: chmod explicitly rather than relying solely on
        # the umask trick, in case the path already existed with looser
        # permissions before the unlink (e.g. a stale socket from a crash).
        os.chmod(self._path, 0o600)
        sock.listen(1)
        self._sock = sock
        self._thread = threading.Thread(target=self._accept_loop, name="airloom-debugport", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stopping.is_set():
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                return
            try:
                self._serve(conn)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _serve(self, conn: socket.socket) -> None:
        buf = b""
        while not self._stopping.is_set():
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                response = self._handle_line(line)
                conn.sendall(json.dumps(response).encode("utf-8") + b"\n")

    def _handle_line(self, line: bytes) -> dict:
        request_id = None
        try:
            message = json.loads(line.decode("utf-8"))
            if not isinstance(message, dict):
                raise ValueError("request must be a JSON object")
            request_id = message.get("id")
        except Exception as exc:  # noqa: BLE001 — any parse failure must become an error reply, not a crash
            return {"id": request_id, "ok": False, "error": f"malformed request: {exc}"}

        event = threading.Event()
        result_box: dict = {}

        def reply(response: dict) -> None:
            # First call wins; a dispatcher that (incorrectly) calls back
            # twice must not resurrect a request the port already timed out.
            if not event.is_set():
                result_box.update(response)
                event.set()

        try:
            self._dispatcher(message, reply)
        except Exception as exc:  # noqa: BLE001 — dispatcher bugs must not crash the port thread
            return {"id": request_id, "ok": False, "error": f"dispatcher error: {exc}"}

        if not event.wait(self._reply_timeout):
            return {"id": request_id, "ok": False, "error": "timed out waiting for reply"}

        out = {"id": request_id}
        out.update(result_box)
        out.setdefault("ok", False)
        return out


# -- Command validation -----------------------------------------------------
# GTK-free by construction: each validator only reads the request dict and
# returns either (normalized_params, None) or (None, error_message). app.py's
# dispatcher calls validate_command() before doing anything GTK-touching, so
# a malformed request never reaches a widget or the webview.


def _validate_no_params(_command: dict) -> tuple[dict, None]:
    return {}, None


def _validate_tap(command: dict) -> tuple[dict | None, str | None]:
    try:
        x = float(command["x"])
        y = float(command["y"])
    except (KeyError, TypeError, ValueError):
        return None, "tap requires numeric 'x' and 'y' fields"
    if not (math.isfinite(x) and math.isfinite(y)):
        return None, "tap requires finite 'x' and 'y' fields"
    return {"x": x, "y": y}, None


def _validate_search(command: dict) -> tuple[dict | None, str | None]:
    query = command.get("query")
    if not isinstance(query, str):
        return None, "search requires a string 'query' field"
    return {"query": query}, None


def _validate_key(command: dict) -> tuple[dict | None, str | None]:
    key = command.get("key")
    if not isinstance(key, str) or not key:
        return None, "key requires a non-empty string 'key' field"
    return {"key": key}, None


def _validate_screenshot(command: dict) -> tuple[dict | None, str | None]:
    path = command.get("path")
    if path is None:
        return {"path": None}, None
    if not isinstance(path, str) or not path:
        return None, "screenshot 'path', if given, must be a non-empty string"
    if not os.path.isabs(path):
        return None, "screenshot 'path' must be an absolute path"
    return {"path": path}, None


# Commands whose handlers are implemented directly in app.py's dispatcher
# using their own pre-existing validation (`eval`, `pinch`) are deliberately
# absent from this table — app.py routes those before consulting it. Every
# other known command, including ones with no parameters, is listed
# explicitly so an unrecognized `cmd` always fails closed.
_VALIDATORS: dict[str, Callable[[dict], "tuple[dict | None, str | None]"]] = {
    "ping": _validate_no_params,
    "version": _validate_no_params,
    "state": _validate_no_params,
    "quit": _validate_no_params,
    "tap": _validate_tap,
    "search": _validate_search,
    "key": _validate_key,
    "screenshot": _validate_screenshot,
}


def validate_command(command: dict) -> tuple[dict | None, str | None]:
    """Validate and normalize a decoded debug command's parameters.

    Returns ``(normalized_params, None)`` on success — an empty dict for
    parameter-less commands — or ``(None, error_message)`` if `command`
    is missing/invalid fields or names a `cmd` this table doesn't know
    about (which includes `eval`/`pinch`; callers must route those before
    falling back to this function).
    """
    validator = _VALIDATORS.get(command.get("cmd"))
    if validator is None:
        return None, f"unknown cmd: {command.get('cmd')!r}"
    return validator(command)
