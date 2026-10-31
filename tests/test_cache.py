from __future__ import annotations

import gc
import io
import json
import os
import sqlite3
import tempfile
import unittest
import warnings
import weakref
from pathlib import Path
from unittest import mock

from airloom import cache, models, presentation, purpleair
from airloom.purpleair import (
    Bounds, bounds_around, DATA_FIELDS, MAP_FIELDS, FetchResult, PurpleAirClient,
    PurpleAirError,
)


# A delta checkpoint plausible against FakeClock's default time; cached
# checkpoints further in the future are distrusted.
CHECKPOINT = 900


def row(sensor_id, lat=45.5, lon=-122.6, **extra):
    values = {"sensor_index": sensor_id, "name": f"S{sensor_id}", "latitude": lat,
              "longitude": lon, "last_seen": 1000, "pm2.5_cf_1": 4.0, "humidity": 40}
    values.update(extra)
    return values


def fake_urlopen(payloads, urls):
    """A urlopen stand-in that serves `payloads` in order and records each URL."""
    remaining = list(payloads)

    def opener(request, timeout=None):
        urls.append(request.full_url)
        payload = remaining.pop(0)
        return mock.MagicMock(
            __enter__=lambda s: io.StringIO(json.dumps(payload)),
            __exit__=lambda s, *a: False,
        )

    return opener


def _close_weak_cache(reference):
    instance = reference()
    if instance is not None:
        instance.close()


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
        self.addCleanup(self.cache.close)


class OwnershipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "cache.db"

    def _cache(self):
        c = cache.SensorCache(self.path, clock=FakeClock())
        # Do not retain either the raw connection or the cache across forced GC.
        self.addCleanup(_close_weak_cache, weakref.ref(c))
        return c

    def test_cache_error_is_a_runtime_error(self):
        self.assertTrue(issubclass(cache.CacheError, RuntimeError))

    def test_close_is_idempotent(self):
        c = self._cache()
        connection = c._db
        c.close()
        c.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_context_manager_closes_connection(self):
        c = self._cache()
        connection = c._db
        with c as entered:
            self.assertIs(entered, c)
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_closed_cache_has_no_forced_gc_resource_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            c = self._cache()
            c.close()
            del c
            gc.collect()
        self.assertEqual(
            [item for item in caught if item.category is ResourceWarning], [])


class SchemaVersionTest(CacheBase):
    def _user_version(self):
        return self.cache._db.execute("PRAGMA user_version").fetchone()[0]

    def test_fresh_database_is_stamped_with_current_version(self):
        self.assertEqual(self._user_version(), cache.SCHEMA_VERSION)

    def test_stale_version_clears_all_tables(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1)], CHECKPOINT)
        self.cache.store_trend(1, [{"label": "Now", "aqi": 40}])
        self.cache._db.execute("PRAGMA user_version = 0")  # simulate pre-feature DB
        self.cache._db.commit()
        self.cache.close()
        reopened = cache.SensorCache(Path(self.tmp.name) / "cache.db", clock=self.clock)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.sensors_in(bounds), [])
        self.assertIsNone(reopened.covering_region(bounds, "outdoor"))
        self.assertIsNone(reopened.get_trend(1, max_age=3600))
        self.assertEqual(reopened._db.execute("PRAGMA user_version").fetchone()[0],
                         cache.SCHEMA_VERSION)

    def test_correction_v1_schema_is_cleared(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1)], CHECKPOINT)
        self.cache.store_trend(1, [{"label": "Now", "aqi": 40}])
        self.cache._db.execute("PRAGMA user_version = 1")
        self.cache._db.commit()
        self.cache.close()
        reopened = cache.SensorCache(Path(self.tmp.name) / "cache.db", clock=self.clock)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.sensors_in(bounds), [])
        self.assertIsNone(reopened.covering_region(bounds, "outdoor"))
        self.assertIsNone(reopened.get_trend(1, max_age=3600))
        self.assertEqual(reopened._db.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_current_version_keeps_rows_across_reconnect(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1)], CHECKPOINT)
        self.cache.close()
        reopened = cache.SensorCache(Path(self.tmp.name) / "cache.db", clock=self.clock)
        self.addCleanup(reopened.close)
        self.assertEqual(len(reopened.sensors_in(bounds)), 1)


class RegionCommitLock:
    """Fail the commit that would make a rows+region transaction visible."""

    def __init__(self, connection):
        self.connection = connection
        self.region_staged = False

    def execute(self, sql, parameters=()):
        if sql.lstrip().startswith("INSERT INTO regions"):
            self.region_staged = True
        return self.connection.execute(sql, parameters)

    def commit(self):
        if self.region_staged:
            raise sqlite3.OperationalError("database is locked")
        self.connection.commit()

    def __getattr__(self, name):
        return getattr(self.connection, name)


class StoreFetchTest(CacheBase):
    def test_locked_rows_and_region_commit_rolls_back_everything(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache._db = RegionCommitLock(self.cache._db)
        with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
            self.cache.store_fetch(bounds, "outdoor", [row(1)], CHECKPOINT)
        observer = sqlite3.connect(self.cache.path)
        try:
            counts = (
                observer.execute("SELECT COUNT(*) FROM sensors").fetchone()[0],
                observer.execute("SELECT COUNT(*) FROM regions").fetchone()[0],
            )
        finally:
            observer.close()
        self.assertEqual(counts, (0, 0))

    def test_round_trips_sensors_in_bounds(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1), row(2, lat=45.51)], CHECKPOINT)
        rows = self.cache.sensors_in(bounds)
        self.assertEqual(sorted(r["sensor_index"] for r in rows), [1, 2])

    def test_sensors_outside_bounds_are_excluded(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1), row(3, lat=44.0)], CHECKPOINT)
        self.assertEqual([r["sensor_index"] for r in self.cache.sensors_in(bounds)], [1])

    def test_persists_across_instances(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1)], CHECKPOINT)
        self.cache.close()
        reopened = cache.SensorCache(Path(self.tmp.name) / "cache.db", clock=self.clock)
        self.addCleanup(reopened.close)
        self.assertEqual(len(reopened.sensors_in(bounds)), 1)
        self.assertIsNotNone(reopened.covering_region(bounds, "outdoor"))


