import time
import unittest

from airloom import presentation
from airloom.demo import demo_sensors


class DemoTest(unittest.TestCase):
    def test_demo_set_mixes_indoor_and_outdoor_deterministically(self):
        first = demo_sensors(45.5, -122.6)
        second = demo_sensors(45.5, -122.6)
        self.assertEqual(
            [sensor.indoor for sensor in first],
            [sensor.indoor for sensor in second],
        )
        indoor = [sensor for sensor in first if sensor.indoor]
        self.assertTrue(indoor)
        self.assertLess(len(indoor), len(first))
        for sensor in indoor:
            self.assertTrue(sensor.to_dict()["indoor"])

    def test_now_trend_point_matches_displayed_aqi(self):
        for sensor in demo_sensors(45.5, -122.6):
            with self.subTest(sensor=sensor.name):
                now = next(point for point in sensor.trend if point["label"] == "Now")
                self.assertEqual(now["aqi"], sensor.aqi)

    def test_generated_sensors_are_labeled_demo(self):
        sensors = demo_sensors(45.5, -122.6)
        self.assertEqual({sensor.origin for sensor in sensors}, {"demo"})
        self.assertEqual({sensor.to_dict()["origin"] for sensor in sensors}, {"demo"})
        # The per-sensor label alone must block alerts, even in a live batch.
        self.assertEqual(
            presentation.alert_transitions(
                sensors,
                favorites={sensor.sensor_id for sensor in sensors},
                previous={},
                threshold=1,
                now=time.time(),
                origin="live",
            ),
            ([], {}),
        )


if __name__ == "__main__":
    unittest.main()
