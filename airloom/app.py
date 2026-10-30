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
import math
import os
import subprocess
import sys
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
gi.require_version("Notify", "0.7")

from gi.repository import Adw, Gio, GLib, Gtk, Notify, WebKit  # noqa: E402

from . import __version__
from . import geocode as geocode_module
from .bridge import decode_message
from .cache import CacheError, SensorCache, fetch_area, fetch_favorites
from .controller import ApplicationController, RefreshRequest
# Release bundles strip airloom/debugport.py (see packaging/*.yml), so debug
# support degrades to "absent" rather than erroring when the module is gone.
try:
    from .debugport import DebugPort, validate_command
except ImportError:  # pragma: no cover — only true in stripped release builds
    DebugPort = None
    validate_command = None
from .demo import demo_sensors
from .geocode import GeocodeError, reverse as reverse_geocode, search as place_search
from .geography import Bounds, bounds_around, bounds_contains, split_bounds
from .location import GeoClueLocator, is_coarse_fix
from .models import Sensor
from .presentation import alert_transitions, visible_sensors
from .purpleair import (
    MAP_FIELDS,
    TREND_FETCH_FIELDS,
    PurpleAirClient,
    PurpleAirError,
    cap_bounds,
    sensor_from_values,
    trend_from_values,
)
from .requests import RequestRegistry, RequestToken
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
BUNDLED_ENTRY_URI = (RESOURCE_DIR / "index.html").as_uri()
LOCATOR_FOCUS_FALLBACK_SECONDS = 20


class _PlaceSearchSuperseded(Exception):
    """A geocoder admitted a request after its bridge intent was superseded."""


_place_search_admission = threading.local()
_place_search_install_lock = threading.Lock()


def _check_place_search_admission() -> None:
    is_current = getattr(_place_search_admission, "is_current", None)
    if is_current is not None and not is_current():
        raise _PlaceSearchSuperseded()


def _install_place_search_admission() -> None:
    """Add adapter-owned cancellation to the shared geocoder's I/O boundary.

    The data-owned Geocoder deliberately has no bridge-request dependency. Its
    public search flow nevertheless exposes two adapter admission points:
    ``_request`` after its search-lock wait and ``_throttle`` after any sleep.
    Wrapping those points preserves its cache, throttling, and transport code.
    Unannotated users of the shared client, including reverse geocoding, retain
    the original behavior.
    """
    client = geocode_module._default_client()
    with _place_search_install_lock:
        if getattr(client, "_airloom_place_search_admission", False):
            return
        original_request = client._request
        original_throttle = client._throttle

        def request(url, params, timeout):
            _check_place_search_admission()
            return original_request(url, params, timeout)

        def throttle():
            original_throttle()
            _check_place_search_admission()

        client._request = request
        client._throttle = throttle
        client._airloom_place_search_admission = True


@dataclass(slots=True)
class RefreshOutcome:
    """Worker-owned readings and transport metadata for one refresh request."""

    request: RefreshRequest
    sensors: list[Sensor]
    area_ids: set[int]
    monitoring_ids: set[int]
    source: str
    origin: str
    delivery: str
    error: str | None
    stale: bool
    bounds: Bounds
    capped: bool
    provenance: dict[int, tuple[str, str]]


def _open_sensor_cache() -> tuple[SensorCache | None, str | None]:
    """Open persistence without making a cache failure fatal to live data."""
    try:
        return SensorCache(), None
    except CacheError as exc:
        warning = f"{exc}. Continuing without persistent caching."
        print(f"Airloom: {warning}", file=sys.stderr)
        return None, warning


def _coverage_payload(requested: Bounds, fetched: Bounds) -> dict:
    return {
        "north": fetched.north,
        "west": fetched.west,
        "south": fetched.south,
        "east": fetched.east,
        "capped": requested != fetched,
    }


def _view_from_message(message: dict) -> tuple[Bounds, tuple[float, float]]:
    view = Bounds(
        float(message["north"]),
        float(message["west"]),
        float(message["south"]),
        float(message["east"]),
    )
    center = (float(message["lat"]), float(message["lon"]))
    if not (
        all(math.isfinite(value) for value in center)
        and -90 <= center[0] <= 90
        and -180 <= center[1] <= 180
    ):
        raise ValueError("view center is outside its valid range")
    return view, center


def _presentation_collections(
    sensors: list[Sensor],
    *,
    area_ids: set[int],
    monitoring_ids: set[int],
    hidden_ids: set[int],
    confidence_filter: bool,
    location_filter: str,
) -> tuple[list[Sensor], list[Sensor], list[Sensor]]:
    """Apply display rules once, keeping area and favorite inputs distinct."""
    area = [sensor for sensor in sensors if sensor.sensor_id in area_ids]
    remote = [
        sensor
        for sensor in sensors
        if sensor.sensor_id in monitoring_ids
        and sensor.sensor_id not in area_ids
        and sensor.favorite
    ]
    visible = visible_sensors(
        [*area, *remote],
        hidden_ids=hidden_ids,
        confidence_filter=confidence_filter,
        location_filter=location_filter,
    )
    visible_ids = {sensor.sensor_id for sensor in visible}
    visible_area = [sensor for sensor in area if sensor.sensor_id in visible_ids]
    favorites = [sensor for sensor in visible if sensor.favorite]
    return visible, visible_area, favorites


def _sensor_payload(sensor: Sensor, *, remote_favorite: bool) -> dict:
    payload = sensor.to_dict()
    payload["remote_favorite"] = remote_favorite
    return payload


def _sensors_from_rows(rows) -> list[Sensor]:
    sensors = []
    seen = set()
    for values in rows:
        sensor = sensor_from_values(values)
        if sensor is not None and sensor.sensor_id not in seen:
            sensors.append(sensor)
            seen.add(sensor.sensor_id)
    return sensors