class CoveringRegionTest(CacheBase):
    def setUp(self):
        super().setUp()
        self.big = bounds_around(45.5, -122.6, 50.0)
        self.small = bounds_around(45.5, -122.6, 10.0)
        self.cache.store_fetch(self.big, "outdoor", [row(1)], CHECKPOINT)

    def test_contained_bounds_hit(self):
        region = self.cache.covering_region(self.small, "outdoor", max_age=120)
        self.assertIsNotNone(region)
        self.assertEqual(region.api_time_stamp, CHECKPOINT)
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
        self.cache.store_fetch(bounds, "outdoor", [row(1, humidity=40)], CHECKPOINT)
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
        self.cache.store_fetch(bounds, "outdoor", [row(1)], CHECKPOINT)
        region = self.cache.covering_region(bounds, "outdoor")
        self.clock.now += 300
        self.cache.apply_delta(region.id, [{"sensor_index": 1, "pm2.5_cf_1": 8.0}], CHECKPOINT + 300)
        touched = self.cache.covering_region(bounds, "outdoor", max_age=120)
        self.assertIsNotNone(touched)
        self.assertEqual(touched.api_time_stamp, CHECKPOINT + 300)

    def test_hostile_ids_do_not_raise_or_alias_an_existing_sqlite_key(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1)], CHECKPOINT)
        hostile_ids = (
            float("nan"), float("inf"), float("-inf"), 10**400,
            1.5, "1.5", True, False,
        )
        for hostile_id in hostile_ids:
            with self.subTest(sensor_id=hostile_id):
                self.assertEqual(self.cache.upsert_rows([row(hostile_id, name="Hostile")]), [])
                stored = self.cache.sensors_in(bounds)
                self.assertEqual([item["sensor_index"] for item in stored], [1])
                self.assertEqual(stored[0]["name"], "S1")

    def test_string_ids_are_normalized_at_the_cache_boundary(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.assertEqual(self.cache.upsert_rows([row("2")]), [])
        stored = self.cache.sensors_in(bounds)
        self.assertEqual([item["sensor_index"] for item in stored], [2])
        self.assertIsInstance(stored[0]["sensor_index"], int)

    def test_invalid_coordinates_are_unresolved_and_never_stored(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        invalid_coordinates = (
            {"latitude": 91}, {"latitude": float("nan")},
            {"longitude": 181}, {"longitude": float("inf")},
            {"latitude": 10**400},
        )
        for index, override in enumerate(invalid_coordinates, start=10):
            with self.subTest(override=override):
                hostile = row(index, **override)
                self.assertEqual(self.cache.unknown_rows([hostile]), [index])
                self.assertEqual(self.cache.upsert_rows([hostile]), [index])
                self.assertEqual(self.cache.sensors_in(bounds), [])


class FreshSensorsTest(CacheBase):
    def test_fresh_vs_stale(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1)], CHECKPOINT)
        self.assertIn(1, self.cache.fresh_sensors([1, 2], max_age=120))
        self.assertNotIn(2, self.cache.fresh_sensors([1, 2], max_age=120))
        self.clock.now += 300
        self.assertEqual(self.cache.fresh_sensors([1], max_age=120), {})


class CorruptionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "cache.db"

    def _create_healthy_cache(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        with cache.SensorCache(self.path, clock=FakeClock()) as c:
            c.store_fetch(bounds, "outdoor", [row(1)], CHECKPOINT)
        return bounds

    def test_garbage_db_file_is_recreated_without_leaking_failed_connection(self):
        self.path.write_bytes(b"this is not a sqlite database at all")
        bounds = bounds_around(45.5, -122.6, 20.0)
        with self.path.open("rb") as original:
            original_inode = os.fstat(original.fileno()).st_ino
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                with cache.SensorCache(self.path, clock=FakeClock()) as c:
                    c.store_fetch(bounds, "outdoor", [row(1)], CHECKPOINT)
                    self.assertEqual(len(c.sensors_in(bounds)), 1)
                gc.collect()
            self.assertEqual(
                [item for item in caught if item.category is ResourceWarning], [])
            self.assertNotEqual(self.path.stat().st_ino, original_inode)

    def test_non_corruption_constructor_failure_closes_connection_and_preserves_file(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("CREATE VIEW sensors AS SELECT 1 AS sensor_index")
            db.commit()
        finally:
            db.close()
        with self.path.open("rb") as original:
            original_inode = os.fstat(original.fileno()).st_ino
            failure = False
            opened = None
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                try:
                    opened = cache.SensorCache(self.path, clock=FakeClock())
                except cache.CacheError:
                    failure = True
                if opened is not None:
                    opened.close()
                gc.collect()
            self.assertEqual(
                [item for item in caught if item.category is ResourceWarning], [])
            self.assertEqual(self.path.stat().st_ino, original_inode)
            self.assertTrue(failure)

    def test_locked_healthy_db_preserves_inode_and_data(self):
        bounds = self._create_healthy_cache()
        with self.path.open("rb") as original:
            original_inode = os.fstat(original.fileno()).st_ino
            locker = sqlite3.connect(self.path, timeout=0)
            try:
                locker.execute("BEGIN EXCLUSIVE")
                opened = None
                try:
                    opened = cache.SensorCache(self.path, clock=FakeClock())
                except cache.CacheError:
                    pass
                if opened is not None:
                    opened.close()
                self.assertEqual(self.path.stat().st_ino, original_inode)
                self.assertIsNone(opened)
            finally:
                locker.close()
        with cache.SensorCache(self.path, clock=FakeClock()) as reopened:
            self.assertEqual(
                [item["sensor_index"] for item in reopened.sensors_in(bounds)], [1])


class TrendTest(CacheBase):
    def test_round_trip_and_ttl(self):
        trend = [{"label": "Now", "aqi": 12}]
        self.cache.store_trend(7, trend)
        self.assertEqual(self.cache.get_trend(7, max_age=120), trend)
        self.clock.now += 300
        self.assertIsNone(self.cache.get_trend(7, max_age=120))
        self.assertIsNone(self.cache.get_trend(8, max_age=120))


class TransactionRollbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "cache.db"
        self.cache = cache.SensorCache(self.path, clock=FakeClock(), busy_timeout=0.01)
        self.addCleanup(self.cache.close)

    def _hold_read_lock(self):
        """Keep a SHARED lock open so the cache's COMMIT fails with "locked"."""
        reader = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM trends").fetchone()
        return reader

    def test_failed_trend_commit_releases_transaction_and_locks(self):
        reader = self._hold_read_lock()
        with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
            self.cache.store_trend(1, [{"label": "Now", "aqi": 5}])
        self.assertFalse(self.cache._db.in_transaction)
        self.assertIsNone(self.cache.get_trend(1, max_age=3600))
        reader.execute("COMMIT")
        writer = sqlite3.connect(self.path, timeout=0.01)
        self.addCleanup(writer.close)
        writer.execute("INSERT INTO trends VALUES (2, '[]', 1000.0)")  # no lock retained
        writer.commit()
        self.cache.store_trend(1, [{"label": "Now", "aqi": 5}])  # retry succeeds
        self.assertEqual(self.cache.get_trend(1, max_age=3600), [{"label": "Now", "aqi": 5}])

    def test_failed_clear_keeps_data_and_is_not_committed_later(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1)], CHECKPOINT)
        self.cache.store_trend(1, [{"label": "Now", "aqi": 5}])
        reader = self._hold_read_lock()
        with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
            self.cache.clear()
        self.assertFalse(self.cache._db.in_transaction)
        reader.execute("COMMIT")
        self.cache.store_trend(2, [])  # an unrelated commit must not carry the clear
        observer = sqlite3.connect(self.path)
        try:
            counts = tuple(
                observer.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("sensors", "regions", "trends")
            )
        finally:
            observer.close()
        self.assertEqual(counts, (1, 1, 2))


