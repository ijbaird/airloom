from __future__ import annotations

import errno
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError

from airloom.controller import (
    ApplicationController,
    RefreshDecision,
    RefreshOutcome,
    RefreshRequest,
)
from airloom.geography import Bounds
from airloom.models import Sensor
from airloom.requests import RequestRegistry


HOME = {
    "latitude": 0.0,
    "longitude": 0.0,
    "radius_km": 55.5,
    "favorites": [7, 11],
    "hidden": {},
    "confidence_filter": True,
    "location_filter": "outdoor",
}

VIEW = {
    "north": 20.0,
    "west": -30.0,
    "south": 10.0,
    "east": -10.0,
    "lat": 15.0,
    "lon": -20.0,
}
OTHER = {**VIEW, "north": 40.0, "south": 30.0, "lat": 35.0}


def make_sensor(sensor_id, *, indoor=False, confidence=100):
    return Sensor(
        sensor_id=sensor_id,
        name=f"Sensor {sensor_id}",
        latitude=15.0,
        longitude=-20.0,
        aqi=50,
        pm25=10.0,
        confidence=confidence,
        indoor=indoor,
    )


class RefreshValueTest(unittest.TestCase):
    def test_refresh_values_are_frozen(self):
        request = RefreshRequest(
            request_id=1,
            generation=0,
            bounds=Bounds(1.0, -1.0, -1.0, 1.0),
            center=(0.0, 0.0),
            force=False,
            favorite_ids=(7,),
        )
        decision = RefreshDecision(True, (), (7,))

        with self.assertRaises(FrozenInstanceError):
            request.force = True
        with self.assertRaises(FrozenInstanceError):
            decision.publish = False


class RefreshTargetingTest(unittest.TestCase):
    def test_manual_and_automatic_refresh_use_the_current_view(self):
        controller = ApplicationController(HOME)
        controller.set_view(VIEW)

        manual = controller.begin_refresh(force=True)
        automatic = controller.begin_refresh()

        for request in (manual, automatic):
            with self.subTest(request_id=request.request_id):
                self.assertEqual(
                    request.bounds,
                    Bounds(20.0, -30.0, 10.0, -10.0),
                )
                self.assertEqual(request.center, (15.0, -20.0))
                self.assertEqual(request.favorite_ids, (7, 11))
        self.assertTrue(manual.force)
        self.assertFalse(automatic.force)
        self.assertEqual((manual.request_id, automatic.request_id), (1, 2))

    def test_refresh_falls_back_to_home_before_any_view_exists(self):
        controller = ApplicationController(HOME)

        request = controller.begin_refresh()

        self.assertEqual(request.bounds, Bounds(0.5, -0.5, -0.5, 0.5))
        self.assertEqual(request.center, (0.0, 0.0))
        self.assertEqual(request.generation, 0)


class RefreshAuthorizationTest(unittest.TestCase):
    def setUp(self):
        self.controller = ApplicationController(HOME)
        self.controller.set_view(VIEW)

    def test_slow_a_cannot_publish_after_b_begins(self):
        request_a = self.controller.begin_refresh()
        request_b = self.controller.begin_refresh()

        decision_a = self.controller.complete_refresh(
            request_a,
            [make_sensor(1)],
            "live",
            "network",
        )
        decision_b = self.controller.complete_refresh(
            request_b,
            [make_sensor(2)],
            "live",
            "network",
        )

        self.assertEqual(decision_a, RefreshDecision(False, (), ()))
        self.assertTrue(decision_b.publish)
        self.assertEqual([sensor.sensor_id for sensor in decision_b.sensors], [2])

    def test_config_change_supersedes_the_inflight_request(self):
        request_a = self.controller.begin_refresh()

        self.controller.set_config({**HOME, "radius_km": 22.2})
        decision_a = self.controller.complete_refresh(
            request_a,
            [make_sensor(1)],
            "live",
            "network",
        )
        request_b = self.controller.begin_refresh()

        self.assertEqual(decision_a, RefreshDecision(False, (), ()))
        self.assertEqual(request_b.generation, request_a.generation + 1)

    def test_filter_change_supersedes_a_and_filters_b_before_publishing(self):
        request_a = self.controller.begin_refresh()
        self.controller.set_config({**HOME, "location_filter": "indoor"})

        decision_a = self.controller.complete_refresh(
            request_a,
            [make_sensor(1)],
            "live",
            "network",
        )
        request_b = self.controller.begin_refresh()
        indoor = make_sensor(2, indoor=True)
        decision_b = self.controller.complete_refresh(
            request_b,
            [make_sensor(1), indoor],
            "live",
            "cache",
        )

        self.assertEqual(decision_a, RefreshDecision(False, (), ()))
        self.assertEqual(decision_b.sensors, (indoor,))
        self.assertEqual(decision_b.trend_ids, (2,))
        self.assertEqual((indoor.origin, indoor.delivery), ("live", "cache"))

    def test_cached_superseded_results_neither_publish_nor_start_trends(self):
        request_a = self.controller.begin_refresh()
        self.controller.begin_refresh()

        decision = self.controller.complete_refresh(
            request_a,
            [make_sensor(7)],
            "live",
            "cache",
        )

        self.assertFalse(decision.publish)
        self.assertEqual(decision.sensors, ())
        self.assertEqual(decision.trend_ids, ())

    def test_latest_b_publishes_and_requests_its_trend_only_once(self):
        self.controller.begin_refresh()
        request_b = self.controller.begin_refresh()
        reading = make_sensor(7)

        first = self.controller.complete_refresh(
            request_b,
            [reading],
            "live",
            "network",
        )
        duplicate = self.controller.complete_refresh(
            request_b,
            [reading],
            "live",
            "network",
        )

        self.assertEqual(first, RefreshDecision(True, (reading,), (7,)))
        self.assertTrue(reading.favorite)
        self.assertEqual(duplicate, RefreshDecision(False, (), ()))


