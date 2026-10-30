from __future__ import annotations

import errno
import json
import sys
import tempfile
import time
import types
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    from airloom import app as app_module
except (ImportError, ValueError):
    # The adapter tests never instantiate GTK widgets. Provide just enough
    # namespace for app.py's class definitions on headless build hosts.
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

from airloom.app import AirloomApplication
from airloom.cache import AreaResult, CacheError, SensorCache
from airloom.controller import ApplicationController
from airloom.geography import Bounds
from airloom.models import Sensor
from airloom.purpleair import FetchResult, PurpleAirError
from airloom.store import Store


NOW = 1_800_000_000
VIEW = {
    "north": 20.0,
    "west": -30.0,
    "south": 10.0,
    "east": -10.0,
    "lat": 15.0,
    "lon": -20.0,
}
CONFIG = {
    "api_key": "test-api-key",
    "latitude": 0.0,
    "longitude": 0.0,
    "location_name": "Test home",
    "radius_km": 55.5,
    "refresh_minutes": 2,
    "favorites": [],
    "hidden": {},
    "confidence_filter": True,
    "location_filter": "outdoor",
    "alert_threshold": 101,
    "alert_states": {},
}


def make_sensor(
    sensor_id=1,
    *,
    indoor=False,
    favorite=False,
    aqi=120,
    confidence=100,
    last_seen=NOW - 60,
    origin="live",
    delivery="network",
):
    return Sensor(
        sensor_id=sensor_id,
        name=f"Sensor {sensor_id}",
        latitude=0.0,
        longitude=0.0,
        aqi=aqi,
        pm25=10.0,
        confidence=confidence,
        indoor=indoor,
        favorite=favorite,
        last_seen=last_seen,
        origin=origin,
        delivery=delivery,
    )


def make_row(sensor_id=1, *, indoor=False, last_seen=NOW - 60):
    return {
        "sensor_index": sensor_id,
        "name": f"Sensor {sensor_id}",
        "latitude": 0.0,
        "longitude": 0.0,
        "last_seen": last_seen,
        "location_type": 1 if indoor else 0,
        "humidity": 50.0,
        "temperature": 68.0,
        "pm1.0": 5.0,
        "pm2.5_cf_1": 10.0,
        "pm10.0": 12.0,
        "confidence": 100,
    }


class FakeStore:
    def __init__(self, **updates):
        self.data = {
            **CONFIG,
            "favorites": list(CONFIG["favorites"]),
            "hidden": dict(CONFIG["hidden"]),
            "alert_states": dict(CONFIG["alert_states"]),
            **updates,
        }
        self.saves = 0

    def hidden_ids(self):
        return {int(key) for key in self.data["hidden"]}

    def public_config(self):
        return {"location_filter": self.data["location_filter"]}

    def apply(self, candidate):
        self.saves += 1
        self.data = deepcopy(candidate)
        return self.data

    def save(self):
        self.apply(deepcopy(self.data))


class FakeCache:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def sensors_in(self, _bounds):
        return list(self.rows)


class FakeClient:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    def fetch_rows(self, **kwargs):
        self.calls.append(kwargs)
        return FetchResult(list(self.rows), 123)


class FakePolicyDecision:
    def __init__(self, uri):
        self.uri = uri
        self.used = False
        self.ignored = False

    def get_navigation_action(self):
        return self

    def get_request(self):
        return self

    def get_uri(self):
        return self.uri

    def use(self):
        self.used = True

    def ignore(self):
        self.ignored = True


class FakeWebView:
    def __init__(self, zoom_level=1.0):
        self.zoom_level = zoom_level
        self.zoom_levels = []
        self.loaded_uris = []

    def get_zoom_level(self):
        return self.zoom_level

    def set_zoom_level(self, scale):
        self.zoom_level = scale
        self.zoom_levels.append(scale)

    def load_uri(self, uri):
        self.loaded_uris.append(uri)


class FakePinchGesture:
    def __init__(self):
        self.state = None

    def get_bounding_box_center(self):
        return True, 40.0, 60.0

    def set_state(self, state):
        self.state = state


class ScriptMessage:
    def __init__(self, message):
        self.message = message

    def to_json(self, _options=0):
        return json.dumps(self.message)


class ApplicationAdapterTestCase(unittest.TestCase):
    def make_app(self, store=None, cache=None):
        app = AirloomApplication.__new__(AirloomApplication)
        app.store = store or FakeStore()
        app.cache = cache
        app.controller = ApplicationController(app.store.data)
        app._shutting_down = False
        app.sensors = []
        app._area_ids = set()
        app._monitoring_ids = set()
        app.selected_id = None
        app.refreshing = False
        app.active_fetch = None
        app.pending_fetch = None
        app.current_view = None
        app.view_bounds = None
        app.view_fetched_at = 0.0
        app.coverage_generation = 0
        app.last_source = None
        app.last_origin = "live"
        app.last_stale = False
        app.coverage = None
        app.window = SimpleNamespace()
        app.webview = None
        app.title = None
        app._cache_error = None
        app._shell_recovery_notice = None
        app._captured_notifications = None
        app.sent = []
        app.launched = []
        app.trends = []
        app._send = lambda event, payload: app.sent.append((event, payload))
        app._start_fetch = lambda *args, **_kwargs: app.launched.append(args)
        app._ensure_trend = lambda sensor_id: app.trends.append(sensor_id)
        app._start_view_label = lambda *_args: None
        return app

    def make_outcome(self, app, sensors, **updates):
        request = updates.pop("request", None) or app.controller.begin_refresh()
        values = {
            "request": request,
            "sensors": sensors,
            "area_ids": {sensor.sensor_id for sensor in sensors},
            "monitoring_ids": {
                sensor.sensor_id for sensor in sensors if sensor.favorite
            },
            "source": "PurpleAir live",
            "origin": "live",
            "delivery": "network",
            "error": None,
            "monitoring_error": None,
            "stale": False,
            "bounds": request.bounds,
            "capped": False,
            "provenance": {
                sensor.sensor_id: (sensor.origin, sensor.delivery)
                for sensor in sensors
            },
        }
        values.update(updates)
        return SimpleNamespace(**values)

    def sensor_payloads(self, app):
        return [payload for event, payload in app.sent if event == "sensors"]


