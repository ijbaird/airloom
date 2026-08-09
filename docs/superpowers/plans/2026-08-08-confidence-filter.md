# Sensor Confidence Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hide sensors whose PurpleAir A/B channel-agreement `confidence` score is below 90, on by default with a settings switch, and version the SQLite cache so pre-feature rows (which lack the field) are cleared on upgrade.

**Architecture:** The `confidence` field is added to the PurpleAir field lists, flows into the existing raw-row SQLite cache untouched, and lands on the `Sensor` dataclass. Filtering happens only at display time in `app.py` (`_send_sensor_state` / `_check_alerts`), exactly like hidden sensors, so toggling the setting never costs an API call. The setting is a bool in `store.py` config; the UI is one checkbox in the existing settings dialog. `cache.py` gains `PRAGMA user_version`-based schema versioning that wipes stale caches.

**Tech Stack:** Python 3 stdlib only (unittest, sqlite3), vanilla JS. **No new dependencies — this project is deliberately zero-third-party.**

**Spec:** `docs/superpowers/specs/2026-08-08-confidence-filter-design.md`

## Global Constraints

- Zero third-party dependencies (Python stdlib + system PyGObject; hand-written JS). Never add pip packages or JS libraries.
- Tests are pure-stdlib `unittest`, GTK-free, and must run without a display: `make test`.
- Before finishing: `make check` (tests + `compileall` + `node --check` on app.js) must pass.
- Threshold is fixed at 90; the user-facing setting is a boolean only, default **on**.
- Missing confidence **fails open** (sensor stays visible): covers demo sensors, rows cached before the field existed, and API omissions.
- Commit after each task; commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: `Sensor.confidence` field and `passes_confidence` predicate

**Files:**
- Modify: `airloom/models.py`
- Test: `tests/test_models.py` (new file)

**Interfaces:**
- Consumes: nothing new.
- Produces: `Sensor.confidence: int | None = None` (new dataclass field); `CONFIDENCE_THRESHOLD = 90` and `passes_confidence(sensor: Sensor, enabled: bool) -> bool` in `airloom.models`. Task 2 sets `confidence=` in `sensor_from_values`; Task 5 imports the predicate in `app.py`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_models.py`:

```python
import unittest

from airloom.models import CONFIDENCE_THRESHOLD, Sensor, passes_confidence


def sensor(confidence=None):
    return Sensor(sensor_id=1, name="S1", latitude=45.5, longitude=-122.6,
                  aqi=40, pm25=9.6, confidence=confidence)


