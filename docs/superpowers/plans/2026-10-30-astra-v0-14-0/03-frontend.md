# W3: Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the eight frontend Important findings (5, 6, 14, 15, 16, 17, 18, 21), the five frontend Minors, and the deferred fixed-home search styling, each test-first, without changing any band or accent background color.
**Architecture:**
- Pure rules go in the UMD `core.js`, so Node and direct-file preview share them: the band table, the freshness mirror, and per-reading provenance.
- `app.js` applies those rules to every reading surface, and gains keyboard panning, tile retry, accessible trend values, and honest preview settings.
- The live harness (`scripts/test-ui-regression`) gets real X11 keyboard input, a real fixture-driven trend failure, a layout matrix extended to details, long labels, and themes, and explicit completion conditions in place of fixed sleeps.
**Tech Stack:** Vanilla JS/CSS/HTML (no build), Node 22 `node --test` with the existing fake-DOM VM harness, Python stdlib `unittest`, real WebKitGTK 6 through the debug port, `ctypes` + libX11/libXtst for real key input under Xvfb.
**Spec:** docs/superpowers/specs/2026-10-30-astra-review-v0-14-0-design.md

## Global Constraints

- Ownership: W3 edits only `airloom/resources/{index.html,app.css,app.js,core.js,state.js}`, the band `foreground` values in `airloom/aqi.py` (nothing else in that file), `scripts/test-ui-regression`, `scripts/test-heatmap`, `scripts/test-save-refresh`, `tests/js/*`, `tests/test_aqi.py` (new contrast class only), and, in `tests/test_integration.py`, only `REQUIRED_COVERAGE` and `UiRegressionScriptContractTest`.
- Do not edit `window.Airloom.receive` (`app.js:89–103`). W2 owns the shell-recovery receive path and handshake there. Rebase around it at integration.
- Do not touch `tests/gi_stub.py`, `tests/record_handoff.py`, `README.md`, `CHANGELOG.md`, AppStream, screenshots, or `.github/`.
- "The six EPA band background colors (`#35b779`, `#f6c945`, `#f39c3d`, `#e65b65`, `#9b6bc3`, `#7e394d`), the unavailable gray, and the accent color all stay exactly as they are." The unavailable gray is `#7d8590`. The accent is `#16805c` light and `#54c492` dark.
- Contrast: "≥4.5:1 for normal text and ≥3:1 for large text (≥24px, or ≥18.66px bold)". Update `foreground` identically in `aqi.py` and `core.js`.
- Badges: **Demo**, **Stale** (the existing `stale` flag), **Old** (fails the shared freshness rule). Live and in-interval-cached readings get no badge. The "Cached data" label goes away everywhere.
- Freshness rule (W1, `airloom/models.py`): fresh iff `last_seen` is a finite number and `-600 <= now - last_seen <= 7200`, both inclusive. Missing is not fresh. Units are epoch seconds. The page's clock is `Date.now() / 1000`.
- No new runtime dependencies, no build step, direct-file preview keeps working. No live PurpleAir, Nominatim, or OSM requests in tests.
- Every task's regression fails on `f1f811d` through the real failing path, then passes. Never weaken an existing assertion. The provenance assertions that change below change because of user decision 4, and each one is listed.
- `make check PYTHON="python3 -W error::ResourceWarning"` passes. Live scripts run as a non-root user under a private headless display.

## Review Focus

- **Undated or future-dated readings looking fresh.** `last_seen: null`, or more than 600 s in the future on the page clock, must show **Old** on every surface, and the age text must not say "just now". Pinned in Task 2: `missing and far-future observation times are Old on every surface`.
- **Per-batch labels leaking across collections.** A remote favorite must not inherit the area's `stale` or demo `source`, and an area reading must not lose them. Pinned in Task 2 by the three mixed-origin fixtures.
- **Arrow and +/- keys hijacked outside the map.** This covers focus in search, a focused marker or popup control, an open Preferences dialog, and Ctrl/Alt/Meta chords. Pinned in Task 6: `map keys are ignored outside the focused map, with modifiers, and while Preferences is open`.
- **Badges and ages going wrong with time and no refetch.** A popup or detail left open past the 2-hour boundary must flip to **Old** on the minute tick. Pinned in Task 3: `the minute tick flips a reading to Old across the 2-hour boundary`.
- **Tile retry storms, or losing the last usable imagery.** Backoff must cap at 60 s, use one timer per layer, and stop when the layer is replaced. Retired imagery stays until a full replacement loads. Pinned in Task 7: `tile retry backoff is capped and a zoom change cancels the old layer's retry`.

---

### Task 1: Accessible foregrounds on unchanged band and accent colors (finding #6)

**Files:**
- Modify `airloom/aqi.py:113-114` (Unhealthy and Very unhealthy foreground) and `:151` (Unavailable foreground). Change only the `foreground` string arguments.
- Modify `airloom/resources/core.js:56-57,60` (same three foregrounds). Export `BANDS` and `UNAVAILABLE` from the factory's return at `:84`.
- Modify `airloom/resources/index.html:104` to `<span class="aqi-number aqi-badge" id="aqi-number">—</span>`.
- Modify `airloom/resources/app.js:526-527` (`renderDetail`).
- Modify `airloom/resources/app.css`: `:root` (add `--on-accent`), `:147` (`.aqi-number`), `:188` (`.shell-status button`), `:211` (`.dialog-actions .primary`), `:224` (dark `:root`).
- Test: `tests/test_aqi.py` (new class `BandContrastTest`), `tests/js/core.test.js`, `tests/js/state.test.js`.

**Interfaces:**
- Produces the new band foregrounds. All are WCAG 2.x ratios against the unchanged background. They are used as the text color on the map marker, sensor-row badge, popup AQI, summary score, detail AQI badge (all via `sensor.foreground`, or `band.foreground` for the summary), and in `Sensor.to_dict()["foreground"]`. Notifications don't use `foreground`.

  | Band | Background | Old fg → new fg | Ratio |
  |---|---|---|---|
  | Good | `#35b779` | `#08271b` (unchanged) | 6.23 |
  | Moderate | `#f6c945` | `#332400` (unchanged) | 9.59 |
  | Unhealthy for sensitive groups | `#f39c3d` | `#341900` (unchanged) | 7.50 |
  | Unhealthy | `#e65b65` | `#ffffff` → **`#2a0509`** | 3.47 → 5.38 |
  | Very unhealthy | `#9b6bc3` | `#ffffff` → **`#12061c`** | 3.97 → 4.95 |
  | Hazardous | `#7e394d` | `#ffffff` (unchanged) | 8.14 |
  | Unavailable | `#7d8590` | `#ffffff` → **`#0b0e11`** | 3.73 → 5.19 |
- Produces the CSS variable `--on-accent`: `#ffffff` in light (on `#16805c`: 4.91) and **`#0b2a1d`** in dark (on `#54c492`: 7.11, was 2.17). It's used by `.dialog-actions .primary` (Save & refresh) and `.shell-status button` (Reload).
- Produces `AirloomCore.BANDS` and `AirloomCore.UNAVAILABLE`, which Task 2 and the tests consume.
- No existing test asserts that the Python and JS tables match. This task adds one.

- [ ] **Step 1: Write the failing tests**
  - `tests/test_aqi.py::BandContrastTest`, with a module helper `_contrast(a, b)` using sRGB relative luminance:
    - `test_band_colors_are_unchanged`: `[b.color for b in BANDS] == ["#35b779", "#f6c945", "#f39c3d", "#e65b65", "#9b6bc3", "#7e394d"]` and `band_for_aqi(None).color == "#7d8590"`.
    - `test_band_foregrounds_meet_normal_text_contrast`: `[b.foreground for b in BANDS] + [band_for_aqi(None).foreground] == ["#08271b", "#332400", "#341900", "#2a0509", "#12061c", "#ffffff", "#0b0e11"]`. Every pair has `_contrast(color, foreground) >= 4.5`.
    - `test_sensor_payload_uses_band_foreground`: `Sensor(1, "x", 0, 0, 180, 70.0).to_dict()["foreground"] == "#2a0509"`.
  - `tests/js/core.test.js`:
    - `band tables match Python exactly`: run `python3 -c` with the existing `execFileSync` pattern (`cwd` = repo root) to print `json.dumps([dataclasses.asdict(b) for b in BANDS] + [dataclasses.asdict(band_for_aqi(None))])`. Then `assert.deepEqual` it against `[...AirloomCore.BANDS, AirloomCore.UNAVAILABLE]`, field for field. This is a drift guard: it passes before and after.
    - `every band foreground passes WCAG contrast in both themes`:
      - With a test-local `contrastRatio(hexA, hexB)`, every `[...BANDS, UNAVAILABLE]` pair is `>= 4.5`.
      - Parse the base `:root { … }` and the `@media (prefers-color-scheme: dark)` `:root { … }` custom properties from `app.css`. In each theme, assert `contrastRatio(--accent, --on-accent) >= 4.5` and `contrastRatio(--soft, --ink) >= 4.5` (the provenance badge pair from Task 2).
      - The `color` of `.dialog-actions .primary` and `.shell-status button` is `var(--on-accent)`.
      - The dark media block declares no `color` for `.aqi-number`, `.sensor-badge`, `.map-marker`, `.popup-aqi`, or `.summary-chip .summary-score`.
    - Update the existing `bandForAQI keeps unavailable and above-scale behavior compatible` assertion from `"#ffffff"` to `"#0b0e11"`. This is a deliberate value change, not a weakening.
  - `tests/js/state.test.js`, `detail AQI sits on a band-colored badge with the band foreground`:
    - Receive a sensor whose `color`/`foreground` come from `AirloomCore.bandForAQI(250)`, with `aqi: 250`. Assert `aqi-number.style.background === "#9b6bc3"` and `aqi-number.style.color === "#12061c"`.
    - Then receive the sensor with `aqi: null` and the unavailable colors. Assert `"#7d8590"` / `"#0b0e11"`.
    - `cssDeclaration(".aqi-number", "border-radius")` is not null.
