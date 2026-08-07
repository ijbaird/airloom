"""One-shot location detection through GeoClue (GNOME location service)."""

from __future__ import annotations

import sys


APP_ID = "ai.stealthvision.Airloom"

# GeoClue stamps every fix with an accuracy radius in meters. IP-based
# fallback fixes come back around 25 km — a city-level guess that is often
# the wrong city — while genuine WiFi/GNSS fixes are a few hundred meters.
COARSE_FIX_METERS = 10000.0


def is_coarse_fix(accuracy: float | None) -> bool:
    """True when a fix is too imprecise to trust over a known location."""
    return accuracy is None or accuracy > COARSE_FIX_METERS


class GeoClueLocator:
    """Requests a single fix; reports via callback on the GLib main loop.

    The callback may fire twice: once with (None, None, None) when the
    attempt times out or errors, and once more with real coordinates if a
    fix still arrives afterwards — the GNOME permission dialog can easily
    outlive the timeout, and a late "Allow" must not be wasted.
    """

    def __init__(self, timeout_seconds: int = 10):
        self.timeout_seconds = timeout_seconds
        self._fix_delivered = False
        self._fallback_sent = False
        self._timeout_id = None
        self._simple = None  # keeps the GeoClue client alive until delivery completes (not beyond)

    def _note_fix(self) -> bool:
        """Record a genuine fix; True when it should be delivered."""
        if self._fix_delivered:
            return False
        self._fix_delivered = True
        return True

    def _note_fallback(self) -> bool:
        """Record a failed attempt; True when the fallback should be reported."""
        if self._fix_delivered or self._fallback_sent:
            return False
        self._fallback_sent = True
        return True

    def start(self, on_fix) -> None:
        try:
            import gi

            gi.require_version("Geoclue", "2.0")
            from gi.repository import Geoclue, GLib
        except (ImportError, ValueError) as exc:
            print(f"Airloom: GeoClue unavailable: {exc}", file=sys.stderr)
            if self._note_fallback():
                on_fix(None, None, None)
            return

        def cancel_timeout():
            if self._timeout_id is not None:
                GLib.source_remove(self._timeout_id)
                self._timeout_id = None

        def on_timeout():
            self._timeout_id = None
            print("Airloom: location fix timed out", file=sys.stderr)
            # Report the fallback but keep listening: the fix that follows a
            # slow answer to the permission dialog is still worth having.
            if self._note_fallback():
                on_fix(None, None, None)
            return GLib.SOURCE_REMOVE

        def finished(_source, result):
            try:
                simple = Geoclue.Simple.new_finish(result)
                location = simple.get_location()
                self._simple = simple
                if self._note_fix():
                    cancel_timeout()
                    on_fix(
                        float(location.get_property("latitude")),
                        float(location.get_property("longitude")),
                        float(location.get_property("accuracy")),
                    )
            except Exception as exc:  # denial, agent missing, service error
                print(f"Airloom: location fix failed: {exc}", file=sys.stderr)
                cancel_timeout()
                self._simple = None
                if self._note_fallback():
                    on_fix(None, None, None)

        self._timeout_id = GLib.timeout_add_seconds(self.timeout_seconds, on_timeout)
        Geoclue.Simple.new(APP_ID, Geoclue.AccuracyLevel.NEIGHBORHOOD, None, finished)
