# PurpleAir API Caching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut PurpleAir API point burn ~50–100× via a persistent SQLite sensor cache with TTL, `modified_since` delta polling, trimmed field lists, lazy trend fetches, and a zoom cap — per the approved spec `docs/superpowers/specs/2026-08-08-purpleair-api-caching-design.md`.

**Architecture:** New GTK-free `airloom/cache.py` (SQLite at `~/.cache/airloom/cache.db`) stores raw per-sensor API field values, fetched-region records, and per-sensor trends. Pure coordinator functions `fetch_area`/`fetch_favorites` in `cache.py` implement the fresh-hit / delta-poll / miss decision flow and are unit-tested with a fake client. `app.py` wires them into its existing worker thread; `purpleair.py` grows field-list constants, `modified_since` support, and a raw-rows fetch path.

**Tech Stack:** Python stdlib only (`sqlite3`, `unittest`), vanilla JS/HTML. No new dependencies of any kind.

## Global Constraints

- **Zero third-party dependencies** (CLAUDE.md): stdlib + system PyGObject only; no pip packages, no JS libraries.
- Tests are pure-stdlib `unittest`, must run **without GTK/display** (`make test` = `python3 -m unittest discover -s tests -v`).
- Never touch GTK/WebKit from a worker thread; results re-enter via `GLib.idle_add`.
- The API key never leaves `public_config()` except as a masked hint; the cache DB must not contain the key.
- Config default `refresh_minutes` = **2**; allowed values {2, 5, 10, 30}.
- Zoom cap: views wider/taller than **200 km** fetch a 200 km box around the view center.
- Sensor/trend cache rows expire from the DB after **24 h** unrefreshed; keep at most the **50** newest regions.
- Run `make check` before declaring the work done.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: `refresh_minutes` config key in `store.py`

**Files:**
- Modify: `airloom/store.py` (DEFAULT_CONFIG ~line 10, `_sanitize` ~line 35, `public_config` ~line 108)
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `store.data["refresh_minutes"]: int` (2/5/10/30, default 2), also present in `public_config()`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_store.py` (match the file's existing test-class style):

```python
class RefreshMinutesTest(unittest.TestCase):
    def test_defaults_to_two(self):
        self.assertEqual(store._sanitize({})["refresh_minutes"], 2)

    def test_accepts_allowed_values(self):
        for minutes in (2, 5, 10, 30):
            self.assertEqual(store._sanitize({"refresh_minutes": minutes})["refresh_minutes"], minutes)

    def test_rejects_everything_else(self):
        for bad in (7, 0, -5, 2.5, "10", True, None, [10]):
            self.assertEqual(store._sanitize({"refresh_minutes": bad})["refresh_minutes"], 2)

    def test_appears_in_public_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = store.Store(Path(tmp) / "config.json")
            self.assertEqual(s.public_config()["refresh_minutes"], 2)
```

(`tests/test_store.py` already imports `store`, `tempfile`, and `Path` — reuse its imports; only add what's missing.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_store.RefreshMinutesTest -v`
Expected: FAIL — `KeyError: 'refresh_minutes'`.

- [ ] **Step 3: Implement**

In `airloom/store.py`:

```python
# In DEFAULT_CONFIG, after "alert_threshold": 101,
    "refresh_minutes": 2,
```

In `_sanitize`, after the `location_filter` block:

```python
    if data.get("refresh_minutes") in (2, 5, 10, 30) and not isinstance(data.get("refresh_minutes"), bool):
        clean["refresh_minutes"] = int(data["refresh_minutes"])
```

Note: `in (2, 5, 10, 30)` also matches floats `2.0` etc. — that's why the test list includes `2.5` (rejected) but plain floats like `10.0` would be accepted and int-coerced, which is fine. `True == 1` is excluded by the bool guard (1 isn't allowed anyway, but keep the guard for clarity with the codebase's other bool guards).

In `public_config()`, after `"alert_threshold": ...,`:

```python
            "refresh_minutes": self.data["refresh_minutes"],
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_store -v`
Expected: all PASS (existing tests too).

- [ ] **Step 5: Commit**

```bash
git add airloom/store.py tests/test_store.py
git commit -m "store: add refresh_minutes setting (2/5/10/30, default 2)"
```

---

### Task 2: Field lists, raw-row fetches, and `modified_since` in `purpleair.py`

**Files:**
- Modify: `airloom/purpleair.py`
- Test: `tests/test_purpleair.py`

**Interfaces:**
- Produces (all in `airloom/purpleair.py`, consumed by Tasks 4–8):
  - `MAP_FIELDS: tuple[str, ...]` — 11 fields for full area fetches
  - `DATA_FIELDS: tuple[str, ...]` — 7 changing fields for delta polls
  - `TREND_FETCH_FIELDS: tuple[str, ...]` — 9 fields for lazy trend fetches
  - `@dataclass FetchResult(rows: list[dict], time_stamp: int | None)` — raw per-sensor field-value dicts + server clock
  - `PurpleAirClient.fetch_rows(bounds=None, show_only=None, location_filter="outdoor", fields=MAP_FIELDS, modified_since=None) -> FetchResult`
  - `sensor_from_values(values: dict) -> Sensor | None` — None when id/lat/lon missing
  - `trend_from_values(values: dict) -> list[dict]` — `[{"label": ..., "aqi": ...}, ...]` for the 7 TREND_FIELDS points
  - `fetch_sensors(...)` and `parse_sensor_payload(...)` keep their existing signatures (now thin wrappers)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_purpleair.py`:

```python
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

    def test_trend_fetch_fields_carry_humidity_for_epa_correction(self):
        self.assertIn("humidity", purpleair.TREND_FETCH_FIELDS)
        self.assertIn("pm2.5_1week", purpleair.TREND_FETCH_FIELDS)


