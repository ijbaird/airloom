"""GUI-free decoding for messages posted from the WebView to Python."""

from __future__ import annotations

import json


def decode_message(raw: str) -> dict:
    """Decode a script-message payload into the message object.

    JSC's to_json() serializes a posted JavaScript string as a JSON string,
    so WebKitGTK may hand us one additional encoding layer.
    """
    message = json.loads(raw)
    if isinstance(message, str):
        message = json.loads(message)
    if not isinstance(message, dict):
        raise TypeError("message is not an object")
    return message
