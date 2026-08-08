# Heat Map Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the visible map width exceeds a user-configurable distance (default 40 km), sensor markers are replaced by an interpolated AQI heat map; zooming back in restores the markers.

**Architecture:** All rendering logic lives in `airloom/resources/app.js` (a new canvas layer + mode switch in the existing render paths). Python only persists and round-trips one new config key, `heatmap_threshold_km`, through `store.py`, `app.py`'s save-settings handler, and the settings dialog. Verification is a `store.py` unit test plus a new end-to-end script that drives the live app over the existing debug port.

**Tech Stack:** Python stdlib + PyGObject (existing), hand-written vanilla JS/CSS (existing). **Zero third-party dependencies — do not add any pip package or JS library.**

Spec: `docs/superpowers/specs/2026-08-07-heatmap-mode-design.md` (read it first).

## Global Constraints

- No new dependencies of any kind (CLAUDE.md rule).
- Never touch GTK/WebKit from a worker thread (not applicable here, but binding).
- `make check` (tests + `compileall` + `node --check` on app.js) must pass before the branch is done.
- New config key: `heatmap_threshold_km`, float, default `40.0`, valid range `5.0–1000.0`.
- Legend palette (already used in `index.html` and `demo.py`): Good `#35b779`, Moderate `#f6c945`, Sensitive `#f39c3d`, Unhealthy `#e65b65`.
- Work on a feature branch (e.g. `feature/heatmap-mode`), not `main`.
- The e2e script requires GTK and a display; it is NOT added to `make test`/`make check`.

---

### Task 1: Persist `heatmap_threshold_km` in the store

**Files:**
- Modify: `airloom/store.py` (DEFAULT_CONFIG ~line 10, `_sanitize` ~line 43, `public_config` ~line 98)
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `Store().data["heatmap_threshold_km"]` (float, default 40.0, clamped 5–1000) and the same key in `Store().public_config()`. Task 2 (app.py) and Task 3 (app.js, via the `config` bridge event which sends `public_config()`) rely on this exact key name.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_store.py` inside `StoreTest` (before `if __name__`):

```python
    def test_heatmap_threshold_defaults_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            self.assertEqual(store.data["heatmap_threshold_km"], 40.0)
            self.assertEqual(store.public_config()["heatmap_threshold_km"], 40.0)
            store.data["heatmap_threshold_km"] = 120.0
            store.save()
            self.assertEqual(Store(path).data["heatmap_threshold_km"], 120.0)

    def test_heatmap_threshold_invalid_values_fall_back(self):
        for bad in ("wide", 4, 1001, float("inf"), True):
            with self.subTest(bad=bad):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "config.json"
                    path.write_text(json.dumps({"heatmap_threshold_km": bad}), encoding="utf-8")
                    self.assertEqual(Store(path).data["heatmap_threshold_km"], 40.0)
```

(`json.dumps(float("inf"))` emits `Infinity`, which Python's `json.loads` accepts back — that's deliberate; `_sanitize`'s `isfinite` check must reject it. `True` must be rejected by the existing bool exclusion.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_store -v`
Expected: the two new tests FAIL with `KeyError: 'heatmap_threshold_km'`; all others pass.

- [ ] **Step 3: Implement** — three one-line edits in `airloom/store.py`:

In `DEFAULT_CONFIG`, after `"radius_km": 22.0,`:

```python
    "heatmap_threshold_km": 40.0,
```

In `_sanitize`, after `number("radius_km", 2.0, 100.0)`:

```python
    number("heatmap_threshold_km", 5.0, 1000.0)
```

In `public_config()`'s dict, after `"radius_km": ...,`:

```python
            "heatmap_threshold_km": self.data["heatmap_threshold_km"],
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_store -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add airloom/store.py tests/test_store.py
git commit -m "Persist heatmap_threshold_km with default 40 km"
```

---

### Task 2: Settings plumbing (dialog field → bridge → app.py)