class FetchRowsTest(unittest.TestCase):
    def _client_with_capture(self, payload):
        captured = {}

        def fake_urlopen(request, timeout=0):
            captured["url"] = request.full_url
            return _FakeResponse(payload)  # reuse/extend the module's existing urlopen-stub helper

        return captured, fake_urlopen

    def test_modified_since_and_fields_in_query(self):
        payload = {"fields": ["sensor_index", "pm2.5_cf_1"], "data": [], "time_stamp": 1754680000}
        captured, fake = self._client_with_capture(payload)
        with unittest.mock.patch.object(purpleair, "urlopen", fake):
            result = purpleair.PurpleAirClient("key").fetch_rows(
                bounds=purpleair.Bounds(46.0, -123.0, 45.0, -122.0),
                fields=purpleair.DATA_FIELDS,
                modified_since=1754670000,
            )
        self.assertIn("modified_since=1754670000", captured["url"])
        self.assertIn("pm2.5_cf_1", captured["url"])
        self.assertNotIn("latitude", captured["url"].split("fields=")[1].split("&")[0])
        self.assertEqual(result.time_stamp, 1754680000)
        self.assertEqual(result.rows, [])

    def test_rows_are_raw_value_dicts(self):
        payload = {
            "fields": ["sensor_index", "pm2.5_cf_1", "humidity"],
            "data": [[7, 4.0, 40], [8, 9.0, 55]],
            "time_stamp": 1754680000,
        }
        _, fake = self._client_with_capture(payload)
        with unittest.mock.patch.object(purpleair, "urlopen", fake):
            result = purpleair.PurpleAirClient("key").fetch_rows(
                bounds=purpleair.Bounds(46.0, -123.0, 45.0, -122.0))
        self.assertEqual(result.rows[0]["sensor_index"], 7)
        self.assertEqual(result.rows[1]["humidity"], 55)


class SensorFromValuesTest(unittest.TestCase):
    def test_builds_sensor_and_skips_partial_rows(self):
        full = {"sensor_index": 7, "name": "Deck", "latitude": 45.5, "longitude": -122.6,
                "pm2.5_cf_1": 4.0, "humidity": 40}
        sensor = purpleair.sensor_from_values(full)
        self.assertEqual(sensor.sensor_id, 7)
        self.assertIsNotNone(sensor.aqi)
        self.assertIsNone(purpleair.sensor_from_values({"sensor_index": 7, "pm2.5_cf_1": 4.0}))

    def test_trend_from_values_produces_seven_points(self):
        values = {"sensor_index": 7, "humidity": 40, "pm2.5_cf_1": 4.0,
                  "pm2.5_10minute": 4.2, "pm2.5_30minute": 4.4, "pm2.5_60minute": 4.6,
                  "pm2.5_6hour": 5.0, "pm2.5_24hour": 6.0, "pm2.5_1week": 5.5}
        trend = purpleair.trend_from_values(values)
        self.assertEqual([point["label"] for point in trend],
                         ["1w", "1d", "6h", "1h", "30m", "10m", "Now"])
        self.assertTrue(all(isinstance(point["aqi"], int) for point in trend))
```

Adapt the urlopen-stubbing mechanics to whatever `tests/test_purpleair.py` already does for `fetch_sensors` tests (see its existing `test_fetch_sensors_builds_show_only_query`) — reuse the same fake-response helper rather than inventing a parallel one, adding a `time_stamp` key where needed.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_purpleair -v`
Expected: new tests FAIL with `AttributeError` (no `MAP_FIELDS` / `fetch_rows` / `sensor_from_values`); existing tests still PASS.

- [ ] **Step 3: Implement**

In `airloom/purpleair.py`:

1. Replace the single `FIELDS` constant with three (keep `TREND_FIELDS` as-is):

```python
MAP_FIELDS = (
    "sensor_index",
    "name",
    "latitude",
    "longitude",
    "last_seen",
    "location_type",
    "humidity",
    "temperature",
    "pm1.0",
    "pm2.5_cf_1",
    "pm10.0",
)
DATA_FIELDS = (
    "sensor_index",
    "last_seen",
    "humidity",
    "temperature",
    "pm1.0",
    "pm2.5_cf_1",
    "pm10.0",
)
TREND_FETCH_FIELDS = (
    "sensor_index",
    "humidity",
    "pm2.5_cf_1",
    "pm2.5_10minute",
    "pm2.5_30minute",
    "pm2.5_60minute",
    "pm2.5_6hour",
    "pm2.5_24hour",
    "pm2.5_1week",
)
```