class PassesConfidenceTest(unittest.TestCase):
    def test_threshold_is_ninety(self):
        self.assertEqual(CONFIDENCE_THRESHOLD, 90)

    def test_disabled_filter_passes_everything(self):
        for value in (None, 0, 30, 89, 90, 100):
            self.assertTrue(passes_confidence(sensor(value), False))

    def test_enabled_filter_blocks_below_threshold(self):
        self.assertFalse(passes_confidence(sensor(0), True))
        self.assertFalse(passes_confidence(sensor(89), True))
        self.assertTrue(passes_confidence(sensor(90), True))
        self.assertTrue(passes_confidence(sensor(100), True))

    def test_missing_confidence_fails_open(self):
        # Demo sensors and rows cached before the field existed have None.
        self.assertTrue(passes_confidence(sensor(None), True))

    def test_confidence_reaches_the_web_payload(self):
        self.assertEqual(sensor(97).to_dict()["confidence"], 97)
        self.assertIsNone(sensor().to_dict()["confidence"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_models -v`
Expected: ImportError — `CONFIDENCE_THRESHOLD`/`passes_confidence` don't exist yet.

- [ ] **Step 3: Implement in `airloom/models.py`**

Add `confidence` to the `Sensor` dataclass after the `pm10` field (before `last_seen`, keeping data fields together — exact position is not behavior-relevant since all these fields are keyword-defaulted):

```python
    pm10: float | None = None
    confidence: int | None = None
    last_seen: int | None = None
```

Append at module level (after the `Sensor` class):

```python
CONFIDENCE_THRESHOLD = 90


def passes_confidence(sensor: Sensor, enabled: bool) -> bool:
    """Display-time filter for poor A/B channel agreement. Fails open on a
    missing score (demo sensors, rows cached before the field existed)."""
    if not enabled or sensor.confidence is None:
        return True
    return sensor.confidence >= CONFIDENCE_THRESHOLD
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_models -v`
Expected: all PASS.

Also run the full suite to catch dataclass fallout: `make test` — expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add airloom/models.py tests/test_models.py
git commit -m "feat: Sensor.confidence field and passes_confidence predicate

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Request and parse `confidence` from the PurpleAir API

**Files:**
- Modify: `airloom/purpleair.py` (`MAP_FIELDS`, `DATA_FIELDS`, `sensor_from_values`)
- Test: `tests/test_purpleair.py`

**Interfaces:**
- Consumes: `Sensor.confidence` from Task 1.
- Produces: `"confidence"` present in `purpleair.MAP_FIELDS` and `purpleair.DATA_FIELDS`; `sensor_from_values(values)` populates `Sensor.confidence` via `_integer(values.get("confidence"))`. Nothing else changes — the cache stores raw field dicts, so the new field flows through `cache.py` untouched.

- [ ] **Step 1: Write the failing tests**

In `tests/test_purpleair.py`, add to the existing `FieldListTest` class:

```python
    def test_confidence_is_requested_in_area_and_delta_fetches(self):
        self.assertIn("confidence", purpleair.MAP_FIELDS)
        self.assertIn("confidence", purpleair.DATA_FIELDS)
```

Add to the existing `SensorFromValuesTest` class:

```python
    def test_confidence_is_parsed(self):
        base = {"sensor_index": 7, "latitude": 45.5, "longitude": -122.6}
        self.assertEqual(purpleair.sensor_from_values({**base, "confidence": 97}).confidence, 97)
        self.assertEqual(purpleair.sensor_from_values({**base, "confidence": "88"}).confidence, 88)
        self.assertIsNone(purpleair.sensor_from_values(base).confidence)
        self.assertIsNone(purpleair.sensor_from_values({**base, "confidence": "junk"}).confidence)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_purpleair.FieldListTest tests.test_purpleair.SensorFromValuesTest -v`
Expected: the two new tests FAIL (`confidence` not in field tuples; attribute is None for 97).

- [ ] **Step 3: Implement in `airloom/purpleair.py`**

Add `"confidence"` to both field tuples (after `"pm10.0"` in each — order within the tuple is cosmetic; PurpleAir returns a `fields` header that `parse_rows` zips dynamically):

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
    "confidence",
)
DATA_FIELDS = (
    "sensor_index",
    "last_seen",
    "humidity",
    "temperature",
    "pm1.0",
    "pm2.5_cf_1",
    "pm10.0",
    "confidence",
)
```

In `sensor_from_values`, add the keyword argument to the `Sensor(...)` construction (alongside the other field reads):

```python
        confidence=_integer(values.get("confidence")),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_purpleair -v`
Expected: all PASS, including pre-existing tests (`test_data_fields_exclude_metadata` still holds — `confidence` is not metadata).

- [ ] **Step 5: Commit**

```bash
git add airloom/purpleair.py tests/test_purpleair.py
git commit -m "feat: fetch and parse PurpleAir confidence score

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Cache schema versioning via `PRAGMA user_version`

**Files:**
- Modify: `airloom/cache.py` (`_connect`, new `SCHEMA_VERSION` constant)
- Test: `tests/test_cache.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (independent).
- Produces: `cache.SCHEMA_VERSION = 1`. `SensorCache._connect` clears all tables when the on-disk `user_version` differs and stamps the current version. Existing user databases are at `user_version` 0, so the first launch of this release starts empty and refetches — after which every cached row carries `confidence`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_cache.py`, add a new test class (module already imports `cache`, `Path`, `bounds_around`, and defines `row`/`FakeClock`/`CacheBase`):

```python
class SchemaVersionTest(CacheBase):
    def _user_version(self):
        return self.cache._db.execute("PRAGMA user_version").fetchone()[0]

    def test_fresh_database_is_stamped_with_current_version(self):
        self.assertEqual(self._user_version(), cache.SCHEMA_VERSION)

    def test_stale_version_clears_all_tables(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1)], 1754680000)
        self.cache.store_trend(1, [{"label": "Now", "aqi": 40}])
        self.cache._db.execute("PRAGMA user_version = 0")  # simulate pre-feature DB
        self.cache._db.commit()
        reopened = cache.SensorCache(Path(self.tmp.name) / "cache.db", clock=self.clock)
        self.assertEqual(reopened.sensors_in(bounds), [])
        self.assertIsNone(reopened.covering_region(bounds, "outdoor"))
        self.assertIsNone(reopened.get_trend(1, max_age=3600))
        self.assertEqual(reopened._db.execute("PRAGMA user_version").fetchone()[0],
                         cache.SCHEMA_VERSION)

    def test_current_version_keeps_rows_across_reconnect(self):
        bounds = bounds_around(45.5, -122.6, 20.0)
        self.cache.store_fetch(bounds, "outdoor", [row(1)], 1754680000)
        reopened = cache.SensorCache(Path(self.tmp.name) / "cache.db", clock=self.clock)
        self.assertEqual(len(reopened.sensors_in(bounds)), 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_cache.SchemaVersionTest -v`
Expected: `test_fresh_database_is_stamped_with_current_version` and `test_stale_version_clears_all_tables` FAIL (`cache.SCHEMA_VERSION` doesn't exist → AttributeError).

- [ ] **Step 3: Implement in `airloom/cache.py`**

Add after the existing `SENSOR_MAX_AGE` constant:

```python
# Bump when cached rows become unusable (e.g. a newly-requested field that
# delta polls would never backfill for unchanged sensors); a mismatched
# cache is cleared on connect and simply refetched.
SCHEMA_VERSION = 1
```

In `_connect`, factor the setup so both the normal path and the corrupt-file fallback stamp the version. Replace the method body with:

```python
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
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._db.commit()
```

(`PRAGMA user_version = ?` does not accept bound parameters; the f-string interpolates a module constant int, not user input.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_cache -v`
Expected: all PASS, including all pre-existing cache tests.

- [ ] **Step 5: Commit**

```bash
git add airloom/cache.py tests/test_cache.py
git commit -m "feat: version the sensor cache schema, clearing stale caches

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: `confidence_filter` config setting in the store

**Files:**
- Modify: `airloom/store.py` (`DEFAULT_CONFIG`, `_sanitize`, `public_config`)
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (independent).
- Produces: `store.data["confidence_filter"]: bool`, default `True`; only genuine JSON booleans survive `_sanitize` (anything else resets to `True`); `public_config()["confidence_filter"]` exposes it to the web UI. Task 5 reads and writes this key.

- [ ] **Step 1: Write the failing tests**

In `tests/test_store.py`, add to the existing `StoreTest` class (module already imports `json`, `tempfile`, `Path`, `Store`):

```python
    def test_confidence_filter_defaults_on_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            self.assertTrue(store.data["confidence_filter"])
            self.assertTrue(store.public_config()["confidence_filter"])
            store.data["confidence_filter"] = False
            store.save()
            loaded = Store(path)
            self.assertFalse(loaded.data["confidence_filter"])
            self.assertFalse(loaded.public_config()["confidence_filter"])

    def test_confidence_filter_rejects_non_bool(self):
        for bad in ("yes", 1, 0, None, [], {}):
            with self.subTest(bad=bad):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "config.json"
                    path.write_text(json.dumps({"confidence_filter": bad}), encoding="utf-8")
                    self.assertTrue(Store(path).data["confidence_filter"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_store -v`
Expected: `test_confidence_filter_defaults_on_and_round_trips` FAILS with KeyError `'confidence_filter'`.

- [ ] **Step 3: Implement in `airloom/store.py`**

In `DEFAULT_CONFIG`, add after `"refresh_minutes": 2,`:

```python
    "confidence_filter": True,
```

In `_sanitize`, add (near the other scalar checks, e.g. after the `refresh_minutes` block):

```python
    if isinstance(data.get("confidence_filter"), bool):
        clean["confidence_filter"] = data["confidence_filter"]
```

In `public_config()`, add to the returned dict after `"refresh_minutes"`:

```python
            "confidence_filter": self.data["confidence_filter"],
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_store -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add airloom/store.py tests/test_store.py
git commit -m "feat: confidence_filter preference, default on

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Apply the filter in the app and expose the settings toggle

**Files:**
- Modify: `airloom/app.py` (`_send_sensor_state`, `_check_alerts`, `_save_settings`, imports)
- Modify: `airloom/resources/index.html` (settings dialog form)
- Modify: `airloom/resources/app.js` (`openSettings`, settings submit handler)
- Test: none unit-testable (GTK/webview layer — the repo's tests cover only GUI-free modules); verified via `make check` and a live run.

**Interfaces:**
- Consumes: `passes_confidence` + `CONFIDENCE_THRESHOLD` (Task 1), `Sensor.confidence` populated by `sensor_from_values` (Task 2), `store.data["confidence_filter"]` / `public_config()` (Task 4).
- Produces: the user-visible feature. `save-settings` bridge messages now carry `confidence_filter: bool`.

- [ ] **Step 1: Wire the filter into `airloom/app.py`**

Find the import of models symbols near the top of `app.py` (`grep -n "from .models" airloom/app.py`) and extend it to include `passes_confidence`:

```python
from .models import Sensor, passes_confidence
```

(If the existing import line differs, keep its symbols and append `passes_confidence`.)

In `_send_sensor_state` (around `airloom/app.py:814`), replace:

```python
        hidden = self.store.hidden_ids()
        visible = [sensor for sensor in self.sensors if sensor.sensor_id not in hidden]
```

with:

```python
        hidden = self.store.hidden_ids()
        confidence_on = bool(self.store.data.get("confidence_filter", True))
        visible = [
            sensor for sensor in self.sensors
            if sensor.sensor_id not in hidden and passes_confidence(sensor, confidence_on)
        ]
```

The selection-reconciliation lines directly below already handle a filtered-out selected sensor — do not touch them.

In `_check_alerts` (around `airloom/app.py:829`), replace:

```python
        hidden = self.store.hidden_ids()
        for sensor in self.sensors:
            if not sensor.favorite or sensor.aqi is None or sensor.sensor_id in hidden:
                continue
```

with:

```python
        hidden = self.store.hidden_ids()
        confidence_on = bool(self.store.data.get("confidence_filter", True))
        for sensor in self.sensors:
            if not sensor.favorite or sensor.aqi is None or sensor.sensor_id in hidden:
                continue
            if not passes_confidence(sensor, confidence_on):
                continue
```

In `_save_settings` (around `airloom/app.py:623`), add to the `updates` dict literal:

```python
                "confidence_filter": bool(message.get("confidence_filter")),
```

The existing tail of `_save_settings` (`self.store.save()`, `self._send("config", ...)`, `self.refresh()`) already persists and re-renders; inside the TTL the refresh serves from cache, so toggling costs no API call.

- [ ] **Step 2: Add the checkbox to `airloom/resources/index.html`**

In the settings form's `.form-grid` (around `index.html:152`), directly after the Temperature fieldset and before the "Hidden sensors" fieldset, insert:

```html
          <label class="checkbox wide"><input name="confidence_filter" type="checkbox"><span>Hide low-confidence sensors</span><small>Below 90% agreement between the sensor's two channels</small></label>
```

(Same `checkbox wide` pattern as the `clear_api_key` row at `index.html:132`.)

- [ ] **Step 3: Wire the checkbox in `airloom/resources/app.js`**

In `openSettings` (around `app.js:699`), after the `form.elements.clear_api_key.checked = false;` line, add:

```js
    form.elements.confidence_filter.checked = config.confidence_filter !== false;
```

(`!== false` so the box is checked in browser-preview mode, where `state.config` has no `confidence_filter` key — matching the Python default of on.)

In the `#settings-form` submit handler (around `app.js:1016`), add to the `bridge({ action: "save-settings", ... })` object, alongside `clear_api_key`:

```js
      confidence_filter: form.get("confidence_filter") === "on",
```

- [ ] **Step 4: Run the full check suite**

Run: `make check`
Expected: all tests PASS, `compileall` clean, `node --check` clean.

- [ ] **Step 5: Verify live via the debug port**

```bash
AIRLOOM_DEBUG_SOCKET="$XDG_RUNTIME_DIR/airloom-debug.sock" scripts/debug-run &
sleep 6
scripts/debug-client state | head -c 600   # config should show "confidence_filter": true
scripts/debug-client quit
```

Expected: the state snapshot's config carries `confidence_filter: true`; with a live API key, sensors with `confidence < 90` are absent from the items list, and unchecking the settings box brings them back. (Without an API key, demo sensors all lack confidence and remain visible — that is the fail-open behavior, not a bug.)

- [ ] **Step 6: Commit**

```bash
git add airloom/app.py airloom/resources/index.html airloom/resources/app.js
git commit -m "feat: hide low-confidence sensors by default, with settings toggle

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Documentation

**Files:**
- Modify: `CLAUDE.md` (architecture notes), `CHANGELOG.md` (unreleased section)

**Interfaces:**
- Consumes: the finished feature (Tasks 1–5).
- Produces: docs only; no code.

- [ ] **Step 1: Update `CLAUDE.md`**

In the Architecture section's cache paragraph (the one starting "**Data flow**"), after the sentence about demo data never being cached, add one sentence:

```
Sensors with a PurpleAir `confidence` score (A/B channel agreement) below 90 are hidden at display time by default — `passes_confidence` in `models.py`, toggled by the `confidence_filter` preference; missing scores fail open. `cache.py`'s `SCHEMA_VERSION` (PRAGMA user_version) clears the whole cache on mismatch so stale rows without newly-requested fields never linger.
```

- [ ] **Step 2: Update `CHANGELOG.md`**

Check the file's existing format (`head -30 CHANGELOG.md`) and add matching entries under an Unreleased heading (create the heading in the file's own style if absent):

```markdown
- Hide sensors with poor A/B channel agreement (PurpleAir confidence below 90%) by default, with a preferences toggle to show them again.
- The sensor cache is now schema-versioned and clears itself on upgrade.
```

- [ ] **Step 3: Run `make check` one final time**

Run: `make check`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md CHANGELOG.md
git commit -m "docs: describe the confidence filter and cache schema versioning

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
