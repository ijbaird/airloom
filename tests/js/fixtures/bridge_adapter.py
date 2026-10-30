"""Run JS-posted bridge messages through the real Python adapter.

Reads ``{"messages": [...], "fail_writes": bool}`` on stdin and prints the
events the adapter published. Used by the JS harness for cross-layer tests;
the adapter is the same fake-free AirloomApplication used by unit tests,
with a real Store in a temporary directory.
"""

import contextlib
import errno
import json
import sys
import tempfile
from unittest import mock

from tests.test_app_requests import ScriptMessage, TransactionalActionTest


def main() -> None:
    request = json.load(sys.stdin)
    with tempfile.TemporaryDirectory() as directory:
        app = TransactionalActionTest().make_real_store_app(directory)
        failure = PermissionError(errno.EACCES, "Permission denied")
        writes = (
            mock.patch.object(app.store, "_write", side_effect=failure)
            if request.get("fail_writes")
            else contextlib.nullcontext()
        )
        with writes:
            for message in request["messages"]:
                app._on_script_message(None, ScriptMessage(message))
        json.dump(
            {
                "events": [[event, payload] for event, payload in app.sent],
                "location_filter": app.store.data["location_filter"],
            },
            sys.stdout,
        )


if __name__ == "__main__":
    main()
