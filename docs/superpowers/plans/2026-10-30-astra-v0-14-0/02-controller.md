# W2: Controller and Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix W2's findings 4, 8, 10, 11, and 12, plus two Minors (strict settings payload validation and reverse-label workers), in `controller.py`, `app.py`, and W2's handshake lines in `app.js`, each with a regression that fails on `f1f811d`. Also adopt W1's settings-durability contract (finding 13's consequence).
**Architecture:** `controller.py` gains the decisions: sticky force intent, alert delivery bookkeeping, a credential generation, cached-startup tokens, and the restorable view. `app.py` acts on those decisions. All SQLite access moves to tracked workers, either count-bounded or coalescing, and results come back through `GLib.idle_add`. Shell recovery adds one Python→JS event, `restore-view`, which is sent after `config` on `ready`, before the refresh.
**Tech Stack:** Python 3 stdlib `unittest`, system PyGObject (`tests/gi_stub.py` when it's absent), SQLite, vanilla JS. Live checks use `scripts/debug-run` and `scripts/debug-client` under Xvfb.
**Spec:** docs/superpowers/specs/2026-10-30-astra-review-v0-14-0-design.md

Worktree: branch `astra-controller-v0-14-0`, created from `astra-v0-14-0`. Every command below runs from the worktree root.

## Global Constraints

- Python stdlib plus system PyGObject only. Vanilla JS/CSS/HTML with no bundler. Helpers stay UMD/namespaced so direct-file preview works.
- GTK and WebKit calls only on the main loop. Workers deliver via `GLib.idle_add`. New blocking I/O must not run on the main loop.
- No live PurpleAir, Nominatim, or OSM calls in tests. Fixtures and the existing debug-only seams only.
- `make check` passes with ResourceWarnings as errors. JS tests run on Node 22. The hosted Python 3.12 job runs without PyGObject.
- Anti-goal: any change that lets demo, cached, stale, expired, or malformed data look like a fresh live reading, or become alert-eligible.
- Anti-goal: any new path that spends PurpleAir credits without a key the user currently has configured, or that multiplies billed requests.
- Anti-goal: weakening an existing assertion or gate to make a fix pass. Where this plan changes an existing test's choreography because a contract changed, the task names the test and keeps or strengthens every assertion.
- Non-goal: exactly-once notification delivery across process crashes; libnotify can't guarantee it.
- Settings durability (controller's call): if the directory fsync fails after a successful rename, the new settings are already visible and stay applied, with no rollback. The failure is reported as a durability warning through the existing error/toast path.
- Ownership: W2 owns `controller.py`, `requests.py`, the non-color parts of `presentation.py`, `app.py`, `bridge.py`, `debugport.py`, `__main__.py`, and only the shell-recovery receive and handshake lines in `app.js`. W1 owns the alert-freshness guard in `presentation.py` (`alert_transitions`'s guard lines, the module constant, and its import) and the freshness rule in `models.py`. W2 never edits those, and adds no age arithmetic of its own. W3 owns the rest of `app.js` and `scripts/test-ui-regression`. W2 edits tests only in `tests/test_controller.py`, `tests/test_app_requests.py`, and `tests/test_app_state.py`, and does not edit `tests/gi_stub.py` or `tests/record_handoff.py`.

## Review Focus

1. A Refresh click, followed 1.2 s later by the page's debounced `view-changed` for the unchanged view, while coverage is fresh. This is the most common real ordering. Pinned by Task 2, `test_identical_covered_view_keeps_manual_refresh`.
2. A notification daemon that's down for several refreshes while the favorite's reading ages past 2 h. A retry must never notify the stale reading. Pinned by Task 3, `test_failed_delivery_retries_each_evaluation_only_while_eligible`.
3. A key that's replaced rather than cleared, with an old-key trend queued. The old key must not reach PurpleAir. Pinned by Task 4, subtest `replace` of `test_key_change_revokes_queued_trend_before_paid_io`.
4. A cache database held by another process (a second Airloom instance, or a backup tool) during selection, trend completion, and startup, then shutdown while a trend write is queued. Pinned by Task 5, `test_cache_lock_never_blocks_the_main_loop` and the extended `test_process_exit_drains_every_worker_class_and_closes_cache_once`.
5. A renderer crash while a place search is in flight. The new page restarts its `request_id` counter at 1, so the old result would match a new search. Pinned by Task 7, `test_crash_away_from_home_restores_view_before_refresh`, which asserts that the old token is dead and that stale page messages are dropped.

---

### Task 1: Strict settings payload validation (Minor: strict settings payload validation)
**Files:** Modify `airloom/controller.py` (lines 747–810, `save_settings` and `_settings_updates`). Test: `tests/test_controller.py` (`PreferenceTransactionTest`) and `tests/test_app_requests.py` (`TransactionalActionTest`).
**Interfaces:** Produces module-private helpers in `controller.py`: `_finite_number(value, field: str) -> float`, `_whole_number(value, field: str) -> int`, and `_real_bool(value, field: str) -> bool`. Each raises `ValueError(f"{field} must be …")`. The `SettingsResult` shape is unchanged.

- [ ] **Step 1: Write the failing test**

In `PreferenceTransactionTest`:
```python
MALFORMED = (
    ("radius_km", "NaN"), ("radius_km", "Infinity"), ("radius_km", "-inf"), ("radius_km", True),
    ("radius_km", None), ("radius_km", []), ("heatmap_threshold_km", "NaN"),
    ("heatmap_threshold_km", True), ("alert_threshold", True), ("alert_threshold", "NaN"),
    ("alert_threshold", "120.5"), ("refresh_minutes", True), ("refresh_minutes", "inf"),
    ("home_lat", "NaN"), ("home_lon", True), ("confidence_filter", "false"),
    ("confidence_filter", 1), ("clear_api_key", "yes"), ("api_key", {"x": 1}),
    ("api_key", 123), ("api_key", ["k"]), ("location_name", {"x": 1}),
)

def test_malformed_settings_values_are_rejected_without_persisting(self):
    for field, value in self.MALFORMED:
        with self.subTest(field=field, value=value):
            h = Harness()
            before = deepcopy(h.config)
            result = h.controller.save_settings({**self.SETTINGS, field: value})
            self.assertEqual(result.status, "invalid")
            self.assertIn(field, result.error)
            self.assertEqual(h.config, before)
            self.assertEqual(h.saves, [])

def test_form_numeric_strings_still_save(self):
    h = Harness(home_mode="auto")
    result = h.controller.save_settings({
        **self.SETTINGS, "radius_km": "30", "heatmap_threshold_km": "50",
        "alert_threshold": "120", "refresh_minutes": "5", "home_lat": "10.5",
        "home_lon": "-20.25", "api_key": "  new-key  ", "clear_api_key": False,
        "confidence_filter": False, "interface_scale": 1,
    })
    self.assertEqual(result.status, "success")
    self.assertEqual(
        {k: h.config[k] for k in ("radius_km", "heatmap_threshold_km", "alert_threshold",
                                  "refresh_minutes", "latitude", "longitude", "api_key",
                                  "confidence_filter")},
        {"radius_km": 30.0, "heatmap_threshold_km": 50.0, "alert_threshold": 120,
         "refresh_minutes": 5, "latitude": 10.5, "longitude": -20.25,
         "api_key": "new-key", "confidence_filter": False},
    )
```
In `TransactionalActionTest`, add `test_malformed_settings_payload_gets_matching_error_result`. It sends `{**self.settings_message(), "radius_km": "NaN", "confidence_filter": "false"}` through `app._on_script_message(None, ScriptMessage({"action": "save-settings", ...}))` and asserts:
- exactly one `settings-save-result`, equal to `{"request_id": "settings-17", "status": "error", "error": mock.ANY}`;
- `app.store.saves == 0`;
- no `config` event;
- no `error` event.

- [ ] **Step 2: Run it and confirm it fails**

`python3 -m unittest tests.test_controller.PreferenceTransactionTest tests.test_app_requests.TransactionalActionTest -v`

Expected: the `radius_km "NaN"`, `confidence_filter "false"`, and `api_key {"x": 1}` subtests fail with `AssertionError: 'success' != 'invalid'`. The adapter test fails on `status: "success"`.

- [ ] **Step 3: Implement**

- `_finite_number(value, field)` rejects `bool`. It accepts `int`, `float`, or `str` via `float(value)`, and requires `math.isfinite`.
- `_whole_number(value, field)` takes `_finite_number(...)` and requires `number == int(number)`, then returns `int(number)`.
- `_real_bool(value, field)` requires `type(value) is bool`.
- In `_settings_updates`, apply these rules:
  - Clamp only the outputs of `_finite_number` for `radius_km` and `heatmap_threshold_km`.
  - Pass `alert_threshold` and `refresh_minutes` through `_whole_number`, keeping the existing clamp and the fallback to `2` for minutes.
  - Pass `home_lat` and `home_lon` through `_finite_number`, then the existing range check.
  - `confidence_filter` uses `_real_bool` when present. When it's absent, keep the committed value.
  - `location_name` must be `str` or `None`.
- Move API-key handling inside the validated `try` in `save_settings`:
  - `clear_api_key` uses `_real_bool` when present.
  - `api_key` must be `None` or `str`.
  - Missing keys still raise `KeyError`, so `test_invalid_settings_also_receive_matching_error_result` keeps its `radius_km` message.

- [ ] **Step 4: Run and confirm it passes**

`python3 -m unittest tests.test_controller tests.test_app_requests tests.test_app_state -v`

- [ ] **Step 5: Commit**

`git add airloom/controller.py tests/test_controller.py tests/test_app_requests.py && git commit -m "fix: reject malformed settings values instead of coercing them"`

---

### Task 2: Manual Refresh intent survives automatic and duplicate view reports (finding #11)
**Files:** Modify `airloom/controller.py`: `__init__` (line 202), `shutdown` (216), `begin_refresh` (280), `refresh` (301), `refresh_home` (311), `view_changed` (320–344), and `accept_refresh` (404). Test: `tests/test_controller.py` (`RefreshWorkflowTest`).
**Interfaces:**
- Produces `ApplicationController._forced_target: tuple[Bounds, tuple[float, float]] | None`.
- Produces `_target() -> tuple[Bounds, tuple[float, float]]`, which is the `(bounds, center)` that `begin_refresh` would use.
- Produces `_forced_outstanding(target) -> bool`.
- Public signatures are unchanged.

- [ ] **Step 1: Write the failing test**

Add these to `RefreshWorkflowTest`. Use `OTHER = {**VIEW, "north": 40.0, "south": 30.0, "lat": 35.0}`.
```python
def test_automatic_refresh_never_downgrades_queued_manual_refresh(self):
    h = Harness()
    h.controller.refresh()
    h.controller.refresh(force=True)
    self.assertIsNone(h.controller.refresh())
    self.assertTrue(h.controller.pending_fetch.force)
    self.assertTrue(h.controller.settle_refresh().force)

def test_automatic_refresh_coalesces_with_active_manual_refresh(self):
    h = Harness()
    manual = h.controller.refresh(force=True)
    self.assertIsNone(h.controller.refresh())
    self.assertIsNone(h.controller.pending_fetch)
    self.assertTrue(h.finish(manual, [reading(1, aqi=150)]).publish)
    self.assertEqual(len(h.started), 1)  # no second billed poll

def test_identical_covered_view_keeps_manual_refresh(self):
    h = Harness()
    h.controller.view_changed(VIEW)
    h.finish(h.started[-1], [reading(1, aqi=30)])
    forced = h.controller.refresh(force=True)
    self.assertIsNone(h.controller.view_changed(VIEW))
    decision = h.finish(forced, [reading(1, aqi=150)])
    self.assertTrue(decision.publish)
    self.assertEqual(h.controller.sensors[0].aqi, 150)
    self.assertFalse(h.controller.refresh().force)  # satisfied force is cleared

def test_new_target_supersedes_manual_refresh(self):
    h = Harness()
    h.controller.view_changed(VIEW)
    h.finish(h.started[-1], [reading(1)])
    forced = h.controller.refresh(force=True)
    self.assertEqual(h.controller.view_changed(OTHER), (35.0, -20.0))
    self.assertFalse(h.controller.accept_refresh(h.outcome(forced, [reading(2)])).publish)
    self.assertFalse(h.controller.settle_refresh().force)

def test_force_is_sticky_across_an_unforced_filter_refresh(self):
    h = Harness()
    h.controller.refresh(force=True)
    h.controller.set_location_filter("indoor")
    h.controller.refresh()
    self.assertTrue(h.controller.pending_fetch.force)
```
The existing `test_covered_fresh_view_discards_pending_without_fetching` and `test_supersession_queues_one_replacement_and_publishes_only_the_latest` stay unchanged and must keep passing.

- [ ] **Step 2: Run it and confirm it fails**

`python3 -m unittest tests.test_controller.RefreshWorkflowTest -v`

Expected failures:
- `pending_fetch.force` is `False`.
- `refresh()` returns a request instead of `None`.
- The identical covered view rejects the forced outcome: `publish` is `False`.

- [ ] **Step 3: Implement**

- `begin_refresh(force)` sets `force = bool(force) or self._forced_target == target`.
- `refresh(force)`:
  - If `self._forced_outstanding(self._target())`, return `None` without registering a token.
  - Otherwise, if `force`, set `self._forced_target = self._target()`, then admit as today.
- `_forced_outstanding(target)` is true when `pending_fetch` or `active_fetch` meets all of these:
  - its `force` is set;
  - its `generation == self._generation`;
  - its `(bounds, center) == target`;
  - its token is current: `self._requests.is_current(RequestToken(r.request_id, "refresh", r.generation))`.
- `view_changed`:
  - After parsing, if `(view, center) == self.current_view` and `self._forced_outstanding((view, center))`, return `None` before `begin_refresh`, leaving pending and active work untouched.
  - If `(view, center) != self._forced_target`, set `_forced_target = None`.
- `refresh_home` clears `_forced_target` when home differs from it.
- `accept_refresh` clears `_forced_target` when a published outcome has `outcome.request.force` and `(request.bounds, request.center) == _forced_target`.
- `shutdown` clears `_forced_target`.

- [ ] **Step 4: Run and confirm it passes**

`python3 -m unittest tests.test_controller tests.test_app_state.RefreshAdapterTest -v`

- [ ] **Step 5: Commit**

`git add airloom/controller.py tests/test_controller.py && git commit -m "fix: keep manual Refresh force until a forced fetch for its target completes"`

---

### Task 3: Alert delivery is acknowledged separately from observed state (finding #4)
**Files:**
- Modify `airloom/controller.py`: `__init__`, plus `evaluate_alerts` at lines 525–551.
- Modify `airloom/app.py`: `_check_alerts` and `_notify_alert`, at lines 1438–1457.
- Test `tests/test_controller.py` (`AlertWorkflowTest`) and `tests/test_app_state.py` (`AlertAdapterTest`).

**Interfaces:**
- Produces `ApplicationController.record_alert_delivery(sensor_id: int, delivered: bool) -> None`.
- Produces in-session `_alert_observed: dict[str, bool]` (the observed threshold state, which overlays persisted `alert_states`).
- Produces `_alert_delivery: dict[int, bool]`. `False` means pending. `True` means delivered this session, which is the in-session delivered ledger.
- Produces `AirloomApplication._notify_alert(sensor) -> bool`, which is `True` when submitted or captured.
- Consumes W1's freshness rule (`models.is_fresh_observation(last_seen, now)`, where fresh means `-600 <= now - last_seen <= 7200`, both inclusive, and `None` is not fresh) only through `presentation.alert_transitions(...)`, whose guard W1 routes through that predicate. The `now` is always `self._wall_now()`. W2 adds no age arithmetic. It doesn't import `is_fresh_observation` directly, because that name doesn't exist on W2's branch until W1 merges; `alert_transitions` is the W1-sanctioned entry point. See Cross-workstream contracts.
- `AlertDecision` is unchanged.

- [ ] **Step 1: Write the failing test**

In `AlertWorkflowTest`:
```python
def test_failed_delivery_retries_each_evaluation_only_while_eligible(self):
    h = Harness(favorites=[1], alert_states={})
    h.finish(h.controller.refresh(), [reading(1, aqi=150)])
    first = h.controller.evaluate_alerts()
    self.assertEqual([s.sensor_id for s in first.notify], [1])
    h.controller.record_alert_delivery(1, False)
    self.assertEqual([s.sensor_id for s in h.controller.evaluate_alerts().notify], [1])
    h.controller.record_alert_delivery(1, False)
    h.wall += 7201                                     # age 7261 s > 7200: not is_fresh_observation
    self.assertEqual(h.controller.evaluate_alerts().notify, ())
    h.finish(h.controller.refresh(), [reading(1, aqi=160, last_seen=int(h.wall) - 60)])
    retry = h.controller.evaluate_alerts()
    self.assertEqual([s.aqi for s in retry.notify], [160])
    h.controller.record_alert_delivery(1, True)
    self.assertEqual(h.controller.evaluate_alerts().notify, ())
    self.assertEqual(h.config["alert_states"], {"1": True})
    self.assertEqual(len(h.saves), 1)

def test_persistence_failure_never_renotifies_the_same_crossing(self):
    h = Harness(favorites=[1], alert_states={})
    h.finish(h.controller.refresh(), [reading(1, aqi=150)])
    h.failure = DENIED
    counts = []
    for _ in range(3):
        decision = h.controller.evaluate_alerts()
        counts.append(len(decision.notify))
        self.assertIn("Could not save alert state", decision.error)
        for sensor in decision.notify:
            h.controller.record_alert_delivery(sensor.sensor_id, True)
    self.assertEqual(counts, [1, 0, 0])
    self.assertEqual(h.config["alert_states"], {})
    h.failure = None
    self.assertEqual(h.controller.evaluate_alerts().notify, ())
    self.assertEqual(h.config["alert_states"], {"1": True})

def test_reset_crossing_alerts_again(self):
    h = Harness(favorites=[1], alert_states={})
    h.finish(h.controller.refresh(), [reading(1, aqi=150)])
    h.controller.evaluate_alerts(); h.controller.record_alert_delivery(1, True)
    h.finish(h.controller.refresh(), [reading(1, aqi=20)])
    self.assertEqual(h.controller.evaluate_alerts().notify, ())
    h.finish(h.controller.refresh(), [reading(1, aqi=150)])
    self.assertEqual([s.sensor_id for s in h.controller.evaluate_alerts().notify], [1])
```
Contract change to an existing test: in `test_only_fresh_real_favorite_crossings_notify_and_persist_once`, insert `h.controller.record_alert_delivery(1, True)` between `alerts = …` and `repeat = …`. All of its assertions stay.

In `AlertAdapterTest`, add `test_failed_notification_is_retried_then_settles`:
- `FakeStore(favorites=[1], alert_states={"1": False})`, sensor `make_sensor(1, favorite=True)`, `_area_ids = {1}`.
- Patch `controller_module.time.time` to `NOW`.
- Patch `app_module.Notify.Notification` so that `new.return_value.show.side_effect = [app_module.GLib.Error("daemon busy"), True]`.
- Call `app._check_alerts()` three times.
- Assert `show.call_count == 2` and `store.data["alert_states"] == {"1": True}`.

- [ ] **Step 2: Run it and confirm it fails**

`python3 -m unittest tests.test_controller.AlertWorkflowTest tests.test_app_state.AlertAdapterTest -v`

Expected:
- `AttributeError: … 'record_alert_delivery'`.
- The adapter test fails with `1 != 2` show calls, which is the review's "attempted once" probe.

- [ ] **Step 3: Implement**

`evaluate_alerts`:
- `previous = {**config.get("alert_states", {}), **self._alert_observed}`.
- Call `alert_transitions(...)` as today, then set `self._alert_observed = dict(states)`.
- Drop `_alert_delivery` entries whose key is now `False` or missing from `states`.
- Set `_alert_delivery[id] = False` for each newly alerted sensor.
- Compute the currently eligible-high IDs with a second call: `alert_transitions(collection, favorites=…, previous={}, threshold=…, now=self._wall_now(), origin=…)[0]`, reusing the single `now` value from the first call. That call goes through W1's eligibility and `is_fresh_observation` guard with the same clock, so the controller has no second freshness rule.
- `notify` is the sensors among those IDs whose `_alert_delivery.get(id) is False`.
- Persist when `states != config["alert_states"]`, retrying on each evaluation while they differ, and report the error as today.

`record_alert_delivery(sensor_id, delivered)` sets `_alert_delivery[sensor_id] = True` only when `delivered` and the key exists.

`_notify_alert` returns `True` for captures. Otherwise it returns `True` after `show()` returns, and `False` on `GLib.Error`, also printing `Airloom: could not show alert notification: {exc}` to stderr.

`_check_alerts` calls `self.controller.record_alert_delivery(sensor.sensor_id, self._notify_alert(sensor))` for each sensor in `decision.notify`.

- [ ] **Step 4: Run and confirm it passes**

`python3 -m unittest tests.test_controller tests.test_app_state tests.test_integration.CachedFallbackAlertIntegrationTest -v`

- [ ] **Step 5: Commit**

`git add airloom/controller.py airloom/app.py tests/test_controller.py tests/test_app_state.py && git commit -m "fix: keep failed alert deliveries pending and never renotify a crossing"`

---

### Task 4: Credential changes revoke queued and in-flight trend work (finding #8)
**Files:**
- Modify `airloom/controller.py`: `TrendPlan` (line 92), `__init__`, `begin_trend` (555), `track_trend` (594), `trend_wanted` (597), `finish_trend` (606), and `save_settings` (747).
- Modify `airloom/app.py`: `_ensure_trend` worker, lines 1364–1373.
- Test `tests/test_controller.py` (`TrendDecisionTest`) and `tests/test_app_requests.py` (`TrendRequestTest`).

**Interfaces:**
- Produces `TrendPlan.credential_generation: int = 0`, as the last field.
- Produces `ApplicationController.credential_generation -> int`, a read-only property.
- `trend_inflight` values become `tuple[RequestToken, int | None, int]`: token, reading `last_seen`, and credential generation.
- `trend_wanted(plan)` additionally requires `plan.credential_generation == self.credential_generation` and a non-empty committed `api_key`.

- [ ] **Step 1: Write the failing test**

In `TrendDecisionTest`, add `test_successful_key_change_revokes_tracked_trend`, which turns the review probe into a test:
- Track a remote plan for `reading(1, last_seen=100)`.
- Call `h.controller.save_settings({**PreferenceTransactionTest.SETTINGS, "clear_api_key": True})`, which succeeds.
- Assert `trend_wanted(plan)` is `False`.
- Assert `finish_trend(1, 100, plan.token, [{"label": "Now", "aqi": 9}], False)` is `None`.
- Assert `1 not in trend_inflight`.

Add these subtests:
- `failed`: with `h.failure = DENIED`, the save fails and `trend_wanted(plan)` stays `True`.
- `unchanged`: a save without a key change keeps `trend_wanted(plan)` `True`.

In `TrendRequestTest`:
```python
def test_key_change_revokes_queued_trend_before_paid_io(self):
    for case, update in (("clear", {"clear_api_key": True}), ("replace", {"api_key": "replacement-key"})):
        with self.subTest(case=case):
            app = self.make_app(sensors=[make_sensor(last_seen=1234)])
            threads, idles = ThreadQueue(), IdleQueue()
            client_type = mock.Mock()
            with (mock.patch.object(app_module.threading, "Thread", new=threads.factory),
                  mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
                  mock.patch.object(app_module, "PurpleAirClient", client_type)):
                app._ensure_trend(1)
                app._save_settings({**TransactionalActionTest.settings_message(), **update})
                threads.threads[0].run()
                for index in range(len(idles.callbacks)):
                    idles.run(index)
            client_type.assert_not_called()
            self.assertNotIn("loaded", [p["status"] for p in self.trend_events(app)])
            self.assertEqual(app._trend_inflight, {})
```

- [ ] **Step 2: Run it and confirm it fails**

`python3 -m unittest tests.test_controller.TrendDecisionTest tests.test_app_requests.TrendRequestTest -v`

Expected:
- `trend_wanted` is `True`.
- `client_type` is called once with `'test-api-key'`.

- [ ] **Step 3: Implement**

`save_settings`:
- Capture `previous_key = config.get("api_key", "")` before saving.
- After a successful `_save` and `_commit`, if `candidate["api_key"] != previous_key`:
  - increment `self._credential_generation`;
  - call `self._requests.invalidate(f"trend:{sid}")` for every `sid` in `trend_inflight`;
  - clear `trend_inflight`.

`begin_trend` stamps the current generation on remote plans, and `track_trend` stores it.

`finish_trend`'s `authorized` also requires `inflight[2] == self._credential_generation`. Its signature is unchanged.

`app.py` worker:
- Read the key inside the worker, right after `trend_wanted(plan)` succeeds: `api_key = self.controller.config.get("api_key", "")`.
- Construct the client only when `trend_wanted` is true, so no old-key client exists for a revoked plan.

- [ ] **Step 4: Run and confirm it passes**

`python3 -m unittest tests.test_controller tests.test_app_requests -v`

- [ ] **Step 5: Commit**

`git add airloom/controller.py airloom/app.py tests/test_controller.py tests/test_app_requests.py && git commit -m "fix: revoke queued trend work when the API key changes or is cleared"`

---

### Task 5: No SQLite on the main loop, with bounded tracked cache workers (finding #10)
**Files:**
- Modify `airloom/app.py`:
  - `__init__` (lines 458–461);
  - `_start_worker` and `_worker_finished` (489–516);
  - `_paint_cached_home` (1249–1261);
  - `_ensure_trend` (1326–1389);
  - `_finish_trend` (1391–1420).
- Modify `airloom/controller.py`: `wants_cached_startup` and `apply_cached_startup` (436–454), and `accept_refresh`. Remove `cached_trend` (586–592), which is replaced by `finish_trend(..., cached=True)`.
- Modify the `CLAUDE.md` **Threading** paragraph to add one sentence: cache reads and writes run on tracked workers, and the main loop never touches SQLite.
- Test `tests/test_app_requests.py`: new `MainLoopCacheTest`, `TrendRequestTest`, and `ShutdownLifecycleTest`.
- Test `tests/test_app_state.py`: `ApplicationAdapterTestCase.make_app` and `CachedStartupTest`.

**Interfaces:**
- `AirloomApplication._start_worker(target, *, name: str, args=(), kind: str | None = None, limit: int | None = None) -> bool` refuses when `limit` workers of `kind` are already active. It tracks them in `self._active_kinds: dict[str, int]`.
- `AirloomApplication._submit_coalesced(kind: str, key, job, run, *, name: str) -> bool`:
  - It queues `job` in `self._coalesced_jobs[kind][key]`, replacing an unstarted job with the same key.
  - At most one tracked worker per `kind` drains the queue in FIFO order, and `self._coalesced_running: set[str]` records which kinds are draining.
  - A job that raises is printed to stderr, and the drain continues.
  - `run(job)` executes on that worker.
- `TREND_WORKER_LIMIT = 4`, a module constant in `app.py`.
- `_finish_trend(sensor_id, reading_last_seen, token, trend, failed, from_cache: bool = False, cache_error: str | None = None) -> bool`.
- `_queue_trend_write(sensor_id: int, trend: list) -> None` uses coalescing kind `"trend-cache"`, keyed by `sensor_id`. Its job is `(sensor_id, trend, credential_generation)`.
- `_write_trend(job)` runs on the worker.
- `_report_trend_cache_error(message: str) -> bool` runs on the main loop.
- `_read_cached_home(job)` runs on the worker, using coalescing kind `"startup-cache"` with key `None`.
- `_apply_cached_home(token, sensors, bounds) -> bool` runs on the main loop.
- Controller:
  - `finish_trend(..., *, cached: bool = False)`. A cached `loaded` result has `store=False`.
  - `begin_cached_startup() -> RequestToken | None` uses context `"cached-startup"`.
  - `apply_cached_startup(token: RequestToken, sensors: list[Sensor], bounds: Bounds) -> bool` requires the current token and an unchanged generation.
  - `accept_refresh` invalidates `"cached-startup"` on publish.

- [ ] **Step 1: Write the failing test**

New `MainLoopCacheTest(ApplicationRequestTestCase)` in `tests/test_app_requests.py`:
```python
MAIN_LOOP_BLOCK_BOUND = 0.25  # seconds; the production busy timeout is 5.0

def test_cache_lock_never_blocks_the_main_loop(self):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "cache.db"
        cache = SensorCache(path)                      # production busy_timeout=5.0
        self.addCleanup(cache.close)
        cache.store_trend(1, [{"label": "Now", "aqi": 42}])
        reader = self.make_app(sensors=[make_sensor(1, last_seen=1234), make_sensor(2, last_seen=99)])
        startup = self.make_app()
        reader.cache = startup.cache = cache
        idles = IdleQueue()
        blocker = sqlite3.connect(path)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN EXCLUSIVE")
        client_type = mock.Mock()
        with (mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
              mock.patch.object(app_module, "PurpleAirClient", client_type)):
            plan = reader.controller.begin_trend(2); reader.controller.track_trend(plan)
            for label, call in (
                ("trend read", lambda: reader._ensure_trend(1)),
                ("trend write", lambda: reader._finish_trend(2, 99, plan.token, [{"label": "Now", "aqi": 7}], False)),
                ("startup paint", startup._paint_cached_home),
            ):
                started = time.monotonic(); call()
                self.assertLess(time.monotonic() - started, MAIN_LOOP_BLOCK_BOUND, label)
            blocker.rollback()
            deadline = time.monotonic() + 10
            while (reader._active_workers or startup._active_workers) and time.monotonic() < deadline:
                time.sleep(0.01)
            for index in range(len(idles.callbacks)):
                idles.run(index)
        self.assertEqual(reader._active_workers + startup._active_workers, 0)
        client_type.assert_not_called()
        loaded = [p for p in self.trend_events(reader) if p["sensor_id"] == 1]
        self.assertEqual(loaded[-1]["status"], "loaded")
        self.assertEqual(loaded[-1]["trend"], [{"label": "Now", "aqi": 42}])
        self.assertEqual(cache.get_trend(2, 600), [{"label": "Now", "aqi": 7}])
```
This test uses real threads. Polling `_active_workers` is the completion condition.

Add `test_trend_admission_is_bounded_and_refusal_is_terminal` to `TrendRequestTest`:
- Five sensors, each selected once through `ThreadQueue`.
- Assert `len(threads.threads) == TREND_WORKER_LIMIT`.
- Assert sensor 5's last trend event has `status == "error"`, so Retry is offered.
- Assert `5 not in app._trend_inflight`.

Extend `test_process_exit_drains_every_worker_class_and_closes_cache_once`:
- Add the new attributes to its app setup.
- Make `RecordingCache.sensors_in` call `block("startup-cache")` and `RecordingCache.store_trend` call `block("trend-cache")`.
- Before `app.sensors = [h.make_sensor(1)]`, call `app._paint_cached_home()`. After it, call `app._queue_trend_write(1, [])`.
- Expect 7 started workers and 8 marker lines. The `set` gains `"startup-cache"` and `"trend-cache"`, and `close` stays last.

Contract changes to existing tests. All their assertions stay.
- `test_trend_cache_write_failure_reports_degradation_and_emits_loaded`: after `idles.run(0)`, also run `threads.threads[1].run()` and `idles.run(1)`. The error now comes from the write worker.
- `CachedStartupTest.test_cached_startup_is_real_cache_delivery_but_not_stale_fallback`: run `_paint_cached_home()` under `ThreadQueue` and `IdleQueue`, imported from `tests.test_app_requests` with the same `try`/`except ImportError` fallback that `gi_stub` uses. Then run the thread and the idle callback before the existing assertions.
- Add `test_refresh_published_first_wins_over_late_cached_paint` to `CachedStartupTest`. The paint worker reads, `accept_refresh` publishes a live outcome, then the paint idle runs. Assert that no second `sensors` event exists and the published sensors stay.

Both `make_app` helpers and the subprocess script set the new attributes:
- `_worker_lock`;
- `_active_workers = 0`;
- `_cache_closed = False`, in `test_app_state`;
- `_active_kinds = {}`, `_coalesced_jobs = {}`, and `_coalesced_running = set()`.

- [ ] **Step 2: Run it and confirm it fails**

`python3 -m unittest tests.test_app_requests.MainLoopCacheTest tests.test_app_requests.TrendRequestTest tests.test_app_requests.ShutdownLifecycleTest tests.test_app_state.CachedStartupTest -v`

Expected:
- `MainLoopCacheTest` fails with `AssertionError: 5.0… not less than 0.25 : trend read`.
- The bounded-admission test makes 5 threads.
- The exit test fails with `AttributeError: _queue_trend_write`.

- [ ] **Step 3: Implement**

`_ensure_trend`:
- For a remote plan, track it and send `loading`.
- Start a worker with `kind="trend", limit=TREND_WORKER_LIMIT`. The worker:
  1. tries `cache.get_trend(sensor_id, ttl)`, recording `cache_error` on exception;
  2. on a miss, runs Task 4's `trend_wanted` check and fetch;
  3. posts `_finish_trend(..., from_cache=cached is not None, cache_error=…)`.
- On refused admission, call `self._finish_trend(sensor_id, plan.reading_last_seen, plan.token, None, True)`.

`_finish_trend`:
- Send the existing "Could not read cached trend: … Fetching a live trend instead." error when `cache_error` is set.
- Call `controller.finish_trend(..., cached=from_cache)`.
- On `update.store`, call `_queue_trend_write`.

`_write_trend` skips the write when `self.cache is None` or the job's generation is not `controller.credential_generation`. On exception, it calls `GLib.idle_add(self._report_trend_cache_error, str(exc))`, which sends the existing "Could not cache trend: …" message.

`_paint_cached_home`:
- Get `token = controller.begin_cached_startup()`.
- Submit `(token, controller.home_bounds())` to `"startup-cache"`.
- The worker reads `sensors_in`, ignores `_CACHE_FAILURES`, and calls `GLib.idle_add(self._apply_cached_home, token, sensors, bounds)`.

`_close_cache_if_drained` is unchanged: every cache user is now an admitted tracked worker, so closing still waits for `_active_workers == 0`.

- [ ] **Step 4: Run and confirm it passes**

`python3 -W error::ResourceWarning -m unittest tests.test_app_requests tests.test_app_state tests.test_controller tests.test_integration -v`

- [ ] **Step 5: Commit**

`git add airloom/app.py airloom/controller.py CLAUDE.md tests/test_app_requests.py tests/test_app_state.py && git commit -m "fix: move trend and startup cache I/O to bounded tracked workers"`

---

### Task 6: Reverse-label workers check authority before I/O and coalesce (Minor: reverse-label workers)
**Files:** Modify `airloom/app.py`:
- `_install_place_search_admission` docstring (lines 106–133);
- `_on_location_fix` worker start (940–947) and `_reverse_label_worker` (949–968);
- `_start_view_label` (1089–1100) and `_view_label_worker` (1102–1116).

Test: `tests/test_app_requests.py` (`ReverseLabelRequestTest`).

**Interfaces:**
- Produces `AirloomApplication._reverse_name(latitude: float, longitude: float, token: RequestToken) -> str | None`. It returns `None` when the request was superseded before the geocoder's I/O or after its throttle wait.
- Label work uses `_submit_coalesced("view-label", None, job, …)` and `_submit_coalesced("home-label", None, job, …)`, with `job = (latitude, longitude, generation, token)`.
- Consumes W1's `Geocoder._request` and `Geocoder._throttle` admission points. See Cross-workstream contracts.

- [ ] **Step 1: Write the failing test**

In `ReverseLabelRequestTest`:
- `test_superseded_label_never_reaches_geocoder`, which turns the review probe into a test:
  - `_start_view_label(10.0, 20.0)` under `ThreadQueue`, then `app.controller.shutdown()`, then `threads.threads[0].run()`.
  - With `reverse_geocode` patched as a `mock.Mock`, assert it's not called and `idles.callbacks == []`.
  - Repeat with `app.requests.invalidate("home-label")` after `_on_location_fix(30.0, 40.0, 50.0)`.
- `test_label_superseded_during_throttle_wait_never_sends`:
  - `client = geocode_module.Geocoder(clock=lambda: 100.0, sleep=lambda _s: app.requests.invalidate("view-label"))` with `client._last_request = 100.0`.
  - Patch `_default_geocoder` and `_default_base_url` as in `test_superseded_search_waiting_in_geocoder_queue_never_reaches_network`, and patch `urllib.request.urlopen` as a `mock.Mock`.
  - Run the queued label worker. Assert `urlopen.assert_not_called()` and no idle callback.
- `test_queued_view_labels_coalesce_to_newest_target`:
  - Set `app.current_view = (None, (12.0, 22.0))`.
  - Start labels for `(10, 20)`, `(11, 21)`, and `(12, 22)`.
  - Assert `len(threads.threads) == 1`.
  - Run it, then assert `reverse_geocode.call_args_list == [mock.call(12.0, 22.0)]` and exactly one `view-name` event.

Contract change to an existing test: `test_slow_home_a_cannot_reverse_newer_home_b`.
- New choreography: assert `len(threads.threads) == 1`, run it, then run `idles.run(0)`.
- With `reverse_geocode` returning `"B home"`, keep every existing outcome assertion: the B label, the coordinates, and `labels == ["B home"]`.
- Add `assert reverse_geocode.call_args_list == [mock.call(30.0, 40.0)]`, which is stronger: A never reaches the network.

- [ ] **Step 2: Run it and confirm it fails**

`python3 -m unittest tests.test_app_requests.ReverseLabelRequestTest -v`

Expected:
- `reverse_geocode` is called once after shutdown.
- `urlopen` is called once.
- 3 threads are made.

- [ ] **Step 3: Implement**

`_reverse_name`:
1. Return `None` if shutdown has begun or the token isn't current.
2. Call `_install_place_search_admission()`.
3. Set `_place_search_admission.is_current = lambda: self.requests.is_current(token)`.
4. Call `reverse_geocode`, mapping `_PlaceSearchSuperseded` to `None`. Keep the existing `GeocodeError` coordinate fallback.
5. Reset `is_current` to `None` in `finally`.

Both label workers return without `idle_add` when `_reverse_name` returns `None`.

Update the `_install_place_search_admission` docstring: annotated reverse callers now share the admission points.

- [ ] **Step 4: Run and confirm it passes**

`python3 -m unittest tests.test_app_requests -v`

- [ ] **Step 5: Commit**

`git add airloom/app.py tests/test_app_requests.py && git commit -m "fix: drop superseded reverse-label work before geocoder I/O and coalesce it"`

---

### Task 7: Shell recovery restores the controller's viewport before refresh (finding #12)
**Files:**
- Modify `airloom/controller.py`: `__init__`, `view_changed` (stores the zoom), and a new `restore_view`.
- Modify `airloom/app.py`:
  - `__init__` (line 465);
  - `_reload_shell` (791–803);
  - `_on_script_message` (995–1030).
- Modify `airloom/resources/app.js`. These are W2's only `app.js` lines:
  - a new line after line 102, inside `receive`;
  - a new function inserted between `applyLocation` (ends line 199) and `applyConfig` (line 201).
- Modify the `CLAUDE.md` bridge protocol bullets (lines 33–34).
- Test: `tests/test_controller.py` (`RefreshWorkflowTest`) and `tests/test_app_state.py` (`ShellRecoveryTest`, plus `make_app`).

**Interfaces:**
- **New bridge event (Python → JS): `restore-view`.**
  - Payload: `{"lat": float, "lon": float, "zoom": float | null}`.
  - `lat` and `lon` come from `controller.current_view[1]`.
  - `zoom` is the last valid `zoom` from a `view-changed` message: finite and `3 <= zoom <= 17`, otherwise `null`.
  - It's sent only from the `ready` handler, after `config` and before any refresh, and only when `controller.current_view is not None`.
- Produces `ApplicationController.current_zoom: float | None`.
- Produces `ApplicationController.restore_view() -> dict | None`.
- Produces `AirloomApplication._awaiting_shell_ready: bool`, set by `_reload_shell` and cleared by `ready`. While it's set, every page action except `ready` is dropped with an stderr note.
- `_reload_shell` invalidates the page-origin contexts `place-search:main`, `place-search:home`, and `view-label`.
- Exact `app.js` lines (W3 merges around them and must not edit them):
  ```js
      if (event === "restore-view") restoreView(payload);
  ```
  ```js
    // Shell recovery: adopt the controller's viewport so the reloaded page and
    // Python target one view before the refresh that follows ready.
    function restoreView(payload) {
      const lat = Number(payload.lat), lon = Number(payload.lon), zoom = Number(payload.zoom);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
      state.center = { lat, lon };
      if (payload.zoom !== null && Number.isFinite(zoom)) state.zoom = Math.max(3, Math.min(17, zoom));
      state.positioned = true;
      renderMap();
    }
  ```
  `restoreView` deliberately doesn't call `scheduleViewChanged()`. The page adopts the view Python already holds, so echoing it back would only register a duplicate.

- [ ] **Step 1: Write the failing test**

In `RefreshWorkflowTest`, add `test_restore_view_reports_last_valid_view_and_zoom`:
- `restore_view()` is `None` before any view.
- After `view_changed({**VIEW, "zoom": 9})`, it equals `{"lat": 15.0, "lon": -20.0, "zoom": 9.0}`.
- After `view_changed({**VIEW, "lat": 16.0, "zoom": "x"})`, its `zoom` is `None`.

In `ShellRecoveryTest`:
```python
def test_crash_away_from_home_restores_view_before_refresh(self):
    app = self.make_app()
    app.webview = FakeWebView()
    app._start_fetch = lambda request: app.sent.append(("fetch", request.center))
    app.controller.view_changed({**VIEW, "zoom": 9})
    app.sent.clear()
    old_search = app.requests.begin("place-search:main")
    old_label = app.requests.begin("view-label")
    labels = []
    app._start_view_label = lambda *center: labels.append(center)
    app._on_web_process_terminated(None, "crashed")
    app._on_script_message(None, ScriptMessage({**VIEW, "action": "view-changed", "lat": 0.0, "lon": 0.0}))
    app._on_script_message(None, ScriptMessage({"action": "refresh"}))
    app._on_script_message(None, ScriptMessage({"action": "ready"}))
    events = [event for event, _ in app.sent]
    self.assertLess(events.index("config"), events.index("restore-view"))
    self.assertLess(events.index("restore-view"), events.index("fetch"))
    self.assertEqual(dict(app.sent)["restore-view"], {"lat": 15.0, "lon": -20.0, "zoom": 9.0})
    self.assertEqual([p for e, p in app.sent if e == "fetch"], [(15.0, -20.0)])
    self.assertEqual(app.controller.current_view[1], (15.0, -20.0))
    self.assertFalse(app.requests.is_current(old_search))
    self.assertFalse(app.requests.is_current(old_label))
    self.assertEqual(labels, [(15.0, -20.0)])

def test_first_ready_without_a_view_sends_no_restore(self):
    app = self.make_app()
    app._on_script_message(None, ScriptMessage({"action": "ready"}))
    self.assertNotIn("restore-view", [event for event, _ in app.sent])
```
`make_app` gains `app._awaiting_shell_ready = False`, as does `ApplicationRequestTestCase.make_app`.

- [ ] **Step 2: Run it and confirm it fails**

`python3 -m unittest tests.test_controller.RefreshWorkflowTest tests.test_app_state.ShellRecoveryTest -v`

Expected:
- `AttributeError: restore_view`.
- `ValueError: 'restore-view' is not in list`.
- The stale `view-changed` moves the fetch to `(0.0, 0.0)`.

- [ ] **Step 3: Implement**

`view_changed` stores `current_zoom` from `message.get("zoom")` when it's a non-bool finite number in `[3, 17]`, and `None` otherwise.

`restore_view()` returns the payload above.

`_reload_shell`:
- Set `_awaiting_shell_ready = True`.
- Invalidate the three contexts.
- Then send `shell-status` and load as today.

`_on_script_message`:
- After decoding, if `_awaiting_shell_ready` and `action != "ready"`, print `Airloom: ignored {action!r} from a replaced page` and return.
- In `ready`:
  1. clear the flag;
  2. send `config`;
  3. if `restore_view()` returns a payload, send `restore-view` and call `_start_view_label(lat, lon)`;
  4. continue with the existing sequence (motion, notice, cache error, paint, refresh).

`app.js`: add exactly the lines given in Interfaces.

`CLAUDE.md`:
- Add `restore-view` to the Python → JS event list.
- Append to the request-identity bullet: "After `reload-shell` or a renderer crash, Python ignores page messages until the new page's `ready`, invalidates the old page's place-search and view-label requests, and answers `ready` with `config` then `restore-view` `{lat, lon, zoom}` (the controller's current view; omitted before one exists) before refreshing."

- [ ] **Step 4: Run and confirm it passes**

`python3 -m unittest tests.test_controller tests.test_app_state tests.test_app_requests -v && node --check airloom/resources/app.js`

- [ ] **Step 5: Commit**

`git add airloom/controller.py airloom/app.py airloom/resources/app.js CLAUDE.md tests/test_controller.py tests/test_app_state.py tests/test_app_requests.py && git commit -m "fix: restore the controller viewport into a recovered shell before refreshing"`

---

### Task 8: Durability failures after the rename count as committed saves (W1 contract 3; spec finding 13's controller's call)

**Precondition:** this task needs W1's `airloom.store.SettingsDurabilityError` and `store._fsync_directory`. Do it after Tasks 1–7, once `astra-data-v0-14-0` has merged into `astra-v0-14-0` (W1 merges first). Start with `git merge astra-v0-14-0`, then `make check PYTHON="python3 -W error::ResourceWarning"`, and resolve nothing in W1-owned files. If W1 hasn't merged when Tasks 1–7 are done, stop and report. Don't stub the class.

**Files:**
- Modify `airloom/controller.py`:
  - import `SettingsDurabilityError` from `.store`;
  - `ChangeResult` and `SettingsResult` gain `warning: str | None = None`;
  - `_save` (lines 248–252);
  - every transaction that calls `_save`: `toggle_favorite`, `toggle_hidden`, `unhide`, `unhide_all`, `set_location_filter`, `set_detected_home`, `set_home_label`, `save_settings`, and `evaluate_alerts`.
- Modify `airloom/app.py`: `_apply_display_change`, `_set_location_filter`, `_save_settings`, `_on_location_fix`, and `_apply_location_name`.
- Test: `tests/test_controller.py` (`Harness`, `PreferenceTransactionTest`, `AlertWorkflowTest`) and `tests/test_app_requests.py` (`TransactionalActionTest`).

**Interfaces:**
- Consumes `store.SettingsDurabilityError(OSError)`, which has `.saved: dict` and is raised only after `os.replace` succeeded and `Store.data == exc.saved`.
- Produces:
  - `ApplicationController._save(candidate) -> str | None`. It returns the durability warning `str(exc)`, or `None` on a clean save. It catches `SettingsDurabilityError` before any `except OSError`, then adopts `exc.saved` as committed configuration.
  - `ChangeResult.warning` and `SettingsResult.warning`.
  - `AlertDecision.error`, which carries the durability warning text for alert-state saves. That error is already a toast.

- [ ] **Step 1: Write the failing test**

`Harness` gains `self.durability_failure = False`. When it's set, `persist` commits `self.config = deepcopy(candidate)` and appends to `saves`, then raises `SettingsDurabilityError("Settings were saved, but the system could not confirm they reached the disk: [Errno 5] I/O error", deepcopy(candidate))`.

In `PreferenceTransactionTest`:
```python
def test_durability_failure_after_rename_commits_and_warns(self):
    cases = (
        ("favorite", lambda c: c.toggle_favorite(1), lambda cfg: 1 in cfg["favorites"]),
        ("hide", lambda c: c.toggle_hidden(1), lambda cfg: "1" in cfg["hidden"]),
        ("filter", lambda c: c.set_location_filter("indoor"), lambda cfg: cfg["location_filter"] == "indoor"),
        ("detected home", lambda c: c.set_detected_home(1.0, 2.0), lambda cfg: cfg["latitude"] == 1.0),
        ("settings", lambda c: c.save_settings(self.SETTINGS), lambda cfg: cfg["location_name"] == "Pinned"),
    )
    for label, action, committed in cases:
        with self.subTest(label=label):
            h = Harness(home_mode="auto")
            h.controller.sensors = [reading(1)]
            generation = h.controller.begin_refresh().generation
            h.durability_failure = True
            result = action(h.controller)
            self.assertTrue(getattr(result, "ok", None) is True or getattr(result, "status", None) == "success")
            self.assertIn("could not confirm", result.warning)
            self.assertFalse(result.warning.startswith("Could not"))
            self.assertTrue(committed(h.config))
            self.assertIsNone(getattr(result, "error", None))
            self.assertEqual(h.controller.begin_refresh().generation, generation + 1)
```
The last assertion checks that `_commit` ran: a new configuration generation exists. Add a separate subtest for `set_home_label("Named", 1.0, 2.0)` on a `Harness(home_mode="auto", latitude=1.0, longitude=2.0)`. It asserts `ok`, the warning, and `h.config["location_name"] == "Named"`, but no generation check, because `set_home_label` never commits a generation today.

In `AlertWorkflowTest`, add `test_alert_state_durability_failure_is_saved_and_not_retried`:
- `favorites=[1]`, with a crossing at AQI 150 and `durability_failure = True`.
- The first evaluation notifies `[1]`, its `error` contains `"could not confirm"`, and `h.config["alert_states"] == {"1": True}`.
- After `record_alert_delivery(1, True)` and `h.durability_failure = False`, the second evaluation has `error is None`, and `len(h.saves) == 1`, so the save isn't retried.

In `TransactionalActionTest`, add `test_settings_durability_failure_acknowledges_success_and_toasts_warning`:
- Use a real store from `make_real_store_app`.
- Patch `mock.patch.object(store_module, "_fsync_directory", side_effect=OSError(errno.EIO, "I/O error"))`. The directory already exists, so only the post-replace directory sync fails.
- Run `app._save_settings(self.settings_message())`, then send `favorite` for a present sensor through `_on_script_message`.
- Assert:
  - the `settings-save-result` is `{"request_id": "settings-17", "status": "success", "error": None}`;
  - a `config` event was sent;
  - `app.store.data["location_name"] == "Pinned home"` and `Store(app.store.path).data["location_name"] == "Pinned home"`;
  - the sensor ends up favorited;
  - exactly two `error` events were sent, each containing `"could not confirm"` and none starting with `"Could not save"`.

- [ ] **Step 2: Run it and confirm it fails**

`python3 -m unittest tests.test_controller.PreferenceTransactionTest tests.test_controller.AlertWorkflowTest tests.test_app_requests.TransactionalActionTest -v`

Expected:
- The subtests fail with `ok False` and `status 'error'`, because today's `except OSError` treats the durability failure as a failed save.
- `AttributeError: 'ChangeResult' object has no attribute 'warning'`.
- The adapter test sees `status: "error"`.

- [ ] **Step 3: Implement**

`_save`:
- Wrap `self._persist(candidate)` in `except SettingsDurabilityError as exc`.
- Set `saved = exc.saved` and `warning = str(exc)`.
- Adopt `saved` exactly like a clean save, and return `warning`.

Each transaction:
- Keep `warning = self._save(candidate)` inside its existing `try`.
- On success, pass `warning=warning` into the `ChangeResult` or `SettingsResult`.
- `evaluate_alerts` sets `error = warning` when there's no persistence error.

Adapter:
- After each successful result, if `result.warning`, call `self._send("error", {"message": result.warning})`.
- In `_save_settings`, send the warning after the success `settings-save-result`. The inline Preferences error path stays reserved for real failures.
- `_check_alerts` is unchanged, because the warning already rides on `decision.error`.

- [ ] **Step 4: Run and confirm it passes**

`python3 -W error::ResourceWarning -m unittest tests.test_controller tests.test_app_requests tests.test_app_state tests.test_store -v`

- [ ] **Step 5: Commit**

`git add airloom/controller.py airloom/app.py tests/test_controller.py tests/test_app_requests.py && git commit -m "fix: treat post-rename settings durability failures as committed saves with a warning"`

---

### Task 9: Workstream verification

- [ ] **Step 1: Full suite with ResourceWarnings as errors.**

`make check PYTHON="python3 -W error::ResourceWarning"`

Expected: all tests pass, `compileall` is clean, and `node --check` and `node --test` pass.

- [ ] **Step 2: Hosted-job shape without PyGObject.**

```
python3 -m venv --without-pip /tmp/opencode/w2-nogi && /tmp/opencode/w2-nogi/bin/python -W error::ResourceWarning -m unittest discover -s tests
```
Expected: OK. The adapter tests load `tests/gi_stub.py`.

- [ ] **Step 3: Live UI scripts.**

Run these as the non-root user under a private display:
```
for script in test-ui-regression test-heatmap test-save-refresh; do dbus-run-session -- xvfb-run -a -s "-screen 0 1280x800x24" env GDK_DEBUG=no-portals python3 "scripts/$script" || exit 1; done
```

- [ ] **Step 4: Live shell-recovery probe** (real WebKit, real `app.js`; no file committed).

Save this as `/tmp/opencode/w2-recovery-probe.py` and run it with `dbus-run-session -- xvfb-run -a -s "-screen 0 1280x800x24" env GDK_DEBUG=no-portals python3 /tmp/opencode/w2-recovery-probe.py "$PWD"`.

The probe:
- Uses a temporary `XDG_CONFIG_HOME` and `XDG_CACHE_HOME`, with config `{"home_mode": "fixed", "latitude": 45.5152, "longitude": -122.6784, "location_name": "Portland"}` and no key, so it runs on demo data.
- Sets `AIRLOOM_NOMINATIM_URL=http://127.0.0.1:9/` so labels fail locally.
- Launches `scripts/debug-run` with `AIRLOOM_DEBUG_SOCKET` set in that directory, then polls `scripts/debug-client ping` and `state` until `sensorCount > 0`.
- Runs `eval {"js": "window.Airloom.receive('location', {latitude: 47.6062, longitude: -122.3321, name: 'Seattle', source: 'fixed'}); 1"}`.
- Sleeps 2.5 s. This is the one justified fixed wait: the page's `view-changed` debounce is 1200 ms after a 600 ms flight, and Python's view has no observable probe.
- Runs `eval {"js": "window.__w2OldPage = true; window.webkit.messageHandlers.airloom.postMessage(JSON.stringify({action: 'reload-shell'})); 1"}`.
- For up to 20 s, polls `eval {"js": "window.__w2OldPage === undefined"}` until it returns `true`, meaning the old document is gone. It then polls `state` until `sensorCount > 0`.
- It asserts that `center` is within 0.01° of `(47.6062, -122.3321)` and not at Portland, then sends `quit`.

Expected: the probe prints `recovered at Seattle` and exits 0. Record the output in the W2 report.

- [ ] **Step 5: Report.**

The W2 report lists:
- the exact `app.js` lines from Task 7;
- the existing tests whose choreography changed, from Tasks 3, 5, and 6, with the reason for each;
- the `git merge astra-v0-14-0` commit that brought in W1 for Task 8;
- the outputs of Steps 1–4.

## Cross-workstream contracts

- **W1 → W2, the freshness rule (W1 contract 1).**
  - W1 defines these in `airloom/models.py`: `MAX_OBSERVATION_AGE = 7200`, `FUTURE_SKEW_TOLERANCE = 600`, `plausible_observation_time(value, now) -> int | None`, and `is_fresh_observation(last_seen, now) -> bool`. Fresh means `last_seen is not None and -600 <= now - last_seen <= 7200`, with both bounds inclusive.
  - W1 routes the guard in `presentation.alert_transitions` through `is_fresh_observation`.
  - W2's alert eligibility (Task 3) stays on that single predicate, and only through `alert_transitions(sensors, *, favorites, previous, threshold, now, origin)`, with `now = self._wall_now()`. It calls `alert_transitions` twice per evaluation, with the same `now` value: once with the observed state for crossings, and once with `previous={}` for the currently eligible-high set used by retries.
  - W2 adds no age arithmetic and never edits the guard, the constant alias, or the import line.
  - W2 doesn't import `is_fresh_observation` directly, because the name only exists once W1 merges. If integration prefers a direct call for the retry set, the call must be `is_fresh_observation(sensor.last_seen, now)`, preceded by a failing-first test that pins the 7200/7201 and -600/-601 boundaries through `evaluate_alerts`.
- **W1 → W2, settings durability (W1 contract 3).**
  - `airloom.store.SettingsDurabilityError(OSError)` has `.saved: dict`. It's raised by `Store.apply` and every mutator only after the rename succeeded and `Store.data == exc.saved`.
  - Task 8 catches it in `ApplicationController._save` before any `except OSError`, adopts `exc.saved`, runs `_commit` as on success, and returns success (`ChangeResult(True, …, warning=…)` or `SettingsResult("success", …, warning=…)`).
  - The adapter sends `str(exc)` as an `error` event (toast), never as "Could not save …", and sends `settings-save-result` with status `success`.
  - Task 8 is sequenced after W1 merges into `astra-v0-14-0`. Until then the subclass falls into today's `except OSError`, which is visible but misworded, as W1 notes.
- **W1 → W2, the geocoder admission points (W1 contract 4).**
  - `Geocoder.reverse` serves cache hits without calling `_throttle` or `_request`. Misses run inside `_reverse_lock` and call `self._throttle()` and `self._request(url, params, timeout)`.
  - Exceptions, including the adapter's `_PlaceSearchSuperseded`, propagate and are never cached.
  - A follower waiting on `_reverse_lock` re-checks the cache and otherwise runs its own `_request`, which passes through its own admission check.
  - Task 6's wrappers rely on exactly this.
- **W1 → W2, the PurpleAir client (W1 contract 5).** No `app.py` change is needed. Malformed responses raise `PurpleAirError`, which `_fetch_readings` already maps to stale-cache or demo fallback. Task 5's trend worker keeps catching `PurpleAirError` as `failed`.
- **W2 → W3, `app.js` ownership.** W2 adds exactly two hunks, given verbatim in Task 7:
  - the `restore-view` line in `window.Airloom.receive`;
  - the `restoreView` function between `applyLocation` and `applyConfig`.
  W3 rebases around them and doesn't edit them. If W3 renames `renderMap`, `state.center`, `state.zoom`, or `state.positioned`, W3 updates `restoreView` in the same commit and says so. The zoom clamp `3..17` mirrors `applyZoom`.
- **W2 → W3, the bridge payloads.**
  - New event: `restore-view` `{lat: number, lon: number, zoom: number | null}`, documented in `CLAUDE.md`.
  - `sensors`, `trend`, and `settings-save-result` shapes are unchanged.
  - `trend` may now go `loading` → `loaded` for cache hits, where before a cache hit went straight to `loaded`. W3's trend rendering must accept that.
  - The summary `source` strings that `app.py` produces (`"PurpleAir · cached …"`) are unchanged by W2. W3's decision-4 work keys badges off per-reading `origin`, `delivery`, `last_seen`, and batch `stale`. If W3 needs a Python source-string change, it's raised at integration with a failing test first.
- **W2 → integration.** Integration replays the shell-recovery handshake in the real page after merging W3, as the Task 9 Step 4 probe does, and keeps the `CLAUDE.md` bridge wording.

## Self-review

- **Spec coverage:**
  - Finding 4 → Task 3.
  - Finding 8 → Task 4.
  - Finding 10 → Task 5.
  - Finding 11 → Task 2.
  - Finding 12 → Task 7.
  - Minor: strict settings payload validation → Task 1.
  - Minor: reverse-label workers → Task 6.
  - Finding 13 controller's call (durability warning, no rollback, W1 contract 3) → Task 8.
  - Workstream verification → Task 9.
  - Every spec bullet maps to an assertion:
    - "test both directions" → the two `AlertWorkflowTest` tests plus the adapter retry.
    - "clear the key while a worker is queued" → `test_key_change_revokes_queued_trend_before_paid_io`.
    - "real held lock" → `MainLoopCacheTest`, with `BEGIN EXCLUSIVE` and the production 5 s timeout.
    - "permutations" → Task 2's manual → auto, manual → identical covered view, and manual → new target.
    - "crash and reload while away from home" → Task 7 and the Task 9 probe.
    - "shutdown drains new workers before close" → the extended process-exit test.
- **Type consistency:**
  - `TrendPlan.credential_generation: int` is stamped by `begin_trend`, stored as the third `trend_inflight` element, checked by `trend_wanted` and `finish_trend`, and read by `_write_trend` through `controller.credential_generation`.
  - `finish_trend(..., *, cached=False)` replaces `cached_trend`, which has no remaining callers.
  - `apply_cached_startup` gains its leading `token`, and both its callers (`_apply_cached_home` and the tests) are updated in Task 5.
  - `_save` returns `str | None` (the warning), and no caller used its old return value.
  - `ChangeResult` and `SettingsResult` gain a defaulted `warning`, so existing constructors are unaffected.
  - `restore-view.zoom` is `float | null` on both sides.
- **Review Focus coverage:**
  - Focus 1 → Task 2.
  - Focus 2 → Task 3.
  - Focus 3 → Task 4.
  - Focus 4 → Task 5.
  - Focus 5 → Task 7.
- **Existing tests whose choreography changes,** each because a contract changed, with no assertion removed:
  - `test_only_fresh_real_favorite_crossings_notify_and_persist_once` (Task 3, acknowledgement);
  - `test_trend_cache_write_failure_reports_degradation_and_emits_loaded` and `test_cached_startup_is_real_cache_delivery_but_not_stale_fallback` (Task 5, worker hop);
  - `test_process_exit_drains_every_worker_class_and_closes_cache_once` (Task 5, strengthened to 7 worker classes);
  - `test_slow_home_a_cannot_reverse_newer_home_b` (Task 6, coalescing; strengthened).
