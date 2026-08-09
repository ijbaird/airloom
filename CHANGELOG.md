# Changelog

All notable changes to Airloom are documented here. The project follows semantic versioning.

## 0.10.0 — 2026-08-08

- Sensors can now be hidden — useful for erroneous units reporting bogus readings. A new eye-off button in the sensor detail pane and the map popup removes the sensor from the map, lists, heat map, counts, and alerts. Preferences gains a "Hidden sensors" section listing hidden sensors by name with per-sensor Unhide and an "Unhide all" action; unhiding restores a sensor instantly, and a hidden favorite keeps its star for when it returns.

## 0.9.0 — 2026-08-08

- Airloom now describes itself as GNOME-native rather than Fedora-native across the AppStream metadata, About dialog, and README. Nothing in the app is Fedora-specific: the Flatpak runs on any Linux distribution with the GNOME runtime. Fedora remains the development and from-source reference platform.

## 0.4.2 — 2026-08-08

- No application changes. Confirms the automatic-update pipeline introduced in 0.4.1 end to end: this release was delivered to an installed copy via `flatpak update` from the signed repository.

## 0.4.1 — 2026-08-08

- Installed copies now update automatically: releases are published to a signed Flatpak repository on GitHub Pages, and bundles are preconfigured with it as their update source. Existing bundle installs pick this up by reinstalling once from a current bundle (see README).

## 0.4.0 — 2026-08-08

- Zooming out past a configurable view width (default 40 km, new "Heat map beyond (km)" preference) now merges the sensor dots into a translucent heat map of interpolated AQI in the legend's colors; zooming back in restores the individual markers.

## 0.3.4 — 2026-08-07

- The close button on the sensor detail card (and the sensors panel and preferences dialog) is easier to see: the × glyph was rendering at body-text size inside its 34px hit area and now matches the neighboring icons.

## 0.3.3 — 2026-08-07

- The developer debug facility (introduced in 0.3.2) is now excluded from release builds entirely: the module is stripped from Flatpak bundles, and debug mode refuses to activate inside an installed Flatpak even if requested. Development checkouts are unaffected.
- Developer tooling: richer debug commands (version/build identity, tap, search, key, state snapshot, PNG screenshots over the wire, quit), `scripts/debug-run`/`scripts/debug-client`, and unmistakable debug-instance chrome (red header bar, "Airloom · DEBUG" title, non-unique instance).

## 0.3.2 — 2026-08-07

- Fixed trackpad pinch really this time: WebKitGTK consumes touchpad pinch internally as page-scale zoom before the page ever sees it, so 0.3.0's in-page handlers never ran. The gesture is now intercepted at the GTK layer (capture-phase claim) and forwarded to the map, which zooms smoothly around the pinch point; the interface chrome stays put.
- New developer feature: an opt-in debug socket (`AIRLOOM_DEBUG_SOCKET`) lets tooling drive and inspect a running instance (ping, JS eval, synthetic pinch) for autonomous debugging.

## 0.3.1 — 2026-08-07

- Fixed sensor markers being unclickable: releasing the pointer rebuilt the marker layer before the click could be delivered, so marker clicks were silently dropped. Latent since the 0.2.0 map redesign, it also blocked the new 0.3.0 marker popup. Clicking a marker now opens the popup as intended.

## 0.3.0 — 2026-08-07

Smooth zoom, sensor popups, and indoor/outdoor filtering.

- Map zoom is now animated and anchored: wheel and button zoom ease smoothly toward the point under the cursor, rapid zooming retargets one fluid animation, and level changes cross-fade tiles instead of flashing a blank map.
- Trackpad pinch zoom works correctly: the gesture now zooms the map around the pinch point instead of triggering the engine's page zoom, which used to scale the whole interface and clip the controls off-screen.
- Clicking a sensor marker opens a compact popup with the AQI, category, indoor/outdoor tag, update time, and a favorite star — with buttons to open the full detail card or view the sensor on the PurpleAir web map in your browser.
- New sensor filter chip on the map cycles between Outdoor, Indoor, and All sensors; the choice is persisted, applied to PurpleAir queries, and reflected in demo mode. Indoor sensors render as rounded squares and are tagged in the popup and detail card.
- Favorited sensors are always fetched and shown regardless of the indoor/outdoor filter.
- Demo readings that stand in after a failed live fetch are now always labeled as demo data, even when part of the fetch had succeeded.

## 0.2.4 — 2026-08-07

