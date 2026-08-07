# Smooth Zoom, Marker Popups, Indoor/Outdoor Filter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Smooth anchored/animated map zoom (including trackpad pinch), a Paku-style marker popup with an open-in-PurpleAir link, and a persisted outdoor/indoor/both sensor filter.

**Architecture:** Three independent tracks designed for parallel worktrees: (A) rewrite the map zoom core in `app.js` around fractional zoom with integer tile levels plus a page-zoom guard in `app.py`; (B) add a popup layer to the map DOM; (C) thread a `location_filter` setting from `store.py` through `purpleair.py`/`demo.py`/`app.py` to a map chip in the web UI. Tracks A and B both edit `app.js` in different regions; the orchestrator resolves overlaps at merge time (Task D).

**Tech Stack:** Python stdlib + PyGObject (GTK4/WebKitGTK 6.0), hand-written vanilla JS/CSS. **Zero third-party dependencies — do not add any pip package or JS library.**

**Spec:** `docs/superpowers/specs/2026-08-07-zoom-popup-indoor-filter-design.md` (read it first).

## Global Constraints

- No new dependencies of any kind (CLAUDE.md).
- Tests are pure-stdlib `unittest`, GUI-free modules only; they must pass without GTK installed.
- `make check` (tests + `compileall` + `node --check` on `app.js`) must pass at every commit.
- Never touch GTK/WebKit from a worker thread; results re-enter via `GLib.idle_add`.
- The web UI must keep working in plain-browser preview mode (no bridge).
- JS/CSS style: match the existing terse, jQuery-free idiom in `app.js`/`app.css`.

---

### Task A: Fractional zoom core + trackpad pinch

Branch: `feature/smooth-zoom`. All in `airloom/resources/` plus one guard in `app.py`.

**Files:**
- Modify: `airloom/resources/app.js` (map section, roughly lines 194–337, plus wheel/button listeners ~460–478)
- Modify: `airloom/resources/index.html:14` (add retiring tile layer div)
- Modify: `airloom/resources/app.css:28-29` (transform-origin, tile fade)
- Modify: `airloom/app.py` (`_on_activate`, add `notify::zoom-level` guard)

**Interfaces:**
- Consumes: existing `worldPoint(lat, lon, zoom)` / `inverseWorld(x, y, zoom)` — already fractional-zoom-safe; do not change their signatures.
- Produces: `applyZoom(z, anchor)` (fractional zoom + anchored recenter + frame render), `animateZoomTo(target, anchor)` (retargetable ease-out to integer zoom), `renderFrame()` (cheap per-frame tile/transform/marker layout), `hideTransientOverlays()` — a no-op hook `function hideTransientOverlays() {}` called at the start of every zoom gesture and drag; Task B's popup close gets folded into it at merge. `renderMap()` remains the full-render entry point (`renderFrame()` + full `renderMapMarkers()`), called by `applyConfig`/`applySensors`/ResizeObserver.

- [ ] **Step A1: Add the retiring layer and CSS scaffolding**

In `index.html`, before the `#tiles` div:

```html
<div id="tiles-old" class="tile-layer" hidden></div>
```

In `app.css`, replace the `.tile-layer, .marker-layer` rule and `.tile` rule:

```css
.tile-layer, .marker-layer { position: absolute; inset: 0; will-change: transform; transform-origin: 0 0; }
.tile-layer[hidden] { display: none; }
.tile { position: absolute; width: 256px; height: 256px; user-select: none; -webkit-user-drag: none; filter: saturate(.7) contrast(.92) brightness(1.03); opacity: 0; transition: opacity .18s linear; }
.tile.loaded { opacity: 1; }
```

(The dark-mode `.tile` filter override stays as is.)

- [ ] **Step A2: Rewrite the map core in `app.js`**

Replace the block from `let tileLayer = ...` through `function zoom(delta) {...}` with:

