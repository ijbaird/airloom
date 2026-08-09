# Caching Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close out the seven Minor findings parked by the final whole-branch review of the PurpleAir caching feature (shipped in 0.11.0, PR #19).

**Architecture:** Small, independent fixes to the existing `airloom/cache.py` and `airloom/app.py` from the caching feature (spec: `docs/superpowers/specs/2026-08-08-purpleair-api-caching-design.md`). No new modules, no schema changes.

**Tech Stack:** Python stdlib (`sqlite3`, `unittest`); no GTK in tests.

## Global Constraints

- Zero third-party dependencies: Python stdlib + system PyGObject only; no pip packages, no JS libraries.
- Tests are pure-stdlib `unittest`, run without GTK (`make test`). `app.py` is GUI code and has **no unit tests by project policy** — its tasks are verified by `make check`.
- Never touch GTK/WebKit from a worker thread.
- Run `make check` before declaring the work done.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

**Explicitly out of scope (wontfix, by decision at final review):**
- The duplicated `epa_corrected_pm25` computation across `sensor_from_values`/`trend_from_values` (negligible cost, inherent to the split).
- Guarding mid-run `sqlite3` errors on main-thread cache reads (`get_trend`/`sensors_in`) — spec only required open-time corruption recovery.

---

### Task 1: Contained-region pruning in `SensorCache`

The spec's pruning section says "drop regions fully contained in newer same-filter regions"; the shipped `_prune_locked` (`airloom/cache.py:181`) only caps the region count at 50 and expires 24 h rows. Implement the containment prune.

**Files:**
- Modify: `airloom/cache.py:181-189` (`_prune_locked`)
- Test: `tests/test_cache.py`

