"""One-shot location detection through GeoClue (GNOME location service)."""

from __future__ import annotations

import sys


APP_ID = "ai.stealthvision.Airloom"


class GeoClueLocator:
    """Requests a single fix; reports via callback on the GLib main loop."""

    def __init__(self, timeout_seconds: int = 10):
        self.timeout_seconds = timeout_seconds
        self._delivered = False
        self._timeout_id = None
        self._simple = None  # keeps the GeoClue client alive until delivery completes (not beyond)

    def start(self, on_fix) -> None:
        try:
            import gi

            gi.require_version("Geoclue", "2.0")
            from gi.repository import Geoclue, GLib
        except (ImportError, ValueError) as exc:
            print(f"Airloom: GeoClue unavailable: {exc}", file=sys.stderr)
            on_fix(None, None)
            return

        def deliver(latitude, longitude):
            if self._delivered:
                return
            self._delivered = True
            if self._timeout_id is not None:
                GLib.source_remove(self._timeout_id)
                self._timeout_id = None
            self._simple = None
            on_fix(latitude, longitude)

        def on_timeout():
            self._timeout_id = None
            print("Airloom: location fix timed out", file=sys.stderr)
            deliver(None, None)
            return GLib.SOURCE_REMOVE

        def finished(_source, result):
            try:
                simple = Geoclue.Simple.new_finish(result)
                location = simple.get_location()
                self._simple = simple
                deliver(
                    float(location.get_property("latitude")),
                    float(location.get_property("longitude")),
                )
            except Exception as exc:  # denial, agent missing, service error
                print(f"Airloom: location fix failed: {exc}", file=sys.stderr)
                deliver(None, None)

        self._timeout_id = GLib.timeout_add_seconds(self.timeout_seconds, on_timeout)
        Geoclue.Simple.new(APP_ID, Geoclue.AccuracyLevel.NEIGHBORHOOD, None, finished)
