from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from airloom.controller import (
    ApplicationController,
    RefreshDecision,
    RefreshRequest,
)
from airloom.geography import Bounds
from airloom.models import Sensor


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


if __name__ == "__main__":
    unittest.main()
