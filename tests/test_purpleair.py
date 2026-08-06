import unittest

from airloom.purpleair import bounds_around, parse_sensor_payload


class PurpleAirTest(unittest.TestCase):
    def test_bounds_are_centered(self):
        bounds = bounds_around(45.5, -122.6, 20)
        self.assertGreater(bounds.north, 45.5)
        self.assertLess(bounds.south, 45.5)
        self.assertLess(bounds.west, -122.6)
        self.assertGreater(bounds.east, -122.6)

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


if __name__ == "__main__":
    unittest.main()