2. Add `FetchResult` and rework the client. `fetch_rows` is the real fetch; `fetch_sensors` becomes a wrapper so its existing tests/callers keep working:

```python
@dataclass(frozen=True, slots=True)
class FetchResult:
    rows: list[dict]
    time_stamp: int | None


class PurpleAirClient:
    def __init__(self, api_key: str, timeout: int = 20):
        self.api_key = api_key.strip()
        self.timeout = timeout

    def fetch_rows(
        self,
        bounds: Bounds | None = None,
        show_only: list[int] | None = None,
        location_filter: str = "outdoor",
        fields: tuple[str, ...] = MAP_FIELDS,
        modified_since: int | None = None,
    ) -> FetchResult:
        if not self.api_key:
            raise PurpleAirError("A PurpleAir read key is required for live data.")
        if show_only:
            show_only = [i for i in (_integer(value) for value in show_only) if i is not None]
        if bounds is None and not show_only:
            raise PurpleAirError("A sensor query needs bounds or sensor ids.")
        params: dict[str, str] = {"fields": ",".join(fields)}
        if show_only:
            params["show_only"] = ",".join(str(sensor_id) for sensor_id in show_only)
        else:
            location_type = {"outdoor": "0", "indoor": "1"}.get(location_filter)
            if location_type is not None:
                params["location_type"] = location_type
            params.update(
                {
                    "nwlat": f"{bounds.north:.6f}",
                    "nwlng": f"{bounds.west:.6f}",
                    "selat": f"{bounds.south:.6f}",
                    "selng": f"{bounds.east:.6f}",
                }
            )
        if modified_since is not None:
            params["modified_since"] = str(modified_since)
        query = urlencode(params)
        request = Request(
            f"{API_URL}?{query}",
            headers={"X-API-Key": self.api_key, "User-Agent": f"Airloom/{__version__}"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except Exception as exc:
            raise PurpleAirError(f"PurpleAir request failed: {exc}") from exc
        try:
            return parse_rows(payload)
        except PurpleAirError:
            raise
        except Exception as exc:
            raise PurpleAirError(f"PurpleAir returned unparseable data: {exc}") from exc

    def fetch_sensors(
        self,
        bounds: Bounds | None = None,
        show_only: list[int] | None = None,
        location_filter: str = "outdoor",
    ) -> list[Sensor]:
        result = self.fetch_rows(bounds=bounds, show_only=show_only, location_filter=location_filter)
        return _sensors_from_rows(result.rows)
```

3. Split parsing. `parse_rows` produces raw dicts; `sensor_from_values` / `trend_from_values` build model objects; `parse_sensor_payload` stays for compatibility:

```python
def parse_rows(payload: dict) -> FetchResult:
    if not isinstance(payload, dict):
        raise PurpleAirError("Unexpected PurpleAir response.")
    fields = payload.get("fields")
    rows = payload.get("data")
    if not isinstance(fields, list) or not isinstance(rows, list):
        message = payload.get("description") or payload.get("error") or "Unexpected PurpleAir response."
        raise PurpleAirError(str(message))
    values_list = [dict(zip(fields, row, strict=False)) for row in rows if isinstance(row, list)]
    return FetchResult(values_list, _integer(payload.get("time_stamp")))


def sensor_from_values(values: dict) -> Sensor | None:
    lat = _number(values.get("latitude"))
    lon = _number(values.get("longitude"))
    sensor_id = _integer(values.get("sensor_index"))
    if lat is None or lon is None or sensor_id is None:
        return None
    humidity = _number(values.get("humidity"))
    corrected_pm = epa_corrected_pm25(_number(values.get("pm2.5_cf_1")), humidity)
    temperature = _number(values.get("temperature"))
    # PurpleAir documents the temperature as being about 8°F above ambient
    # because the sensor electronics warm the enclosure.
    ambient_temperature = temperature - 8.0 if temperature is not None else None
    return Sensor(
        sensor_id=sensor_id,
        name=str(values.get("name") or f"Sensor {sensor_id}"),
        latitude=lat,
        longitude=lon,
        aqi=aqi_from_pm25(corrected_pm),
        pm25=_rounded(corrected_pm),
        temperature_f=_rounded(ambient_temperature),
        humidity=_rounded(humidity),
        pm1=_rounded(_number(values.get("pm1.0"))),
        pm10=_rounded(_number(values.get("pm10.0"))),
        last_seen=_integer(values.get("last_seen")),
        trend=[],
        indoor=_integer(values.get("location_type")) == 1,
    )


def trend_from_values(values: dict) -> list[dict]:
    humidity = _number(values.get("humidity"))
    trend = []
    for label, key in TREND_FIELDS:
        point_pm = epa_corrected_pm25(_number(values.get(key)), humidity)
        trend.append({"label": label, "aqi": aqi_from_pm25(point_pm)})
    return trend


def _sensors_from_rows(rows: list[dict]) -> list[Sensor]:
    sensors = []
    for values in rows:
        sensor = sensor_from_values(values)
        if sensor is not None:
            sensor.trend = trend_from_values(values)
            sensors.append(sensor)
    return sensors


def parse_sensor_payload(payload: dict) -> list[Sensor]:
    return _sensors_from_rows(parse_rows(payload).rows)
```