class PruneTest(CacheBase):
    def test_region_count_is_capped(self):
        for index in range(cache.MAX_REGIONS + 10):
            bounds = bounds_around(45.5 + index * 0.001, -122.6, 5.0)
            self.clock.now += 1
            self.cache.store_fetch(bounds, "outdoor", [], CHECKPOINT + index)
        count = self.cache._db.execute("SELECT COUNT(*) FROM regions").fetchone()[0]
        self.assertEqual(count, cache.MAX_REGIONS)

    def test_ancient_sensors_and_trends_are_dropped(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1)], CHECKPOINT)
        self.cache.store_trend(1, [{"label": "Now", "aqi": 12}])
        self.clock.now += cache.SENSOR_MAX_AGE + 60
        self.cache.store_fetch(
            bounds, "outdoor", [row(2, last_seen=self.clock.now)], CHECKPOINT + 86400)
        self.assertEqual([r["sensor_index"] for r in self.cache.sensors_in(bounds)], [2])
        self.assertIsNone(self.cache.get_trend(1, max_age=cache.SENSOR_MAX_AGE * 2))

    def test_observation_prune_invalidates_coverage_before_revisit(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1)], CHECKPOINT)
        self.clock.now += 24 * 3600 - 10
        self.cache.upsert_rows([{"sensor_index": 1, "last_seen": 1000, "humidity": 55}])
        self.clock.now += 20
        self.cache.upsert_rows([])
        self.assertEqual(self.cache.sensors_in(bounds), [])
        self.assertIsNone(self.cache.covering_region(bounds, "outdoor"))

        client = FakeClient([FetchResult([row(2, last_seen=self.clock.now)], CHECKPOINT + 86400)])
        result = cache.fetch_area(client, self.cache, bounds, "outdoor", ttl=120)
        self.assertEqual([r["sensor_index"] for r in result.rows], [2])
        self.assertEqual(client.calls[0]["fields"], MAP_FIELDS)
        self.assertNotIn("modified_since", client.calls[0])


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def fetch_rows(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        fields = kwargs.get("fields")
        if fields is not None:
            response = FetchResult(
                [{key: value for key, value in values.items() if key in fields}
                 for values in response.rows],
                response.time_stamp,
            )
        return response


class FetchAreaTest(CacheBase):
    def setUp(self):
        super().setUp()
        self.bounds = bounds_around(45.5, -122.6, 20.0)

    def test_miss_full_fetches_and_stores(self):
        client = FakeClient([FetchResult([row(1)], CHECKPOINT)])
        result = cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        self.assertTrue(result.polled)
        self.assertEqual([r["sensor_index"] for r in result.rows], [1])
        self.assertEqual(client.calls[0]["fields"], MAP_FIELDS)
        self.assertNotIn("modified_since", {k: v for k, v in client.calls[0].items() if v is not None})
        self.assertIsNotNone(self.cache.covering_region(self.bounds, "outdoor", max_age=120))

    def test_fresh_hit_makes_no_calls(self):
        self.cache.store_fetch(self.bounds, "outdoor", [row(1)], CHECKPOINT)
        client = FakeClient([])
        result = cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        self.assertFalse(result.polled)
        self.assertEqual(client.calls, [])
        self.assertEqual(len(result.rows), 1)
        self.assertGreaterEqual(result.age, 0.0)

    def test_stale_observation_cannot_use_long_lived_area_cache_hit(self):
        self.cache.store_fetch(self.bounds, "outdoor", [row(1, name="Original")], CHECKPOINT)
        self.clock.now += 25 * 3600
        client = FakeClient([FetchResult([
            row(1, name="Fresh", last_seen=self.clock.now)
        ], CHECKPOINT + 300)])
        result = cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=48 * 3600)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["fields"], MAP_FIELDS)
        self.assertNotIn("modified_since", client.calls[0])
        self.assertEqual(result.rows[0]["name"], "Fresh")

    def test_area_cache_hit_expires_metadata_at_24_hours(self):
        self.cache.store_fetch(self.bounds, "outdoor", [row(1, name="Original")], CHECKPOINT)
        self.clock.now += 12 * 3600
        self.cache.upsert_rows([{"sensor_index": 1, "last_seen": self.clock.now}])
        self.clock.now = 1000 + 24 * 3600
        client = FakeClient([FetchResult([
            row(1, name="Current", last_seen=self.clock.now)
        ], CHECKPOINT + 300)])
        result = cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=48 * 3600)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["fields"], MAP_FIELDS)
        self.assertNotIn("modified_since", client.calls[0])
        self.assertEqual(result.rows[0]["name"], "Current")

    def test_stale_hit_delta_polls_with_region_bounds(self):
        big = bounds_around(45.5, -122.6, 50.0)
        self.cache.store_fetch(big, "outdoor", [row(1, humidity=40)], CHECKPOINT)
        self.clock.now += 300
        client = FakeClient([FetchResult([{"sensor_index": 1, "humidity": 60}], CHECKPOINT + 300)])
        result = cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        self.assertTrue(result.polled)
        call = client.calls[0]
        self.assertEqual(call["fields"], DATA_FIELDS)
        self.assertEqual(call["modified_since"], CHECKPOINT)
        self.assertEqual(call["bounds"], big)  # re-poll the whole region it serves
        self.assertEqual(result.rows[0]["humidity"], 60)
        self.assertEqual(result.rows[0]["name"], "S1")

    def test_unknown_delta_ids_trigger_followup_full_fetch(self):
        self.cache.store_fetch(self.bounds, "outdoor", [row(1)], CHECKPOINT)
        self.clock.now += 300
        client = FakeClient([
            FetchResult([{"sensor_index": 2, "pm2.5_cf_1": 9.0}], CHECKPOINT + 300),
            FetchResult([row(2, lat=45.52)], CHECKPOINT + 301),
        ])
        result = cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        self.assertEqual(client.calls[1]["show_only"], [2])
        self.assertEqual(sorted(r["sensor_index"] for r in result.rows), [1, 2])

    def test_failed_backfill_leaves_retry_checkpoint_and_rows_unchanged(self):
        original_stamp = CHECKPOINT
        self.cache.store_fetch(self.bounds, "outdoor", [row(1, humidity=40)], original_stamp)
        self.clock.now += 300
        delta = FetchResult([
            {"sensor_index": 1, "last_seen": 1300, "humidity": 60},
            {"sensor_index": 2, "last_seen": 1300, "pm2.5_cf_1": 9.0},
        ], CHECKPOINT + 300)
        client = FakeClient([
            delta,
            PurpleAirError("backfill failed"),
            delta,
            FetchResult([row(2, lat=45.52, last_seen=1300)], CHECKPOINT + 301),
        ])
        with self.assertRaisesRegex(PurpleAirError, "backfill failed"):
            cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        region = self.cache.covering_region(self.bounds, "outdoor")
        self.assertEqual(region.api_time_stamp, original_stamp)
        self.assertEqual(self.cache.sensors_in(self.bounds)[0]["humidity"], 40)

        result = cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        self.assertEqual(client.calls[0]["modified_since"], original_stamp)
        self.assertEqual(client.calls[2]["modified_since"], original_stamp)
        self.assertEqual(sorted(r["sensor_index"] for r in result.rows), [1, 2])
        self.assertEqual(self.cache.covering_region(self.bounds, "outdoor").api_time_stamp,
                         CHECKPOINT + 300)

    def test_empty_backfill_leaves_rows_and_checkpoint_unchanged_for_retry(self):
        original_stamp = CHECKPOINT
        self.cache.store_fetch(self.bounds, "outdoor", [row(1, humidity=40)], original_stamp)
        self.clock.now += 300
        delta = FetchResult([
            {"sensor_index": 1, "last_seen": 1300, "humidity": 60},
            {"sensor_index": 2, "last_seen": 1300, "pm2.5_cf_1": 9.0},
        ], CHECKPOINT + 300)
        client = FakeClient([
            delta,
            FetchResult([], CHECKPOINT + 301),
            delta,
            FetchResult([row(2, lat=45.52, last_seen=1300)], CHECKPOINT + 302),
        ])
        try:
            cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        except PurpleAirError:
            pass
        self.assertEqual(
            self.cache.covering_region(self.bounds, "outdoor").api_time_stamp,
            original_stamp,
        )
        stored = self.cache.sensors_in(self.bounds)
        self.assertEqual([r["sensor_index"] for r in stored], [1])
        self.assertEqual(stored[0]["humidity"], 40)

        result = cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        self.assertEqual(client.calls[2]["modified_since"], original_stamp)
        self.assertEqual(sorted(r["sensor_index"] for r in result.rows), [1, 2])

    def test_partial_backfill_leaves_rows_and_checkpoint_unchanged_for_retry(self):
        original_stamp = CHECKPOINT
        self.cache.store_fetch(self.bounds, "outdoor", [row(1)], original_stamp)
        self.clock.now += 300
        delta = FetchResult([
            {"sensor_index": 2, "last_seen": 1300, "pm2.5_cf_1": 9.0},
            {"sensor_index": 3, "last_seen": 1300, "pm2.5_cf_1": 10.0},
        ], CHECKPOINT + 300)
        client = FakeClient([
            delta,
            FetchResult([
                row(2, lat=45.52, last_seen=1300),
                {"sensor_index": 3, "latitude": 45.53, "last_seen": 1300},
            ], CHECKPOINT + 301),
            delta,
            FetchResult([
                row(2, lat=45.52, last_seen=1300),
                row(3, lat=45.53, last_seen=1300),
            ], CHECKPOINT + 302),
        ])
        try:
            cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        except PurpleAirError:
            pass
        self.assertEqual(
            self.cache.covering_region(self.bounds, "outdoor").api_time_stamp,
            original_stamp,
        )
        self.assertEqual([r["sensor_index"] for r in self.cache.sensors_in(self.bounds)], [1])

        result = cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        self.assertEqual(client.calls[2]["modified_since"], original_stamp)
        self.assertEqual(sorted(r["sensor_index"] for r in result.rows), [1, 2, 3])

    def test_invalid_backfill_coordinates_leave_rows_freshness_and_checkpoint_unchanged(self):
        original_stamp = CHECKPOINT
        self.cache.store_fetch(
            self.bounds, "outdoor", [row(1, humidity=40)], original_stamp)
        original_region = self.cache.covering_region(self.bounds, "outdoor")
        original_row = self.cache.sensors_in(self.bounds)
        original_fetched_at = self.cache._db.execute(
            "SELECT fetched_at FROM sensors WHERE sensor_index = 1").fetchone()[0]
        self.clock.now += 300
        client = FakeClient([
            FetchResult([
                {"sensor_index": 1, "last_seen": 1300, "humidity": 60},
                {"sensor_index": 2, "last_seen": 1300, "pm2.5_cf_1": 9.0},
            ], CHECKPOINT + 300),
            FetchResult([row(2, latitude=91, last_seen=1300)], CHECKPOINT + 301),
        ])
        with self.assertRaisesRegex(PurpleAirError, "required sensor metadata"):
            cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        self.assertEqual(self.cache.covering_region(self.bounds, "outdoor"), original_region)
        self.assertEqual(self.cache.sensors_in(self.bounds), original_row)
        self.assertEqual(
            self.cache._db.execute(
                "SELECT fetched_at FROM sensors WHERE sensor_index = 1").fetchone()[0],
            original_fetched_at,
        )

    def test_repeated_empty_deltas_do_not_delay_metadata_snapshot(self):
        self.cache.store_fetch(self.bounds, "outdoor", [row(1, name="Original")], CHECKPOINT)
        client = FakeClient([
            FetchResult([], CHECKPOINT + 1),
            FetchResult([], CHECKPOINT + 2),
            FetchResult([], CHECKPOINT + 3),
            FetchResult([row(1, name="Current", last_seen=1000 + 25 * 3600)], CHECKPOINT + 4),
        ])
        for hours in (6, 12, 18):
            self.clock.now = 1000 + hours * 3600
            cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        self.clock.now = 1000 + 25 * 3600
        result = cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        self.assertEqual([call["fields"] for call in client.calls],
                         [DATA_FIELDS, DATA_FIELDS, DATA_FIELDS, MAP_FIELDS])
        self.assertNotIn("modified_since", client.calls[-1])
        self.assertEqual(result.rows[0]["name"], "Current")

    def test_metadata_snapshot_applies_rename_relocation_and_reclassification(self):
        self.cache.store_fetch(
            self.bounds, "outdoor", [row(1, name="Original", location_type=0)], CHECKPOINT)
        self.clock.now += 25 * 3600
        client = FakeClient([FetchResult([
            row(1, name="Renamed", lat=45.52, location_type=1, last_seen=self.clock.now)
        ], CHECKPOINT + 300)])
        result = cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        self.assertEqual(result.rows, [])
        stored = self.cache.sensors_in(self.bounds)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["name"], "Renamed")
        self.assertEqual(stored[0]["latitude"], 45.52)
        self.assertEqual(stored[0]["location_type"], 1)
        self.assertEqual(client.calls[0]["fields"], MAP_FIELDS)
        self.assertNotIn("modified_since", client.calls[0])

    def test_empty_authoritative_snapshot_removes_recently_refreshed_omitted_sensor(self):
        self.cache.store_fetch(
            self.bounds, "outdoor", [row(1, location_type=0)], CHECKPOINT)
        self.clock.now = 1000 + 12 * 3600
        self.cache.upsert_rows([{"sensor_index": 1, "last_seen": self.clock.now}])
        self.clock.now = 1000 + 24 * 3600
        client = FakeClient([FetchResult([], CHECKPOINT + 300)])
        result = cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=48 * 3600)
        self.assertEqual(client.calls[0]["fields"], MAP_FIELDS)
        self.assertNotIn("modified_since", client.calls[0])
        self.assertEqual(result.rows, [])
        self.assertEqual(self.cache.sensors_in(self.bounds), [])

    def test_omission_reclassification_preserves_other_filters_and_overlapping_regions(self):
        outside = Bounds(46.0, -122.6, 45.0, -122.0)
        self.cache.store_fetch(self.bounds, "outdoor", [
            row(1, location_type=0),
            row(2, lat=45.51, location_type=1),
            row(3, lat=45.51, lon=-122.2, location_type=0),
        ], CHECKPOINT)
        self.cache.store_fetch(outside, "outdoor", [
            row(3, lat=45.51, lon=-122.2, location_type=0),
        ], CHECKPOINT + 1)
        self.clock.now = 1000 + 12 * 3600
        self.cache.upsert_rows([
            {"sensor_index": sensor_id, "last_seen": self.clock.now}
            for sensor_id in (1, 2, 3)
        ])
        self.clock.now = 1000 + 24 * 3600
        client = FakeClient([FetchResult([], CHECKPOINT + 300)])
        result = cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=48 * 3600)
        self.assertEqual(result.rows, [])
        remaining = self.cache.sensors_in(Bounds(47.0, -180.0, 44.0, 180.0))
        self.assertEqual(sorted(item["sensor_index"] for item in remaining), [2, 3])
        self.assertEqual(
            [item["sensor_index"] for item in self.cache.sensors_in(outside)
             if item["location_type"] == 0],
            [3],
        )
        self.assertIsNotNone(self.cache.covering_region(outside, "outdoor"))

    def test_overlapping_full_snapshot_preserves_absent_rows_outside_bounds(self):
        left = Bounds(46.0, -123.0, 45.0, -122.4)
        right = Bounds(46.0, -122.6, 45.0, -122.0)
        self.cache.store_fetch(left, "outdoor", [row(1, lon=-122.9)], CHECKPOINT)
        self.clock.now += 1
        self.cache.store_fetch(right, "outdoor", [row(2, lon=-122.2)], CHECKPOINT + 1)
        self.clock.now = 1000 + 12 * 3600
        self.cache.upsert_rows([{"sensor_index": 2, "last_seen": self.clock.now}])
        self.clock.now = 1000 + 25 * 3600
        client = FakeClient([FetchResult([
            row(1, name="Left refreshed", lon=-122.9, last_seen=self.clock.now)
        ], CHECKPOINT + 300)])
        result = cache.fetch_area(client, self.cache, left, "outdoor", ttl=120)
        self.assertEqual(client.calls[0]["fields"], MAP_FIELDS)
        self.assertEqual(result.rows[0]["name"], "Left refreshed")
        self.assertEqual([r["sensor_index"] for r in self.cache.sensors_in(right)], [2])

    def test_crossing_bounds_split_queries_and_dedupe_sensor_ids(self):
        crossing = Bounds(10.0, 170.0, -10.0, -170.0)
        client = FakeClient([
            FetchResult([row(1, lat=0.0, lon=175.0)], CHECKPOINT),
            FetchResult([
                row(1, lat=0.0, lon=175.0),
                row(2, lat=0.0, lon=-175.0),
            ], CHECKPOINT + 1),
        ])
        result = cache.fetch_area(client, self.cache, crossing, "outdoor", ttl=120)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0]["bounds"], Bounds(10.0, 170.0, -10.0, 180.0))
        self.assertEqual(client.calls[1]["bounds"], Bounds(10.0, -180.0, -10.0, -170.0))
        self.assertEqual(sorted(r["sensor_index"] for r in result.rows), [1, 2])
        self.assertEqual(self.cache.covering_region(crossing, "outdoor").api_time_stamp, CHECKPOINT)

    def test_force_polls_even_when_fresh(self):
        self.cache.store_fetch(self.bounds, "outdoor", [row(1)], CHECKPOINT)
        client = FakeClient([FetchResult([], CHECKPOINT + 60)])
        result = cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120, force=True)
        self.assertTrue(result.polled)
        self.assertEqual(client.calls[0]["modified_since"], CHECKPOINT)

    def test_indoor_rows_filtered_from_outdoor_view(self):
        rows = [row(1, location_type=0), row(2, lat=45.51, location_type=1)]
        self.cache.store_fetch(self.bounds, "outdoor", rows, CHECKPOINT)
        result = cache.fetch_area(FakeClient([]), self.cache, self.bounds, "outdoor", ttl=120)
        self.assertEqual([r["sensor_index"] for r in result.rows], [1])