class CachedStartupTest(ApplicationAdapterTestCase):
    def test_cached_startup_is_real_cache_delivery_but_not_stale_fallback(self):
        app = self.make_app(cache=FakeCache([make_row(7)]))

        app._paint_cached_home()

        payload = self.sensor_payloads(app)[0]
        self.assertIs(payload.get("stale"), False)
        self.assertEqual([item["id"] for item in payload.get("area", [])], [7])
        self.assertEqual(payload["items"][0]["origin"], "live")
        self.assertEqual(payload["items"][0]["delivery"], "cache")
        self.assertFalse(payload["items"][0]["remote_favorite"])
        self.assertIs(payload["coverage"]["capped"], False)


class PresentationPayloadTest(ApplicationAdapterTestCase):
    def test_opposite_type_favorites_obey_one_filter_and_remote_summary_inputs(self):
        local = make_sensor(1, favorite=True, delivery="cache")
        remote = make_sensor(2, indoor=True, favorite=True)
        app = self.make_app()
        app.sensors = [local, remote]
        app._area_ids = {1}
        app._monitoring_ids = {1, 2}
        app.store.data["favorites"] = [1, 2]

        app._send_sensor_state("PurpleAir live")
        outdoor = self.sensor_payloads(app)[-1]
        self.assertEqual([item["id"] for item in outdoor["items"]], [1])
        self.assertEqual([item["id"] for item in outdoor["area"]], [1])
        self.assertEqual([item["id"] for item in outdoor["favorites"]], [1])
        self.assertIs(outdoor["items"][0]["remote_favorite"], False)
        self.assertIs(outdoor["stale"], False)

        app.store.data["location_filter"] = "indoor"
        app._send_sensor_state()
        indoor = self.sensor_payloads(app)[-1]
        self.assertEqual([item["id"] for item in indoor["items"]], [2])
        self.assertEqual(indoor["area"], [])
        self.assertEqual([item["id"] for item in indoor["favorites"]], [2])
        self.assertIs(indoor["favorites"][0]["remote_favorite"], True)
        self.assertEqual(indoor["favorites"][0]["delivery"], "network")

    def test_capped_coverage_contains_actual_fetch_bounds(self):
        requested = Bounds(80.0, -170.0, -80.0, 170.0)
        fetched = Bounds(1.0, -1.0, -1.0, 1.0)

        coverage = app_module._coverage_payload(requested, fetched)

        self.assertEqual(
            coverage,
            {
                "north": 1.0,
                "west": -1.0,
                "south": -1.0,
                "east": 1.0,
                "capped": True,
            },
        )


