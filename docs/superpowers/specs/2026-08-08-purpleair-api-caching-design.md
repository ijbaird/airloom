# PurpleAir API Caching — Design

**Date:** 2026-08-08
**Status:** Approved
**Motivation:** Two days of dev work burned ~1.4M PurpleAir API points. Per PurpleAir's
pricing (`total_cost = base_cost + sum_of_field_costs × rows`), Airloom's 17-field area
fetches cost ~25–30 points per sensor row, and every relaunch, pan, and 5-minute
auto-refresh repaid full price. PurpleAir's own
[API use guidelines](https://community.purpleair.com/t/api-use-guidelines/1589)
recommend `modified_since` delta polling, minimal field selection, and polling no faster
than the 2-minute sensor report cadence.

## Goals

1. Relaunch-heavy dev sessions cost near zero points (persistent cache).
2. Steady-state use pays only for sensors that changed, at ~16 pts/row, on the user's
   chosen interval.
3. The header refresh button always yields fully current data (bypasses TTL).
4. A settings dropdown controls the interval; no other user-facing complexity.
5. Zero third-party dependencies (stdlib `sqlite3`).

## Non-goals

- Historical data, offline mode beyond stale-cache display, multi-process cache sharing.

## Architecture

New GTK-free module `airloom/cache.py` with a `SensorCache` class backed by SQLite at
`~/.cache/airloom/cache.db` (XDG cache dir — regenerable data; directory mode 0700).
`PurpleAirClient` stays a pure API client; the fetch worker in `app.py` consults the
cache around it. The cache stores **post-parse sensor dicts** (what the UI receives),
never raw API responses, and never demo data.

### Schema

```sql
sensors(
  sensor_index INTEGER PRIMARY KEY,
  data         TEXT NOT NULL,   -- JSON: parsed Sensor map fields (no trend)
  fetched_at   REAL NOT NULL
);
regions(
  id INTEGER PRIMARY KEY,
  north REAL, west REAL, south REAL, east REAL,
  location_filter TEXT NOT NULL, -- outdoor / indoor / all
  fetched_at REAL NOT NULL,
  api_time_stamp INTEGER         -- server time_stamp from the response
);
trends(
  sensor_index INTEGER PRIMARY KEY,
  data       TEXT NOT NULL,      -- JSON trend array for the detail chart
  fetched_at REAL NOT NULL
);
```

### Operations

- `covering_region(bounds, location_filter, ttl)` → freshest region containing the
  bounds with the same filter, else `None`.
- `sensors_in(bounds)` → cached sensors within bounds.
- `store_fetch(bounds, location_filter, sensors, api_time_stamp)` → upsert sensor rows,
  insert region, prune (see below).
- `get_trend(sensor_index, ttl)` / `store_trend(sensor_index, trend)`.
- `clear()` — for tests and corruption recovery.

## Fetch decision flow

All fetch paths (`refresh()`, `_on_view_changed`, auto-refresh, force refresh) funnel
through one decision in the worker thread:

1. **Effective bounds.** If the requested view is wider than ~200 km, shrink to a
   200 km box centred on the view centre (zoom cap). Prevents one scroll-wheel flick
   from fetching a whole metro area.
2. **Fresh hit** — covering region younger than TTL, same filter → serve
   `sensors_in(bounds)`; zero API calls.
3. **Stale hit** — covering region exists but older than TTL → **delta poll**: same
   bounded query plus `modified_since=<region.api_time_stamp>`. Merge returned rows over
   cached rows; unchanged sensors keep cached values; region gets new `fetched_at` and
   `api_time_stamp`.
4. **Miss** — no covering region → full fetch with the trimmed map-field list
   (17 fields minus the six pm2.5 average fields ⇒ ~16 pts/row), stored as a new region.

**Force refresh** (header button): skip the TTL check, always delta poll. `modified_since`
results are fully current by definition (unchanged rows are identical to cache). Does
not wipe the cache.

**Startup:** if the cache holds anything for home bounds, paint it immediately (even
stale), then run the normal decision flow in the background. The map is never blank
while waiting on the network.

**Trends (lazy):** the six pm2.5 average fields leave the area fetch. On sensor select:
cached trend fresher than TTL → serve; else one `show_only=<id>` fetch of the six
averages + `pm2.5_cf_1` + `humidity` (humidity feeds the EPA correction of each
average) ≈ 17 points. The chart shows a brief loading state on a miss.

**Favorites:** favorites already fresh in the sensor table are not refetched; only
missing/stale ones enter the `show_only` call.

**Demo data** is never cached; a key-less session cannot poison the cache.

## Settings

New `refresh_minutes` config key in `store.py`: default **5**, allowed {2, 5, 10, 30},
sanitized like other numeric keys, exposed via `public_config()`. The settings dialog
gains a "Refresh interval" dropdown; `save-settings` persists it and re-arms the
auto-refresh timer. The same value is the cache TTL — one knob, both behaviors.

## Error handling

- **API failure with cached data:** show the stale cache plus the error banner
  ("Couldn't reach PurpleAir — showing readings from N min ago"). Demo sensors are the
  fallback only when the cache has nothing for the area. (Deliberate behavior change:
  stale real data beats fake data.)
- **SQLite corruption** (any `sqlite3` error on open): delete the DB file, recreate,
  treat as a miss.
- **Delta poll returns zero rows:** success — bump region timestamps.
- **Pruning on each store:** drop regions fully contained in newer same-filter regions;
  keep at most the newest ~50 regions; delete sensor and trend rows not refreshed in
  24 h.
- **Clock skew:** `modified_since` always uses the server's echoed `time_stamp`, never
  the client clock.

## Visible state

The summary chip's source label gains a cached variant — "PurpleAir · cached 3 min ago"
— alongside the existing "PurpleAir live" / "Demo data" labels.

## Testing

All GTK-free stdlib `unittest`, consistent with the existing suite:

- `tests/test_cache.py`: region containment and TTL, delta merge preserves unchanged
  rows, filter mismatch forces a miss, corruption recovery, trend TTL, pruning bounds
  the DB.
- `tests/test_purpleair.py`: `modified_since` request encoding, trimmed area field
  list, trend-only fetch field list.
- `tests/test_store.py`: `refresh_minutes` sanitize bounds.
- `make check` before PR.

## Expected impact

- Dev relaunch loop: near-zero points (fresh-hit path).
- Steady state: ~16 pts × changed rows per interval; trends only for opened sensors.
- A repeat of the last two dev days should cost low tens of thousands of points
  instead of 1.4M.