class BooleanCachedValueTest(CacheBase):
    bounds = bounds_around(45.5, -122.6, 20.0)

    def test_cached_boolean_measurements_stay_unavailable(self):
        client = FakeClient([FetchResult(
            [row(1, **{"pm2.5_cf_1": False}), row(2, humidity=True, **{"pm2.5_cf_1": 20.0})],
            CHECKPOINT,
        )])
        result = cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        neutral = purpleair.sensor_from_values(row(2, humidity=None, **{"pm2.5_cf_1": 20.0}))
        for rows in (result.rows, self.cache.sensors_in(self.bounds)):
            sensors = {r["sensor_index"]: purpleair.sensor_from_values(r) for r in rows}
            self.assertIsNone(sensors[1].aqi)
            self.assertEqual(sensors[2].aqi, neutral.aqi)

    def test_boolean_coordinates_are_unresolved_and_not_stored(self):
        self.assertEqual(self.cache.upsert_rows([row(3, lat=True)]), [3])
        self.assertEqual(self.cache.sensors_in(self.bounds), [])

    def test_boolean_last_seen_does_not_become_epoch_one(self):
        self.clock.now = 200000.0
        self.cache.store_fetch(self.bounds, "outdoor", [row(3, last_seen=200000)], 200000)
        self.cache.upsert_rows([row(4, last_seen=True)])
        observed_at = self.cache._db.execute(
            "SELECT observed_at FROM sensors WHERE sensor_index = 4").fetchone()[0]
        self.assertEqual(observed_at, self.clock.now)
        self.cache.prune()
        self.assertEqual(
            sorted(r["sensor_index"] for r in self.cache.sensors_in(self.bounds)), [3, 4])
        self.assertIsNotNone(self.cache.covering_region(self.bounds, "outdoor"))

    def test_boolean_location_type_is_outdoor_for_filters(self):
        self.cache.store_fetch(
            self.bounds, "indoor", [row(5, location_type=True)], CHECKPOINT)
        result = cache.fetch_area(FakeClient([]), self.cache, self.bounds, "indoor", ttl=120)
        self.assertEqual(result.rows, [])


