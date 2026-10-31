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

from .purpleair import (
    Bounds, bounds_contains, split_bounds, DATA_FIELDS, MAP_FIELDS, MEASUREMENT_FIELDS,
    FetchResult, PurpleAirError, _integer, _number,
)
from .models import FUTURE_SKEW_TOLERANCE, plausible_observation_time
from .validation import sensor_id

MAX_REGIONS = 50
SENSOR_MAX_AGE = 24 * 3600.0
METADATA_MAX_AGE = 24 * 3600

# Bump when cached rows become unusable (e.g. a newly-requested field that
# delta polls would never backfill for unchanged sensors); a mismatched
# cache is cleared on connect and simply refetched.
SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sensors (
    sensor_index INTEGER PRIMARY KEY,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    data TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    observed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS regions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    north REAL NOT NULL, west REAL NOT NULL, south REAL NOT NULL, east REAL NOT NULL,
    location_filter TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    api_time_stamp INTEGER,
    metadata_fetched_at REAL NOT NULL
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
    metadata_fetched_at: float


def _default_cache_dir() -> Path:
    # Same XDG rule as store.py: an empty or relative override is ignored.
    xdg = os.environ.get("XDG_CACHE_HOME", "")
    base = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".cache"
    return base / "airloom"


class CacheError(RuntimeError):
    """The cache could not be opened without destroying valid data."""


class CacheWriteError(CacheError):
    """The cache failed after a full fetch (persisting or rereading its rows);
    the fetched rows remain valid live data."""

    def __init__(self, message: str, rows: list[dict]):
        super().__init__(message)
        self.rows = rows


def _coordinates(values: dict) -> tuple[float, float] | None:
    latitude = _number(values.get("latitude"))
    longitude = _number(values.get("longitude"))
    if (
        latitude is None
        or longitude is None
        or not -90.0 <= latitude <= 90.0
        or not -180.0 <= longitude <= 180.0
    ):
        return None
    return latitude, longitude


def _observed_at(merged: dict, stored_observed_at: float | None, now: float) -> float:
    """When a merged row was observed; its last_seen must already be validated."""
    last_seen = _integer(merged.get("last_seen"))
    if last_seen is not None:
        return float(last_seen)
    return float(stored_observed_at) if stored_observed_at is not None else now


def _already_expired(merged: dict, stored_observed_at: float | None, now: float) -> bool:
    return _observed_at(merged, stored_observed_at, now) < now - SENSOR_MAX_AGE


class SensorCache:
    def __init__(self, path: Path | None = None, clock=time.time,
                 busy_timeout: float = 5.0):
        self.path = path or _default_cache_dir() / "cache.db"
        self.clock = clock
        self.busy_timeout = busy_timeout
        self._lock = threading.Lock()
        self._db = None
        self._connect()

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def __enter__(self) -> SensorCache:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _connect(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._open_and_migrate()
        except sqlite3.Error as exc:
            corruption_codes = {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}
            if getattr(exc, "sqlite_errorcode", None) not in corruption_codes:
                raise CacheError(f"Could not open cache at {self.path}") from exc
            # Only confirmed corruption is disposable; locks, permissions,
            # and I/O errors must leave the existing file untouched.
            try:
                self.path.unlink(missing_ok=True)
                self._open_and_migrate()
            except (OSError, sqlite3.Error) as recovery_exc:
                raise CacheError(f"Could not recreate cache at {self.path}") from recovery_exc
        except OSError as exc:
            raise CacheError(f"Could not open cache at {self.path}") from exc

    def _open_and_migrate(self) -> None:
        db = sqlite3.connect(
            self.path, timeout=self.busy_timeout, check_same_thread=False)
        self._db = db
        try:
            db.executescript(_SCHEMA)
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                # The schema itself changes with v2, so recreate rather than
                # merely deleting rows from tables that lack the new columns.
                db.executescript(
                    "DROP TABLE IF EXISTS sensors;"
                    "DROP TABLE IF EXISTS regions;"
                    "DROP TABLE IF EXISTS trends;"
                    + _SCHEMA
                )
                # PRAGMA does not take bound parameters; SCHEMA_VERSION is a
                # module constant int, never user input.
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            db.commit()
        except BaseException:
            self._db = None
            db.close()
            raise

    def prune(self) -> None:
        """Drop stale observations and invalidate coverage before cache reads."""
        with self._lock:
            try:
                self._prune_locked(self.clock())
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def upsert_rows(self, rows: list[dict]) -> list[int]:
        with self._lock:
            try:
                unknown = self._upsert_locked(rows)
                self._prune_locked(self.clock())
                self._db.commit()
                return unknown
            except BaseException:
                self._db.rollback()
                raise

    def unknown_rows(self, rows: list[dict]) -> list[int]:
        """Return delta IDs that need full metadata, without staging writes.
        Rows the cache would discard as already expired never need metadata."""
        with self._lock:
            now = self.clock()
            order: list[int] = []
            merged_by_id: dict[int, dict] = {}
            stored_observed_at: dict[int, float | None] = {}
            for values in rows:
                normalized_id = sensor_id(values.get("sensor_index"))
                if normalized_id is None:
                    continue
                if normalized_id not in merged_by_id:
                    order.append(normalized_id)
                    cursor = self._db.execute(
                        "SELECT data, observed_at FROM sensors WHERE sensor_index = ?",
                        (normalized_id,))
                    existing = cursor.fetchone()
                    merged_by_id[normalized_id] = dict(json.loads(existing[0])) if existing else {}
                    stored_observed_at[normalized_id] = existing[1] if existing else None
                merged = merged_by_id[normalized_id]
                merged.update(values)
                if "last_seen" in values:
                    merged["last_seen"] = plausible_observation_time(
                        _integer(values["last_seen"]), now)
                merged["sensor_index"] = normalized_id
            return [
                normalized_id for normalized_id in order
                if not _already_expired(
                    merged_by_id[normalized_id], stored_observed_at[normalized_id], now)
                and _coordinates(merged_by_id[normalized_id]) is None
            ]

    def _upsert_locked(self, rows: list[dict]) -> list[int]:
        now = self.clock()
        unknown: list[int] = []
        for values in rows:
            normalized_id = sensor_id(values.get("sensor_index"))
            if normalized_id is None:
                continue
            cursor = self._db.execute(
                "SELECT data, observed_at FROM sensors WHERE sensor_index = ?", (normalized_id,))
            existing = cursor.fetchone()
            previous = json.loads(existing[0]) if existing else {}
            merged = dict(previous)
            merged.update(values)
            if "last_seen" in values:
                observed = plausible_observation_time(_integer(values["last_seen"]), now)
                merged["last_seen"] = observed
                # A new observation time must never date a retained old reading.
                if observed is not None and observed != _integer(previous.get("last_seen")):
                    for key in MEASUREMENT_FIELDS - {"last_seen"}:
                        if key in previous and key not in values:
                            merged[key] = None
            merged["sensor_index"] = normalized_id
            stored_observed_at = existing[1] if existing else None
            if _already_expired(merged, stored_observed_at, now):
                # Storing an already-expired row would only make the next prune
                # wipe every region's coverage; the caller still shows the row.
                continue
            observed_at = _observed_at(merged, stored_observed_at, now)
            coordinates = _coordinates(merged)
            if coordinates is None:
                unknown.append(normalized_id)
                continue
            lat, lon = coordinates
            merged["latitude"], merged["longitude"] = lat, lon
            self._db.execute(
                "INSERT INTO sensors (sensor_index, latitude, longitude, data, fetched_at,"
                " observed_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(sensor_index) DO UPDATE SET"
                " latitude = excluded.latitude, longitude = excluded.longitude,"
                " data = excluded.data, fetched_at = excluded.fetched_at,"
                " observed_at = excluded.observed_at",
                (normalized_id, lat, lon, json.dumps(merged), now, observed_at),
            )
        return unknown

    def _reconcile_locked(self, bounds: Bounds, location_filter: str,
                          rows: list[dict]) -> None:
        """Apply an authoritative snapshot's absence only to its own query scope."""
        present = {
            normalized for values in rows
            if (normalized := sensor_id(values.get("sensor_index"))) is not None
        }
        candidates: dict[int, dict] = {}
        for part in split_bounds(bounds):
            cursor = self._db.execute(
                "SELECT sensor_index, data FROM sensors WHERE latitude BETWEEN ? AND ?"
                " AND longitude BETWEEN ? AND ?",
                (part.south, part.north, part.west, part.east),
            )
            for sensor_index, data in cursor:
                values = json.loads(data)
                if sensor_index not in present and _matches_filter(values, location_filter):
                    candidates[sensor_index] = values
        self._db.executemany(
            "DELETE FROM sensors WHERE sensor_index = ?",
            ((sensor_index,) for sensor_index in candidates),
        )

    def store_fetch(self, bounds: Bounds, location_filter: str,
                    rows: list[dict], api_time_stamp: int | None) -> None:
        with self._lock:
            try:
                self._upsert_locked(rows)
                self._reconcile_locked(bounds, location_filter, rows)
                now = self.clock()
                cursor = self._db.execute(
                    "INSERT INTO regions (north, west, south, east, location_filter,"
                    " fetched_at, api_time_stamp, metadata_fetched_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (bounds.north, bounds.west, bounds.south, bounds.east,
                     location_filter, now, api_time_stamp, now),
                )
                self._prune_locked(now, preserve_region=cursor.lastrowid)
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def apply_delta(self, region_id: int, rows: list[dict],
                    api_time_stamp: int | None) -> list[int]:
        with self._lock:
            try:
                unknown = self._upsert_locked(rows)
                now = self.clock()
                self._db.execute(
                    "UPDATE regions SET fetched_at = ?, api_time_stamp ="
                    " COALESCE(?, api_time_stamp) WHERE id = ?",
                    (now, api_time_stamp, region_id),
                )
                self._prune_locked(now)
                self._db.commit()
                return unknown
            except BaseException:
                self._db.rollback()
                raise

    def covering_region(self, bounds: Bounds, location_filter: str,
                        max_age: float | None = None) -> Region | None:
        with self._lock:
            cursor = self._db.execute(
                "SELECT id, north, west, south, east, location_filter, fetched_at,"
                " api_time_stamp, metadata_fetched_at FROM regions WHERE location_filter = ?"
                " ORDER BY fetched_at DESC",
                (location_filter,),
            )
            now = self.clock()
            for (rid, north, west, south, east, mode, fetched_at, stamp,
                 metadata_fetched_at) in cursor:
                if max_age is not None and now - fetched_at >= max_age:
                    break  # ordered newest-first: everything after is older
                region = Region(
                    rid, Bounds(north, west, south, east), mode, fetched_at, stamp,
                    metadata_fetched_at,
                )
                if bounds_contains(region.bounds, bounds):
                    return region
            return None

    def sensors_in(self, bounds: Bounds) -> list[dict]:
        with self._lock:
            rows_by_id: dict[int, dict] = {}
            for part in split_bounds(bounds):
                cursor = self._db.execute(
                    "SELECT sensor_index, data FROM sensors WHERE latitude BETWEEN ? AND ?"
                    " AND longitude BETWEEN ? AND ?",
                    (part.south, part.north, part.west, part.east),
                )
                for sensor_id, data in cursor:
                    rows_by_id[sensor_id] = json.loads(data)
            return list(rows_by_id.values())

    def fresh_sensors(self, ids, max_age: float) -> dict[int, dict]:
        wanted = [normalized for value in ids if (normalized := sensor_id(value)) is not None]
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

    def _prune_locked(self, now: float, preserve_region: int | None = None) -> None:
        cutoff = now - SENSOR_MAX_AGE
        cursor = self._db.execute(
            "DELETE FROM sensors WHERE observed_at < ? OR observed_at > ?",
            (cutoff, now + FUTURE_SKEW_TOLERANCE),
        )
        if cursor.rowcount:
            # A delta checkpoint cannot prove that a pruned row was absent from
            # the next response. Keep only a just-completed full snapshot, if any.
            if preserve_region is None:
                self._db.execute("DELETE FROM regions")
            else:
                self._db.execute("DELETE FROM regions WHERE id != ?", (preserve_region,))
        self._db.execute("DELETE FROM trends WHERE fetched_at < ?", (cutoff,))
        self._db.execute(
            "DELETE FROM regions WHERE id NOT IN"
            " (SELECT id FROM regions ORDER BY fetched_at DESC LIMIT ?)",
            (MAX_REGIONS,),
        )

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


def _fetch_rows(client, bounds: Bounds, **kwargs) -> FetchResult:
    """Fetch canonical query rectangles and merge duplicate sensor IDs."""
    rows_by_id: dict[int, dict] = {}
    unkeyed: list[dict] = []
    stamps: list[int | None] = []
    for part in split_bounds(bounds):
        result = client.fetch_rows(bounds=part, **kwargs)
        stamps.append(result.time_stamp)
        for values in result.rows:
            normalized_id = sensor_id(values.get("sensor_index"))
            if normalized_id is None:
                unkeyed.append(values)
            else:
                rows_by_id[normalized_id] = values
    time_stamp = min(stamps) if stamps and all(stamp is not None for stamp in stamps) else None
    return FetchResult([*rows_by_id.values(), *unkeyed], time_stamp)


def _rows_within(rows: list[dict], bounds: Bounds, location_filter: str) -> list[dict]:
    parts = split_bounds(bounds)
    return [
        values for values in rows
        if (coordinates := _coordinates(values)) is not None
        and _matches_filter(values, location_filter)
        and any(
            part.south <= coordinates[0] <= part.north
            and part.west <= coordinates[1] <= part.east
            for part in parts
        )
    ]


def fetch_area(client, cache: SensorCache, bounds: Bounds, location_filter: str,
               ttl: float, force: bool = False) -> AreaResult:
    """Serve an area from cache, delta-poll a stale known region, or full-fetch
    a new one. May raise PurpleAirError — the caller owns fallback policy.
    SQLite failures propagate, except that any failure after a full fetch
    (storing or rereading) raises CacheWriteError carrying the fetched rows
    within `bounds`, so obtained live data never needs a second paid request.
    A delta response alone is not a complete view, so delta-path failures
    propagate and the caller refetches without the cache."""
    cache.prune()
    region = cache.covering_region(bounds, location_filter)
    now = cache.clock()

    def rows_in_view() -> list[dict]:
        return [v for v in cache.sensors_in(bounds) if _matches_filter(v, location_filter)]

    if (
        region is not None
        and not force
        and now - region.fetched_at < ttl
        and now - region.metadata_fetched_at < METADATA_MAX_AGE
    ):
        return AreaResult(rows_in_view(), now - region.fetched_at, False)
    if (
        region is not None
        and region.api_time_stamp is not None
        and now - region.metadata_fetched_at < METADATA_MAX_AGE
    ):
        result = _fetch_rows(
            client,
            region.bounds,
            location_filter=location_filter,
            fields=DATA_FIELDS,
            modified_since=region.api_time_stamp,
        )
        unknown = cache.unknown_rows(result.rows)
        followup_rows: list[dict] = []
        if unknown:
            followup = client.fetch_rows(show_only=sorted(unknown))
            followup_rows = followup.rows
            unresolved = set(cache.unknown_rows(result.rows + followup_rows))
            if any(sensor_id in unresolved for sensor_id in unknown):
                raise PurpleAirError("PurpleAir backfill omitted required sensor metadata.")
        cache.apply_delta(region.id, result.rows + followup_rows, result.time_stamp)
        return AreaResult(rows_in_view(), 0.0, True)
    full_bounds = region.bounds if region is not None else bounds
    result = _fetch_rows(
        client, full_bounds, location_filter=location_filter, fields=MAP_FIELDS)
    try:
        cache.store_fetch(full_bounds, location_filter, result.rows, result.time_stamp)
        rows = rows_in_view()
    except sqlite3.Error as exc:
        raise CacheWriteError(
            f"Could not cache readings: {exc}",
            _rows_within(result.rows, bounds, location_filter),
        ) from exc
    return AreaResult(rows, 0.0, True)


def fetch_favorites(client, cache: SensorCache, favorite_ids, have_ids,
                    ttl: float, force: bool = False) -> list[dict]:
    """Rows for favorites not already in `have_ids`: cached-fresh ones for free,
    one show_only batch for the rest."""
    cache.prune()
    normalized_have = {
        normalized for value in have_ids if (normalized := sensor_id(value)) is not None
    }
    missing = [
        normalized for value in favorite_ids
        if (normalized := sensor_id(value)) is not None and normalized not in normalized_have
    ]
    if not missing:
        return []
    cached = {} if force else cache.fresh_sensors(missing, ttl)
    rows = list(cached.values())
    need = sorted(set(missing) - set(cached))
    if need:
        result = client.fetch_rows(show_only=need)
        rows += result.rows
        try:
            cache.upsert_rows(result.rows)
        except sqlite3.Error as exc:
            raise CacheWriteError(f"Could not cache readings: {exc}", rows) from exc
    return rows
