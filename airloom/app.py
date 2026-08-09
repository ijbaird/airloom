"""Airloom's Adw.Application shell.

Debug-instance signaling: when ``AIRLOOM_DEBUG_SOCKET`` is set, GNOME/Wayland
gives an agent-launched instance no reliably distinct taskbar icon — the
dock/switcher follow the app-id's desktop entry regardless of what
``Gtk.Window.set_icon_name`` is told at runtime, so this module does not
fight that. The **reliable** signals that a window belongs to a debug
instance are the window title (``"Airloom · DEBUG"``) and the red header bar
CSS (see ``_apply_debug_chrome``); the best-effort icon override is applied
too but is not something to depend on.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
gi.require_version("Notify", "0.7")

from gi.repository import Adw, Gio, GLib, Gtk, Notify, WebKit  # noqa: E402

from . import __version__
from .bridge import decode_message
from .cache import SensorCache, fetch_area, fetch_favorites
# Release bundles strip airloom/debugport.py (see packaging/*.yml), so debug
# support degrades to "absent" rather than erroring when the module is gone.
try:
    from .debugport import DebugPort, validate_command
except ImportError:  # pragma: no cover — only true in stripped release builds
    DebugPort = None
    validate_command = None
from .demo import demo_sensors
from .geocode import GeocodeError, reverse as reverse_geocode, search as place_search
from .location import GeoClueLocator, is_coarse_fix
from .models import Sensor
from .purpleair import (
    TREND_FETCH_FIELDS,
    Bounds,
    PurpleAirClient,
    PurpleAirError,
    bounds_around,
    bounds_contains,
    cap_bounds,
    sensor_from_values,
    trend_from_values,
)
from .store import Store

# How long a debug-port `eval` command waits for evaluate_javascript to
# call back before giving up and returning a timeout error to the client.
DEBUG_EVAL_TIMEOUT_SECONDS = 5
# Same idea for `screenshot`'s get_snapshot round-trip.
DEBUG_SCREENSHOT_TIMEOUT_SECONDS = 10
# `git describe` should return almost instantly; this is a backstop against
# a wedged/misconfigured git rather than a realistic expectation.
DEBUG_BUILD_ID_TIMEOUT_SECONDS = 2


APP_ID = "ai.stealthvision.Airloom"
RESOURCE_DIR = Path(__file__).parent / "resources"
LOCATOR_FOCUS_FALLBACK_SECONDS = 20


def _filter_demo(sensors: list[Sensor], mode: str) -> list[Sensor]:
    if mode not in ("indoor", "outdoor"):
        return sensors
    return [sensor for sensor in sensors if sensor.indoor == (mode == "indoor")]


def _no_sensors_message(mode: str) -> str:
    kind = {"outdoor": "outdoor ", "indoor": "indoor "}.get(mode, "")
    return f"No public {kind}sensors were found in this area."


def _age_label(seconds: float) -> str:
    return "just now" if seconds < 90 else f"{int(seconds // 60)} min ago"


class AirloomApplication(Adw.Application):
    def __init__(self):
        # Decided once, up front: every debug-instance signal (window
        # title/color, GApplication uniqueness, the new debug commands)
        # reads this single flag rather than re-checking the environment.
        # Debug mode requires all three: the env var opting in, the module
        # actually present (release bundles strip it), and NOT running from
        # an installed Flatpak (/.flatpak-info exists inside the sandbox) —
        # a release artifact must stay inert even if someone sets the env
        # var, and even in a hand-rolled bundle that forgot the strip.
        self.debug_mode = (
            bool(os.environ.get("AIRLOOM_DEBUG_SOCKET"))
            and DebugPort is not None
            and not os.path.exists("/.flatpak-info")
        )
        if os.environ.get("AIRLOOM_DEBUG_SOCKET") and not self.debug_mode:
            print("Airloom: debug support is not available in this build", file=sys.stderr)
        # With the default (unique) GApplication flags, a second launch just
        # activates the already-running instance instead of starting a new
        # one — fatal for a debug launch, which must always be its own
        # process so an agent can find and quit it independently of whatever
        # instance the user has open.
        flags = Gio.ApplicationFlags.NON_UNIQUE if self.debug_mode else Gio.ApplicationFlags.DEFAULT_FLAGS
        super().__init__(application_id=APP_ID, flags=flags)
        # `git describe` once at startup, not per `version` call — see
        # _compute_build_id.
        self._debug_build_id: str | None = self._compute_build_id() if self.debug_mode else None
        self.store = Store()
        self.cache = SensorCache()
        self._auto_refresh_id: int | None = None
        self.window: Adw.ApplicationWindow | None = None
        self.webview: WebKit.WebView | None = None
        self.title: Adw.WindowTitle | None = None
        self.sensors: list[Sensor] = []
        self.selected_id: int | None = None
        self.refreshing = False
        self.pending_fetch: tuple | None = None
        self.locator: GeoClueLocator | None = None
        self._locator_focus_handler: int | None = None
        self._locator_focus_fallback: int | None = None
        self.view_bounds: Bounds | None = None
        # Last real fetch outcome ("PurpleAir live" / "Demo data"), so callers
        # that resend sensor state without refetching (e.g. favorite toggles)
        # never guess a label that contradicts what was actually fetched.
        self.last_source: str | None = None
        # Where the user is currently looking, stamped as one (bounds, center)
        # pair so the auto-refresh timer can never combine a stale half.
        self.current_view: tuple[Bounds, tuple[float, float]] | None = None
        self.view_fetched_at = 0.0
        # Monotonic ticket for view-center reverse lookups: only the newest
        # request may label the chip, so a slow lookup for a view the user
        # already left can never overwrite a fresher name.
        self._view_name_generation = 0
        self._pinch_gesture: Gtk.GestureZoom | None = None
        self._debug_port: DebugPort | None = None
        self.connect("activate", self._on_activate)

    def _on_activate(self, _application) -> None:
        if self.window:
            self.window.present()
            return

        Notify.init("Airloom")
        window_title = "Airloom · DEBUG" if self.debug_mode else "Airloom"
        self.window = Adw.ApplicationWindow(application=self, title=window_title)
        self.window.set_default_size(1240, 780)
        self.window.set_size_request(760, 540)
        if self.debug_mode:
            self._apply_debug_chrome()

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title = Adw.WindowTitle(title=window_title, subtitle=self.store.data["location_name"])
        header.set_title_widget(self.title)

        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh readings")
        refresh_button.connect("clicked", lambda *_: self.refresh(force=True))
        header.pack_start(refresh_button)

        settings_button = Gtk.Button(icon_name="preferences-system-symbolic", tooltip_text="Preferences")
        settings_button.connect("clicked", lambda *_: self._send("open-settings", self.store.public_config()))
        header.pack_end(settings_button)

        about_button = Gtk.Button(icon_name="help-about-symbolic", tooltip_text="About Airloom")
        about_button.connect("clicked", self._show_about)
        header.pack_end(about_button)

        manager = WebKit.UserContentManager()
        manager.register_script_message_handler("airloom", None)
        manager.connect("script-message-received::airloom", self._on_script_message)
        self.webview = WebKit.WebView(user_content_manager=manager)
        self.webview.set_hexpand(True)
        self.webview.set_vexpand(True)
        settings = self.webview.get_settings()
        settings.set_enable_developer_extras(False)
        settings.set_user_agent_with_application_details("Airloom", __version__)
        self.webview.connect("decide-policy", self._on_decide_policy)
        self.webview.connect("notify::zoom-level", self._on_zoom_level_changed)
        self.webview.load_uri((RESOURCE_DIR / "index.html").as_uri())

        # WebKitGTK's own gesture controller silently claims trackpad pinch
        # as an internal page-scale zoom before the DOM ever sees a
        # ctrl+wheel or gesture* event for it. Intercepting in the capture
        # phase and claiming the sequence up front denies WebKit that
        # gesture, so we can forward it to the JS map zoom instead.
        pinch = Gtk.GestureZoom()
        pinch.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        pinch.connect("begin", self._on_native_pinch_begin)
        pinch.connect("scale-changed", self._on_native_pinch_scale)
        pinch.connect("end", self._on_native_pinch_end)
        pinch.connect("cancel", self._on_native_pinch_end)
        self.webview.add_controller(pinch)
        self._pinch_gesture = pinch

        toolbar.add_top_bar(header)
        toolbar.set_content(self.webview)
        self.window.set_content(toolbar)
        self.window.present()
        self._arm_auto_refresh()
        if self.store.data.get("home_mode") == "auto":
            self._start_locator_when_focused()

        debug_socket_path = os.environ.get("AIRLOOM_DEBUG_SOCKET")
        if debug_socket_path and self.debug_mode:
            self._debug_port = DebugPort(debug_socket_path, self._dispatch_debug_command)
            self._debug_port.start()
            print(f"Airloom: debug port listening on {debug_socket_path}", file=sys.stderr)

    def _apply_debug_chrome(self) -> None:
        """Make a debug-mode window impossible to mistake for a normal one.

        Adds the `airloom-debug` CSS class to the window and installs an
        application-priority CssProvider that paints the header bar red —
        see the module docstring for why this (plus the window title) is
        the reliable signal rather than the taskbar icon.
        """
        self.window.add_css_class("airloom-debug")
        provider = Gtk.CssProvider()
        provider.load_from_string(
            ".airloom-debug headerbar { background: #b3261e; color: #ffffff; }"
            ".airloom-debug headerbar windowtitle .subtitle { color: #ffd8d4; }"
        )
        Gtk.StyleContext.add_provider_for_display(
            self.window.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        # Best-effort only — see module docstring.
        if hasattr(self.window, "set_icon_name"):
            self.window.set_icon_name("applications-engineering-symbolic")

    @staticmethod
    def _compute_build_id() -> str | None:
        """`git describe --always --dirty` for the checkout this package runs
        from, computed once at startup (see __init__) since it never changes
        for the life of the process. Returns None on any failure (git
        missing, not a checkout, e.g. inside a Flatpak/installed build) —
        this is a debug nicety, never load-bearing.
        """
        try:
            result = subprocess.run(
                ["git", "describe", "--always", "--dirty"],
                cwd=str(Path(__file__).resolve().parent),
                capture_output=True,
                text=True,
                timeout=DEBUG_BUILD_ID_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 — any failure means "no build id", not a crash
            return None
        if result.returncode != 0:
            return None
        build = result.stdout.strip()
        return build or None

    def _start_locator(self) -> None:
        if self.locator is not None:
            self.locator.cancel()
        self.locator = GeoClueLocator()
        self.locator.start(self._on_location_fix)

    def _start_locator_when_focused(self) -> None:
        # GNOME Shell only shows the location-permission dialog for the
        # focused app; a request fired before our window is focused crashes
        # the dialog and comes back as a denial (gnome-shell#7548). Wait for
        # focus so the dialog can attach to our window.
        if self.window.is_active():
            self._start_locator()
            return
        self._locator_focus_handler = self.window.connect(
            "notify::is-active", self._on_window_active_for_locator
        )
        self._locator_focus_fallback = GLib.timeout_add_seconds(
            LOCATOR_FOCUS_FALLBACK_SECONDS, self._on_locator_focus_fallback
        )

    def _on_window_active_for_locator(self, window, _pspec) -> None:
        if not window.is_active():
            return
        self._clear_locator_focus_wait()
        self._start_locator()

    def _on_locator_focus_fallback(self) -> bool:
        self._locator_focus_fallback = None
        # Fire even without focus — better than never asking — but keep the
        # focus handler connected: if the window gains focus later, the retry
        # replaces this attempt, which GNOME may have refused unfocused.
        self._start_locator()
        return GLib.SOURCE_REMOVE

    def _clear_locator_focus_wait(self) -> None:
        if self._locator_focus_handler is not None:
            self.window.disconnect(self._locator_focus_handler)
            self._locator_focus_handler = None
        if self._locator_focus_fallback is not None:
            GLib.source_remove(self._locator_focus_fallback)
            self._locator_focus_fallback = None

    def _refresh_seconds(self) -> int:
        return int(self.store.data.get("refresh_minutes", 2)) * 60

    def _arm_auto_refresh(self) -> None:
        if self._auto_refresh_id is not None:
            GLib.source_remove(self._auto_refresh_id)
        self._auto_refresh_id = GLib.timeout_add_seconds(self._refresh_seconds(), self._auto_refresh)

    def _auto_refresh(self) -> bool:
        # Refresh whatever the user is currently looking at, not home — a user
        # who panned elsewhere shouldn't watch their markers get replaced by
        # home-area sensors on every refresh tick. Favorites are still folded in so
        # the alert check keeps covering starred sensors regardless of view.
        if self.current_view is not None:
            bounds, center = self.current_view
            self._start_fetch(bounds, center, include_favorites=True)
        else:
            self.refresh()
        return GLib.SOURCE_CONTINUE

    def _on_decide_policy(self, _webview, decision, decision_type) -> bool:
        # The UI is a local page; anything else (e.g. the OSM attribution
        # link) belongs in the system browser, and a remote origin must never
        # gain access to the script-message bridge.
        if decision_type not in (
            WebKit.PolicyDecisionType.NAVIGATION_ACTION,
            WebKit.PolicyDecisionType.NEW_WINDOW_ACTION,
        ):
            return False
        uri = decision.get_navigation_action().get_request().get_uri() or ""
        if uri.startswith("file://"):
            return False
        decision.ignore()
        if uri.startswith(("http://", "https://")) and self.window:
            Gtk.UriLauncher.new(uri).launch(self.window, None, None)
        return True

    def _on_zoom_level_changed(self, webview, _pspec) -> None:
        # The map handles pinch itself; engine-level page zoom would scale the
        # whole document and clip the overlays, so snap it straight back.
        if webview.get_zoom_level() != 1.0:
            webview.set_zoom_level(1.0)

    def _pinch_centroid(self, gesture: Gtk.GestureZoom) -> tuple[float, float]:
        ok, x, y = gesture.get_bounding_box_center()
        if ok:
            return x, y
        width = self.webview.get_width() if self.webview else 0
        height = self.webview.get_height() if self.webview else 0
        return width / 2, height / 2

    def _on_native_pinch_begin(self, gesture: Gtk.GestureZoom, _sequence) -> None:
        if not self.webview:
            return
        # Claiming the sequence here is what denies WebKit's internal
        # page-scale gesture controller the same touch sequence.
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        x, y = self._pinch_centroid(gesture)
        self._send("pinch", {"phase": "begin", "scale": 1.0, "x": x, "y": y})

    def _on_native_pinch_scale(self, gesture: Gtk.GestureZoom, scale: float) -> None:
        if not self.webview:
            return
        x, y = self._pinch_centroid(gesture)
        self._send("pinch", {"phase": "change", "scale": float(scale), "x": x, "y": y})

    def _on_native_pinch_end(self, gesture: Gtk.GestureZoom, _sequence=None) -> None:
        if not self.webview:
            return
        self._send("pinch", {"phase": "end"})

    def _on_location_fix(self, latitude, longitude, accuracy=None) -> None:
        if self.store.data.get("home_mode") != "auto":
            # A delayed fix from a locator started under auto mode must not
            # clobber coordinates the user has since pinned in fixed mode.
            return
        if latitude is None or longitude is None:
            self._send(
                "location",
                {
                    "latitude": self.store.data["latitude"],
                    "longitude": self.store.data["longitude"],
                    "name": self.store.data["location_name"],
                    "source": "fallback",
                },
            )
            if self.store.has_custom_location():
                self._send("error", {"message": "Using last known location."})
            else:
                # Fresh install: the store still holds the shipped default,
                # which is nobody's "last known location" - say what happened.
                self._send(
                    "error",
                    {
                        "message": (
                            "Couldn't detect your location - showing a default area. "
                            "Set your home in Preferences, or check that location "
                            "access is allowed in system Settings."
                        )
                    },
                )
            return
        if is_coarse_fix(accuracy) and self.store.has_custom_location():
            # An IP-level guess (tens of km, often the ISP's city rather than
            # the user's) must not overwrite a location we already know.
            self._send(
                "location",
                {
                    "latitude": self.store.data["latitude"],
                    "longitude": self.store.data["longitude"],
                    "name": self.store.data["location_name"],
                    "source": "fallback",
                },
            )
            self._send(
                "error",
                {
                    "message": (
                        f"Location detection was only approximate — keeping "
                        f"{self.store.data['location_name']}. Set a fixed home in "
                        f"Preferences if this looks wrong."
                    )
                },
            )
            return
        self.store.data.update({"latitude": float(latitude), "longitude": float(longitude)})
        self.store.save()
        self._send(
            "location",
            {
                "latitude": float(latitude),
                "longitude": float(longitude),
                "name": self.store.data["location_name"],
                "source": "geoclue",
            },
        )
        self.refresh()
        threading.Thread(
            target=self._reverse_label_worker, args=(float(latitude), float(longitude)), name="airloom-revgeo", daemon=True
        ).start()

    def _reverse_label_worker(self, latitude: float, longitude: float) -> None:
        try:
            name = reverse_geocode(latitude, longitude)
        except GeocodeError as exc:
            print(f"Airloom: {exc}", file=sys.stderr)
            name = f"{latitude:.2f}, {longitude:.2f}"
        GLib.idle_add(self._apply_location_name, name)

    def _apply_location_name(self, name: str) -> bool:
        if self.store.data.get("home_mode") == "auto":
            self.store.data["location_name"] = name[:80]
            self.store.save()
            if self.title:
                self.title.set_subtitle(name[:80])
            self._send("config", self.store.public_config())
        return GLib.SOURCE_REMOVE

    def _on_script_message(self, _manager, value) -> None:
        try:
            if hasattr(value, "to_json"):
                raw = value.to_json(0)
            elif hasattr(value, "to_string"):
                raw = value.to_string()
            else:
                raw = str(value)
            message = decode_message(raw)
        except Exception as exc:
            print(f"Airloom: ignored invalid web message: {exc}", file=sys.stderr)
            return

        action = message.get("action")
        if action == "ready":
            self._send("config", self.store.public_config())
            self._paint_cached_home()
            self.refresh()
        elif action == "refresh":
            self.refresh(force=True)
        elif action == "select":
            sensor_id = self._message_sensor_id(message)
            if sensor_id is not None:
                self.selected_id = sensor_id
                self._ensure_trend(sensor_id)
        elif action == "favorite":
            sensor_id = self._message_sensor_id(message)
            if sensor_id is not None and any(sensor.sensor_id == sensor_id for sensor in self.sensors):
                enabled = self.store.toggle_favorite(sensor_id)
                for sensor in self.sensors:
                    if sensor.sensor_id == sensor_id:
                        sensor.favorite = enabled
                self._send_sensor_state()
        elif action == "hide":
            sensor_id = self._message_sensor_id(message)
            if sensor_id is not None:
                if self.store.is_hidden(sensor_id):
                    self.store.unhide(sensor_id)
                    self._send_sensor_state()
                else:
                    sensor = next((s for s in self.sensors if s.sensor_id == sensor_id), None)
                    if sensor is not None:
                        self.store.hide(sensor_id, sensor.name)
                        self._send_sensor_state()
        elif action == "unhide":
            sensor_id = self._message_sensor_id(message)
            if sensor_id is not None:
                self.store.unhide(sensor_id)
                self._send_sensor_state()
        elif action == "unhide-all":
            self.store.unhide_all()
            self._send_sensor_state()
        elif action == "save-settings":
            self._save_settings(message)
        elif action == "view-changed":
            self._on_view_changed(message)
        elif action == "place-search":
            self._on_place_search(message)
        elif action == "set-location-filter":
            self._set_location_filter(message)
        else:
            print(f"Airloom: ignored unknown web action: {action!r}", file=sys.stderr)

    @staticmethod
    def _message_sensor_id(message: dict) -> int | None:
        try:
            return int(message["id"])
        except (KeyError, TypeError, ValueError):
            print("Airloom: ignored web message with invalid sensor id", file=sys.stderr)
            return None

    def _on_view_changed(self, message: dict) -> None:
        try:
            view = Bounds(
                float(message["north"]), float(message["west"]),
                float(message["south"]), float(message["east"]),
            )
            center = (float(message["lat"]), float(message["lon"]))
        except (KeyError, TypeError, ValueError):
            return
        if not (
            -90 <= view.south <= view.north <= 90
            and -180 <= view.west <= 180
            and -180 <= view.east <= 180
            and view.west <= view.east
        ):
            return
        self.current_view = (view, center)
        fresh = (time.monotonic() - self.view_fetched_at) < self._refresh_seconds()
        if self.view_bounds is not None and fresh and bounds_contains(self.view_bounds, view):
            return
        self._start_fetch(view, center, include_favorites=False)
        self._start_view_label(*center)

    def _start_view_label(self, latitude: float, longitude: float) -> None:
        """Label the summary chip with the area the user is now looking at."""
        self._view_name_generation += 1
        threading.Thread(
            target=self._view_label_worker,
            args=(latitude, longitude, self._view_name_generation),
            name="airloom-viewgeo",
            daemon=True,
        ).start()

    def _view_label_worker(self, latitude: float, longitude: float, generation: int) -> None:
        try:
            name = reverse_geocode(latitude, longitude)
        except GeocodeError as exc:
            print(f"Airloom: {exc}", file=sys.stderr)
            name = f"{latitude:.2f}, {longitude:.2f}"
        GLib.idle_add(self._apply_view_name, name, generation)

    def _apply_view_name(self, name: str, generation: int) -> bool:
        if generation == self._view_name_generation:
            self._send("view-name", {"name": name})
        return GLib.SOURCE_REMOVE

    def _on_place_search(self, message: dict) -> None:
        query = str(message.get("query") or "").strip()[:120]
        if not query:
            return

        def worker() -> None:
            payload = {"query": query, "results": []}
            try:
                payload["results"] = [
                    {"name": place.name, "latitude": place.latitude, "longitude": place.longitude}
                    for place in place_search(query)
                ]
            except GeocodeError:
                payload["error"] = "Place lookup unavailable."
            GLib.idle_add(self._send_places, payload)

        threading.Thread(target=worker, name="airloom-geocode", daemon=True).start()

    def _send_places(self, payload: dict) -> bool:
        self._send("places", payload)
        return GLib.SOURCE_REMOVE

    def _set_location_filter(self, message: dict) -> None:
        value = message.get("value")
        if value not in ("outdoor", "indoor", "both"):
            print(f"Airloom: ignored invalid location filter: {value!r}", file=sys.stderr)
            return
        self.store.data["location_filter"] = value
        self.store.save()
        self._send("config", self.store.public_config())
        if self.current_view is not None:
            bounds, center = self.current_view
            self._start_fetch(bounds, center, include_favorites=True)
        else:
            self.refresh()

    def _save_settings(self, message: dict) -> None:
        previous_mode = self.store.data.get("home_mode")
        try:
            radius = max(2.0, min(100.0, float(message["radius_km"])))
            heatmap = max(5.0, min(1000.0, float(message["heatmap_threshold_km"])))
            threshold = max(1, min(500, int(message["alert_threshold"])))
            home_mode = "fixed" if message.get("home_mode") == "fixed" else "auto"
            updates = {
                "radius_km": radius,
                "heatmap_threshold_km": heatmap,
                "alert_threshold": threshold,
                "home_mode": home_mode,
                "temperature_unit": "C" if message.get("temperature_unit") == "C" else "F",
            }
            minutes = int(message["refresh_minutes"])
            if minutes not in (2, 5, 10, 30):
                minutes = 2
            updates["refresh_minutes"] = minutes
            if home_mode == "fixed":
                latitude = float(message["home_lat"])
                longitude = float(message["home_lon"])
                if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                    raise ValueError("Coordinates are outside their valid range.")
                updates["latitude"] = latitude
                updates["longitude"] = longitude
                updates["location_name"] = str(message.get("location_name") or "Custom location")[:80]
        except (KeyError, TypeError, ValueError) as exc:
            self._send("error", {"message": f"Could not save preferences: {exc}"})
            return

        self.store.data.update(updates)
        api_key = str(message.get("api_key") or "").strip()
        if message.get("clear_api_key"):
            self.store.data["api_key"] = ""
        elif api_key:
            self.store.data["api_key"] = api_key
        self.store.save()
        self._arm_auto_refresh()
        if self.title:
            self.title.set_subtitle(self.store.data["location_name"])
        self._send("config", self.store.public_config())
        if home_mode == "auto" and previous_mode != "auto":
            # The user just clicked Save in our UI, so the window is focused
            # and the permission dialog (if any) can appear immediately.
            self._start_locator()
        if home_mode == "fixed":
            # Glide the map to the newly pinned home instead of leaving it
            # wherever the user happened to be looking when they saved.
            self._send(
                "location",
                {
                    "latitude": self.store.data["latitude"],
                    "longitude": self.store.data["longitude"],
                    "name": self.store.data["location_name"],
                    "source": "fixed",
                },
            )
        self.refresh()

    def _paint_cached_home(self) -> None:
        """First paint from the cache so launch never blocks on the network.
        The refresh that follows replaces it under the normal TTL rules."""
        if self.sensors or not self.store.data.get("api_key"):
            return
        config = self.store.data
        bounds = bounds_around(config["latitude"], config["longitude"], config["radius_km"])
        cached = [s for s in map(sensor_from_values, self.cache.sensors_in(bounds)) if s is not None]
        if cached:
            self.sensors = cached
            self._send_sensor_state("PurpleAir · cached")

    def refresh(self, force: bool = False) -> None:
        """Home refresh: home bounds plus favorited sensors wherever they are."""
        config = self.store.data
        bounds = bounds_around(config["latitude"], config["longitude"], config["radius_km"])
        self._start_fetch(bounds, (config["latitude"], config["longitude"]),
                          include_favorites=True, force=force)

    def _start_fetch(self, bounds: Bounds, center: tuple[float, float],
                     include_favorites: bool, force: bool = False) -> None:
        if not self.webview:
            return
        if self.refreshing:
            # Coalesce: the newest request wins and runs when the current lands.
            self.pending_fetch = (bounds, center, include_favorites, force)
            return
        self.refreshing = True
        bounds = cap_bounds(bounds, center)
        self._send("loading", {"active": True})
        config = dict(self.store.data)
        ttl = self._refresh_seconds()

        def worker() -> None:
            source = "Demo data"
            error = None
            sensors: list[Sensor] = []
            mode = config.get("location_filter", "outdoor")
            try:
                if config.get("api_key"):
                    client = PurpleAirClient(config["api_key"])
                    try:
                        area = fetch_area(client, self.cache, bounds, mode, ttl, force)
                        rows = list(area.rows)
                        if include_favorites:
                            have = {r.get("sensor_index") for r in rows}
                            rows += fetch_favorites(client, self.cache,
                                                    config.get("favorites", []), have, ttl, force)
                        sensors = [s for s in map(sensor_from_values, rows) if s is not None]
                        source = "PurpleAir live" if area.polled else \
                            f"PurpleAir · cached {_age_label(area.age)}"
                        if not sensors:
                            error = _no_sensors_message(mode)
                    except PurpleAirError as exc:
                        stale = [s for s in map(sensor_from_values, self.cache.sensors_in(bounds))
                                 if s is not None]
                        if stale:
                            # Stale real readings beat fake ones; demo only when
                            # the cache has nothing for this area.
                            sensors = stale
                            source = "PurpleAir · cached"
                            error = f"{exc} Showing cached readings."
                        else:
                            sensors = _filter_demo(demo_sensors(center[0], center[1]), mode)
                            source = "Demo data"
                            error = f"{exc} Showing demo readings instead."
                else:
                    sensors = _filter_demo(demo_sensors(center[0], center[1]), mode)
            except Exception as exc:  # noqa: BLE001 — a crashed worker must never wedge the refresh state
                sensors = []
                error = f"Refresh failed unexpectedly: {exc}"
            GLib.idle_add(self._finish_refresh, sensors, source, error, bounds)

        threading.Thread(target=worker, name="airloom-refresh", daemon=True).start()

    def _finish_refresh(self, sensors: list[Sensor], source: str, error: str | None, bounds: Bounds) -> bool:
        favorites = set(self.store.data.get("favorites", []))
        for sensor in sensors:
            sensor.favorite = sensor.sensor_id in favorites
        self.sensors = sensors
        if self.selected_id not in {sensor.sensor_id for sensor in sensors}:
            self.selected_id = sensors[0].sensor_id if sensors else None
        self.refreshing = False
        self.last_source = source
        self._send_sensor_state(source)
        self._send("loading", {"active": False})
        if error:
            self._send("error", {"message": error})
        self._check_alerts()
        if error is None:
            # A transient live-API failure must not be remembered as fresh —
            # that would suppress retries for the refresh interval.
            self.view_bounds = bounds
            self.view_fetched_at = time.monotonic()
        if self.selected_id is not None:
            self._ensure_trend(self.selected_id)
        if self.pending_fetch is not None:
            pending, self.pending_fetch = self.pending_fetch, None
            self._start_fetch(*pending)
        return GLib.SOURCE_REMOVE

    def _ensure_trend(self, sensor_id: int) -> None:
        """Attach a trend to the selected sensor: cached if fresh, else one
        cheap single-row fetch. Demo sensors already carry trends inline."""
        if not self.store.data.get("api_key"):
            return
        sensor = next((s for s in self.sensors if s.sensor_id == sensor_id), None)
        if sensor is None:
            return
        cached = self.cache.get_trend(sensor_id, self._refresh_seconds())
        if cached is not None:
            if sensor.trend != cached:
                sensor.trend = cached
                self._send_sensor_state()
            return
        api_key = self.store.data["api_key"]

        def worker() -> None:
            trend = None
            try:
                result = PurpleAirClient(api_key).fetch_rows(
                    show_only=[sensor_id], fields=TREND_FETCH_FIELDS)
                if result.rows:
                    trend = trend_from_values(result.rows[0])
            except PurpleAirError:
                trend = None  # chart keeps its loading/empty state; next select retries
            GLib.idle_add(self._finish_trend, sensor_id, trend)

        threading.Thread(target=worker, name="airloom-trend", daemon=True).start()

    def _finish_trend(self, sensor_id: int, trend: list | None) -> bool:
        if trend:
            self.cache.store_trend(sensor_id, trend)
            sensor = next((s for s in self.sensors if s.sensor_id == sensor_id), None)
            if sensor is not None:
                sensor.trend = trend
                self._send_sensor_state()
        return GLib.SOURCE_REMOVE

    def _send_sensor_state(self, source: str | None = None) -> None:
        hidden = self.store.hidden_ids()
        visible = [sensor for sensor in self.sensors if sensor.sensor_id not in hidden]
        # Hiding the selected sensor must not leave a dangling selection —
        # same reconciliation rule _finish_refresh applies after a fetch.
        if self.selected_id not in {sensor.sensor_id for sensor in visible}:
            self.selected_id = visible[0].sensor_id if visible else None
        payload = {
            "items": [sensor.to_dict() for sensor in visible],
            "selected_id": self.selected_id,
            "source": source or self.last_source or ("PurpleAir live" if self.store.data.get("api_key") else "Demo data"),
            "config": self.store.public_config(),
        }
        self._send("sensors", payload)

    def _check_alerts(self) -> None:
        try:
            threshold = int(self.store.data.get("alert_threshold", 101))
        except (TypeError, ValueError):
            threshold = 101
        states = self.store.data.setdefault("alert_states", {})
        changed = False
        favorite_keys = {str(sensor_id) for sensor_id in self.store.data.get("favorites", [])}
        for stale_key in [key for key in states if key not in favorite_keys]:
            del states[stale_key]
            changed = True
        hidden = self.store.hidden_ids()
        for sensor in self.sensors:
            if not sensor.favorite or sensor.aqi is None or sensor.sensor_id in hidden:
                continue
            key = str(sensor.sensor_id)
            was_high = bool(states.get(key, False))
            is_high = sensor.aqi >= threshold
            if is_high != was_high:
                states[key] = is_high
                changed = True
                if is_high:
                    notification = Notify.Notification.new(
                        f"Air quality alert · {sensor.name[:80]}",
                        f"AQI is now {sensor.aqi}. Open Airloom for health guidance.",
                        APP_ID,
                    )
                    try:
                        notification.show()
                    except GLib.Error:
                        pass
        if changed:
            self.store.save()

    def _send(self, event: str, payload) -> None:
        if not self.webview:
            return
        # ensure_ascii keeps U+2028/U+2029 (JS line terminators) out of the
        # evaluated source, since the payload is spliced in as a JS literal.
        event_json = json.dumps(event, ensure_ascii=True)
        payload_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        script = f"window.Airloom && window.Airloom.receive({event_json}, {payload_json});"
        self.webview.evaluate_javascript(script, -1, None, None, None, None, None)

    # -- Debug port -------------------------------------------------------
    # Only ever reachable when AIRLOOM_DEBUG_SOCKET is set (see _on_activate).
    # DebugPort calls this from its own accept/serve thread, so the very
    # first thing it must do is marshal onto the GTK main loop — nothing
    # below this point may touch GTK/WebKit off that thread.

    def _dispatch_debug_command(self, message: dict, reply) -> None:
        def run_on_main_loop() -> bool:
            self._handle_debug_command(message, reply)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(run_on_main_loop)

    def _handle_debug_command(self, message: dict, reply) -> None:
        cmd = message.get("cmd")
        # `eval` and `pinch` predate the validate_command() table and keep
        # their own inline validation; every other command's parameters are
        # validated there before any handler below sees them, so an
        # unrecognized `cmd` (or bad params) always fails closed without a
        # handler ever running.
        if cmd == "eval":
            self._debug_eval(message, reply)
            return
        if cmd == "pinch":
            self._debug_pinch(message, reply)
            return
        params, error = validate_command(message)
        if error is not None:
            reply({"ok": False, "error": error})
            return
        if cmd == "ping":
            reply(
                {
                    "ok": True,
                    "result": {"pong": True, "version": __version__, "pid": os.getpid(), "debug": True},
                }
            )
        elif cmd == "version":
            self._debug_version(reply)
        elif cmd == "state":
            self._debug_state(reply)
        elif cmd == "tap":
            self._debug_tap(params, reply)
        elif cmd == "search":
            self._debug_search(params, reply)
        elif cmd == "key":
            self._debug_key(params, reply)
        elif cmd == "screenshot":
            self._debug_screenshot(params, reply)
        elif cmd == "quit":
            self._debug_quit(reply)

    def _debug_pinch(self, message: dict, reply) -> None:
        phase = message.get("phase")
        if phase not in ("begin", "change", "end"):
            reply({"ok": False, "error": f"invalid pinch phase: {phase!r}"})
            return
        try:
            payload: dict = {"phase": phase}
            if phase in ("begin", "change"):
                scale = float(message.get("scale"))
                if not scale > 0:
                    raise ValueError("scale must be > 0")
                payload["scale"] = scale
                payload["x"] = float(message.get("x"))
                payload["y"] = float(message.get("y"))
        except (TypeError, ValueError) as exc:
            reply({"ok": False, "error": f"invalid pinch params: {exc}"})
            return
        self._send("pinch", payload)
        reply({"ok": True, "result": {"sent": True}})

    def _debug_eval(self, message: dict, reply) -> None:
        js = message.get("js")
        if not isinstance(js, str):
            reply({"ok": False, "error": "eval requires a string 'js' field"})
            return
        self._debug_evaluate(js, reply)

    def _debug_version(self, reply) -> None:
        reply(
            {
                "ok": True,
                "result": {
                    "version": __version__,
                    "build": self._debug_build_id,
                    "pid": os.getpid(),
                    "debug": True,
                },
            }
        )

    def _debug_state(self, reply) -> None:
        # window.Airloom.debugState() (app.js) is the single source of truth
        # for page state; this command is just eval's plumbing pointed at it
        # so a client gets one round-trip instead of composing its own JS.
        self._debug_evaluate("window.Airloom.debugState()", reply)

    def _debug_tap(self, params: dict, reply) -> None:
        js = f"window.Airloom.debugTap({json.dumps(params['x'])}, {json.dumps(params['y'])})"
        self._debug_evaluate(js, reply)

    def _debug_search(self, params: dict, reply) -> None:
        js = f"window.Airloom.debugSearch({json.dumps(params['query'])})"
        self._debug_evaluate(js, reply)

    def _debug_key(self, params: dict, reply) -> None:
        js = f"window.Airloom.debugKey({json.dumps(params['key'])})"
        self._debug_evaluate(js, reply)

    def _debug_quit(self, reply) -> None:
        # Reply before quitting so the client sees {"ok": true} rather than
        # a connection drop — the 50ms delay just needs to be long enough
        # for DebugPort's sendall() to flush ahead of the process exiting.
        reply({"ok": True})
        GLib.timeout_add(50, lambda: (self.quit(), GLib.SOURCE_REMOVE)[1])

    def _debug_screenshot(self, params: dict, reply) -> None:
        """`{"path": "/abs/path.png"}` (or no path) → PNG of the webview.

        This is webview content only — the WebKit snapshot API has no
        notion of the surrounding GTK header bar/window chrome, so a
        screenshot can never be used to confirm the debug-mode red header
        or "Airloom · DEBUG" window title; those are visible signals for a
        human looking at the window, not something a screenshot or `eval`
        can assert on. `ping`/`version` reporting `"debug": true` is the
        machine-checkable equivalent.
        """
        if not self.webview:
            reply({"ok": False, "error": "webview not available"})
            return
        path = params.get("path")

        done = False
        timeout_id: list[int | None] = [None]

        def finish(response: dict) -> None:
            nonlocal done
            if done:
                return
            done = True
            if timeout_id[0] is not None:
                GLib.source_remove(timeout_id[0])
                timeout_id[0] = None
            reply(response)

        def on_timeout() -> bool:
            timeout_id[0] = None
            finish({"ok": False, "error": "screenshot timed out"})
            return GLib.SOURCE_REMOVE

        timeout_id[0] = GLib.timeout_add_seconds(DEBUG_SCREENSHOT_TIMEOUT_SECONDS, on_timeout)

        def on_result(webview, task, _data) -> None:
            try:
                texture = webview.get_snapshot_finish(task)
            except GLib.Error as exc:
                finish({"ok": False, "error": str(exc)})
                return
            try:
                data = texture.save_to_png_bytes().get_data()
            except Exception as exc:  # noqa: BLE001 — genuinely unencodable texture
                finish({"ok": False, "error": f"failed to encode png: {exc}"})
                return
            if path:
                try:
                    with open(path, "wb") as handle:
                        handle.write(data)
                except OSError as exc:
                    finish({"ok": False, "error": f"failed to write {path}: {exc}"})
                    return
                finish({"ok": True, "result": {"path": path, "bytes": len(data)}})
            else:
                finish({"ok": True, "result": {"png_base64": base64.b64encode(data).decode("ascii")}})

        self.webview.get_snapshot(
            WebKit.SnapshotRegion.VISIBLE, WebKit.SnapshotOptions.NONE, None, on_result, None
        )

    def _debug_evaluate(self, js: str, reply) -> None:
        """Evaluate `js` in the webview and reply with its JSON-decoded
        result. Shared by `eval` itself and every other debug command that
        drives or reads page state through a window.Airloom.debug* helper.
        """
        if not self.webview:
            reply({"ok": False, "error": "webview not available"})
            return

        done = False
        timeout_id: list[int | None] = [None]

        def finish(response: dict) -> None:
            nonlocal done
            if done:
                return
            done = True
            if timeout_id[0] is not None:
                GLib.source_remove(timeout_id[0])
                timeout_id[0] = None
            reply(response)

        def on_timeout() -> bool:
            timeout_id[0] = None
            finish({"ok": False, "error": "eval timed out"})
            return GLib.SOURCE_REMOVE

        timeout_id[0] = GLib.timeout_add_seconds(DEBUG_EVAL_TIMEOUT_SECONDS, on_timeout)

        def on_result(webview, task, _data) -> None:
            try:
                value = webview.evaluate_javascript_finish(task)
            except GLib.Error as exc:
                finish({"ok": False, "error": str(exc)})
                return
            if value is None:
                finish({"ok": True, "result": None})
                return
            raw = None
            try:
                raw = value.to_json(0)
            except Exception:  # noqa: BLE001 — fall back to to_string below
                pass
            if raw is None:
                try:
                    raw = value.to_string()
                except Exception:  # noqa: BLE001 — genuinely unrepresentable result
                    raw = None
            result = raw
            if raw is not None:
                try:
                    result = json.loads(raw)
                except (TypeError, ValueError):
                    result = raw
            finish({"ok": True, "result": result})

        self.webview.evaluate_javascript(js, -1, None, None, None, on_result, None)

    def _show_about(self, _button) -> None:
        if not self.window:
            return
        about = Adw.AboutDialog(
            application_name="Airloom",
            application_icon=APP_ID,
            developer_name="Airloom contributors",
            version=__version__,
            comments="A focused, GNOME-native viewer for hyperlocal PurpleAir readings.",
            website="https://www2.purpleair.com/",
            license_type=Gtk.License.MIT_X11,
        )
        about.present(self.window)


def main() -> int:
    return AirloomApplication().run(sys.argv)