class FutureObservationTest(CacheBase):
    NOW = 1_800_000_000.0

    def setUp(self):
        super().setUp()
        self.clock.now = self.NOW
        self.bounds = bounds_around(45.5, -122.6, 20.0)

    def _observed_at(self, sensor_index):
        return self.cache._db.execute(
            "SELECT observed_at FROM sensors WHERE sensor_index = ?", (sensor_index,)).fetchone()[0]

    def test_future_last_seen_is_stored_as_missing_and_still_ages_out(self):
        self.cache.upsert_rows([row(1, last_seen=int(self.NOW) * 1000)])
        self.assertIsNone(self.cache.sensors_in(self.bounds)[0]["last_seen"])
        self.assertEqual(self._observed_at(1), self.NOW)
        self.clock.now += cache.SENSOR_MAX_AGE + 1
        self.cache.prune()
        self.assertEqual(self.cache.sensors_in(self.bounds), [])

    def test_legacy_future_observed_at_is_pruned(self):
        self.cache._db.execute(
            "INSERT INTO sensors (sensor_index, latitude, longitude, data, fetched_at,"
            " observed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (1, 45.5, -122.6, json.dumps(row(1)), self.NOW, self.NOW * 1000),
        )
        self.cache._db.commit()
        self.cache.prune()
        count = self.cache._db.execute("SELECT COUNT(*) FROM sensors").fetchone()[0]
        self.assertEqual(count, 0)

    def test_future_delta_checkpoint_keeps_previous_checkpoint(self):
        now = int(self.NOW)
        self.cache.store_fetch(self.bounds, "outdoor", [row(1, last_seen=now)], now)
        self.clock.now += 300
        client = PurpleAirClient("key", clock=self.clock)
        urls = []
        payloads = [
            {"fields": list(DATA_FIELDS), "data": [[1, now + 300, 50, 70, 1.0, 4.0, 5.0, 99]],
             "time_stamp": now * 1000},
            {"fields": list(DATA_FIELDS), "data": [], "time_stamp": now + 600},
        ]
        with mock.patch("airloom.purpleair.urlopen", side_effect=fake_urlopen(payloads, urls)):
            cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
            self.assertEqual(
                self.cache.covering_region(self.bounds, "outdoor").api_time_stamp, now)
            self.clock.now += 300
            cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        self.assertEqual(len(urls), 2)
        self.assertIn(f"modified_since={now}", urls[1])

    def test_future_full_snapshot_stamp_forces_next_full_fetch(self):
        now = int(self.NOW)
        client = PurpleAirClient("key", clock=self.clock)
        urls = []
        map_row = [row(1, last_seen=now).get(field) for field in MAP_FIELDS]
        payloads = [
            {"fields": list(MAP_FIELDS), "data": [map_row], "time_stamp": now * 1000},
            {"fields": list(MAP_FIELDS), "data": [map_row], "time_stamp": now + 300},
        ]
        with mock.patch("airloom.purpleair.urlopen", side_effect=fake_urlopen(payloads, urls)):
            cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
            self.assertIsNone(self.cache.covering_region(self.bounds, "outdoor").api_time_stamp)
            self.clock.now += 300
            cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        self.assertEqual(len(urls), 2)
        self.assertNotIn("modified_since", urls[1])
        self.assertIn("latitude", urls[1].split("fields=")[1].split("&")[0])

    def _stored_checkpoints(self):
        return [stamp for (stamp,) in self.cache._db.execute(
            "SELECT api_time_stamp FROM regions ORDER BY id")]

    def _seed_legacy_future_checkpoint(self):
        """A region as 0.13 could persist it: a checkpoint far in the future."""
        now = int(self.NOW)
        self.cache.store_fetch(self.bounds, "outdoor", [row(1, last_seen=now)], now)
        self.cache._db.execute("UPDATE regions SET api_time_stamp = ?", (now * 1000,))
        self.cache._db.commit()

    def _fetch_with_real_client(self, force):
        now = int(self.NOW)
        map_row = [row(1, last_seen=now + 300).get(field) for field in MAP_FIELDS]
        urls = []
        payloads = [{"fields": list(MAP_FIELDS), "data": [map_row], "time_stamp": now + 300}]
        client = PurpleAirClient("key", clock=self.clock)
        with mock.patch("airloom.purpleair.urlopen", side_effect=fake_urlopen(payloads, urls)):
            cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120, force=force)
        return urls

    def test_legacy_future_region_checkpoint_selects_full_snapshot(self):
        for force in (True, False):
            with self.subTest(force=force):
                self.cache.clear()
                self.clock.now = self.NOW
                self._seed_legacy_future_checkpoint()
                self.cache.prune()
                self.clock.now += 300
                urls = self._fetch_with_real_client(force)
                self.assertEqual(len(urls), 1)
                self.assertNotIn("modified_since", urls[0])
                self.assertIn("latitude", urls[0].split("fields=")[1].split("&")[0])
                self.assertEqual(
                    self.cache.covering_region(self.bounds, "outdoor").api_time_stamp,
                    int(self.NOW) + 300,
                )

    def test_legacy_future_checkpoint_is_absent_from_covering_region(self):
        self._seed_legacy_future_checkpoint()
        self.assertIsNone(self.cache.covering_region(self.bounds, "outdoor").api_time_stamp)

    def test_store_fetch_persists_future_checkpoint_as_missing(self):
        self.cache.store_fetch(self.bounds, "outdoor", [row(1)], int(self.NOW) * 1000)
        self.assertEqual(self._stored_checkpoints(), [None])

    def test_store_fetch_keeps_checkpoint_at_skew_tolerance(self):
        stamp = int(self.NOW) + models.FUTURE_SKEW_TOLERANCE
        self.cache.store_fetch(self.bounds, "outdoor", [row(1)], stamp)
        self.assertEqual(self._stored_checkpoints(), [stamp])

    def test_apply_delta_persists_future_checkpoint_as_missing(self):
        now = int(self.NOW)
        self.cache.store_fetch(self.bounds, "outdoor", [row(1)], now)
        region_id = self.cache.covering_region(self.bounds, "outdoor").id
        # A future response checkpoint never replaces a plausible one...
        self.cache.apply_delta(region_id, [], now * 1000)
        self.assertEqual(self._stored_checkpoints(), [now])
        # ...nor does an existing future checkpoint survive the COALESCE.
        for checkpoint in (None, now * 1000):
            with self.subTest(checkpoint=checkpoint):
                self.cache._db.execute("UPDATE regions SET api_time_stamp = ?", (now * 1000,))
                self.cache._db.commit()
                self.cache.apply_delta(region_id, [], checkpoint)
                self.assertEqual(self._stored_checkpoints(), [None])

    def test_plausible_checkpoint_is_still_sent_as_modified_since(self):
        now = int(self.NOW)
        self.cache.store_fetch(self.bounds, "outdoor", [row(1, last_seen=now)], now)
        self.clock.now += 300
        client = PurpleAirClient("key", clock=self.clock)
        urls = []
        payloads = [{"fields": list(DATA_FIELDS), "data": [], "time_stamp": now + 300}]
        with mock.patch("airloom.purpleair.urlopen", side_effect=fake_urlopen(payloads, urls)):
            cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        self.assertEqual(len(urls), 1)
        self.assertIn(f"modified_since={now}", urls[0])
        self.assertEqual(self._stored_checkpoints(), [now + 300])