```js
let tileLayer = { zoom: null, tiles: new Map(), pending: 0 };
let retireTimer = null;

function tileZoomLevel() { return Math.max(3, Math.min(17, Math.round(state.zoom))); }

function mapViewport() {
  const panel = $("#map-panel");
  const center = worldPoint(state.center.lat, state.center.lon, state.zoom);
  return {
    panel,
    left: center.x - panel.clientWidth / 2,
    top: center.y - panel.clientHeight / 2,
    width: panel.clientWidth,
    height: panel.clientHeight,
  };
}

function retireTiles() {
  const old = $("#tiles-old");
  old.textContent = "";
  const live = $("#tiles");
  while (live.firstChild) old.appendChild(live.firstChild);
  old.dataset.zoom = tileLayer.zoom;
  old.hidden = !old.firstChild;
  clearTimeout(retireTimer);
  retireTimer = setTimeout(clearRetiredTiles, 1500);
}

function clearRetiredTiles() {
  clearTimeout(retireTimer);
  const old = $("#tiles-old");
  old.textContent = "";
  old.hidden = true;
}

function renderFrame() {
  const view = mapViewport();
  if (!view.width || !view.height) return;
  const tz = tileZoomLevel();
  if (tileLayer.zoom !== tz) {
    if (tileLayer.zoom !== null) retireTiles();
    tileLayer = { zoom: tz, tiles: new Map(), pending: 0 };
  }
  const scale = 2 ** (state.zoom - tz);
  const maxTile = 2 ** tz;
  // Visible range in tile-zoom space (view coords are fractional-zoom space).
  const left = view.left / scale, top = view.top / scale;
  const right = (view.left + view.width) / scale, bottom = (view.top + view.height) / scale;
  const layer = $("#tiles");
  const needed = new Set();
  for (let ty = Math.floor(top / 256); ty <= Math.floor(bottom / 256); ty++) {
    if (ty < 0 || ty >= maxTile) continue;
    for (let tx = Math.floor(left / 256); tx <= Math.floor(right / 256); tx++) {
      const key = `${tx}/${ty}`;
      needed.add(key);
      if (!tileLayer.tiles.has(key)) {
        const wrappedX = ((tx % maxTile) + maxTile) % maxTile;
        const img = document.createElement("img");
        img.className = "tile";
        img.draggable = false;
        img.alt = "";
        tileLayer.pending++;
        img.onload = img.onerror = () => {
          img.classList.add("loaded");
          img.onload = img.onerror = null;
          if (--tileLayer.pending <= 0) clearRetiredTiles();
        };
        img.src = `https://tile.openstreetmap.org/${tz}/${wrappedX}/${ty}.png`;
        img.style.left = `${tx * 256}px`;
        img.style.top = `${ty * 256}px`;
        layer.appendChild(img);
        tileLayer.tiles.set(key, img);
      }
    }
  }
  for (const [key, img] of tileLayer.tiles) {
    if (!needed.has(key)) {
      if (!img.classList.contains("loaded")) tileLayer.pending--;
      img.onload = img.onerror = null;
      img.remove();
      tileLayer.tiles.delete(key);
    }
  }
  if (tileLayer.pending <= 0) clearRetiredTiles();
  layer.style.transform = `translate(${-view.left}px, ${-view.top}px) scale(${scale})`;
  const old = $("#tiles-old");
  if (!old.hidden) {
    const oldScale = 2 ** (state.zoom - Number(old.dataset.zoom));
    old.style.transform = `translate(${-view.left}px, ${-view.top}px) scale(${oldScale})`;
  }
  layoutMarkers(view);
}

function layoutMarkers(view = mapViewport()) {
  const markers = $("#markers");
  markers.style.transform = `translate(${-view.left}px, ${-view.top}px)`;
  const byId = new Map(state.sensors.map((sensor) => [sensor.id, sensor]));
  markers.querySelectorAll(".map-marker").forEach((element) => {
    const sensor = byId.get(Number(element.dataset.id));
    if (!sensor) return;
    const point = worldPoint(sensor.latitude, sensor.longitude, state.zoom);
    element.style.left = `${point.x}px`;
    element.style.top = `${point.y}px`;
  });
}

function renderMap() {
  renderFrame();
  renderMapMarkers();
}

// Transient overlays (Task B's popup) hook in here; base build has none.
function hideTransientOverlays() {}

function applyZoom(z, anchor) {
  const view = mapViewport();
  if (!view.width) return;
  z = Math.max(3, Math.min(17, z));
  const geo = inverseWorld(view.left + anchor.x, view.top + anchor.y, state.zoom);
  const point = worldPoint(geo.lat, geo.lon, z);
  state.zoom = z;
  state.center = inverseWorld(
    point.x - anchor.x + view.width / 2,
    point.y - anchor.y + view.height / 2,
    z,
  );
  renderFrame();
}

function centerAnchor() {
  const view = mapViewport();
  return { x: view.width / 2, y: view.height / 2 };
}

