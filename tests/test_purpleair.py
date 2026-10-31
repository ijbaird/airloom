import io
import json
import time
import unittest
from unittest import mock

from airloom import presentation, purpleair
from airloom.purpleair import PurpleAirClient, PurpleAirError, Bounds, bounds_around, bounds_contains, parse_sensor_payload


NOW = 1_800_000_000


def map_row(sensor_id, **values) -> list:
    defaults = {
        "sensor_index": sensor_id,
        "name": f"S{sensor_id}",
        "latitude": 45.5,
        "longitude": -122.6,
        "last_seen": NOW,
        "pm2.5_cf_1": 20.0,
        "humidity": 50,
    }
    defaults.update(values)
    return [defaults.get(field) for field in purpleair.MAP_FIELDS]


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
            '          ["abc", 45.2, -122.2, 50, 20, 1700000000]]}'
        )
        sensors = parse_sensor_payload(payload)
        by_id = {sensor.sensor_id: sensor for sensor in sensors}
        self.assertEqual(sorted(by_id), [10, 11])  # "abc" row dropped
        self.assertIsNone(by_id[10].aqi)  # Infinity pm2.5 -> no reading
        self.assertIsNone(by_id[11].last_seen)  # bad last_seen tolerated
        self.assertIsNotNone(by_id[11].aqi)  # NaN humidity -> neutral default

    def test_short_row_is_rejected_not_tolerated(self):
        payload = {
            "fields": ["sensor_index", "latitude", "longitude", "humidity", "pm2.5_cf_1", "last_seen"],
            "data": [[10, 45.0, -122.0, 50, 20, 1700000000], [13, 45.3, -122.3, 50, 20]],
        }
        with self.assertRaises(PurpleAirError):
            parse_sensor_payload(payload)

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
                __enter__=lambda s: io.StringIO(json.dumps({"fields": list(purpleair.MAP_FIELDS), "data": []})),
                __exit__=lambda s, *a: False,
            )

        client = PurpleAirClient("key")
        with mock.patch("airloom.purpleair.urlopen", side_effect=fake_urlopen):
            client.fetch_sensors(show_only=[42, 7])
        self.assertIn("show_only=42%2C7", captured["url"])
        self.assertNotIn("nwlat", captured["url"])

    def test_show_only_tolerates_bad_ids(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            import io
            return mock.MagicMock(
                __enter__=lambda s: io.StringIO(json.dumps({"fields": list(purpleair.MAP_FIELDS), "data": []})),
                __exit__=lambda s, *a: False,
            )

        client = PurpleAirClient("key")
        with mock.patch("airloom.purpleair.urlopen", side_effect=fake_urlopen):
            client.fetch_sensors(show_only=["42", "abc", 7, "7.5", 8.9, 0, -3])
        self.assertTrue(captured["url"].endswith("show_only=42%2C7"))

    def test_show_only_with_no_valid_ids_raises(self):
        with self.assertRaises(PurpleAirError):
            PurpleAirClient("key").fetch_sensors(show_only=["abc"])

    def test_fetch_sensors_requires_bounds_or_show_only(self):
        with self.assertRaises(PurpleAirError):
            PurpleAirClient("key").fetch_sensors()

    def _fetch_url(self, **kwargs):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            import io
            return mock.MagicMock(
                __enter__=lambda s: io.StringIO(json.dumps({"fields": list(purpleair.MAP_FIELDS), "data": []})),
                __exit__=lambda s, *a: False,
            )

        with mock.patch("airloom.purpleair.urlopen", side_effect=fake_urlopen):
            PurpleAirClient("key").fetch_sensors(**kwargs)
        return captured["url"]

    def test_location_filter_maps_to_location_type_param(self):
        bounds = Bounds(46.0, -123.0, 45.0, -122.0)
        self.assertIn("location_type=0", self._fetch_url(bounds=bounds))
        self.assertIn("location_type=0", self._fetch_url(bounds=bounds, location_filter="outdoor"))
        self.assertIn("location_type=1", self._fetch_url(bounds=bounds, location_filter="indoor"))
        # "location_type" (no "=") also appears in the fields= listing (the
        # field is always requested so payloads can be parsed for `indoor`),
        # so this checks for the query *parameter* specifically.
        self.assertNotIn("location_type=", self._fetch_url(bounds=bounds, location_filter="both"))

    def test_show_only_never_sends_location_filter(self):
        url = self._fetch_url(show_only=[42], location_filter="outdoor")
        self.assertNotIn("location_type=", url)

    def test_location_type_field_is_requested_and_parsed(self):
        self.assertIn("location_type", self._fetch_url(bounds=Bounds(46.0, -123.0, 45.0, -122.0)))
        payload = {
            "fields": ["sensor_index", "latitude", "longitude", "location_type"],
            "data": [[1, 45.0, -122.0, 1], [2, 45.1, -122.1, 0], [3, 45.2, -122.2, None]],
        }
        sensors = {s.sensor_id: s for s in parse_sensor_payload(payload)}
        self.assertTrue(sensors[1].indoor)
        self.assertFalse(sensors[2].indoor)
        self.assertFalse(sensors[3].indoor)
        self.assertTrue(sensors[1].to_dict()["indoor"])


class FieldListTest(unittest.TestCase):
    def test_map_fields_exclude_trend_averages(self):
        for field in purpleair.MAP_FIELDS:
            self.assertNotIn("minute", field)
            self.assertNotIn("hour", field)
            self.assertNotIn("week", field)

    def test_data_fields_exclude_metadata(self):
        for field in ("name", "latitude", "longitude", "location_type"):
            self.assertNotIn(field, purpleair.DATA_FIELDS)
        self.assertIn("sensor_index", purpleair.DATA_FIELDS)
        self.assertIn("pm2.5_cf_1", purpleair.DATA_FIELDS)

    def test_confidence_is_requested_in_area_and_delta_fetches(self):
        self.assertIn("confidence", purpleair.MAP_FIELDS)
        self.assertIn("confidence", purpleair.DATA_FIELDS)

    def test_trend_fetch_fields_carry_correction_inputs(self):
        self.assertIn("humidity", purpleair.TREND_FETCH_FIELDS)
        self.assertIn("location_type", purpleair.TREND_FETCH_FIELDS)
        self.assertIn("pm2.5_1week", purpleair.TREND_FETCH_FIELDS)


class FetchRowsTest(unittest.TestCase):
    def _fetch_rows_with_payload(self, payload, clock=time.time, **kwargs):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            return mock.MagicMock(
                __enter__=lambda s: io.StringIO(json.dumps(payload)),
                __exit__=lambda s, *a: False,
            )

        with mock.patch("airloom.purpleair.urlopen", side_effect=fake_urlopen):
            result = PurpleAirClient("key", clock=clock).fetch_rows(**kwargs)
        return captured["url"], result

    def test_modified_since_and_fields_in_query(self):
        payload = {"fields": list(purpleair.DATA_FIELDS), "data": [], "time_stamp": 1754680000}
        url, result = self._fetch_rows_with_payload(
            payload,
            bounds=Bounds(46.0, -123.0, 45.0, -122.0),
            fields=purpleair.DATA_FIELDS,
            modified_since=1754670000,
        )
        self.assertIn("modified_since=1754670000", url)
        self.assertIn("pm2.5_cf_1", url)
        self.assertNotIn("latitude", url.split("fields=")[1].split("&")[0])
        self.assertEqual(result.time_stamp, 1754680000)
        self.assertEqual(result.rows, [])

    def test_rows_are_raw_value_dicts(self):
        payload = {
            "fields": ["sensor_index", "pm2.5_cf_1", "humidity"],
            "data": [[7, 4.0, 40], [8, 9.0, 55]],
            "time_stamp": 1754680000,
        }
        _, result = self._fetch_rows_with_payload(
            payload, bounds=Bounds(46.0, -123.0, 45.0, -122.0),
            fields=("sensor_index", "pm2.5_cf_1", "humidity"))
        self.assertEqual(result.rows[0]["sensor_index"], 7)
        self.assertEqual(result.rows[1]["humidity"], 55)


class ResponseValidationTest(unittest.TestCase):
    BOUNDS = Bounds(46.0, -123.0, 45.0, -122.0)

    def _fetch(self, payload, **kwargs):
        return FetchRowsTest._fetch_rows_with_payload(
            self, payload, clock=lambda: NOW, bounds=self.BOUNDS, **kwargs)[1]

    def _assert_rejected(self, payload, **kwargs):
        with self.assertRaises(PurpleAirError):
            self._fetch(payload, **kwargs)

    def test_short_row_rejects_whole_response(self):
        self._assert_rejected(
            {"fields": list(purpleair.DATA_FIELDS), "data": [[1, NOW]], "time_stamp": NOW},
            fields=purpleair.DATA_FIELDS)

    def test_long_row_rejects_whole_response(self):
        self._assert_rejected(
            {"fields": list(purpleair.MAP_FIELDS), "data": [map_row(1), [*map_row(2), 99]]})

    def test_non_list_row_rejects_whole_response(self):
        self._assert_rejected({"fields": list(purpleair.MAP_FIELDS), "data": [map_row(1), "junk"]})

    def test_missing_requested_measurement_field_rejects_response(self):
        fields = [field for field in purpleair.MAP_FIELDS if field != "pm2.5_cf_1"]
        rows = [[value for field, value in zip(purpleair.MAP_FIELDS, map_row(1))
                 if field != "pm2.5_cf_1"]]
        self._assert_rejected({"fields": fields, "data": rows})

    def test_missing_sensor_index_rejects_response(self):
        self._assert_rejected({"fields": list(purpleair.MAP_FIELDS[1:]), "data": [map_row(1)[1:]]})

    def test_non_string_field_name_rejects_response(self):
        self._assert_rejected({"fields": [*purpleair.MAP_FIELDS[:-1], 5], "data": [map_row(1)]})

    def test_omitted_metadata_is_not_required_for_delta(self):
        result = self._fetch(
            {"fields": list(purpleair.DATA_FIELDS),
             "data": [[1, NOW, 50, 70, 1.0, 4.0, 5.0, 99]], "time_stamp": NOW},
            fields=purpleair.DATA_FIELDS)
        self.assertEqual(result.rows[0]["sensor_index"], 1)
        self.assertEqual(result.rows[0]["last_seen"], NOW)
        self.assertEqual(result.time_stamp, NOW)


class FutureTimestampTest(unittest.TestCase):
    BOUNDS = Bounds(46.0, -123.0, 45.0, -122.0)

    def _fetch(self, payload):
        return FetchRowsTest._fetch_rows_with_payload(
            self, payload, clock=lambda: NOW, bounds=self.BOUNDS)[1]

    def test_future_last_seen_is_missing_at_parse_and_sensor_boundaries(self):
        payload = {
            "fields": list(purpleair.MAP_FIELDS),
            "data": [
                map_row(1, last_seen=NOW + 600),
                map_row(2, last_seen=NOW + 601),
                map_row(3, last_seen=NOW * 1000),
                map_row(4, last_seen=2**63 - 1),
            ],
        }
        result = self._fetch(payload)
        self.assertEqual([r["last_seen"] for r in result.rows], [NOW + 600, None, None, None])
        base = {"sensor_index": 9, "latitude": 45.5, "longitude": -122.6}
        self.assertIsNone(
            purpleair.sensor_from_values({**base, "last_seen": NOW + 601}, now=NOW).last_seen)
        self.assertEqual(
            purpleair.sensor_from_values({**base, "last_seen": NOW + 600}, now=NOW).last_seen,
            NOW + 600)

    def test_future_time_stamp_is_not_a_checkpoint(self):
        stamps = []
        for time_stamp in (NOW + 601, NOW * 1000, NOW + 600):
            payload = {"fields": list(purpleair.MAP_FIELDS), "data": [], "time_stamp": time_stamp}
            stamps.append(self._fetch(payload).time_stamp)
        self.assertEqual(stamps, [None, None, NOW + 600])

    def test_fetch_sensors_uses_the_injected_clock(self):
        # NOW is ahead of the real clock, so NOW + 600 survives only if the
        # client's clock, not time.time(), decides what is in the future.
        payload = {
            "fields": list(purpleair.MAP_FIELDS),
            "data": [map_row(1, last_seen=NOW + 600), map_row(2, last_seen=NOW + 601)],
        }
        with mock.patch("airloom.purpleair.urlopen", return_value=mock.MagicMock(
            __enter__=lambda s: io.StringIO(json.dumps(payload)),
            __exit__=lambda s, *a: False,
        )):
            sensors = PurpleAirClient("key", clock=lambda: NOW).fetch_sensors(bounds=self.BOUNDS)
        self.assertEqual([s.last_seen for s in sensors], [NOW + 600, None])

    def test_far_future_observation_parses_ineligible_for_alerts(self):
        sensors = parse_sensor_payload({
            "fields": list(purpleair.MAP_FIELDS),
            "data": [map_row(1, last_seen=2**63 - 1, **{"pm2.5_cf_1": 100.0})],
        }, now=NOW)
        self.assertEqual(
            presentation.alert_transitions(
                sensors, favorites={1}, previous={}, threshold=101, now=NOW, origin="live"),
            ([], {}))


class SensorFromValuesTest(unittest.TestCase):
    def test_builds_sensor_and_skips_partial_rows(self):
        full = {"sensor_index": 7, "name": "Deck", "latitude": 45.5, "longitude": -122.6,
                "pm2.5_cf_1": 4.0, "humidity": 40}
        sensor = purpleair.sensor_from_values(full)
        self.assertEqual(sensor.sensor_id, 7)
        self.assertIsNotNone(sensor.aqi)
        self.assertIsNone(purpleair.sensor_from_values({"sensor_index": 7, "pm2.5_cf_1": 4.0}))

    def test_confidence_is_parsed(self):
        base = {"sensor_index": 7, "latitude": 45.5, "longitude": -122.6}
        self.assertEqual(purpleair.sensor_from_values({**base, "confidence": 97}).confidence, 97)
        self.assertEqual(purpleair.sensor_from_values({**base, "confidence": "88"}).confidence, 88)
        self.assertIsNone(purpleair.sensor_from_values(base).confidence)
        self.assertIsNone(purpleair.sensor_from_values({**base, "confidence": "junk"}).confidence)

    def test_fractional_and_nonpositive_sensor_ids_are_rejected(self):
        base = {"latitude": 45.5, "longitude": -122.6}
        for sensor_id in (1.5, "1.5", 0, -1, True, False, float("nan"), float("inf")):
            with self.subTest(sensor_id=sensor_id):
                self.assertIsNone(purpleair.sensor_from_values({**base, "sensor_index": sensor_id}))

    def test_large_integer_sensor_id_is_preserved_exactly(self):
        sensor_id = 9007199254740991
        sensor = purpleair.sensor_from_values(
            {"sensor_index": sensor_id, "latitude": 45.5, "longitude": -122.6}
        )
        self.assertEqual(sensor.sensor_id, sensor_id)
        # One past JavaScript's safe range would alias in the web UI.
        self.assertIsNone(purpleair.sensor_from_values(
            {"sensor_index": sensor_id + 2, "latitude": 45.5, "longitude": -122.6}
        ))

    def test_sensor_ids_enforce_javascript_safe_limit_without_losing_boundaries(self):
        maximum = 2**53 - 1
        base = {"latitude": 45.5, "longitude": -122.6}
        for value in (maximum, str(maximum), "9.007199254740991e15"):
            with self.subTest(valid=value):
                sensor = purpleair.sensor_from_values({**base, "sensor_index": value})
                self.assertEqual(sensor.sensor_id, maximum)
        for value in ("1e5000", "-1e5000", "9.007199254740992e15", 2**53, 2**63 - 1, 10**400):
            with self.subTest(invalid=value):
                self.assertIsNone(purpleair.sensor_from_values({**base, "sensor_index": value}))

    def test_near_integer_fractional_string_sensor_id_is_rejected(self):
        sensor = purpleair.sensor_from_values(
            {"sensor_index": "7.0000000000000001", "latitude": 45.5, "longitude": -122.6}
        )
        self.assertIsNone(sensor)

    def test_out_of_range_and_non_finite_coordinates_are_rejected(self):
        base = {"sensor_index": 7, "latitude": 45.5, "longitude": -122.6}
        invalid = (
            {"latitude": 90.1},
            {"latitude": -90.1},
            {"longitude": 180.1},
            {"longitude": -180.1},
            {"latitude": float("nan")},
            {"longitude": float("inf")},
            {"latitude": 10**400},
            {"longitude": 10**400},
        )
        for override in invalid:
            with self.subTest(override=override):
                self.assertIsNone(purpleair.sensor_from_values({**base, **override}))
        self.assertIsNotNone(purpleair.sensor_from_values({**base, "latitude": 90, "longitude": 180}))
        self.assertIsNotNone(purpleair.sensor_from_values({**base, "latitude": -90, "longitude": -180}))

    def test_huge_measurements_clamp_or_become_unavailable(self):
        base = {"sensor_index": 7, "latitude": 45.5, "longitude": -122.6, "humidity": 50}
        huge = purpleair.sensor_from_values({**base, "pm2.5_cf_1": 1e30})
        self.assertEqual(huge.aqi, 500)
        overflowing = purpleair.sensor_from_values({**base, "pm2.5_cf_1": 1e308})
        self.assertIsNone(overflowing.aqi)
        self.assertIsNone(overflowing.pm25)

    def test_oversized_integer_measurements_become_unavailable(self):
        base = {"sensor_index": 7, "latitude": 45.5, "longitude": -122.6}
        concentration = purpleair.sensor_from_values({**base, "pm2.5_cf_1": 10**400})
        self.assertIsNone(concentration.aqi)
        self.assertIsNone(concentration.pm25)
        humidity = purpleair.sensor_from_values({**base, "pm2.5_cf_1": 20, "humidity": 10**400})
        self.assertIsNone(humidity.humidity)
        self.assertIsNotNone(humidity.aqi)  # Missing humidity uses the neutral default.

    def test_trend_from_values_produces_seven_points(self):
        values = {"sensor_index": 7, "humidity": 40, "pm2.5_cf_1": 4.0,
                  "pm2.5_10minute": 4.2, "pm2.5_30minute": 4.4, "pm2.5_60minute": 4.6,
                  "pm2.5_6hour": 5.0, "pm2.5_24hour": 6.0, "pm2.5_1week": 5.5}
        trend = purpleair.trend_from_values(values)
        self.assertEqual([point["label"] for point in trend],
                         ["1w", "1d", "6h", "1h", "30m", "10m", "Now"])
        self.assertTrue(all(isinstance(point["aqi"], int) for point in trend))

    def test_outdoor_and_indoor_trends_use_their_measurement_basis(self):
        values = {
            "sensor_index": 7,
            "humidity": 50,
            "pm2.5_cf_1": 100,
            "pm2.5_10minute": 100,
            "pm2.5_30minute": 100,
            "pm2.5_60minute": 100,
            "pm2.5_6hour": 100,
            "pm2.5_24hour": 100,
            "pm2.5_1week": 100,
        }
        # At RH=50, CF1(100)=53.84 -> AQI 146; ATM(100)=80.04 -> AQI 168.
        # The explicit pm2.5_cf_1 Now field remains CF1 for both sensor types.
        outdoor = purpleair.trend_from_values({**values, "location_type": 0})
        indoor = purpleair.trend_from_values({**values, "location_type": 1})
        self.assertEqual([point["aqi"] for point in outdoor], [168, 168, 168, 168, 168, 168, 146])
        self.assertEqual([point["aqi"] for point in indoor], [146, 146, 146, 146, 146, 146, 146])


class BooleanValueTest(unittest.TestCase):
    base = {"sensor_index": 7, "latitude": 45.5, "longitude": -122.6, "humidity": 50}

    def test_boolean_pm25_is_unavailable_not_good(self):
        for value in (False, True):
            with self.subTest(value=value):
                sensor = purpleair.sensor_from_values({**self.base, "pm2.5_cf_1": value})
                self.assertIsNone(sensor.aqi)
                self.assertIsNone(sensor.pm25)

    def test_boolean_humidity_uses_neutral_assumption(self):
        sensor = purpleair.sensor_from_values(
            {**self.base, "pm2.5_cf_1": 20, "humidity": True})
        neutral = purpleair.sensor_from_values(
            {**self.base, "pm2.5_cf_1": 20, "humidity": None})
        self.assertIsNone(sensor.humidity)
        self.assertEqual(sensor.aqi, neutral.aqi)

    def test_boolean_metadata_and_secondary_measurements_are_missing(self):
        for key, attribute in (("temperature", "temperature_f"), ("pm1.0", "pm1"),
                               ("pm10.0", "pm10"), ("confidence", "confidence"),
                               ("last_seen", "last_seen")):
            with self.subTest(key=key):
                sensor = purpleair.sensor_from_values({**self.base, key: True})
                self.assertIsNone(getattr(sensor, attribute))
        self.assertIs(
            purpleair.sensor_from_values({**self.base, "location_type": True}).indoor, False)
        self.assertEqual(
            purpleair.trend_from_values(
                {**self.base, "location_type": True, "pm2.5_60minute": 30.0}),
            purpleair.trend_from_values(
                {**self.base, "location_type": 0, "pm2.5_60minute": 30.0}),
        )

    def test_boolean_coordinates_drop_the_row(self):
        for key in ("latitude", "longitude"):
            for value in (True, False):
                with self.subTest(key=key, value=value):
                    self.assertIsNone(purpleair.sensor_from_values({**self.base, key: value}))

    def test_boolean_trend_windows_are_unavailable(self):
        trend = purpleair.trend_from_values(
            {**self.base, "pm2.5_cf_1": 10.0, "pm2.5_60minute": True, "pm2.5_1week": False})
        points = {point["label"]: point["aqi"] for point in trend}
        self.assertIsNone(points["1h"])
        self.assertIsNone(points["1w"])
        self.assertIsNotNone(points["Now"])

    def test_boolean_time_stamp_is_not_a_checkpoint(self):
        result = purpleair.parse_rows({"fields": ["sensor_index"], "data": [], "time_stamp": True})
        self.assertIsNone(result.time_stamp)


class CapBoundsTest(unittest.TestCase):
    def test_small_view_passes_through(self):
        bounds = purpleair.bounds_around(45.5, -122.6, 20.0)
        self.assertEqual(purpleair.cap_bounds(bounds, (45.5, -122.6)), bounds)

    def test_huge_view_is_capped_around_center(self):
        bounds = purpleair.Bounds(49.0, -130.0, 32.0, -114.0)  # ~1900 km tall
        capped = purpleair.cap_bounds(bounds, (40.0, -120.0))
        self.assertEqual(capped, purpleair.bounds_around(40.0, -120.0, 100.0))
        self.assertTrue(purpleair.bounds_contains(bounds, capped))

    def test_narrow_antimeridian_crossing_passes_through(self):
        bounds = purpleair.Bounds(0.1, 179.9, -0.1, -179.9)
        self.assertEqual(purpleair.cap_bounds(bounds, (0.0, 180.0)), bounds)

    def test_over_cap_antimeridian_crossing_uses_wrapped_span(self):
        bounds = purpleair.Bounds(0.1, 100.0, -0.1, -100.0)
        self.assertEqual(
            purpleair.cap_bounds(bounds, (0.0, 0.0)),
            purpleair.bounds_around(0.0, 0.0, 100.0),
        )


if __name__ == "__main__":
    unittest.main()

