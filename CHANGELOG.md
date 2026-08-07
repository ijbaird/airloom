# Changelog

All notable changes to Airloom are documented here. The project follows semantic versioning.

## Unreleased

- Fixed startup location detection on GNOME: the GeoClue request now waits until the app window has focus, so the system location-permission dialog can actually appear instead of crashing and reading as a denial (fresh installs were stuck on the shipped default location).
- A location fix that arrives after the 10-second timeout (for example while the permission dialog is open) is now applied instead of being dropped for the session.
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

