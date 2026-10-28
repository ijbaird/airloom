# Foundation Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish shared geography, request-identity, provenance/presentation, and dependency-free JavaScript test contracts.

**Architecture:** Move geographic primitives into a GTK-free module re-exported for compatibility, add pure Python request/presentation helpers, and expose browser/Node-compatible JavaScript primitives without a build step.

**Tech Stack:** Python stdlib, vanilla JavaScript, Node `node:test`.

**Spec:** `docs/superpowers/specs/2026-10-28-sol-foundation-rearchitecture-design.md`

## Global Constraints

No new dependencies; preserve existing imports and direct file preview; all work is test-first and committed per task.

## Review Focus

Crossing bounds at ±180°, non-finite IDs/coordinates, superseded request tokens, and remote favorites in summaries each require hostile-input tests below.

---

### Task 1: Canonical geography and frontend math

**Files:**
- Create: `airloom/geography.py`, `tests/test_geography.py`, `airloom/resources/core.js`, `tests/js/core.test.js`
- Modify: `airloom/purpleair.py` (re-export compatibility), `airloom/resources/index.html` only when frontend integrates

**Interfaces:**
- Produces: `Bounds(north, west, south, east)`, `bounds_around(...)`, `bounds_contains(outer, inner)`, `split_bounds(bounds) -> tuple[Bounds, ...]`, `nearest_longitude(longitude, reference) -> float`.
- Produces JS `AirloomCore.splitBounds`, `nearestLongitude`, `bandForAQI`, `summarySensors(sensors, view)` with UMD-style browser/`module.exports` exposure.

- [x] Write failing Python tests for 179/-179 splitting, containment on both sides, bounds_around wrapping, nearest-world ±180 projection, finite/range validation; write Node tests for the same math, all six AQI boundaries, and exclusion of remote favorites from summaries.
- [x] Run `python3 -m unittest tests.test_geography -v` and `node --test tests/js/core.test.js`; expect missing-module/export failures.
- [x] Move/re-export geographic primitives, implement split/nearest helpers, and implement pure JS math using the six existing `aqi.py` bands. `summarySensors` accepts an explicit `isRemoteFavorite` flag and never guesses from distance.
- [x] Re-run both focused suites and existing `tests.test_purpleair`; expect PASS.
- [x] Commit `feat: add canonical geography and frontend math contracts`.

### Task 2: Request identity registry

**Files:** Create `airloom/requests.py`, `tests/test_requests.py`.

**Interfaces:**
- `RequestToken(request_id: int, context: str, generation: int)` frozen dataclass.
- `RequestRegistry.begin(context: str, generation: int = 0) -> RequestToken`.
- `is_current(token) -> bool`, `finish(token) -> None`, `invalidate(context: str | None = None) -> None`.

- [ ] Write failing tests for monotonically increasing IDs, same-context supersession, independent contexts, stale finish, generation mismatch, and targeted/global invalidation.
- [ ] Run `python3 -m unittest tests.test_requests -v`; expect import failure.
- [ ] Implement the thread-safe registry with no GTK or clock dependency; only the newest live token for a context/generation is current.
- [ ] Re-run focused tests; expect PASS.
- [ ] Commit `feat: add request identity registry`.

### Task 3: Presentation and alert decisions

**Files:** Create `airloom/presentation.py`, `tests/test_presentation.py`; modify `airloom/models.py` only for provenance fields needed by all workstreams.

**Interfaces:**
- `visible_sensors(sensors, *, hidden_ids, confidence_filter, location_filter) -> list[Sensor]`.
- `alert_transitions(sensors, *, favorites, previous: dict[str, bool], threshold: int, now: float, origin: str) -> tuple[list[Sensor], dict[str, bool]]`.
- Sensor provenance fields: `origin: str = "live"`, `delivery: str = "network"`; serialized to JS.
- `MAX_ALERT_OBSERVATION_AGE = 2 * 3600`.

- [ ] Write failing tests proving cached/network/favorite paths obey hidden/confidence/location filters, demo and >2h observations neither alert nor mutate state, fresh real crossings alert once, and origin/delivery serialize.
- [ ] Run `python3 -m unittest tests.test_presentation tests.test_models -v`; expect import/attribute failures.
- [ ] Implement pure filtering and threshold transitions using `last_seen`; do not infer freshness from retrieval time.
- [ ] Re-run focused tests; expect PASS.
- [ ] Commit `feat: add provenance-aware presentation decisions`.

### Task 4: Wire dependency-free checks

**Files:** Modify `Makefile`; create `tests/test_build.py`; leave CI workflow changes to the release plan.

**Interfaces:** `make check` runs Python unittest discovery, compileall, `node --check` for every JS source, and `node --test tests/js/*.test.js`.

- [ ] Write a failing `BuildCheckTest` that runs `make -n check` and asserts the dry-run output contains `node --test`; run `python3 -m unittest tests.test_build -v` and confirm the assertion fails.
- [ ] Run `make check`; note that the new Node suite is not invoked.
- [ ] Add `check-js` and include it in `check` without adding package installation.
- [ ] Run `make check`; expect all existing and new tests PASS.
- [ ] Commit `build: include pure JavaScript tests in checks` and request Sol review of the complete foundation diff.