NOW = 1_800_000_000
FULL_CONFIG = {
    **HOME,
    "api_key": "key",
    "location_name": "Home",
    "home_mode": "auto",
    "heatmap_threshold_km": 40.0,
    "interface_scale": 1.0,
    "temperature_unit": "F",
    "alert_threshold": 101,
    "refresh_minutes": 2,
    "alert_states": {},
}


class Harness:
    """Injected persistence, fetch launching, and clocks; no GTK anywhere."""

    def __init__(self, **updates):
        self.config = deepcopy({**FULL_CONFIG, **updates})
        self.failure = None
        self.saves = []
        self.started = []
        self.monotonic = 1_000.0
        self.wall = float(NOW)
        self.requests = RequestRegistry()
        self.controller = ApplicationController(
            lambda: self.config,
            persist=self.persist,
            start_fetch=self.start,
            monotonic=lambda: self.monotonic,
            wall_clock=lambda: self.wall,
            requests=self.requests,
        )

    def persist(self, candidate):
        if self.failure is not None:
            raise self.failure
        self.config = deepcopy(candidate)
        self.saves.append(deepcopy(candidate))
        return self.config

    def start(self, request):
        self.started.append(request)
        return True

    def outcome(self, request, sensors, **updates):
        values = {
            "request": request,
            "sensors": sensors,
            "area_ids": {sensor.sensor_id for sensor in sensors},
            "monitoring_ids": set(),
            "source": "PurpleAir live",
            "origin": "live",
            "delivery": "network",
            "error": None,
            "monitoring_error": None,
            "stale": False,
            "bounds": request.bounds,
            "capped": False,
            "provenance": {sensor.sensor_id: ("live", "network") for sensor in sensors},
            "cache_warning": None,
        }
        values.update(updates)
        return RefreshOutcome(**values)

    def finish(self, request, sensors, **updates):
        decision = self.controller.accept_refresh(self.outcome(request, sensors, **updates))
        self.controller.settle_refresh()
        return decision


def reading(sensor_id, *, indoor=False, aqi=50, last_seen=NOW - 60, origin="live"):
    sensor = make_sensor(sensor_id, indoor=indoor)
    sensor.aqi = aqi
    sensor.last_seen = last_seen
    sensor.origin = origin
    return sensor


DENIED = PermissionError(errno.EACCES, "Permission denied")


