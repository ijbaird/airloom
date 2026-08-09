# Sensor Confidence Filter

**Date:** 2026-08-08
**Status:** Approved

## Goal

Hide sensors whose A/B channel agreement is poor. PurpleAir's `confidence`
field (0–100) is its channel-agreement score; sensors below 90 are filtered
out by default, with a settings switch to turn the filter off.

Decisions made during brainstorming:
- Use PurpleAir's `confidence` metric, not a hand-computed comparison of the
  raw `pm2.5_cf_1_a`/`pm2.5_cf_1_b` channels (one billed field instead of
  two, and no duplicated formula).
- Fixed threshold of 90; the setting is a boolean toggle only, default on.
- Version the SQLite cache schema and clear the cache when the version
  changes (rows cached before this feature have no `confidence` value and
  delta polls would never backfill unchanged sensors).

## Data flow

- `purpleair.py`: add `"confidence"` to `MAP_FIELDS` and `DATA_FIELDS`.
  `sensor_from_values` reads it with `_integer` into a new
  `Sensor.confidence: int | None`.
- `models.py`: `Sensor` gains `confidence: int | None = None`. `to_dict()`
  includes it automatically via `asdict`. Add module constants/helper:
  `CONFIDENCE_THRESHOLD = 90` and
  `passes_confidence(sensor, enabled: bool) -> bool` — returns True when the
  filter is off, when `confidence is None` (fail open: demo sensors, rows
  cached before the field existed, API omissions), or when
  `confidence >= CONFIDENCE_THRESHOLD`.
- `cache.py`: no per-field changes — the cache stores raw field dicts, so
  `confidence` flows through area fetches and delta polls unchanged.
- `demo.py`: untouched; demo sensors have no confidence and pass the filter.

## Cache schema versioning

- `cache.py`: new module constant `SCHEMA_VERSION = 1`, tracked with SQLite's
  `PRAGMA user_version` (currently 0 for all existing databases).
- In `_connect`, after the schema script runs: if `PRAGMA user_version` !=
  `SCHEMA_VERSION`, delete all rows from `sensors`, `regions`, and `trends`,
  then `PRAGMA user_version = SCHEMA_VERSION` and commit.
- Effect on upgrade: the first launch after this release starts with an empty
  cache and does one full area fetch, after which every cached row carries
  `confidence`. Future field additions bump the constant.

## Filtering (display time, Python side)

The filter is a display preference like hidden sensors — applied where
sensors leave the state of record, never at fetch/cache time, so toggling it
resends state without an API call:

- `app.py` `_send_sensor_state`: extend the `visible` comprehension to also
  require `passes_confidence(sensor, enabled)` where
  `enabled = bool(self.store.data.get("confidence_filter", True))`. Existing
  selection reconciliation handles a filtered-out selected sensor.
- `app.py` `_check_alerts`: skip sensors failing `passes_confidence` in the
  favorites scan, mirroring the hidden-sensor skip (no notifications from
  sensors the user can't see).
- The filter applies uniformly, including favorites.

## Settings

- `store.py`: `"confidence_filter": True` in `DEFAULT_CONFIG`; `_sanitize`
  accepts only `bool` values; `public_config()` exposes it.
- `app.py` `_save_settings`: `updates["confidence_filter"] =
  bool(message.get("confidence_filter"))`. The existing tail of
  `_save_settings` (save, resend config, `self.refresh()`) already applies
  the change; the refresh serves from cache inside the TTL, so no extra API
  cost.
- `index.html` settings dialog: a checkbox row (same pattern as
  `clear_api_key`) named `confidence_filter`, labeled
  "Hide low-confidence sensors" with a hint noting the threshold
  (below 90% A/B channel agreement).
- `app.js`: `openSettings` sets
  `form.elements.confidence_filter.checked = config.confidence_filter !== false`;
  the submit handler sends
  `confidence_filter: form.get("confidence_filter") === "on"`.
- Browser preview mode needs no filter emulation — preview sensors have no
  confidence values.

## Tests (all GTK-free, per CONTRIBUTING.md)

- `test_purpleair.py`: `confidence` present in `MAP_FIELDS`/`DATA_FIELDS`;
  `sensor_from_values` parses it (int, float coercion, missing → None).
- `tests/test_models.py` (new) or alongside existing model coverage:
  `passes_confidence` — off → always True; on with None → True; on with
  89 → False; on with 90/100 → True.
- `test_store.py`: default True; sanitize keeps False, rejects non-bool
  (strings, numbers) back to True; `public_config` exposes it.
- `test_cache.py`: fresh DB gets `user_version == SCHEMA_VERSION`; a DB with
  a stale version and populated tables comes up empty with the new version;
  a DB already at `SCHEMA_VERSION` keeps its rows across reconnect.

## Out of scope

- Showing the confidence value in the detail pane (the field is in the
  payload; UI can adopt it later).
- Adjustable threshold.
- Backfilling confidence into existing cache rows (versioned clear instead).
