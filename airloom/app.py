from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
gi.require_version("Notify", "0.7")

from gi.repository import Adw, Gio, GLib, Gtk, Notify, WebKit  # noqa: E402

from . import __version__
from .demo import demo_sensors
from .models import Sensor
from .purpleair import PurpleAirClient, PurpleAirError, bounds_around
from .store import Store


APP_ID = "ai.stealthvision.Airloom"
RESOURCE_DIR = Path(__file__).parent / "resources"


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
        self.webview.get_settings().set_enable_developer_extras(False)
        self.webview.load_uri((RESOURCE_DIR / "index.html").as_uri())

        toolbar.add_top_bar(header)
        toolbar.set_content(self.webview)
        self.window.set_content(toolbar)
        self.window.present()

    def _on_script_message(self, _manager, value) -> None:
        try:
            if hasattr(value, "to_json"):
                raw = value.to_json(0)
            elif hasattr(value, "to_string"):
                raw = value.to_string()
            else:
                raw = str(value)
            message = json.loads(raw)
            # JSC's to_json() serializes a posted JavaScript string as a JSON
            # string, so WebKitGTK may hand us one additional encoding layer.
            if isinstance(message, str):
                message = json.loads(message)
            if not isinstance(message, dict):
                raise TypeError("message is not an object")
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
            self.selected_id = int(message["id"])
        elif action == "favorite":
            sensor_id = int(message["id"])
            enabled = self.store.toggle_favorite(sensor_id)
            for sensor in self.sensors:
                if sensor.sensor_id == sensor_id:
                    sensor.favorite = enabled
            self._send_sensor_state()
        elif action == "save-settings":
            self._save_settings(message)

    def _save_settings(self, message: dict) -> None:
        try:
            latitude = float(message["latitude"])
            longitude = float(message["longitude"])
            radius = max(2.0, min(100.0, float(message["radius_km"])))
            threshold = max(1, min(500, int(message["alert_threshold"])))
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise ValueError("Coordinates are outside their valid range.")
        except (KeyError, TypeError, ValueError) as exc:
            self._send("error", {"message": f"Could not save preferences: {exc}"})
            return

        self.store.data.update(
            {
                "latitude": latitude,
                "longitude": longitude,
                "radius_km": radius,
                "location_name": str(message.get("location_name") or "Custom location")[:80],
                "temperature_unit": "C" if message.get("temperature_unit") == "C" else "F",
                "alert_threshold": threshold,
            }
        )
        api_key = str(message.get("api_key") or "").strip()
        if message.get("clear_api_key"):
            self.store.data["api_key"] = ""
        elif api_key:
            self.store.data["api_key"] = api_key
        self.store.save()
        if self.title:
            self.title.set_subtitle(self.store.data["location_name"])
        self._send("config", self.store.public_config())
        self.refresh()

    def refresh(self) -> None:
        if self.refreshing or not self.webview:
            return
        self.refreshing = True
        self._send("loading", {"active": True})
        config = dict(self.store.data)

        def worker() -> None:
            source = "demo"
            error = None
            try:
                if config.get("api_key"):
                    client = PurpleAirClient(config["api_key"])
                    bounds = bounds_around(config["latitude"], config["longitude"], config["radius_km"])
                    sensors = client.fetch_sensors(bounds)
                    source = "PurpleAir live"
                    if not sensors:
                        error = "No public outdoor sensors were found in this area."
                else:
                    sensors = demo_sensors(config["latitude"], config["longitude"])
            except PurpleAirError as exc:
                sensors = demo_sensors(config["latitude"], config["longitude"])
                error = f"{exc} Showing demo readings instead."
            GLib.idle_add(self._finish_refresh, sensors, source, error)

        threading.Thread(target=worker, name="airloom-refresh", daemon=True).start()

    def _finish_refresh(self, sensors: list[Sensor], source: str, error: str | None) -> bool:
        favorites = set(self.store.data.get("favorites", []))
        for sensor in sensors:
            sensor.favorite = sensor.sensor_id in favorites
        self.sensors = sensors
        if self.selected_id not in {sensor.sensor_id for sensor in sensors}:
            self.selected_id = sensors[0].sensor_id if sensors else None
        self.refreshing = False
        self._send_sensor_state(source)
        self._send("loading", {"active": False})
        if error:
            self._send("error", {"message": error})
        self._check_alerts()
        return GLib.SOURCE_REMOVE

    def _send_sensor_state(self, source: str | None = None) -> None:
        payload = {
            "items": [sensor.to_dict() for sensor in self.sensors],
            "selected_id": self.selected_id,
            "source": source or ("PurpleAir live" if self.store.data.get("api_key") else "Demo data"),
            "config": self.store.public_config(),
        }
        self._send("sensors", payload)

    def _check_alerts(self) -> None:
        threshold = int(self.store.data.get("alert_threshold", 101))
        states = self.store.data.setdefault("alert_states", {})
        changed = False
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
                        f"Air quality alert · {sensor.name}",
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
        event_json = json.dumps(event, ensure_ascii=False)
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        script = f"window.Airloom && window.Airloom.receive({event_json}, {payload_json});"
        self.webview.evaluate_javascript(script, -1, None, None, None, None, None)

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
