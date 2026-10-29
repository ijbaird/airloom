# Application Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move refresh, provenance, alert, request, settings, and lifecycle decisions into deterministic GTK-free orchestration and port the GTK adapter to it.

**Architecture:** A pure controller authorizes completions while `app.py` owns actual threads/GLib/GTK. Shared presentation and request registries prevent stale state, and store/cache APIs provide transactional persistence and cleanup.

**Tech Stack:** Python stdlib unittest, system PyGObject.

**Spec:** `docs/superpowers/specs/2026-10-28-sol-foundation-rearchitecture-design.md`

## Global Constraints

Consume data/foundation interfaces without editing their owned files; no new dependencies; GTK/WebKit only on main loop; preserve bridge compatibility except versioned request/trend fields.

## Review Focus

Slow A before B, config changes during fetch, failed persistence during pending drain, delayed home labels/location fixes, duplicate trends, and shutdown with live workers must preserve newest intent and consistent state.

---

### Task 1: Pure refresh controller

**Files:** Create `airloom/controller.py`, `tests/test_controller.py`.

**Interfaces:**
- `RefreshRequest(request_id, generation, bounds, center, force, favorite_ids)` frozen dataclass.
- `RefreshDecision(publish: bool, sensors: tuple, trend_ids: tuple[int, ...])` frozen dataclass.
- `ApplicationController(config: dict)` with `set_view(view: dict)`, `set_config(config: dict)`, `begin_refresh(force=False)`, `complete_refresh(request, sensors, origin, delivery)`.

- [x] Write failing tests for current-view manual/auto refresh, home fallback before view, A superseded by B/config/filter, cached superseded results not publishing or starting trends, and latest B publishing once.
- [x] Run `python3 -m unittest tests.test_controller -v`; expect import failure.
- [x] Implement generation-based newest-request authorization; cache writes remain adapter-side and do not require `publish=True`.
- [x] Re-run focused tests; expect PASS.
- [x] Commit `feat: add authoritative refresh controller`.

### Task 2: Port sensor state, filters, summaries, and alerts

**Files:** Modify `airloom/app.py`, `tests/test_controller.py`, create `tests/test_app_state.py` for GTK-free adapter helpers.

**Interfaces:** Consumes `visible_sensors`, `alert_transitions`, controller refresh methods, and data `fetch_area`/`fetch_favorites`; JS payload includes origin, delivery, coverage, and area/favorite collections.

- [x] Add failing tests for cached startup/stale fallback/opposite favorite filters, antimeridian view acceptance/split fetching, CacheError nonpersistent degradation, remote favorite summary exclusion inputs, demo/stale alert suppression, fresh real alert once, alert-save failure finalization, and capped coverage metadata.
- [x] Run focused tests; reproduce current mixed-state behavior.
- [x] Port `_on_view_changed`, `_start_fetch`, `_finish_refresh`, `_paint_cached_home`, `_send_sensor_state`, and `_check_alerts` to geography/controller/presentation decisions. Keep favorite monitoring separate; route both Refresh controls through current-view `begin_refresh`; surface cache-open failure and continue without persistence; drain pending work in `finally`.
- [x] Run focused tests and `python3 -m unittest discover -s tests -v`; expect PASS.
- [x] Commit `fix: authorize sensor publication and alert eligibility`.

### Task 3: Coordinated trends, search, and labels

**Files:** Modify `airloom/app.py`, `airloom/location.py`, `tests/test_location.py`, create `tests/test_app_requests.py`.

**Interfaces:** Bridge `place-search` accepts `{request_id, context, query}` and `places` returns the same; trend events include `sensor_id`, `status`, `trend`; `GeoClueLocator.cancel()` cancels its `Gio.Cancellable`.

- [ ] Write failing tests for duplicate in-flight trends, terminal trend error/unavailable, obsolete search/label responses, home A/B reversal, auto→fixed cancellation, deferred-start mode recheck, and dispatcher exception replies for non-string commands.
- [ ] Run focused tests; confirm current missing identity/cancellation/failure behavior.
- [ ] Integrate `RequestRegistry`, single-flight trend state, contextual place search with obsolete queued requests dropped before network I/O, coordinate/home-generation labels, real location cancellation, and safe async debug error replies.
- [ ] Re-run focused tests; expect PASS.
- [ ] Commit `fix: coordinate asynchronous application requests`.

### Task 4: Transactional UI actions and lifecycle

**Files:** Modify `airloom/app.py`, `tests/test_app_state.py`, `tests/test_app_requests.py`.

**Interfaces:** Consumes `Store.apply`, transactional favorite/hide methods, `SensorCache.close`, and safe `DebugPort.start/stop` from their owning plans.

- [ ] Write failing tests for PermissionError/ENOSPC settings/favorite/hide/alert saves, no false config publication, continued pending refresh, rejected new work after shutdown, and one cache/debug/location/timer cleanup.
- [ ] Run focused tests; reproduce inconsistent/false-success behavior.
- [ ] Persist candidates before publishing, acknowledge success/failure to JS, keep Preferences open on failure, make alert-state save nonfatal, and implement idempotent shutdown/draining.
- [ ] Run full Python suite with ResourceWarnings as errors; expect PASS.
- [ ] Commit `fix: make UI persistence and shutdown transactional`.

### Task 5: Shell trust and failure recovery

**Files:** Modify `airloom/app.py`, `tests/test_app_state.py`; update `index.html` only for reload/status hooks if frontend has landed, otherwise define the contract for integration.

- [ ] Add failing policy/lifecycle tests for unrelated `file://` rejection, bundled document allowance, approved external links, load failure, web-process termination recovery, persistent interface scale, and map-pinch independence.
- [ ] Run focused tests; confirm unrestricted file navigation/missing recovery.
- [ ] Restrict navigation, add explicit error/reload handling, and replace forced zoom 1.0 with persisted interface-scale handling that does not interfere with native map pinch.
- [ ] Run `make check`; expect PASS.
- [ ] Commit `fix: harden WebKit navigation and recovery`; request Sol controller-workstream review.