- Fixed sensors not appearing after navigating with the place search: the search text no longer doubles as a sensor-name filter once a place result is chosen, so sensors fetched at the destination actually show on the map and in the list.
- The area AQI chip now names the area you are looking at: panning or searching far enough to fetch new sensors reverse-geocodes the view center, and recentering home restores the home name. The window title continues to show home.

## 0.2.3 — 2026-08-07

- Fixed startup location detection on GNOME: the GeoClue request now waits until the app window has focus, so the system location-permission dialog can actually appear instead of crashing and reading as a denial (fresh installs were stuck on the shipped default location).
- A location fix that arrives after the detection timeout (for example while the permission dialog is open) is now applied instead of being dropped for the session, and the timeout was raised from 10 to 45 seconds so the permission dialog can be answered calmly.
- When detection fails on a fresh install, the app now says location couldn't be detected and points to Preferences, instead of calling the shipped default the "last known location".

## 0.2.2 — 2026-08-06

- Reverse geocoding now resolves town-level names, so small towns (like Tahoe City) are labeled by name instead of their county.

## 0.2.1 — 2026-08-06

Trustworthy location detection.

- Location detection now respects GeoClue's reported accuracy: a coarse, IP-level fix (which is often the ISP's city rather than yours) no longer overwrites a known location — the app keeps your saved home and explains that detection was only approximate. Precise WiFi/GNSS fixes behave as before, and first-run detection still accepts any fix.
- The 5-minute background refresh now tracks the viewed area and its center as one unit, so a failed or empty fetch can no longer cause later refreshes to target a stale area.
- PurpleAir favorite queries tolerate malformed sensor ids instead of failing the whole fetch.
- Small polish: the fixed-home search no longer shows an empty dropdown, and map marker lookups are scoped to the marker layer.

## 0.2.0 — 2026-08-06

Major UI redesign and automatic location detection.

- Automatic location detection via GeoClue on every launch, with a Fixed-home option in Preferences to disable queries and use a saved location.
- Place search and reverse-geocoded location labels via OpenStreetMap Nominatim; location appears in the header and search uses place-name prefix matching.
- Redesigned map-first interface: full-window map with floating search card, area AQI chip, collapsible sensors panel, detail card overlay, and toggle-able legend.
- Reworked tile renderer: persistent tile cache with transform-based panning eliminates redundant requests and smooths interaction across pan, zoom, and window resize.
- Sensors auto-load for the current map viewport on pan and zoom; demo mode generates new labeled sensors for panned areas without an API key.
- Background refresh now covers home sensors plus any favorited sensors wherever they are located, firing every 5 minutes with independent timers for AQI alerts.

## 0.1.1 — 2026-08-06

Hardening and bug-fix release.

- Fixed a failure mode where an unexpected error during a refresh left the app loading forever with a dead Refresh button; malformed PurpleAir responses now fall back gracefully.
- External links (such as the OpenStreetMap attribution) now open in the system browser instead of replacing the app UI, and remote pages can never reach the app's internal bridge.
- The config file (which can hold the PurpleAir API key) is now created with private permissions from the start, in a private directory, and corrupt or wrong-typed config values no longer crash startup.
- Replaced the EPA wildfire-smoke correction with the published piecewise form: the quadratic branch for heavy smoke (above 343 µg/m³) is now applied, and readings without a humidity value use a neutral default instead of raw over-reporting data.
- Sensor data refreshes automatically every 5 minutes, so AQI threshold notifications fire without manual refreshing; a settings change made mid-refresh is no longer dropped.
- Preferences dialog: pressing Enter now saves instead of silently discarding edits, and reopening the dialog no longer throws.
- Map: panning is clamped to the Web Mercator range, markers survive crossing the date line, and interrupted drags no longer leave the map stuck in drag mode.
- Identifying User-Agent sent to OpenStreetMap and PurpleAir; notification titles cap hostile sensor names; various stale-UI states reset correctly.
- Build/CI: GitHub Actions pinned to commit SHAs; local Flatpak builds run from a pristine export of committed HEAD so stray local files can never enter the bundle.

- Initial Fedora and GNOME release.
- Interactive OpenStreetMap sensor map and responsive three-pane interface.
- PurpleAir live data with private local API-key storage and a deterministic demo mode.
- EPA 2024 AQI calculations, trends, favorites, health guidance, and threshold notifications.
- Reproducible Flatpak manifest, x86_64/aarch64 CI artifacts, and tag-driven GitHub releases.