**Files:**
- Modify: `airloom/resources/index.html` (form grid, ~line 136)
- Modify: `airloom/resources/app.js` (`state.config` default ~line 8, `openSettings` ~line 567, submit handler ~line 822)
- Modify: `airloom/app.py` (`_save_settings`, ~line 571)

**Interfaces:**
- Consumes: `heatmap_threshold_km` from Task 1's store.
- Produces: the settings dialog round-trips the value; the `save-settings` bridge message carries a `heatmap_threshold_km` field which `app.py` clamps to 5–1000 and stores. Task 3's JS reads `state.config.heatmap_threshold_km` (arrives via the existing `config` event — no new bridge events).

- [ ] **Step 1: Add the dialog input** — in `index.html`, immediately after the `Map radius (km)` label (line 136):

```html
          <label><span>Heat map beyond (km)</span><input name="heatmap_threshold_km" type="number" step="1" min="5" max="1000" required></label>
```

- [ ] **Step 2: Plumb it through app.js** — three edits:

In the `state.config` default object (line 8), after `radius_km: 22,`, add `heatmap_threshold_km: 40,`.

In `openSettings` (line 567), extend the prefill loop:

```js
    for (const field of ["radius_km", "alert_threshold", "heatmap_threshold_km"]) form.elements[field].value = config[field];
```

In the submit handler's `bridge({...})` payload (line 827), on the `radius_km` line, add `heatmap_threshold_km: form.get("heatmap_threshold_km"),` after `radius_km: form.get("radius_km"),`.

- [ ] **Step 3: Accept it in app.py** — in `_save_settings` (line 571), inside the `try`, after the `radius = ...` line add:

```python
            heatmap = max(5.0, min(1000.0, float(message["heatmap_threshold_km"])))
```

and add to the `updates` dict after `"radius_km": radius,`:

```python
                "heatmap_threshold_km": heatmap,
```

- [ ] **Step 4: Syntax checks**

Run: `node --check airloom/resources/app.js && python3 -m compileall -q airloom && python3 -m unittest discover -s tests`
Expected: no errors, all tests pass. (The full save path is exercised end-to-end by Task 5.)

- [ ] **Step 5: Commit**

```bash
git add airloom/resources/index.html airloom/resources/app.js airloom/app.py
git commit -m "Add heat map threshold to preferences dialog and save path"
```

---

### Task 3: Heat map rendering and mode switching in app.js

**Files:**
- Modify: `airloom/resources/index.html` (map panel layers, ~line 15)
- Modify: `airloom/resources/app.css` (layer styles, ~line 32)
- Modify: `airloom/resources/app.js` (map section, `renderFrame`, `renderMapMarkers`, `debugState`)

