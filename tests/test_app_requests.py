from __future__ import annotations

import io
import sys
import threading
import types
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from airloom import app as app_module
except (ImportError, ValueError):
    gi = types.ModuleType("gi")
    gi.require_version = lambda *_args: None
    repository = types.ModuleType("gi.repository")
    repository.Adw = SimpleNamespace(Application=object)
    repository.Gio = SimpleNamespace()
    repository.GLib = SimpleNamespace(
        Error=Exception,
        SOURCE_REMOVE=False,
        SOURCE_CONTINUE=True,
    )
    repository.Gtk = SimpleNamespace()
    repository.Notify = SimpleNamespace(Notification=object)
    repository.WebKit = SimpleNamespace()
    gi.repository = repository
    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = repository
    from airloom import app as app_module

from airloom import geocode as geocode_module
from airloom.app import AirloomApplication
from airloom.geocode import Place
from airloom.models import Sensor
from airloom.purpleair import FetchResult, PurpleAirError
from airloom.requests import RequestRegistry


CONFIG = {
    "api_key": "test-api-key",
    "latitude": 0.0,
    "longitude": 0.0,
    "location_name": "Original home",
    "home_mode": "auto",
    "radius_km": 55.5,
    "heatmap_threshold_km": 50.0,
    "refresh_minutes": 2,
    "alert_threshold": 101,
    "temperature_unit": "F",
    "confidence_filter": True,
    "location_filter": "outdoor",
    "favorites": [],
    "hidden": {},
    "alert_states": {},
}


def make_sensor(sensor_id=1, *, last_seen=100, trend=None):
    return Sensor(
        sensor_id=sensor_id,
        name=f"Sensor {sensor_id}",
        latitude=1.0,
        longitude=2.0,
        aqi=50,
        pm25=10.0,
        confidence=100,
        last_seen=last_seen,
        trend=[] if trend is None else trend,
    )


class FakeStore:
    def __init__(self, **updates):
        self.data = {**CONFIG, **updates}
        self.saves = 0

    def save(self):
        self.saves += 1

    def public_config(self):
        return {
            "home_mode": self.data["home_mode"],
            "location_name": self.data["location_name"],
        }

    def has_custom_location(self):
        return False


class FakeCache:
    def __init__(self):
        self.stored = []

    def get_trend(self, _sensor_id, _ttl):
        return None

    def store_trend(self, sensor_id, trend):
        self.stored.append((sensor_id, trend))


class FakeWindow:
    def __init__(self, active=True):
        self.active = active
        self.disconnected = []

    def is_active(self):
        return self.active

    def disconnect(self, handler_id):
        self.disconnected.append(handler_id)


class FakeLocator:
    def __init__(self):
        self.cancel_calls = 0

    def cancel(self):
        self.cancel_calls += 1


class QueuedThread:
    def __init__(self, *, target, args=(), kwargs=None, **_options):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.started = False

    def start(self):
        self.started = True

    def run(self):
        self.target(*self.args, **self.kwargs)


class ThreadQueue:
    def __init__(self):
        self.threads = []

    def factory(self, **kwargs):
        thread = QueuedThread(**kwargs)
        self.threads.append(thread)
        return thread


class IdleQueue:
    def __init__(self):
        self.callbacks = []

    def add(self, callback, *args):
        self.callbacks.append((callback, args))
        return len(self.callbacks)

    def run(self, index):
        callback, args = self.callbacks[index]
        return callback(*args)