class FetchAdapterTest(ApplicationAdapterTestCase):
    def test_stale_fallback_has_explicit_stale_real_cache_provenance(self):
        config = dict(CONFIG)
        controller = ApplicationController(config)
        request = controller.begin_refresh()
        cache = FakeCache([make_row(7)])

        with (
            mock.patch.object(app_module, "PurpleAirClient", return_value=FakeClient()),
            mock.patch.object(
                app_module, "fetch_area", side_effect=PurpleAirError("offline")
            ),
        ):
            outcome = app_module._fetch_readings(
                config, cache, request, request.bounds, 120
            )

        self.assertIs(outcome.stale, True)
        self.assertEqual(outcome.origin, "live")
        self.assertEqual(outcome.delivery, "cache")
        self.assertEqual(outcome.provenance, {7: ("live", "cache")})
        self.assertIn("Showing cached readings", outcome.error)

    def test_cached_area_and_network_favorite_keep_per_sensor_delivery(self):
        config = {**CONFIG, "favorites": [8]}
        controller = ApplicationController(config)
        request = controller.begin_refresh()
        cache = FakeCache()
        cache.fresh_sensors = lambda _ids, _ttl: {}
        area = AreaResult([make_row(7)], age=30.0, polled=False)

        with (
            mock.patch.object(app_module, "PurpleAirClient", return_value=FakeClient()),
            mock.patch.object(app_module, "fetch_area", return_value=area),
            mock.patch.object(
                app_module, "fetch_favorites", return_value=[make_row(8)]
            ),
        ):
            outcome = app_module._fetch_readings(
                config, cache, request, request.bounds, 120
            )

        self.assertIs(outcome.stale, False)
        self.assertEqual(outcome.area_ids, {7})
        self.assertEqual(outcome.monitoring_ids, {8})
        self.assertEqual(
            outcome.provenance,
            {7: ("live", "cache"), 8: ("live", "network")},
        )

    def test_favorite_failure_preserves_live_area_and_reports_monitoring_separately(self):
        app = self.make_app(store=FakeStore(favorites=[8]))
        request = app.controller.begin_refresh()
        cache = FakeCache()
        cache.fresh_sensors = lambda _ids, _ttl: {}
        area = AreaResult([make_row(7)], age=0.0, polled=True)

        with (
            mock.patch.object(app_module, "PurpleAirClient", return_value=FakeClient()),
            mock.patch.object(app_module, "fetch_area", return_value=area),
            mock.patch.object(
                app_module,
                "fetch_favorites",
                side_effect=PurpleAirError("favorites offline"),
            ),
        ):
            outcome = app_module._fetch_readings(
                app.store.data, cache, request, request.bounds, 120
            )
            app._finish_refresh(outcome)

        self.assertEqual([sensor.sensor_id for sensor in outcome.sensors], [7])
        self.assertEqual(outcome.area_ids, {7})
        self.assertEqual(outcome.monitoring_ids, set())
        self.assertEqual(outcome.origin, "live")
        self.assertEqual(outcome.delivery, "network")
        self.assertEqual(outcome.provenance, {7: ("live", "network")})
        self.assertIsNone(outcome.error)
        self.assertIn("favorites offline", outcome.monitoring_error)
        self.assertEqual(app.view_bounds, outcome.bounds)
        payload = self.sensor_payloads(app)[-1]
        self.assertEqual([item["id"] for item in payload["area"]], [7])
        self.assertEqual(payload["area"][0]["origin"], "live")
        self.assertEqual(payload["area"][0]["delivery"], "network")
        errors = [
            item["message"] for event, item in app.sent if event == "error"
        ]
        self.assertEqual(len(errors), 1)
        self.assertIn("favorite monitoring", errors[0].lower())
        self.assertIn("favorites offline", errors[0])

    def test_cache_open_failure_degrades_to_split_network_fetch_without_persistence(self):
        with mock.patch.object(
            app_module, "SensorCache", side_effect=CacheError("cache locked")
        ):
            cache, warning = app_module._open_sensor_cache()
        self.assertIsNone(cache)
        self.assertIn("without persistent caching", warning)

        config = dict(CONFIG)
        controller = ApplicationController(config)
        controller.set_view(
            {**VIEW, "west": 179.0, "east": -179.0, "lat": 0.0, "lon": 180.0}
        )
        request = controller.begin_refresh()
        client = FakeClient([make_row(7)])
        with mock.patch.object(app_module, "PurpleAirClient", return_value=client):
            outcome = app_module._fetch_readings(
                config, cache, request, request.bounds, 120
            )

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(
            [(call["bounds"].west, call["bounds"].east) for call in client.calls],
            [(179.0, 180.0), (-180.0, -179.0)],
        )
        self.assertEqual(outcome.provenance, {7: ("live", "network")})
        self.assertIs(outcome.stale, False)

    def test_persistent_area_fetch_accepts_antimeridian_and_splits_queries(self):
        config = dict(CONFIG)
        controller = ApplicationController(config)
        controller.set_view(
            {**VIEW, "west": 179.0, "east": -179.0, "lat": 0.0, "lon": 180.0}
        )
        request = controller.begin_refresh()
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            cache = SensorCache(Path(directory) / "cache.db")
            try:
                with mock.patch.object(
                    app_module, "PurpleAirClient", return_value=client
                ):
                    app_module._fetch_readings(
                        config, cache, request, request.bounds, 120
                    )
            finally:
                cache.close()

        area_calls = [call for call in client.calls if "bounds" in call]
        self.assertEqual(len(area_calls), 2)
        self.assertEqual(
            [(call["bounds"].west, call["bounds"].east) for call in area_calls],
            [(179.0, 180.0), (-180.0, -179.0)],
        )


