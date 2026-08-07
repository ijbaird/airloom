from __future__ import annotations

import json
import os
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
from .debugport import DebugPort
from .demo import demo_sensors
from .geocode import GeocodeError, reverse as reverse_geocode, search as place_search
from .location import GeoClueLocator, is_coarse_fix
from .models import Sensor
from .purpleair import Bounds, PurpleAirClient, PurpleAirError, bounds_around, bounds_contains
from .store import Store

# How long a debug-port `eval` command waits for evaluate_javascript to
# call back before giving up and returning a timeout error to the client.
DEBUG_EVAL_TIMEOUT_SECONDS = 5


APP_ID = "ai.stealthvision.Airloom"
RESOURCE_DIR = Path(__file__).parent / "resources"
AUTO_REFRESH_SECONDS = 300
LOCATOR_FOCUS_FALLBACK_SECONDS = 20


def _filter_demo(sensors: list[Sensor], mode: str) -> list[Sensor]:
    if mode not in ("indoor", "outdoor"):
        return sensors
    return [sensor for sensor in sensors if sensor.indoor == (mode == "indoor")]


def _no_sensors_message(mode: str) -> str:
    kind = {"outdoor": "outdoor ", "indoor": "indoor "}.get(mode, "")
    return f"No public {kind}sensors were found in this area."


class AirloomApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.store = Store()
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
        self.window = Adw.ApplicationWindow(application=self, title="Airloom")
        self.window.set_default_size(1240, 780)
        self.window.set_size_request(760, 540)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title = Adw.WindowTitle(title="Airloom", subtitle=self.store.data["location_name"])
        header.set_title_widget(self.title)

        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh readings")
        refresh_button.connect("clicked", lambda *_: self.refresh())
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
        GLib.timeout_add_seconds(AUTO_REFRESH_SECONDS, self._auto_refresh)
        if self.store.data.get("home_mode") == "auto":
            self._start_locator_when_focused()

        debug_socket_path = os.environ.get("AIRLOOM_DEBUG_SOCKET")
        if debug_socket_path:
            self._debug_port = DebugPort(debug_socket_path, self._dispatch_debug_command)
            self._debug_port.start()
            print(f"Airloom: debug port listening on {debug_socket_path}", file=sys.stderr)

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

    def _auto_refresh(self) -> bool:
        # Refresh whatever the user is currently looking at, not home — a user
        # who panned elsewhere shouldn't watch their markers get replaced by
        # home-area sensors every 5 minutes. Favorites are still folded in so
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
            self.refresh()
        elif action == "refresh":
            self.refresh()
        elif action == "select":
            sensor_id = self._message_sensor_id(message)
            if sensor_id is not None:
                self.selected_id = sensor_id
        elif action == "favorite":
            sensor_id = self._message_sensor_id(message)
            if sensor_id is not None and any(sensor.sensor_id == sensor_id for sensor in self.sensors):
                enabled = self.store.toggle_favorite(sensor_id)
                for sensor in self.sensors:
                    if sensor.sensor_id == sensor_id:
                        sensor.favorite = enabled
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
        fresh = (time.monotonic() - self.view_fetched_at) < AUTO_REFRESH_SECONDS
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
            threshold = max(1, min(500, int(message["alert_threshold"])))
            home_mode = "fixed" if message.get("home_mode") == "fixed" else "auto"
            updates = {
                "radius_km": radius,
                "alert_threshold": threshold,
                "home_mode": home_mode,
                "temperature_unit": "C" if message.get("temperature_unit") == "C" else "F",
            }
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

    def refresh(self) -> None:
        """Home refresh: home bounds plus favorited sensors wherever they are."""
        config = self.store.data
        bounds = bounds_around(config["latitude"], config["longitude"], config["radius_km"])
        self._start_fetch(bounds, (config["latitude"], config["longitude"]), include_favorites=True)

    def _start_fetch(self, bounds: Bounds, center: tuple[float, float], include_favorites: bool) -> None:
        if not self.webview:
            return
        if self.refreshing:
            # Coalesce: the newest request wins and runs when the current lands.
            self.pending_fetch = (bounds, center, include_favorites)
            return
        self.refreshing = True
        self._send("loading", {"active": True})
        config = dict(self.store.data)

        def worker() -> None:
            source = "Demo data"
            error = None
            sensors: list[Sensor] = []
            mode = config.get("location_filter", "outdoor")
            try:
                try:
                    if config.get("api_key"):
                        client = PurpleAirClient(config["api_key"])
                        sensors = client.fetch_sensors(bounds=bounds, location_filter=mode)
                        source = "PurpleAir live"
                        if include_favorites:
                            missing = set(config.get("favorites", [])) - {s.sensor_id for s in sensors}
                            if missing:
                                sensors += client.fetch_sensors(show_only=sorted(missing))
                        if not sensors:
                            error = _no_sensors_message(mode)
                    else:
                        sensors = _filter_demo(demo_sensors(center[0], center[1]), mode)
                except PurpleAirError as exc:
                    # The area fetch may already have flipped source to live
                    # before a later favorites fetch failed; what we deliver
                    # is demo data either way, so label (and cache) it as such.
                    source = "Demo data"
                    sensors = _filter_demo(demo_sensors(center[0], center[1]), mode)
                    error = f"{exc} Showing demo readings instead."
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
            # that would suppress retries for AUTO_REFRESH_SECONDS.
            self.view_bounds = bounds
            self.view_fetched_at = time.monotonic()
        if self.pending_fetch is not None:
            pending, self.pending_fetch = self.pending_fetch, None
            self._start_fetch(*pending)
        return GLib.SOURCE_REMOVE

    def _send_sensor_state(self, source: str | None = None) -> None:
        payload = {
            "items": [sensor.to_dict() for sensor in self.sensors],
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
        for sensor in self.sensors:
            if not sensor.favorite or sensor.aqi is None:
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
        if cmd == "ping":
            reply({"ok": True, "result": {"pong": True}})
        elif cmd == "eval":
            self._debug_eval(message, reply)
        elif cmd == "pinch":
            self._debug_pinch(message, reply)
        else:
            reply({"ok": False, "error": f"unknown cmd: {cmd!r}"})

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
            comments="A focused, Fedora-native viewer for hyperlocal PurpleAir readings.",
            website="https://www2.purpleair.com/",
            license_type=Gtk.License.MIT_X11,
        )
        about.present(self.window)


def main() -> int:
    return AirloomApplication().run(sys.argv)
