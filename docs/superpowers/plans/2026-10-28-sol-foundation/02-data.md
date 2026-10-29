# Data Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct scientific conversion, persistence, cache consistency/freshness, and geocoder behavior while preserving dependency-free APIs.

**Architecture:** Validate at ingestion, use explicit CF1/ATM correction paths, make storage transactional and owner-closed, and separate observation freshness from poll freshness.

**Tech Stack:** Python stdlib, SQLite, unittest.

**Spec:** `docs/superpowers/specs/2026-10-28-sol-foundation-rearchitecture-design.md`

## Global Constraints

Consume foundation `geography.py` and provenance fields; no new dependencies; preserve valid user settings; no live network in tests.

## Review Focus

Huge finite values, malformed Unicode favorites, preexisting symlinks, locked healthy DBs, failed unknown-ID backfills, repeated empty deltas, and antimeridian queries must not destroy data or falsely advance coverage.

---

### Task 1: Correction model and hostile ingestion

**Files:** Modify `airloom/aqi.py`, `purpleair.py`, `demo.py`; tests `test_aqi.py`, `test_purpleair.py`, `test_demo.py`; consume the provenance fields already established in `models.py` by the foundation plan.

**Interfaces:**
- `epa_corrected_pm25(value, humidity, basis="CF1") -> float | None`; basis is `CF1` or `ATM`.
- `CORRECTION_VERSION = 2`; trend requests include `location_type`; cache schema bumps in Task 3/4.

- [x] Add independently calculated failing fixtures for CF1 <570, 570–611 blend, ≥611; all five ATM branches; indoor/outdoor equivalent trends; `1e30`, `1e308`, NaN/Infinity, fractional IDs, invalid coordinates; demo `Now == aqi`.
- [x] Run the three focused modules and confirm failures reproduce InvalidOperation/infinite-to-Good/wrong outdoor trends.
- [x] Implement finite-safe correction/truncation/clamping, strict sensor ID/coordinate validation, basis selection, location_type trend fields, and demo endpoint consistency. Document historical humidity approximation and sources in code.
- [x] Re-run focused modules; expect PASS and no crash/AQI 0 for invalid measurements.
- [x] Commit `fix: version and correct sensor conversion paths`.

### Task 2: Transactional private settings

**Files:** Modify `airloom/store.py`, `tests/test_store.py`.

**Interfaces:**
- `sensor_id(value) -> int | None` shared validator.
- `Store.apply(candidate: dict) -> dict` sanitizes/persists before replacing `self.data` and returns the sanitized saved snapshot.
- `interface_scale` defaults to `1.0`, accepts only `1.0`, `1.25`, or `1.5`, and appears in public config.
- Existing favorite/hide methods become transactional on save failure.

- [x] Add failing file-based tests for NaN/Infinity/`"²"` favorites, permissive temp, temp symlink, overlapping saves, failed-save rollback, mode 0600, valid-entry survival, and interface-scale defaults/validation.
- [x] Run `python3 -m unittest tests.test_store -v`; reproduce startup exceptions and permission failures.
- [x] Implement shared validation, the three interface-scale values, `mkstemp` + 0600 + flush/fsync + atomic replace + cleanup, per-store save lock, deep-candidate apply, and rollback for mutators.
- [x] Re-run focused tests; expect PASS with no temp/symlink damage.
- [x] Commit `fix: make settings validation and saves transactional`.

### Task 3: Safe SQLite ownership and recovery

**Files:** Modify `airloom/cache.py`, `tests/test_cache.py`, `airloom/app.py` only if controller workstream has not started; otherwise expose close and let controller wire shutdown.

**Interfaces:** `SensorCache.close() -> None`, context manager, `CacheError(RuntimeError)` for non-corruption open failures.

- [x] Add failing tests for idempotent close, constructor failure cleanup, forced-gc ResourceWarnings, locked healthy DB inode/data preservation, and actual corruption-only recreation.
- [x] Run focused tests; reproduce deletion/warnings.
- [x] Restrict recovery to SQLITE_CORRUPT/SQLITE_NOTADB evidence, close failed connections, add lock-protected close/context management, and close every test fixture/reopen.
- [x] Run with `-W error::ResourceWarning`; expect PASS.
- [x] Commit `fix: make cache recovery and ownership safe`.

### Task 4: Transactional coverage and observation freshness

**Files:** Modify `airloom/cache.py`, `purpleair.py` only for geography imports, `tests/test_cache.py`, `test_purpleair.py`.

**Interfaces:**
- `METADATA_MAX_AGE = 24 * 3600` and `SCHEMA_VERSION = 2` (clearing readings/trends derived by correction v1).
- `fetch_area(...)` splits crossing bounds via `split_bounds`, dedupes by sensor ID, and only commits checkpoint/freshness after required backfill succeeds.

- [x] Add failing sequence tests for failed backfill then unchanged retry, prune/revisit, repeated empty deltas >24h, rename/relocation/reclassification, overlapping regions, locked commit rollback, and crossing-bound queries.
- [x] Run focused tests; confirm premature checkpoint/stale observations/obsolete metadata.
- [x] Bump the schema, stage backfills before one rows+region transaction, prune on all fetch paths using observation `last_seen` rather than rejuvenating it with retrieval time, invalidate regions when rows are pruned, force full snapshots beyond metadata TTL, and merge split queries without global absent-row deletion.
- [x] Re-run focused and full Python tests; expect PASS.
- [x] Commit `fix: preserve cache coverage and observation freshness`.

### Task 5: Explicit-submit geocoder foundation

**Files:** Modify `airloom/geocode.py`, `tests/test_geocode.py`.

**Interfaces:**
- `Geocoder(search_url=None, reverse_url=None, clock=..., sleep=...)` with `search(query, limit=5, timeout=10)` and `reverse(...)`; module functions retain compatibility.
- `AIRLOOM_NOMINATIM_URL` selects a compatible base URL; bounded TTL query cache and finite coordinate validation are required.

- [x] Add failing tests for NaN/Infinity/out-of-range places, endpoint override, repeated-query cache, empty queries, throttling, and failed-request non-caching.
- [x] Run `python3 -m unittest tests.test_geocode -v`; confirm failures.
- [x] Implement endpoint normalization/override, bounded cache, validation, and injectable timing without changing the explicit-submit UI contract owned by frontend.
- [x] Re-run focused tests; expect PASS and zero requests for empty queries.
- [x] Commit `fix: make geocoding validated cacheable and configurable`; run `make check` and request Sol data-workstream review.