let zoomAnim = null;
function animateZoomTo(target, anchor) {
  hideTransientOverlays();
  target = Math.max(3, Math.min(17, Math.round(target)));
  if (zoomAnim) { zoomAnim.target = target; zoomAnim.anchor = anchor; return; }
  zoomAnim = { target, anchor };
  const step = () => {
    if (!zoomAnim) return;
    const diff = zoomAnim.target - state.zoom;
    if (Math.abs(diff) < 0.02) {
      applyZoom(zoomAnim.target, zoomAnim.anchor);
      zoomAnim = null;
      renderMapMarkers();
      scheduleViewChanged();
      return;
    }
    // Exponential ease-out: retargeting mid-flight stays smooth.
    applyZoom(state.zoom + diff * 0.22, zoomAnim.anchor);
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function cancelZoomAnimation() { zoomAnim = null; }
```

Notes:
- `mapViewport()` above is unchanged from today except it now sees fractional `state.zoom` — keep the existing function, shown for context.
- Delete the old `updateMapTransform()`; its two call sites in `renderMapMarkers`/`panBy` are replaced below.

- [ ] **Step A3: Rewire `renderMapMarkers`, `panBy`, `flyTo`, and listeners**

In `renderMapMarkers`, replace the trailing `updateMapTransform();` with `layoutMarkers(view);` (it already computed `view`). Markers are already positioned via `worldPoint(..., state.zoom)` — unchanged.

Replace `panBy` with:

```js
function panBy(dx, dy) {
  const center = worldPoint(state.center.lat, state.center.lon, state.zoom);
  state.center = inverseWorld(center.x - dx, center.y - dy, state.zoom);
  renderFrame();
  scheduleViewChanged();
}
```

In `flyTo`'s animation step, replace `renderMap()` with `renderFrame()` and add `renderMapMarkers()` next to the existing `scheduleViewChanged()` completion call.

Replace the zoom listeners:

```js
$("#zoom-in").addEventListener("click", (event) => { event.stopPropagation(); animateZoomTo((zoomAnim ? zoomAnim.target : Math.round(state.zoom)) + 1, centerAnchor()); });
$("#zoom-out").addEventListener("click", (event) => { event.stopPropagation(); animateZoomTo((zoomAnim ? zoomAnim.target : Math.round(state.zoom)) - 1, centerAnchor()); });
$("#map-panel").addEventListener("wheel", (event) => {
  if (event.ctrlKey) return; // pinch path; the document-level handler owns it
  event.preventDefault();
  const rect = event.currentTarget.getBoundingClientRect();
  const base = zoomAnim ? zoomAnim.target : Math.round(state.zoom);
  animateZoomTo(base + (event.deltaY < 0 ? 1 : -1), { x: event.clientX - rect.left, y: event.clientY - rect.top });
}, { passive: false });
```

Also add `hideTransientOverlays();` at the top of the `#map-panel` `pointerdown` handler (drag start).

Delete the old `function zoom(delta)`.

- [ ] **Step A4: Pinch handling (document level)**

Add near the other listeners:

```js
let pinch = null;
let pinchSettleTimer = null;
let lastZoomAnchor = null;

function mapAnchorFromClient(clientX, clientY) {
  const rect = $("#map-panel").getBoundingClientRect();
  return { x: clientX - rect.left, y: clientY - rect.top };
}

// WebKitGTK delivers trackpad pinch either as ctrl+wheel or as proprietary
// gesture* events depending on version/compositor. preventDefault on both,
// unconditionally, so the engine can never apply page zoom (which scaled the
// whole document and clipped the fixed overlays).
document.addEventListener("wheel", (event) => {
  if (!event.ctrlKey) return;
  event.preventDefault();
  cancelZoomAnimation();
  hideTransientOverlays();
  lastZoomAnchor = mapAnchorFromClient(event.clientX, event.clientY);
  applyZoom(state.zoom - event.deltaY * 0.01, lastZoomAnchor);
  clearTimeout(pinchSettleTimer);
  pinchSettleTimer = setTimeout(() => animateZoomTo(Math.round(state.zoom), lastZoomAnchor), 180);
}, { passive: false });

document.addEventListener("gesturestart", (event) => {
  event.preventDefault();
  cancelZoomAnimation();
  hideTransientOverlays();
  pinch = { startZoom: state.zoom };
}, { passive: false });
document.addEventListener("gesturechange", (event) => {
  event.preventDefault();
  if (!pinch) return;
  lastZoomAnchor = mapAnchorFromClient(event.clientX, event.clientY);
  applyZoom(pinch.startZoom + Math.log2(Math.max(0.05, event.scale)), lastZoomAnchor);
}, { passive: false });
document.addEventListener("gestureend", (event) => {
  event.preventDefault();
  if (!pinch) return;
  pinch = null;
  animateZoomTo(Math.round(state.zoom), lastZoomAnchor || centerAnchor());
}, { passive: false });
```

- [ ] **Step A5: Page-zoom guard in `app.py`**

In `_on_activate`, after `self.webview.connect("decide-policy", ...)`:

```python
self.webview.connect("notify::zoom-level", self._on_zoom_level_changed)
```

New method:

```python
def _on_zoom_level_changed(self, webview, _pspec) -> None:
    # The map handles pinch itself; engine-level page zoom would scale the
    # whole document and clip the overlays, so snap it straight back.
    if webview.get_zoom_level() != 1.0:
        webview.set_zoom_level(1.0)
```

- [ ] **Step A6: Verify**

Run: `node --check airloom/resources/app.js && make check`
Expected: PASS (no Python behavior changed except the guard; `compileall` covers it).

Then open `airloom/resources/index.html` in a browser (preview mode) and confirm: wheel zoom animates and stays anchored under the cursor; ctrl+wheel zooms smoothly and settles; +/− buttons animate; no blank flash between zoom levels (old tiles remain scaled while new fade in); dragging still pans; markers stay glued to their geo positions throughout.

- [ ] **Step A7: Commit**

```bash
git add airloom/resources/app.js airloom/resources/index.html airloom/resources/app.css airloom/app.py
git commit -m "Smooth anchored map zoom with animated transitions and trackpad pinch"
```

---

### Task B: Marker popup with PurpleAir link

Branch: `feature/marker-popup`. Pure frontend; builds against current `main` (integer-zoom map).

**Files:**
- Modify: `airloom/resources/index.html:15` (popup layer after `#markers`)
- Modify: `airloom/resources/app.js` (popup module + marker click behavior + close hooks)
- Modify: `airloom/resources/app.css` (popup styles)

**Interfaces:**
- Consumes: `worldPoint`, `selectSensor(id, revealDetail)`, `relativeTime`, `escapeHtml`, `state.source`, existing `updateMapTransform()` (adds `#popup-layer` to it).
- Produces: `showPopup(sensor)`, `hidePopup()`. Marker clicks call `selectSensor(id, false)` + `showPopup(sensor)` — the detail card no longer auto-opens from markers. Sensors with a truthy `.indoor` field (arrives with Task C; absent field = outdoor) are tagged "Indoor" in the popup meta line.

- [ ] **Step B1: Popup DOM**

In `index.html`, directly after the `#markers` div:

```html
<div id="popup-layer" class="marker-layer">
  <div class="map-popup" id="map-popup" hidden>
    <div class="popup-head">
      <span class="popup-aqi" id="popup-aqi">—</span>
      <div class="popup-copy"><strong id="popup-name"></strong><small id="popup-meta"></small></div>
      <button class="icon-button popup-star" id="popup-favorite" title="Favorite this sensor" aria-label="Favorite this sensor">
        <svg viewBox="0 0 24 24"><path d="m12 17.3-6.2 3.5 1.4-6.9-5.1-4.7 7-.8L12 2l2.9 6.4 7 .8-5.1 4.7 1.4 6.9-6.2-3.5Z"/></svg>
      </button>
    </div>
    <div class="popup-actions">
      <button id="popup-details" class="popup-action">Details</button>
      <a id="popup-purpleair" class="popup-action" target="_blank" rel="noreferrer">Open in PurpleAir ↗</a>
    </div>
  </div>
</div>
```

- [ ] **Step B2: Popup styles in `app.css`**

```css
.map-popup { position: absolute; z-index: 6; width: 232px; transform: translate(-50%, calc(-100% - 30px)); background: var(--panel-solid); border-radius: 13px; box-shadow: 0 8px 28px rgba(0,0,0,.3); padding: 11px 12px 9px; pointer-events: auto; }
.map-popup::after { content: ""; position: absolute; left: 50%; bottom: -6px; width: 12px; height: 12px; transform: translateX(-50%) rotate(45deg); background: var(--panel-solid); box-shadow: 4px 4px 8px rgba(0,0,0,.08); }
.map-popup[hidden] { display: none; }
.popup-head { display: flex; align-items: center; gap: 9px; }
.popup-aqi { min-width: 36px; height: 36px; border-radius: 50%; display: grid; place-items: center; font-weight: 800; font-size: 12px; background: var(--soft); }
.popup-copy { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.popup-copy strong { font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.popup-copy small { color: var(--muted); font-size: 10px; }
.popup-star { width: 28px; height: 28px; margin-left: auto; flex: none; }
.popup-star svg { width: 15px; }
.popup-star.active { color: #d18d12; }
.popup-actions { display: flex; gap: 6px; margin-top: 9px; }
.popup-action { flex: 1; text-align: center; border: 0; border-radius: 8px; padding: 6px 4px; background: var(--soft); color: var(--ink); font: inherit; font-size: 11px; font-weight: 700; cursor: pointer; text-decoration: none; }
.popup-action:hover { background: var(--accent-soft); }
```

- [ ] **Step B3: Popup logic in `app.js`**

Add `popupId: null` to the `state` object literal. Add after `selectedSensor()`:

```js
function showPopup(sensor) {
  const point = worldPoint(sensor.latitude, sensor.longitude, state.zoom);
  const popup = $("#map-popup");
  popup.style.left = `${point.x}px`;
  popup.style.top = `${point.y}px`;
  $("#popup-aqi").textContent = sensor.aqi ?? "—";
  $("#popup-aqi").style.background = sensor.color || "";
  $("#popup-aqi").style.color = sensor.foreground || "";
  $("#popup-name").textContent = sensor.name;
  $("#popup-meta").textContent = [sensor.category, sensor.indoor ? "Indoor" : "Outdoor", relativeTime(sensor.last_seen)].filter(Boolean).join(" · ");
  $("#popup-favorite").classList.toggle("active", Boolean(sensor.favorite));
  const demo = (state.source || "").includes("Demo");
  const link = $("#popup-purpleair");
  link.hidden = demo; // demo sensor ids do not exist on the public map
  if (!demo) link.href = `https://map.purpleair.com/1/mAQI/a10/p604800/cC0?select=${sensor.id}#14/${sensor.latitude}/${sensor.longitude}`;
  popup.hidden = false;
  state.popupId = sensor.id;
}

function hidePopup() {
  $("#map-popup").hidden = true;
  state.popupId = null;
}
```

Wire-up (with the other listeners):

```js
$("#popup-details").addEventListener("click", () => { $("#detail-card").hidden = false; renderDetail(); hidePopup(); });
$("#popup-favorite").addEventListener("click", () => { if (state.popupId !== null) bridge({ action: "favorite", id: state.popupId }); });
```

Behavior changes:
- In `renderMapMarkers`, the marker click handler becomes:
  `selectSensor(Number(marker.dataset.id), false); const sensor = state.sensors.find((s) => s.id === Number(marker.dataset.id)); if (sensor) showPopup(sensor);`
- In `applySensors`, after state is updated: if `state.popupId !== null`, re-show from fresh data — `const open = state.sensors.find((s) => s.id === state.popupId); open ? showPopup(open) : hidePopup();` (keeps the favorite star and AQI live).
- In `updateMapTransform`, apply the same transform to `$("#popup-layer")`.
- Close paths: add `hidePopup()` at the top of the `#map-panel` `pointerdown` handler (fires for drag start and plain map clicks; marker/popup clicks are excluded by the existing `closest("button")` guard — extend it to `event.target.closest("button, a, .map-popup")`); in `zoom()`; and as the *first* branch of the Escape cascade: `if (!$("#map-popup").hidden) hidePopup(); else if (...)`.
- Browser-preview favorite toggling has no bridge; guard: in the popup-star listener, when `!window.webkit?.messageHandlers?.airloom`, toggle `sensor.favorite` locally and call `renderAll()` + `showPopup(sensor)` instead of `bridge(...)`.

- [ ] **Step B4: Verify**

Run: `node --check airloom/resources/app.js && make check`
Expected: PASS.

Browser preview: click a marker → popup appears above it with name/AQI/category/updated line, no detail card; "Details" opens the card; star toggles; popup follows the map while dragging only until pointerdown hides it (acceptable: it closes on drag start); Escape and map-background clicks close it; PurpleAir link hidden (preview is demo data).

- [ ] **Step B5: Commit**

```bash
git add airloom/resources/index.html airloom/resources/app.css airloom/resources/app.js
git commit -m "Show a marker popup with details and PurpleAir link instead of jumping to the detail card"
```

---

### Task C: Indoor/outdoor location filter

Branch: `feature/location-filter`. Python model/store/client/app + map chip UI + all new tests.

**Files:**
- Modify: `airloom/store.py` (`DEFAULT_CONFIG`, `_sanitize`, `public_config`)
- Modify: `airloom/models.py` (`indoor` field)
- Modify: `airloom/purpleair.py` (`FIELDS`, `fetch_sensors` signature, parsing)
- Modify: `airloom/demo.py` (deterministic indoor subset)
- Modify: `airloom/app.py` (fetch plumbing, `set-location-filter` action, error copy)
- Modify: `airloom/resources/index.html:37-39` (filter chip), detail-card sensor-name row (indoor tag)
- Modify: `airloom/resources/app.js` (chip cycle, indoor marker class, preview filtering)
- Modify: `airloom/resources/app.css` (indoor marker shape, tag style)
- Test: `tests/test_store.py`, `tests/test_purpleair.py`, create `tests/test_demo.py`

**Interfaces:**
- Consumes: existing `Store`, `PurpleAirClient.fetch_sensors(bounds=..., show_only=...)`, bridge dispatch in `_on_script_message`.
- Produces: config key `location_filter` ∈ `{"outdoor","indoor","both"}` in `store.data` and `public_config()`; `Sensor.indoor: bool` (in `to_dict()` as `indoor`); `fetch_sensors(..., location_filter="outdoor")`; bridge action `{"action": "set-location-filter", "value": <mode>}`; JS `state.config.location_filter`.

- [ ] **Step C1: Failing tests first**

Append to `tests/test_store.py`:

```python
    def test_location_filter_defaults_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            self.assertEqual(store.data["location_filter"], "outdoor")
            self.assertEqual(store.public_config()["location_filter"], "outdoor")
            store.data["location_filter"] = "both"
            store.save()
            self.assertEqual(Store(path).data["location_filter"], "both")

    def test_invalid_location_filter_falls_back_to_outdoor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"location_filter": "underwater"}), encoding="utf-8")
            self.assertEqual(Store(path).data["location_filter"], "outdoor")
```

Append to `tests/test_purpleair.py` (reuse the `fake_urlopen` capture pattern from `test_fetch_sensors_builds_show_only_query`):

```python
    def _fetch_url(self, **kwargs):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            import io
            return mock.MagicMock(
                __enter__=lambda s: io.StringIO('{"fields": [], "data": []}'),
                __exit__=lambda s, *a: False,
            )

        with mock.patch("airloom.purpleair.urlopen", side_effect=fake_urlopen):
            PurpleAirClient("key").fetch_sensors(**kwargs)
        return captured["url"]

    def test_location_filter_maps_to_location_type_param(self):
        bounds = Bounds(46.0, -123.0, 45.0, -122.0)
        self.assertIn("location_type=0", self._fetch_url(bounds=bounds))
        self.assertIn("location_type=0", self._fetch_url(bounds=bounds, location_filter="outdoor"))
        self.assertIn("location_type=1", self._fetch_url(bounds=bounds, location_filter="indoor"))
        self.assertNotIn("location_type", self._fetch_url(bounds=bounds, location_filter="both"))

    def test_show_only_never_sends_location_filter(self):
        url = self._fetch_url(show_only=[42], location_filter="outdoor")
        self.assertNotIn("location_type", url)

    def test_location_type_field_is_requested_and_parsed(self):
        self.assertIn("location_type", self._fetch_url(bounds=Bounds(46.0, -123.0, 45.0, -122.0)))
        payload = {
            "fields": ["sensor_index", "latitude", "longitude", "location_type"],
            "data": [[1, 45.0, -122.0, 1], [2, 45.1, -122.1, 0], [3, 45.2, -122.2, None]],
        }
        sensors = {s.sensor_id: s for s in parse_sensor_payload(payload)}
        self.assertTrue(sensors[1].indoor)
        self.assertFalse(sensors[2].indoor)
        self.assertFalse(sensors[3].indoor)
        self.assertTrue(sensors[1].to_dict()["indoor"])
```

Create `tests/test_demo.py`:

```python
import unittest

from airloom.demo import demo_sensors


class DemoTest(unittest.TestCase):
    def test_demo_set_mixes_indoor_and_outdoor_deterministically(self):
        first = demo_sensors(45.5, -122.6)
        second = demo_sensors(45.5, -122.6)
        self.assertEqual(
            [sensor.indoor for sensor in first],
            [sensor.indoor for sensor in second],
        )
        indoor = [sensor for sensor in first if sensor.indoor]
        self.assertTrue(indoor)
        self.assertLess(len(indoor), len(first))
        for sensor in indoor:
            self.assertTrue(sensor.to_dict()["indoor"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step C2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_store tests.test_purpleair tests.test_demo -v`
Expected: FAIL — `KeyError: 'location_filter'`, `TypeError: fetch_sensors() got an unexpected keyword argument`, `AttributeError: ... no attribute 'indoor'`.

- [ ] **Step C3: Implement the Python side**

`store.py` — `DEFAULT_CONFIG` gains `"location_filter": "outdoor",`; in `_sanitize` (next to the `home_mode` check):

```python
    if data.get("location_filter") in ("outdoor", "indoor", "both"):
        clean["location_filter"] = data["location_filter"]
```

and `public_config()` gains `"location_filter": self.data["location_filter"],`.

`models.py` — add field after `favorite`:

```python
    indoor: bool = False
```

(`asdict` already carries it into `to_dict()`.)

`purpleair.py` — add `"location_type",` to `FIELDS`. New signature and param logic:

```python
    def fetch_sensors(
        self,
        bounds: Bounds | None = None,
        show_only: list[int] | None = None,
        location_filter: str = "outdoor",
    ) -> list[Sensor]:
        ...
        params: dict[str, str] = {"fields": ",".join(FIELDS)}
        if show_only:
            params["show_only"] = ",".join(str(sensor_id) for sensor_id in show_only)
        else:
            location_type = {"outdoor": "0", "indoor": "1"}.get(location_filter)
            if location_type is not None:
                params["location_type"] = location_type
            params.update({... nwlat/nwlng/selat/selng as today ...})
```

In `parse_sensor_payload`, pass `indoor=_integer(values.get("location_type")) == 1` to the `Sensor(...)` constructor.

`demo.py` — every fifth sensor is indoor: add `indoor=index % 5 == 2,` to the `Sensor(...)` call (gives 4 indoor out of 18, deterministic).

`app.py` — in `_start_fetch`'s worker:

```python
                    mode = config.get("location_filter", "outdoor")
                    if config.get("api_key"):
                        client = PurpleAirClient(config["api_key"])
                        sensors = client.fetch_sensors(bounds=bounds, location_filter=mode)
                        ...
                        if not sensors:
                            error = _no_sensors_message(mode)
                    else:
                        sensors = _filter_demo(demo_sensors(center[0], center[1]), mode)
                except PurpleAirError as exc:
                    sensors = _filter_demo(demo_sensors(center[0], center[1]), mode)
```

Module-level helpers in `app.py`:

```python
def _filter_demo(sensors: list[Sensor], mode: str) -> list[Sensor]:
    if mode not in ("indoor", "outdoor"):
        return sensors
    return [sensor for sensor in sensors if sensor.indoor == (mode == "indoor")]


def _no_sensors_message(mode: str) -> str:
    kind = {"outdoor": "outdoor ", "indoor": "indoor "}.get(mode, "")
    return f"No public {kind}sensors were found in this area."
```

Bridge action — in `_on_script_message`, before the unknown-action fallback:

```python
        elif action == "set-location-filter":
            self._set_location_filter(message)
```

```python
    def _set_location_filter(self, message: dict) -> None:
        value = message.get("value")
        if value not in ("outdoor", "indoor", "both"):
            print(f"Airloom: ignored invalid location filter: {value!r}", file=sys.stderr)
            return
        self.store.data["location_filter"] = value
        self.store.save()
        self._send("config", self.store.public_config())
        if self.current_view is not None:
            bounds, center = self.current_view
            self._start_fetch(bounds, center, include_favorites=True)
        else:
            self.refresh()
```

- [ ] **Step C4: Run tests to verify they pass**

Run: `make check`
Expected: PASS, including the new tests.

- [ ] **Step C5: Frontend chip, indoor markers, preview parity**

`index.html` — in `.corner-buttons`, after the legend chip:

```html
<button id="filter-chip" title="Cycle which sensors are shown">Outdoor</button>
```

In the detail card, change the sensor-name line to carry a tag:

```html
<p class="sensor-name"><span id="sensor-name">Choose a sensor</span><span class="indoor-tag" id="detail-indoor" hidden>Indoor</span></p>
```

`app.js`:
- `state.config` literal gains `location_filter: "outdoor"`.
- Add near `stampPlaceName`:

```js
  const FILTER_LABELS = { outdoor: "Outdoor", indoor: "Indoor", both: "All sensors" };
  const FILTER_NEXT = { outdoor: "indoor", indoor: "both", both: "outdoor" };
  function stampFilterChip() {
    $("#filter-chip").textContent = FILTER_LABELS[state.config.location_filter] || "Outdoor";
  }
```

- Call `stampFilterChip()` inside `applyConfig` (config events echo saved filter changes back).
- Listener:

```js
  $("#filter-chip").addEventListener("click", () => {
    state.config.location_filter = FILTER_NEXT[state.config.location_filter] || "indoor";
    stampFilterChip();
    if (window.webkit?.messageHandlers?.airloom) bridge({ action: "set-location-filter", value: state.config.location_filter });
    else renderAll(); // preview: filter locally, no bridge round-trip
  });
```

- Preview-only local filtering in `visibleSensors()` (Python already filters in the app, and API-fetched favorites must stay visible regardless of filter, so the app path must NOT re-filter):

```js
  function visibleSensors() {
    let sensors = state.sensors;
    if (!window.webkit?.messageHandlers?.airloom && state.config.location_filter !== "both") {
      sensors = sensors.filter((s) => Boolean(s.indoor) === (state.config.location_filter === "indoor"));
    }
    const query = state.query.trim().toLowerCase();
    return query ? sensors.filter((s) => s.name.toLowerCase().includes(query) || String(s.aqi).includes(query)) : sensors;
  }
```

- Indoor marker shape: in `renderMapMarkers`, the class string becomes `` `map-marker${sensor.indoor ? " indoor" : ""}${sensor.id === state.selectedId ? " selected" : ""}` ``.
- `renderDetail` adds `$("#detail-indoor").hidden = !sensor.indoor;`.
- `browserPreviewData()`: give each generated sensor `indoor: index % 5 === 2`.

`app.css`:

```css
.map-marker.indoor { border-radius: 26%; }
.indoor-tag { margin-left: 7px; padding: 2px 7px; border-radius: 99px; background: var(--soft); color: var(--muted); font-size: 9px; font-weight: 800; letter-spacing: .06em; text-transform: uppercase; vertical-align: 2px; }
```

- [ ] **Step C6: Verify**

Run: `make check`
Expected: PASS.

Browser preview: chip cycles Outdoor → Indoor → All sensors; marker set, list, and counts change accordingly; indoor markers are rounded squares; detail card shows the Indoor tag for an indoor sensor.

- [ ] **Step C7: Commit**

```bash
git add airloom tests
git commit -m "Add outdoor/indoor/both sensor filter with map chip and indoor marker styling"
```

---

### Task D: Integration (orchestrator — not a subagent task)

**Files:** merge branches; conflict resolution in `airloom/resources/app.js` (+`index.html`/`app.css` context lines).

- [ ] **Step D1:** Merge order: `feature/location-filter` → `feature/smooth-zoom` → `feature/marker-popup` into an integration branch off `main`.
- [ ] **Step D2:** Known reconciliations to apply by hand:
  - Popup close-on-zoom: replace Task B's `hidePopup()` call inside the deleted `zoom()` with a body for Task A's `hideTransientOverlays()` hook: `function hideTransientOverlays() { hidePopup(); }`. The pointerdown handler ends up calling both hooks once (dedupe to `hideTransientOverlays()`).
  - `updateMapTransform` no longer exists after Task A: move Task B's `#popup-layer` transform into `layoutMarkers`: `$("#popup-layer").style.transform = markers.style.transform;` and reposition the open popup there from `worldPoint(sensor.latitude, sensor.longitude, state.zoom)` if `state.popupId` is set (popup is closed during zoom, so this only matters for settle/fly frames).
  - Marker class string merges Task B's click behavior with Task C's `indoor` class.
- [ ] **Step D3:** Run `make check`; fix fallout.
- [ ] **Step D4:** Launch the real app (`./run`), verify all four spec behaviors end-to-end, including trackpad pinch on the laptop if available.
- [ ] **Step D5:** Bump nothing (no release in scope); merge integration branch to `main` via PR per repo convention.