**Interfaces:**
- Consumes: existing `SensorCache.store_fetch` (calls `_prune_locked` on every store), `Bounds`, `bounds_contains` (already imported in `cache.py`).
- Produces: no new public interface — behavior change only.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cache.py` (reuse the existing `CacheBase`, `row`, `bounds_around` fixtures):

```python
class ContainedRegionPruneTest(CacheBase):
    def _region_count(self):
        return self.cache._db.execute("SELECT COUNT(*) FROM regions").fetchone()[0]

    def test_older_contained_region_is_dropped(self):
        small = bounds_around(45.5, -122.6, 10.0)
        big = bounds_around(45.5, -122.6, 50.0)
        self.cache.store_fetch(small, "outdoor", [row(1)], 1754680000)
        self.clock.now += 10
        self.cache.store_fetch(big, "outdoor", [row(2, lat=45.6)], 1754680010)
        self.assertEqual(self._region_count(), 1)
        # The survivor is the big region and it still serves the small view.
        region = self.cache.covering_region(small, "outdoor", max_age=120)
        self.assertEqual(region.bounds, big)

    def test_different_filter_is_not_pruned(self):
        small = bounds_around(45.5, -122.6, 10.0)
        big = bounds_around(45.5, -122.6, 50.0)
        self.cache.store_fetch(small, "indoor", [], 1754680000)
        self.clock.now += 10
        self.cache.store_fetch(big, "outdoor", [], 1754680010)
        self.assertEqual(self._region_count(), 2)

    def test_newer_contained_region_survives(self):
        # A newer, smaller fetch inside an older big one must NOT be dropped:
        # it carries the fresher api_time_stamp for its area.
        big = bounds_around(45.5, -122.6, 50.0)
        small = bounds_around(45.5, -122.6, 10.0)
        self.cache.store_fetch(big, "outdoor", [], 1754680000)
        self.clock.now += 10
        self.cache.store_fetch(small, "outdoor", [], 1754680010)
        self.assertEqual(self._region_count(), 2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cache.ContainedRegionPruneTest -v`
Expected: `test_older_contained_region_is_dropped` FAILS (count is 2, not 1); the other two may already pass — they lock the boundaries of the new behavior.

- [ ] **Step 3: Implement**

In `airloom/cache.py`, extend `_prune_locked` (keep the existing count-cap and age lines):

```python
    def _prune_locked(self, now: float) -> None:
        # Newest-first walk: drop any region fully contained in a newer
        # region with the same filter — the newer one serves every view the
        # older one could, with a fresher api_time_stamp.
        rows = self._db.execute(
            "SELECT id, north, west, south, east, location_filter FROM regions"
            " ORDER BY fetched_at DESC, id DESC"
        ).fetchall()
        kept: list[tuple[Bounds, str]] = []
        doomed: list[int] = []
        for region_id, north, west, south, east, mode in rows:
            bounds = Bounds(north, west, south, east)
            if any(mode == kept_mode and bounds_contains(kept_bounds, bounds)
                   for kept_bounds, kept_mode in kept):
                doomed.append(region_id)
            else:
                kept.append((bounds, mode))
        if doomed:
            marks = ",".join("?" for _ in doomed)
            self._db.execute(f"DELETE FROM regions WHERE id IN ({marks})", doomed)
        self._db.execute(
            "DELETE FROM regions WHERE id NOT IN"
            " (SELECT id FROM regions ORDER BY fetched_at DESC LIMIT ?)",
            (MAX_REGIONS,),
        )
        cutoff = now - SENSOR_MAX_AGE
        self._db.execute("DELETE FROM sensors WHERE fetched_at < ?", (cutoff,))
        self._db.execute("DELETE FROM trends WHERE fetched_at < ?", (cutoff,))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_cache -v` — all pass, including the pre-existing `PruneTest`.

- [ ] **Step 5: Commit**

```bash
git add airloom/cache.py tests/test_cache.py
git commit -m "cache: prune regions contained in newer same-filter regions"
```

---

### Task 2: `SensorCache` hygiene — close pre-recovery handle, delete dead `clear()`

Two Task-4 leftovers: `_connect` (`airloom/cache.py:71`) leaks the first sqlite handle when corruption recovery kicks in, and `clear()` (`airloom/cache.py:210`) is called by nothing (app and tests both use file deletion / fresh instances). The final review's ruling: close the handle, delete the method.

**Files:**
- Modify: `airloom/cache.py:71-82` (`_connect`), `airloom/cache.py:210-215` (delete `clear`)
- Test: `tests/test_cache.py` (existing `CorruptionTest` covers the recovery path; no new test needed)

**Interfaces:**
- Consumes: nothing new.
- Produces: `SensorCache.clear` no longer exists — verify nothing references it (`grep -rn "\.clear()" airloom/ tests/` must return no SensorCache hits) before deleting.

- [ ] **Step 1: Implement both changes**

In `_connect`, close the failed handle before recreating:

```python
    def _connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            self._db.executescript(_SCHEMA)
            self._db.commit()
        except sqlite3.Error:
            # It's only a cache: a corrupt file is deleted, never repaired.
            try:
                self._db.close()
            except (sqlite3.Error, AttributeError):
                pass
            self.path.unlink(missing_ok=True)
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            self._db.executescript(_SCHEMA)
            self._db.commit()
```

Delete the entire `clear()` method.

- [ ] **Step 2: Verify**

Run: `grep -rn "clear()" airloom/ tests/ | grep -v unhide` — no SensorCache references.
Run: `python3 -m unittest tests.test_cache -v` — all pass (CorruptionTest exercises the amended path).

- [ ] **Step 3: Commit**

```bash
git add airloom/cache.py
git commit -m "cache: close pre-recovery handle, drop unused clear()"
```

---

### Task 3: Show the readings' age in the offline fallback banner

The spec's error-handling section promised "Couldn't reach PurpleAir — showing readings from N min ago"; the shipped banner (`airloom/app.py:736`) says only "Showing cached readings." Plumb the serving region's age through.

**Files:**
- Modify: `airloom/app.py:728-739` (the `except PurpleAirError` branch inside the `_start_fetch` worker)

**Interfaces:**
- Consumes: existing `SensorCache.covering_region(bounds, location_filter, max_age=None)` → `Region | None` (any age when `max_age` omitted), `self.cache.clock()`, module helper `_age_label(seconds)` ("just now" / "N min ago").
- Produces: no new interface.

- [ ] **Step 1: Implement**

Replace the stale-cache branch body:

```python
                    except PurpleAirError as exc:
                        stale = [s for s in map(sensor_from_values, self.cache.sensors_in(bounds))
                                 if s is not None]
                        if stale:
                            # Stale real readings beat fake ones; demo only when
                            # the cache has nothing for this area.
                            sensors = stale
                            source = "PurpleAir · cached"
                            region = self.cache.covering_region(bounds, mode)
                            if region is not None:
                                age = _age_label(self.cache.clock() - region.fetched_at)
                                error = f"{exc} Showing cached readings from {age}."
                            else:
                                error = f"{exc} Showing cached readings."
                        else:
                            sensors = _filter_demo(demo_sensors(center[0], center[1]), mode)
                            source = "Demo data"
                            error = f"{exc} Showing demo readings instead."
```

(`_age_label` yields "just now" for <90 s, so the sentence reads "…readings from just now." — acceptable; do not special-case it.)

- [ ] **Step 2: Verify and commit**

Run: `make check` — clean.

```bash
git add airloom/app.py
git commit -m "app: include cache age in the offline fallback banner"
```

---

### Task 4: Preserve the force flag when coalescing fetches

`_start_fetch`'s coalescing (`airloom/app.py:698-701`) overwrites `pending_fetch` wholesale, so a queued force refresh (user clicked refresh while a fetch was in flight) is silently downgraded by a later non-force request (e.g. a pan). Once a force is queued, it must survive.

**Files:**
- Modify: `airloom/app.py:698-701`

**Interfaces:** none new.

- [ ] **Step 1: Implement**

```python
        if self.refreshing:
            # Coalesce: the newest request wins and runs when the current
            # lands — but a queued force refresh must not be downgraded by a
            # later passive request.
            if self.pending_fetch is not None:
                force = force or self.pending_fetch[3]
            self.pending_fetch = (bounds, center, include_favorites, force)
            return
```

- [ ] **Step 2: Verify and commit**

Run: `make check` — clean.

```bash
git add airloom/app.py
git commit -m "app: keep a queued force refresh forced when coalescing"
```

---

### Task 5: Filter and star the startup cache paint

`_paint_cached_home` (`airloom/app.py:675-685`) paints every cached sensor in the home bounds — ignoring the indoor/outdoor filter and favorite stars — leaving a one-frame flicker until the follow-up refresh corrects it. Apply both at paint time.

**Files:**
- Modify: `airloom/app.py:675-685`

**Interfaces:**
- Consumes: `Sensor.indoor: bool`, `Sensor.favorite: bool` (set post-construction, matching `_finish_refresh`'s pattern).

- [ ] **Step 1: Implement**

```python
    def _paint_cached_home(self) -> None:
        """First paint from the cache so launch never blocks on the network.
        The refresh that follows replaces it under the normal TTL rules."""
        if self.sensors or not self.store.data.get("api_key"):
            return
        config = self.store.data
        bounds = bounds_around(config["latitude"], config["longitude"], config["radius_km"])
        cached = [s for s in map(sensor_from_values, self.cache.sensors_in(bounds)) if s is not None]
        mode = config.get("location_filter", "outdoor")
        if mode != "both":
            cached = [s for s in cached if s.indoor == (mode == "indoor")]
        favorites = set(config.get("favorites", []))
        for sensor in cached:
            sensor.favorite = sensor.sensor_id in favorites
        if cached:
            self.sensors = cached
            self._send_sensor_state("PurpleAir · cached")
```

- [ ] **Step 2: Verify and commit**

Run: `make check` — clean.

```bash
git add airloom/app.py
git commit -m "app: apply location filter and stars to the startup cache paint"
```

---

### Task 6: Surface trend-fetch failures instead of loading forever

A failed lazy trend fetch (`_finish_trend` receiving `None`, `airloom/app.py:805`) currently leaves the chart on "Loading trend…" with no feedback; only re-selecting retries. Emit the app's standard error toast so the user knows what happened; the chart keeps its state and re-select still retries.

**Files:**
- Modify: `airloom/app.py:795-810` (`_ensure_trend`'s worker + `_finish_trend`)

**Interfaces:**
- Consumes: existing `self._send("error", {"message": ...})` toast channel.

- [ ] **Step 1: Implement**

In `_finish_trend`, handle the failure branch (currently `if trend:` silently drops `None`):

```python
    def _finish_trend(self, sensor_id: int, trend: list | None) -> bool:
        if trend:
            self.cache.store_trend(sensor_id, trend)
            sensor = next((s for s in self.sensors if s.sensor_id == sensor_id), None)
            if sensor is not None:
                sensor.trend = trend
                self._send_sensor_state()
        elif sensor_id == self.selected_id:
            # Only bother the user about the sensor they're still looking at.
            self._send("error", {"message": "Couldn't load this sensor's trend. Select it again to retry."})
        return GLib.SOURCE_REMOVE
```

Note: the worker delivers `None` both on `PurpleAirError` and when the API returned no rows (`trend` starts as `None` and is only assigned from a non-empty `result.rows`), so the `elif` branch covers every failure shape.

- [ ] **Step 2: Verify and commit**

Run: `make check` — clean.

```bash
git add airloom/app.py
git commit -m "app: toast when a trend fetch fails instead of loading forever"
```

---

### Task 7: Final verification

**Files:** none (verification only).

- [ ] **Step 1: Full check**

Run: `make check` — full suite + compileall + node --check, all clean.

- [ ] **Step 2: Optional live smoke test** (needs display + API key)

```bash
AIRLOOM_DEBUG_SOCKET="$XDG_RUNTIME_DIR/airloom-debug.sock" scripts/debug-run &
sleep 6
scripts/debug-client state | head -c 400   # source label sane; startup paint filtered/starred
scripts/debug-client quit
```

- [ ] **Step 3: Update CHANGELOG**

Add under a new `## Unreleased` heading at the top of `CHANGELOG.md`:

```markdown
## Unreleased

- Caching polish: the offline banner now shows how old the cached readings are,
  a queued manual refresh is no longer downgraded by a later automatic one, the
  startup map paint respects the indoor/outdoor filter and favorite stars, and a
  failed trend load shows an error instead of loading forever.
```

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for caching polish"
```