Note the behavior preserved for existing tests: `parse_sensor_payload` still attaches the trend (old payloads carried the average fields), while `sensor_from_values` alone returns `trend=[]` — area fetches with `MAP_FIELDS` have no averages to build a trend from, per the spec's lazy-trend design. `trend_from_values` with `values` missing the average keys yields `aqi: None` points for those labels; the JS chart already filters non-finite points.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_purpleair -v`
Expected: all PASS, including all pre-existing tests unchanged. If an existing test referenced the removed `FIELDS` constant, update that reference to `MAP_FIELDS`.

- [ ] **Step 5: Commit**

```bash
git add airloom/purpleair.py tests/test_purpleair.py
git commit -m "purpleair: raw-row fetches, modified_since, split field lists"
```

---

### Task 3: Zoom cap — `cap_bounds` in `purpleair.py`

**Files:**
- Modify: `airloom/purpleair.py` (next to `bounds_around`/`bounds_contains`)
- Test: `tests/test_purpleair.py`

**Interfaces:**
- Produces: `MAX_FETCH_SPAN_KM = 200.0` and `cap_bounds(bounds: Bounds, center: tuple[float, float]) -> Bounds` — returns `bounds` unchanged when within the span, else a 200 km box around `center`.

- [ ] **Step 1: Write the failing test**

```python
class CapBoundsTest(unittest.TestCase):
    def test_small_view_passes_through(self):
        bounds = purpleair.bounds_around(45.5, -122.6, 20.0)
        self.assertEqual(purpleair.cap_bounds(bounds, (45.5, -122.6)), bounds)

    def test_huge_view_is_capped_around_center(self):
        bounds = purpleair.Bounds(49.0, -130.0, 32.0, -114.0)  # ~1900 km tall
        capped = purpleair.cap_bounds(bounds, (40.0, -120.0))
        self.assertEqual(capped, purpleair.bounds_around(40.0, -120.0, 100.0))
        self.assertTrue(purpleair.bounds_contains(bounds, capped))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_purpleair.CapBoundsTest -v`
Expected: FAIL — `AttributeError: ... 'cap_bounds'`.

- [ ] **Step 3: Implement**

```python
MAX_FETCH_SPAN_KM = 200.0


def cap_bounds(bounds: Bounds, center: tuple[float, float]) -> Bounds:
    """Clamp a viewport to a fetchable area so one zoomed-out scroll can't
    request thousands of sensor rows."""
    height_km = (bounds.north - bounds.south) * 111.0
    mid_lat = (bounds.north + bounds.south) / 2
    width_km = (bounds.east - bounds.west) * 111.0 * max(0.1, math.cos(math.radians(mid_lat)))
    if height_km <= MAX_FETCH_SPAN_KM and width_km <= MAX_FETCH_SPAN_KM:
        return bounds
    return bounds_around(center[0], center[1], MAX_FETCH_SPAN_KM / 2)
```

