from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from airloom import cache
from airloom.purpleair import Bounds, bounds_around, DATA_FIELDS, MAP_FIELDS, FetchResult


def row(sensor_id, lat=45.5, lon=-122.6, **extra):
    values = {"sensor_index": sensor_id, "name": f"S{sensor_id}", "latitude": lat,
              "longitude": lon, "pm2.5_cf_1": 4.0, "humidity": 40}
    values.update(extra)
    return values


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class CacheBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.cache = cache.SensorCache(Path(self.tmp.name) / "cache.db", clock=self.clock)


class StoreFetchTest(CacheBase):
    def test_round_trips_sensors_in_bounds(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1), row(2, lat=45.51)], 1754680000)
        rows = self.cache.sensors_in(bounds)
        self.assertEqual(sorted(r["sensor_index"] for r in rows), [1, 2])

    def test_sensors_outside_bounds_are_excluded(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1), row(3, lat=44.0)], 1754680000)
        self.assertEqual([r["sensor_index"] for r in self.cache.sensors_in(bounds)], [1])

    def test_persists_across_instances(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1)], 1754680000)
        reopened = cache.SensorCache(Path(self.tmp.name) / "cache.db", clock=self.clock)
        self.assertEqual(len(reopened.sensors_in(bounds)), 1)
        self.assertIsNotNone(reopened.covering_region(bounds, "outdoor"))


class CoveringRegionTest(CacheBase):
    def setUp(self):
        super().setUp()
        self.big = bounds_around(45.5, -122.6, 50.0)
        self.small = bounds_around(45.5, -122.6, 10.0)
        self.cache.store_fetch(self.big, "outdoor", [row(1)], 1754680000)

    def test_contained_bounds_hit(self):
        region = self.cache.covering_region(self.small, "outdoor", max_age=120)
        self.assertIsNotNone(region)
        self.assertEqual(region.api_time_stamp, 1754680000)
        self.assertEqual(region.bounds, self.big)

    def test_filter_mismatch_misses(self):
        self.assertIsNone(self.cache.covering_region(self.small, "indoor", max_age=120))

    def test_expired_region_misses_with_max_age(self):
        self.clock.now += 300
        self.assertIsNone(self.cache.covering_region(self.small, "outdoor", max_age=120))
        self.assertIsNotNone(self.cache.covering_region(self.small, "outdoor"))  # any age

    def test_non_contained_bounds_miss(self):
        elsewhere = bounds_around(40.0, -120.0, 10.0)
        self.assertIsNone(self.cache.covering_region(elsewhere, "outdoor", max_age=120))


class UpsertTest(CacheBase):
    def test_delta_merges_onto_cached_metadata(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1, humidity=40)], 1754680000)
        unknown = self.cache.upsert_rows([{"sensor_index": 1, "humidity": 55, "pm2.5_cf_1": 9.0}])
        self.assertEqual(unknown, [])
        merged = self.cache.sensors_in(bounds)[0]
        self.assertEqual(merged["humidity"], 55)
        self.assertEqual(merged["name"], "S1")  # metadata survived the delta

    def test_unknown_partial_rows_are_reported_not_stored(self):
        unknown = self.cache.upsert_rows([{"sensor_index": 99, "pm2.5_cf_1": 9.0}])
        self.assertEqual(unknown, [99])
        self.assertEqual(self.cache.sensors_in(bounds_around(45.5, -122.6, 100.0)), [])

    def test_apply_delta_touches_region(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1)], 1754680000)
        region = self.cache.covering_region(bounds, "outdoor")
        self.clock.now += 300
        self.cache.apply_delta(region.id, [{"sensor_index": 1, "pm2.5_cf_1": 8.0}], 1754680300)
        touched = self.cache.covering_region(bounds, "outdoor", max_age=120)
        self.assertIsNotNone(touched)
        self.assertEqual(touched.api_time_stamp, 1754680300)


class FreshSensorsTest(CacheBase):
    def test_fresh_vs_stale(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1)], 1754680000)
        self.assertIn(1, self.cache.fresh_sensors([1, 2], max_age=120))
        self.assertNotIn(2, self.cache.fresh_sensors([1, 2], max_age=120))
        self.clock.now += 300
        self.assertEqual(self.cache.fresh_sensors([1], max_age=120), {})


class CorruptionTest(unittest.TestCase):
    def test_garbage_db_file_is_recreated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.db"
            path.write_bytes(b"this is not a sqlite database at all")
            c = cache.SensorCache(path, clock=FakeClock())
            bounds = bounds_around(45.5, -122.6, 20.0)
            c.store_fetch(bounds, "outdoor", [row(1)], 1754680000)
            self.assertEqual(len(c.sensors_in(bounds)), 1)


class TrendTest(CacheBase):
    def test_round_trip_and_ttl(self):
        trend = [{"label": "Now", "aqi": 12}]
        self.cache.store_trend(7, trend)
        self.assertEqual(self.cache.get_trend(7, max_age=120), trend)
        self.clock.now += 300
        self.assertIsNone(self.cache.get_trend(7, max_age=120))
        self.assertIsNone(self.cache.get_trend(8, max_age=120))