- [ ] **Step 2: Run them and confirm they fail**
  - `python3 -m unittest tests.test_aqi.BandContrastTest -v`. Expect `Lists differ: [... '#ffffff', '#ffffff', ...] != [... '#2a0509', '#12061c', ...]`.
  - `node --test --test-name-pattern "WCAG|band-colored badge|unavailable and above-scale" tests/js/core.test.js tests/js/state.test.js`.
    - `3.47 >= 4.5` fails for Unhealthy.
    - `--on-accent` is undefined.
    - `style.color` is `"#9b6bc3"`, because the number is colored with the band color today.
- [ ] **Step 3: Implement**
  - Set the three foreground values in both tables.
  - `renderDetail`: `style.background = sensor.color || AirloomCore.UNAVAILABLE.color` and `style.color = sensor.foreground || AirloomCore.UNAVAILABLE.foreground`. Delete the `style.color = sensor.color` line.
  - `.aqi-number` keeps its font rules and adds `display: inline-grid; place-items: center; flex: none; max-width: 100%; min-width: calc(84px * var(--interface-scale)); padding: calc(6px * var(--interface-scale)) calc(14px * var(--interface-scale)); border-radius: calc(16px * var(--interface-scale)); background: var(--soft); color: var(--ink);`.
  - Add `--on-accent` to both `:root` blocks and use it in the two button rules in place of `white`.
- [ ] **Step 4: Run and confirm it passes**
  `python3 -m unittest tests.test_aqi tests.test_models -v && node --test tests/js/*.test.js`
- [ ] **Step 5: Commit**
  `git add airloom/aqi.py airloom/resources/core.js airloom/resources/index.html airloom/resources/app.js airloom/resources/app.css tests/test_aqi.py tests/js/core.test.js tests/js/state.test.js && git commit -m "fix: give AQI badges and primary actions accessible foregrounds without changing colors"`

### Task 2: Per-reading provenance badges, degraded only (finding #5)

**Files:**
- Modify `airloom/resources/core.js` (new exports).
- Modify `airloom/resources/app.js`:
  - `:302-329`: replace `summaryProvenanceLabel` and `detailProvenanceLabel`.
  - `:331-332`: `renderSummary`.
  - `:424-434`: `updateSensorRow`.
  - `:448-449`: the announcement.
  - `:479-487`: `showPopup`.
  - `:517-519`: `renderDetail`.
  - `:1014-1019`: `updateMapMarker`.
  - `:1214-1220`: `relativeTime`.
- Modify `airloom/resources/index.html`:
  - `:22`: add `<span class="provenance-badge" id="popup-provenance" hidden></span>` after `#popup-meta`.
  - `:52` and `:105`: class `provenance-status` → `provenance-badge`.
- Modify `airloom/resources/app.css:79-81`: replace the `.provenance-status` rules with `.provenance-badge`.
- Modify `scripts/test-ui-regression`: `scenario_labels:424`, `scenario_alerts:965`, `scenario_cache_fallback:1008,1013`, and a new Old check in `scenario_alerts`.
- Test: `tests/js/core.test.js`, `tests/js/state.test.js` (harness plus the provenance tests at `:1027-1124`).

**Interfaces:**
- Produces in `core.js` (exported on `AirloomCore` and `module.exports`):
  - `MAX_OBSERVATION_AGE = 7200` and `FUTURE_SKEW_TOLERANCE = 600`.
  - `isFreshObservation(lastSeen, nowSeconds) -> boolean`: `typeof lastSeen === "number" && Number.isFinite(lastSeen) && Number.isFinite(nowSeconds)`, and `-600 <= nowSeconds - lastSeen <= 7200`.
  - `readingProvenance(sensor, batch, nowSeconds) -> "" | "Demo" | "Stale" | "Old"`, where `batch = { source, stale }`. Precedence is **Demo > Stale > Old**:
    - **Demo** if `sensor.origin === "demo"`, or if `sensor.remote_favorite !== true` and the lowercased `batch.source` includes `"demo"`.
    - **Stale** if `sensor.stale === true`, or if `sensor.remote_favorite !== true` and either `batch.stale === true` or (`batch.stale === undefined` and `batch.source === "PurpleAir · cached"`, the legacy payload).
    - **Old** if `!isFreshObservation(sensor.last_seen, nowSeconds)`.
    - Otherwise `""`.
  - `PROVENANCE_DESCRIPTIONS = { Demo: "Demo data, not a real sensor reading", Stale: "Cached reading shown after a failed refresh", Old: "Last observation over 2 hours ago or unknown" }`.
- Produces in `app.js`:
  - `nowSeconds() = Date.now() / 1000`, the single page clock.
  - `provenanceOf(sensor) = AirloomCore.readingProvenance(sensor, { source: state.source, stale: state.stale }, nowSeconds())`.
  - `stampProvenance(id, label)` also sets `title` from `PROVENANCE_DESCRIPTIONS`.
- Produces these exact surface strings, with `category = sensor.category || AirloomCore.bandForAQI(sensor.aqi).label` and `aqi = sensor.aqi ?? "unavailable"`:
  - Marker `title` and `aria-label`: `` `${name} · AQI ${aqi} · ${category}${badge ? ` · ${badge}` : ""}` ``
  - Row `aria-label`: `` `${name}, AQI ${aqi}, ${category}${badge ? `, ${badge}` : ""}${favorite ? ", favorite" : ""}` ``. The visible row badge is `<span class="provenance-badge" title="…">${badge}</span>` as the last child of `.sensor-copy`.
  - Selection announcement: `` `${name}. AQI ${aqi}, ${category}.${badge ? ` ${badge}.` : ""}` ``
  - The popup (`#popup-provenance`) and the detail (`#detail-provenance`) show the reading's badge.
  - The summary (`#summary-provenance`) shows the badge of the median reading it displays, or nothing when it shows none.
- `relativeTime(timestamp, now = nowSeconds())` returns `"Update time unavailable"` when the timestamp is not finite or `timestamp - now > FUTURE_SKEW_TOLERANCE`. Otherwise it keeps today's wording.
- Consumes W1's rule (`airloom/models.py`). See Cross-workstream contracts.