class MalformedResponseTest(CacheBase):
    def setUp(self):
        super().setUp()
        self.bounds = bounds_around(45.5, -122.6, 20.0)

    def _region(self):
        return self.cache._db.execute(
            "SELECT COUNT(*), MAX(fetched_at) FROM regions").fetchone()

    def test_short_delta_row_cannot_refresh_old_measurement_or_alert(self):
        self.clock.now = 100000.0
        self.cache.store_fetch(self.bounds, "outdoor", [
            row(1, humidity=50, last_seen=100000, **{"pm2.5_cf_1": 100.0})], 100000)
        self.clock.now = 110000.0
        client = PurpleAirClient("key", clock=self.clock)
        payload = {"fields": list(DATA_FIELDS), "data": [[1, 110000]], "time_stamp": 110000}
        with mock.patch("airloom.purpleair.urlopen", side_effect=fake_urlopen([payload], [])):
            with self.assertRaises(PurpleAirError):
                cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
        rows = self.cache.sensors_in(self.bounds)
        self.assertEqual(rows[0]["last_seen"], 100000)
        self.assertEqual(rows[0]["pm2.5_cf_1"], 100.0)
        region = self.cache.covering_region(self.bounds, "outdoor")
        self.assertEqual(region.api_time_stamp, 100000)
        self.assertEqual(region.fetched_at, 100000.0)
        sensors = [purpleair.sensor_from_values(r, now=110000) for r in rows]
        self.assertEqual(
            presentation.alert_transitions(
                sensors, favorites={1}, previous={}, threshold=101, now=110000, origin="live"),
            ([], {}))

    def test_malformed_full_snapshot_preserves_cache_and_checkpoint(self):
        self.clock.now = 100000.0
        self.cache.store_fetch(self.bounds, "outdoor", [
            row(1, last_seen=100000), row(2, last_seen=100000)], None)
        self.clock.now += 300
        client = PurpleAirClient("key", clock=self.clock)
        payloads = {
            "non-list row": {"fields": list(MAP_FIELDS), "data": ["not a row"]},
            "short row": {"fields": list(MAP_FIELDS), "data": [[1, "S1"]]},
            "empty fields": {"fields": [], "data": []},
        }
        for label, payload in payloads.items():
            with self.subTest(label):
                urls = []
                with mock.patch("airloom.purpleair.urlopen",
                                side_effect=fake_urlopen([payload], urls)):
                    with self.assertRaises(PurpleAirError):
                        cache.fetch_area(client, self.cache, self.bounds, "outdoor", ttl=120)
                self.assertNotIn("modified_since", urls[0])
                self.assertEqual(
                    sorted(r["sensor_index"] for r in self.cache.sensors_in(self.bounds)), [1, 2])
                self.assertEqual(self._region(), (1, 100000.0))

    def test_partial_upsert_never_dates_retained_measurement(self):
        self.clock.now = 100000.0
        self.cache.store_fetch(self.bounds, "outdoor", [
            row(1, last_seen=100000, **{"pm2.5_cf_1": 100.0})], 100000)
        self.clock.now = 100500.0
        self.cache.upsert_rows([{"sensor_index": 1, "last_seen": 100500}])
        stored = self.cache.sensors_in(self.bounds)[0]
        self.assertEqual(stored["last_seen"], 100500)
        self.assertIsNone(stored["pm2.5_cf_1"])
        self.assertIsNone(purpleair.sensor_from_values(stored, now=100500).aqi)

    def test_row_without_last_seen_merges_onto_existing_measurements(self):
        self.clock.now = 100000.0
        self.cache.store_fetch(self.bounds, "outdoor", [
            row(1, last_seen=100000, **{"pm2.5_cf_1": 100.0})], 100000)
        self.cache.upsert_rows([{"sensor_index": 1, "humidity": 60}])
        stored = self.cache.sensors_in(self.bounds)[0]
        self.assertEqual(stored["last_seen"], 100000)
        self.assertEqual(stored["pm2.5_cf_1"], 100.0)
        self.assertEqual(stored["humidity"], 60)