class RefreshAdapterTest(ApplicationAdapterTestCase):
    def test_manual_and_automatic_refresh_target_current_view(self):
        app = self.make_app()
        app.controller.set_view(VIEW)
        app.current_view = (
            Bounds(20.0, -30.0, 10.0, -10.0),
            (15.0, -20.0),
        )

        app.refresh(force=True)
        app._auto_refresh()

        self.assertEqual(len(app.launched), 2)
        for call in app.launched:
            request = call[0]
            self.assertTrue(hasattr(request, "request_id"))
            self.assertEqual(request.bounds, Bounds(20.0, -30.0, 10.0, -10.0))
            self.assertEqual(request.center, (15.0, -20.0))

    def test_antimeridian_view_is_accepted(self):
        app = self.make_app()
        message = {
            **VIEW,
            "west": 179.0,
            "east": -179.0,
            "lat": 0.0,
            "lon": 180.0,
        }

        app._on_view_changed(message)

        self.assertEqual(len(app.launched), 1)
        request = app.launched[0][0]
        self.assertEqual(request.bounds.west, 179.0)
        self.assertEqual(request.bounds.east, -179.0)

    def test_returning_to_fresh_a_invalidates_active_b_without_fetching(self):
        app = self.make_app()
        original = make_sensor(99, aqi=20)
        app.sensors = [original]
        app._area_ids = {99}
        app.controller.set_view(VIEW)
        app.current_view = (
            Bounds(20.0, -30.0, 10.0, -10.0),
            (15.0, -20.0),
        )
        app.view_bounds = Bounds(20.0, -30.0, 10.0, -10.0)
        app.view_fetched_at = 1_000.0
        view_b = {
            "north": 40.0,
            "west": 10.0,
            "south": 30.0,
            "east": 30.0,
            "lat": 35.0,
            "lon": 20.0,
        }
        alerts = []
        app._check_alerts = lambda: alerts.append(True)

        with mock.patch.object(app_module.time, "monotonic", return_value=1_000.0):
            app._on_view_changed(view_b)
            request_b = app.launched[-1][0]
            app.refreshing = True
            app._on_view_changed(VIEW)
            outcome_b = self.make_outcome(
                app,
                [make_sensor(2)],
                request=request_b,
            )
            app._finish_refresh(outcome_b)

        self.assertEqual(len(app.launched), 1)
        self.assertEqual(app.sensors, [original])
        self.assertEqual(self.sensor_payloads(app), [])
        self.assertEqual(alerts, [])
        self.assertEqual(app.trends, [])

    def test_returning_to_cached_coverage_discards_pending_fetch(self):
        app = self.make_app()
        app.controller.set_view(VIEW)
        app.current_view = (
            Bounds(20.0, -30.0, 10.0, -10.0),
            (15.0, -20.0),
        )
        app.view_bounds = Bounds(20.0, -30.0, 10.0, -10.0)
        app.view_fetched_at = 1_000.0
        app.pending_fetch = app.controller.begin_refresh()

        with mock.patch.object(app_module.time, "monotonic", return_value=1_000.0):
            app._on_view_changed(VIEW)

        self.assertIsNone(app.pending_fetch)
        self.assertEqual(app.launched, [])

    def test_filter_change_invalidates_covered_view_before_delayed_completion(self):
        app = self.make_app()
        app.webview = object()
        app._start_worker = mock.Mock(return_value=True)
        app._start_fetch = lambda request: AirloomApplication._start_fetch(app, request)
        app.controller.set_view(VIEW)
        bounds = Bounds(20.0, -30.0, 10.0, -10.0)
        app.current_view = (bounds, (15.0, -20.0))
        app.view_bounds = bounds
        app.view_fetched_at = 1_000.0
        outdoor_request = app.controller.begin_refresh()
        app.refreshing = True

        with mock.patch.object(app_module.time, "monotonic", return_value=1_000.0):
            app._set_location_filter({"value": "indoor"})
            app._on_view_changed(VIEW)
            replacement = app.pending_fetch
            app._finish_refresh(
                self.make_outcome(
                    app,
                    [make_sensor(1)],
                    request=outdoor_request,
                )
            )

        self.assertIsNotNone(replacement)
        self.assertEqual(replacement.generation, outdoor_request.generation + 1)
        self.assertEqual(app._start_worker.call_count, 1)

    def test_favorite_change_reissues_active_refresh_under_committed_config(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "config.json")
            store.save()
            app = self.make_app(store=store)
            app.sensors = [make_sensor(1)]
            app._area_ids = {1}
            active = app.controller.begin_refresh()
            app.refreshing = True

            app._on_script_message(
                None, ScriptMessage({"action": "favorite", "id": 1})
            )
            replacement = app.pending_fetch
            app._finish_refresh(
                self.make_outcome(app, [make_sensor(2)], request=active)
            )

            self.assertIsNotNone(replacement)
            self.assertEqual(replacement.generation, active.generation + 1)
            self.assertEqual(app.launched, [(replacement,)])

    def test_hide_change_replaces_pending_refresh_under_committed_config(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "config.json")
            store.save()
            app = self.make_app(store=store)
            app.sensors = [make_sensor(1)]
            app._area_ids = {1}
            active = app.controller.begin_refresh()
            pending = app.controller.begin_refresh()
            app.refreshing = True
            app.pending_fetch = pending

            app._on_script_message(None, ScriptMessage({"action": "hide", "id": 1}))
            replacement = app.pending_fetch
            app._finish_refresh(
                self.make_outcome(app, [make_sensor(2)], request=active)
            )

            self.assertIsNot(replacement, pending)
            self.assertEqual(replacement.generation, pending.generation + 1)
            self.assertEqual(app.launched, [(replacement,)])

    def _real_fetch_app(self, store):
        app = self.make_app(store=store)
        app.webview = object()
        app._start_worker = mock.Mock(return_value=True)
        app._start_fetch = lambda request: AirloomApplication._start_fetch(app, request)
        app.sensors = [make_sensor(1)]
        app._area_ids = {1}
        return app

    def test_favorite_reissue_preserves_active_manual_refresh_force(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "config.json")
            store.save()
            app = self._real_fetch_app(store)
            app._on_script_message(None, ScriptMessage({"action": "refresh"}))
            self.assertEqual(app._start_worker.call_count, 1)

            app._on_script_message(
                None, ScriptMessage({"action": "favorite", "id": 1})
            )

            self.assertIsNotNone(app.pending_fetch)
            self.assertTrue(app.pending_fetch.force)

    def test_hide_reissue_preserves_queued_manual_refresh_force(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "config.json")
            store.save()
            app = self._real_fetch_app(store)
            app.refresh()
            app._on_script_message(None, ScriptMessage({"action": "refresh"}))
            queued = app.pending_fetch
            self.assertTrue(queued.force)

            app._on_script_message(None, ScriptMessage({"action": "hide", "id": 1}))
            replacement = app.pending_fetch

            self.assertIsNot(replacement, queued)
            self.assertGreater(replacement.request_id, queued.request_id)
            self.assertTrue(replacement.force)
            self.assertEqual(app._start_worker.call_count, 1)

    def test_reissue_after_unforced_work_stays_unforced(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "config.json")
            store.save()
            app = self._real_fetch_app(store)
            app.refresh()

            app._on_script_message(None, ScriptMessage({"action": "hide", "id": 1}))

            self.assertFalse(app.pending_fetch.force)

    def test_refresh_ensures_preserved_non_first_selection_trend(self):
        app = self.make_app()
        app.selected_id = 2
        outcome = self.make_outcome(
            app,
            [make_sensor(1, aqi=20), make_sensor(2, aqi=30)],
        )

        app._finish_refresh(outcome)

        self.assertEqual(app.selected_id, 2)
        self.assertEqual(app.trends, [2])

    def test_superseded_completion_never_publishes_alerts_or_starts_trends(self):
        app = self.make_app()
        previous = make_sensor(99, aqi=20)
        app.sensors = [previous]
        outcome = self.make_outcome(
            app,
            [make_sensor(1, favorite=True, origin="demo", delivery="cache")],
        )
        pending = app.controller.begin_refresh()
        app.pending_fetch = pending
        app.refreshing = True
        alerts = []
        app._check_alerts = lambda: alerts.append(True)

        app._finish_refresh(outcome)

        self.assertEqual(app.sensors, [previous])
        self.assertTrue(outcome.sensors[0].favorite)
        self.assertEqual(outcome.sensors[0].origin, "demo")
        self.assertEqual(outcome.sensors[0].delivery, "cache")
        self.assertEqual(self.sensor_payloads(app), [])
        self.assertEqual(alerts, [])
        self.assertEqual(app.trends, [])
        self.assertEqual(app.launched, [(pending,)])
        self.assertIsNone(app.pending_fetch)
        self.assertFalse(app.refreshing)

    def test_alert_permission_and_space_failures_preserve_state_and_drain_pending_refresh(self):
        failures = (
            PermissionError(errno.EACCES, "Permission denied"),
            OSError(errno.ENOSPC, "No space left on device"),
        )
        for failure in failures:
            with self.subTest(error=failure.errno), tempfile.TemporaryDirectory() as directory:
                store = Store(Path(directory) / "config.json")
                store.data.update({"favorites": [1], "alert_states": {"1": False}})
                store.save()
                previous = deepcopy(store.data)
                app = self.make_app(store=store)
                reading = make_sensor(1, favorite=True)
                outcome = self.make_outcome(app, [reading])
                app.pending_fetch = replace(outcome.request, request_id=999)
                app.refreshing = True

                with (
                    mock.patch.object(store, "_write", side_effect=failure),
                    mock.patch.object(app_module.time, "time", return_value=NOW),
                    mock.patch.object(app_module.Notify, "Notification") as notification_type,
                ):
                    notification_type.new.return_value.show.return_value = None
                    app._finish_refresh(outcome)

                self.assertEqual(len(app.launched), 1)
                self.assertEqual(app.launched[0][0].request_id, 999)
                self.assertIsNone(app.pending_fetch)
                self.assertFalse(app.refreshing)
                self.assertEqual(len(self.sensor_payloads(app)), 1)
                self.assertIn(
                    "Could not save alert state",
                    [
                        payload.get("message", "")
                        for event, payload in app.sent
                        if event == "error"
                    ][-1],
                )
                self.assertEqual(
                    [payload for event, payload in app.sent if event == "loading"][-1],
                    {"active": False},
                )
                self.assertEqual(store.data, previous)
                self.assertEqual(Store(store.path).data, previous)