class RefreshWorkflowTest(unittest.TestCase):
    def test_supersession_queues_one_replacement_and_publishes_only_the_latest(self):
        h = Harness()
        first = h.controller.refresh()
        self.assertEqual(h.started, [first])
        queued = h.controller.refresh(force=True)
        replacement = h.controller.refresh()
        self.assertIsNone(queued)
        self.assertIsNone(replacement)
        self.assertEqual(len(h.started), 1)
        self.assertTrue(h.controller.refreshing)

        stale = h.controller.accept_refresh(h.outcome(first, [reading(1)]))
        started = h.controller.settle_refresh()

        self.assertFalse(stale.publish)
        self.assertEqual(h.controller.sensors, [])
        self.assertIs(started, h.started[-1])
        self.assertEqual(len(h.started), 2)
        self.assertGreater(started.request_id, first.request_id)
        latest = h.finish(started, [reading(2)])
        self.assertTrue(latest.publish)
        self.assertEqual([sensor.sensor_id for sensor in h.controller.sensors], [2])
        self.assertFalse(h.controller.refreshing)

    def test_display_change_reissues_inflight_work_inheriting_force(self):
        h = Harness()
        h.controller.sensors = [reading(1)]
        h.controller.area_ids = {1}
        active = h.controller.refresh(force=True)

        result = h.controller.toggle_favorite(1)

        self.assertTrue(result.ok)
        self.assertEqual(h.config["favorites"], [1, 7, 11])
        self.assertTrue(h.controller.sensors[0].favorite)
        pending = h.controller.pending_fetch
        self.assertTrue(pending.force)
        self.assertEqual(pending.generation, active.generation + 1)
        self.assertFalse(h.controller.accept_refresh(h.outcome(active, [reading(3)])).publish)
        self.assertIs(h.controller.settle_refresh(), pending)

    def test_display_change_without_inflight_work_starts_nothing(self):
        h = Harness()
        h.controller.sensors = [reading(1)]
        h.controller.area_ids = {1}

        self.assertTrue(h.controller.toggle_hidden(1).ok)

        self.assertEqual(h.config["hidden"], {"1": "Sensor 1"})
        self.assertEqual(h.started, [])
        self.assertIsNone(h.controller.pending_fetch)
        self.assertEqual(h.controller.publication()["items"], [])
        self.assertTrue(h.controller.toggle_hidden(1).ok)
        self.assertEqual(h.config["hidden"], {})

    def test_covered_fresh_view_discards_pending_without_fetching(self):
        h = Harness()
        view = h.controller.view_changed(VIEW)
        self.assertEqual(view, (15.0, -20.0))
        request = h.started[-1]
        h.finish(request, [reading(1)])
        h.controller.refresh()
        h.controller.refresh()
        self.assertIsNotNone(h.controller.pending_fetch)

        self.assertIsNone(h.controller.view_changed(VIEW))

        self.assertIsNone(h.controller.pending_fetch)
        self.assertEqual(len(h.started), 2)
        h.monotonic += 120
        self.assertEqual(h.controller.view_changed(VIEW), (15.0, -20.0))

    def test_automatic_refresh_never_downgrades_queued_manual_refresh(self):
        h = Harness()
        h.controller.refresh()
        h.controller.refresh(force=True)
        self.assertIsNone(h.controller.refresh())
        self.assertTrue(h.controller.pending_fetch.force)
        self.assertTrue(h.controller.settle_refresh().force)

    def test_automatic_refresh_coalesces_with_active_manual_refresh(self):
        h = Harness()
        manual = h.controller.refresh(force=True)
        self.assertIsNone(h.controller.refresh())
        self.assertIsNone(h.controller.pending_fetch)
        self.assertTrue(h.finish(manual, [reading(1, aqi=150)]).publish)
        self.assertEqual(len(h.started), 1)  # no second billed poll

    def test_identical_covered_view_keeps_manual_refresh(self):
        h = Harness()
        h.controller.view_changed(VIEW)
        h.finish(h.started[-1], [reading(1, aqi=30)])
        forced = h.controller.refresh(force=True)
        self.assertIsNone(h.controller.view_changed(VIEW))
        decision = h.finish(forced, [reading(1, aqi=150)])
        self.assertTrue(decision.publish)
        self.assertEqual(h.controller.sensors[0].aqi, 150)
        self.assertFalse(h.controller.refresh().force)  # satisfied force is cleared

    def test_new_target_supersedes_manual_refresh(self):
        h = Harness()
        h.controller.view_changed(VIEW)
        h.finish(h.started[-1], [reading(1)])
        forced = h.controller.refresh(force=True)
        self.assertEqual(h.controller.view_changed(OTHER), (35.0, -20.0))
        self.assertFalse(h.controller.accept_refresh(h.outcome(forced, [reading(2)])).publish)
        self.assertFalse(h.controller.settle_refresh().force)

    def test_home_report_keeps_manual_refresh_for_implicit_and_explicit_home(self):
        for name, start in (
            ("implicit", lambda c: c.refresh(force=True)),
            ("explicit", lambda c: c.refresh_home(force=True)),
        ):
            with self.subTest(home=name):
                h = Harness()
                forced = start(h.controller)
                bounds = forced.bounds
                home = {
                    "north": bounds.north,
                    "west": bounds.west,
                    "south": bounds.south,
                    "east": bounds.east,
                    "lat": forced.center[0],
                    "lon": forced.center[1],
                }

                self.assertIsNone(h.controller.view_changed(home))
                self.assertIsNone(h.controller.refresh())

                self.assertIsNone(h.controller.pending_fetch)
                self.assertIs(h.controller.active_fetch, forced)
                self.assertTrue(h.finish(forced, [reading(1, aqi=150)]).publish)
                self.assertEqual(len(h.started), 1)  # one billed poll

    def test_new_target_supersedes_implicit_home_manual_refresh(self):
        h = Harness()
        forced = h.controller.refresh(force=True)
        self.assertEqual(h.controller.view_changed(OTHER), (35.0, -20.0))
        self.assertFalse(h.controller.accept_refresh(h.outcome(forced, [reading(2)])).publish)
        self.assertFalse(h.controller.settle_refresh().force)

    def test_force_is_sticky_across_an_unforced_filter_refresh(self):
        h = Harness()
        h.controller.refresh(force=True)
        h.controller.set_location_filter("indoor")
        h.controller.refresh()
        self.assertTrue(h.controller.pending_fetch.force)

    def test_invalid_views_are_ignored(self):
        h = Harness()
        for view in ({**VIEW, "lat": float("nan")}, {**VIEW, "north": 91.0}, {"lat": 1}):
            with self.subTest(view=view):
                self.assertIsNone(h.controller.view_changed(view))
        self.assertEqual(h.started, [])

    def test_failed_retrieval_does_not_suppress_the_next_retry(self):
        h = Harness()
        h.controller.view_changed(VIEW)
        h.finish(h.started[-1], [reading(1)], error="offline")

        self.assertEqual(h.controller.view_changed(VIEW), (15.0, -20.0))

    def test_publication_keeps_area_and_remote_favorites_separate(self):
        h = Harness()
        request = h.controller.refresh()
        local, remote = reading(1), reading(7, indoor=True)
        decision = h.finish(
            request,
            [local, remote],
            area_ids={1},
            monitoring_ids={7},
            monitoring_error="favorites slow",
        )

        self.assertEqual(decision.trend_ids, (1,))
        self.assertEqual(decision.messages, ("favorites slow",))
        outdoor = h.controller.publication()
        self.assertEqual([item["id"] for item in outdoor["items"]], [1])
        h.config["location_filter"] = "indoor"
        indoor = h.controller.publication()
        self.assertEqual(indoor["area"], [])
        self.assertEqual([item["id"] for item in indoor["favorites"]], [7])
        self.assertTrue(indoor["favorites"][0]["remote_favorite"])
        self.assertEqual(indoor["selected_id"], 7)

    def test_preserved_selection_decides_the_trend_request(self):
        h = Harness()
        h.controller.select(2)
        decision = h.finish(h.controller.refresh(), [reading(1), reading(2)])
        self.assertEqual(decision.trend_ids, (2,))

    def test_restore_view_reports_last_valid_view_and_zoom(self):
        h = Harness()
        self.assertIsNone(h.controller.restore_view())

        h.controller.view_changed({**VIEW, "zoom": 9})
        self.assertEqual(
            h.controller.restore_view(), {"lat": 15.0, "lon": -20.0, "zoom": 9.0}
        )

        h.controller.view_changed({**VIEW, "lat": 16.0, "zoom": "x"})
        self.assertIsNone(h.controller.restore_view()["zoom"])