- [ ] **Step 1: Write the failing tests**
  - `tests/js/state.test.js` harness:
    - `loadApp({ nowMs })` replaces the context `Date` with `class extends Date { static now() { return clock.nowMs ?? realNow(); } }` and returns `setNow(ms)`.
    - Add `const NOW_S = 1_800_000_000` and `const FRESH = NOW_S - 60`. New and rewritten tests use `loadApp({ nowMs: NOW_S * 1000 })`.
  - New tests in `tests/js/state.test.js`. Each opens the popup through the marker click, the detail through the row click, and reads `#ui-status`:
    - `live area and demo favorite carry their own badges on every reading surface`. Area `[fixtureSensor(1, { last_seen: FRESH })]`, favorites `[fixtureSensor(2, { origin: "demo", favorite: true, last_seen: FRESH })]`, `source: "PurpleAir live"`, `stale: false`.
      - Area row and marker: no `provenance-badge` in the row's `innerHTML`, and marker `aria-label === "Sensor 1 · AQI 21 · Fixture"`.
      - Favorite row: `innerHTML` matches `/class="provenance-badge"[^>]*>Demo</`. Its `aria-label === "Sensor 2, AQI 22, Fixture, Demo, favorite"`.
      - After the favorite row click: `#ui-status` is `"Sensor 2. AQI 22, Fixture. Demo."`, `#detail-provenance` is `"Demo"`, and `#summary-provenance.hidden === true`.
    - `stale area and fresh favorite: only area readings say Stale`. Area sensor 1 (`delivery: "cache"`), remote favorite sensor 2 (`delivery: "network"`), both `FRESH`, `source: "PurpleAir · cached"`, `stale: true`.
      - Area: summary `"Stale"`, the area row badge is Stale, the popup for marker 1 shows `#popup-provenance` `"Stale"`, and the detail after the area row click is `"Stale"`.
      - The favorite row has no badge, and its detail is hidden.
    - `fresh area and Old favorite: only the favorite says Old`. Area sensor 1 at `FRESH`, remote favorite sensor 2 at `NOW_S - 7201`. The favorite row, its detail, and the announcement say `Old`. The area row and summary don't.
    - `missing and far-future observation times are Old on every surface`.
      - Sensor 1 with `last_seen: null`: marker `aria-label` ends `" · Old"`. Popup badge `"Old"`, and `#popup-meta` contains `"Update time unavailable"`.
      - Sensor 2 at `NOW_S + 601`: badge `"Old"`, and `#popup-meta` doesn't contain `"just now"`.
      - Sensor 3 at `NOW_S + 600`: no badge.
    - `in-interval cached and live readings carry no badge`. `delivery: "cache"`, `source: "PurpleAir · cached"`, `stale: false`, `FRESH`. Every badge element is hidden or absent, and no surface text contains `"Cached"`.
  - Rewrite these existing tests for decision 4 (exact new expectations). Readings expected to be unbadged get `last_seen: FRESH`:
    - `:1027` `demo and coverage provenance remain visible near AQI`: `"Demo data"` → `"Demo"`.
    - `:1049` `selecting a demo favorite…`: `"Demo data"` → `"Demo"`.
    - `:1072` `mixed stale area and cached favorite…`:
      - `:1085` and `:1089-1090`: `"Stale cached data"` → `"Stale"`.
      - `:1086`: detail for the cached remote favorite → `hidden === true`.
    - `:1093` and `:1102`: `"Stale cached data"` → `"Stale"`.
    - `:1111` (renamed `ordinary in-interval cached payloads carry no provenance badge`): `summary.hidden === true` and `detail.hidden === true` for both payloads.
  - New tests in `tests/js/core.test.js`:
    - `isFreshObservation mirrors the shared Python boundary vectors`:
      - With `now = 1_800_000_000`: ages `0`, `7200`, `-600` are fresh. Ages `7201`, `-601`, plus `null`, `undefined`, `NaN`, `true`, and `"7200"` are not. `MAX_OBSERVATION_AGE === 7200` and `FUTURE_SKEW_TOLERANCE === 600`.
      - Cross-language part (existing `execFileSync("python3", ["-c", …])` pattern): for ages `[0, 7200, 7201, null]`, build `airloom.models.Sensor(1, "x", 0, 0, 150, 60.0, last_seen=…)`. Print whether `airloom.presentation.alert_transitions([s], favorites={1}, previous={}, threshold=101, now=now, origin="live")` alerts. Also print `presentation.MAX_ALERT_OBSERVATION_AGE`. Assert that Python eligibility equals `isFreshObservation` for each age, and that the constant is `7200`.
    - `readingProvenance uses Demo > Stale > Old precedence`:
      - A demo, stale, undated sensor → `"Demo"`.
      - A live sensor in a `stale: true` batch with `last_seen: null` → `"Stale"`.
      - A remote favorite in that batch → `"Old"`.
      - A `source: "PurpleAir · cached startup"` batch with no `stale` and `FRESH` → `""`.
  - Live harness (`scripts/test-ui-regression`):
    - `scenario_labels`: expect `stale == "Stale"`.
    - `scenario_alerts`: expect `demo["label"] == "Demo"`.
    - `scenario_cache_fallback`: both `"Stale cached data"` → `"Stale"`.
    - After the first live publish in `scenario_alerts`, add a JS probe. The `#sensor-list` row with `data-id="2"` (fixture age 3 h) has a `.provenance-badge`-matching `innerHTML` with text `Old`, and marker 2's `aria-label` ends `" · Old"`. Rows and markers for ids 1 and 4 have neither.
- [ ] **Step 2: Run it and confirm it fails**
  `node --test tests/js/*.test.js`. Expect:
  - `AirloomCore.isFreshObservation is not a function`;
  - marker `aria-label` `'Sensor 2 · AQI 22'` (no category or badge);
  - `#popup-provenance` missing;
  - `'Cached data' !== hidden`.
- [ ] **Step 3: Implement** the interfaces above.
  - Delete `summaryProvenanceLabel` and `detailProvenanceLabel`.
  - The `.provenance-badge` CSS:
    ```css
    .provenance-badge {
      align-self: flex-start;
      max-width: 100%;
      overflow-wrap: anywhere;
      padding: calc(1px * var(--interface-scale)) calc(7px * var(--interface-scale));
      border: 1px solid var(--line);
      border-radius: 99px;
      background: var(--soft);
      color: var(--ink);
      font-size: calc(10px * var(--interface-scale));
      font-weight: 800;
    }
    .provenance-badge[hidden] { display: none; }
    ```
- [ ] **Step 4: Run and confirm it passes**
  - `node --test tests/js/*.test.js`.
  - Under a display: `python3 scripts/test-ui-regression --only labels --only alerts --only cache-fallback` (Task 15 gives the display command).
- [ ] **Step 5: Commit**
  `git add airloom/resources/core.js airloom/resources/app.js airloom/resources/index.html airloom/resources/app.css scripts/test-ui-regression tests/js/core.test.js tests/js/state.test.js && git commit -m "fix: label each reading Demo, Stale, or Old on every surface and drop the ordinary Cached label"`

### Task 3: Visible ages and badges tick without refetching (Minor: ticking ages)

**Files:** Modify `airloom/resources/app.js` (new `tickAges` near `relativeTime`, start it beside `applyMotionPreference()` at `:1572`). Test: `tests/js/state.test.js` (harness: record timer delays).
**Interfaces:**
- Produces `const AGE_TICK_MS = 60000`.
- Produces `function tickAges()`. It recomputes only the time-derived output:
  - `#updated-time`;
  - `#popup-meta` and `#popup-provenance` when the popup is open;
  - `#detail-provenance`;
  - `renderSummary()`, `renderLists()`, and `renderMapMarkers()` (badges and accessible names).
- Each tick re-arms itself with `setTimeout(tickAges, AGE_TICK_MS)`. It never calls `bridge`, `renderChart`, or `applySensors`.
- The harness's fake `setTimeout(callback, delay)` records `delay`. `loadApp` returns `timerDelays()` (the delays of pending timers).

- [ ] **Step 1: Write the failing tests**
  - `relative ages tick every minute without refetching`:
    - `loadApp({ nowMs: NOW_S * 1000 })` and `configureMap`. Receive `fixtureSensor(1, { last_seen: NOW_S - 30, aqi: 42 })`, then click its row.
    - Assert `#updated-time` is `"Updated just now"` and `timerDelays()` includes `60000`.
    - `setNow((NOW_S + 600) * 1000)` and `runTimers()`.
    - Assert `#updated-time` is `"Updated 10 min ago"`, `#aqi-number` is still `"42"`, no new `refresh`/`select`/`ready` messages were posted, and `timerDelays()` includes `60000` again.
  - `the minute tick flips a reading to Old across the 2-hour boundary`:
    - Same setup with the popup also open via a marker click.
    - `setNow((NOW_S - 30 + 7201) * 1000)` and `runTimers()`.
    - `#detail-provenance` is `"Old"`, `#popup-provenance` is `"Old"`, the marker `aria-label` ends `" · Old"`, and the row `innerHTML` has the Old badge.
- [ ] **Step 2: Run it and confirm it fails**
  `node --test --test-name-pattern "tick" tests/js/state.test.js`. Expect `'Updated just now' !== 'Updated 10 min ago'` and `timerDelays()` lacking `60000`.
- [ ] **Step 3: Implement** `tickAges` and start the first tick at load.
- [ ] **Step 4: Run and confirm it passes**
  `node --test tests/js/*.test.js`
- [ ] **Step 5: Commit**
  `git add airloom/resources/app.js tests/js/state.test.js && git commit -m "fix: update visible reading ages and Old badges every minute without refetching"`

### Task 4: Accessible trend values (finding #16)

**Files:** Modify `airloom/resources/app.js:574-614` (`renderChart`) and `airloom/resources/app.css:162-168` (`.chart .point-value`). Test: `tests/js/state.test.js`.
**Interfaces:**
- Produces `const WINDOW_LABELS = { "1w": "1-week average", "1d": "1-day average", "6h": "6-hour average", "1h": "1-hour average", "30m": "30-minute average", "10m": "10-minute average", Now: "Current" }`. It replaces `comparisonLabels`. An unknown label falls back to the escaped raw label.
- Produces the loaded-chart markup inside `#chart`:
  - `<svg role="img" aria-labelledby="trend-chart-title" aria-describedby="trend-chart-desc" …>`, starting with:
    - `<title id="trend-chart-title">${chart-period text}</title>`;
    - `<desc id="trend-chart-desc">${direction}. ${items}.</desc>`, where `items` are `` `${window} AQI ${aqi}, ${category}` `` joined with `"; "`.
  - Each point gets a visible `<text class="point-value" x="…" y="${y - 7}" text-anchor="middle">${aqi}</text>`.
  - After the SVG: `<table class="visually-hidden" id="trend-values"><caption>Rolling-average AQI values</caption><thead><tr><th scope="col">Window</th><th scope="col">AQI</th><th scope="col">Category</th></tr></thead><tbody>` plus one `<tr data-window="${label}"><th scope="row">${window}</th><td>${aqi}</td><td>${category}</td></tr>` per point.
  - `category = AirloomCore.bandForAQI(aqi).label`. The wording stays "current vs rolling average", never "history".
- Task 5's live scenario reads `#trend-values tr[data-window]`.