class ShellPolicyTest(ApplicationAdapterTestCase):
    def decide(self, app, uri):
        decision = FakePolicyDecision(uri)
        decision_types = SimpleNamespace(
            NAVIGATION_ACTION="navigation",
            NEW_WINDOW_ACTION="new-window",
        )
        with mock.patch.object(
            app_module.WebKit, "PolicyDecisionType", decision_types, create=True
        ):
            handled = app._on_decide_policy(None, decision, "navigation")
        return handled, decision

    def test_unrelated_file_navigation_is_rejected(self):
        app = self.make_app()

        handled, decision = self.decide(app, "file:///tmp/untrusted.html")

        self.assertTrue(handled)
        self.assertTrue(decision.ignored)
        self.assertFalse(decision.used)

    def test_bundled_entry_document_is_explicitly_allowed(self):
        app = self.make_app()
        entry_uri = (app_module.RESOURCE_DIR / "index.html").as_uri()

        handled, decision = self.decide(app, entry_uri)

        self.assertTrue(handled)
        self.assertTrue(decision.used)
        self.assertFalse(decision.ignored)

    def test_approved_external_links_open_outside_the_bridge_webview(self):
        app = self.make_app()
        for uri in (
            "https://www.openstreetmap.org/copyright",
            "HTTP://example.com/legacy",
        ):
            with self.subTest(uri=uri):
                launcher = mock.Mock()
                launcher_type = SimpleNamespace(new=mock.Mock(return_value=launcher))
                with mock.patch.object(
                    app_module.Gtk, "UriLauncher", launcher_type, create=True
                ):
                    handled, decision = self.decide(app, uri)

                self.assertTrue(handled)
                self.assertTrue(decision.ignored)
                self.assertFalse(decision.used)
                launcher_type.new.assert_called_once_with(uri)
                launcher.launch.assert_called_once_with(app.window, None, None)


