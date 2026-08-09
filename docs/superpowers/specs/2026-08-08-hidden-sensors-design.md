# Hidden Sensors — Design

**Date:** 2026-08-08
**Status:** Approved (brainstorming session with Ian)

## Purpose

Let the user hide individual sensors (e.g. erroneous ones reporting bogus readings) so they stop appearing anywhere in the app, with a Settings section to review and unhide them.

## Decisions (from brainstorming)

1. **Hide scope:** hide everywhere — map markers, nearby list, favorites list, heat-map aggregation, counts, and desktop alerts.
2. **Hide affordance:** a hide button (eye-off icon) next to the existing favorite/star button in **both** the sensor detail pane and the map marker popup.
3. **Favorites interaction:** hiding a favorited sensor keeps the favorite flag stored; hide wins at display time. Unhiding restores the sensor with its star intact.
4. **Unhide UI:** Settings dialog gets a "Hidden sensors" section — one row per hidden sensor (last-known name + Unhide button) plus an "Unhide all (N)" action and an empty state when nothing is hidden.

## Approach

Python-side filtering (state of record stays in Python, per architecture). `self.sensors` in `app.py` continues to hold **everything fetched**, including hidden sensors; hidden sensors are filtered out at send time in `_send_sensor_state`, so the web UI never renders them and every surface (map, lists, heat-map, counts) is automatically correct. Unhide therefore restores a sensor instantly from the retained list without a network refetch.

Rejected alternative: sending a `hidden` flag and filtering in JS — would require touching every render surface plus a second filter in Python for alerts (two authorities, easy to miss a spot).

## Components

### store.py

- New `DEFAULT_CONFIG` key: `"hidden": {}` — dict of `str(sensor_id) → last-known name` (string). Name capped at 80 chars, same convention as `location_name`.
- `_sanitize`: accept only dict entries whose key is a digit-string (or int coercible) and whose value is a string; coerce keys to `str(int(key))`, truncate names to 80 chars.
- New methods, each persisting via `save()`:
  - `hide(sensor_id: int, name: str) -> None` — record `str(sensor_id) → name`.
  - `unhide(sensor_id: int) -> None` — remove if present.
  - `unhide_all() -> None` — clear the dict.
  - `is_hidden(sensor_id: int) -> bool` — convenience lookup.
- `public_config()` gains `"hidden": [{"id": int, "name": str}, ...]` sorted by name (case-insensitive), so the settings dialog can render the list. The favorites list is untouched by hide/unhide.

### app.py

- `_hidden_ids() -> set[int]` helper reading the store.
- `_send_sensor_state`: `items` excludes hidden sensors. If `selected_id` is hidden (or absent), reselect the first *visible* sensor or `None` — same convention as `_finish_refresh`.
- `_check_alerts`: skip sensors whose id is hidden (hidden favorites must not notify). Alert `states` cleanup keys off favorites and is unchanged.
- New bridge actions in `_on_script_message`:
  - `hide {id}` — toggle: if hidden, unhide; else look up the sensor in `self.sensors` (guard like `favorite` does) and `store.hide(id, sensor.name)`. Then `_send_sensor_state()` (which also carries the refreshed `public_config`). No refetch.
  - `unhide {id}` — `store.unhide(id)` + `_send_sensor_state()`.
  - `unhide-all` — `store.unhide_all()` + `_send_sensor_state()`.
- Favorites fetch (`include_favorites`) still includes hidden favorites; they're fetched but filtered at send time — harmless and keeps unhide instant.

### Web UI (app.js / index.html / app.css)

- **Detail pane:** hide button (eye-off SVG icon) beside `#favorite-button`; click → `bridge({action: "hide", id: state.selectedId})`.
- **Map popup:** same beside `#popup-favorite`; click → `bridge({action: "hide", id: state.popupId})`; in browser-preview fallback, set `sensor.hidden` locally and re-render.
- After hiding, the next `sensors` payload no longer contains the sensor; verify existing selection/popup handling clears stale detail pane and popup (patch if it lingers).
- **Settings dialog:** "Hidden sensors" section rendered from `config.hidden`: a row per sensor (name + "Unhide" button → `bridge({action: "unhide", id})`), an "Unhide all (N)" button → `bridge({action: "unhide-all"})`, and an empty-state line ("No hidden sensors.") when the list is empty. Actions apply immediately (they are not part of the settings form submit; the dialog stays open).
- **Browser-preview fallback:** a couple of demo sensors start hidden; the fallback filters `s.hidden` from the visible set and settings renders/unhides from local state, so UI iteration works without the bridge.

## Data flow

1. User clicks hide on a visible sensor → JS posts `{action: "hide", id}`.
2. Python records id + current name in config (atomic 0600 save), then `_send_sensor_state()`.
3. JS re-renders from the new payload: sensor gone from map/lists/heat-map; `config.hidden` now lists it in Settings.
4. Unhide (Settings row or Unhide all) reverses step 2–3; sensor reappears instantly from the retained fetch, star intact if it was favorited.

## Error handling

- Invalid/unknown ids in `hide`/`unhide` messages: ignored with the existing `_message_sensor_id` stderr pattern; `unhide` on a non-hidden id is a no-op.
- Malformed `hidden` config entries are dropped by `_sanitize` (fall back to `{}` on garbage), consistent with existing keys.
- Hiding the selected sensor never leaves a dangling `selected_id`.

## Testing

- `tests/test_store.py` (GUI-free, stdlib unittest, per CONTRIBUTING.md rule that data-behavior changes need tests):
  - `hidden` sanitize round-trip: valid entries survive, garbage keys/values dropped, names truncated to 80 chars.
  - `hide`/`unhide`/`unhide_all` persist and round-trip through a reload.
  - `public_config()["hidden"]` shape and name-sorted order.
  - Hiding a favorited id leaves `favorites` untouched.
- Manual/E2E over the debug port (`scripts/debug-client`): hide a sensor → gone from map and lists; Settings lists it; unhide → back instantly with star intact; `state` snapshot confirms counts.
- `make check` (tests + compileall + `node --check app.js`) must pass before the PR.
