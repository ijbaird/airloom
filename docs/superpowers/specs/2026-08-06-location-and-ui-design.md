# Airloom 0.2.0 — Location detection and map-first UI

Date: 2026-08-06 · Status: approved

## Problem

Airloom's "location" is a hardcoded Portland, Oregon default; there is no
geolocation or geocoding anywhere, so every user starts in the wrong city and
can only correct it by typing decimal coordinates. Separately, the UI shows
three permanent panes (~15 blocks) at once; the map — the natural center of a
hyperlocal air-quality app — gets squeezed into the middle column.

## Decisions (user-confirmed)

1. **Auto-detect location on every launch** via GeoClue, with a system
   permission prompt on first use. Manual override remains via a
   "Fixed home" mode in Preferences.
2. **Place search + reverse-geocoded labels** via OSM Nominatim (one search
   box handles sensors and places; detected coordinates get a name like
   "Tahoe City").
3. **Layout A — floating overlays**: full-bleed map; everything else floats
   over it and appears on demand.
4. **Auto-fetch sensors on map move** (debounced), not a manual
   "search this area" button.
5. Preferences: "Detect automatically" (default) / "Fixed home" (set via
   place search). Raw lat/lon fields removed.

## Technology choices

- **GeoClue** through the `Geoclue.Simple` PyGObject API (same stack as GNOME
  Weather; verified available on the dev machine). NEIGHBORHOOD accuracy,
  async, 10 s timeout. No IP-geolocation web services.
- **Nominatim client hand-written** in `airloom/geocode.py` (stdlib urllib,
  HTTPS, identifying `Airloom/<version>` User-Agent, throttled to 1 request/s
  per Nominatim policy). No geocode-glib dependency.
- **Map renderer rework** (hand-written, still no JS libraries): keyed
  persistent tile elements + CSS-transform panning; tiles re-diffed only when
  the viewport crosses a tile boundary; markers reuse the same transform.
  Replaces the rebuild-everything-per-pointermove renderer, which is
  untenable at full-window size and unfriendly to the OSM tile policy.

## UX specification

**Launch**: window opens immediately on the last-known home (never a blank
map) → GeoClue fix arrives → in auto mode the map glides to the detected
location, the label updates via reverse geocoding, and sensors fetch. In
fixed mode GeoClue is never called.

**The window is the map.** Floating overlays:

- **Search pill** (top-left): typing filters sensor list/markers live; on
  Enter or a short pause the dropdown adds a "Places" group from Nominatim;
  choosing a place flies the map there.
- **Area AQI chip** (top-right): median AQI of visible sensors, band-colored,
  with count in a tooltip/subtitle.
- **Sensors button** (bottom-left): opens a floating panel — Favorites
  section then Nearby, rows show badge/name/meta, stars toggle favorites.
  Dismissed by ✕, Esc, or clicking the map.
- **Detail card** (right, slides in on marker/row select): sensor name, hero
  AQI + category, trend chart, temperature/humidity/PM2.5/PM10, health
  guidance, favorite star, close button. Only one card at a time.
- **Map controls** (zoom in/out, recenter-to-home), **legend chip** that
  expands to the AQI color key on demand, **OSM attribution** (opens system
  browser).
- Narrow windows: detail card and sensors panel become full-height sheets;
  same DOM, CSS breakpoints only.

**Auto-fetch on move**: after the map settles (~1.2 s debounce), fetch
sensors for the visible bounds — skipped when the view is contained in the
last-fetched bounds and that data is younger than the auto-refresh interval.
Demo mode (no API key) regenerates demo sensors around the new center.

**Alerts follow favorites everywhere**: the 5-minute background refresh
always covers home bounds plus all favorited sensor IDs (PurpleAir
`show_only` parameter), so threshold notifications work regardless of where
the map was left. Alert evaluation itself is unchanged from 0.1.1.