class ShellRecoveryTest(ApplicationAdapterTestCase):
    def test_load_failure_reports_recoverable_status_and_keeps_error_page(self):
        app = self.make_app()
        handler = getattr(app, "_on_load_failed", None)
        self.assertIsNotNone(handler)

        handled = handler(None, None, "file:///missing/index.html", "read failed")

        self.assertFalse(handled)
        self.assertEqual(
            app.sent[-1],
            (
                "shell-status",
                {
                    "status": "error",
                    "message": mock.ANY,
                    "can_reload": True,
                },
            ),
        )
        message = app.sent[-1][1]["message"]
        self.assertIn("read failed", message)
        self.assertIn("reload", message.lower())

    def test_reload_shell_action_loads_only_the_bundled_entry(self):
        app = self.make_app()
        app.webview = FakeWebView()
        entry_uri = (app_module.RESOURCE_DIR / "index.html").as_uri()

        app._on_script_message(None, ScriptMessage({"action": "reload-shell"}))

        self.assertEqual(app.webview.loaded_uris, [entry_uri])

    def test_web_process_termination_reloads_and_reports_after_ready(self):
        app = self.make_app()
        app.webview = FakeWebView()
        entry_uri = (app_module.RESOURCE_DIR / "index.html").as_uri()
        handler = getattr(app, "_on_web_process_terminated", None)
        self.assertIsNotNone(handler)

        handler(None, "crashed")
        app._on_script_message(None, ScriptMessage({"action": "ready"}))

        self.assertEqual(app.webview.loaded_uris, [entry_uri])
        statuses = [
            payload for event, payload in app.sent if event == "shell-status"
        ]
        self.assertEqual(statuses[-1]["status"], "error")
        self.assertIn("reloaded", statuses[-1]["message"].lower())
        self.assertTrue(statuses[-1]["can_reload"])


class DebugFakeWiringTest(ApplicationAdapterTestCase):
    def setUp(self):
        original = app_module._purpleair_client_factory
        self.addCleanup(setattr, app_module, "_purpleair_client_factory", original)
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.fixture = Path(self._tempdir.name) / "purpleair.json"
        self.fixture.write_text('{"rows": []}', encoding="utf-8")
        self.environ = {
            "AIRLOOM_DEBUG_FAKE_PURPLEAIR": str(self.fixture),
            "AIRLOOM_DEBUG_CAPTURE_NOTIFICATIONS": "1",
        }

    def test_fakes_stay_inert_outside_debug_mode(self):
        app = self.make_app()
        app.debug_mode = False
        app._install_debug_fakes(self.environ)
        self.assertIsNone(app_module._purpleair_client_factory)
        self.assertIsInstance(app_module._new_purpleair_client("key"), app_module.PurpleAirClient)
        self.assertIsNone(app._captured_notifications)

    def test_fakes_stay_inert_when_the_debug_module_is_stripped(self):
        app = self.make_app()
        app.debug_mode = True
        with mock.patch.object(app_module, "debug_purpleair_factory", None):
            app._install_debug_fakes(self.environ)
        self.assertIsNone(app_module._purpleair_client_factory)
        self.assertIsInstance(app_module._new_purpleair_client("key"), app_module.PurpleAirClient)

    def test_debug_mode_installs_the_fixture_client_and_notification_capture(self):
        app = self.make_app()
        app.debug_mode = True
        app._install_debug_fakes(self.environ)
        client = app_module._new_purpleair_client("fake-key")
        self.assertEqual(type(client).__name__, "FakePurpleAirClient")
        self.assertEqual(app._captured_notifications, [])

    def test_captured_alerts_never_reach_libnotify(self):
        app = self.make_app(store=FakeStore(favorites=[1], alert_threshold=101))
        app._captured_notifications = []
        app.sensors = [make_sensor(1, aqi=180, last_seen=int(time.time()) - 60)]
        app._area_ids = {1}
        notify = SimpleNamespace(Notification=SimpleNamespace(new=mock.Mock(side_effect=AssertionError)))
        with mock.patch.object(app_module, "Notify", notify):
            app._check_alerts()
        self.assertEqual([item["sensor_id"] for item in app._captured_notifications], [1])
        self.assertIn("AQI is now 180", app._captured_notifications[0]["body"])

    def test_notifications_debug_command_reports_captures(self):
        app = self.make_app()
        app._captured_notifications = [{"sensor_id": 1, "title": "t", "body": "b"}]
        replies = []
        app._handle_debug_command({"cmd": "notifications"}, replies.append)
        self.assertEqual(replies, [{"ok": True, "result": [{"sensor_id": 1, "title": "t", "body": "b"}]}])