(`bounds_around` clamps radius to ≤100 km, so the capped box is exactly the 200 km spec value.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_purpleair -v` — all PASS.

- [ ] **Step 5: Commit**

```bash
git add airloom/purpleair.py tests/test_purpleair.py
git commit -m "purpleair: cap oversized view fetches at a 200 km box"
```

---

### Task 4: `SensorCache` core — schema, upserts, regions, freshness

**Files:**
- Create: `airloom/cache.py`
- Test: `tests/test_cache.py` (new)

**Interfaces:**
- Produces (consumed by Tasks 5–8):
  - `@dataclass Region(id: int, bounds: Bounds, location_filter: str, fetched_at: float, api_time_stamp: int | None)`
  - `SensorCache(path: Path | None = None, clock=time.time)` — SQLite-backed, thread-safe (internal lock, `check_same_thread=False`), default path `$XDG_CACHE_HOME/airloom/cache.db` or `~/.cache/airloom/cache.db`
  - `.upsert_rows(rows: list[dict]) -> list[int]` — merge raw value dicts by `sensor_index`; returns ids whose merged row still lacks lat/lon (unknown sensors, not stored)
  - `.store_fetch(bounds, location_filter, rows, api_time_stamp) -> None`
  - `.apply_delta(region_id, rows, api_time_stamp) -> list[int]` — upsert + touch region; returns unknown ids
  - `.covering_region(bounds, location_filter, max_age: float | None = None) -> Region | None`
  - `.sensors_in(bounds) -> list[dict]` — raw value dicts inside bounds
  - `.fresh_sensors(ids, max_age) -> dict[int, dict]`
  - `.clear() -> None`
  - `.clock` — the injected time source (used by coordinators in Task 6)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cache.py`:

```python
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from airloom import cache
from airloom.purpleair import Bounds, bounds_around


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_cache -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'airloom.cache'` (import error counts as the failing state).

- [ ] **Step 3: Implement `airloom/cache.py`**

```python
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

from .purpleair import Bounds, bounds_contains

MAX_REGIONS = 50
SENSOR_MAX_AGE = 24 * 3600.0

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
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            self._db.executescript(_SCHEMA)
            self._db.commit()
        except sqlite3.Error:
            # It's only a cache: a corrupt file is deleted, never repaired.
            self.path.unlink(missing_ok=True)
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            self._db.executescript(_SCHEMA)
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

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM sensors")
            self._db.execute("DELETE FROM regions")
            self._db.execute("DELETE FROM trends")
            self._db.commit()
```

(The `trends` table and its prune line land now; its accessors come in Task 5.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_cache -v`
Expected: all PASS. Then `make test` — everything else still green.

- [ ] **Step 5: Commit**

```bash
git add airloom/cache.py tests/test_cache.py
git commit -m "cache: SQLite sensor cache with regions, deltas, freshness"
```

---

### Task 5: Trend storage and pruning behavior

**Files:**
- Modify: `airloom/cache.py`
- Test: `tests/test_cache.py`

**Interfaces:**
- Produces: `SensorCache.get_trend(sensor_id: int, max_age: float) -> list | None`, `SensorCache.store_trend(sensor_id: int, trend: list) -> None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cache.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_cache.TrendTest tests.test_cache.PruneTest -v`
Expected: `TrendTest` FAILS (`AttributeError: 'SensorCache' object has no attribute 'store_trend'`); `PruneTest` may partially pass (pruning shipped in Task 4) — that's fine, it locks the behavior under test.

- [ ] **Step 3: Implement**

Add to `SensorCache`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_cache -v` — all PASS.

- [ ] **Step 5: Commit**

```bash
git add airloom/cache.py tests/test_cache.py
git commit -m "cache: per-sensor trend storage with TTL"
```

---

### Task 6: Fetch coordinators — `fetch_area` and `fetch_favorites`

**Files:**
- Modify: `airloom/cache.py`
- Test: `tests/test_cache.py`

**Interfaces:**
- Consumes: `PurpleAirClient.fetch_rows` signature (Task 2), `SensorCache` (Tasks 4–5). The client is duck-typed — anything with `.fetch_rows(...)`.
- Produces:
  - `@dataclass AreaResult(rows: list[dict], age: float, polled: bool)` — `age` is seconds since the serving region's fetch (0.0 when polled), `polled` False only on a pure cache hit
  - `fetch_area(client, cache, bounds, location_filter, ttl, force=False) -> AreaResult`
  - `fetch_favorites(client, cache, favorite_ids, have_ids, ttl, force=False) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cache.py`:

```python
from airloom.purpleair import DATA_FIELDS, MAP_FIELDS, FetchResult


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_cache.FetchAreaTest tests.test_cache.FetchFavoritesTest -v`
Expected: FAIL — `AttributeError: module 'airloom.cache' has no attribute 'fetch_area'`.

- [ ] **Step 3: Implement**

Add to `airloom/cache.py` (imports grow: `from .purpleair import Bounds, bounds_contains, DATA_FIELDS, MAP_FIELDS, _integer`):

```python
@dataclass(frozen=True, slots=True)
class AreaResult:
    rows: list[dict]
    age: float
    polled: bool


def _matches_filter(values: dict, location_filter: str) -> bool:
    if location_filter == "both":
        return True
    want = 1 if location_filter == "indoor" else 0
    return _integer(values.get("location_type")) == want


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
```

Also change `purpleair._integer`'s leading underscore usage note: `cache.py` importing `_integer` is intra-package reuse of an existing helper — acceptable here; do **not** duplicate the coercion logic.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_cache -v` — all PASS. Then `make test`.

- [ ] **Step 5: Commit**

```bash
git add airloom/cache.py tests/test_cache.py
git commit -m "cache: fetch_area/fetch_favorites decision-flow coordinators"
```

---

### Task 7: Wire the cache into `app.py`

**Files:**
- Modify: `airloom/app.py` — imports (~line 40), `AUTO_REFRESH_SECONDS` (~line 61), `__init__` (~line 103), timer setup (~line 192), `_auto_refresh` (~line 289), `_on_script_message` `"ready"`/`"refresh"` (~lines 453–457), `_on_view_changed` (~line 525), `_save_settings` (~line 590), `refresh`/`_start_fetch` (~lines 644–692)

**Interfaces:**
- Consumes: `SensorCache`, `fetch_area`, `fetch_favorites`, `AreaResult` (cache.py); `cap_bounds`, `sensor_from_values`, `PurpleAirClient`, `PurpleAirError` (purpleair.py); `refresh_minutes` (store).
- Produces: `AirloomApp.refresh(force: bool = False)`, `AirloomApp._start_fetch(bounds, center, include_favorites, force=False)`, `AirloomApp._refresh_seconds() -> int`, `AirloomApp._arm_auto_refresh()`. Header refresh button and JS `"refresh"` action pass `force=True`; `"ready"`, auto-refresh, view-changed, filter/settings changes do not.

No unit tests (GUI module — consistent with the project's test policy); verification is `make check` plus the debug-port script in Task 10.

- [ ] **Step 1: Imports, cache instance, interval plumbing**

```python
# imports: add
from .cache import SensorCache, fetch_area, fetch_favorites
from .purpleair import PurpleAirClient, PurpleAirError, Bounds, bounds_around, bounds_contains, cap_bounds, sensor_from_values
# (extend the existing purpleair import line rather than adding a new one)
```

Delete the `AUTO_REFRESH_SECONDS = 300` constant. In `__init__` (near `self.store = Store()`):

```python
        self.cache = SensorCache()
        self._auto_refresh_id: int | None = None
```

Replace the `GLib.timeout_add_seconds(AUTO_REFRESH_SECONDS, self._auto_refresh)` line (~192) with `self._arm_auto_refresh()`, and add:

```python
    def _refresh_seconds(self) -> int:
        return int(self.store.data.get("refresh_minutes", 2)) * 60

    def _arm_auto_refresh(self) -> None:
        if self._auto_refresh_id is not None:
            GLib.source_remove(self._auto_refresh_id)
        self._auto_refresh_id = GLib.timeout_add_seconds(self._refresh_seconds(), self._auto_refresh)
```

In `_on_view_changed` (~line 525) replace `AUTO_REFRESH_SECONDS` with `self._refresh_seconds()`. In the comment inside `_finish_refresh` (~line 710) referencing `AUTO_REFRESH_SECONDS`, update the wording to "the refresh interval".

- [ ] **Step 2: Force-refresh plumbing**

```python
    def refresh(self, force: bool = False) -> None:
        """Home refresh: home bounds plus favorited sensors wherever they are."""
        config = self.store.data
        bounds = bounds_around(config["latitude"], config["longitude"], config["radius_km"])
        self._start_fetch(bounds, (config["latitude"], config["longitude"]),
                          include_favorites=True, force=force)
```

- Header button (~line 150): `refresh_button.connect("clicked", lambda *_: self.refresh(force=True))`
- `"refresh"` bridge action (~line 457): `self.refresh(force=True)`
- Everything else (`"ready"`, `_auto_refresh`, `_on_view_changed`, `_set_location_filter`, `_save_settings` tail) keeps calling without `force`.
- `pending_fetch` tuples now carry four elements — the `(bounds, center, include_favorites)` tuple at ~line 655 becomes `(bounds, center, include_favorites, force)` and `_start_fetch(*pending)` keeps working.

- [ ] **Step 3: Rewrite `_start_fetch` worker around the cache**

Replace the body of `_start_fetch`/worker (lines 650–692) with:

```python
    def _start_fetch(self, bounds: Bounds, center: tuple[float, float],
                     include_favorites: bool, force: bool = False) -> None:
        if not self.webview:
            return
        if self.refreshing:
            # Coalesce: the newest request wins and runs when the current lands.
            self.pending_fetch = (bounds, center, include_favorites, force)
            return
        self.refreshing = True
        bounds = cap_bounds(bounds, center)
        self._send("loading", {"active": True})
        config = dict(self.store.data)
        ttl = self._refresh_seconds()

        def worker() -> None:
            source = "Demo data"
            error = None
            sensors: list[Sensor] = []
            mode = config.get("location_filter", "outdoor")
            try:
                if config.get("api_key"):
                    client = PurpleAirClient(config["api_key"])
                    try:
                        area = fetch_area(client, self.cache, bounds, mode, ttl, force)
                        rows = list(area.rows)
                        if include_favorites:
                            have = {r.get("sensor_index") for r in rows}
                            rows += fetch_favorites(client, self.cache,
                                                    config.get("favorites", []), have, ttl, force)
                        sensors = [s for s in map(sensor_from_values, rows) if s is not None]
                        source = "PurpleAir live" if area.polled else \
                            f"PurpleAir · cached {_age_label(area.age)}"
                        if not sensors:
                            error = _no_sensors_message(mode)
                    except PurpleAirError as exc:
                        stale = [s for s in map(sensor_from_values, self.cache.sensors_in(bounds))
                                 if s is not None]
                        if stale:
                            # Stale real readings beat fake ones; demo only when
                            # the cache has nothing for this area.
                            sensors = stale
                            source = "PurpleAir · cached"
                            error = f"{exc} Showing cached readings."
                        else:
                            sensors = _filter_demo(demo_sensors(center[0], center[1]), mode)
                            source = "Demo data"
                            error = f"{exc} Showing demo readings instead."
                else:
                    sensors = _filter_demo(demo_sensors(center[0], center[1]), mode)
            except Exception as exc:  # noqa: BLE001 — a crashed worker must never wedge the refresh state
                sensors = []
                error = f"Refresh failed unexpectedly: {exc}"
            GLib.idle_add(self._finish_refresh, sensors, source, error, bounds)

        threading.Thread(target=worker, name="airloom-refresh", daemon=True).start()
```

Module-level helper (near `_no_sensors_message`):

```python
def _age_label(seconds: float) -> str:
    return "just now" if seconds < 90 else f"{int(seconds // 60)} min ago"
```

Notes for the implementer:
- `SensorCache` is thread-safe (internal lock), so the worker thread may use it freely; only GTK/WebKit calls must stay on the main loop.
- The stale-fallback path no longer filters by indoor/outdoor via `_matches_filter` — an error path favors showing *something*; `sensors_in` unfiltered is acceptable there, and `Sensor.indoor` still lets the JS browser-preview filter apply. Keep it simple.
- The `except PurpleAirError` no longer wraps the favorites fetch separately; a favorites failure after a successful area fetch lands in the stale-cache branch, which serves the area rows from cache — equivalent outcome to today's demo-label rule but with real data.

- [ ] **Step 4: Startup paint-from-cache**

In `_on_script_message`, the `"ready"` branch becomes:

```python
        if action == "ready":
            self._send("config", self.store.public_config())
            self._paint_cached_home()
            self.refresh()
```

And add:

```python
    def _paint_cached_home(self) -> None:
        """First paint from the cache so launch never blocks on the network.
        The refresh that follows replaces it under the normal TTL rules."""
        if self.sensors or not self.store.data.get("api_key"):
            return
        config = self.store.data
        bounds = bounds_around(config["latitude"], config["longitude"], config["radius_km"])
        cached = [s for s in map(sensor_from_values, self.cache.sensors_in(bounds)) if s is not None]
        if cached:
            self.sensors = cached
            self._send_sensor_state("PurpleAir · cached")
```

(If the cache is fresh, the subsequent `refresh()` is a zero-call fresh hit and just re-sends the same rows.)

- [ ] **Step 5: Settings save re-arms the timer**

In `_save_settings`, inside the `try` block after the `temperature_unit` line, add:

```python
            minutes = int(message["refresh_minutes"])
            if minutes not in (2, 5, 10, 30):
                minutes = 2
            updates["refresh_minutes"] = minutes
```

and after `self.store.save()` add `self._arm_auto_refresh()`.

- [ ] **Step 6: Verify and commit**

Run: `make check` — tests pass, `compileall` clean, `node --check` clean.
Also run: `python3 -c "import ast; ast.parse(open('airloom/app.py').read())"` (redundant with compileall, but fast feedback).

```bash
git add airloom/app.py
git commit -m "app: serve sensors through the persistent cache; force refresh; interval-driven timer"
```

---

### Task 8: Lazy trend fetch on sensor select

**Files:**
- Modify: `airloom/app.py` (`"select"` branch ~line 458, `_finish_refresh` ~line 694)
- Modify: `airloom/resources/app.js` (detail chart empty-state, ~line 280)

**Interfaces:**
- Consumes: `TREND_FETCH_FIELDS`, `trend_from_values`, `PurpleAirClient.fetch_rows` (Task 2); `SensorCache.get_trend`/`store_trend` (Task 5).
- Produces: `AirloomApp._ensure_trend(sensor_id: int)` and `AirloomApp._finish_trend(sensor_id: int, trend: list | None) -> bool`.

- [ ] **Step 1: Python side**

`"select"` branch becomes:

```python
        elif action == "select":
            sensor_id = self._message_sensor_id(message)
            if sensor_id is not None:
                self.selected_id = sensor_id
                self._ensure_trend(sensor_id)
```

At the end of `_finish_refresh`, right before the `pending_fetch` block, add:

```python
        if self.selected_id is not None:
            self._ensure_trend(self.selected_id)
```

New methods:

```python
    def _ensure_trend(self, sensor_id: int) -> None:
        """Attach a trend to the selected sensor: cached if fresh, else one
        cheap single-row fetch. Demo sensors already carry trends inline."""
        if not self.store.data.get("api_key"):
            return
        sensor = next((s for s in self.sensors if s.sensor_id == sensor_id), None)
        if sensor is None:
            return
        cached = self.cache.get_trend(sensor_id, self._refresh_seconds())
        if cached is not None:
            if sensor.trend != cached:
                sensor.trend = cached
                self._send_sensor_state()
            return
        api_key = self.store.data["api_key"]

        def worker() -> None:
            trend = None
            try:
                result = PurpleAirClient(api_key).fetch_rows(
                    show_only=[sensor_id], fields=TREND_FETCH_FIELDS)
                if result.rows:
                    trend = trend_from_values(result.rows[0])
            except PurpleAirError:
                trend = None  # chart keeps its loading/empty state; next select retries
            GLib.idle_add(self._finish_trend, sensor_id, trend)

        threading.Thread(target=worker, name="airloom-trend", daemon=True).start()

    def _finish_trend(self, sensor_id: int, trend: list | None) -> bool:
        if trend:
            self.cache.store_trend(sensor_id, trend)
            sensor = next((s for s in self.sensors if s.sensor_id == sensor_id), None)
            if sensor is not None:
                sensor.trend = trend
                self._send_sensor_state()
        return GLib.SOURCE_REMOVE
```

Extend the purpleair import line with `TREND_FETCH_FIELDS, trend_from_values`.

(No re-entrancy guard needed: a duplicate select while a trend fetch is in flight costs one redundant 17-point call at worst, and `_finish_trend` is idempotent.)

- [ ] **Step 2: JS chart loading state**

In `airloom/resources/app.js` ~line 281, the chart's empty state currently reads `'<div class="empty-state">No trend available</div>'`. Change that branch to distinguish "not loaded yet" (live mode) from "genuinely none":

```javascript
    if (!points.length) {
      const message = state.config.has_api_key ? "Loading trend…" : "No trend available";
      $("#chart").innerHTML = `<div class="empty-state">${message}</div>`;
      $("#trend-direction").textContent = "—";
      return;
    }
```

- [ ] **Step 3: Verify and commit**

Run: `make check` — all green (this also runs `node --check` on app.js).

```bash
git add airloom/app.py airloom/resources/app.js
git commit -m "app: lazy per-sensor trend fetch with cache and loading state"
```

---

### Task 9: Refresh-interval setting in the UI

**Files:**
- Modify: `airloom/resources/index.html` (form-grid, ~line 143)
- Modify: `airloom/resources/app.js` (default config ~line 9, `openSettings` ~line 697, submit handler ~line 1015)

**Interfaces:**
- Consumes: `refresh_minutes` in `public_config()` (Task 1) and `_save_settings` parsing (Task 7 Step 5).
- Produces: `refresh_minutes` in the `save-settings` bridge payload as a string ("2"/"5"/"10"/"30").

- [ ] **Step 1: index.html — add the control**

After the "Alert at AQI" label (~line 145):

```html
          <label><span>Refresh interval</span><select name="refresh_minutes">
            <option value="2">Every 2 minutes</option>
            <option value="5">Every 5 minutes</option>
            <option value="10">Every 10 minutes</option>
            <option value="30">Every 30 minutes</option>
          </select></label>
```

- [ ] **Step 2: app.js — defaults, populate, submit**

1. Line ~9 default config: add `refresh_minutes: 2,` after `alert_threshold: 101,`.
2. `openSettings` (~line 697): extend the populate loop's field list:

```javascript
    for (const field of ["radius_km", "alert_threshold", "heatmap_threshold_km", "refresh_minutes"]) form.elements[field].value = config[field];
```

3. Submit handler payload (~line 1015): add `refresh_minutes: form.get("refresh_minutes"),` alongside `alert_threshold`.

- [ ] **Step 3: Verify**

Run: `make check` (node --check passes). Open `airloom/resources/index.html` in a browser (preview fallback): the Preferences dialog shows the dropdown with "Every 2 minutes" preselected.

- [ ] **Step 4: Commit**

```bash
git add airloom/resources/index.html airloom/resources/app.js
git commit -m "ui: refresh interval preference (2/5/10/30 minutes)"
```

---

### Task 10: Docs, changelog, and end-to-end verification

**Files:**
- Modify: `CLAUDE.md` (Architecture + Data flow paragraphs)
- Modify: `CHANGELOG.md` (new Unreleased section at top)

**Interfaces:** none — documentation and verification only.

- [ ] **Step 1: CLAUDE.md**

In the **Data flow** paragraph, after the sentence about `purpleair.py`/`demo.py`, add:

```markdown
`cache.py` sits between them: a SQLite cache (`~/.cache/airloom/cache.db`, GTK-free,
thread-safe) of raw sensor field values plus fetched-region records. Area fetches are
served from cache inside the `refresh_minutes` TTL, delta-polled via `modified_since`
when stale (only changed rows are returned and billed), and fully fetched only for new
regions; views wider than 200 km are capped (`cap_bounds`). Trend averages are not part
of area fetches — they're fetched lazily per sensor on selection and cached. The header
refresh button forces a poll (`refresh(force=True)`) but still delta-polls. On API
failure, stale cached readings are shown before demo data. Demo data is never cached.
```

Also update the Commands section's test hint line to mention `cache` in the list of GUI-free tested modules ("`aqi`, `purpleair`, `store`, `cache`, bridge encoding").

- [ ] **Step 2: CHANGELOG.md**

Insert at the top (below the intro line, above `## 0.10.0`):

```markdown
## Unreleased

- Live PurpleAir data is now cached in a local SQLite database, cutting API point
  usage dramatically: map areas are reused within a configurable refresh interval
  (new "Refresh interval" preference: 2/5/10/30 minutes, default 2), repeat polls
  send `modified_since` so only changed sensors are billed, trend charts load
  per-sensor on demand instead of for every sensor on the map, and very wide map
  views cap the fetched area at 200 km. The header refresh button always forces a
  poll. If PurpleAir is unreachable, Airloom now shows your cached readings (with
  their age) instead of demo data.
```

- [ ] **Step 3: Full verification**

1. `make check` — full suite + compileall + node --check.
2. Debug-port end-to-end (needs a real API key configured):

```bash
AIRLOOM_DEBUG_SOCKET="$XDG_RUNTIME_DIR/airloom-debug.sock" scripts/debug-run &
sleep 6
scripts/debug-client state | head -c 400        # source should say "PurpleAir live" (first run: miss)
scripts/debug-client quit
AIRLOOM_DEBUG_SOCKET="$XDG_RUNTIME_DIR/airloom-debug.sock" scripts/debug-run &
sleep 6
scripts/debug-client state | head -c 400        # relaunch within TTL: source "PurpleAir · cached just now", no API call
scripts/debug-client tap '{"x": 400, "y": 300}' # select a sensor → trend fetch → chart fills
scripts/debug-client state | head -c 400
scripts/debug-client quit
```

3. Confirm `~/.cache/airloom/cache.db` exists and `sqlite3 ~/.cache/airloom/cache.db 'SELECT COUNT(*) FROM sensors'` is > 0.
4. In the app (or via `scripts/debug-client eval ...`): open Preferences → verify the Refresh interval dropdown round-trips; click the header refresh button → source flips to "PurpleAir live".

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md CHANGELOG.md
git commit -m "docs: describe the sensor cache and refresh interval"
```