def _no_sensors_message(mode: str) -> str:
    kind = {"outdoor": "outdoor ", "indoor": "indoor "}.get(mode, "")
    return f"No public {kind}sensors were found in this area."


def _age_label(seconds: float) -> str:
    return "just now" if seconds < 90 else f"{int(seconds // 60)} min ago"


def _fetch_readings(
    config: dict,
    cache: SensorCache | None,
    request: RefreshRequest,
    bounds: Bounds,
    ttl: float,
) -> RefreshOutcome:
    """Fetch and cache in the worker; publication is authorized separately."""
    mode = config.get("location_filter", "outdoor")
    origin = "live" if config.get("api_key") else "demo"
    delivery = "network"
    source = "PurpleAir live" if origin == "live" else "Demo data"
    error = None
    stale = False
    sensors: list[Sensor] = []
    area_ids: set[int] = set()
    monitoring_ids: set[int] = set()
    provenance: dict[int, tuple[str, str]] = {}

    try:
        if config.get("api_key"):
            client = PurpleAirClient(config["api_key"])
            try:
                cached_favorite_ids: set[int] = set()
                if cache is None:
                    area_rows = []
                    for part in split_bounds(bounds):
                        result = client.fetch_rows(
                            bounds=part,
                            location_filter=mode,
                            fields=MAP_FIELDS,
                        )
                        area_rows.extend(result.rows)
                    area_polled = True
                    area_age = 0.0
                else:
                    area = fetch_area(client, cache, bounds, mode, ttl, request.force)
                    area_rows = list(area.rows)
                    area_polled = area.polled
                    area_age = area.age

                area_sensors = _sensors_from_rows(area_rows)
                area_ids = {sensor.sensor_id for sensor in area_sensors}
                missing_favorites = [
                    sensor_id
                    for sensor_id in request.favorite_ids
                    if sensor_id not in area_ids
                ]
                favorite_rows = []
                if missing_favorites:
                    if cache is None:
                        favorite_rows = client.fetch_rows(
                            show_only=sorted(missing_favorites)
                        ).rows
                    else:
                        if not request.force:
                            cached_favorite_ids = set(
                                cache.fresh_sensors(missing_favorites, ttl)
                            )
                        favorite_rows = fetch_favorites(
                            client,
                            cache,
                            request.favorite_ids,
                            area_ids,
                            ttl,
                            request.force,
                        )

                favorite_sensors = _sensors_from_rows(favorite_rows)
                monitoring_ids = {
                    sensor.sensor_id
                    for sensor in favorite_sensors
                    if sensor.sensor_id not in area_ids
                }
                sensors = [
                    *area_sensors,
                    *[
                        sensor
                        for sensor in favorite_sensors
                        if sensor.sensor_id not in area_ids
                    ],
                ]
                area_delivery = "network" if area_polled else "cache"
                provenance = {
                    sensor.sensor_id: ("live", area_delivery)
                    for sensor in area_sensors
                }
                provenance.update(
                    {
                        sensor.sensor_id: (
                            "live",
                            "cache"
                            if sensor.sensor_id in cached_favorite_ids
                            else "network",
                        )
                        for sensor in favorite_sensors
                        if sensor.sensor_id not in area_ids
                    }
                )
                delivery = area_delivery
                source = (
                    "PurpleAir live"
                    if area_polled
                    else f"PurpleAir · cached {_age_label(area_age)}"
                )
                if not sensors:
                    error = _no_sensors_message(mode)
            except PurpleAirError as exc:
                stale_rows = [] if cache is None else cache.sensors_in(bounds)
                stale_sensors = _sensors_from_rows(stale_rows)
                if stale_sensors:
                    # Retrieval is degraded, but each observation keeps its own
                    # last_seen timestamp for alert freshness decisions.
                    sensors = stale_sensors
                    area_ids = {sensor.sensor_id for sensor in stale_sensors}
                    monitoring_ids = set()
                    provenance = {
                        sensor.sensor_id: ("live", "cache")
                        for sensor in stale_sensors
                    }
                    origin = "live"
                    delivery = "cache"
                    stale = True
                    source = "PurpleAir · cached"
                    error = f"{exc} Showing cached readings."
                else:
                    sensors = demo_sensors(request.center[0], request.center[1])
                    area_ids = {sensor.sensor_id for sensor in sensors}
                    provenance = {
                        sensor.sensor_id: ("demo", "network") for sensor in sensors
                    }
                    origin = "demo"
                    delivery = "network"
                    source = "Demo data"
                    error = f"{exc} Showing demo readings instead."
        else:
            sensors = demo_sensors(request.center[0], request.center[1])
            area_ids = {sensor.sensor_id for sensor in sensors}
            provenance = {
                sensor.sensor_id: ("demo", "network") for sensor in sensors
            }
    except Exception as exc:  # noqa: BLE001 — a worker failure must still finalize
        sensors = []
        area_ids = set()
        monitoring_ids = set()
        provenance = {}
        error = f"Refresh failed unexpectedly: {exc}"

    return RefreshOutcome(
        request=request,
        sensors=sensors,
        area_ids=area_ids,
        monitoring_ids=monitoring_ids,
        source=source,
        origin=origin,
        delivery=delivery,
        error=error,
        stale=stale,
        bounds=bounds,
        capped=request.bounds != bounds,
        provenance=provenance,
    )


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
        self.controller = ApplicationController(self.store.data)
        self.requests = RequestRegistry()
        self._worker_lock = threading.Lock()
        self._active_workers = 0
        self._cache_closed = False
        self._shutting_down = False
        self.cache, self._cache_error = _open_sensor_cache()
        self._auto_refresh_id: int | None = None
        self.window: Adw.ApplicationWindow | None = None
        self.webview: WebKit.WebView | None = None
        self._shell_recovery_notice: str | None = None
        self.title: Adw.WindowTitle | None = None
        self.sensors: list[Sensor] = []
        self._area_ids: set[int] = set()
        self._monitoring_ids: set[int] = set()
        self.selected_id: int | None = None
        self._trend_inflight: dict[int, tuple[RequestToken, int | None]] = {}
        self.refreshing = False
        self.pending_fetch: RefreshRequest | None = None
        self.locator: GeoClueLocator | None = None
        self._locator_focus_handler: int | None = None
        self._locator_focus_fallback: int | None = None
        self.view_bounds: Bounds | None = None
        # Last real fetch outcome ("PurpleAir live" / "Demo data"), so callers
        # that resend sensor state without refetching (e.g. favorite toggles)
        # never guess a label that contradicts what was actually fetched.
        self.last_source: str | None = None
        self.last_origin = "live"
        self.last_stale = False
        self.coverage: dict | None = None
        # Where the user is currently looking, stamped as one (bounds, center)
        # pair so the auto-refresh timer can never combine a stale half.
        self.current_view: tuple[Bounds, tuple[float, float]] | None = None
        self.view_fetched_at = 0.0
        self.coverage_generation: int | None = None
        # Reverse labels carry both request identity and their exact target;
        # generation alone cannot authorize a callback after coordinates change.
        self._home_name_generation = 0
        self._view_name_generation = 0
        self._pinch_gesture: Gtk.GestureZoom | None = None
        self._debug_port: DebugPort | None = None
        self.connect("activate", self._on_activate)
        self.connect("shutdown", self._on_shutdown)

    def _start_worker(self, target, *, name: str, args=()) -> bool:
        """Admit one tracked worker unless shutdown has begun."""
        with self._worker_lock:
            if self._shutting_down:
                return False
            self._active_workers += 1

        def run() -> None:
            try:
                target(*args)
            finally:
                self._worker_finished()

        try:
            # Python waits for non-daemon workers at interpreter shutdown, so
            # every admitted worker runs its finalizer and the last one closes
            # the cache. The GTK thread never joins a worker or waits for a
            # queued GUI callback, avoiding shutdown deadlocks.
            threading.Thread(target=run, name=name, daemon=False).start()
        except BaseException:
            self._worker_finished()
            raise
        return True

    def _worker_finished(self) -> None:
        with self._worker_lock:
            self._active_workers -= 1
        self._close_cache_if_drained()

    def _close_cache_if_drained(self) -> None:
        with self._worker_lock:
            if (
                not self._shutting_down
                or self._active_workers
                or self._cache_closed
            ):
                return
            self._cache_closed = True
            cache = self.cache
        if cache is not None:
            cache.close()

    def _on_shutdown(self, _application=None) -> None:
        with self._worker_lock:
            if self._shutting_down:
                return
            self._shutting_down = True

        self.pending_fetch = None
        self.refreshing = False
        self.requests.invalidate()
        # The pure controller has its own refresh registry. Advancing the
        # configuration generation invalidates every outstanding completion.
        self.controller.set_config(self.store.data)
        if self._auto_refresh_id is not None:
            GLib.source_remove(self._auto_refresh_id)
            self._auto_refresh_id = None
        self._clear_locator_focus_wait()
        if self.locator is not None:
            self.locator.cancel()
            self.locator = None
        if self._debug_port is not None:
            self._debug_port.stop()
            self._debug_port = None
        self._close_cache_if_drained()

    def _on_activate(self, _application) -> None:
        if self._shutting_down:
            return
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
        self.webview.connect("load-failed", self._on_load_failed)
        self.webview.connect("web-process-terminated", self._on_web_process_terminated)
        self.webview.connect("notify::zoom-level", self._on_zoom_level_changed)
        self.webview.set_zoom_level(self._interface_scale())
        self.webview.load_uri(BUNDLED_ENTRY_URI)

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
        if self._shutting_down:
            return
        if self.locator is not None:
            self.locator.cancel()
        self.locator = GeoClueLocator()
        self.locator.start(self._on_location_fix)

    def _start_locator_when_focused(self) -> None:
        if self._shutting_down:
            return
        # GNOME Shell only shows the location-permission dialog for the
        # focused app; a request fired before our window is focused crashes
        # the dialog and comes back as a denial (gnome-shell#7548). Wait for
        # focus so the dialog can attach to our window.
        if self.store.data.get("home_mode") != "auto":
            return
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
        if self.store.data.get("home_mode") == "auto":
            self._start_locator()

    def _on_locator_focus_fallback(self) -> bool:
        self._locator_focus_fallback = None
        if self.store.data.get("home_mode") != "auto":
            self._clear_locator_focus_wait()
            return GLib.SOURCE_REMOVE
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
        if self._shutting_down:
            return
        if self._auto_refresh_id is not None:
            GLib.source_remove(self._auto_refresh_id)
        self._auto_refresh_id = GLib.timeout_add_seconds(self._refresh_seconds(), self._auto_refresh)

    def _auto_refresh(self) -> bool:
        if self._shutting_down:
            return GLib.SOURCE_REMOVE
        # Refresh whatever the user is currently looking at, not home — a user
        # who panned elsewhere shouldn't watch their markers get replaced by
        # home-area sensors on every refresh tick. Favorites are still folded in so
        # the alert check keeps covering starred sensors regardless of view.
        self.refresh()
        return GLib.SOURCE_CONTINUE

    def _on_decide_policy(self, _webview, decision, decision_type) -> bool:
        # Only the bundled entry may use the script-message bridge. Approved
        # web links open in the system browser; every other navigation is
        # ignored rather than being allowed to inherit this WebView's trust.
        if decision_type not in (
            WebKit.PolicyDecisionType.NAVIGATION_ACTION,
            WebKit.PolicyDecisionType.NEW_WINDOW_ACTION,
        ):
            return False
        uri = decision.get_navigation_action().get_request().get_uri() or ""
        if uri == BUNDLED_ENTRY_URI:
            decision.use()
            return True
        decision.ignore()
        try:
            parsed = urlsplit(uri)
        except ValueError:
            return True
        if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
            Gtk.UriLauncher.new(uri).launch(self.window, None, None)
        return True

    def _on_load_failed(self, _webview, _load_event, _failing_uri, error) -> bool:
        self._send(
            "shell-status",
            {
                "status": "error",
                "message": f"Airloom could not load its interface ({error}). Reload to try again.",
                "can_reload": True,
            },
        )
        # Keep WebKit's own error page as a visible fallback when the bundled
        # document is not alive to display the bridge status above.
        return False

    def _on_web_process_terminated(self, _webview, _reason) -> None:
        # A dead renderer cannot display an error event. Reload the trusted
        # entry immediately, then surface the queued notice after its ready
        # handshake instead of leaving a silent blank WebView.
        self._shell_recovery_notice = (
            "Airloom's interface process stopped unexpectedly. The interface "
            "was reloaded; Reload to try again if the problem continues."
        )
        self._reload_shell()

    def _reload_shell(self) -> None:
        if self._shutting_down or not self.webview:
            return
        self._send(
            "shell-status",
            {
                "status": "loading",
                "message": "Reloading Airloom…",
                "can_reload": False,
            },
        )
        self.webview.set_zoom_level(self._interface_scale())
        self.webview.load_uri(BUNDLED_ENTRY_URI)

    def _interface_scale(self) -> float:
        scale = self.store.data.get("interface_scale", 1.0)
        if isinstance(scale, float) and scale in (1.0, 1.25, 1.5):
            return scale
        return 1.0

    def _on_zoom_level_changed(self, webview, _pspec) -> None:
        # Native map pinch is forwarded to the DOM, not page zoom. If WebKit
        # changes page scale anyway, restore the persisted interface choice.
        scale = self._interface_scale()
        if webview.get_zoom_level() != scale:
            webview.set_zoom_level(scale)

    def _pinch_centroid(self, gesture: Gtk.GestureZoom) -> tuple[float, float]:
        ok, x, y = gesture.get_bounding_box_center()
        if ok:
            return x, y
        width = self.webview.get_width() if self.webview else 0
        height = self.webview.get_height() if self.webview else 0
        return width / 2, height / 2

    def _pinch_document_coordinates(self, gesture: Gtk.GestureZoom) -> tuple[float, float]:
        x, y = self._pinch_centroid(gesture)
        interface_scale = self._interface_scale()
        return x / interface_scale, y / interface_scale

    def _on_native_pinch_begin(self, gesture: Gtk.GestureZoom, _sequence) -> None:
        if not self.webview:
            return
        # Claiming the sequence here is what denies WebKit's internal
        # page-scale gesture controller the same touch sequence.
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        x, y = self._pinch_document_coordinates(gesture)
        self._send("pinch", {"phase": "begin", "scale": 1.0, "x": x, "y": y})

    def _on_native_pinch_scale(self, gesture: Gtk.GestureZoom, scale: float) -> None:
        if not self.webview:
            return
        x, y = self._pinch_document_coordinates(gesture)
        self._send("pinch", {"phase": "change", "scale": float(scale), "x": x, "y": y})

    def _on_native_pinch_end(self, gesture: Gtk.GestureZoom, _sequence=None) -> None:
        if not self.webview:
            return
        self._send("pinch", {"phase": "end"})

    def _on_location_fix(self, latitude, longitude, accuracy=None) -> None:
        if self._shutting_down:
            return
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
        candidate = deepcopy(self.store.data)
        candidate.update({"latitude": float(latitude), "longitude": float(longitude)})
        try:
            self.store.apply(candidate)
        except OSError as exc:
            self._send(
                "error",
                {
                    "message": (
                        f"Could not save detected location: {exc}. Check disk space "
                        "and file permissions, then try again."
                    )
                },
            )
            return
        self.controller.set_config(self.store.data)
        self._send(
            "location",
            {
                "latitude": float(latitude),
                "longitude": float(longitude),
                "name": self.store.data["location_name"],
                "source": "geoclue",
            },
        )
        self._refresh_home()
        self._home_name_generation += 1
        generation = self._home_name_generation
        token = self.requests.begin("home-label", generation)
        self._start_worker(
            self._reverse_label_worker,
            args=(float(latitude), float(longitude), generation, token),
            name="airloom-revgeo",
        )

    def _reverse_label_worker(
        self,
        latitude: float,
        longitude: float,
        generation: int,
        token: RequestToken,
    ) -> None:
        try:
            name = reverse_geocode(latitude, longitude)
        except GeocodeError as exc:
            print(f"Airloom: {exc}", file=sys.stderr)
            name = f"{latitude:.2f}, {longitude:.2f}"
        GLib.idle_add(
            self._apply_location_name,
            name,
            latitude,
            longitude,
            generation,
            token,
        )

    def _apply_location_name(
        self,
        name: str,
        latitude: float,
        longitude: float,
        generation: int,
        token: RequestToken,
    ) -> bool:
        config = self.store.data
        current_home = (config.get("latitude"), config.get("longitude"))
        try:
            if (
                not self._shutting_down
                and self.requests.is_current(token)
                and generation == self._home_name_generation
                and config.get("home_mode") == "auto"
                and current_home == (latitude, longitude)
            ):
                candidate = deepcopy(config)
                candidate["location_name"] = name[:80]
                try:
                    self.store.apply(candidate)
                except OSError as exc:
                    self._send(
                        "error",
                        {
                            "message": (
                                f"Could not save detected location name: {exc}. "
                                "Check disk space and file permissions, then try again."
                            )
                        },
                    )
                else:
                    if self.title:
                        self.title.set_subtitle(name[:80])
                    self._send("config", self.store.public_config())
        finally:
            self.requests.finish(token)
        return GLib.SOURCE_REMOVE

    def _on_script_message(self, _manager, value) -> None:
        if self._shutting_down:
            return
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
        if action == "reload-shell":
            self._reload_shell()
        elif action == "ready":
            self._send("config", self.store.public_config())
            recovery_notice = getattr(self, "_shell_recovery_notice", None)
            if recovery_notice:
                self._shell_recovery_notice = None
                self._send(
                    "shell-status",
                    {
                        "status": "error",
                        "message": recovery_notice,
                        "can_reload": True,
                    },
                )
            if self._cache_error:
                self._send("error", {"message": self._cache_error})
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
                try:
                    enabled = self.store.toggle_favorite(sensor_id)
                except OSError as exc:
                    self._send(
                        "error",
                        {
                            "message": (
                                f"Could not save favorite: {exc}. Check disk space and "
                                "file permissions, then try again."
                            )
                        },
                    )
                    return
                self._set_publication_config()
                for sensor in self.sensors:
                    if sensor.sensor_id == sensor_id:
                        sensor.favorite = enabled
                self._send_sensor_state()
        elif action == "hide":
            sensor_id = self._message_sensor_id(message)
            if sensor_id is not None:
                if self.store.is_hidden(sensor_id):
                    try:
                        self.store.unhide(sensor_id)
                    except OSError as exc:
                        self._send(
                            "error",
                            {
                                "message": (
                                    f"Could not unhide sensor: {exc}. Check disk space "
                                    "and file permissions, then try again."
                                )
                            },
                        )
                        return
                    self._set_publication_config()
                    self._send_sensor_state()
                else:
                    sensor = next((s for s in self.sensors if s.sensor_id == sensor_id), None)
                    if sensor is not None:
                        try:
                            self.store.hide(sensor_id, sensor.name)
                        except OSError as exc:
                            self._send(
                                "error",
                                {
                                    "message": (
                                        f"Could not hide sensor: {exc}. Check disk space "
                                        "and file permissions, then try again."
                                    )
                                },
                            )
                            return
                        self._set_publication_config()
                        self._send_sensor_state()
        elif action == "unhide":
            sensor_id = self._message_sensor_id(message)
            if sensor_id is not None:
                try:
                    self.store.unhide(sensor_id)
                except OSError as exc:
                    self._send(
                        "error",
                        {
                            "message": (
                                f"Could not unhide sensor: {exc}. Check disk space "
                                "and file permissions, then try again."
                            )
                        },
                    )
                    return
                self._set_publication_config()
                self._send_sensor_state()
        elif action == "unhide-all":
            try:
                self.store.unhide_all()
            except OSError as exc:
                self._send(
                    "error",
                    {
                        "message": (
                            f"Could not unhide all sensors: {exc}. Check disk space "
                            "and file permissions, then try again."
                        )
                    },
                )
                return
            self._set_publication_config()
            self._send_sensor_state()
        elif action == "save-settings":
            self._save_settings(message)
        elif action == "view-changed":
            self._on_view_changed(message)
        elif action == "place-search":
            self._on_place_search(message)
        elif action == "cancel-place-search":
            context = message.get("context")
            if context in ("main", "home"):
                self.requests.invalidate(f"place-search:{context}")
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
        if self._shutting_down:
            return
        try:
            view, center = _view_from_message(message)
        except (KeyError, TypeError, ValueError, OverflowError):
            return
        self.current_view = (view, center)
        self.controller.set_view(message)
        # Register new view intent even when cached coverage needs no worker:
        # the token supersedes active/pending requests for the previous view.
        request = self.controller.begin_refresh()
        self.pending_fetch = None
        fresh = (time.monotonic() - self.view_fetched_at) < self._refresh_seconds()
        if (
            self.view_bounds is not None
            and self.coverage_generation == request.generation
            and fresh
            and bounds_contains(self.view_bounds, view)
        ):
            return
        self._start_fetch(request)
        self._start_view_label(*center)

    def _start_view_label(self, latitude: float, longitude: float) -> None:
        """Label the summary chip with the area the user is now looking at."""
        if self._shutting_down:
            return
        self._view_name_generation += 1
        generation = self._view_name_generation
        token = self.requests.begin("view-label", generation)
        self._start_worker(
            self._view_label_worker,
            args=(latitude, longitude, generation, token),
            name="airloom-viewgeo",
        )

    def _view_label_worker(
        self,
        latitude: float,
        longitude: float,
        generation: int,
        token: RequestToken,
    ) -> None:
        try:
            name = reverse_geocode(latitude, longitude)
        except GeocodeError as exc:
            print(f"Airloom: {exc}", file=sys.stderr)
            name = f"{latitude:.2f}, {longitude:.2f}"
        GLib.idle_add(
            self._apply_view_name, name, latitude, longitude, generation, token
        )

    def _apply_view_name(
        self,
        name: str,
        latitude: float,
        longitude: float,
        generation: int,
        token: RequestToken,
    ) -> bool:
        center = self.current_view[1] if self.current_view is not None else None
        if (
            not self._shutting_down
            and self.requests.is_current(token)
            and generation == self._view_name_generation
            and center == (latitude, longitude)
        ):
            self._send("view-name", {"name": name})
        self.requests.finish(token)
        return GLib.SOURCE_REMOVE

    def _on_place_search(self, message: dict) -> None:
        if self._shutting_down:
            return
        raw_query = str(message.get("query") or "")
        query = raw_query.strip()[:120]
        context = message.get("context", "main")
        if not query or context not in ("main", "home"):
            return

        token = self.requests.begin(f"place-search:{context}")
        payload = {
            "request_id": message.get("request_id", token.request_id),
            "context": context,
            "query": raw_query,
            "results": [],
        }

        def worker() -> None:
            # A thread queued behind newer user intent must not reach the network.
            if not self.requests.is_current(token):
                return
            _install_place_search_admission()
            _place_search_admission.is_current = lambda: self.requests.is_current(token)
            try:
                try:
                    payload["results"] = [
                        {"name": place.name, "latitude": place.latitude, "longitude": place.longitude}
                        for place in place_search(query)
                    ]
                except _PlaceSearchSuperseded:
                    return
                except GeocodeError:
                    payload["error"] = "Place lookup unavailable."
                GLib.idle_add(self._send_places, payload, token)
            finally:
                _place_search_admission.is_current = None

        self._start_worker(worker, name="airloom-geocode")

    def _send_places(self, payload: dict, token: RequestToken) -> bool:
        if not self._shutting_down and self.requests.is_current(token):
            self._send("places", payload)
        self.requests.finish(token)
        return GLib.SOURCE_REMOVE

    def _set_location_filter(self, message: dict) -> None:
        if self._shutting_down:
            return
        value = message.get("value")
        if value not in ("outdoor", "indoor", "both"):
            print(f"Airloom: ignored invalid location filter: {value!r}", file=sys.stderr)
            return
        candidate = deepcopy(self.store.data)
        candidate["location_filter"] = value
        try:
            self.store.apply(candidate)
        except OSError as exc:
            self._send(
                "error",
                {
                    "message": (
                        f"Could not save location filter: {exc}. Check disk space "
                        "and file permissions, then try again."
                    )
                },
            )
            return
        self.controller.set_config(self.store.data)
        self._send("config", self.store.public_config())
        self.refresh()

    def _save_settings(self, message: dict) -> None:
        if self._shutting_down:
            return
        previous_mode = self.store.data.get("home_mode")
        previous_home = (self.store.data.get("latitude"), self.store.data.get("longitude"))
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
                "confidence_filter": bool(message.get("confidence_filter")),
                "interface_scale": message.get(
                    "interface_scale", self.store.data.get("interface_scale", 1.0)
                ),
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
            error = f"Could not save preferences: {exc}"
            self._send("error", {"message": error})
            self._send(
                "settings-save-result",
                {
                    "request_id": message.get("request_id"),
                    "status": "error",
                    "error": error,
                },
            )
            return

        candidate = deepcopy(self.store.data)
        candidate.update(updates)
        api_key = str(message.get("api_key") or "").strip()
        if message.get("clear_api_key"):
            candidate["api_key"] = ""
        elif api_key:
            candidate["api_key"] = api_key
        try:
            self.store.apply(candidate)
        except OSError as exc:
            self._send(
                "settings-save-result",
                {
                    "request_id": message.get("request_id"),
                    "status": "error",
                    "error": (
                        f"Could not save preferences: {exc}. Check disk space and "
                        "file permissions, then try again."
                    ),
                },
            )
            return
        self.controller.set_config(self.store.data)
        if getattr(self, "webview", None):
            self.webview.set_zoom_level(self._interface_scale())
        if home_mode != "auto":
            self._clear_locator_focus_wait()
            if self.locator is not None:
                self.locator.cancel()
            self._home_name_generation += 1
            self.requests.invalidate("home-label")
        self._arm_auto_refresh()
        if self.title:
            self.title.set_subtitle(self.store.data["location_name"])
        self._send("config", self.store.public_config())
        self._send(
            "settings-save-result",
            {
                "request_id": message.get("request_id"),
                "status": "success",
                "error": None,
            },
        )
        if home_mode == "auto" and previous_mode != "auto":
            # The user just clicked Save in our UI, so the window is focused
            # and the permission dialog (if any) can appear immediately.
            self._start_locator()
        home_pinned = home_mode == "fixed" and (
            previous_mode != "fixed"
            or (updates["latitude"], updates["longitude"]) != previous_home
        )
        if home_pinned:
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
        if home_pinned:
            self._refresh_home()
        else:
            # Preference and filter changes keep the current viewport target.
            self.refresh()

    def _paint_cached_home(self) -> None:
        """First paint from the cache so launch never blocks on the network.
        The refresh that follows replaces it under the normal TTL rules."""
        if self.sensors or self.cache is None or not self.store.data.get("api_key"):
            return
        config = self.store.data
        bounds = bounds_around(config["latitude"], config["longitude"], config["radius_km"])
        cached = _sensors_from_rows(self.cache.sensors_in(bounds))
        if cached:
            favorites = set(config.get("favorites", []))
            for sensor in cached:
                sensor.favorite = sensor.sensor_id in favorites
                sensor.origin = "live"
                sensor.delivery = "cache"
            self.sensors = cached
            self._area_ids = {sensor.sensor_id for sensor in cached}
            self._monitoring_ids = set()
            self.last_origin = "live"
            self.last_stale = False
            self.coverage = _coverage_payload(bounds, bounds)
            self._send_sensor_state("PurpleAir · cached")

    def _set_publication_config(self) -> None:
        """Keep active or queued refresh work authorized after display changes."""
        self.controller.set_config(self.store.data)
        if self.refreshing or self.pending_fetch is not None:
            self.pending_fetch = self.controller.begin_refresh()

    def refresh(self, force: bool = False) -> None:
        """Refresh the current view, falling back to home before one exists."""
        if self._shutting_down:
            return
        self._start_fetch(self.controller.begin_refresh(force=force))

    def _refresh_home(self, force: bool = False) -> None:
        """Explicit recentering is separate from ordinary Refresh actions."""
        if self._shutting_down:
            return
        config = self.store.data
        center = (float(config["latitude"]), float(config["longitude"]))
        bounds = bounds_around(*center, float(config["radius_km"]))
        self.current_view = (bounds, center)
        self.controller.set_view(
            {
                "north": bounds.north,
                "west": bounds.west,
                "south": bounds.south,
                "east": bounds.east,
                "lat": center[0],
                "lon": center[1],
            }
        )
        self.refresh(force=force)

    def _start_fetch(self, request: RefreshRequest) -> None:
        if self._shutting_down or not self.webview:
            return
        if self.refreshing:
            # The descriptor is already registered, so its newer identity also
            # supersedes the active completion while only one worker runs.
            self.pending_fetch = request
            return
        self.refreshing = True
        bounds = cap_bounds(request.bounds, request.center)
        self._send("loading", {"active": True})
        config = dict(self.store.data)
        ttl = self._refresh_seconds()

        def worker() -> None:
            outcome = _fetch_readings(config, self.cache, request, bounds, ttl)
            GLib.idle_add(self._finish_refresh, outcome)

        self._start_worker(worker, name="airloom-refresh")

    def _finish_refresh(self, outcome: RefreshOutcome) -> bool:
        if self._shutting_down:
            return GLib.SOURCE_REMOVE
        try:
            decision = self.controller.complete_refresh(
                outcome.request,
                outcome.sensors,
                outcome.origin,
                outcome.delivery,
            )
            if decision.publish:
                for sensor in outcome.sensors:
                    sensor.origin, sensor.delivery = outcome.provenance.get(
                        sensor.sensor_id,
                        (outcome.origin, outcome.delivery),
                    )
                self.sensors = outcome.sensors
                self._area_ids = set(outcome.area_ids)
                self._monitoring_ids = set(outcome.monitoring_ids)
                self.last_source = outcome.source
                self.last_origin = outcome.origin
                self.last_stale = outcome.stale
                self.coverage = _coverage_payload(
                    outcome.request.bounds, outcome.bounds
                )
                self._send_sensor_state(outcome.source)
                if outcome.error:
                    self._send("error", {"message": outcome.error})
                self._check_alerts()
                if outcome.error is None:
                    # Failed live retrieval must not suppress the next retry.
                    self.view_bounds = outcome.bounds
                    self.view_fetched_at = time.monotonic()
                    self.coverage_generation = outcome.request.generation
                if self.selected_id is not None:
                    self._ensure_trend(self.selected_id)
        finally:
            self.refreshing = False
            self._send("loading", {"active": False})
            if self.pending_fetch is not None:
                pending, self.pending_fetch = self.pending_fetch, None
                self._start_fetch(pending)
        return GLib.SOURCE_REMOVE

    def _send_trend_state(
        self,
        sensor_id: int,
        token: RequestToken,
        status: str,
        reading_last_seen: int | None,
        trend: list | None = None,
    ) -> None:
        self._send(
            "trend",
            {
                "sensor_id": sensor_id,
                "request_id": token.request_id,
                "status": status,
                "trend": trend or [],
                "reading_last_seen": reading_last_seen,
            },
        )

    def _ensure_trend(self, sensor_id: int) -> None:
        """Attach one current trend request to a sensor reading.

        Repeated selection reuses identical in-flight work. A newer reading gets
        a new token, so neither an old request nor an old observation can publish.
        """
        if self._shutting_down:
            return
        sensor = next((s for s in self.sensors if s.sensor_id == sensor_id), None)
        reading_last_seen = sensor.last_seen if sensor is not None else None
        inflight = self._trend_inflight.get(sensor_id)
        if (
            sensor is not None
            and inflight is not None
            and inflight[1] == reading_last_seen
            and self.requests.is_current(inflight[0])
        ):
            return

        token = self.requests.begin(
            f"trend:{sensor_id}", generation=reading_last_seen or 0
        )
        if sensor is None:
            self._send_trend_state(sensor_id, token, "idle", None)
            self.requests.finish(token)
            return

        if not self.store.data.get("api_key"):
            status = "loaded" if sensor.trend else "unavailable"
            self._send_trend_state(
                sensor_id, token, status, reading_last_seen, sensor.trend
            )
            self.requests.finish(token)
            return

        cached = None
        if self.cache is not None:
            try:
                cached = self.cache.get_trend(sensor_id, self._refresh_seconds())
            except Exception as exc:  # noqa: BLE001 — caching is optional
                self._send(
                    "error",
                    {
                        "message": (
                            f"Could not read cached trend: {exc}. "
                            "Fetching a live trend instead."
                        )
                    },
                )
        if cached is not None:
            status = "loaded" if cached else "unavailable"
            if cached and sensor.trend != cached:
                sensor.trend = cached
                self._send_sensor_state()
            self._send_trend_state(
                sensor_id, token, status, reading_last_seen, cached
            )
            self.requests.finish(token)
            return

        self._trend_inflight[sensor_id] = (token, reading_last_seen)
        self._send_trend_state(sensor_id, token, "loading", reading_last_seen)
        api_key = self.store.data["api_key"]

        def worker() -> None:
            trend = None
            failed = False
            try:
                result = PurpleAirClient(api_key).fetch_rows(
                    show_only=[sensor_id], fields=TREND_FETCH_FIELDS
                )
                if result.rows:
                    trend = trend_from_values(result.rows[0])
            except PurpleAirError:
                failed = True
            except Exception:  # noqa: BLE001 — worker failures must reach a terminal state
                failed = True
            GLib.idle_add(
                self._finish_trend,
                sensor_id,
                reading_last_seen,
                token,
                trend,
                failed,
            )

        self._start_worker(worker, name="airloom-trend")

    def _finish_trend(
        self,
        sensor_id: int,
        reading_last_seen: int | None,
        token: RequestToken,
        trend: list | None,
        failed: bool,
    ) -> bool:
        inflight = self._trend_inflight.get(sensor_id)
        sensor = next((s for s in self.sensors if s.sensor_id == sensor_id), None)
        owns_inflight = inflight is not None and inflight[0] == token
        authorized = (
            not self._shutting_down
            and owns_inflight
            and self.requests.is_current(token)
            and sensor is not None
            and sensor.last_seen == reading_last_seen
        )
        if owns_inflight:
            # This callback settles the stored request even when its reading is
            # no longer publishable; a later selection must never reuse it.
            del self._trend_inflight[sensor_id]
            self.requests.finish(token)
        if not authorized:
            return GLib.SOURCE_REMOVE

        status = "error" if failed else "loaded" if trend else "unavailable"
        if status == "loaded":
            if self.cache is not None:
                try:
                    self.cache.store_trend(sensor_id, trend)
                except Exception as exc:  # noqa: BLE001 — live data remains valid
                    self._send(
                        "error",
                        {
                            "message": (
                                f"Could not cache trend: {exc}. "
                                "Showing the live result without trend caching."
                            )
                        },
                    )
            sensor.trend = trend
            self._send_sensor_state()
        self._send_trend_state(
            sensor_id, token, status, reading_last_seen, trend
        )
        return GLib.SOURCE_REMOVE

    def _send_sensor_state(self, source: str | None = None) -> None:
        visible, area, favorites = _presentation_collections(
            self.sensors,
            area_ids=self._area_ids,
            monitoring_ids=self._monitoring_ids,
            hidden_ids=self.store.hidden_ids(),
            confidence_filter=bool(self.store.data.get("confidence_filter", True)),
            location_filter=self.store.data.get("location_filter", "outdoor"),
        )
        # Hiding or filtering the selected sensor must not leave a dangling ID.
        if self.selected_id not in {sensor.sensor_id for sensor in visible}:
            self.selected_id = visible[0].sensor_id if visible else None
        if self.coverage is None:
            if self.current_view is not None:
                bounds = self.current_view[0]
            else:
                config = self.store.data
                bounds = bounds_around(
                    config["latitude"], config["longitude"], config["radius_km"]
                )
            self.coverage = _coverage_payload(bounds, bounds)
        payload = {
            "items": [
                _sensor_payload(
                    sensor,
                    remote_favorite=sensor.sensor_id not in self._area_ids,
                )
                for sensor in visible
            ],
            "area": [
                _sensor_payload(sensor, remote_favorite=False) for sensor in area
            ],
            "favorites": [
                _sensor_payload(
                    sensor,
                    remote_favorite=sensor.sensor_id not in self._area_ids,
                )
                for sensor in favorites
            ],
            "selected_id": self.selected_id,
            "source": source or self.last_source or ("PurpleAir live" if self.store.data.get("api_key") else "Demo data"),
            "stale": bool(self.last_stale),
            "coverage": dict(self.coverage),
            "config": self.store.public_config(),
        }
        self._send("sensors", payload)

    def _check_alerts(self) -> None:
        try:
            threshold = int(self.store.data.get("alert_threshold", 101))
        except (TypeError, ValueError):
            threshold = 101
        visible, _area, _favorites = _presentation_collections(
            self.sensors,
            area_ids=self._area_ids,
            monitoring_ids=self._monitoring_ids,
            hidden_ids=self.store.hidden_ids(),
            confidence_filter=bool(self.store.data.get("confidence_filter", True)),
            location_filter=self.store.data.get("location_filter", "outdoor"),
        )
        previous = dict(self.store.data.get("alert_states", {}))
        alerted, states = alert_transitions(
            visible,
            favorites={int(sensor_id) for sensor_id in self.store.data.get("favorites", [])},
            previous=previous,
            threshold=threshold,
            now=time.time(),
            origin=self.last_origin,
        )
        for sensor in alerted:
            notification = Notify.Notification.new(
                f"Air quality alert · {sensor.name[:80]}",
                f"AQI is now {sensor.aqi}. Open Airloom for health guidance.",
                APP_ID,
            )
            try:
                notification.show()
            except GLib.Error:
                pass
        if states != previous:
            candidate = deepcopy(self.store.data)
            candidate["alert_states"] = states
            try:
                self.store.apply(candidate)
            except Exception as exc:  # noqa: BLE001 — persistence is nonfatal here
                self._send(
                    "error",
                    {"message": f"Could not save alert state: {exc}"},
                )

    def _send(self, event: str, payload) -> None:
        if self._shutting_down or not self.webview:
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
        if self._shutting_down:
            reply({"ok": False, "error": "application is shutting down"})
            return

        def run_on_main_loop() -> bool:
            try:
                self._handle_debug_command(message, reply)
            except Exception as exc:  # noqa: BLE001 — async failures still need a reply
                reply({"ok": False, "error": f"dispatcher error: {exc}"})
            return GLib.SOURCE_REMOVE

        GLib.idle_add(run_on_main_loop)

    def _handle_debug_command(self, message: dict, reply) -> None:
        if self._shutting_down:
            reply({"ok": False, "error": "application is shutting down"})
            return
        cmd = message.get("cmd")
        if not isinstance(cmd, str):
            reply({"ok": False, "error": "debug command must be a string"})
            return
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
