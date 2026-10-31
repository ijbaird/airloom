"""Run JS-posted bridge messages through the real Python adapter.

Reads ``{"messages": [...], "fail_writes": bool, "sensors": [{"id", "origin"}]}``
on stdin and prints the events the adapter published, the seeded sensors as
Python publishes them, and the resulting selection. Used by the JS harness for cross-layer tests;
the adapter is the same fake-free AirloomApplication used by unit tests,
with a real Store in a temporary directory.
"""

import contextlib
import errno
import json
import sys
from unittest import mock

from airloom.controller import sensor_payload
from tests.test_app_requests import ScriptMessage, TransactionalActionTest, make_sensor


def main() -> None:
    request = json.load(sys.stdin)
    helper = TransactionalActionTest()
    # Drains the store's settings-sync worker before the directory is removed.
    with helper.settings_directory() as directory:
        sensors = []
        for item in request.get("sensors", []):
            sensor = make_sensor(item["id"])
            sensor.origin = item.get("origin", "live")
            sensors.append(sensor)
        app = helper.make_real_store_app(directory, sensors=sensors)
        failure = PermissionError(errno.EACCES, "Permission denied")
        writes = (
            mock.patch.object(app.store, "_write", side_effect=failure)
            if request.get("fail_writes")
            else contextlib.nullcontext()
        )
        with writes:
            for message in request["messages"]:
                app._on_script_message(None, ScriptMessage(message))
        # Encoded exactly as AirloomApplication._send splices payloads into JS.
        json.dump(
            {
                "events": [[event, payload] for event, payload in app.sent],
                "location_filter": app.store.data["location_filter"],
                "published": [
                    sensor_payload(sensor, remote_favorite=False) for sensor in sensors
                ],
                "selected_id": app.selected_id,
            },
            sys.stdout,
            ensure_ascii=True,
            separators=(",", ":"),
        )


if __name__ == "__main__":
    main()