**Preferences dialog**: PurpleAir key management (unchanged); Home mode
radio — Detect automatically / Fixed home with a place-search field (results
inline, choosing one sets home + label); radius; alert threshold; temperature
unit. Privacy note lists all four external services: PurpleAir (bounds +
key), OSM tiles (viewport), Nominatim (search text / home coordinates),
GeoClue (local system service).

## Architecture

New modules (both GUI-free where possible):

- `airloom/location.py` — `GeoClueLocator.start(on_fix)` wrapping
  `Geoclue.Simple.new` (async). Calls `on_fix(lat, lon)` once per launch on
  success; `on_fix(None)` on denial/timeout/missing GIR. All GTK-thread-safe
  (GeoClue is GLib-async already).
- `airloom/geocode.py` — `search(query, limit=5) -> list[Place]`,
  `reverse(lat, lon) -> str`, `Place` dataclass
  (`name, latitude, longitude`), `GeocodeError`; module-level throttle.
  Called from daemon threads; results marshalled with `GLib.idle_add`.

Changed:

- `airloom/purpleair.py` — `fetch_sensors(bounds, show_only=None)`;
  pure helper `bounds_contains(outer, inner)`.
- `airloom/store.py` — sanitizer gains `home_mode` ("auto"|"fixed",
  default "auto"). `latitude/longitude/location_name` now mean "home",
  auto-updated by detection when in auto mode.
- `airloom/app.py` — starts `GeoClueLocator` after window present; on fix
  (auto mode): update store, send `location`, reverse-geocode label in a
  thread, trigger refresh. Orchestrates view fetches with the
  containment/freshness skip. 5-minute timer refreshes home ∪ favorites.
- `airloom/resources/` — `index.html` restructured to map + overlays;
  `app.css` rewritten for the overlay system (light/dark preserved);
  `app.js` keeps the state-object + render-function pattern, adds keyed tile
  layer, transform panning, view-change debounce, places dropdown, detail
  card/panel show-hide. Browser-preview fallback retained.

Bridge protocol additions (existing events unchanged):

- JS → Python: `view-changed {north, west, south, east, lat, lon, zoom}`
  (debounced), `place-search {query}`; `save-settings` gains
  `home_mode`/`home_lat`/`home_lon` fields and loses raw lat/lon.
- Python → JS: `location {latitude, longitude, name, source}`
  (source: "geoclue" | "fixed" | "fallback"), `places {query, results}`.

## Error handling

- GeoClue unavailable, denied, or no fix within 10 s → keep last-known home,
  one toast "Using last known location", source="fallback". Never blocks UI.
- Nominatim failure → search dropdown shows "Place lookup unavailable";
  reverse-geocode failure → coordinate label ("39.17, −120.14"). Throttle
  queues at most one pending search; stale responses (query changed) are
  dropped.
- All network work uses the 0.1.1 worker pattern: catch-all → error toast →
  state always unwound; refresh coalescing (`refresh_pending`) reused for
  view fetches.

## Testing

- Unit (stdlib, no GTK — project rule): geocode URL construction/parsing/
  throttle/error mapping with mocked `urlopen`; `show_only` query building;
  `bounds_contains` and fetch-skip decisions; store sanitizer `home_mode`
  cases; existing suites stay green.
- Manual checklist (in the implementation plan): first-run permission prompt,
  launch glide, search-fly, pan auto-fetch + skip behavior, favorite alert
  from a non-home area, prefs fixed-home flow, demo mode, narrow-window
  sheets, dark mode.

## Packaging / release

- Flatpak manifest: add `--system-talk-name=org.freedesktop.GeoClue2`.
- Version 0.2.0; CHANGELOG + metainfo release entry; released via the
  established manual-dispatch flow.

## Out of scope

- Continuous location tracking (one fix per launch only).
- Marker clustering, fractional/pointer-anchored zoom (renderer keeps
  integer zoom steps centered on the view).
- Tile-consent dialog or offline tile cache.
- Accessibility audit beyond keeping existing labels/roles intact.