class ApplicationRequestTestCase(unittest.TestCase):
    def make_app(self, *, sensors=(), home_mode="auto"):
        app = AirloomApplication.__new__(AirloomApplication)
        app.store = FakeStore(home_mode=home_mode)
        app.cache = FakeCache()
        app.controller = SimpleNamespace(set_config=lambda _config: None)
        app.requests = RequestRegistry()
        app.sensors = list(sensors)
        app.selected_id = None
        app._trend_inflight = {}
        app._home_name_generation = 0
        app._view_name_generation = 0
        app.current_view = None
        app.window = FakeWindow()
        app.title = None
        app.locator = None
        app._locator_focus_handler = None
        app._locator_focus_fallback = None
        app.sent = []
        app.sensor_state_sends = 0
        app.refreshed_homes = []
        app._send = lambda event, payload: app.sent.append((event, payload))
        app._send_sensor_state = lambda *_args, **_kwargs: setattr(
            app, "sensor_state_sends", app.sensor_state_sends + 1
        )
        app._refresh_home = lambda *args, **_kwargs: app.refreshed_homes.append(args)
        app._arm_auto_refresh = lambda: None
        app.refresh = lambda *args, **_kwargs: None
        return app

    def trend_events(self, app):
        return [payload for event, payload in app.sent if event == "trend"]

    def place_events(self, app):
        return [payload for event, payload in app.sent if event == "places"]