- [ ] **Step 1: Write the failing test**
  `trend values are exposed as accessible text and visible point labels`. With `has_api_key`, receive a `loaded` trend `[{label:"1w",aqi:40},{label:"1d",aqi:45},{label:"Now",aqi:48}]`. Then assert on `#chart.innerHTML`:
  - it matches `/<svg[^>]*role="img"[^>]*aria-labelledby="trend-chart-title"[^>]*aria-describedby="trend-chart-desc"/`;
  - it includes `<title id="trend-chart-title">Current vs 1-week average</title>`;
  - it includes `<desc id="trend-chart-desc">Up 8 points. 1-week average AQI 40, Good; 1-day average AQI 45, Good; Current AQI 48, Good.</desc>`;
  - it includes `<tr data-window="1d"><th scope="row">1-day average</th><td>45</td><td>Good</td></tr>`;
  - three `class="point-value"` matches with the texts `40`, `45`, `48`;
  - it doesn't match `/history/i`.
- [ ] **Step 2: Run it and confirm it fails**
  `node --test --test-name-pattern "accessible text" tests/js/state.test.js`. Expect the regex mismatch, because the SVG has no `role`.
- [ ] **Step 3: Implement** the markup above. The existing tests `trend loaded renders…` and `rolling-average charts name…` must still pass unchanged.
- [ ] **Step 4: Run and confirm it passes**
  `node --test tests/js/*.test.js`
- [ ] **Step 5: Commit**
  `git add airloom/resources/app.js airloom/resources/app.css tests/js/state.test.js && git commit -m "fix: expose rolling-average trend values to screen readers and label each plotted point"`

### Task 5: Trend retry drives a real failure to a rendered terminal state (finding #21)

**Files:** Modify `scripts/test-ui-regression`:
- `fixture_rows:901-910`: row 4 gains `"pm2.5_10minute": 6, "pm2.5_30minute": 7, "pm2.5_60minute": 8, "pm2.5_6hour": 10, "pm2.5_24hour": 12, "pm2.5_1week": 14`.
- Replace `scenario_trend_retry:577-614`.
- `APP_SCENARIOS`/`STANDALONE_SCENARIOS:1352-1370`.

Test: `tests/test_integration.py::UiRegressionScriptContractTest`.

**Interfaces:**
- Produces `TREND_PROBE_JS`, which returns `{ retryHidden, retryDisabled, chartText, rows: [[window, aqi]] }`, where `rows` comes from `#trend-values tr[data-window]` as `[tr.dataset.window, Number(td.textContent)]`.
- Produces `terminal_trend(probe) -> dict | None`. It returns the probe only when `retryHidden` is true, `"Loading"` is not in `chartText`, and either `rows` is non-empty or `chartText` contains `"Trend unavailable"`. Otherwise it returns `None`.
- Produces `scenario_trend_retry(root, geocoder)`, now a STANDALONE scenario under the same name `"trend-retry"` on a `FixtureApp`.