class DebugResizeTest(ApplicationAdapterTestCase):
    def test_resize_sets_the_window_size_and_lifts_the_minimum_for_narrow_probes(self):
        app = self.make_app()
        app.window = mock.Mock()
        replies = []
        app._handle_debug_command({"cmd": "resize", "width": 360, "height": 700}, replies.append)
        self.assertEqual(replies, [{"ok": True, "result": {"width": 360, "height": 700}}])
        app.window.set_size_request.assert_called_once_with(360, 540)
        app.window.set_default_size.assert_called_once_with(360, 700)

    def test_resize_never_reaches_the_window_with_invalid_params(self):
        app = self.make_app()
        app.window = mock.Mock()
        replies = []
        app._handle_debug_command({"cmd": "resize", "width": 50, "height": 700}, replies.append)
        self.assertFalse(replies[0]["ok"])
        app.window.set_default_size.assert_not_called()


class FakeGtkSettings:
    def __init__(self, animations):
        self.props = SimpleNamespace(gtk_enable_animations=animations)
        self.handlers = []

    def connect(self, signal, handler):
        self.handlers.append((signal, handler))
        return len(self.handlers)


class MotionPreferenceAdapterTest(ApplicationAdapterTestCase):
    def _with_settings(self, settings):
        gtk = SimpleNamespace(Settings=SimpleNamespace(get_default=lambda: settings))
        return mock.patch.object(app_module, "Gtk", gtk)

    def motion_events(self, app):
        return [payload for event, payload in app.sent if event == "motion"]

    def test_ready_publishes_disabled_system_animations_as_reduced_motion(self):
        app = self.make_app()
        app._paint_cached_home = lambda: None
        app.refresh = lambda *args, **kwargs: None
        with self._with_settings(FakeGtkSettings(False)):
            app._on_script_message(None, ScriptMessage({"action": "ready"}))
        self.assertEqual(self.motion_events(app), [{"reduced": True}])

    def test_ready_publishes_enabled_animations_and_tolerates_no_settings(self):
        for settings, expected in ((FakeGtkSettings(True), False), (None, False)):
            with self.subTest(settings=settings):
                app = self.make_app()
                app._paint_cached_home = lambda: None
                app.refresh = lambda *args, **kwargs: None
                with self._with_settings(settings):
                    app._on_script_message(None, ScriptMessage({"action": "ready"}))
                self.assertEqual(self.motion_events(app), [{"reduced": expected}])

    def test_system_animation_changes_are_republished_live(self):
        settings = FakeGtkSettings(True)
        app = self.make_app()
        with self._with_settings(settings):
            app._watch_motion_preference()
            self.assertEqual([signal for signal, _ in settings.handlers],
                             ["notify::gtk-enable-animations"])
            settings.props.gtk_enable_animations = False
            settings.handlers[0][1](settings, None)
            settings.props.gtk_enable_animations = True
            settings.handlers[0][1](settings, None)
        self.assertEqual(self.motion_events(app), [{"reduced": True}, {"reduced": False}])