class PreferenceTransactionTest(unittest.TestCase):
    SETTINGS = {
        "request_id": "save-1",
        "radius_km": 30,
        "heatmap_threshold_km": 50,
        "alert_threshold": 120,
        "home_mode": "fixed",
        "temperature_unit": "C",
        "confidence_filter": False,
        "refresh_minutes": 5,
        "home_lat": 10.0,
        "home_lon": 20.0,
        "location_name": "Pinned",
    }

    def test_filter_success_commits_and_requests_refresh(self):
        h = Harness()
        generation = h.controller.begin_refresh().generation

        result = h.controller.set_location_filter("indoor")

        self.assertTrue(result.ok)
        self.assertTrue(result.refresh)
        self.assertEqual(h.config["location_filter"], "indoor")
        self.assertEqual(h.controller.begin_refresh().generation, generation + 1)

    def test_failed_transactions_roll_back_and_explain(self):
        cases = (
            ("location filter", lambda c: c.set_location_filter("indoor")),
            ("favorite", lambda c: c.toggle_favorite(1)),
            ("hide", lambda c: c.toggle_hidden(1)),
            ("detected location", lambda c: c.set_detected_home(1.0, 2.0)),
        )
        for label, action in cases:
            with self.subTest(label=label):
                h = Harness()
                h.controller.sensors = [reading(1)]
                before = deepcopy(h.config)
                active = h.controller.refresh()
                h.failure = DENIED

                result = action(h.controller)

                self.assertFalse(result.ok)
                self.assertIn(label, result.error.lower())
                self.assertIn("Permission denied", result.error)
                self.assertIn("try again", result.error.lower())
                self.assertEqual(h.config, before)
                self.assertFalse(h.controller.sensors[0].favorite)
                self.assertIsNone(h.controller.pending_fetch)
                self.assertTrue(h.controller.accept_refresh(h.outcome(active, [reading(1)])).publish)

    def test_settings_candidate_validates_then_commits(self):
        h = Harness(home_mode="auto")

        result = h.controller.save_settings(self.SETTINGS)

        self.assertEqual(result.status, "success")
        self.assertTrue(result.home_pinned)
        self.assertTrue(result.fixed_home)
        self.assertEqual(
            {key: h.config[key] for key in ("latitude", "longitude", "radius_km", "refresh_minutes")},
            {"latitude": 10.0, "longitude": 20.0, "radius_km": 30.0, "refresh_minutes": 5},
        )
        self.assertEqual(h.config["location_name"], "Pinned")

    def test_settings_failure_and_invalid_input_leave_config(self):
        h = Harness()
        before = deepcopy(h.config)
        invalid = h.controller.save_settings({**self.SETTINGS, "home_lat": 91})
        h.failure = DENIED
        failed = h.controller.save_settings(self.SETTINGS)

        self.assertEqual(invalid.status, "invalid")
        self.assertIn("valid range", invalid.error)
        self.assertEqual(failed.status, "error")
        self.assertIn("try again", failed.error)
        self.assertEqual(h.config, before)
        self.assertEqual(h.saves, [])

    MALFORMED = (
        ("radius_km", "NaN"), ("radius_km", "Infinity"), ("radius_km", "-inf"), ("radius_km", True),
        ("radius_km", None), ("radius_km", []), ("heatmap_threshold_km", "NaN"),
        ("heatmap_threshold_km", True), ("alert_threshold", True), ("alert_threshold", "NaN"),
        ("alert_threshold", "120.5"), ("refresh_minutes", True), ("refresh_minutes", "inf"),
        ("home_lat", "NaN"), ("home_lon", True), ("confidence_filter", "false"),
        ("confidence_filter", 1), ("clear_api_key", "yes"), ("api_key", {"x": 1}),
        ("api_key", 123), ("api_key", ["k"]), ("location_name", {"x": 1}),
    )

    def test_malformed_settings_values_are_rejected_without_persisting(self):
        for field, value in self.MALFORMED:
            with self.subTest(field=field, value=value):
                h = Harness()
                before = deepcopy(h.config)
                result = h.controller.save_settings({**self.SETTINGS, field: value})
                self.assertEqual(result.status, "invalid")
                self.assertIn(field, result.error)
                self.assertEqual(h.config, before)
                self.assertEqual(h.saves, [])

    def test_malformed_home_fields_are_rejected_in_auto_mode_too(self):
        cases = (
            ("location_name", {"x": 1}), ("location_name", 7), ("home_lat", "NaN"),
            ("home_lat", True), ("home_lon", "inf"), ("home_lon", []),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                h = Harness(home_mode="auto")
                before = deepcopy(h.config)
                result = h.controller.save_settings(
                    {**self.SETTINGS, "home_mode": "auto", field: value}
                )
                self.assertEqual(result.status, "invalid")
                self.assertIn(field, result.error)
                self.assertEqual(h.config, before)
                self.assertEqual(h.saves, [])

    def test_auto_mode_saves_with_well_formed_or_absent_home_fields(self):
        payloads = (
            {**self.SETTINGS, "home_mode": "auto", "home_lat": "10.5", "home_lon": "-20.25",
             "location_name": "Somewhere"},
            {key: value for key, value in self.SETTINGS.items()
             if key not in ("home_lat", "home_lon", "location_name")} | {"home_mode": "auto"},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                h = Harness(home_mode="auto")
                before = {k: h.config[k] for k in ("latitude", "longitude", "location_name")}
                result = h.controller.save_settings(payload)
                self.assertEqual(result.status, "success")
                self.assertEqual(
                    {k: h.config[k] for k in ("latitude", "longitude", "location_name")}, before
                )

    def test_form_numeric_strings_still_save(self):
        h = Harness(home_mode="auto")
        result = h.controller.save_settings({
            **self.SETTINGS, "radius_km": "30", "heatmap_threshold_km": "50",
            "alert_threshold": "120", "refresh_minutes": "5", "home_lat": "10.5",
            "home_lon": "-20.25", "api_key": "  new-key  ", "clear_api_key": False,
            "confidence_filter": False, "interface_scale": 1,
        })
        self.assertEqual(result.status, "success")
        self.assertEqual(
            {k: h.config[k] for k in ("radius_km", "heatmap_threshold_km", "alert_threshold",
                                      "refresh_minutes", "latitude", "longitude", "api_key",
                                      "confidence_filter")},
            {"radius_km": 30.0, "heatmap_threshold_km": 50.0, "alert_threshold": 120,
             "refresh_minutes": 5, "latitude": 10.5, "longitude": -20.25,
             "api_key": "new-key", "confidence_filter": False},
        )

    def test_detected_home_requires_auto_mode(self):
        h = Harness(home_mode="fixed")
        self.assertIsNone(h.controller.set_detected_home(1.0, 2.0))
        self.assertEqual(h.saves, [])


class AlertWorkflowTest(unittest.TestCase):
    def test_only_fresh_real_favorite_crossings_notify_and_persist_once(self):
        h = Harness(favorites=[1, 2], alert_states={})
        decision = h.finish(
            h.controller.refresh(),
            [reading(1, aqi=150), reading(2, aqi=150, last_seen=NOW - 7201), reading(3, aqi=150)],
        )
        self.assertTrue(decision.publish)

        alerts = h.controller.evaluate_alerts()
        h.controller.record_alert_delivery(1, True)
        repeat = h.controller.evaluate_alerts()

        self.assertEqual([sensor.sensor_id for sensor in alerts.notify], [1])
        self.assertIsNone(alerts.error)
        self.assertEqual(h.config["alert_states"], {"1": True})
        self.assertEqual(repeat.notify, ())
        self.assertEqual(len(h.saves), 1)

    def test_demo_results_neither_notify_nor_touch_state(self):
        h = Harness(favorites=[900000], alert_states={"900000": False})
        h.finish(
            h.controller.refresh(),
            [reading(900000, aqi=200, origin="demo")],
            origin="demo",
            provenance={900000: ("demo", "network")},
        )
        alerts = h.controller.evaluate_alerts()
        self.assertEqual(alerts.notify, ())
        self.assertEqual(h.saves, [])

    def test_alert_state_save_failure_is_reported_not_raised(self):
        h = Harness(favorites=[1], alert_states={"1": False})
        h.finish(h.controller.refresh(), [reading(1, aqi=150)])
        h.failure = DENIED

        alerts = h.controller.evaluate_alerts()

        self.assertIn("Could not save alert state", alerts.error)
        self.assertEqual(h.config["alert_states"], {"1": False})

    def test_failed_delivery_retries_each_evaluation_only_while_eligible(self):
        h = Harness(favorites=[1], alert_states={})
        h.finish(h.controller.refresh(), [reading(1, aqi=150)])
        first = h.controller.evaluate_alerts()
        self.assertEqual([s.sensor_id for s in first.notify], [1])
        h.controller.record_alert_delivery(1, False)
        self.assertEqual([s.sensor_id for s in h.controller.evaluate_alerts().notify], [1])
        h.controller.record_alert_delivery(1, False)
        h.wall += 7201                                     # age 7261 s > 7200: not is_fresh_observation
        self.assertEqual(h.controller.evaluate_alerts().notify, ())
        h.finish(h.controller.refresh(), [reading(1, aqi=160, last_seen=int(h.wall) - 60)])
        retry = h.controller.evaluate_alerts()
        self.assertEqual([s.aqi for s in retry.notify], [160])
        h.controller.record_alert_delivery(1, True)
        self.assertEqual(h.controller.evaluate_alerts().notify, ())
        self.assertEqual(h.config["alert_states"], {"1": True})
        self.assertEqual(len(h.saves), 1)

    def test_persistence_failure_never_renotifies_the_same_crossing(self):
        h = Harness(favorites=[1], alert_states={})
        h.finish(h.controller.refresh(), [reading(1, aqi=150)])
        h.failure = DENIED
        counts = []
        for _ in range(3):
            decision = h.controller.evaluate_alerts()
            counts.append(len(decision.notify))
            self.assertIn("Could not save alert state", decision.error)
            for sensor in decision.notify:
                h.controller.record_alert_delivery(sensor.sensor_id, True)
        self.assertEqual(counts, [1, 0, 0])
        self.assertEqual(h.config["alert_states"], {})
        h.failure = None
        self.assertEqual(h.controller.evaluate_alerts().notify, ())
        self.assertEqual(h.config["alert_states"], {"1": True})

    def test_reset_crossing_alerts_again(self):
        h = Harness(favorites=[1], alert_states={})
        h.finish(h.controller.refresh(), [reading(1, aqi=150)])
        h.controller.evaluate_alerts(); h.controller.record_alert_delivery(1, True)
        h.finish(h.controller.refresh(), [reading(1, aqi=20)])
        self.assertEqual(h.controller.evaluate_alerts().notify, ())
        h.finish(h.controller.refresh(), [reading(1, aqi=150)])
        self.assertEqual([s.sensor_id for s in h.controller.evaluate_alerts().notify], [1])


class TrendDecisionTest(unittest.TestCase):
    def test_demo_and_missing_sensors_never_need_remote_work(self):
        h = Harness()
        demo = reading(900001, origin="demo")
        demo.trend = [{"label": "Now", "aqi": 50}]
        h.controller.sensors = [demo]

        local = h.controller.begin_trend(900001)
        missing = h.controller.begin_trend(5)

        self.assertEqual((local.kind, local.status, local.trend), ("local", "loaded", demo.trend))
        self.assertEqual((missing.kind, missing.status), ("idle", "idle"))
        self.assertFalse(h.requests.is_current(local.token))

    def test_remote_trend_is_single_flight_and_bound_to_its_reading(self):
        h = Harness()
        sensor = reading(1, last_seen=100)
        h.controller.sensors = [sensor]
        plan = h.controller.begin_trend(1)
        self.assertEqual(plan.kind, "remote")
        h.controller.track_trend(plan)
        self.assertIsNone(h.controller.begin_trend(1))
        self.assertTrue(h.controller.trend_wanted(plan))

        sensor.last_seen = 200
        self.assertFalse(h.controller.trend_wanted(plan))
        stale = h.controller.finish_trend(1, 100, plan.token, [{"label": "Now", "aqi": 9}], False)
        self.assertIsNone(stale)
        self.assertEqual(sensor.trend, [])
        self.assertNotIn(1, h.controller.trend_inflight)

        fresh = h.controller.begin_trend(1)
        h.controller.track_trend(fresh)
        loaded = h.controller.finish_trend(1, 200, fresh.token, [{"label": "Now", "aqi": 9}], False)
        self.assertEqual(loaded.status, "loaded")
        self.assertTrue(loaded.store)
        self.assertEqual(sensor.trend, [{"label": "Now", "aqi": 9}])

    def test_successful_key_change_revokes_tracked_trend(self):
        h = Harness()
        h.controller.sensors = [reading(1, last_seen=100)]
        plan = h.controller.begin_trend(1)
        h.controller.track_trend(plan)

        result = h.controller.save_settings(
            {**PreferenceTransactionTest.SETTINGS, "clear_api_key": True}
        )

        self.assertEqual(result.status, "success")
        self.assertFalse(h.controller.trend_wanted(plan))
        self.assertIsNone(
            h.controller.finish_trend(1, 100, plan.token, [{"label": "Now", "aqi": 9}], False)
        )
        self.assertNotIn(1, h.controller.trend_inflight)

        for case, failure, update in (
            ("failed", DENIED, {"clear_api_key": True}),
            ("unchanged", None, {}),
        ):
            with self.subTest(case=case):
                h = Harness()
                h.controller.sensors = [reading(1, last_seen=100)]
                plan = h.controller.begin_trend(1)
                h.controller.track_trend(plan)
                h.failure = failure

                result = h.controller.save_settings({**PreferenceTransactionTest.SETTINGS, **update})

                self.assertEqual(result.status, "error" if failure else "success")
                self.assertTrue(h.controller.trend_wanted(plan))
                self.assertIn(1, h.controller.trend_inflight)

    def test_trend_credential_is_the_committed_key_only_while_authorized(self):
        for case, update in (("clear", {"clear_api_key": True}), ("replace", {"api_key": "new"})):
            with self.subTest(case=case):
                h = Harness()
                h.controller.sensors = [reading(1, last_seen=100)]
                plan = h.controller.begin_trend(1)
                h.controller.track_trend(plan)
                self.assertEqual(h.controller.trend_credential(plan), "key")

                h.controller.save_settings({**PreferenceTransactionTest.SETTINGS, **update})

                # Neither the old key nor its replacement is spent on a revoked plan.
                self.assertIsNone(h.controller.trend_credential(plan))

    def test_unsettled_trend_serves_only_its_reading_and_credential(self):
        trend = [{"label": "Now", "aqi": 9}]
        h = Harness()
        sensor = reading(1, last_seen=100)
        h.controller.sensors = [sensor]
        plan = h.controller.begin_trend(1)
        h.controller.track_trend(plan)
        h.controller.finish_trend(1, 100, plan.token, trend, False)
        h.controller.hold_trend(1, 100, h.controller.credential_generation, trend)

        served = h.controller.begin_trend(1)
        self.assertEqual((served.kind, served.status, served.trend), ("local", "loaded", trend))
        self.assertFalse(h.requests.is_current(served.token))

        # Settling an older write leaves a newer hold in place.
        h.controller.settle_trend(1, 99, h.controller.credential_generation)
        self.assertIn(1, h.controller.trend_unsettled)
        h.controller.settle_trend(1, 100, h.controller.credential_generation)
        self.assertNotIn(1, h.controller.trend_unsettled)
        self.assertEqual(h.controller.begin_trend(1).kind, "remote")

        # A newer reading supersedes the hold instead of being served by it.
        h.controller.hold_trend(1, 100, h.controller.credential_generation, trend)
        sensor.last_seen = 200
        self.assertEqual(h.controller.begin_trend(1).kind, "remote")
        self.assertNotIn(1, h.controller.trend_unsettled)

        # A credential change revokes the hold.
        sensor.last_seen = 100
        h.controller.hold_trend(1, 100, h.controller.credential_generation, trend)
        h.controller.save_settings({**PreferenceTransactionTest.SETTINGS, "api_key": "new"})
        self.assertNotIn(1, h.controller.trend_unsettled)
        self.assertEqual(h.controller.begin_trend(1).kind, "remote")

    def test_adopting_readings_keeps_only_holds_for_readings_still_present(self):
        trend = [{"label": "Now", "aqi": 9}]
        h = Harness()
        h.finish(h.controller.refresh(), [reading(1, last_seen=100), reading(2, last_seen=100),
                                          reading(3, last_seen=100)])
        generation = h.controller.credential_generation
        for sensor_id in (1, 2, 3):
            h.controller.hold_trend(sensor_id, 100, generation, trend)

        # 1 keeps its exact reading, 2 has a newer one, 3 is gone; nobody
        # reselects 2 or 3, so only adoption can release their holds.
        h.finish(h.controller.refresh(force=True),
                 [reading(1, last_seen=100), reading(2, last_seen=200)])

        self.assertEqual(set(h.controller.trend_unsettled), {1})
        served = h.controller.begin_trend(1)
        self.assertEqual((served.kind, served.status, served.trend), ("local", "loaded", trend))

    def test_shutdown_releases_every_hold(self):
        h = Harness()
        h.controller.sensors = [reading(1, last_seen=100)]
        h.controller.hold_trend(1, 100, h.controller.credential_generation, [{"label": "Now"}])

        h.controller.shutdown()

        self.assertEqual(h.controller.trend_unsettled, {})


class CachedStartupDecisionTest(unittest.TestCase):
    def test_home_paint_is_bound_to_the_home_target(self):
        h = Harness()
        home_bounds = h.controller.home_bounds()
        token = h.controller.begin_cached_startup()
        h.controller.view_changed(VIEW)

        self.assertFalse(h.controller.apply_cached_startup(token, [reading(7)], home_bounds))
        self.assertEqual(h.controller.sensors, [])
        self.assertEqual(h.controller.current_view[1], (15.0, -20.0))
        # A view away from home never admits a home paint.
        self.assertIsNone(h.controller.begin_cached_startup())

    def test_home_report_keeps_the_home_paint(self):
        h = Harness()
        home_bounds = h.controller.home_bounds()
        token = h.controller.begin_cached_startup()
        config = h.config
        h.controller.view_changed(
            {
                "north": home_bounds.north,
                "west": home_bounds.west,
                "south": home_bounds.south,
                "east": home_bounds.east,
                "lat": float(config["latitude"]),
                "lon": float(config["longitude"]),
            }
        )

        self.assertTrue(h.controller.apply_cached_startup(token, [reading(7)], home_bounds))


class LifecycleTest(unittest.TestCase):
    def test_shutdown_rejects_new_work_and_invalidates_callbacks(self):
        h = Harness()
        h.controller.sensors = [reading(1)]
        active = h.controller.refresh()
        h.controller.refresh()
        plan = h.controller.begin_trend(1)
        h.controller.track_trend(plan)

        h.controller.shutdown()

        self.assertTrue(h.controller.stopped)
        self.assertFalse(h.controller.refreshing)
        self.assertIsNone(h.controller.pending_fetch)
        self.assertIsNone(h.controller.refresh())
        self.assertIsNone(h.controller.view_changed(VIEW))
        self.assertIsNone(h.controller.settle_refresh())
        self.assertFalse(h.controller.accept_refresh(h.outcome(active, [reading(2)])).publish)
        self.assertIsNone(h.controller.finish_trend(1, plan.reading_last_seen, plan.token, [], False))
        self.assertIsNone(h.controller.begin_trend(1))
        self.assertIsNone(h.controller.toggle_favorite(1))
        self.assertIsNone(h.controller.set_location_filter("indoor"))
        self.assertIsNone(h.controller.save_settings(PreferenceTransactionTest.SETTINGS))
        self.assertEqual(len(h.started), 1)
        self.assertEqual(h.saves, [])


if __name__ == "__main__":
    unittest.main()
