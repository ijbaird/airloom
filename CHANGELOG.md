# Changelog

All notable changes to Airloom are documented here. The project follows semantic versioning.

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