- [ ] **Step 1: Write the failing tests**
  - `test_trend_terminal_state_rejects_loading_and_pending_retry`:
    - `terminal_trend({"retryHidden": True, "retryDisabled": False, "chartText": "Loading trend…", "rows": []}) is None` (the reviewer's stuck state);
    - an error probe with `retryHidden: False` is `None`;
    - a probe with `rows [["Now", 21]]` and `retryHidden: True` is returned.
  - `test_trend_retry_uses_a_real_fixture_failure`:
    - `"trend-retry" in script.STANDALONE_SCENARIOS` and not in `APP_SCENARIOS`;
    - `"1000000" not in inspect.getsource(script.scenario_trend_retry)`.
- [ ] **Step 2: Run it and confirm it fails**
  `python3 -m unittest tests.test_integration.UiRegressionScriptContractTest -v`. Expect `AttributeError: module 'airloom_ui_regression' has no attribute 'terminal_trend'`.
- [ ] **Step 3: Implement**
  1. Start `FixtureApp(root, "trend-retry", geocoder, fail=False)`, install `SENSOR_SPY_JS`, and `publish(REFRESH_JS, …)`.
  2. `set_fixture(fail=True)`, then click `#sensor-list .sensor-row[data-id="4"]`.
  3. Wait (20 s) until the probe shows Retry visible and `"Trend unavailable"`. That is the real backend `error` via `_ensure_trend`.
  4. `set_fixture(fail=False)`, then click `#trend-retry`.
  5. `wait_for(lambda: terminal_trend(eval_js(sock, TREND_PROBE_JS)), 20, …)`.
  6. Compute `expected = [[p["label"], p["aqi"]] for p in airloom.purpleair.trend_from_values({**row4, "last_seen": 0})]` after inserting `PROJECT` on `sys.path`. Check that the probe's `rows == expected`, and that the chart `innerHTML` contains `<svg`.
  7. Stop the app in `finally`.
- [ ] **Step 4: Run and confirm it passes**
  `python3 -m unittest tests.test_integration.UiRegressionScriptContractTest -v`, then under a display `python3 scripts/test-ui-regression --only trend-retry`.
- [ ] **Step 5: Commit**
  `git add scripts/test-ui-regression tests/test_integration.py && git commit -m "test: drive trend retry through a real fixture failure to rendered values"`

### Task 6: Keyboard map panning (finding #15)

**Files:**
- Modify `airloom/resources/index.html:13`: `<section class="map-panel" id="map-panel" tabindex="0" aria-label="Air quality sensor map" aria-roledescription="map" aria-describedby="map-keyboard-help">`. Add `<p class="visually-hidden" id="map-keyboard-help">Use the arrow keys to pan the map, Shift with an arrow key to pan farther, and plus or minus to zoom.</p>` as its first child.
- Modify `airloom/resources/app.css` (after `:30`): `.map-panel:focus-visible { outline: calc(3px * var(--interface-scale)) solid var(--accent); outline-offset: calc(-3px * var(--interface-scale)); }`.
- Modify `airloom/resources/app.js`: add `onMapKeydown` near the pointer handlers (`:1431-1438`), and add `navigationIdle` to `debugState` (`:107-122`).
- Modify `scripts/test-ui-regression`: new `X11Input` helper and `scenario_keyboard_map`.
- Test: `tests/js/state.test.js`, `tests/test_integration.py` (`REQUIRED_COVERAGE` gains `"keyboard map traversal and panning": "keyboard-map"`).

**Interfaces:**
- Produces `MAP_KEY_PAN_FRACTION = 0.2` and `MAP_KEY_PAN_SHIFT_FRACTION = 0.6`.
- Produces `onMapKeydown(event)` on `#map-panel`. It acts only when all of these hold:
  - `event.target === $("#map-panel")`;
  - `!event.ctrlKey && !event.altKey && !event.metaKey`;
  - `!$("#settings-dialog").open`.
- What it does:
  - Arrow keys (Shift allowed): `beginDirectManipulation(); hideTransientOverlays(); panBy(dx, dy); renderMapMarkers()`. `ArrowRight` → `panBy(-step, 0)`, `ArrowLeft` → `panBy(step, 0)`, `ArrowDown` → `panBy(0, -step)`, `ArrowUp` → `panBy(0, step)`. The step is the fraction times the panel width (horizontal) or height (vertical).
  - `"+"` or `"="` → `animateZoomTo((zoomAnim ? zoomAnim.target : Math.round(state.zoom)) + 1, centerAnchor())`. `"-"` or `"_"` → −1.
  - `preventDefault()` only on handled keys.
- This is the same cancellation and `scheduleViewChanged` path as pointer drag.
- Produces `debugState().navigationIdle: boolean`. It is true when `zoomAnim === null`, there's no current flight, `pinch === null`, and `nativePinchStartZoom === null`. `flyTo` records its token in `let flightToken` and clears it when the flight completes. Task 14 consumes this.
- Produces in the script:
  - `class X11Input`, using `ctypes.CDLL("libX11.so.6")` and `ctypes.CDLL("libXtst.so.6")`, with:
    - `XOpenDisplay(None)`;
    - `key(name: str, shift: bool = False)`, via `XStringToKeysym` → `XKeysymToKeycode` → `XTestFakeKeyEvent` press/release, with `Shift_L` around it, then `XFlush`;
    - `move(x, y)` (`XTestFakeMotionEvent(dpy, -1, x, y, 0)`);
    - `click(x, y)` (move, then `XTestFakeButtonEvent` 1 press/release);
    - `close()`.
  - Its constructor raises `AssertionError("keyboard-map needs an X11 display with XTEST (libXtst)")` when `DISPLAY` is unset or a library is missing. It fails, never skips.
  - `page_origin(sock, x11) -> (ox, oy)`: install a `mousemove` recorder in the page, `x11.move(600, 400)`, wait for a recorded event, and return `(600 - clientX, 400 - clientY)` at 100% scale.

- [ ] **Step 1: Write the failing tests**
  - `tests/js/state.test.js`:
    - `the map is a named focusable region with keyboard help`: `map-panel.getAttribute("tabindex") === "0"`, `aria-describedby === "map-keyboard-help"`, `map-keyboard-help.textContent` matches `/arrow keys.*plus or minus/`, and `cssDeclaration(".map-panel:focus-visible", "outline")` is not null.
    - `arrow keys pan the focused map by a fifth of the viewport and schedule a view fetch`:
      - `configureMap(app)`, then dispatch `keydown` on `map-panel` with `{ key: "ArrowRight", target: panel }`.
      - Center lon is `closeTo(0.2 * 512 / (256 * 2 ** 12) * 360)` and lat is unchanged. `preventDefault` was called (record it on the event object).
      - `runTimers()` posts one `view-changed` whose `lon` equals that value.
      - Shift+ArrowDown moves lat south by the 0.6 step.
    - `map keyboard panning cancels an in-flight recenter`: `startFlight(app)`, then ArrowLeft, then `runAnimationFrames(10000)`. The center equals the post-pan center, not the flight target.
    - `plus and minus zoom the focused map`: `loadApp({ reducedMotion: true })`. `"+"` → zoom 13, `"="` → 14, `"-"` → 13.
    - `map keys are ignored outside the focused map, with modifiers, and while Preferences is open`. In each case the center and zoom are unchanged and nothing is prevented:
      - target `search`, and a target that is a marker `button` inside the map;
      - `ctrlKey: true`;
      - after `openSettings(app)`.
    - `debugState reports navigation idle only after flights and zoom animations settle`: `true` initially, `false` after `startFlight`, `true` after `runAnimationFrames(10000)`, and `false` after a non-reduced `zoom-in` click until frames settle it.
  - `tests/test_integration.py`: `REQUIRED_COVERAGE` addition (makes `test_lists_every_required_scenario` fail), plus `test_keyboard_map_scenario_requires_x11_rather_than_skipping`. With `DISPLAY` removed via `mock.patch.dict(os.environ, …, clear=False)` and popped, `X11Input()` raises `AssertionError` matching `"X11 display with XTEST"`.
- [ ] **Step 2: Run them and confirm they fail**
  - `node --test --test-name-pattern "map|navigation idle" tests/js/state.test.js`. Expect `null !== '0'` for `tabindex`, an unchanged center after ArrowRight (the review's probe), and `navigationIdle` undefined.
  - `python3 -m unittest tests.test_integration.UiRegressionScriptContractTest -v`. Expect the scenario set mismatch.
- [ ] **Step 3: Implement** the page changes, then `scenario_keyboard_map(root, geocoder)` as a STANDALONE scenario on its own `DebugApp(root, "keyboard-map", geocoder.url, extra_env={"GDK_BACKEND": "x11"})`:
  1. `wait_ready`, `x11 = X11Input()`, and `(ox, oy) = page_origin(…)`.
  2. Real click on the center of `#search` (its `getBoundingClientRect` plus the origin).
  3. Read `expected = [...document.querySelectorAll(".map-marker")].map(m => m.dataset.id).reverse()` plus `"map-panel"`.
  4. For each expected target, call `x11.key("Tab", shift=True)`, then wait (5 s) until `document.activeElement` is that marker (`dataset.id`) or `#map-panel`.
  5. Check that `#map-panel` matches `:focus-visible` and its computed `outlineStyle !== "none"`.
  6. Install `COUNT_SENSOR_PAYLOADS_JS`. Real `Right` → wait for `center.lon` to increase, then for `window.__sensorsPayloads >= 1` (the view fetch).
  7. Real `equal` → wait for `zoom == before + 1` and `navigationIdle`. Real `minus` → back.
  8. Real click on `#search`, read the center, real `Right` then real `x`. Wait for `#search.value.endsWith("x")`, then check that the center is unchanged.
  9. `x11.close()` and stop the app in `finally`.
- [ ] **Step 4: Run and confirm it passes**
  `node --test tests/js/*.test.js && python3 -m unittest tests.test_integration -v`. Then `--only keyboard-map` and `--only focus --only motion` under Xvfb (Task 15 command).
- [ ] **Step 5: Commit**
  `git add airloom/resources/index.html airloom/resources/app.css airloom/resources/app.js scripts/test-ui-regression tests/js/state.test.js tests/test_integration.py && git commit -m "feat: pan and zoom the focused map from the keyboard, verified with real key input"`

### Task 7: Failed current tiles retry with bounded backoff (finding #14)

**Files:**
- Modify `airloom/resources/app.js`:
  - `:765`: `tileLayer` gains `failures: Map<key, attempts>` and `retryTimer`.
  - `:821-893`: extract `requestTile(owner, key, tx, ty, tz)` from the loop.
  - New `scheduleTileRetry(owner)` and `retryFailedTiles(owner = tileLayer)`.
  - `:259-283`: `applySensors` calls `retryFailedTiles()`.
  - `:1363`: the footer Refresh click calls `retryFailedTiles()` after `actions.refresh()`.
- Test: `tests/js/state.test.js`.

**Interfaces:**
- Produces `TILE_RETRY_BASE_MS = 2000` and `TILE_RETRY_MAX_MS = 60000`. The delay is `Math.min(TILE_RETRY_MAX_MS, TILE_RETRY_BASE_MS * 2 ** (attempts - 1))`, using the smallest attempt count among the layer's failed tiles.
- On error, `settle(false)` marks `img.dataset.failed = "true"`, increments `owner.failures.get(key)`, and calls `scheduleTileRetry(owner)`. A layer has at most one `retryTimer`.
- `retryFailedTiles(owner)`:
  - clears the timer;
  - for each failed key still in `owner.tiles`, removes the failed `img` and calls `requestTile` again with the same URL (a fresh element, so normal HTTP caching applies);
  - is a no-op unless `owner === tileLayer`.
- On load, `failures.delete(key)`. Replacing `tileLayer` clears the old layer's `retryTimer`. Retired imagery still leaves only through `clearRetiredTilesWhenCovered`.

- [ ] **Step 1: Write the failing tests**
  The harness `setTimeout` records delays (from Task 3).
  - `a failed tile is retried at the same center and zoom and then loads`:
    - Call `onerror()` on `tiles.childNodes[0]`. `timerDelays()` includes `2000`.
    - `runTimers()`. A different element with the same `src` and `dataset.pending === "true"` is now in `#tiles`, and the failed one is disconnected.
    - Its `onload()` adds `loaded`. `debugState()` zoom and center are unchanged.
  - `manual Refresh retries failed tiles immediately`: after `onerror()`, clicking `footer-refresh` posts `{action:"refresh"}` and replaces the tile without `runTimers()`.
  - `a sensors publication retries failed tiles`: same, via `receiveSensors`. This covers the header-bar Refresh, whose result arrives as a publication.
  - `retained imagery stays until the retried replacement layer fully loads`:
    - Load all tiles, then `debugZoomTo(13)`, then fail every new tile, then `runTimers()`.
    - `#tiles-old` is still populated.
    - After `onload()` on every replacement, `#tiles-old` is empty and hidden.
  - `tile retry backoff is capped and a zoom change cancels the old layer's retry`:
    - Fail the same key repeatedly with `runTimers()` between failures. The scheduled delays go `2000, 4000, 8000, 16000, 32000, 60000, 60000`.
    - Then fail a tile, `debugZoomTo(14)`, and `runTimers()`. No element with the old layer's `src` is re-requested.
- [ ] **Step 2: Run it and confirm it fails**
  `node --test --test-name-pattern "tile" tests/js/state.test.js`. Expect the failed element still in place and no `2000` delay. That is the review's reproduction: Refresh retains the failed tile with null handlers.
- [ ] **Step 3: Implement** as specified. The existing five tile-retention tests at `state.test.js:640-740` must pass unchanged.
- [ ] **Step 4: Run and confirm it passes**
  `node --test tests/js/*.test.js`
- [ ] **Step 5: Commit**
  `git add airloom/resources/app.js tests/js/state.test.js && git commit -m "fix: retry failed map tiles with capped backoff and on Refresh while keeping old imagery"`

### Task 8: Preview disables the settings it doesn't implement (finding #18)

**Files:**
- Modify `airloom/resources/index.html`:
  - `:142`: `<fieldset class="wide" id="home-fieldset">`.
  - After `:173`: `<p class="privacy-note" id="preview-settings-note" hidden>Browser preview: map radius, refresh interval, alert threshold, the confidence filter, and home location work only in the Airloom desktop app.</p>`.
- Modify `airloom/resources/app.js:62-78` (`previewAdapter.saveSettings`) and `:1109-1138` (`openSettings`).
- Modify `scripts/test-ui-regression:1139-1155` (`PREVIEW_ACTIONS_JS`) and `:1304-1309` (expectations).
- Test: `tests/js/state.test.js`.

**Interfaces:**
- Produces `PREVIEW_UNSUPPORTED_FIELDS = ["radius_km", "refresh_minutes", "alert_threshold", "confidence_filter"]` and `PREVIEW_SUPPORTED_SETTINGS = ["temperature_unit", "interface_scale", "heatmap_threshold_km"]`.
- `openSettings` sets `disabled = !desktopMode` on each unsupported field, `#home-fieldset`, and `#home-place-input`. Each of these gets `aria-describedby="preview-settings-note"` in preview. `#preview-settings-note.hidden = desktopMode`.
- `previewAdapter.saveSettings(settings)` applies only `PREVIEW_SUPPORTED_SETTINGS`, with `heatmap_threshold_km` and `interface_scale` coerced by `Number`. The existing announcement is kept.

- [ ] **Step 1: Write the failing tests**
  - `preview disables unimplemented settings with a visible explanation`:
    - In preview, after `openSettings(app)`: each unsupported field, `home-fieldset`, and `home-place-input` is `disabled === true`, and `preview-settings-note.hidden === false`.
    - In desktop mode they are all `false`, and the note is hidden.
  - `preview Save ignores unimplemented settings`. In preview, set `radius_km.value = "99"`, `refresh_minutes.value = "30"`, `alert_threshold.value = "5"`, then submit. Reopening Preferences shows `radius_km.value === 22`, `refresh_minutes.value === 2`, `alert_threshold.value === 101`.
  - `preview heat-map threshold has an observable effect`: in preview, `debugState().heatmapActive === false`. After saving `heatmap_threshold_km = "5"` it is `true`.
  - The existing temperature and interface-scale preview tests stay as the observable-effect tests for those settings.
  - Live `PREVIEW_ACTIONS_JS` adds `out.unsupportedDisabled` (all four fields plus `#home-fieldset` disabled) and `out.noteVisible`. `scenario_layouts` expects both to be `True`.
- [ ] **Step 2: Run it and confirm it fails**
  `node --test --test-name-pattern "preview" tests/js/state.test.js`. Expect `false !== true` for `radius_km.disabled`, and `99 !== 22`.
- [ ] **Step 3: Implement** as specified.
- [ ] **Step 4: Run and confirm it passes**
  `node --test tests/js/*.test.js`
- [ ] **Step 5: Commit**
  `git add airloom/resources/index.html airloom/resources/app.js scripts/test-ui-regression tests/js/state.test.js && git commit -m "fix: disable and explain settings the browser preview does not implement"`

### Task 9: Dismissing a pending main search cancels it (Minor: search dismissal)

**Files:** Modify `airloom/resources/app.js:1225-1228` (new `cancelMainSearch`), `:1336-1343` (input handler), and `:1352-1354` (Escape). Test: `tests/js/state.test.js`.
**Interfaces:**
- Produces `function cancelMainSearch()`. If `state.placeRequests.main !== null`, it calls `invalidatePlaceSearch("main")` and then `bridge({ action: "cancel-place-search", context: "main" })`. Otherwise it only calls `invalidatePlaceSearch("main")`.
- The input handler (clearing or replacing the query) calls it in place of `invalidatePlaceSearch("main")`.
- Escape (with Preferences closed and the popup hidden) calls it, hides `#search-results`, and focuses `#search` when either the results box is open or `document.activeElement === $("#search")` with a pending request.

- [ ] **Step 1: Write the failing tests**
  - `Escape while a main search is pending cancels it and a late result cannot reopen results`:
    - Submit `"Paris"`, focus `search`, then dispatch `Escape`. Exactly one `{action:"cancel-place-search", context:"main"}` is posted.
    - Then `receive("places", { request_id: 1, context: "main", query: "Paris", results: [...] })` leaves `search-results.hidden === true`.
  - `editing a submitted query cancels its pending remote search once`: submit `"Portland"`, then input `"Paris"`, then input `"Pari"`. There is exactly one main cancellation.
  - `clearing search with nothing pending sends no cancellation`.
- [ ] **Step 2: Run it and confirm it fails**
  `node --test --test-name-pattern "cancel|nothing pending" tests/js/state.test.js`. Expect `0 !== 1` cancellations and results reopened (the review's probe).
- [ ] **Step 3: Implement** as specified.
- [ ] **Step 4: Run and confirm it passes**
  `node --test tests/js/*.test.js`
- [ ] **Step 5: Commit**
  `git add airloom/resources/app.js tests/js/state.test.js && git commit -m "fix: cancel a pending place search when it is dismissed, cleared, or replaced"`

### Task 10: Hidden-sensors list keeps focus (Minor: hidden-sensors list)

**Files:** Modify `airloom/resources/app.js:1140-1149` (`renderHiddenList`) and `:1393-1397` (Unhide all). Test: `tests/js/state.test.js`.
**Interfaces:**
- Produces `createHiddenRow()` and `updateHiddenRow(row, item)`:
  - The row is `div.hidden-row` containing a `span` (name via `textContent`) and a `button.unhide-button` with `type="button"`.
  - The click listener is attached once, in `create`, and calls `requestUnhide(Number(row.dataset.key))`.
- `renderHiddenList()`:
  - Snapshots the previous child keys, then calls `AirloomState.reconcileKeyed($("#hidden-list"), items, (i) => i.id, createHiddenRow, updateHiddenRow)`.
  - Uses the empty-state markup when there are no items.
  - When reconcile returns `removedFocus`, it focuses the `.unhide-button` of the next surviving key after the removed one in the previous order, else the previous surviving key, else `#close-settings`.
  - If `#unhide-all` had focus and becomes hidden, it focuses `#close-settings`.

- [ ] **Step 1: Write the failing test**
  `hidden list keeps focus through refreshes and Unhide moves to a successor`. Receive sensors with `config.hidden = [{id:1,name:"A"},{id:2,name:"B"},{id:3,name:"C"}]`, then `openSettings(app)`. Focus B's button.
  1. Receive the same list again (a background refresh). The same node is still `activeElement`.
  2. Click B's button. `{action:"unhide", id:2}` is posted.
  3. Receive the list without B. `activeElement` is C's button.
  4. Unhide C, then receive `[A]`. `activeElement` is A's button.
  5. Unhide A, then receive `[]`. `activeElement` is `close-settings`.
- [ ] **Step 2: Run it and confirm it fails**
  `node --test --test-name-pattern "hidden list" tests/js/state.test.js`. Expect `activeElement` to be `null` after the refresh, because `innerHTML` rebuilds the list.
- [ ] **Step 3: Implement** as specified.
- [ ] **Step 4: Run and confirm it passes**
  `node --test tests/js/*.test.js`
- [ ] **Step 5: Commit**
  `git add airloom/resources/app.js tests/js/state.test.js && git commit -m "fix: keep hidden-sensor focus through refreshes and move it to a successor on Unhide"`

### Task 11: Fixed-home search input uses the shared input styling (deferred styling)

**Files:** Modify `airloom/resources/app.css:200` by adding `.form-grid input[type="search"]` to the selector list. Test: `tests/js/state.test.js`.
**Interfaces:** None.
- [ ] **Step 1: Write the failing test**
  `fixed-home search input shares the form input styling`. For each of `width`, `height`, `padding`, `border`, `border-radius`, `background`, and `color`:
  ```js
  assert.equal(
    cssDeclaration('.form-grid input[type="search"]', p),
    cssDeclaration('.form-grid input[type="text"]', p),
  );
  ```
  Also assert that the search value is not null.
- [ ] **Step 2: Run it and confirm it fails**
  `node --test --test-name-pattern "fixed-home search" tests/js/state.test.js`. Expect `null !== '100%'`.
- [ ] **Step 3: Implement** the one selector.
- [ ] **Step 4: Run and confirm it passes**
  `node --test tests/js/*.test.js`
- [ ] **Step 5: Commit**
  `git add airloom/resources/app.css tests/js/state.test.js && git commit -m "fix: style the fixed-home place search like the other preference inputs"`

### Task 12: Narrow and scaled layouts fit expanded details and long labels in both themes (finding #17)

**Files:**
- Modify `airloom/resources/app.css`:
  - `:73-76`: `.summary-chip` gets `max-width: calc(100vw - 28px * var(--interface-scale))`, and `.summary-chip .summary-copy` gets `min-width: 0`.
  - `:146-150`: `.aqi-row` gets `flex-wrap: wrap`. `.aqi-row > div` gets `min-width: 0; flex: 1 1 calc(120px * var(--interface-scale))`. `#aqi-category` gets `max-width: none; overflow-wrap: anywhere`.
  - `@media (max-width: 640px)`: `.aqi-number { font-size: calc(52px * var(--interface-scale)); }`.
- Modify `scripts/test-ui-regression`:
  - `:52-55`: `LAYOUT_THEMES = ("light", "dark")`.
  - New `layout_cases()`.
  - `LAYOUT_MEASURE_JS:1057-1094`: adds `theme`, extends `clipped` with `.detail-card`, `.aqi-row`, `.summary-copy`, and adds `escaped`.
  - `LAYOUT_STATES_JS:1096-1137`: new `"detail"` state after `"sensors"`.
  - `layout_child:1158-1260`: applies the theme.
  - `scenario_layouts:1263-1321`: uses `layout_cases()` and the new expectations.
- Test: `tests/test_integration.py::UiRegressionScriptContractTest`.

**Interfaces:**
- Produces `layout_cases() -> list[dict]`:
  - Every `(mode, width, height, scale)` of `("desktop", "preview") × LAYOUT_SIZES × SCALES` with `theme: "light"`;
  - plus `theme: "dark"` for both modes × widths 320 and 375 × all `SCALES`.
- Produces the `"detail"` state script:
  - Click `#close-sensors`.
  - Receive `sensors` with one sensor `990001`:
    - name `"Northeast Martin Luther King Junior Boulevard Community Garden Rooftop Monitor 3"`;
    - at the map center, `aqi: 150`, with `category`, `color`, `foreground`, and `guidance` from `AirloomCore.bandForAQI(150)`;
    - `last_seen` 30 s ago, `delivery: "cache"`, `indoor: true`, `favorite: true`;
    - in the payload `{ items, area: [sensor], favorites: [], selected_id: 990001, source: "PurpleAir · cached", stale: true }`.
  - Receive `view-name` `{ name: "Llanfairpwllgwyngyllgogerychwyrndrobwllllantysiliogogogoch, Isle of Anglesey, Wales" }`.
  - Click `#sensor-list .sensor-row`.
  - Return `"detail"` if `#detail-card` is visible and `#detail-provenance` reads `Stale`, else `"no-detail"`.
- Measurements:
  - `escaped`: for each visible container in `.summary-chip`, `.detail-card`, `.map-popup`, `.sensors-panel`, every visible descendant outside `.visually-hidden` has `left >= container.left - 0.5` and `right <= container.right + 0.5`.
  - `theme`: `matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"`.
- `layout_child` applies the theme before creating the WebView: `Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", case["theme"] == "dark")`. `scenario_layouts` also passes `GTK_THEME=Adwaita:dark` in the child env for dark cases.

- [ ] **Step 1: Write the failing tests**
  - Extend `test_layouts_cover_the_reported_scaled_widths_at_every_scale`:
    - `script.LAYOUT_THEMES == ("light", "dark")` and `"detail" in script.LAYOUT_STATES_JS`;
    - `{(c["mode"], c["width"], c["scale"], c["theme"]) for c in script.layout_cases()}` contains every `(mode, w, s, "dark")` for `w in (320, 375)` and `s in SCALES`, plus every existing light case;
    - `"escaped"` and `"prefers-color-scheme: dark"` appear in `LAYOUT_MEASURE_JS`.
  - Add expectations in `scenario_layouts`:
    - `row["theme"] == row["case_theme"]` (the matrix self-validates);
    - `not row["escaped"]`;
    - `outcome not in (..., "no-detail")`.
- [ ] **Step 2: Run them and confirm they fail**
  - `python3 -m unittest tests.test_integration.UiRegressionScriptContractTest -v`. Expect `AttributeError: … 'LAYOUT_THEMES'`.
  - With only the script changes applied (CSS untouched), run under Xvfb `python3 scripts/test-ui-regression --only layouts`. Expect the review's reproduction: `preview 320px at 1.5x [detail]: no clipped overlay content ['.detail-card 334>320']` and a `.summary-chip` clip from the long place name.
- [ ] **Step 3: Implement** the CSS above.
- [ ] **Step 4: Run and confirm it passes**
  The same two commands; all layout rows pass in both themes.
- [ ] **Step 5: Commit**
  `git add airloom/resources/app.css scripts/test-ui-regression tests/test_integration.py && git commit -m "fix: fit three-digit details and long labels in narrow scaled layouts and test both themes"`

### Task 13: `descendants()` tolerates only vanished processes (Minor: descendants() in test-ui-regression)

**Files:** Modify `scripts/test-ui-regression:229-240`. Test: `tests/test_integration.py::UiRegressionScriptContractTest`.
**Interfaces:** `descendants(pid) -> list[int]` catches only `(FileNotFoundError, ProcessLookupError)` (skip that parent). Every other `OSError` propagates.
- [ ] **Step 1: Write the failing test**
  `test_descendants_skip_only_vanished_processes`:
  - With `mock.patch.object(self.script.Path, "read_text", side_effect=FileNotFoundError())`, `descendants(12345) == []`; the same for `ProcessLookupError(errno.ESRCH, …)`.
  - For `PermissionError(errno.EACCES, …)` and `OSError(errno.EIO, …)`, `assertRaises` that type with the same `errno`.
  - Using the real `/proc`: a spawned `sleep 30` child of this process is in `descendants(os.getpid())`. Kill and wait on it in `addCleanup`.
- [ ] **Step 2: Run it and confirm it fails**
  `python3 -m unittest tests.test_integration.UiRegressionScriptContractTest.test_descendants_skip_only_vanished_processes -v`. Expect `PermissionError not raised`.
- [ ] **Step 3: Implement** the narrowed `except`.
- [ ] **Step 4: Run and confirm it passes**
  `python3 -m unittest tests.test_integration -v`
- [ ] **Step 5: Commit**
  `git add scripts/test-ui-regression tests/test_integration.py && git commit -m "test: let descendant enumeration surface errors other than a vanished process"`

### Task 14: Replace fixed completion sleeps with completion conditions (Minor: fixed completion sleeps)

**Files:** Modify `scripts/test-ui-regression` at the current lines (the spec's "~466, 473, 489, 502, 512, 927, 1027, 1237/1240"):
- `GeocoderStub:140-186` (completion record);
- `GEOCODER_RESPONSES:58-64` (add `"sentineltown": (0.0, [{"display_name": "Sentineltown, Oregon", "lat": "44.4", "lon": "-121.4"}])`);
- `scenario_search:476,483,499`;
- `scenario_interrupted_motion:512,522`;
- `FixtureApp.publish:937`;
- `scenario_stripped_debug:1037`;
- `layout_child:1248,1251,1258`.

Test: `tests/test_integration.py::UiRegressionScriptContractTest`.

**Interfaces:**
- `GeocoderStub.completed(query) -> bool`. The handler appends the query to `self.done` after `self.wfile.write(data)` and `self.wfile.flush()`.
- `main_loop_barrier(sock) -> None`: two `request(sock, "state")` round trips. Each is dispatched with `GLib.idle_add` behind any callback already queued.
- `settle_layout(webview) -> bool` inside `layout_child`:
  - Polls `LAYOUT_SIGNATURE_JS` (`JSON.stringify` of `document.documentElement.scrollWidth` and the rounded rects of `.search-overlay`, `.summary-chip`, `.detail-card`, `.map-popup`, `.sensors-panel`, `.settings-dialog`, `.shell-status`) every 0.05 s.
  - Returns once two consecutive results are equal. After 5 s it returns `False`, and the case reports an error row.
- Every remaining `time.sleep(`, `SLEEP(`, or `pump(` call carries a same-line `# sleep-ok: <reason>` comment. The allowed ones:
  - `wait_for`'s 0.2 s poll ("polling cadence");
  - the stub's simulated latency ("simulated server delay");
  - the ready loop's `pump(0.2)` and `settle_layout`'s `pump(0.05)` ("polling cadence");
  - the post-`set_child(None)` `pump(0.1)` ("lets GTK drop the view; nothing is measured after it").
- Replacements:
  - **Search order (`:476`):** `wait_for(geocoder.completed("slowtown"))`, then `main_loop_barrier`, then the existing check. Comment: Python drops superseded results without an event, so completion plus the barrier is the last observable point.
  - **Cancel (`:483`):** the same with `"canceltown"`.
  - **Queued home (`:499`):** `wait_for(geocoder.completed("blockertown"))`, then `type_and_submit("#search", "sentineltown")`, then wait for `"Sentineltown"` in the results, then the existing negative check. Comment: geocoder admission is serialized, and the queued home request was admitted-or-rejected before the later sentinel could finish.
  - **Motion (`:512`, `:522`):** `wait_for(lambda: request(sock, "state")["navigationIdle"], 10, …)` (from Task 6).
  - **Alerts (`:937`):** delete the sleep. Comment: `_finish_refresh` sends sensors, errors, and alert persistence in one main-loop callback, and the eval that observed the new count was queued after it.
  - **Stripped (`:1037`):** `wait_for(lambda: any(b"WebKitWebProcess" in proc_bytes(p, "cmdline") for p in descendants(app.process.pid)), 30, "the stripped app's web process")`, then the existing liveness checks.
  - **Layout (`:1248`, `:1251`):** `settle_layout(webview)`.

- [ ] **Step 1: Write the failing tests**
  - `test_fixed_sleeps_are_justified`: every source line of the script that contains `time.sleep(`, `SLEEP(`, or `pump(` (excluding the `def pump` line) also contains `# sleep-ok:`.
  - `test_geocoder_stub_records_completion_after_the_response`: with the stub entered, `urllib.request.urlopen(f"{stub.url}/search?q=fasttown").read()` succeeds, and then `stub.completed("fasttown")` is True and `stub.completed("slowtown")` is False.
- [ ] **Step 2: Run them and confirm they fail**
  `python3 -m unittest tests.test_integration.UiRegressionScriptContractTest -v`. Expect `AssertionError: unjustified sleep: time.sleep(3.5)  # the delayed older response…` and `AttributeError: 'GeocoderStub' object has no attribute 'completed'`.
- [ ] **Step 3: Implement** the replacements above.
- [ ] **Step 4: Run and confirm it passes**
  `python3 -m unittest tests.test_integration -v`, then under Xvfb `python3 scripts/test-ui-regression --only search --only motion --only alerts --only stripped-debug --only layouts`.
- [ ] **Step 5: Commit**
  `git add scripts/test-ui-regression tests/test_integration.py && git commit -m "test: wait for explicit completion instead of fixed sleeps in the UI regressions"`

### Task 15: Workstream verification

**Files:** None (verification only).
- [ ] **Step 1:** `make check PYTHON="python3 -W error::ResourceWarning"`. Expect exit 0, including `node --check` and `node --test tests/js/*.test.js`.
- [ ] **Step 2:** Run the three live scripts as in CI's `webkit-ui` job. This host has no Xvfb, and `keyboard-map` needs X11 with XTEST, so use the job's own image and package list plus `libXtst`. Run from the W3 worktree:
  ```bash
  podman run --rm -v "$PWD":/src:ro,Z fedora:44 bash -euo pipefail -c '
    dnf install -y git-core python3-gobject gobject-introspection gtk4 libadwaita webkitgtk6.0 libnotify xorg-x11-server-Xvfb xorg-x11-xauth dbus-daemon util-linux shadow-utils libXtst
    useradd --create-home --user-group airloom-ci
    workdir="$(mktemp -d)"; cp -a /src "${workdir}/src"; mkdir -m 700 "${workdir}/runtime"; chown -R airloom-ci:airloom-ci "${workdir}"; cd "${workdir}/src"
    export NO_AT_BRIDGE=1 WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1
    for script in scripts/test-heatmap scripts/test-save-refresh; do
      runuser -u airloom-ci -- env XDG_RUNTIME_DIR="${workdir}/runtime" NO_AT_BRIDGE=1 WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1 xvfb-run -a -s "-screen 0 1280x800x24" "${script}"
    done
    runuser -u airloom-ci -- env XDG_RUNTIME_DIR="${workdir}/runtime" NO_AT_BRIDGE=1 WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1 dbus-run-session -- xvfb-run -a -s "-screen 0 1280x800x24" env GDK_DEBUG=no-portals python3 scripts/test-ui-regression'
  ```
  Expect `PASS: 16 UI regression scenario(s)` with no skips, plus PASS from both smaller scripts.
- [ ] **Step 3:** Run the layout matrix three times in a row (`--only layouts`) to show that `settle_layout` is stable. Record the case count: 30 cases × 7 states, plus 15 preview-action rows.
- [ ] **Step 4:** Record the following in the W3 report:
  - the exact `app.js` regions changed;
  - confirmation that `window.Airloom.receive` (`app.js:89–103`) is untouched;
  - the two `tests/test_integration.py` regions edited;
  - the list of existing provenance assertions changed under decision 4 (Task 2).

## Cross-workstream contracts

1. **Freshness rule (W1 → W3).**
   - W3 mirrors W1's `airloom/models.py` (`MAX_OBSERVATION_AGE = 7200`, `FUTURE_SKEW_TOLERANCE = 600`, `is_fresh_observation`) as `AirloomCore.isFreshObservation(lastSeen, nowSeconds)`, with `AirloomCore.MAX_OBSERVATION_AGE` and `AirloomCore.FUTURE_SKEW_TOLERANCE`. Bounds are inclusive. `null`, `undefined`, non-numbers, and non-finite values are not fresh.
   - The page clock is `Date.now() / 1000` (epoch seconds, the same wall clock as Python's `time.time()` / `controller._wall_now()`). Node tests inject it with `loadApp({ nowMs })` / `setNow(ms)`.
   - "Old" = `readingProvenance(...) === "Old"`, that is `!isFreshObservation(sensor.last_seen, now)` once Demo and Stale don't apply. Precedence is **Demo > Stale > Old**. A missing `last_seen` is Old.
   - In W3's branch, the cross-language test compares JS against Python's real `presentation.alert_transitions` for ages `0, 7200, 7201, null`, because `airloom.models.is_fresh_observation` doesn't exist there yet.
   - **At integration (05 Task 1 Step 3):** switch that snippet to `airloom.models.is_fresh_observation` and `FUTURE_SKEW_TOLERANCE`. Extend the vectors to W1's full set: `0, 7200, -600` fresh; `7201, -601, null` not.
   - W3 consumes only fields the page already receives: per-sensor `last_seen`, `origin`, `delivery`, `remote_favorite`, plus batch `source` and `stale`. W3 asks for no new payload fields.
2. **Shell recovery (W2 ↔ W3).** W3 does not edit `window.Airloom.receive` (`app.js:89–103`). W3's `app.js` additions sit outside it: `debugState.navigationIdle` inside the `window.Airloom` object literal, `onMapKeydown`, tile retry, `tickAges`, and the provenance helpers. W2's new shell-recovery event is consumed only through W2's own handler.
3. **Band foregrounds (W3 → everyone).** `#2a0509` (Unhealthy), `#12061c` (Very unhealthy), and `#0b0e11` (Unavailable) change identically in `aqi.py` and `core.js`. The new `band tables match Python exactly` test guards drift. No background color changes. The only Python consumer of `foreground` is `Sensor.to_dict()`.
4. **CI package (W3 → W4).** The `keyboard-map` scenario loads `libXtst.so.6` via `ctypes`. The `webkit-ui` job's `dnf install` line in `.github/workflows/flatpak.yml` must add `libXtst`; no current dependency pulls it in. W3's local container run (Task 15) installs it explicitly. Until W4 adds it, the hosted `webkit-ui` job fails loudly at `keyboard-map`; it never skips.
5. **Docs and screenshots (W3 → integration, 05 Task 4).**
   - The README must document keyboard map panning: Tab to the map, arrows (Shift for farther), and `+`/`-`.
   - It must also document the preview's disabled settings.
   - Screenshots use demo data, so every reading in them correctly shows a **Demo** badge. That is expected, not "spurious".
   - The release notes must state the narrowed badge rule: Demo, Stale, and Old only, with no "Cached" label.
6. **Live harness scenarios (W3 → integration).**
   - The scenario list gains `keyboard-map`.
   - `trend-retry` keeps its name but becomes a standalone fixture scenario.
   - The layout matrix grows to 30 cases (dark themes at 320 and 375 px).
   - The integration replay must run all of these in real Docker.

## Self-review

**Spec coverage:**
- Finding 5 (per-reading provenance, mixed fixtures, no Cached, Old on the shared rule) → Task 2. Old's minute-level freshness is also covered by Task 3.
- Finding 6 (contrast, detail badge, dark Save, parity, both themes) → Task 1.
- Finding 14 (tile retry, Refresh, retained imagery) → Task 7.
- Finding 15 (focusable named map, focus ring, arrows, Shift, +/-, not in inputs/dialogs/popups, same navigation path, accessible description, real Tab-then-arrows live test) → Task 6. README → contract 5.
- Finding 16 (name, description, hidden table, visible per-point labels, "current vs rolling average") → Task 4.
- Finding 17 (320/375 × 100/125/150 × light/dark, expanded three-digit detail with long category and badge, long place name) → Task 12.
- Finding 18 (disable and explain refresh, radius, and automatic location, plus alert threshold and confidence; observable effect per enabled setting) → Task 8.
- Finding 21 (real failure and retry via real request identity, terminal rendered state, asserted values) → Task 5.
- Minors:
  - search dismissal → Task 9;
  - hidden-sensors focus → Task 10;
  - ticking ages → Task 3;
  - `descendants()` → Task 13;
  - fixed completion sleeps (all eight sites) → Task 14;
  - deferred fixed-home search styling → Task 11.
- Verification → Task 15.

**Type consistency:**
- `readingProvenance` returns exactly `"" | "Demo" | "Stale" | "Old"`.
- The same strings appear in `PROVENANCE_DESCRIPTIONS`, the badge text, the accessible names, the live checks, and the layout `"detail"` state (`Stale`).
- `isFreshObservation(lastSeen, nowSeconds)` takes seconds on both sides. Every test uses `NOW_S` seconds, with `nowMs = NOW_S * 1000` only for the injected `Date`.
- `debugState().navigationIdle` (Task 6) is the field Task 14 waits on.
- `#trend-values tr[data-window]` (Task 4) is what `TREND_PROBE_JS` (Task 5) reads.
- `timerDelays()` is introduced in Task 3 and reused in Task 7.

**Review Focus coverage:**
- undated or future readings → Task 2, `missing and far-future…`;
- per-batch leakage → Task 2, the three mixed fixtures;
- key hijacking → Task 6, `map keys are ignored…`;
- time passing without refetch → Task 3, `the minute tick flips…`;
- tile storms and retained imagery → Task 7, `tile retry backoff is capped…` and `retained imagery stays…`.

**Ambiguities resolved:**
- **Old for a missing timestamp:** Old, per W1's contract.
- **Old for a timestamp more than 10 minutes in the future:** also Old, and its age reads "Update time unavailable".
- **The summary badge:** it shows the badge of the median reading it displays, the same per-reading rule.
- **The unavailable gray's white text:** it fails at 3.73:1, so its foreground changes too, to `#0b0e11`.
- **Preview settings:** the preview also disables the alert threshold and the confidence filter, because the preview has no alerts and no confidence data.
- **Manual Refresh from the header bar:** it reaches the page only as a `sensors` publication, so publications also retry failed tiles.
- **Real keyboard input:** it uses XTEST under X11, because GTK4 has no public key-synthesis API and the debug port belongs to W2.
- **Badge layout:** one badge per reading, not stacked.