class TrendRequestTest(ApplicationRequestTestCase):
    def test_duplicate_selection_reuses_inflight_request_and_loads_once(self):
        sensor = make_sensor(last_seen=1234)
        app = self.make_app(sensors=[sensor])
        threads = ThreadQueue()
        idles = IdleQueue()
        trend = [{"label": "Now", "aqi": 50}]
        client = mock.Mock()
        client.fetch_rows.return_value = FetchResult([{"sensor_index": 1}], 1)

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "PurpleAirClient", return_value=client),
            mock.patch.object(app_module, "trend_from_values", return_value=trend),
        ):
            app._ensure_trend(1)
            app._ensure_trend(1)

            self.assertEqual(len(threads.threads), 1)
            self.assertEqual(client.fetch_rows.call_count, 0)
            loading = self.trend_events(app)
            self.assertEqual(len(loading), 1)
            self.assertEqual(loading[0]["sensor_id"], 1)
            self.assertEqual(loading[0]["status"], "loading")
            self.assertEqual(loading[0]["trend"], [])
            self.assertEqual(loading[0]["reading_last_seen"], 1234)

            threads.threads[0].run()
            idles.run(0)

        loaded = self.trend_events(app)[-1]
        self.assertEqual(loaded["status"], "loaded")
        self.assertEqual(loaded["trend"], trend)
        self.assertEqual(loaded["request_id"], loading[0]["request_id"])
        self.assertEqual(loaded["reading_last_seen"], 1234)
        self.assertEqual(sensor.trend, trend)
        self.assertEqual(client.fetch_rows.call_count, 1)

    def test_trend_error_is_terminal_and_allows_manual_retry(self):
        app = self.make_app(sensors=[make_sensor(last_seen=1234)])
        threads = ThreadQueue()
        idles = IdleQueue()
        client = mock.Mock()
        client.fetch_rows.side_effect = PurpleAirError("offline")

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "PurpleAirClient", return_value=client),
        ):
            app._ensure_trend(1)
            threads.threads[0].run()
            idles.run(0)
            request_id = self.trend_events(app)[0]["request_id"]

            self.assertEqual(
                self.trend_events(app)[-1],
                {
                    "sensor_id": 1,
                    "request_id": request_id,
                    "status": "error",
                    "trend": [],
                    "reading_last_seen": 1234,
                },
            )

            app._ensure_trend(1)

        self.assertEqual(len(threads.threads), 2)
        self.assertNotEqual(self.trend_events(app)[-1]["request_id"], request_id)
        self.assertEqual(self.trend_events(app)[-1]["status"], "loading")

    def test_empty_trend_is_unavailable_not_perpetually_loading(self):
        app = self.make_app(sensors=[make_sensor(last_seen=1234)])
        threads = ThreadQueue()
        idles = IdleQueue()
        client = mock.Mock()
        client.fetch_rows.return_value = FetchResult([], 1)

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "PurpleAirClient", return_value=client),
        ):
            app._ensure_trend(1)
            request_id = self.trend_events(app)[0]["request_id"]
            threads.threads[0].run()
            idles.run(0)

        self.assertEqual(
            self.trend_events(app)[-1],
            {
                "sensor_id": 1,
                "request_id": request_id,
                "status": "unavailable",
                "trend": [],
                "reading_last_seen": 1234,
            },
        )

    def test_missing_sensor_emits_idle_state(self):
        app = self.make_app()

        app._ensure_trend(99)

        self.assertEqual(len(self.trend_events(app)), 1)
        self.assertEqual(self.trend_events(app)[0]["sensor_id"], 99)
        self.assertEqual(self.trend_events(app)[0]["status"], "idle")
        self.assertEqual(self.trend_events(app)[0]["trend"], [])
        self.assertIsNone(self.trend_events(app)[0]["reading_last_seen"])
        self.assertIn("request_id", self.trend_events(app)[0])

    def test_old_reading_and_request_completions_cannot_publish(self):
        sensor = make_sensor(last_seen=100)
        app = self.make_app(sensors=[sensor])
        threads = ThreadQueue()
        idles = IdleQueue()
        client = mock.Mock()
        client.fetch_rows.return_value = FetchResult([{"sensor_index": 1}], 1)
        old_trend = [{"label": "Now", "aqi": 10}]
        new_trend = [{"label": "Now", "aqi": 80}]

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "PurpleAirClient", return_value=client),
            mock.patch.object(
                app_module, "trend_from_values", side_effect=[new_trend, old_trend]
            ),
        ):
            app._ensure_trend(1)
            sensor.last_seen = 200
            app._ensure_trend(1)
            self.assertEqual(len(threads.threads), 2)

            threads.threads[1].run()
            threads.threads[0].run()
            idles.run(0)
            idles.run(1)

        loaded = [
            payload for payload in self.trend_events(app)
            if payload["status"] == "loaded"
        ]
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["trend"], new_trend)
        self.assertEqual(loaded[0]["reading_last_seen"], 200)
        self.assertEqual(sensor.trend, new_trend)

    def test_unpublishable_completion_is_not_reused_after_reselection(self):
        sensor = make_sensor(last_seen=100)
        app = self.make_app(sensors=[sensor])
        threads = ThreadQueue()
        idles = IdleQueue()
        client = mock.Mock()
        client.fetch_rows.return_value = FetchResult([{"sensor_index": 1}], 1)

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "PurpleAirClient", return_value=client),
            mock.patch.object(
                app_module,
                "trend_from_values",
                return_value=[{"label": "Now", "aqi": 50}],
            ),
        ):
            app._ensure_trend(1)
            app.sensors = []
            threads.threads[0].run()
            idles.run(0)

            app.sensors = [sensor]
            app._ensure_trend(1)

        self.assertEqual(len(threads.threads), 2)
        self.assertEqual(self.trend_events(app)[-1]["status"], "loading")
        self.assertNotEqual(
            self.trend_events(app)[-1]["request_id"],
            self.trend_events(app)[0]["request_id"],
        )