class PruneTest(CacheBase):
    def test_region_count_is_capped(self):
        for index in range(cache.MAX_REGIONS + 10):
            bounds = bounds_around(45.5 + index * 0.001, -122.6, 5.0)
            self.clock.now += 1
            self.cache.store_fetch(bounds, "outdoor", [], 1754680000 + index)
        count = self.cache._db.execute("SELECT COUNT(*) FROM regions").fetchone()[0]
        self.assertEqual(count, cache.MAX_REGIONS)

    def test_ancient_sensors_and_trends_are_dropped(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1)], 1754680000)
        self.cache.store_trend(1, [{"label": "Now", "aqi": 12}])
        self.clock.now += cache.SENSOR_MAX_AGE + 60
        self.cache.store_fetch(bounds, "outdoor", [row(2)], 1754770000)
        self.assertEqual([r["sensor_index"] for r in self.cache.sensors_in(bounds)], [2])
        self.assertIsNone(self.cache.get_trend(1, max_age=cache.SENSOR_MAX_AGE * 2))


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def fetch_rows(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FetchAreaTest(CacheBase):
    def setUp(self):
        super().setUp()
        self.bounds = bounds_around(45.5, -122.6, 20.0)

    def test_miss_full_fetches_and_stores(self):
        client = FakeClient([FetchResult([row(1)], 1754680000)])
        result = cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        self.assertTrue(result.polled)
        self.assertEqual([r["sensor_index"] for r in result.rows], [1])
        self.assertEqual(client.calls[0]["fields"], MAP_FIELDS)
        self.assertNotIn("modified_since", {k: v for k, v in client.calls[0].items() if v is not None})
        self.assertIsNotNone(self.cache.covering_region(self.bounds, "outdoor", max_age=120))

    def test_fresh_hit_makes_no_calls(self):
        self.cache.store_fetch(self.bounds, "outdoor", [row(1)], 1754680000)
        client = FakeClient([])
        result = cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        self.assertFalse(result.polled)
        self.assertEqual(client.calls, [])
        self.assertEqual(len(result.rows), 1)
        self.assertGreaterEqual(result.age, 0.0)

    def test_stale_hit_delta_polls_with_region_bounds(self):
        big = bounds_around(45.5, -122.6, 50.0)
        self.cache.store_fetch(big, "outdoor", [row(1, humidity=40)], 1754680000)
        self.clock.now += 300
        client = FakeClient([FetchResult([{"sensor_index": 1, "humidity": 60}], 1754680300)])
        result = cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        self.assertTrue(result.polled)
        call = client.calls[0]
        self.assertEqual(call["fields"], DATA_FIELDS)
        self.assertEqual(call["modified_since"], 1754680000)
        self.assertEqual(call["bounds"], big)  # re-poll the whole region it serves
        self.assertEqual(result.rows[0]["humidity"], 60)
        self.assertEqual(result.rows[0]["name"], "S1")

    def test_unknown_delta_ids_trigger_followup_full_fetch(self):
        self.cache.store_fetch(self.bounds, "outdoor", [row(1)], 1754680000)
        self.clock.now += 300
        client = FakeClient([
            FetchResult([{"sensor_index": 2, "pm2.5_cf_1": 9.0}], 1754680300),
            FetchResult([row(2, lat=45.52)], 1754680301),
        ])
        result = cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        self.assertEqual(client.calls[1]["show_only"], [2])
        self.assertEqual(sorted(r["sensor_index"] for r in result.rows), [1, 2])

    def test_force_polls_even_when_fresh(self):
        self.cache.store_fetch(self.bounds, "outdoor", [row(1)], 1754680000)
        client = FakeClient([FetchResult([], 1754680060)])
        result = cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120, force=True)
        self.assertTrue(result.polled)
        self.assertEqual(client.calls[0]["modified_since"], 1754680000)

    def test_indoor_rows_filtered_from_outdoor_view(self):
        rows = [row(1, location_type=0), row(2, lat=45.51, location_type=1)]
        self.cache.store_fetch(self.bounds, "outdoor", rows, 1754680000)
        result = cache.fetch_area(FakeClient([]), self.cache, self.bounds, "outdoor", ttl=120)
        self.assertEqual([r["sensor_index"] for r in result.rows], [1])


class FetchFavoritesTest(CacheBase):
    def test_only_stale_missing_favorites_are_fetched(self):
        self.cache.store_fetch(bounds_around(45.5, -122.6, 20.0), "outdoor", [row(5)], 1754680000)
        client = FakeClient([FetchResult([row(6, lat=39.1)], 1754680060)])
        rows = cache.fetch_favorites(client, self.cache, [5, 6], have_ids={1}, ttl=120)
        self.assertEqual(sorted(r["sensor_index"] for r in rows), [5, 6])
        self.assertEqual(client.calls[0]["show_only"], [6])

    def test_no_call_when_everything_is_covered(self):
        client = FakeClient([])
        self.assertEqual(cache.fetch_favorites(client, self.cache, [5], have_ids={5}, ttl=120), [])
        self.assertEqual(client.calls, [])