class InterfaceScaleAdapterTest(ApplicationAdapterTestCase):
    def test_all_supported_scales_persist_and_apply_to_the_webview(self):
        for scale in (1.0, 1.25, 1.5):
            with self.subTest(scale=scale), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                store = Store(path)
                starting_scale = 1.5 if scale == 1.0 else 1.0
                store.data["interface_scale"] = starting_scale
                store.save()
                app = self.make_app(store=store)
                app.webview = FakeWebView(starting_scale)
                app._arm_auto_refresh = lambda: None
                app.refresh = lambda *args, **kwargs: None

                app._save_settings(
                    {
                        "radius_km": 55.5,
                        "heatmap_threshold_km": 40.0,
                        "alert_threshold": 101,
                        "home_mode": "auto",
                        "temperature_unit": "F",
                        "confidence_filter": True,
                        "refresh_minutes": 2,
                        "interface_scale": scale,
                    }
                )

                self.assertEqual(Store(path).data["interface_scale"], scale)
                self.assertEqual(app.webview.zoom_level, scale)

    def test_save_accepts_interface_size_numbers_from_the_selector(self):
        # JSON turns the selector's 100% (Number 1) into a Python int.
        for sent, expected in ((1, 1.0), (1.25, 1.25), (1.5, 1.5), ("1.25", 1.25)):
            with self.subTest(sent=sent), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                store = Store(path)
                store.data["interface_scale"] = 1.5 if expected != 1.5 else 1.0
                store.save()
                app = self.make_app(store=store)
                app.webview = FakeWebView(store.data["interface_scale"])
                app._arm_auto_refresh = lambda: None
                app.refresh = lambda *args, **kwargs: None

                app._on_script_message(None, ScriptMessage({
                    "action": "save-settings", "request_id": 4,
                    "radius_km": 22, "heatmap_threshold_km": 40, "alert_threshold": 101,
                    "refresh_minutes": 2, "temperature_unit": "F", "home_mode": "auto",
                    "interface_scale": sent,
                }))

                results = [p for e, p in app.sent if e == "settings-save-result"]
                self.assertEqual(results[-1]["status"], "success")
                self.assertEqual(Store(path).data["interface_scale"], expected)
                self.assertEqual(app.webview.zoom_level, expected)

    def test_save_rejects_unsupported_interface_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            store.data["interface_scale"] = 1.25
            store.save()
            app = self.make_app(store=store)
            app.webview = FakeWebView(1.25)
            app._arm_auto_refresh = lambda: None
            app.refresh = lambda *args, **kwargs: None

            app._on_script_message(None, ScriptMessage({
                "action": "save-settings", "request_id": 5,
                "radius_km": 22, "heatmap_threshold_km": 40, "alert_threshold": 101,
                "refresh_minutes": 2, "temperature_unit": "F", "home_mode": "auto",
                "interface_scale": 3,
            }))

            results = [p for e, p in app.sent if e == "settings-save-result"]
            self.assertEqual(results[-1]["status"], "error")
            self.assertEqual(Store(path).data["interface_scale"], 1.25)

    def test_native_pinch_uses_document_coordinates_at_every_interface_scale(self):
        cases = (
            (1.0, 40.0, 60.0),
            (1.25, 32.0, 48.0),
            (1.5, 26.666666666666668, 40.0),
        )
        for interface_scale, expected_x, expected_y in cases:
            with self.subTest(interface_scale=interface_scale):
                app = self.make_app(
                    store=FakeStore(interface_scale=interface_scale)
                )
                app.webview = FakeWebView(interface_scale)
                gesture = FakePinchGesture()
                sequence_state = SimpleNamespace(CLAIMED=object())
                with mock.patch.object(
                    app_module.Gtk, "EventSequenceState", sequence_state, create=True
                ):
                    app._on_native_pinch_begin(gesture, None)
                    app._on_native_pinch_scale(gesture, 1.4)

                self.assertEqual(
                    app.sent,
                    [
                        (
                            "pinch",
                            {
                                "phase": "begin",
                                "scale": 1.0,
                                "x": expected_x,
                                "y": expected_y,
                            },
                        ),
                        (
                            "pinch",
                            {
                                "phase": "change",
                                "scale": 1.4,
                                "x": expected_x,
                                "y": expected_y,
                            },
                        ),
                    ],
                )

    def test_native_map_pinch_never_replaces_the_persisted_interface_scale(self):
        for scale in (1.0, 1.25, 1.5):
            with self.subTest(scale=scale):
                app = self.make_app(store=FakeStore(interface_scale=scale))
                app.webview = FakeWebView(scale)
                gesture = FakePinchGesture()
                claimed = object()
                sequence_state = SimpleNamespace(CLAIMED=claimed)
                with mock.patch.object(
                    app_module.Gtk, "EventSequenceState", sequence_state, create=True
                ):
                    app._on_native_pinch_begin(gesture, None)
                    app._on_native_pinch_scale(gesture, 1.4)

                # If WebKit still emits a page-scale notification, restore the
                # interface preference rather than confusing it with map zoom.
                app.webview.zoom_level = 1.75
                app._on_zoom_level_changed(app.webview, None)

                self.assertIs(gesture.state, claimed)
                self.assertEqual(
                    [event for event, _payload in app.sent], ["pinch", "pinch"]
                )
                self.assertEqual(app.webview.zoom_level, scale)


class DemoPublicationTest(ApplicationAdapterTestCase):
    CENTERS = ((0.0, 179.99), (0.0, -179.99), (89.99, 0.0), (-89.99, 0.0), (90.0, 180.0))

    def test_demo_publication_near_geographic_limits_has_canonical_coordinates(self):
        for latitude, longitude in self.CENTERS:
            with self.subTest(center=(latitude, longitude)):
                store = FakeStore(
                    api_key="", latitude=latitude, longitude=longitude,
                    location_filter="both",
                )
                app = self.make_app(store=store)
                request = app.controller.begin_refresh()

                outcome = app_module._fetch_readings(
                    app.store.data, None, request, request.bounds, 120
                )
                app._finish_refresh(outcome)

                payload = self.sensor_payloads(app)[-1]
                self.assertEqual(len(payload["items"]), 18)
                for item in payload["items"]:
                    self.assertTrue(-90.0 <= item["latitude"] <= 90.0, item)
                    self.assertTrue(-180.0 <= item["longitude"] <= 180.0, item)


class AlertAdapterTest(ApplicationAdapterTestCase):
    def test_only_fresh_real_crossing_alerts_once(self):
        cases = (
            ("demo", make_sensor(1, favorite=True, origin="demo"), "demo", False, 0),
            (
                "stale",
                make_sensor(1, favorite=True, last_seen=NOW - 7201, delivery="cache"),
                "live",
                False,
                0,
            ),
            (
                "fresh cached real",
                make_sensor(1, favorite=True, delivery="cache"),
                "live",
                True,
                1,
            ),
        )
        for name, reading, origin, should_save, notification_count in cases:
            with self.subTest(name=name):
                store = FakeStore(favorites=[1], alert_states={"1": False})
                app = self.make_app(store=store)
                app.sensors = [reading]
                app._area_ids = {1}
                app._monitoring_ids = {1}
                app.last_origin = origin
                with (
                    mock.patch.object(app_module.time, "time", return_value=NOW),
                    mock.patch.object(app_module.Notify, "Notification") as notification_type,
                ):
                    notification_type.new.return_value.show.return_value = None
                    app._check_alerts()
                    app._check_alerts()

                self.assertEqual(notification_type.new.call_count, notification_count)
                self.assertEqual(store.data["alert_states"], {"1": should_save})
                self.assertEqual(store.saves, int(should_save))


if __name__ == "__main__":
    unittest.main()
