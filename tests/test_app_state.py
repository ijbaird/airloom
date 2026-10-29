from __future__ import annotations

import sys
import tempfile
import types
import unittest
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

    def save(self):
        self.saves += 1


class FailingAlertStore(FakeStore):
    def save(self):
        self.saves += 1
        raise OSError("alert state disk full")


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


class ApplicationAdapterTestCase(unittest.TestCase):
    def make_app(self, store=None, cache=None):
        app = AirloomApplication.__new__(AirloomApplication)
        app.store = store or FakeStore()
        app.cache = cache
        app.controller = ApplicationController(app.store.data)
        app.sensors = []
        app._area_ids = set()
        app._monitoring_ids = set()
        app.selected_id = None
        app.refreshing = False
        app.pending_fetch = None
        app.current_view = None
        app.view_bounds = None
        app.view_fetched_at = 0.0
        app.last_source = None
        app.last_origin = "live"
        app.last_stale = False
        app.coverage = None
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
            [make_sensor(1)],
            delivery="cache",
            provenance={1: ("live", "cache")},
        )
        pending = app.controller.begin_refresh()
        app.pending_fetch = pending
        app.refreshing = True
        alerts = []
        app._check_alerts = lambda: alerts.append(True)

        app._finish_refresh(outcome)

        self.assertEqual(app.sensors, [previous])
        self.assertEqual(outcome.sensors[0].origin, "live")
        self.assertEqual(outcome.sensors[0].delivery, "network")
        self.assertEqual(self.sensor_payloads(app), [])
        self.assertEqual(alerts, [])
        self.assertEqual(app.trends, [])
        self.assertEqual(app.launched, [(pending,)])
        self.assertIsNone(app.pending_fetch)
        self.assertFalse(app.refreshing)

    def test_alert_save_failure_still_drains_pending_refresh(self):
        store = FailingAlertStore(favorites=[1], alert_states={"1": False})
        app = self.make_app(store=store)
        reading = make_sensor(1, favorite=True)
        outcome = self.make_outcome(app, [reading])
        app.pending_fetch = replace(outcome.request, request_id=999)
        app.refreshing = True

        with (
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
            [payload.get("message", "") for event, payload in app.sent if event == "error"][-1],
        )
        self.assertEqual(
            [payload for event, payload in app.sent if event == "loading"][-1],
            {"active": False},
        )


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