**Interfaces:**
- Consumes: `state.config.heatmap_threshold_km` (Tasks 1–2), existing `visibleSensors()`, `worldPoint()`, `mapViewport()`, `hidePopup()`.
- Produces: `viewportWidthKm(): number` and `heatmapActive(): boolean` (used by `debugState` and Task 4's `debugZoomTo`); `window.Airloom.debugState()` gains `heatmapActive` and `viewportKm` keys (Task 5 asserts on them); a `<canvas id="heatmap">` whose painted pixels Task 5 counts.

- [ ] **Step 1: Add the canvas layer** — in `index.html`, between the `#tiles` div and the `#markers` div:

```html
        <canvas id="heatmap" class="heatmap-layer" hidden></canvas>
```

- [ ] **Step 2: Style it** — in `app.css`, after the `.marker-layer` rule (line 32):

```css
.heatmap-layer { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; filter: blur(3px); }
.heatmap-layer[hidden] { display: none; }
```

(The canvas bitmap is one pixel per coarse grid cell; stretching it to the full panel with the browser's default smoothing plus the blur produces the soft field look. `pointer-events: none` keeps the heat map passive per the spec — drags and zooms pass through to the map panel.)

- [ ] **Step 3: Add mode + field computation to app.js** — insert after the `inverseWorld` function (line 306):

```js
  // --- Heat map mode (spec: docs/superpowers/specs/2026-08-07-heatmap-mode-design.md) ---
  const EARTH_CIRCUMFERENCE_KM = 40075.016686;
  const HEATMAP_CELL = 8;        // CSS px per field-grid cell
  const HEATMAP_RADIUS = 90;     // screen-px influence radius per sensor
  const HEATMAP_MAX_ALPHA = 0.55;
  let heatmapWasActive = false;

  function viewportWidthKm() {
    const panel = $("#map-panel");
    if (!panel.clientWidth) return 0;
    return panel.clientWidth / (256 * 2 ** state.zoom) * EARTH_CIRCUMFERENCE_KM *
      Math.cos(clampLat(state.center.lat) * Math.PI / 180);
  }

  function heatmapActive() {
    return viewportWidthKm() > Number(state.config.heatmap_threshold_km || 40);
  }

  // Same buckets as the legend and demo.py; interpolated AQI values take the
  // color of the bucket they land in, so every on-screen color is in the legend.
  function aqiBucketColor(aqi) {
    if (aqi <= 50) return [53, 183, 121];   // #35b779 Good
    if (aqi <= 100) return [246, 201, 69];  // #f6c945 Moderate
    if (aqi <= 150) return [243, 156, 61];  // #f39c3d Sensitive
    return [230, 91, 101];                  // #e65b65 Unhealthy
  }

  // Re-render markers exactly once per threshold crossing; a marker popup
  // cannot survive into heat-map mode.
  function syncHeatmapMode() {
    const active = heatmapActive();
    if (active === heatmapWasActive) return active;
    heatmapWasActive = active;
    if (active) hidePopup();
    renderMapMarkers();
    return active;
  }

  function clearHeatmap() {
    const canvas = $("#heatmap");
    if (canvas.hidden) return;
    canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
    canvas.hidden = true;
  }

  function renderHeatmap(view) {
    const canvas = $("#heatmap");
    const cols = Math.max(1, Math.ceil(view.width / HEATMAP_CELL));
    const rows = Math.max(1, Math.ceil(view.height / HEATMAP_CELL));
    if (canvas.width !== cols || canvas.height !== rows) { canvas.width = cols; canvas.height = rows; }
    const ctx = canvas.getContext("2d");
    const image = ctx.createImageData(cols, rows);
    const pad = HEATMAP_RADIUS + HEATMAP_CELL;
    const points = [];
    for (const sensor of visibleSensors()) {
      if (!Number.isFinite(sensor.aqi)) continue;
      const point = worldPoint(sensor.latitude, sensor.longitude, state.zoom);
      const x = point.x - view.left, y = point.y - view.top;
      if (x < -pad || x > view.width + pad || y < -pad || y > view.height + pad) continue;
      points.push({ x, y, aqi: sensor.aqi });
    }
    const radius2 = HEATMAP_RADIUS * HEATMAP_RADIUS;
    for (let gy = 0; points.length && gy < rows; gy++) {
      const cy = (gy + 0.5) * HEATMAP_CELL;
      for (let gx = 0; gx < cols; gx++) {
        const cx = (gx + 0.5) * HEATMAP_CELL;
        let weightSum = 0, aqiSum = 0, nearest2 = radius2;
        for (const point of points) {
          const dx = cx - point.x, dy = cy - point.y;
          const d2 = dx * dx + dy * dy;
          if (d2 > radius2) continue;
          const w = 1 / (d2 + 4); // +4 keeps the weight finite directly over a sensor
          weightSum += w;
          aqiSum += point.aqi * w;
          if (d2 < nearest2) nearest2 = d2;
        }
        if (!weightSum) continue;
        const [r, g, b] = aqiBucketColor(aqiSum / weightSum);
        // Feather toward the influence edge so blobs fade out instead of
        // ending in a hard circle.
        const edge = 1 - Math.sqrt(nearest2) / HEATMAP_RADIUS;
        const alpha = HEATMAP_MAX_ALPHA * Math.min(1, edge * 1.6);
        const offset = (gy * cols + gx) * 4;
        image.data[offset] = r;
        image.data[offset + 1] = g;
        image.data[offset + 2] = b;
        image.data[offset + 3] = Math.round(alpha * 255);
      }
    }
    ctx.putImageData(image, 0, 0);
    canvas.hidden = false;
  }
```

- [ ] **Step 4: Hook into the render paths** — two edits:

In `renderFrame()` (line 358), replace the final `layoutMarkers(view);` line with:

```js
    layoutMarkers(view);
    if (syncHeatmapMode()) renderHeatmap(view);
    else clearHeatmap();
```

In `renderMapMarkers()` (line 505), right after the `if (!view.width) return;` guard, add:

```js
    if (heatmapActive()) { $("#markers").innerHTML = ""; return; }
```

(`renderFrame` runs on every pan/zoom frame, so the field re-renders in lockstep with tiles, and `syncHeatmapMode` re-renders markers exactly once per threshold crossing. All other `renderMapMarkers()` callers — sensor updates, search input, drag end — behave correctly because the guard clears the layer while the heat map is active.)

- [ ] **Step 5: Expose the mode to the debug port** — in `debugState` (line 49), after `visibleCount: ...,` add:

```js
      heatmapActive: heatmapActive(),
      viewportKm: viewportWidthKm(),
```

- [ ] **Step 6: Syntax check + browser preview sanity check**

Run: `node --check airloom/resources/app.js`
Expected: clean.

Then open the preview (demo data, no bridge): `xdg-open airloom/resources/index.html` — zoom out with the − button until the view is wider than 40 km (4–5 clicks from the default). Expected: markers disappear, a soft green/yellow blended field appears over the Portland demo cluster; zooming back in restores markers. This is a quick human check; the binding verification is Task 5.

- [ ] **Step 7: Commit**

```bash
git add airloom/resources/index.html airloom/resources/app.css airloom/resources/app.js
git commit -m "Render an interpolated AQI heat map past the zoom-out threshold"
```

---

### Task 4: `debugZoomTo` accessor for deterministic zoom over the debug port

**Files:**
- Modify: `airloom/resources/app.js` (`window.Airloom` debug accessors, ~line 70)

**Interfaces:**
- Consumes: existing `cancelZoomAnimation()`, `applyZoom()`, `centerAnchor()`, `renderMapMarkers()`, `scheduleViewChanged()`.
- Produces: `window.Airloom.debugZoomTo(zoom: number): number` — jumps instantly (no animation) to the given zoom anchored at the panel center, returns the resulting `state.zoom` (clamped 3–17 by `applyZoom`). Task 5 calls it via the debug port's `eval` command.

No new debug-port *command* is added — `eval` reaches this accessor — so `airloom/debugport.py` and `tests/test_debugport.py` are untouched.

- [ ] **Step 1: Add the accessor** — in the `window.Airloom` object, after the `debugTap` method (line 85), add:

```js
    debugZoomTo(zoom) {
      cancelZoomAnimation();
      applyZoom(Number(zoom), centerAnchor());
      renderMapMarkers();
      scheduleViewChanged();
      return state.zoom;
    },
```

(`applyZoom` calls `renderFrame`, which runs `syncHeatmapMode` — so a debug zoom exercises the exact same transition path as user zooming.)

- [ ] **Step 2: Syntax check**

Run: `node --check airloom/resources/app.js`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add airloom/resources/app.js
git commit -m "Add debugZoomTo accessor for deterministic zoom in debug tests"
```

---

### Task 5: End-to-end debug-port test, iterated until green

**Files:**
- Create: `scripts/test-heatmap` (executable, python3 stdlib only)

**Interfaces:**
- Consumes: the debug port protocol (`airloom/debugport.py`: newline-delimited JSON over `AIRLOOM_DEBUG_SOCKET`; `eval` replies `{"ok": true, "result": <JSON-decoded JS value>}` — see `_debug_evaluate` in `app.py:928`), `scripts/debug-run`, `window.Airloom.debugZoomTo` (Task 4), `debugState().heatmapActive/.viewportKm` (Task 3).
- Produces: a script that exits 0 when the feature works end-to-end, non-zero with a clear assertion message otherwise. Screenshots land in a temp dir whose path is printed.

- [ ] **Step 1: Write the script** — create `scripts/test-heatmap`:

```python
#!/usr/bin/env python3
"""End-to-end test for heat map mode, driven over the debug port.

Launches the app via scripts/debug-run on a private socket, waits for
sensors, then uses window.Airloom.debugZoomTo (through `eval`) to cross
the heat-map threshold in both directions, asserting that markers and the
heat map canvas swap correctly. Requires GTK and a display; not part of
`make test`. Exits 0 on success.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

PAINTED_CELLS_JS = """
(() => {
  const canvas = document.querySelector("#heatmap");
  if (canvas.hidden || !canvas.width) return 0;
  const data = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
  let painted = 0;
  for (let i = 3; i < data.length; i += 4) if (data[i] > 0) painted++;
  return painted;
})()
"""


def request(sock_path: str, cmd: str, **fields):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(15)
    try:
        sock.connect(sock_path)
        sock.sendall(json.dumps({"id": 1, "cmd": cmd, **fields}).encode("utf-8") + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed before a full response")
            buf += chunk
    finally:
        sock.close()
    response = json.loads(buf.split(b"\n", 1)[0])
    if not response.get("ok"):
        raise AssertionError(f"debug command {cmd!r} failed: {response}")
    return response.get("result")


def eval_js(sock_path: str, js: str):
    return request(sock_path, "eval", js=js)


def wait_for(probe, timeout_s: float, what: str):
    deadline = time.monotonic() + timeout_s
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = probe()
            if value is not None:
                return value
        except (OSError, ConnectionError, AssertionError) as exc:
            last_error = exc
        time.sleep(0.4)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {what} (last error: {last_error})")


def check(condition: bool, message: str):
    if not condition:
        raise AssertionError(message)
    print(f"  ok: {message}")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="airloom-heatmap-test-"))
    sock_path = str(workdir / "debug.sock")
    env = {**os.environ, "AIRLOOM_DEBUG_SOCKET": sock_path}
    print(f"work dir (screenshots): {workdir}")
    app = subprocess.Popen([str(PROJECT / "scripts" / "debug-run")], env=env)
    try:
        wait_for(lambda: request(sock_path, "ping") or True, 25, "debug socket")
        wait_for(
            lambda: (request(sock_path, "state").get("sensorCount", 0) > 0) or None,
            30,
            "sensors to load",
        )

        print("— markers mode (zoom 14) —")
        eval_js(sock_path, "window.Airloom.debugZoomTo(14)")
        state = request(sock_path, "state")
        check(state["viewportKm"] < 40, f"viewport {state['viewportKm']:.1f} km is under the 40 km default")
        check(state["heatmapActive"] is False, "heatmapActive is false when zoomed in")
        markers = eval_js(sock_path, "document.querySelectorAll('.map-marker').length")
        check(markers > 0, f"{markers} markers rendered when zoomed in")
        check(eval_js(sock_path, PAINTED_CELLS_JS) == 0, "heat map canvas is blank when zoomed in")
        request(sock_path, "screenshot", path=str(workdir / "1-markers.png"))

        print("— heat map mode (zoom 8) —")
        eval_js(sock_path, "window.Airloom.debugZoomTo(8)")
        state = request(sock_path, "state")
        check(state["viewportKm"] > 40, f"viewport {state['viewportKm']:.1f} km exceeds the 40 km default")
        check(state["heatmapActive"] is True, "heatmapActive is true when zoomed out")
        markers = eval_js(sock_path, "document.querySelectorAll('.map-marker').length")
        check(markers == 0, "no markers rendered in heat map mode")
        painted = eval_js(sock_path, PAINTED_CELLS_JS)
        check(painted > 0, f"heat map canvas has {painted} painted cells")
        check(eval_js(sock_path, "document.querySelector('#map-popup').hidden") is True,
              "marker popup is closed in heat map mode")
        request(sock_path, "screenshot", path=str(workdir / "2-heatmap.png"))

        print("— back to markers (zoom 14) —")
        eval_js(sock_path, "window.Airloom.debugZoomTo(14)")
        state = request(sock_path, "state")
        check(state["heatmapActive"] is False, "heatmapActive is false again after zooming back in")
        markers = eval_js(sock_path, "document.querySelectorAll('.map-marker').length")
        check(markers > 0, f"{markers} markers restored after zooming back in")
        check(eval_js(sock_path, PAINTED_CELLS_JS) == 0, "heat map canvas cleared after zooming back in")
        request(sock_path, "screenshot", path=str(workdir / "3-markers-again.png"))

        print("PASS")
        return 0
    finally:
        try:
            request(sock_path, "quit")
            app.wait(timeout=10)
        except (AssertionError, OSError, ConnectionError, subprocess.TimeoutExpired):
            app.kill()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
```

Make it executable: `chmod +x scripts/test-heatmap`.

- [ ] **Step 2: Run it against the real app and iterate until green**

Run: `scripts/test-heatmap`
Expected on first runs: possibly FAIL — this is the iteration loop the feature must survive. Debug with the printed assertion, the screenshots in the work dir, and ad-hoc `scripts/debug-client eval '...'` probes against a `scripts/debug-run` instance. Known things to watch for (fix the app, not the assertion, unless the assertion is wrong):
  - `state["viewportKm"]` at zoom 14 on an unusually wide window can exceed 40 km (40 km needs ~1350 px at 39°N/zoom 14 — safe on normal windows; if it trips, zoom the "markers mode" phase to 15, which is safe to ~5400 px).
  - `sensorCount > 0` but zero markers at zoom 14: the home center may sit between sensors so all are culled — pick the zoom-out phase first, or assert `visibleCount` instead; investigate before changing the test.
  - Marker-count assertions race the 1200 ms `scheduleViewChanged` refetch after a zoom: `debugZoomTo` re-renders synchronously, so counts are valid immediately, but a refetch that lands between eval calls can change them — if flaky, re-read `state` and marker count together in one `eval`.
Iterate: fix `app.js`/the script, re-run, until the script prints `PASS` and exits 0 **twice in a row**.

- [ ] **Step 3: Inspect the screenshots**

Open `2-heatmap.png` from the printed work dir and confirm visually: no circular markers, a soft color field over the sensor cluster in legend colors, map chrome (search pill, chips, controls) intact. Confirm `1-markers.png` and `3-markers-again.png` show ordinary markers.

- [ ] **Step 4: Commit**

```bash
git add scripts/test-heatmap
git commit -m "Add end-to-end debug-port test for heat map mode"
```

---

### Task 6: Full verification + changelog

**Files:**
- Modify: `CHANGELOG.md` (top of file)

**Interfaces:**
- Consumes: everything above.
- Produces: a branch ready for review/merge.

- [ ] **Step 1: Run the full check suite**

Run: `make check && scripts/test-heatmap`
Expected: `make check` fully green (unit tests, compileall, node --check); e2e prints PASS.

- [ ] **Step 2: Add a changelog entry** — CHANGELOG.md has no "Unreleased" section convention; releases add their own version heading. Add an `## Unreleased` section at the top (below the intro paragraph) so the next release can fold it in:

```markdown
## Unreleased

- Zooming out past a configurable view width (default 40 km, new "Heat map beyond (km)" preference) now merges the sensor dots into a translucent heat map of interpolated AQI in the legend's colors; zooming back in restores the individual markers.
```

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "Changelog: heat map mode for zoomed-out views"
```

- [ ] **Step 4: Finish the branch** — use the superpowers:finishing-a-development-branch skill (merge/PR decision belongs to the user).