class FetchFavoritesTest(CacheBase):
    def test_only_stale_missing_favorites_are_fetched(self):
        self.cache.store_fetch(bounds_around(45.5, -122.6, 20.0), "outdoor", [row(5)], CHECKPOINT)
        client = FakeClient([FetchResult([row(6, lat=39.1)], CHECKPOINT + 60)])
        rows = cache.fetch_favorites(client, self.cache, [5, 6], have_ids={1}, ttl=120)
        self.assertEqual(sorted(r["sensor_index"] for r in rows), [5, 6])
        self.assertEqual(client.calls[0]["show_only"], [6])

    def test_stale_observation_cannot_be_served_fully_from_favorite_cache(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(5, name="Original")], CHECKPOINT)
        self.clock.now += 25 * 3600
        client = FakeClient([FetchResult([
            row(5, name="Fresh", last_seen=self.clock.now)
        ], CHECKPOINT + 300)])
        rows = cache.fetch_favorites(
            client, self.cache, [5], have_ids=set(), ttl=48 * 3600)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["show_only"], [5])
        self.assertEqual(rows[0]["name"], "Fresh")
        self.assertIsNone(self.cache.covering_region(bounds, "outdoor"))

    def test_no_call_when_everything_is_covered(self):
        client = FakeClient([])
        self.assertEqual(cache.fetch_favorites(client, self.cache, [5], have_ids={5}, ttl=120), [])
        self.assertEqual(client.calls, [])

    def test_hostile_favorite_ids_are_ignored_and_string_ids_are_normalized(self):
        client = FakeClient([FetchResult([row(2)], CHECKPOINT + 60)])
        rows = cache.fetch_favorites(
            client,
            self.cache,
            [float("nan"), float("inf"), 10**400, 1.5, True, False, "2"],
            have_ids=set(),
            ttl=120,
        )
        self.assertEqual(client.calls[0]["show_only"], [2])
        self.assertEqual([item["sensor_index"] for item in rows], [2])
        self.assertEqual(list(self.cache.fresh_sensors(["2"], max_age=120)), [2])

    def test_invalid_favorite_backfill_does_not_replace_valid_cached_metadata(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(5, name="Valid")], CHECKPOINT)
        original = self.cache.sensors_in(bounds)
        self.clock.now += 300
        client = FakeClient([FetchResult([row(5, name="Invalid", latitude=91)], CHECKPOINT + 300)])
        cache.fetch_favorites(client, self.cache, [5], have_ids=set(), ttl=120, force=True)
        self.assertEqual(self.cache.sensors_in(bounds), original)


