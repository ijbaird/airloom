import json
import unittest
from unittest import mock

from airloom.purpleair import PurpleAirClient, PurpleAirError, Bounds, bounds_around, bounds_contains, parse_sensor_payload


class PurpleAirTest(unittest.TestCase):
    def test_bounds_are_centered(self):
        bounds = bounds_around(45.5, -122.6, 20)
        self.assertGreater(bounds.north, 45.5)
        self.assertLess(bounds.south, 45.5)
        self.assertLess(bounds.west, -122.6)
        self.assertGreater(bounds.east, -122.6)

    def test_bounds_stay_inside_coordinate_domain(self):
        bounds = bounds_around(89.9, 179.5, 100)
        self.assertLessEqual(bounds.north, 90.0)
        self.assertGreaterEqual(bounds.south, -90.0)
        self.assertLessEqual(bounds.east, 180.0)
        self.assertGreaterEqual(bounds.west, -180.0)
        south_pole = bounds_around(-89.9, -179.5, 100)
        self.assertGreaterEqual(south_pole.south, -90.0)
        self.assertGreaterEqual(south_pole.west, -180.0)

    def test_dynamic_field_order_is_respected(self):
        payload = {
            "fields": ["longitude", "sensor_index", "humidity", "pm2.5_cf_1", "name", "latitude", "temperature", "last_seen"],
            "data": [[-122.67, 1234, 50, 20, "Test Station", 45.52, 72, 1700000000]],
        }
        sensor = parse_sensor_payload(payload)[0]
        self.assertEqual(sensor.sensor_id, 1234)
        self.assertEqual(sensor.name, "Test Station")
        self.assertEqual(sensor.temperature_f, 64.0)
        self.assertEqual(sensor.pm25, 11.9)
        self.assertEqual(sensor.aqi, 56)
        self.assertEqual(len(sensor.trend), 7)

    def test_rows_without_coordinates_are_skipped(self):
        payload = {"fields": ["sensor_index", "latitude", "longitude"], "data": [[1, None, -122.0], [2, 45.0, -122.0]]}
        sensors = parse_sensor_payload(payload)
        self.assertEqual([sensor.sensor_id for sensor in sensors], [2])

    def test_non_object_payloads_raise_purpleair_error(self):
        for payload in ([], None, "error", 5, {"fields": "x", "data": "y"}):
            with self.subTest(payload=payload):
                with self.assertRaises(PurpleAirError):
                    parse_sensor_payload(payload)

    def test_hostile_values_never_raise(self):
        # json.loads accepts Infinity/NaN, so the parser must tolerate them.
        payload = json.loads(
            '{"fields": ["sensor_index", "latitude", "longitude", "humidity", "pm2.5_cf_1", "last_seen"],'
            ' "data": [[10, 45.0, -122.0, 50, Infinity, 1700000000],'
            '          [11, 45.1, -122.1, NaN, 20, "xyz"],'
            '          ["abc", 45.2, -122.2, 50, 20, 1700000000],'
            '          [13, 45.3, -122.3, 50, 20]]}'
        )
        sensors = parse_sensor_payload(payload)
        by_id = {sensor.sensor_id: sensor for sensor in sensors}
        self.assertEqual(sorted(by_id), [10, 11, 13])  # "abc" row dropped
        self.assertIsNone(by_id[10].aqi)  # Infinity pm2.5 -> no reading
        self.assertIsNone(by_id[11].last_seen)  # bad last_seen tolerated
        self.assertIsNotNone(by_id[11].aqi)  # NaN humidity -> neutral default
        self.assertIsNotNone(by_id[13].aqi)  # short row tolerated

    def test_string_sensor_index_is_coerced(self):
        payload = {"fields": ["sensor_index", "latitude", "longitude"], "data": [["42", 45.0, -122.0]]}
        self.assertEqual(parse_sensor_payload(payload)[0].sensor_id, 42)

    def test_bounds_contains(self):
        outer = Bounds(46.0, -123.0, 45.0, -122.0)
        self.assertTrue(bounds_contains(outer, Bounds(45.9, -122.9, 45.1, -122.1)))
        self.assertTrue(bounds_contains(outer, outer))
        self.assertFalse(bounds_contains(outer, Bounds(46.1, -122.9, 45.1, -122.1)))  # pokes north
        self.assertFalse(bounds_contains(outer, Bounds(45.9, -123.5, 45.1, -122.1)))  # pokes west

    def test_fetch_sensors_builds_show_only_query(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            import io
            return mock.MagicMock(
                __enter__=lambda s: io.StringIO('{"fields": [], "data": []}'),
                __exit__=lambda s, *a: False,
            )

        client = PurpleAirClient("key")
        with mock.patch("airloom.purpleair.urlopen", side_effect=fake_urlopen):
            client.fetch_sensors(show_only=[42, 7])
        self.assertIn("show_only=42%2C7", captured["url"])
        self.assertNotIn("nwlat", captured["url"])

    def test_fetch_sensors_requires_bounds_or_show_only(self):
        with self.assertRaises(PurpleAirError):
            PurpleAirClient("key").fetch_sensors()


if __name__ == "__main__":
    unittest.main()