class PlaceSearchRequestTest(ApplicationRequestTestCase):
    def test_place_results_echo_exact_request_identity_context_and_query(self):
        app = self.make_app()
        threads = ThreadQueue()
        idles = IdleQueue()
        raw_query = "  Paris  "

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(
                app_module,
                "place_search",
                return_value=[Place("Paris", 48.8566, 2.3522)],
            ) as search,
        ):
            app._on_place_search(
                {"request_id": "main-17", "context": "main", "query": raw_query}
            )
            threads.threads[0].run()
            idles.run(0)

        search.assert_called_once_with("Paris")
        self.assertEqual(
            self.place_events(app),
            [
                {
                    "request_id": "main-17",
                    "context": "main",
                    "query": raw_query,
                    "results": [
                        {"name": "Paris", "latitude": 48.8566, "longitude": 2.3522}
                    ],
                }
            ],
        )

    def test_obsolete_queued_search_is_dropped_before_network_io(self):
        app = self.make_app()
        threads = ThreadQueue()
        idles = IdleQueue()

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "place_search") as search,
        ):
            app._on_place_search(
                {"request_id": 1, "context": "main", "query": "old"}
            )
            app._on_place_search(
                {"request_id": 2, "context": "main", "query": "new"}
            )
            threads.threads[0].run()

        search.assert_not_called()
        self.assertEqual(idles.callbacks, [])
        self.assertEqual(self.place_events(app), [])

    def test_superseded_search_waiting_in_geocoder_queue_never_reaches_network(self):
        app = self.make_app()
        idles = IdleQueue()
        client = geocode_module.Geocoder(clock=lambda: 100.0, sleep=lambda _seconds: None)
        base_url = geocode_module._base_url()
        old_waiting = threading.Event()
        queue_lock = threading.Lock()
        queue_lock.acquire()

        class SignallingLock:
            def __enter__(self):
                old_waiting.set()
                return queue_lock.__enter__()

            def __exit__(self, *args):
                return queue_lock.__exit__(*args)

        client._search_lock = SignallingLock()
        real_thread = threading.Thread
        threads = []

        def recording_thread(**kwargs):
            thread = real_thread(**kwargs)
            threads.append(thread)
            return thread

        requests = []

        def empty_response(request, timeout=None):
            requests.append(request.full_url)
            return mock.MagicMock(
                __enter__=lambda _response: io.StringIO("[]"),
                __exit__=lambda _response, *_args: False,
            )

        with (
            mock.patch.object(geocode_module, "_default_geocoder", client),
            mock.patch.object(geocode_module, "_default_base_url", base_url),
            mock.patch.object(app_module.threading, "Thread", new=recording_thread),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(
                geocode_module.urllib.request, "urlopen", side_effect=empty_response
            ),
        ):
            try:
                app._on_place_search(
                    {"request_id": 1, "context": "main", "query": "old"}
                )
                self.assertTrue(old_waiting.wait(2), "old search did not enter geocoder queue")
                app._on_place_search(
                    {"request_id": 2, "context": "main", "query": "new"}
                )
            finally:
                if queue_lock.locked():
                    queue_lock.release()
                for thread in threads:
                    thread.join(5)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(requests), 1)
        self.assertIn("q=new", requests[0])

    def test_obsolete_search_response_cannot_publish(self):
        app = self.make_app()
        threads = ThreadQueue()
        idles = IdleQueue()

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "place_search", return_value=[]),
        ):
            app._on_place_search(
                {"request_id": 1, "context": "home", "query": "old"}
            )
            threads.threads[0].run()
            app._on_place_search(
                {"request_id": 2, "context": "home", "query": "new"}
            )
            idles.run(0)

        self.assertEqual(self.place_events(app), [])

    def test_main_and_home_searches_have_independent_lifetimes(self):
        app = self.make_app()
        threads = ThreadQueue()
        idles = IdleQueue()

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "place_search", return_value=[]),
        ):
            app._on_place_search(
                {"request_id": "m", "context": "main", "query": "main query"}
            )
            app._on_place_search(
                {"request_id": "h", "context": "home", "query": "home query"}
            )
            threads.threads[0].run()
            idles.run(0)

        self.assertEqual(len(self.place_events(app)), 1)
        self.assertEqual(self.place_events(app)[0]["request_id"], "m")


