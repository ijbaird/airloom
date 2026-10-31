"""Replay a shared-id favorite through the real adapter, controller, and trend worker.

Sensor 7 is a favorite that PurpleAir reports both in the area fetch and on
its own favorite fetch, with different observations (``last_seen`` AREA_SEEN
and FAVORITE_SEEN). Sensor 8 is a favorite outside the area. One real refresh
runs through ``_fetch_readings`` and ``_finish_refresh``; then the JS-posted
bridge messages read from stdin as ``{"messages": [...]}`` are applied, and
every trend worker runs to completion.

Prints the published events, encoded as ``AirloomApplication._send`` splices
them into JS, and the PurpleAir calls the fake client served. The run is
deterministic, so a replay with more messages extends the same event list.
Used by tests/js/state.test.js and tests/test_cross_contracts.py.
"""

import json
import sys
from unittest import mock

# First: on hosts without PyGObject it installs the gi stub airloom.app needs.
try:
    from tests.test_app_requests import (
        ApplicationRequestTestCase,
        IdleQueue,
        ScriptMessage,
        ThreadQueue,
    )
except ImportError:  # loaded from inside tests/ (unittest discover -s tests)
    from test_app_requests import ApplicationRequestTestCase, IdleQueue, ScriptMessage, ThreadQueue

from airloom import app as app_module
from airloom.purpleair import TREND_FETCH_FIELDS, FetchResult

AREA_SEEN = 1_790_000_000
FAVORITE_SEEN = AREA_SEEN - 600
SHARED_ID = 7
REMOTE_ID = 8
AREA_ONLY_ID = 9


def row(sensor_id: int, last_seen: int, pm: float) -> dict:
    return {
        "sensor_index": sensor_id,
        "name": f"Sensor {sensor_id}",
        "latitude": 0.0,
        "longitude": 0.0,
        "last_seen": last_seen,
        "location_type": 0,
        "humidity": 50.0,
        "temperature": 68.0,
        "pm1.0": 5.0,
        "pm2.5_cf_1": pm,
        "pm10.0": 12.0,
        "confidence": 100,
    }


AREA_ROWS = [row(AREA_ONLY_ID, AREA_SEEN - 120, 4.0), row(SHARED_ID, AREA_SEEN, 10.0)]
FAVORITE_ROWS = {
    SHARED_ID: row(SHARED_ID, FAVORITE_SEEN, 20.0),
    REMOTE_ID: row(REMOTE_ID, AREA_SEEN - 300, 6.0),
}


def trend_row(sensor_id: int) -> dict:
    return {
        "sensor_index": sensor_id,
        "humidity": 50.0,
        "location_type": 0,
        "pm2.5_cf_1": 10.0,
        "pm2.5_10minute": 9.0,
        "pm2.5_30minute": 8.0,
        "pm2.5_60minute": 7.0,
        "pm2.5_6hour": 6.0,
        "pm2.5_24hour": 5.0,
        "pm2.5_1week": 4.0,
    }


class FakeClient:
    def __init__(self):
        self.calls = []

    def fetch_rows(self, bounds=None, show_only=None, location_filter="outdoor", fields=None):
        if fields == TREND_FETCH_FIELDS:
            self.calls.append({"kind": "trend", "ids": list(show_only)})
            return FetchResult([trend_row(sensor_id) for sensor_id in show_only], 123)
        if show_only is not None:
            self.calls.append({"kind": "favorites", "ids": list(show_only)})
            return FetchResult([FAVORITE_ROWS[i] for i in show_only if i in FAVORITE_ROWS], 123)
        self.calls.append({"kind": "area"})
        return FetchResult(list(AREA_ROWS), 123)


def capture(messages: list) -> dict:
    helper = ApplicationRequestTestCase()
    app = helper.make_app()
    app.store.data["favorites"] = [SHARED_ID, REMOTE_ID]
    app.controller = app._new_controller()
    app.cache = None
    # make_app stubs publication out; this replay needs the real sensors event.
    del app._send_sensor_state
    client = FakeClient()
    threads = ThreadQueue()
    idles = IdleQueue()

    def drain():
        while threads.threads or idles.callbacks:
            while threads.threads:
                threads.threads.pop(0).run()
            while idles.callbacks:
                callback, args = idles.callbacks.pop(0)
                callback(*args)

    with (
        mock.patch.object(app_module, "_purpleair_client_factory", lambda _key: client),
        mock.patch.object(app_module.threading, "Thread", new=threads.factory),
        mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
    ):
        request = app.controller.begin_refresh()
        outcome = app_module._fetch_readings(
            app.controller.config, None, request, request.bounds, 120
        )
        app._finish_refresh(outcome)
        drain()
        for message in messages:
            app._on_script_message(None, ScriptMessage(message))
            drain()
    return {
        "events": [[event, payload] for event, payload in app.sent],
        "calls": client.calls,
    }


def main() -> None:
    json.dump(
        capture(json.load(sys.stdin)["messages"]),
        sys.stdout,
        ensure_ascii=True,
        separators=(",", ":"),
    )


if __name__ == "__main__":
    main()
