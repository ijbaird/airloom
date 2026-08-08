# Heat map mode for zoomed-out views — design

Date: 2026-08-07
Status: approved

## Summary

When the user zooms out far enough that individual sensor markers stop being
useful, the map switches to a heat map: a translucent overlay showing air
quality as an interpolated color field. Zooming back in restores the
individual marker circles. The switch point is a user-configurable distance:
when the visible map width exceeds N kilometers, the heat map is shown.

## Threshold

- New persisted setting `heatmap_threshold_km` (float), default **40**,
  sanitized range **5–1000**, stored in `~/.config/airloom/config.json`
  alongside the existing settings and exposed through `public_config()`.
- Settings dialog gains a "Heat map beyond (km)" number input next to
  "Map radius (km)", plumbed through the existing `save-settings` bridge
  action and `app.py` save handling.
- The JS computes the visible map width in km each render:
  `widthKm = panelWidthPx / (256 * 2^zoom) * 40075.016686 * cos(centerLat)`.
  Heat map mode is active when `widthKm > heatmap_threshold_km`.
- No hysteresis: the comparison is deterministic and cheap; crossing the
  boundary mid-pinch just swaps layers on the next frame.

### Choosing the 40 km default

Three independent estimates converge on ~40 km:

1. **Marker collision.** Markers are ~28 px circles that overlap below ~32 px
   spacing. Dense urban PurpleAir coverage runs about one sensor per 1–2 km;
   on a typical ~1200 px panel, 1 km ≈ 34 px when the viewport is 35 km wide.
   So city-core dots start colliding at roughly a 35–40 km view width.
2. **Zoom mapping.** At 1200 px and mid-latitudes (~39°N), viewport width is
   ≈ 18 km at zoom 13, 36 km at zoom 12, 71 km at zoom 11. A 40 km threshold
   flips between zoom 12 and 11: markers persist at neighborhood scale, the
   field view takes over at metro scale and beyond.
3. **Data extent.** The default fetch radius is 22 km, so the sensor field
   spans ~44 km. Once the viewport is wider than the data, dots are just a
   clump in the middle; a ~40 km threshold switches modes right there.

## Rendering (approach A: interpolated AQI field)

- A `<canvas id="heatmap">` element sits in the map panel between the tile
  layers and the marker layer, sized to the panel (ResizeObserver already
  triggers `renderMap()`).
- The field is computed on a coarse grid (cell ≈ 8 CSS px) into an offscreen
  `ImageData`, then drawn scaled up to the canvas with a slight CSS blur for
  smoothness.
- Per cell: inverse-distance-weighted (IDW, weight = 1/d², d in screen px)
  average of the AQI of sensors whose screen distance is within an influence
  radius R (≈ 90 px). Cells with no sensor in range stay fully transparent.
- Cell color: the interpolated AQI value mapped through the same palette the
  legend shows (Good green / Moderate yellow / Sensitive orange / Unhealthy
  red). A small JS `aqiColor(aqi)` mapping is added mirroring the Python
  palette in `aqi.py`; colors between category boundaries are the bucket
  color, so every on-screen color corresponds to a legend entry.
- Alpha ramps with proximity weight (denser/closer coverage is more opaque,
  capped at ~0.55 so tiles remain readable underneath).
- Input sensors: `visibleSensors()` with finite AQI — the location filter and
  search query apply to the heat map exactly as they do to markers.
- Redraw happens in `renderFrame()` (same cadence as tile layout), so pan,
  pinch, and animated zoom stay in sync.

## Mode switching and interaction

- While the heat map is active, `renderMapMarkers()` renders no markers and
  any open marker popup is closed; the heat map is passive (clicks/taps on it
  do nothing). Map drag and zoom gestures work unchanged.
- Search, the sensors panel, favorites, the summary chip, and the detail card
  keep working in both modes.
- The browser-preview fallback (bottom of `app.js`) uses the same logic with
  the default threshold so the heat map can be exercised in a plain browser.

## Data flow / architecture notes

- All new UI logic lives in `app.js`; Python only stores/roundtrips the new
  config key. No new bridge events — `config`/`sensors` already deliver
  everything needed.
- `store.py`: add `heatmap_threshold_km` to `DEFAULT_CONFIG`, `_sanitize()`
  (numeric clamp 5–1000), and `public_config()`.
- `app.py`: include the field in the `save-settings` handler.
- `debugState()` gains `heatmapActive: bool` and `viewportKm: number` so the
  debug port can observe the mode.

## Error handling

- Missing/invalid stored values fall back to the default via `_sanitize()`.
- Sensors without a finite AQI are excluded from the field (matches the
  summary chip's filtering).
- Zero-size panel (startup) skips rendering, as the existing renderers do.

## Testing

- **Unit (in `make test`)**: `tests/test_store.py` additions covering
  `heatmap_threshold_km` defaulting, clamping, and presence in
  `public_config()`.
- **End-to-end via debug port**: new `scripts/test-heatmap` script that
  1. launches the app with the debug socket (`scripts/debug-run` semantics),
  2. waits for `ping`,
  3. reads `state` at a zoomed-in level → asserts `heatmapActive` is false
     and markers are rendered (via `eval` DOM query),
  4. `eval`s a zoom-out past the threshold → asserts `heatmapActive` is true,
     zero `.map-marker` elements, and a non-blank heat map canvas,
  5. zooms back in → asserts markers return and the canvas clears,
  6. optionally captures screenshots for visual confirmation.
  The script exits non-zero on any failed assertion; it is iterated against
  the real app until it passes. It is not part of `make test` (needs GTK and
  a display), mirroring how the debug port itself is dev-only.
