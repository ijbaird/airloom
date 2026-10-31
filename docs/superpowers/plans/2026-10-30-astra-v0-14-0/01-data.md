# W1: Data Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Malformed, boolean, future-dated, or expired provider data can never look like a fresh, alert-eligible reading, can never defeat delta polling, and cache, settings, and geocoder writes and lookups fail safely.
**Architecture:** One shared freshness rule (`models.py`) governs ingestion (`purpleair.py`, `cache.py`) and the alert boundary (`presentation.py`). `parse_rows` validates a whole response against the requested fields before anything reaches the cache. Every mutating SQLite transaction rolls back on failure. Settings writes fsync the directory entry. Reverse geocoding gets the same bounded, coalesced cache as search.
**Tech Stack:** Python 3 stdlib (`sqlite3`, `os`, `threading`, `unittest`, `unittest.mock`); no new dependencies.
**Spec:** docs/superpowers/specs/2026-10-30-astra-review-v0-14-0-design.md

## Global Constraints

- Python stdlib plus system PyGObject only. Vanilla JS/CSS/HTML with no bundler. Helpers stay UMD/namespaced so direct-file preview works.
- GTK and WebKit calls only on the main loop. Workers deliver via `GLib.idle_add`. New blocking I/O must not run on the main loop.
- No live PurpleAir, Nominatim, or OSM calls in tests. Fixtures and the existing debug-only seams only.
- `make check` passes with ResourceWarnings as errors. JS tests run on Node 22. The hosted Python 3.12 job runs without PyGObject.
- Any change that lets demo, cached, stale, expired, or malformed data look like a fresh live reading, or become alert-eligible, is a failure even if tests pass.
- Any new path that spends PurpleAir credits without a key the user currently has configured, or that multiplies billed requests, is a failure.
- Weakening an existing assertion or gate to make a fix pass is a failure.
- Changing the EPA AQI band colors, breakpoints, or correction equations is a non-goal.
- Freshness: the 2-hour observation age and a **10-minute skew tolerance** (controller's call).
- Settings: "If the directory fsync fails after a successful rename, the new settings are already visible and stay applied (no rollback). The failure is reported as a durability warning through the existing error/toast path."
- Ownership: W1 edits only `purpleair.py`, `cache.py`, `store.py`, `geocode.py`, `demo.py`, `models.py`, `geography.py`, `validation.py`, `location.py`, and the alert-freshness guard in `presentation.py` (`alert_transitions`, the module constant, and its import line), plus tests for those modules. Do not touch `tests/gi_stub.py` or `tests/record_handoff.py`.

## Review Focus

1. **Milliseconds supplied as seconds** (`last_seen`/`time_stamp` × 1000): such a value is far in the future, so it must parse as missing and never become a checkpoint. Pinned in Task 3 (`test_future_last_seen_is_missing_at_parse_and_sensor_boundaries`, `test_future_delta_checkpoint_keeps_previous_checkpoint`).
2. **A full snapshot with empty or garbled `fields` and empty `data`**: today it reads as authoritative absence, and `_reconcile_locked` deletes every cached sensor in scope. Pinned in Task 4 (`test_malformed_full_snapshot_preserves_cache_and_checkpoint`, empty-data subcase).
3. **Pre-0.14 cache rows whose `observed_at` is already far in the future**: no new write repairs them. Pinned in Task 3 (`test_legacy_future_observed_at_is_pruned`).
4. **First run, where `~/.config/airloom` doesn't exist yet, and a directory fsync failure after the rename**: covers both the pre-write and post-write failure classes. Pinned in Task 7 (`test_first_save_syncs_new_directory_entries_before_writing`, `test_directory_fsync_failure_after_replace_keeps_new_settings`, `test_new_directory_sync_failure_is_a_plain_prewrite_error`).
5. **A reverse-geocode leader that fails while an identical request waits**: the waiter must retry rather than receive a cached failure, and a later success must be cached. Pinned in Task 9 (`test_failed_reverse_leader_unblocks_waiter_for_retry_and_later_cache`).

---

### Task 1: Shared freshness rule and alert boundary (finding #3, alert part)

**Files:**
- Modify `airloom/models.py` (add constants and two functions after `passes_confidence`, ~line 51)
- Modify `airloom/presentation.py:5-8` (import, constant alias) and `:57-63` (guard only)
- Test: `tests/test_models.py` (new class `ObservationFreshnessTest`), `tests/test_presentation.py` (`AlertTransitionsTest`)

**Interfaces:**
- Produces (in `airloom/models.py`; W2 and W3 consume these, see Cross-workstream contracts):
  - `MAX_OBSERVATION_AGE: int = 2 * 3600`
  - `FUTURE_SKEW_TOLERANCE: int = 10 * 60`
  - `def plausible_observation_time(value: int | None, now: float) -> int | None`: returns `value` unless it is `None` or `value > now + FUTURE_SKEW_TOLERANCE`, in which case it returns `None`.
  - `def is_fresh_observation(last_seen: int | None, now: float) -> bool`: `True` iff `last_seen is not None` and `-FUTURE_SKEW_TOLERANCE <= now - last_seen <= MAX_OBSERVATION_AGE`. Both bounds are inclusive.
- `presentation.MAX_ALERT_OBSERVATION_AGE` stays as an alias of `MAX_OBSERVATION_AGE`, because `tests/test_presentation.py` imports it.

- [ ] **Step 1: Write the failing test**
  - `tests/test_models.py::ObservationFreshnessTest`:
    - `test_freshness_boundaries`. With `NOW = 1_800_000_000.0`, and the import inside the test so other tests in the module still run:
      ```python
      from airloom.models import FUTURE_SKEW_TOLERANCE, MAX_OBSERVATION_AGE, is_fresh_observation, plausible_observation_time
      self.assertEqual((MAX_OBSERVATION_AGE, FUTURE_SKEW_TOLERANCE), (7200, 600))
      for age, fresh in ((0, True), (7200, True), (7201, False), (-600, True), (-601, False)):
          self.assertIs(is_fresh_observation(int(NOW - age), NOW), fresh)
      self.assertFalse(is_fresh_observation(None, NOW))
      self.assertFalse(is_fresh_observation(int(NOW) * 1000, NOW))
      self.assertEqual(plausible_observation_time(int(NOW) + 600, NOW), int(NOW) + 600)
      self.assertIsNone(plausible_observation_time(int(NOW) + 601, NOW))
      self.assertIsNone(plausible_observation_time(None, NOW))
      self.assertEqual(plausible_observation_time(1, NOW), 1)  # old is plausible, just not fresh
      ```
  - `tests/test_presentation.py::AlertTransitionsTest`:
    - `test_future_observation_beyond_skew_never_alerts_or_changes_state`. Sensors `make_sensor(1, aqi=150, favorite=True, last_seen=NOW + 601)`, `make_sensor(2, aqi=150, favorite=True, last_seen=2**63 - 1)`, `make_sensor(3, aqi=150, favorite=True, last_seen=int(NOW) * 1000)`, with `previous={"1": False, "2": False, "3": False}`. Assert `alerted == []` and `state == previous`.
    - `test_observation_within_future_skew_is_eligible`. One sensor at `last_seen=NOW + 600`, `previous={"1": False}`. Assert `[s.sensor_id for s in alerted] == [1]` and `state == {"1": True}`.
- [ ] **Step 2: Run it and confirm it fails**
  `python3 -m unittest tests.test_models.ObservationFreshnessTest tests.test_presentation.AlertTransitionsTest -v`. Expect `ImportError: cannot import name 'FUTURE_SKEW_TOLERANCE'` in the models test. In the presentation test, expect `AssertionError: Lists differ: [<Sensor 1>, <Sensor 2>, <Sensor 3>] != []`, because negative ages pass today's `now - last_seen > MAX` guard.
- [ ] **Step 3: Implement** the four names above in `models.py`. In `presentation.py`, import `MAX_OBSERVATION_AGE` and `is_fresh_observation` from `.models`, set `MAX_ALERT_OBSERVATION_AGE = MAX_OBSERVATION_AGE`, and replace the two conditions `sensor.last_seen is None or now - sensor.last_seen > MAX_ALERT_OBSERVATION_AGE` with `not is_fresh_observation(sensor.last_seen, now)`. Leave the rest of `alert_transitions` untouched, because it is W2's.
- [ ] **Step 4: Run and confirm it passes**
  `python3 -m unittest tests.test_models tests.test_presentation -v`
- [ ] **Step 5: Commit**
  `git add airloom/models.py airloom/presentation.py tests/test_models.py tests/test_presentation.py && git commit -m "fix: share one observation freshness rule and reject future ages at the alert boundary"`

### Task 2: JSON booleans are never numbers (finding #2)

**Files:**
- Modify `airloom/purpleair.py:236-262` (`_number`, `_integer`)
- Test: `tests/test_purpleair.py` (new class `BooleanValueTest`), `tests/test_cache.py` (new class `BooleanCachedValueTest(CacheBase)`)

**Interfaces:**
- `_number(value) -> float | None` and `_integer(value) -> int | None` return `None` for any `bool`. `cache.py` already imports both, so `_coordinates`, `_matches_filter`, and `observed_at` inherit the change.

- [ ] **Step 1: Write the failing test**
  In `tests/test_purpleair.py::BooleanValueTest`, with `base = {"sensor_index": 7, "latitude": 45.5, "longitude": -122.6, "humidity": 50}`:
  - `test_boolean_pm25_is_unavailable_not_good`. This is the review's reproduction. For `value in (False, True)`, `s = purpleair.sensor_from_values({**base, "pm2.5_cf_1": value})` gives `s.aqi is None` and `s.pm25 is None`. Today it gives AQI 8 or 11.
  - `test_boolean_humidity_uses_neutral_assumption`. With `{**base, "pm2.5_cf_1": 20, "humidity": True}`: `humidity is None`, and `aqi` equals the AQI from the same row with `"humidity": None`.
  - `test_boolean_metadata_and_secondary_measurements_are_missing`. `temperature`, `pm1.0`, `pm10.0`, `confidence`, and `last_seen` set to `True` each give `None` on the `Sensor`. `location_type=True` gives `indoor is False`, and `trend_from_values({**base, "location_type": True, "pm2.5_60minute": 30.0})` equals the trend computed with `location_type=0` (the ATM basis).
  - `test_boolean_coordinates_drop_the_row`. For each of `latitude`/`longitude` set to `True` or `False`, `sensor_from_values` returns `None`.
  - `test_boolean_trend_windows_are_unavailable`. In `trend_from_values({**base, "pm2.5_cf_1": 10.0, "pm2.5_60minute": True, "pm2.5_1week": False})`, the `"1h"` and `"1w"` points have `aqi is None` and `"Now"` does not.
  - `test_boolean_time_stamp_is_not_a_checkpoint`. `purpleair.parse_rows({"fields": ["sensor_index"], "data": [], "time_stamp": True}).time_stamp is None`.

  In `tests/test_cache.py::BooleanCachedValueTest(CacheBase)`, with `bounds = bounds_around(45.5, -122.6, 20.0)`:
  - `test_cached_boolean_measurements_stay_unavailable`. `cache.fetch_area(FakeClient([FetchResult([row(1, **{"pm2.5_cf_1": False}), row(2, humidity=True, **{"pm2.5_cf_1": 20.0})], 1754680000)]), self.cache, bounds, "outdoor", ttl=120)`. Then `{r["sensor_index"]: purpleair.sensor_from_values(r) for r in result.rows}` gives sensor 1 `aqi is None`, and sensor 2 an AQI equal to `sensor_from_values(row(2, humidity=None, **{"pm2.5_cf_1": 20.0})).aqi`. Re-read through `self.cache.sensors_in(bounds)` gives the same results.
  - `test_boolean_coordinates_are_unresolved_and_not_stored`. `self.cache.upsert_rows([row(3, lat=True)]) == [3]` and `self.cache.sensors_in(bounds) == []`.
  - `test_boolean_last_seen_does_not_become_epoch_one`. Set `self.clock.now = 200000.0` so that epoch 1 is past the 24-hour prune cutoff, store a region with `store_fetch(bounds, "outdoor", [row(3, last_seen=200000)], 200000)`, then `self.cache.upsert_rows([row(4, last_seen=True)])`. Then `observed_at` (via `self.cache._db.execute("SELECT observed_at FROM sensors WHERE sensor_index = 4")`) equals `self.clock.now`. The row survives `self.cache.prune()`, and `covering_region` for a region stored beforehand is not deleted. Today `observed_at == 1.0`, so the row is pruned and coverage is invalidated.
  - `test_boolean_location_type_is_outdoor_for_filters`. Store `row(5, location_type=True)` via `store_fetch(bounds, "indoor", ..., 1754680000)`, then `cache.fetch_area(FakeClient([]), self.cache, bounds, "indoor", ttl=120).rows == []`.
  - Add `from airloom import purpleair` to the imports.
- [ ] **Step 2: Run it and confirm it fails**
  `python3 -m unittest tests.test_purpleair.BooleanValueTest tests.test_cache.BooleanCachedValueTest -v`. Expect failures such as `AssertionError: 8 is not None` and `1.0 != 1000.0`.
- [ ] **Step 3: Implement** In both helpers, return `None` when `value is None or isinstance(value, bool)`. Do not touch the other numeric handling.
- [ ] **Step 4: Run and confirm it passes**
  `python3 -m unittest tests.test_purpleair tests.test_cache -v`
- [ ] **Step 5: Commit**
  `git add airloom/purpleair.py tests/test_purpleair.py tests/test_cache.py && git commit -m "fix: treat JSON booleans as missing for every numeric PurpleAir field"`

### Task 3: Future observation times and checkpoints are rejected at ingestion (finding #3, ingestion part)

**Files:**
- Modify `airloom/purpleair.py`: `PurpleAirClient.__init__` (:95), `fetch_rows` (:141), `parse_rows` (:157-166), `sensor_from_values` (:169-202), `_sensors_from_rows` (:222), `parse_sensor_payload` (:232)
- Modify `airloom/cache.py`: `_upsert_locked` (:230-234), `_prune_locked` (:354-357)
- Test: `tests/test_purpleair.py` (new class `FutureTimestampTest`), `tests/test_cache.py` (new class `FutureObservationTest(CacheBase)`)

**Interfaces:**
- Consumes `plausible_observation_time` and `FUTURE_SKEW_TOLERANCE` from Task 1.
- Produces:
  - `PurpleAirClient(api_key: str, timeout: int = 20, clock: Callable[[], float] = time.time)`. `fetch_rows` calls `parse_rows(payload, now=self.clock())`. Task 4 adds `fields`.
  - `parse_rows(payload: dict, requested: tuple[str, ...] | None = None, *, now: float | None = None) -> FetchResult`. `now=None` means `time.time()`. Every row that has a `"last_seen"` key gets `plausible_observation_time(_integer(value), now)` written back. `FetchResult.time_stamp` is `plausible_observation_time(_integer(payload.get("time_stamp")), now)`. Task 4 uses `requested`. Add the parameter now and leave it unused.
  - `sensor_from_values(values: dict, now: float | None = None) -> Sensor | None`. `last_seen` is `plausible_observation_time(_integer(...), time.time() if now is None else now)`.
  - `_sensors_from_rows(rows, now: float | None = None)` and `parse_sensor_payload(payload, now: float | None = None)` pass `now` through.
  - `app.py`'s callers keep working unchanged through the defaults.

- [ ] **Step 1: Write the failing test**
  - `tests/test_purpleair.py`: add the module helper `def map_row(sensor_id, **values) -> list` (cells in `MAP_FIELDS` order, defaults `latitude=45.5, longitude=-122.6, last_seen=NOW, pm2.5_cf_1=20.0, humidity=50`, `name=f"S{sensor_id}"`, other fields `None`) and the constant `NOW = 1_800_000_000`. Reuse `FetchRowsTest._fetch_rows_with_payload`, adding a `clock` parameter forwarded to `PurpleAirClient("key", clock=clock)`.
    - `test_future_last_seen_is_missing_at_parse_and_sensor_boundaries`. Rows `map_row(1, last_seen=NOW + 600)`, `map_row(2, last_seen=NOW + 601)`, `map_row(3, last_seen=NOW * 1000)`, and `map_row(4, last_seen=2**63 - 1)`, fetched with `clock=lambda: NOW` and `bounds=Bounds(46.0, -123.0, 45.0, -122.0)`. Assert `[r["last_seen"] for r in result.rows] == [NOW + 600, None, None, None]`. Also assert `purpleair.sensor_from_values({"sensor_index": 9, "latitude": 45.5, "longitude": -122.6, "last_seen": NOW + 601}, now=NOW).last_seen is None`, and that `NOW + 600` is kept.
    - `test_future_time_stamp_is_not_a_checkpoint`. With `"time_stamp"` set to `NOW + 601`, then `NOW * 1000`, then `NOW + 600`, `result.time_stamp` is `None`, `None`, `NOW + 600`.
    - `test_far_future_observation_parses_ineligible_for_alerts`. This is the review's `2**63-1` reproduction. `sensors = parse_sensor_payload({"fields": list(purpleair.MAP_FIELDS), "data": [map_row(1, last_seen=2**63 - 1, **{"pm2.5_cf_1": 100.0})]}, now=NOW)`, then `presentation.alert_transitions(sensors, favorites={1}, previous={}, threshold=101, now=NOW, origin="live")` returns `([], {})`.
  - `tests/test_cache.py::FutureObservationTest(CacheBase)`, with `self.clock.now = NOW = 1_800_000_000.0` in `setUp`:
    - `test_future_last_seen_is_stored_as_missing_and_still_ages_out`. `self.cache.upsert_rows([row(1, last_seen=int(NOW) * 1000)])`. The `sensors_in(bounds)[0]["last_seen"]` is `None`, and `SELECT observed_at` is `NOW`. After `self.clock.now += cache.SENSOR_MAX_AGE + 1; self.cache.prune()`, `sensors_in(bounds) == []`.
    - `test_legacy_future_observed_at_is_pruned`. Insert a row with `observed_at = NOW * 1000` and `fetched_at = NOW` directly via `self.cache._db.execute("INSERT INTO sensors ...")` and commit. After `self.cache.prune()` the row is gone.
    - `test_future_delta_checkpoint_keeps_previous_checkpoint` (the real path, PurpleAirClient → `fetch_area` → cache):
      1. `self.cache.store_fetch(bounds, "outdoor", [row(1, last_seen=int(NOW))], int(NOW))`, then `self.clock.now += 300`.
      2. Build `client = PurpleAirClient("key", clock=self.clock)` and patch `airloom.purpleair.urlopen`. The first response is `{"fields": list(DATA_FIELDS), "data": [[1, int(NOW) + 300, 50, 70, 1.0, 4.0, 5.0, 99]], "time_stamp": int(NOW) * 1000}`. Capture every request URL.
      3. Call `cache.fetch_area(client, self.cache, bounds, "outdoor", ttl=120)`. Then `self.cache.covering_region(bounds, "outdoor").api_time_stamp == int(NOW)`.
      4. After `self.clock.now += 300`, a second `fetch_area` (the second response is an empty `DATA_FIELDS` payload with `time_stamp: int(NOW) + 600`) requests a URL containing `f"modified_since={int(NOW)}"`.
    - `test_future_full_snapshot_stamp_forces_next_full_fetch`. With no region, the first response is a `MAP_FIELDS` payload with `time_stamp: int(NOW) * 1000`. Then `covering_region(...).api_time_stamp is None`. After `+300`, the second URL contains no `modified_since` and its `fields=` lists `latitude`.
  - Add to `tests/test_cache.py`: `import io, json`, `from unittest import mock`, `from airloom.purpleair import PurpleAirClient`, and a module helper `def fake_urlopen(payloads, urls)` modeled on `test_purpleair.FetchRowsTest`.
- [ ] **Step 2: Run it and confirm it fails**
  `python3 -m unittest tests.test_purpleair.FutureTimestampTest tests.test_cache.FutureObservationTest -v`. Expect `TypeError: PurpleAirClient.__init__() got an unexpected keyword argument 'clock'` and `TypeError: sensor_from_values() got an unexpected keyword argument 'now'`. The legacy-prune test fails with `AssertionError: 1 != 0`.
- [ ] **Step 3: Implement** the signatures above.
  - In `cache._upsert_locked`, when `"last_seen" in values`, set `merged["last_seen"] = plausible_observation_time(_integer(values["last_seen"]), now)` after `merged.update(values)`. `observed_at` derives from that validated value, falling back to the existing `observed_at`, then to `now`.
  - In `_prune_locked`, also delete `observed_at > now + FUTURE_SKEW_TOLERANCE` in the same `DELETE`, so it counts toward `cursor.rowcount`, which keeps the conservative region invalidation.
- [ ] **Step 4: Run and confirm it passes**
  `python3 -m unittest tests.test_purpleair tests.test_cache tests.test_presentation -v`
- [ ] **Step 5: Commit**
  `git add airloom/purpleair.py airloom/cache.py tests/test_purpleair.py tests/test_cache.py && git commit -m "fix: reject future observation times and checkpoints beyond a 10-minute skew"`

### Task 4: Validate whole responses against the requested fields (finding #1)

**Files:**
- Modify `airloom/purpleair.py`: new `MEASUREMENT_FIELDS` after `TREND_FIELDS` (~:65), `fetch_rows` (:141), `parse_rows` (:157-166)
- Modify `airloom/cache.py:_upsert_locked` (:221-223, the retained-measurement guard)
- Test: `tests/test_purpleair.py` (new class `ResponseValidationTest`; update existing fixtures as listed), `tests/test_cache.py` (new class `MalformedResponseTest(CacheBase)`)

**Interfaces:**
- Produces:
  - `MEASUREMENT_FIELDS: frozenset[str]`, which is `{"last_seen", "humidity", "temperature", "pm1.0", "pm2.5_cf_1", "pm10.0", "confidence"} | {key for _, key in TREND_FIELDS}`.
  - `parse_rows(payload, requested, *, now)` raises `PurpleAirError` and returns no rows when:
    - `fields` is not a list of `str`;
    - any `data` element is not a `list`, or its `len` differs from `len(fields)`. This applies whether or not `requested` is given.
    - with `requested` given: `"sensor_index"` is missing from `fields`, or any field in `set(requested) & MEASUREMENT_FIELDS` is missing.
  - `fetch_rows` passes `fields` as `requested`. Because the error is raised inside `client.fetch_rows`, `fetch_area` and `fetch_favorites` never reach `apply_delta`, `store_fetch`, or `upsert_rows`, so the checkpoint and cache stay intact without further cache changes.
- Cache guard (defense in depth for non-parsed row sources such as the debug fake client): in `_upsert_locked`, if the incoming row carries a validated `last_seen` that differs from the existing row's `last_seen`, every key in `MEASUREMENT_FIELDS - {"last_seen"}` that is present in the existing data but absent from `values` is set to `None`. A retained old measurement never gets a new observation time.

- [ ] **Step 1: Write the failing test**
  - `tests/test_purpleair.py::ResponseValidationTest` (through `FetchRowsTest._fetch_rows_with_payload`, so the real `fetch_rows` → `parse_rows` path runs):
    - `test_short_row_rejects_whole_response`. `{"fields": list(DATA_FIELDS), "data": [[1, NOW]], "time_stamp": NOW}` with `fields=DATA_FIELDS` raises `PurpleAirError`.
    - `test_long_row_rejects_whole_response` (one extra cell).
    - `test_non_list_row_rejects_whole_response`. `"data": [map_row(1), "junk"]`.
    - `test_missing_requested_measurement_field_rejects_response`. `fields` is `MAP_FIELDS` minus `"pm2.5_cf_1"` and rows match that shorter length. Raises with the default `fields=MAP_FIELDS`.
    - `test_missing_sensor_index_rejects_response`.
    - `test_non_string_field_name_rejects_response`. `"fields": [*MAP_FIELDS[:-1], 5]`.
    - `test_omitted_metadata_is_not_required_for_delta`. A `DATA_FIELDS`-requested response whose `fields` are exactly `DATA_FIELDS` (no `name`, `latitude`, `longitude`, `location_type`) parses.
  - Update existing fixtures that the new rule makes malformed. This is not a weakening: each keeps its original assertion.
    - `test_fetch_sensors_builds_show_only_query`, `test_show_only_tolerates_bad_ids`, and `_fetch_url`: the body becomes `json.dumps({"fields": list(purpleair.MAP_FIELDS), "data": []})`.
    - `test_modified_since_and_fields_in_query`: `"fields": list(purpleair.DATA_FIELDS)`.
    - `test_rows_are_raw_value_dicts`: pass `fields=("sensor_index", "pm2.5_cf_1", "humidity")` to `fetch_rows`.
    - `test_hostile_values_never_raise`: drop the short `[13, ...]` row and its `"short row tolerated"` assertion, and add `test_short_row_is_rejected_not_tolerated` asserting `parse_sensor_payload` of a payload whose last row is short raises `PurpleAirError`. The spec reverses this behavior deliberately, so call the change out in the commit message.
  - `tests/test_cache.py::MalformedResponseTest(CacheBase)`:
    - `test_short_delta_row_cannot_refresh_old_measurement_or_alert`. This is the review's C1 reproduction.
      1. `self.clock.now = 100000.0`; `self.cache.store_fetch(bounds, "outdoor", [row(1, humidity=50, last_seen=100000, **{"pm2.5_cf_1": 100.0})], 100000)`; `self.clock.now = 110000.0`.
      2. Patch `urlopen` to return `{"fields": list(DATA_FIELDS), "data": [[1, 110000]], "time_stamp": 110000}`, with `client = PurpleAirClient("key", clock=self.clock)`.
      3. `with self.assertRaises(PurpleAirError): cache.fetch_area(client, self.cache, bounds, "outdoor", ttl=120)`.
      4. `rows = self.cache.sensors_in(bounds)` gives `rows[0]["last_seen"] == 100000` and `rows[0]["pm2.5_cf_1"] == 100.0`. The region has `api_time_stamp == 100000` and `fetched_at == 100000.0`.
      5. `sensors = [purpleair.sensor_from_values(r, now=110000) for r in rows]` and `presentation.alert_transitions(sensors, favorites={1}, previous={}, threshold=101, now=110000, origin="live") == ([], {})`.
    - `test_malformed_full_snapshot_preserves_cache_and_checkpoint`. Store `[row(1, last_seen=100000), row(2, last_seen=100000)]` with `api_time_stamp=None` at `clock=100000`, which forces the full path. Then `+300`. Use `subTest` for these `MAP_FIELDS` responses: `"data": ["not a row"]`; `"data": [[1, "S1"]]` (short); `"fields": [], "data": []` (empty and garbled).
      - Each raises `PurpleAirError`.
      - Afterwards, `sorted(r["sensor_index"] for r in self.cache.sensors_in(bounds)) == [1, 2]`.
      - `SELECT COUNT(*), MAX(fetched_at) FROM regions` gives `(1, 100000.0)`.
    - `test_partial_upsert_never_dates_retained_measurement`. Store `row(1, last_seen=100000, **{"pm2.5_cf_1": 100.0})`, then `self.cache.upsert_rows([{"sensor_index": 1, "last_seen": 100500}])` at clock `100500`. The stored row has `last_seen == 100500` and `["pm2.5_cf_1"] is None`, so `sensor_from_values(...).aqi is None`. A row that carries no `last_seen` (for example `{"sensor_index": 1, "humidity": 60}`) still merges onto the existing measurements, as `test_stale_hit_delta_polls_with_region_bounds` expects.
- [ ] **Step 2: Run it and confirm it fails**
  `python3 -m unittest tests.test_purpleair.ResponseValidationTest tests.test_cache.MalformedResponseTest -v`. Expect `AssertionError: PurpleAirError not raised`, and in the guard test `AssertionError: 100.0 is not None`.
- [ ] **Step 3: Implement** the validation in `parse_rows`. It must run before building any row dict, and it uses `zip(fields, row, strict=True)` once lengths are checked. Add `MEASUREMENT_FIELDS`, the `fetch_rows` call change, and the `_upsert_locked` guard as specified. Error messages: `"PurpleAir response rows do not match its fields."` and `"PurpleAir response omitted requested fields: <sorted names>."`
- [ ] **Step 4: Run and confirm it passes**
  `python3 -m unittest tests.test_purpleair tests.test_cache tests.test_app_requests tests.test_integration -v`. The last two exercise `fetch_area` through the adapter with partial fake rows. If a fixture there breaks because it relied on a partial row with a new `last_seen` keeping old PM, report it rather than edit it: those modules are W2's.
- [ ] **Step 5: Commit**
  `git add airloom/purpleair.py airloom/cache.py tests/test_purpleair.py tests/test_cache.py && git commit -m "fix: reject short or field-incomplete PurpleAir responses before any cache commit" -m "test_hostile_values_never_raise no longer tolerates short rows; the spec requires rejecting them."`

### Task 5: An expired favorite observation cannot invalidate area coverage (finding #7)

**Files:**
- Modify `airloom/cache.py:_upsert_locked` (:211-243)
- Test: `tests/test_cache.py` (new class `OfflineFavoriteCoverageTest(CacheBase)`)

**Interfaces:**
- `_upsert_locked(rows) -> list[int]` skips (does not insert or update) any row whose validated `observed_at` is already `< now - SENSOR_MAX_AGE`. The skip happens before the coordinate check, and such rows are not reported as unknown.
- `fetch_favorites` still returns the fetched row (`rows += result.rows` is unchanged), so the offline favorite is still shown. Its `last_seen` makes it Old and alert-ineligible.
- Genuine expiry of previously stored rows still deletes regions through `_prune_locked`, unchanged. `test_observation_prune_invalidates_coverage_before_revisit` must keep passing unmodified.

- [ ] **Step 1: Write the failing test**
  `test_offline_favorite_keeps_area_delta_polling_across_cycles`. This is the review's I1 reproduction.
  1. Setup: `self.clock.now = 200000.0`, `bounds = bounds_around(45.5, -122.6, 20.0)`, `offline = row(99, lat=40.0, lon=-120.0, last_seen=200000 - 2 * 86400)`.
  2. Script three cycles: `area = cache.fetch_area(client, self.cache, bounds, "outdoor", ttl=120)`, then `fav = cache.fetch_favorites(client, self.cache, [99], {r["sensor_index"] for r in area.rows}, ttl=120)`, then `self.clock.now += 300`.
  3. Script `FakeClient` responses in call order:
     - `FetchResult([row(1, last_seen=200000)], 200000)`, then `FetchResult([offline], 200000)`;
     - `FetchResult([], 200300)`, then `FetchResult([offline], 200300)`;
     - `FetchResult([], 200600)`, then `FetchResult([offline], 200600)`.
  4. Assertions:
     - `client.calls[0]["fields"] == MAP_FIELDS` and `"modified_since" not in client.calls[0]`;
     - `[c.get("show_only") for c in client.calls[1::2]] == [[99], [99], [99]]`, so there is exactly one favorite request per cycle;
     - `client.calls[2]["fields"] == DATA_FIELDS`, `client.calls[2]["modified_since"] == 200000`, `client.calls[4]["fields"] == DATA_FIELDS`, `client.calls[4]["modified_since"] == 200300`;
     - each cycle's `fav` has `[r["sensor_index"] for r in fav] == [99]` with `last_seen == 200000 - 2 * 86400`, which shows the user-visible favorite didn't regress;
     - `self.cache.covering_region(bounds, "outdoor") is not None` after every cycle;
     - `self.cache.fresh_sensors([99], 3600) == {}`.

  `test_expired_incoming_row_is_not_reported_unknown`. `self.cache.upsert_rows([row(98, lat=None, last_seen=1)]) == []` at clock 200000.
- [ ] **Step 2: Run it and confirm it fails**
  `python3 -m unittest tests.test_cache.OfflineFavoriteCoverageTest -v`. Expect `AssertionError: (12 fields...) != ('sensor_index', 'last_seen', ...)` on `calls[2]["fields"]` (today it is `MAP_FIELDS`, with no `modified_since`).
- [ ] **Step 3: Implement** the skip in `_upsert_locked` after computing the validated `observed_at`, which means moving the coordinate check after it.
- [ ] **Step 4: Run and confirm it passes**
  `python3 -m unittest tests.test_cache -v`
- [ ] **Step 5: Commit**
  `git add airloom/cache.py tests/test_cache.py && git commit -m "fix: an already-expired favorite observation no longer wipes area coverage"`

### Task 6: Every mutating cache transaction rolls back on failure (finding #9)

**Files:**
- Modify `airloom/cache.py:371-379` (`store_trend`), `:390-395` (`clear`)
- Test: `tests/test_cache.py` (new class `TransactionRollbackTest(unittest.TestCase)`)

**Interfaces:** None new. `store_trend` and `clear` get the same `try: ... commit() / except BaseException: rollback(); raise` discipline as `store_fetch`. `prune`, `upsert_rows`, `store_fetch`, and `apply_delta` already have it, and nothing else mutates.

- [ ] **Step 1: Write the failing test**
  `setUp`: temporary dir, `self.cache = cache.SensorCache(path, clock=FakeClock(), busy_timeout=0.01)`, and a helper `_hold_read_lock()` that opens `sqlite3.connect(path, isolation_level=None)`, runs `BEGIN` plus `SELECT COUNT(*) FROM trends`, and registers `addCleanup(close)`.
  - `test_failed_trend_commit_releases_transaction_and_locks`:
    ```python
    reader = self._hold_read_lock()
    with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
        self.cache.store_trend(1, [{"label": "Now", "aqi": 5}])
    self.assertFalse(self.cache._db.in_transaction)
    self.assertIsNone(self.cache.get_trend(1, max_age=3600))
    reader.execute("COMMIT")
    writer = sqlite3.connect(self.path, timeout=0.01); self.addCleanup(writer.close)
    writer.execute("INSERT INTO trends VALUES (2, '[]', 1000.0)"); writer.commit()   # no lock retained
    self.cache.store_trend(1, [{"label": "Now", "aqi": 5}])                          # retry succeeds
    self.assertEqual(self.cache.get_trend(1, max_age=3600), [{"label": "Now", "aqi": 5}])
    ```
  - `test_failed_clear_keeps_data_and_is_not_committed_later`. Seed `store_fetch(bounds, "outdoor", [row(1)], 1754680000)` and `store_trend(1, ...)`, then hold the read lock. `clear()` raises `OperationalError`, and `in_transaction` is False. Release the lock, then call `self.cache.store_trend(2, [])`, an unrelated successful commit. An independent observer connection still counts `(1, 1, 2)` across sensors, regions, and trends.
- [ ] **Step 2: Run it and confirm it fails**
  `python3 -m unittest tests.test_cache.TransactionRollbackTest -v`. Expect `AssertionError: True is not false` on `in_transaction`, which matches the review's probe.
- [ ] **Step 3: Implement** the rollback wrappers.
- [ ] **Step 4: Run and confirm it passes**
  `python3 -W error::ResourceWarning -m unittest tests.test_cache -v`
- [ ] **Step 5: Commit**
  `git add airloom/cache.py tests/test_cache.py && git commit -m "fix: roll back failed trend and clear transactions so locks are released"`

### Task 7: Settings saves are crash-durable (finding #13)

**Files:**
- Modify `airloom/store.py`: new exception and helper near the top, `Store.apply` (:117-122), `Store._write` (:124-149)
- Test: `tests/test_store.py` (new class `DurabilityTest(unittest.TestCase)`)

**Interfaces:**
- Produces (W2 consumes, see Cross-workstream contracts):
  - `class SettingsDurabilityError(OSError)` with `__init__(self, message: str, saved: dict)` and attribute `saved: dict`.
  - `Store.apply(candidate) -> dict` raises `SettingsDurabilityError` only after `os.replace` succeeded, and only after setting `self.data = saved`. All mutators (`toggle_favorite`, `hide`, `unhide`, `unhide_all`, `save`) propagate it. Message: `f"Settings were saved, but the system could not confirm they reached the disk: {exc}"`.
  - `def _fsync_directory(path: Path) -> None` opens with `os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)`, then `os.fsync` and `os.close` in a `finally`.
- Write order in `_write`:
  1. Collect the nonexistent ancestors of `self.path.parent`, outermost first.
  2. `mkdir`.
  3. `_fsync_directory(d.parent)` for each created `d`, outermost first. A failure here is a plain `OSError`, which counts as a pre-write failure, so data stays unchanged.
  4. Write the temp file, then `os.fsync` it.
  5. `os.replace`.
  6. `_fsync_directory(self.path.parent)`. A failure here raises `SettingsDurabilityError(message, data)`.

- [ ] **Step 1: Write the failing test**
  Use a recorder that wraps the real calls:
  ```python
  events = []
  real_fsync, real_replace = os.fsync, os.replace
  def fsync(fd): events.append(("fsync", os.fstat(fd).st_ino)); real_fsync(fd)
  def replace(src, dst): events.append(("replace",)); real_replace(src, dst)
  # mock.patch("airloom.store.os.fsync", side_effect=fsync), mock.patch("airloom.store.os.replace", side_effect=replace)
  ```
  - `test_save_syncs_directory_after_replace`. The directory already exists. After `Store(path).save()`, `events == [("fsync", path.stat().st_ino), ("replace",), ("fsync", path.parent.stat().st_ino)]`.
  - `test_first_save_syncs_new_directory_entries_before_writing`. `path = Path(tmp) / "new" / "airloom" / "config.json"`. Then `events == [("fsync", ino(tmp)), ("fsync", ino(tmp/"new")), ("fsync", path.stat().st_ino), ("replace",), ("fsync", ino(tmp/"new"/"airloom"))]`.
  - `test_directory_fsync_failure_after_replace_keeps_new_settings`. The fsync wrapper raises `OSError(errno.EIO, "I/O error")` when `stat.S_ISDIR(os.fstat(fd).st_mode)` and `("replace",) in events`. `settings.apply({**settings.data, "location_name": "Changed"})` raises `store.SettingsDurabilityError`, and the following hold:
    - `isinstance(exc, OSError)`;
    - `exc.saved["location_name"] == "Changed"`;
    - `settings.data == exc.saved`;
    - `Store(path).data == exc.saved`;
    - `list(Path(tmp).iterdir()) == [path]`, so no temp file is left behind;
    - `"saved" in str(exc)`.

    The same check applies to `settings.toggle_favorite(5)`: it raises, and `5 in settings.data["favorites"]`.
  - `test_new_directory_sync_failure_is_a_plain_prewrite_error`. On first creation, the wrapper raises on the first directory fsync. Then the call raises `OSError` that is not a `SettingsDurabilityError`, `settings.data` is unchanged, and `path.exists()` is False.
  - Existing `test_save_fsync_failure_preserves_disk_and_cleans_temp` and `test_apply_permission_failure_preserves_memory_disk_and_cleans_temp` stay unmodified and must pass.
  - Add `import errno, stat` to the imports.
- [ ] **Step 2: Run it and confirm it fails**
  `python3 -m unittest tests.test_store.DurabilityTest -v`. Expect `AssertionError: Lists differ` (the trailing directory fsync is missing) and `AttributeError: module 'airloom.store' has no attribute 'SettingsDurabilityError'`.
- [ ] **Step 3: Implement** as specified. In `apply`, catch `SettingsDurabilityError` from `_write`, set `self.data = clean`, and re-raise.
- [ ] **Step 4: Run and confirm it passes**
  `python3 -W error::ResourceWarning -m unittest tests.test_store -v`
- [ ] **Step 5: Commit**
  `git add airloom/store.py tests/test_store.py && git commit -m "fix: fsync the config directory after replacing settings and on first creation"`

### Task 8: Demo sensors carry demo provenance (Minor: demo origin)

**Files:**
- Modify `airloom/demo.py:54-68`
- Test: `tests/test_demo.py`

**Interfaces:** `demo_sensors(...)` returns sensors with `origin="demo"`. `delivery` keeps its default of `"network"`, which is what `app.py` already assigns.

- [ ] **Step 1: Write the failing test**
  `test_generated_sensors_are_labeled_demo`. `sensors = demo_sensors(45.5, -122.6)` gives `{s.origin for s in sensors} == {"demo"}` and `{s.to_dict()["origin"] for s in sensors} == {"demo"}`. Also `presentation.alert_transitions(sensors, favorites={s.sensor_id for s in sensors}, previous={}, threshold=1, now=time.time(), origin="live") == ([], {})`. The per-sensor demo label alone must block alerts even when the batch origin says `"live"`.
- [ ] **Step 2: Run it and confirm it fails**
  `python3 -m unittest tests.test_demo -v`. Expect `AssertionError: Items in the first set but not the second: 'live'`.
- [ ] **Step 3: Implement** by passing `origin="demo"` in the `Sensor(...)` constructor.
- [ ] **Step 4: Run and confirm it passes**
  `python3 -m unittest tests.test_demo tests.test_app_requests -v`
- [ ] **Step 5: Commit**
  `git add airloom/demo.py tests/test_demo.py && git commit -m "fix: label generated demo sensors with demo provenance"`

### Task 9: Bounded, coalesced reverse-geocoding cache (Minor: reverse geocoding cache and coalescing)

**Files:**
- Modify `airloom/geocode.py`: `Geocoder.__init__` (:48-57), `reverse` (:86-109), `_cached`/`_store_cached` (:111-129)
- Test: `tests/test_geocode.py` (`GeocodeTest.setUp`, new class `ReverseCacheTest`)

**Interfaces:**
- `Geocoder` gains `self._reverse_cache: OrderedDict` (guarded by the existing `_cache_lock`) and `self._reverse_lock: threading.Lock` (the coalescing lock, the same pattern as `_search_lock`).
- `_cached(self, key, cache: OrderedDict)` and `_store_cached(self, key, value, cache: OrderedDict)` take the target cache. Search keeps returning a fresh `list` and storing a `tuple`.
- Cache key: `(self.reverse_url, f"{latitude:.6f}", f"{longitude:.6f}", "14")`. These are exactly the endpoint and the `lat`/`lon`/`zoom` query values sent, so the request params are built from the same strings.
- Same `_CACHE_TTL` (300 s) and `_CACHE_MAXSIZE` (64) as search.
- Cache the label only when the payload is a `dict`, whether the label comes from `address` or `name` or is the coordinate fallback for an empty result. `GeocodeError`, any other exception (including adapter-raised cancellation from W2's wrappers), and non-dict payloads are never cached.
- `reverse` still calls `self._throttle()` and `self._request(...)` only on a miss, inside `_reverse_lock` after re-checking the cache. W2's admission wrappers keep working.

- [ ] **Step 1: Write the failing test**
  - In `GeocodeTest.setUp`, add `geocode._default_geocoder = None` and `self.addCleanup(setattr, geocode, "_default_geocoder", None)`. Otherwise the module client's new reverse cache would leak between `test_reverse_falls_back_to_coordinates` and `test_reverse_prefers_smallest_locality`, which share coordinates.
  - `ReverseCacheTest` (uses `make_geocoder()`):
    - `test_repeated_reverse_uses_cache_without_request_or_throttle`. Two `client.reverse(45, -122)` calls give `"Stubville"` (payload `{"address": {"city": "Stubville"}}`), `spy.call_count == 1`, and `timing.sleeps == []`. This is the review's M2 reproduction (today `call_count == 2`).
    - `test_reverse_key_is_the_rounded_query_sent`. `reverse(45.0000001, -122)` and `reverse(45.0000004, -122)` share one request. `reverse(45.000001, -122)` makes a second request.
    - `test_reverse_key_includes_endpoint`. After the first call, setting `client.reverse_url = "https://other.example.test/reverse"` makes the next identical call request that URL.
    - `test_reverse_cache_expires_and_is_bounded`. `timing.advance(301)` causes a refetch. 65 distinct latitudes, then the first again, gives `call_count == 66`.
    - `test_reverse_failures_and_malformed_payloads_are_not_cached`. With `side_effect=[OSError("boom"), fake_response(payload)]`, the first call raises `GeocodeError`, the second returns the label, and there are 2 calls. With `side_effect=[fake_response([]), fake_response(payload)]`, the first returns `"45.00, -122.00"`, the second returns the label, and there are 2 calls.
    - `test_concurrent_identical_reverse_misses_share_one_request`. Mirror `test_concurrent_identical_cache_misses_share_one_request`, using `client._reverse_lock = EntrySignalingLock(client._reverse_lock)`. Results `["Shared", "Shared"]`, with `len(calls) == 1`.
    - `test_failed_reverse_leader_unblocks_waiter_for_retry_and_later_cache`. Mirror the search version: the first caller gets `GeocodeError`, the waiter gets the label, a third call is served from cache, and `len(calls) == 2`.
- [ ] **Step 2: Run it and confirm it fails**
  `python3 -m unittest tests.test_geocode.ReverseCacheTest -v`. Expect `AssertionError: 2 != 1` and `AttributeError: 'Geocoder' object has no attribute '_reverse_lock'`.
- [ ] **Step 3: Implement** as specified. Refactor the label extraction into `_label_from(payload: dict) -> str | None` so `reverse` can decide whether to cache.
- [ ] **Step 4: Run and confirm it passes**
  `python3 -m unittest tests.test_geocode tests.test_app_requests -v`
- [ ] **Step 5: Commit**
  `git add airloom/geocode.py tests/test_geocode.py && git commit -m "fix: cache and coalesce identical reverse-geocoding requests"`

### Task 10: Workstream verification

**Files:** none modified.

- [ ] **Step 1: Run the full gate.** `make check PYTHON="python3 -W error::ResourceWarning"` must pass with 0 failures, 0 errors, and no ResourceWarning.
- [ ] **Step 2: Run the hosted test-job reproduction** (no PyGObject, Python 3.12), because W1 adds stdlib-only tests that must not import `gi`:
  `docker run --rm -v "$PWD":/src:ro -w /src ubuntu:24.04 bash -c 'apt-get update -qq && apt-get install -y -qq python3 >/dev/null && cp -r /src /tmp/w && cd /tmp/w && python3 -W error::ResourceWarning -m unittest discover -s tests'`. If Docker is unavailable, use `podman` with the same arguments and say so in the report.
- [ ] **Step 3: Run the live UI scripts.** W1 changes no UI code, but the parser and demo provenance feed every scenario. Run as a non-root user under a private headless mutter, the same invocation `scripts/make-screenshots` uses. mutter does not propagate the child's exit status, so the command writes it to a file:
  `rm -f /tmp/w1-ui.status; GDK_DEBUG=no-portals NO_AT_BRIDGE=1 WAYLAND_DISPLAY=w1-ui dbus-run-session -- mutter --headless --wayland --no-x11 --virtual-monitor 1920x1080 --wayland-display w1-ui -- bash -c 'python3 scripts/test-ui-regression && python3 scripts/test-heatmap && python3 scripts/test-save-refresh; echo $? > /tmp/w1-ui.status'; cat /tmp/w1-ui.status`
  The status must be `0`. A W1-caused failure here is in scope. Report a pre-existing flake with its output rather than retrying it away.
- [ ] **Step 4: Write the report.** List any edit to a test file that isn't W1-owned (expected: none). List the fixture changes in `tests/test_purpleair.py` (Task 4) with the reason for each.
- [ ] **Step 5: Commit.** Nothing to commit unless verification required a fix. A fix gets its own task-style test-first commit.

---

## Cross-workstream contracts

1. **Freshness rule (W1 → W2, W3).**
   - `airloom/models.py` defines:
     - `MAX_OBSERVATION_AGE = 7200`
     - `FUTURE_SKEW_TOLERANCE = 600`
     - `plausible_observation_time(value: int | None, now: float) -> int | None`
     - `is_fresh_observation(last_seen: int | None, now: float) -> bool`
   - The rule: `last_seen is not None and -600 <= now - last_seen <= 7200`. Units are Unix seconds and the clock is wall-clock (`time.time()` / `Date.now() / 1000`).
   - **W2:** alert eligibility must go through `presentation.alert_transitions` (whose guard W1 owns) or call `is_fresh_observation(sensor.last_seen, now)` directly, with `now = controller._wall_now()`. W2's alert-delivery restructuring must not reintroduce its own age arithmetic, and must not edit the guard lines.
   - **W3:** the "Old" badge mirrors the predicate in `core.js` as `AirloomCore.isFreshObservation(lastSeen, nowSeconds)` with constants `7200` and `600`, both bounds inclusive, and `null`, `undefined`, or non-finite values not fresh. W3's JS test pins these shared boundary vectors, the same ones as `ObservationFreshnessTest`: `now - lastSeen` of `0, 7200, -600` is fresh; `7201, -601, null` is not.
   - Badge "Old" = `origin !== "demo" && !isFreshObservation(sensor.last_seen, Date.now() / 1000)`. A missing `last_seen` is therefore Old, which matches alert ineligibility. W3 owns badge precedence (Demo > Stale > Old).
2. **What reaches the page (W1 → W3).**
   - `Sensor.to_dict()` (via `controller.sensor_payload`) carries:
     - `last_seen`: int seconds or `null`. After W1 it is never a boolean, and never more than 600 s ahead of the parsing clock, because a future value becomes `null`.
     - `origin`: `"live"` or `"demo"`. Demo sensors now originate as `"demo"` too.
     - `delivery`: `"network"` or `"cache"`.
     - `remote_favorite`.
   - Batch-level `stale` stays in the `sensors` event (W2-owned). W1 adds no payload fields.
3. **Settings durability (W1 → W2).**
   - `airloom.store.SettingsDurabilityError(OSError)` has `.saved: dict`. It is raised by `Store.apply` and every mutator only after the rename, and only after `Store.data == .saved`.
   - **W2:** `ApplicationController._save`, or each caller of it, must catch this subclass before its `except OSError` handlers. It must treat the save as committed: adopt `exc.saved`, run `_commit`, and return success (`SettingsResult("success", ...)` / `ChangeResult(True, ...)`). It must surface `str(exc)` through the existing `error` event (toast) as a warning, not as "Could not save …".
   - Until W2 lands this, the subclass falls into today's `except OSError` path, which surfaces it as a save error. The failure is visible, but its wording is wrong. The W2 test should drive a real `Store` whose directory fsync is patched to fail after the rename (see `DurabilityTest` in `tests/test_store.py` for the fault-injection recorder).
4. **Geocoder (W1 → W2).**
   - `Geocoder.reverse` serves cache hits without calling `_throttle` or `_request`. Misses run inside `_reverse_lock` and then call `self._throttle()` and `self._request(url, params, timeout)`, exactly as before.
   - Any exception from those calls, including adapter-raised cancellation, propagates and is not cached. W2's reverse-label admission checks can wrap the same two points that `_install_place_search_admission` wraps.
5. **PurpleAir client (W1 → W2).**
   - `PurpleAirClient(api_key, timeout=20, clock=time.time)`.
   - `sensor_from_values(values, now=None)` defaults to `time.time()`.
   - `app.py`'s `_new_purpleair_client` and `_sensors_from_rows` need no change.
   - Malformed responses now raise `PurpleAirError` from `fetch_rows`, which `app._fetch_readings` already maps to stale-cache or demo fallback.
   - `debugport.FakePurpleAirClient` (W2) returns `FetchResult` directly and bypasses `parse_rows`. `cache._upsert_locked`'s future-time, expiry, and retained-measurement guards still apply to its rows.

## Self-review

- **Spec coverage:**
  - Finding 1 → Task 4 (response validation, whole-response rejection, untouched checkpoint and cache, retained-measurement guard, parser→cache→alert and malformed-full-snapshot regressions).
  - Finding 2 → Task 2 (measurements, humidity neutral fallback, temperature, coordinates, confidence, `last_seen`, `location_type`, `time_stamp`, trend windows; direct and cached paths).
  - Finding 3 → Task 1 (shared rule, alert boundary) and Task 3 (injected clock, observation and checkpoint rejection, ms-as-seconds, legacy future rows).
  - Finding 7 → Task 5. Finding 9 → Task 6. Finding 13 → Task 7.
  - Minor demo origin → Task 8. Minor reverse geocoding → Task 9. Verification → Task 10.
- **Type consistency:**
  - `last_seen`, `time_stamp`, and `value` are `int | None` in seconds; `now` is `float` seconds throughout.
  - `parse_rows(payload, requested, *, now)` gets its parameters in Task 3 and its validation in Task 4.
  - `SettingsDurabilityError` subclasses `OSError`, so existing `except OSError` paths still catch it.
  - `MEASUREMENT_FIELDS` is a `frozenset[str]` used by both `parse_rows` and `_upsert_locked`.
- **Review Focus coverage:**
  1. Milliseconds → Task 3.
  2. Empty or garbled full snapshot → Task 4.
  3. Legacy future rows → Task 3.
  4. First-run directory and post-rename fsync failure → Task 7.
  5. Failed reverse leader with a waiter → Task 9.
