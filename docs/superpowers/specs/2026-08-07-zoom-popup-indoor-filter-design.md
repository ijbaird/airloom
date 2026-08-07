# Smooth zoom, marker popups, and indoor/outdoor filtering

**Date:** 2026-08-07
**Status:** Approved

Three user-facing problems, one release:

1. Zoom is chunky — discrete integer jumps, tile layer wiped on every step, no
   animation, wheel not anchored at the cursor.
2. Trackpad pinch is broken — WebKitGTK applies *page* zoom, scaling the whole
   document so fixed overlays clip off-screen; the gesture never reaches the map.
3. Clicking a map marker jumps straight to the full detail card; there is no
   lightweight Paku-style popup, no way to open the sensor on the PurpleAir web
   map, and no way to filter indoor vs outdoor sensors (outdoor-only is
   hard-coded via `location_type=0` in `purpleair.py`).

Decisions made during brainstorming with the user:

- Indoor/outdoor filter lives in a **map chip** next to the ☰ Sensors / AQI
  buttons, cycling Outdoor → Indoor → Both. Persisted.
- Marker click is **popup-first**: compact popup at the marker with a
  "Details" button that opens the existing detail card. List rows still open
  the card directly.

## 1. Fractional zoom core (`app.js`)

`state.zoom` becomes a float during gestures/animations; tile fetching stays
integer.

- **Rendering:** `tileZoom = clamp(Math.round(state.zoom), 3, 17)`. Tiles are
  positioned in `tileZoom` world coordinates; the tile layer gets
  `translate(...) scale(2^(state.zoom - tileZoom))` so fractional zooms render
  as a scaled layer. Markers are positioned with `worldPoint(lat, lon,
  state.zoom)` directly (the math already accepts fractional zoom).
- **No blank flash:** when `tileZoom` changes, the previous level's tiles move
  to a "retiring" layer kept behind the new one, still transformed/scaled
  correctly; it is removed once the new level's visible tiles have loaded
  (`img.onload` counting, with a ~1.5 s timeout fallback). New tiles fade in
  over the old scaled ones (CSS opacity transition).
- **Anchored zoom:** all zoom paths take an anchor point in viewport pixels
  (cursor, pinch centroid, or viewport center). Each frame, `state.center` is
  recomputed so the geo point under the anchor stays under the anchor.
- **Discrete animated zoom (wheel ticks, +/− buttons):** animate `state.zoom`
  to the target integer over ~220 ms ease-out via rAF. A new tick while
  animating retargets the same animation (accumulates to ±2, ±3 smoothly).
  Wheel ticks anchor at the cursor; buttons anchor at viewport center.
- **Clamp** stays 3–17. `sendViewChanged` continues to fire (debounced) after
  zoom settles; settled zoom is always an integer.

## 2. Trackpad pinch (`app.js` + guard in `app.py`)

- Document-level handlers for both pinch encodings WebKitGTK can emit:
  `wheel` with `ctrlKey` set, and proprietary
  `gesturestart`/`gesturechange`/`gestureend`. Both call `preventDefault()`
  **unconditionally** (this alone fixes the clipped-overlay page zoom), then
  drive the map when the gesture is over it.
- `gesturechange`: `state.zoom = gestureStartZoom + log2(event.scale)`,
  anchored at the gesture centroid, applied live (fractional). `gestureend`:
  settle-animate to the nearest integer zoom.
- `ctrl+wheel`: continuous fractional zoom `zoom -= deltaY * 0.01` anchored at
  the cursor, with the same settle on idle (~180 ms after last event).
- Python guard: connect to the webview's `notify::zoom-level` and reset any
  non-1.0 value back to 1.0 — page zoom becomes structurally impossible.

## 3. Marker popup (`app.js`, `index.html`, `app.css`, `app.py`)

- New `#map-popup` element inside a map-space layer sharing the tile/marker
  transform, so it tracks its sensor while the map moves. Anchored above the
  marker with a pointer notch.
- Contents: sensor name, AQI badge (band color), category, updated-time,
  Indoor/Outdoor tag, favorite star toggle, and two actions:
  - **Details** — opens the existing detail card (`#detail-card`).
  - **Open in PurpleAir ↗** — an `https://map.purpleair.com/1/mAQI/a10/p604800/cC0?select=<id>#14/<lat>/<lon>`
    link. The existing `decide-policy` handler already routes it to the system
    browser. Hidden when running on demo data (demo sensor IDs don't exist on
    the real map).
- Behavior: marker click selects the sensor and shows the popup (detail card
  no longer auto-opens from markers). Popup closes on map click, Escape,
  drag start, zoom start, or when another marker opens it. List-row clicks
  keep today's behavior (select + open card).

## 4. Indoor/outdoor filter (all layers)

- **Setting:** `location_filter` ∈ `"outdoor"` (default) | `"indoor"` |
  `"both"`. Sanitized in `store.py`, included in `public_config()`.
- **Model:** `Sensor.indoor: bool = False`; `to_dict()` exposes it.
- **API client (`purpleair.py`):** add `location_type` to `FIELDS`; parse it
  into `indoor`. `fetch_sensors()` gains a `location_filter` parameter mapping
  outdoor→`location_type=0`, indoor→`1`, both→param omitted. The `show_only`
  (favorites) path never sends the filter — explicitly requested sensors
  always come back.
- **App (`app.py`):** pass the setting into fetches; filter demo sensors by
  the same rule. New bridge action `set-location-filter` (validated) saves the
  setting, re-sends config, and refetches the current view (falls back to home
  refresh). The "No public outdoor sensors…" error message adapts its wording
  to the active filter.
- **Demo (`demo.py`):** a deterministic subset (e.g. every 5th sensor) is
  marked indoor so the filter is testable without an API key. Browser preview
  data in `app.js` likewise.
- **UI (`app.js`/`index.html`/`app.css`):** `#filter-chip` in
  `.corner-buttons` shows the current mode ("Outdoor" / "Indoor" / "All
  sensors") and cycles on click via the bridge. Indoor sensors render as
  rounded-square markers (outdoor stay circles) and show an "Indoor" tag in
  popup and detail card.

## Error handling

- Unknown/invalid `set-location-filter` values are ignored (logged), state
  unchanged — same pattern as other bridge actions.
- Pinch/zoom code must not throw when the map panel has zero size (same
  guards as `renderMap`).
- Retiring-tile-layer cleanup must be timeout-backed so a never-loading tile
  can't strand a stale layer.

## Testing

- `tests/test_purpleair.py`: `location_filter` → request param mapping
  (outdoor/indoor/both, and absence on `show_only`); `location_type` parsed to
  `indoor`.
- `tests/test_store.py`: `location_filter` default, valid values, junk
  rejected.
- New `tests/test_demo.py`: demo set contains both indoor and outdoor sensors,
  deterministically.
- GUI behavior (zoom feel, pinch, popup) verified by launching the app;
  browser preview (`index.html` in a browser) covers popup/filter UI without
  GTK. `make check` gates the merge.

## Out of scope

- Continuous fractional *resting* zoom (map always settles on integers).
- Popup for list rows, clustering, or any map-library adoption.
- Per-favorite filter overrides.
