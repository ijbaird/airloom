"""Persistent sensor cache: SQLite-backed storage of raw PurpleAir field
values, fetched-region records, and per-sensor trends.

GTK-free by design (unit-tested without a display). Thread-safe: app.py
reads on the main thread and writes from the refresh worker, so every
operation takes the internal lock and the connection is created with
check_same_thread=False.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .purpleair import Bounds, bounds_contains, DATA_FIELDS, MAP_FIELDS, _integer

MAX_REGIONS = 50
SENSOR_MAX_AGE = 24 * 3600.0

# Bump when cached rows become unusable (e.g. a newly-requested field that
# delta polls would never backfill for unchanged sensors); a mismatched
# cache is cleared on connect and simply refetched.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sensors (
    sensor_index INTEGER PRIMARY KEY,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    data TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS regions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    north REAL NOT NULL, west REAL NOT NULL, south REAL NOT NULL, east REAL NOT NULL,
    location_filter TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    api_time_stamp INTEGER
);
CREATE TABLE IF NOT EXISTS trends (
    sensor_index INTEGER PRIMARY KEY,
    data TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class Region:
    id: int
    bounds: Bounds
    location_filter: str
    fetched_at: float
    api_time_stamp: int | None


def _default_cache_dir() -> Path:
    # Same XDG rule as store.py: an empty or relative override is ignored.
    xdg = os.environ.get("XDG_CACHE_HOME", "")
    base = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".cache"
    return base / "airloom"


class SensorCache:
    def __init__(self, path: Path | None = None, clock=time.time):
        self.path = path or _default_cache_dir() / "cache.db"
        self.clock = clock
        self._lock = threading.Lock()
        self._connect()

    def _connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._open_and_migrate()
        except sqlite3.Error:
            # It's only a cache: a corrupt file is deleted, never repaired.
            self.path.unlink(missing_ok=True)
            self._open_and_migrate()

    def _open_and_migrate(self) -> None:
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            self._db.execute("DELETE FROM sensors")
            self._db.execute("DELETE FROM regions")
            self._db.execute("DELETE FROM trends")
            # PRAGMA does not take bound parameters; SCHEMA_VERSION is a
            # module constant int, never user input.
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._db.commit()

    def upsert_rows(self, rows: list[dict]) -> list[int]:
        with self._lock:
            return self._upsert_locked(rows)

    def _upsert_locked(self, rows: list[dict]) -> list[int]:
        now = self.clock()
        unknown: list[int] = []
        for values in rows:
            sensor_id = values.get("sensor_index")
            if not isinstance(sensor_id, (int, float)):
                continue
            sensor_id = int(sensor_id)
            cursor = self._db.execute(
                "SELECT data FROM sensors WHERE sensor_index = ?", (sensor_id,))
            existing = cursor.fetchone()
            merged = dict(json.loads(existing[0])) if existing else {}
            merged.update(values)
            lat, lon = merged.get("latitude"), merged.get("longitude")
            if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
                unknown.append(sensor_id)
                continue
            self._db.execute(
                "INSERT INTO sensors (sensor_index, latitude, longitude, data, fetched_at)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT(sensor_index) DO UPDATE SET"
                " latitude = excluded.latitude, longitude = excluded.longitude,"
                " data = excluded.data, fetched_at = excluded.fetched_at",
                (sensor_id, float(lat), float(lon), json.dumps(merged), now),
            )
        self._db.commit()
        return unknown

    def store_fetch(self, bounds: Bounds, location_filter: str,
                    rows: list[dict], api_time_stamp: int | None) -> None:
        with self._lock:
            self._upsert_locked(rows)
            now = self.clock()
            self._db.execute(
                "INSERT INTO regions (north, west, south, east, location_filter,"
                " fetched_at, api_time_stamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (bounds.north, bounds.west, bounds.south, bounds.east,
                 location_filter, now, api_time_stamp),
            )
            self._prune_locked(now)
            self._db.commit()

    def apply_delta(self, region_id: int, rows: list[dict],
                    api_time_stamp: int | None) -> list[int]:
        with self._lock:
            unknown = self._upsert_locked(rows)
            self._db.execute(
                "UPDATE regions SET fetched_at = ?, api_time_stamp ="
                " COALESCE(?, api_time_stamp) WHERE id = ?",
                (self.clock(), api_time_stamp, region_id),
            )
            self._db.commit()
            return unknown

    def covering_region(self, bounds: Bounds, location_filter: str,
                        max_age: float | None = None) -> Region | None:
        with self._lock:
            cursor = self._db.execute(
                "SELECT id, north, west, south, east, location_filter, fetched_at,"
                " api_time_stamp FROM regions WHERE location_filter = ?"
                " ORDER BY fetched_at DESC",
                (location_filter,),
            )
            now = self.clock()
            for rid, north, west, south, east, mode, fetched_at, stamp in cursor:
                if max_age is not None and now - fetched_at >= max_age:
                    break  # ordered newest-first: everything after is older
                region = Region(rid, Bounds(north, west, south, east), mode, fetched_at, stamp)
                if bounds_contains(region.bounds, bounds):
                    return region
            return None

    def sensors_in(self, bounds: Bounds) -> list[dict]:
        with self._lock:
            cursor = self._db.execute(
                "SELECT data FROM sensors WHERE latitude BETWEEN ? AND ?"
                " AND longitude BETWEEN ? AND ?",
                (bounds.south, bounds.north, bounds.west, bounds.east),
            )
            return [json.loads(row[0]) for row in cursor]

    def fresh_sensors(self, ids, max_age: float) -> dict[int, dict]:
        wanted = [int(i) for i in ids]
        if not wanted:
            return {}
        with self._lock:
            marks = ",".join("?" for _ in wanted)
            cursor = self._db.execute(
                f"SELECT sensor_index, data FROM sensors WHERE sensor_index IN ({marks})"
                " AND fetched_at > ?",
                (*wanted, self.clock() - max_age),
            )
            return {row[0]: json.loads(row[1]) for row in cursor}

    def _prune_locked(self, now: float) -> None:
        self._db.execute(
            "DELETE FROM regions WHERE id NOT IN"
            " (SELECT id FROM regions ORDER BY fetched_at DESC LIMIT ?)",
            (MAX_REGIONS,),
        )
        cutoff = now - SENSOR_MAX_AGE
        self._db.execute("DELETE FROM sensors WHERE fetched_at < ?", (cutoff,))
        self._db.execute("DELETE FROM trends WHERE fetched_at < ?", (cutoff,))

    def store_trend(self, sensor_id: int, trend: list) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO trends (sensor_index, data, fetched_at) VALUES (?, ?, ?)"
                " ON CONFLICT(sensor_index) DO UPDATE SET data = excluded.data,"
                " fetched_at = excluded.fetched_at",
                (int(sensor_id), json.dumps(trend), self.clock()),
            )
            self._db.commit()

    def get_trend(self, sensor_id: int, max_age: float) -> list | None:
        with self._lock:
            cursor = self._db.execute(
                "SELECT data FROM trends WHERE sensor_index = ? AND fetched_at > ?",
                (int(sensor_id), self.clock() - max_age),
            )
            found = cursor.fetchone()
            return json.loads(found[0]) if found else None

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM sensors")
            self._db.execute("DELETE FROM regions")
            self._db.execute("DELETE FROM trends")
            self._db.commit()


@dataclass(frozen=True, slots=True)
class AreaResult:
    rows: list[dict]
    age: float
    polled: bool


def _matches_filter(values: dict, location_filter: str) -> bool:
    if location_filter == "both":
        return True
    want = 1 if location_filter == "indoor" else 0
    # A missing location_type (e.g. a delta payload that only carries changed
    # fields) defaults to outdoor, matching PurpleAir's own field default.
    return (_integer(values.get("location_type")) or 0) == want


def fetch_area(client, cache: SensorCache, bounds: Bounds, location_filter: str,
               ttl: float, force: bool = False) -> AreaResult:
    """Serve an area from cache, delta-poll a stale known region, or full-fetch
    a new one. May raise PurpleAirError — the caller owns fallback policy."""
    region = cache.covering_region(bounds, location_filter)
    now = cache.clock()

    def rows_in_view() -> list[dict]:
        return [v for v in cache.sensors_in(bounds) if _matches_filter(v, location_filter)]

    if region is not None and not force and now - region.fetched_at < ttl:
        return AreaResult(rows_in_view(), now - region.fetched_at, False)
    if region is not None and region.api_time_stamp is not None:
        result = client.fetch_rows(
            bounds=region.bounds,
            location_filter=location_filter,
            fields=DATA_FIELDS,
            modified_since=region.api_time_stamp,
        )
        unknown = cache.apply_delta(region.id, result.rows, result.time_stamp)
        if unknown:
            followup = client.fetch_rows(show_only=sorted(unknown))
            cache.upsert_rows(followup.rows)
        return AreaResult(rows_in_view(), 0.0, True)
    result = client.fetch_rows(bounds=bounds, location_filter=location_filter, fields=MAP_FIELDS)
    cache.store_fetch(bounds, location_filter, result.rows, result.time_stamp)
    return AreaResult(rows_in_view(), 0.0, True)


def fetch_favorites(client, cache: SensorCache, favorite_ids, have_ids,
                    ttl: float, force: bool = False) -> list[dict]:
    """Rows for favorites not already in `have_ids`: cached-fresh ones for free,
    one show_only batch for the rest."""
    missing = [int(i) for i in favorite_ids if int(i) not in have_ids]
    if not missing:
        return []
    cached = {} if force else cache.fresh_sensors(missing, ttl)
    rows = list(cached.values())
    need = sorted(set(missing) - set(cached))
    if need:
        result = client.fetch_rows(show_only=need)
        cache.upsert_rows(result.rows)
        rows += result.rows
    return rows