class OfflineFavoriteCoverageTest(CacheBase):
    def test_offline_favorite_keeps_area_delta_polling_across_cycles(self):
        self.clock.now = 200000.0
        bounds = bounds_around(45.5, -122.6, 20.0)
        offline = row(99, lat=40.0, lon=-120.0, last_seen=200000 - 2 * 86400)
        client = FakeClient([
            FetchResult([row(1, last_seen=200000)], 200000),
            FetchResult([offline], 200000),
            FetchResult([], 200300),
            FetchResult([offline], 200300),
            FetchResult([], 200600),
            FetchResult([offline], 200600),
        ])
        for _ in range(3):
            area = cache.fetch_area(client, self.cache, bounds, "outdoor", ttl=120)
            fav = cache.fetch_favorites(
                client, self.cache, [99], {r["sensor_index"] for r in area.rows}, ttl=120)
            self.assertEqual([r["sensor_index"] for r in fav], [99])
            self.assertEqual(fav[0]["last_seen"], 200000 - 2 * 86400)
            self.assertIsNotNone(self.cache.covering_region(bounds, "outdoor"))
            self.clock.now += 300

        self.assertEqual(client.calls[0]["fields"], MAP_FIELDS)
        self.assertNotIn("modified_since", client.calls[0])
        self.assertEqual([c.get("show_only") for c in client.calls[1::2]], [[99], [99], [99]])
        self.assertEqual(client.calls[2]["fields"], DATA_FIELDS)
        self.assertEqual(client.calls[2]["modified_since"], 200000)
        self.assertEqual(client.calls[4]["fields"], DATA_FIELDS)
        self.assertEqual(client.calls[4]["modified_since"], 200300)
        self.assertEqual(self.cache.fresh_sensors([99], 3600), {})

    def test_expired_incoming_row_is_not_reported_unknown(self):
        self.clock.now = 200000.0
        self.assertEqual(self.cache.upsert_rows([row(98, lat=None, last_seen=1)]), [])

    def test_expired_coordinate_less_delta_rows_request_no_metadata(self):
        self.clock.now = 200000.0
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1, last_seen=200000)], 200000)
        expired = {"sensor_index": 50, "last_seen": 200000 - 2 * 86400, "pm2.5_cf_1": 9.0}
        client = FakeClient([
            FetchResult([expired], 200300),
            FetchResult([expired], 200600),
            FetchResult([expired, {"sensor_index": 51, "last_seen": 200900,
                                   "pm2.5_cf_1": 7.0}], 200900),
            FetchResult([row(51, lat=45.52, last_seen=200900)], 200900),
        ])
        for _ in range(3):
            self.clock.now += 300
            area = cache.fetch_area(client, self.cache, bounds, "outdoor", ttl=120)
            self.assertTrue(area.polled)
            self.assertIsNotNone(self.cache.covering_region(bounds, "outdoor"))

        self.assertEqual(
            [c["modified_since"] for c in client.calls if "modified_since" in c],
            [200000, 200300, 200600])
        self.assertEqual(
            [c["show_only"] for c in client.calls if "show_only" in c], [[51]])
        self.assertEqual(sorted(r["sensor_index"] for r in area.rows), [1, 51])