class ReverseLabelRequestTest(ApplicationRequestTestCase):
    def test_slow_home_a_cannot_reverse_newer_home_b(self):
        app = self.make_app()
        threads = ThreadQueue()
        idles = IdleQueue()

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(
                app_module, "reverse_geocode", side_effect=["A home", "B home"]
            ),
        ):
            app._on_location_fix(10.0, 20.0, 50.0)
            app._on_location_fix(30.0, 40.0, 50.0)
            threads.threads[0].run()
            threads.threads[1].run()
            idles.run(1)
            idles.run(0)

        self.assertEqual(app.store.data["location_name"], "B home")
        self.assertEqual(
            (app.store.data["latitude"], app.store.data["longitude"]),
            (30.0, 40.0),
        )
        labels = [
            payload["location_name"]
            for event, payload in app.sent
            if event == "config"
        ]
        self.assertEqual(labels, ["B home"])

    def test_view_label_requires_matching_coordinates_as_well_as_generation(self):
        app = self.make_app()
        app.current_view = (None, (30.0, 40.0))
        threads = ThreadQueue()
        idles = IdleQueue()

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "reverse_geocode", return_value="Old view"),
        ):
            app._start_view_label(10.0, 20.0)
            threads.threads[0].run()
            idles.run(0)

        self.assertEqual(
            [payload for event, payload in app.sent if event == "view-name"], []
        )


class LocationModeRequestTest(ApplicationRequestTestCase):
    def settings_message(self):
        return {
            "radius_km": 55.5,
            "heatmap_threshold_km": 50.0,
            "alert_threshold": 101,
            "home_mode": "fixed",
            "temperature_unit": "F",
            "confidence_filter": True,
            "refresh_minutes": 2,
            "home_lat": 30.0,
            "home_lon": 40.0,
            "location_name": "Pinned home",
        }

    def test_leaving_auto_cancels_focus_wait_and_inflight_location(self):
        app = self.make_app(home_mode="auto")
        locator = FakeLocator()
        app.locator = locator
        app._locator_focus_handler = 41
        app._locator_focus_fallback = 42

        with mock.patch.object(app_module.GLib, "source_remove") as source_remove:
            app._save_settings(self.settings_message())

        self.assertEqual(locator.cancel_calls, 1)
        self.assertEqual(app.window.disconnected, [41])
        source_remove.assert_called_once_with(42)
        self.assertIsNone(app._locator_focus_handler)
        self.assertIsNone(app._locator_focus_fallback)

    def test_focus_fallback_rechecks_mode_before_deferred_start(self):
        app = self.make_app(home_mode="fixed")
        app._locator_focus_fallback = 42
        app._start_locator = mock.Mock()

        app._on_locator_focus_fallback()

        app._start_locator.assert_not_called()

    def test_focus_signal_rechecks_mode_before_deferred_start(self):
        app = self.make_app(home_mode="fixed")
        app._clear_locator_focus_wait = mock.Mock()
        app._start_locator = mock.Mock()

        app._on_window_active_for_locator(app.window, None)

        app._start_locator.assert_not_called()

    def test_focus_wait_entry_does_nothing_in_fixed_mode(self):
        app = self.make_app(home_mode="fixed")
        app._start_locator = mock.Mock()

        app._start_locator_when_focused()

        app._start_locator.assert_not_called()
        self.assertIsNone(app._locator_focus_handler)
        self.assertIsNone(app._locator_focus_fallback)


class DebugDispatcherRequestTest(ApplicationRequestTestCase):
    def test_non_string_command_receives_immediate_error_reply(self):
        app = self.make_app()
        replies = []

        app._handle_debug_command({"cmd": []}, replies.append)

        self.assertEqual(len(replies), 1)
        self.assertIs(replies[0]["ok"], False)
        self.assertIn("command", replies[0]["error"])

    def test_async_handler_exception_becomes_error_reply(self):
        app = self.make_app()
        idles = IdleQueue()
        replies = []
        app._handle_debug_command = mock.Mock(side_effect=RuntimeError("boom"))

        with mock.patch.object(app_module.GLib, "idle_add", new=idles.add):
            app._dispatch_debug_command({"cmd": "ping"}, replies.append)
            idles.run(0)

        self.assertEqual(replies, [{"ok": False, "error": "dispatcher error: boom"}])


if __name__ == "__main__":
    unittest.main()
