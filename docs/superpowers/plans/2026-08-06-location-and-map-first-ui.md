# Location Detection + Map-First UI (Airloom 0.2.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-detect the user's location via GeoClue on every launch, add Nominatim place search/labels, and rebuild the UI as a full-bleed map with floating overlays that auto-fetches sensors wherever the map goes.

**Architecture:** Python keeps all state of record and gains two GUI-free modules (`geocode.py` Nominatim client, `location.py` GeoClue wrapper); `app.py` orchestrates one-shot location fixes, debounced view fetches with a containment/freshness skip, and a home∪favorites timer refresh. The web UI is rewritten as map + overlays with a keyed persistent tile layer moved by CSS transforms. Spec: `docs/superpowers/specs/2026-08-06-location-and-ui-design.md`.

**Tech Stack:** Python stdlib + system PyGObject (GTK4/libadwaita/WebKitGTK/libnotify + Geoclue GIR), hand-written vanilla JS/CSS, OSM raster tiles, Nominatim REST, PurpleAir REST.

## Global Constraints

- Zero third-party dependencies: no pip packages, no JS libraries (CLAUDE.md).
- Unit tests must run without GTK installed: `python3 -m unittest discover -s tests` (CLAUDE.md).
- `make check` (tests + compileall + `node --check airloom/resources/app.js`) must pass at every commit.
- Nominatim policy: max 1 request/second, identifying User-Agent `Airloom/<__version__>`.
- Never touch GTK/WebKit from a worker thread; marshal with `GLib.idle_add`.
- All JSON sent to the WebView uses `ensure_ascii=True` (established in 0.1.1).
- Sensor names are attacker-controlled: escape at every HTML sink with `escapeHtml`.
- Version 0.2.0 must match `airloom/__init__.py`, `CHANGELOG.md`, and the metainfo release entry at release time (Task 10 only).

---

### Task 1: Nominatim geocode client

**Files:**
- Create: `airloom/geocode.py`
- Test: `tests/test_geocode.py`

**Interfaces:**
- Consumes: `airloom.__version__` (exists).
- Produces: `Place` dataclass (`name: str, latitude: float, longitude: float`); `search(query: str, limit: int = 5, timeout: int = 10) -> list[Place]`; `reverse(latitude: float, longitude: float, timeout: int = 10) -> str`; `GeocodeError(RuntimeError)`. Task 5 calls `search`/`reverse` from daemon threads.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_geocode.py
import io
import json
import unittest
from unittest import mock

from airloom import geocode
from airloom.geocode import GeocodeError, Place, reverse, search


def fake_response(payload):
    return mock.MagicMock(
        __enter__=lambda s: io.StringIO(json.dumps(payload)),
        __exit__=lambda s, *a: False,
    )


class GeocodeTest(unittest.TestCase):
    def setUp(self):
        geocode._last_request = 0.0  # defeat the throttle between tests

    def test_search_parses_places_and_shortens_names(self):
        payload = [
            {"lat": "39.1677", "lon": "-120.1452",
             "display_name": "Tahoe City, Placer County, California, United States"},
            {"lat": "bad", "lon": "-120.0", "display_name": "Broken"},
            "not a dict",
        ]
        with mock.patch.object(geocode.urllib.request, "urlopen", return_value=fake_response(payload)) as spy:
            places = search("tahoe city")
        self.assertEqual(places, [Place("Tahoe City, Placer County, California", 39.1677, -120.1452)])
        url = spy.call_args[0][0].full_url
        self.assertIn("nominatim.openstreetmap.org/search", url)
        self.assertIn("q=tahoe+city", url)
        self.assertIn("Airloom/", spy.call_args[0][0].get_header("User-agent"))

    def test_search_empty_query_returns_empty_without_request(self):
        with mock.patch.object(geocode.urllib.request, "urlopen") as spy:
            self.assertEqual(search("   "), [])
        spy.assert_not_called()

    def test_search_wraps_network_errors(self):
        with mock.patch.object(geocode.urllib.request, "urlopen", side_effect=OSError("boom")):
            with self.assertRaises(GeocodeError):
                search("tahoe")

    def test_search_rejects_non_list_payload(self):
        with mock.patch.object(geocode.urllib.request, "urlopen", return_value=fake_response({"error": "x"})):
            with self.assertRaises(GeocodeError):
                search("tahoe")

    def test_reverse_prefers_smallest_locality(self):
        payload = {"address": {"town": "Tahoe City", "county": "Placer County", "state": "California"}}
        with mock.patch.object(geocode.urllib.request, "urlopen", return_value=fake_response(payload)):
            self.assertEqual(reverse(39.1677, -120.1452), "Tahoe City")

    def test_reverse_falls_back_to_coordinates(self):
        with mock.patch.object(geocode.urllib.request, "urlopen", return_value=fake_response({})):
            self.assertEqual(reverse(39.1677, -120.1452), "39.17, -120.15")

    def test_throttle_spaces_requests_one_second_apart(self):
        sleeps = []
        with mock.patch.object(geocode.time, "sleep", side_effect=sleeps.append):
            with mock.patch.object(geocode.urllib.request, "urlopen", return_value=fake_response([])):
                search("one")
            with mock.patch.object(geocode.urllib.request, "urlopen", return_value=fake_response([])):
                search("two")
        self.assertTrue(sleeps and 0 < sleeps[0] <= 1.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_geocode -v`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'airloom.geocode'`

- [ ] **Step 3: Write the implementation**

```python
# airloom/geocode.py
"""Minimal, dependency-free Nominatim client for place search and labels."""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import __version__


SEARCH_URL = "https://nominatim.openstreetmap.org/search"
REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
# Nominatim usage policy: at most one request per second, identifying UA.
_MIN_INTERVAL = 1.0
_last_request = 0.0


class GeocodeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Place:
    name: str
    latitude: float
    longitude: float


def search(query: str, limit: int = 5, timeout: int = 10) -> list[Place]:
    query = query.strip()
    if not query:
        return []
    payload = _request(SEARCH_URL, {"q": query, "format": "jsonv2", "limit": str(limit)}, timeout)
    if not isinstance(payload, list):
        raise GeocodeError("Unexpected geocoder response.")
    places = []
    for row in payload:
        if isinstance(row, dict) and (place := _place_from(row)):
            places.append(place)
    return places


def reverse(latitude: float, longitude: float, timeout: int = 10) -> str:
    payload = _request(
        REVERSE_URL,
        {"lat": f"{latitude:.6f}", "lon": f"{longitude:.6f}", "format": "jsonv2", "zoom": "10"},
        timeout,
    )
    if isinstance(payload, dict):
        address = payload.get("address")
        if isinstance(address, dict):
            for key in ("village", "town", "city", "municipality", "county", "state"):
                value = address.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:80]
        name = payload.get("name") or payload.get("display_name")
        if isinstance(name, str) and name.strip():
            return name.split(",")[0].strip()[:80]
    return f"{latitude:.2f}, {longitude:.2f}"


def _place_from(row: dict) -> Place | None:
    try:
        latitude = float(row["lat"])
        longitude = float(row["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    name = row.get("display_name") or row.get("name") or ""
    if not isinstance(name, str) or not name.strip():
        return None
    parts = [part.strip() for part in name.split(",")]
    return Place(", ".join(parts[:3])[:80], latitude, longitude)


def _request(url: str, params: dict, timeout: int):
    _throttle()
    request = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": f"Airloom/{__version__}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except Exception as exc:
        raise GeocodeError(f"Place lookup failed: {exc}") from exc


def _throttle() -> None:
    global _last_request
    wait = _MIN_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_geocode -v`
Expected: 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add airloom/geocode.py tests/test_geocode.py
git commit -m "Add throttled Nominatim geocode client"
```

---

### Task 2: PurpleAir `show_only` + bounds containment

**Files:**
- Modify: `airloom/purpleair.py` (fetch_sensors ~line 70; add helper after `bounds_around`)
- Test: `tests/test_purpleair.py` (append tests)

**Interfaces:**
- Consumes: existing `Bounds`, `PurpleAirClient`, `FIELDS`.
- Produces: `PurpleAirClient.fetch_sensors(self, bounds: Bounds | None = None, show_only: list[int] | None = None) -> list[Sensor]` (at least one argument required, else `PurpleAirError`); `bounds_contains(outer: Bounds, inner: Bounds) -> bool`. Task 5 uses both.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_purpleair.py`; also add `from airloom.purpleair import PurpleAirClient, bounds_contains, Bounds` to its imports)

```python
    def test_bounds_contains(self):
        outer = Bounds(46.0, -123.0, 45.0, -122.0)
        self.assertTrue(bounds_contains(outer, Bounds(45.9, -122.9, 45.1, -122.1)))
        self.assertTrue(bounds_contains(outer, outer))
        self.assertFalse(bounds_contains(outer, Bounds(46.1, -122.9, 45.1, -122.1)))  # pokes north
        self.assertFalse(bounds_contains(outer, Bounds(45.9, -123.5, 45.1, -122.1)))  # pokes west

    def test_fetch_sensors_builds_show_only_query(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            import io
            return mock.MagicMock(
                __enter__=lambda s: io.StringIO('{"fields": [], "data": []}'),
                __exit__=lambda s, *a: False,
            )

        client = PurpleAirClient("key")
        with mock.patch("airloom.purpleair.urlopen", side_effect=fake_urlopen):
            client.fetch_sensors(show_only=[42, 7])
        self.assertIn("show_only=42%2C7", captured["url"])
        self.assertNotIn("nwlat", captured["url"])

    def test_fetch_sensors_requires_bounds_or_show_only(self):
        with self.assertRaises(PurpleAirError):
            PurpleAirClient("key").fetch_sensors()
```

Add `from unittest import mock` to the test file imports.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_purpleair -v`
Expected: FAIL with `ImportError: cannot import name 'bounds_contains'`

- [ ] **Step 3: Implement.** In `airloom/purpleair.py`, add after `bounds_around`:

```python
def bounds_contains(outer: Bounds, inner: Bounds) -> bool:
    """True when `inner` lies entirely within `outer`."""
    return (
        inner.north <= outer.north
        and inner.south >= outer.south
        and inner.west >= outer.west
        and inner.east <= outer.east
    )
```

Replace the `fetch_sensors` signature and query construction:

```python
    def fetch_sensors(self, bounds: Bounds | None = None, show_only: list[int] | None = None) -> list[Sensor]:
        if not self.api_key:
            raise PurpleAirError("A PurpleAir read key is required for live data.")
        if bounds is None and not show_only:
            raise PurpleAirError("A sensor query needs bounds or sensor ids.")
        params: dict[str, str] = {"fields": ",".join(FIELDS), "location_type": "0"}
        if show_only:
            params["show_only"] = ",".join(str(int(sensor_id)) for sensor_id in show_only)
        else:
            params.update(
                {
                    "nwlat": f"{bounds.north:.6f}",
                    "nwlng": f"{bounds.west:.6f}",
                    "selat": f"{bounds.south:.6f}",
                    "selng": f"{bounds.east:.6f}",
                }
            )
        query = urlencode(params)
```

(The rest of the method — request, error boundary, parse — is unchanged.)

- [ ] **Step 4: Run the full suite**

Run: `python3 -m unittest discover -s tests -v`
Expected: all PASS (existing bounds tests unaffected)

- [ ] **Step 5: Commit**

```bash
git add airloom/purpleair.py tests/test_purpleair.py
git commit -m "Support show_only sensor fetches and bounds containment"
```

---

### Task 3: `home_mode` in the store

**Files:**
- Modify: `airloom/store.py` (DEFAULT_CONFIG, `_sanitize`, `public_config`)
- Test: `tests/test_store.py` (append tests)

**Interfaces:**
- Produces: `store.data["home_mode"]` ∈ {"auto", "fixed"}, default "auto"; included in `public_config()`. Tasks 5 and 8 rely on it.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_store.py`)

```python
    def test_home_mode_defaults_to_auto_and_survives_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            self.assertEqual(store.data["home_mode"], "auto")
            self.assertEqual(store.public_config()["home_mode"], "auto")
            store.data["home_mode"] = "fixed"
            store.save()
            self.assertEqual(Store(path).data["home_mode"], "fixed")

    def test_invalid_home_mode_falls_back_to_auto(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"home_mode": "sometimes"}), encoding="utf-8")
            self.assertEqual(Store(path).data["home_mode"], "auto")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_store -v`
Expected: FAIL with `KeyError: 'home_mode'`

- [ ] **Step 3: Implement.** In `airloom/store.py`: add `"home_mode": "auto",` to `DEFAULT_CONFIG` (after `"temperature_unit": "F",`); in `_sanitize` add (next to the `temperature_unit` check):

```python
    if data.get("home_mode") in ("auto", "fixed"):
        clean["home_mode"] = data["home_mode"]
```

In `public_config()` add `"home_mode": self.data["home_mode"],` after the `temperature_unit` entry.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_store -v` — Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add airloom/store.py tests/test_store.py
git commit -m "Add home_mode (auto/fixed) to config store"
```

---

### Task 4: GeoClue locator wrapper

**Files:**
- Create: `airloom/location.py`

**Interfaces:**
- Produces: `GeoClueLocator(timeout_seconds: int = 10)` with `.start(on_fix)` where `on_fix(latitude: float | None, longitude: float | None)` is called exactly once on the GLib main loop — coordinates on success, `(None, None)` on denial/timeout/missing GIR. Task 5 consumes it.
- No unit test: the module needs the GLib main loop and the system GeoClue service; it is exercised by the Task 10 manual checklist. Keep it import-safe without `gi` (the import lives inside `start`).

- [ ] **Step 1: Write the module**

```python
# airloom/location.py
"""One-shot location detection through GeoClue (GNOME location service)."""

from __future__ import annotations

import sys


APP_ID = "ai.stealthvision.Airloom"


class GeoClueLocator:
    """Requests a single fix; reports via callback on the GLib main loop."""

    def __init__(self, timeout_seconds: int = 10):
        self.timeout_seconds = timeout_seconds
        self._delivered = False
        self._timeout_id = None
        self._simple = None  # keeps the GeoClue client alive until delivery

    def start(self, on_fix) -> None:
        try:
            import gi

            gi.require_version("Geoclue", "2.0")
            from gi.repository import Geoclue, GLib
        except (ImportError, ValueError) as exc:
            print(f"Airloom: GeoClue unavailable: {exc}", file=sys.stderr)
            on_fix(None, None)
            return

        def deliver(latitude, longitude):
            if self._delivered:
                return
            self._delivered = True
            if self._timeout_id is not None:
                GLib.source_remove(self._timeout_id)
                self._timeout_id = None
            self._simple = None
            on_fix(latitude, longitude)

        def on_timeout():
            self._timeout_id = None
            print("Airloom: location fix timed out", file=sys.stderr)
            deliver(None, None)
            return GLib.SOURCE_REMOVE

        def finished(_source, result):
            try:
                simple = Geoclue.Simple.new_finish(result)
                location = simple.get_location()
                self._simple = simple
                deliver(
                    float(location.get_property("latitude")),
                    float(location.get_property("longitude")),
                )
            except Exception as exc:  # denial, agent missing, service error
                print(f"Airloom: location fix failed: {exc}", file=sys.stderr)
                deliver(None, None)

        self._timeout_id = GLib.timeout_add_seconds(self.timeout_seconds, on_timeout)
        Geoclue.Simple.new(APP_ID, Geoclue.AccuracyLevel.NEIGHBORHOOD, None, finished)
```

- [ ] **Step 2: Verify it imports without GTK/GeoClue**

Run: `python3 -c "from airloom.location import GeoClueLocator; GeoClueLocator().start(lambda a, b: print('fix:', a, b))"`
Expected: either a real fix printed (dev machine has GeoClue + main loop absent → likely `fix: None None` after the gi import succeeds but no main loop; both outcomes acceptable) — the essential check is **no traceback**.

- [ ] **Step 3: Run `make check`** — Expected: PASS (tests never import `location.py`).

- [ ] **Step 4: Commit**

```bash
git add airloom/location.py
git commit -m "Add one-shot GeoClue locator wrapper"
```

---

### Task 5: app.py orchestration — location fix, place search, view fetches, home∪favorites timer

**Files:**
- Modify: `airloom/app.py`

**Interfaces:**
- Consumes: `GeoClueLocator` (Task 4), `geocode.search/reverse/GeocodeError` (Task 1), `fetch_sensors(bounds=…, show_only=…)`/`bounds_contains` (Task 2), `store.data["home_mode"]` (Task 3).
- Produces bridge contract for Task 8:
  - handles JS actions `view-changed {north, west, south, east, lat, lon, zoom}` and `place-search {query}`;
  - `save-settings` accepts `{api_key, clear_api_key, home_mode, location_name, home_lat, home_lon, radius_km, alert_threshold, temperature_unit}` (raw `latitude`/`longitude` gone; `home_lat/lon` required only when `home_mode == "fixed"`);
  - sends `location {latitude, longitude, name, source}` (source: "geoclue" | "fixed" | "fallback") and `places {query, results: [{name, latitude, longitude}], error?}`.

- [ ] **Step 1: Add imports and state.** In `airloom/app.py` add `import time` to the stdlib imports; add `from .geocode import GeocodeError, reverse as reverse_geocode, search as place_search` and `from .location import GeoClueLocator` and extend the purpleair import to `from .purpleair import Bounds, PurpleAirClient, PurpleAirError, bounds_around, bounds_contains`. In `__init__`, replace `self.refresh_pending = False` with:

```python
        self.pending_fetch: tuple | None = None
        self.locator: GeoClueLocator | None = None
        self.view_bounds: Bounds | None = None
        self.view_fetched_at = 0.0
```

- [ ] **Step 2: Start the locator on activate.** At the end of `_on_activate`, after `GLib.timeout_add_seconds(...)`:

```python
        if self.store.data.get("home_mode") == "auto":
            self.locator = GeoClueLocator()
            self.locator.start(self._on_location_fix)
```

And add the handlers after `_on_decide_policy`:

```python
    def _on_location_fix(self, latitude, longitude) -> None:
        if latitude is None or longitude is None:
            self._send(
                "location",
                {
                    "latitude": self.store.data["latitude"],
                    "longitude": self.store.data["longitude"],
                    "name": self.store.data["location_name"],
                    "source": "fallback",
                },
            )
            self._send("error", {"message": "Using last known location."})
            return
        self.store.data.update({"latitude": float(latitude), "longitude": float(longitude)})
        self.store.save()
        self._send(
            "location",
            {
                "latitude": float(latitude),
                "longitude": float(longitude),
                "name": self.store.data["location_name"],
                "source": "geoclue",
            },
        )
        self.refresh()
        threading.Thread(
            target=self._reverse_label_worker, args=(float(latitude), float(longitude)), name="airloom-revgeo", daemon=True
        ).start()

    def _reverse_label_worker(self, latitude: float, longitude: float) -> None:
        try:
            name = reverse_geocode(latitude, longitude)
        except GeocodeError as exc:
            print(f"Airloom: {exc}", file=sys.stderr)
            name = f"{latitude:.2f}, {longitude:.2f}"
        GLib.idle_add(self._apply_location_name, name)

    def _apply_location_name(self, name: str) -> bool:
        if self.store.data.get("home_mode") == "auto":
            self.store.data["location_name"] = name[:80]
            self.store.save()
            if self.title:
                self.title.set_subtitle(name[:80])
            self._send("config", self.store.public_config())
        return GLib.SOURCE_REMOVE
```

- [ ] **Step 3: Dispatch the new actions.** In `_on_script_message`, before the `else` branch add:

```python
        elif action == "view-changed":
            self._on_view_changed(message)
        elif action == "place-search":
            self._on_place_search(message)
```

And add the handlers:

```python
    def _on_view_changed(self, message: dict) -> None:
        try:
            view = Bounds(
                float(message["north"]), float(message["west"]),
                float(message["south"]), float(message["east"]),
            )
            center = (float(message["lat"]), float(message["lon"]))
        except (KeyError, TypeError, ValueError):
            return
        if not (-90 <= view.south <= view.north <= 90 and -180 <= view.west <= 180 and -180 <= view.east <= 180):
            return
        fresh = (time.monotonic() - self.view_fetched_at) < AUTO_REFRESH_SECONDS
        if self.view_bounds is not None and fresh and bounds_contains(self.view_bounds, view):
            return
        self._start_fetch(view, center, include_favorites=False)

    def _on_place_search(self, message: dict) -> None:
        query = str(message.get("query") or "").strip()[:120]
        if not query:
            return

        def worker() -> None:
            payload = {"query": query, "results": []}
            try:
                payload["results"] = [
                    {"name": place.name, "latitude": place.latitude, "longitude": place.longitude}
                    for place in place_search(query)
                ]
            except GeocodeError:
                payload["error"] = "Place lookup unavailable."
            GLib.idle_add(self._send_places, payload)

        threading.Thread(target=worker, name="airloom-geocode", daemon=True).start()

    def _send_places(self, payload: dict) -> bool:
        self._send("places", payload)
        return GLib.SOURCE_REMOVE
```

- [ ] **Step 4: Rework refresh into `_start_fetch`.** Replace the whole `refresh()` method with:

```python
    def refresh(self) -> None:
        """Home refresh: home bounds plus favorited sensors wherever they are."""
        config = self.store.data
        bounds = bounds_around(config["latitude"], config["longitude"], config["radius_km"])
        self._start_fetch(bounds, (config["latitude"], config["longitude"]), include_favorites=True)

    def _start_fetch(self, bounds: Bounds, center: tuple[float, float], include_favorites: bool) -> None:
        if not self.webview:
            return
        if self.refreshing:
            # Coalesce: the newest request wins and runs when the current lands.
            self.pending_fetch = (bounds, center, include_favorites)
            return
        self.refreshing = True
        self._send("loading", {"active": True})
        config = dict(self.store.data)

        def worker() -> None:
            source = "Demo data"
            error = None
            sensors: list[Sensor] = []
            try:
                try:
                    if config.get("api_key"):
                        client = PurpleAirClient(config["api_key"])
                        sensors = client.fetch_sensors(bounds=bounds)
                        source = "PurpleAir live"
                        if include_favorites:
                            missing = set(config.get("favorites", [])) - {s.sensor_id for s in sensors}
                            if missing:
                                sensors += client.fetch_sensors(show_only=sorted(missing))
                        if not sensors:
                            error = "No public outdoor sensors were found in this area."
                    else:
                        sensors = demo_sensors(center[0], center[1])
                except PurpleAirError as exc:
                    sensors = demo_sensors(center[0], center[1])
                    error = f"{exc} Showing demo readings instead."
            except Exception as exc:  # noqa: BLE001 — a crashed worker must never wedge the refresh state
                sensors = []
                error = f"Refresh failed unexpectedly: {exc}"
            GLib.idle_add(self._finish_refresh, sensors, source, error, bounds)

        threading.Thread(target=worker, name="airloom-refresh", daemon=True).start()
```

- [ ] **Step 5: Update `_finish_refresh`.** Change its signature to `def _finish_refresh(self, sensors, source, error, bounds) -> bool:` and replace the `refresh_pending` block at the end with:

```python
        self.view_bounds = bounds
        self.view_fetched_at = time.monotonic()
        if self.pending_fetch is not None:
            pending, self.pending_fetch = self.pending_fetch, None
            self._start_fetch(*pending)
        return GLib.SOURCE_REMOVE
```

- [ ] **Step 6: Rework `_save_settings`.** Replace the try-block and store update with:

```python
        try:
            radius = max(2.0, min(100.0, float(message["radius_km"])))
            threshold = max(1, min(500, int(message["alert_threshold"])))
            home_mode = "fixed" if message.get("home_mode") == "fixed" else "auto"
            updates = {
                "radius_km": radius,
                "alert_threshold": threshold,
                "home_mode": home_mode,
                "temperature_unit": "C" if message.get("temperature_unit") == "C" else "F",
            }
            if home_mode == "fixed":
                latitude = float(message["home_lat"])
                longitude = float(message["home_lon"])
                if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                    raise ValueError("Coordinates are outside their valid range.")
                updates["latitude"] = latitude
                updates["longitude"] = longitude
                updates["location_name"] = str(message.get("location_name") or "Custom location")[:80]
        except (KeyError, TypeError, ValueError) as exc:
            self._send("error", {"message": f"Could not save preferences: {exc}"})
            return

        self.store.data.update(updates)
```

(Keep the api_key handling, save, subtitle, config send, and `self.refresh()` that follow. When switching to auto mode with the window open, also start a locator if none delivered yet this session:)

```python
        if home_mode == "auto":
            self.locator = GeoClueLocator()
            self.locator.start(self._on_location_fix)
```

Insert that immediately before the final `self.refresh()` — and in that case `refresh()` still runs (the fix will re-refresh when it arrives; coalescing handles overlap).

- [ ] **Step 7: Verify**

Run: `make check && python3 -c "import airloom.app; print('imports OK')"`
Expected: all tests PASS, no import errors.

- [ ] **Step 8: Commit**

```bash
git add airloom/app.py
git commit -m "Wire location fixes, place search, and view-driven fetching"
```

---

### Task 6: Keyed tile renderer with transform panning

**Files:**
- Modify: `airloom/resources/app.js` (`renderMap`, `renderMapMarkers`, `panBy`, `zoom`; new `updateMapTransform`)
- Modify: `airloom/resources/app.css` (`.tile-layer`, `.marker-layer`, `.tile`, `.map-marker` positioning)

This task lands inside the existing three-pane layout (the shell rewrite is Task 7); the DOM ids `#map-panel`, `#tiles`, `#markers` are unchanged.

**Interfaces:**
- Produces: `renderMap()` (diffs tiles, repositions layers), `updateMapTransform()` (pan-frame-cheap), `renderMapMarkers()` (rebuilds marker DOM; call only on data/selection/zoom/settle). Tiles and markers are positioned in **world pixels** at the current zoom; both layers share one `translate` transform.

- [ ] **Step 1: Replace the map render functions in `app.js`**

```js
  let tileLayer = { zoom: null, tiles: new Map() };

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

  function renderMap() {
    const view = mapViewport();
    if (!view.width || !view.height) return;
    const layer = $("#tiles");
    if (tileLayer.zoom !== state.zoom) {
      layer.textContent = "";
      tileLayer = { zoom: state.zoom, tiles: new Map() };
    }
    const maxTile = 2 ** state.zoom;
    const needed = new Set();
    for (let ty = Math.floor(view.top / 256); ty <= Math.floor((view.top + view.height) / 256); ty++) {
      if (ty < 0 || ty >= maxTile) continue;
      for (let tx = Math.floor(view.left / 256); tx <= Math.floor((view.left + view.width) / 256); tx++) {
        const key = `${tx}/${ty}`;
        needed.add(key);
        if (!tileLayer.tiles.has(key)) {
          const wrappedX = ((tx % maxTile) + maxTile) % maxTile;
          const img = document.createElement("img");
          img.className = "tile";
          img.draggable = false;
          img.alt = "";
          img.src = `https://tile.openstreetmap.org/${state.zoom}/${wrappedX}/${ty}.png`;
          img.style.left = `${tx * 256}px`;
          img.style.top = `${ty * 256}px`;
          layer.appendChild(img);
          tileLayer.tiles.set(key, img);
        }
      }
    }
    for (const [key, img] of tileLayer.tiles) {
      if (!needed.has(key)) { img.remove(); tileLayer.tiles.delete(key); }
    }
    updateMapTransform();
    renderMapMarkers();
  }

  function updateMapTransform() {
    const view = mapViewport();
    if (!view.width) return;
    const transform = `translate(${-view.left}px, ${-view.top}px)`;
    $("#tiles").style.transform = transform;
    $("#markers").style.transform = transform;
  }

  function renderMapMarkers() {
    const view = mapViewport();
    if (!view.width) return;
    const pad = 300; // generous cull margin so mid-pan gaps are rare
    const markers = visibleSensors().map((sensor) => {
      const point = worldPoint(sensor.latitude, sensor.longitude, state.zoom);
      if (point.x < view.left - pad || point.x > view.left + view.width + pad ||
          point.y < view.top - pad || point.y > view.top + view.height + pad) return "";
      return `<button class="map-marker${sensor.id === state.selectedId ? " selected" : ""}" data-id="${sensor.id}" title="${escapeHtml(sensor.name)} · AQI ${sensor.aqi ?? "unavailable"}" style="left:${point.x}px;top:${point.y}px;--sensor:${sensor.color};--sensor-fg:${sensor.foreground}">${sensor.aqi ?? "—"}</button>`;
    });
    $("#markers").innerHTML = markers.join("");
    document.querySelectorAll(".map-marker").forEach((marker) => marker.addEventListener("click", (event) => { event.stopPropagation(); selectSensor(Number(marker.dataset.id), true); }));
    updateMapTransform();
  }

  function panBy(dx, dy) {
    const center = worldPoint(state.center.lat, state.center.lon, state.zoom);
    state.center = inverseWorld(center.x - dx, center.y - dy, state.zoom);
    const view = mapViewport();
    // Cheap per-frame path: move layers; only re-diff tiles when the view
    // crosses outside the currently materialized tile ring.
    updateMapTransform();
    const tx0 = Math.floor(view.left / 256), ty0 = Math.floor(view.top / 256);
    const tx1 = Math.floor((view.left + view.width) / 256), ty1 = Math.floor((view.top + view.height) / 256);
    for (let ty = ty0; ty <= ty1; ty++) {
      for (let tx = tx0; tx <= tx1; tx++) {
        if (!tileLayer.tiles.has(`${tx}/${ty}`) && ty >= 0 && ty < 2 ** state.zoom) { renderMap(); return; }
      }
    }
  }
```

Keep the `zoom(delta)` function but it now relies on `renderMap()`'s zoom-change rebuild (no change needed beyond what exists).

- [ ] **Step 2: Update CSS.** In `app.css`, ensure these rules (replacing any `left/top`-based tile rules):

```css
.tile-layer, .marker-layer { position: absolute; inset: 0; will-change: transform; }
.tile { position: absolute; width: 256px; height: 256px; user-select: none; }
.map-marker { position: absolute; transform: translate(-50%, -50%); }
```

(`.map-marker` keeps its existing size/color rules; only the positioning basis changes.)

- [ ] **Step 3: Hook pan settle.** In the `pointerup` listener add `renderMapMarkers();` after the drag-state reset (refreshes culling after a drag).

- [ ] **Step 4: Verify**

Run: `node --check airloom/resources/app.js && make check`
Then open `airloom/resources/index.html` in a browser (preview mode): drag the map — tiles must persist (no flicker/refetch on every frame; verify in devtools Network that tile requests only occur when new tiles scroll into view), markers move with the map, zoom in/out works, date-line and top/bottom clamps still hold.

- [ ] **Step 5: Commit**

```bash
git add airloom/resources/app.js airloom/resources/app.css
git commit -m "Rework map renderer: keyed persistent tiles, transform panning"
```

---

### Task 7: Map-first shell — index.html + app.css + render wiring

**Files:**
- Modify: `airloom/resources/index.html` (full body rewrite)
- Modify: `airloom/resources/app.css` (full rewrite of layout sections; keep palette/dark-mode variables)
- Modify: `airloom/resources/app.js` (render functions + listeners for the new DOM)

**Interfaces:**
- Consumes: Task 6 renderer (unchanged ids `#map-panel`, `#tiles`, `#markers`).
- Produces DOM contract used by Task 8: `#search`, `#search-results`, `#summary-chip`, `#sensors-button`, `#sensors-panel`, `#detail-card`, `#legend-chip`, `#legend`, plus the existing detail ids (`#sensor-name`, `#aqi-number`, `#aqi-category`, `#temperature`, `#humidity`, `#pm25`, `#pm10`, `#guidance`, `#guidance-card`, `#favorite-button`, `#updated-time`, `#chart`, `#trend-direction`), list ids (`#favorites-list`, `#sensor-list`, `#sensor-count`, `#favorite-count`), status ids (`#data-source`, `#place-name`), controls (`#zoom-in`, `#zoom-out`, `#recenter`), settings dialog ids unchanged from 0.1.1 except the location fields (Task 8 changes those).

- [ ] **Step 1: Replace the `<body>` of `index.html`** (head/CSP unchanged):

```html
  <body>
    <main class="map-shell">
      <section class="map-panel" id="map-panel" aria-label="Air quality sensor map">
        <div id="tiles" class="tile-layer"></div>
        <div id="markers" class="marker-layer"></div>
      </section>

      <div class="overlay search-overlay">
        <label class="search-pill">
          <svg viewBox="0 0 24 24"><path d="m21 20-5.2-5.2a7 7 0 1 0-1 1L20 21l1-1ZM5 10.5a5.5 5.5 0 1 1 11 0 5.5 5.5 0 0 1-11 0Z"/></svg>
          <input id="search" type="search" placeholder="Search sensors or places" autocomplete="off">
          <kbd>Ctrl K</kbd>
        </label>
        <div id="search-results" class="search-results" hidden></div>
      </div>

      <button class="overlay summary-chip" id="summary-chip" title="Area air quality">
        <span class="summary-score" id="summary-aqi">—</span>
        <span class="summary-copy">
          <strong id="place-name">Locating…</strong>
          <small id="summary-label">Loading readings</small>
        </span>
        <span class="pulse" id="loading-pulse"></span>
      </button>

      <div class="overlay corner-buttons">
        <button id="sensors-button">☰ Sensors <span class="count" id="sensor-count">0</span></button>
        <button id="legend-chip" aria-expanded="false">AQI</button>
      </div>
      <div class="legend" id="legend" hidden>
        <div><i style="--c:#35b779"></i><span>Good</span><b>0–50</b></div>
        <div><i style="--c:#f6c945"></i><span>Moderate</span><b>51–100</b></div>
        <div><i style="--c:#f39c3d"></i><span>Sensitive</span><b>101–150</b></div>
        <div><i style="--c:#e65b65"></i><span>Unhealthy</span><b>151+</b></div>
      </div>

      <div class="overlay map-controls">
        <button id="zoom-in" title="Zoom in" aria-label="Zoom in">+</button>
        <button id="zoom-out" title="Zoom out" aria-label="Zoom out">−</button>
        <button id="recenter" title="Back to home" aria-label="Back to home">
          <svg viewBox="0 0 24 24"><path d="M11 2h2v3.1A7 7 0 0 1 18.9 11H22v2h-3.1a7 7 0 0 1-5.9 5.9V22h-2v-3.1A7 7 0 0 1 5.1 13H2v-2h3.1A7 7 0 0 1 11 5.1V2Zm1 5a5 5 0 1 0 0 10 5 5 0 0 0 0-10Zm0 3a2 2 0 1 1 0 4 2 2 0 0 1 0-4Z"/></svg>
        </button>
      </div>

      <aside class="sensors-panel" id="sensors-panel" hidden>
        <header><h2>Sensors</h2><button class="icon-button" id="close-sensors" aria-label="Close sensor list">×</button></header>
        <div class="section-heading"><h3>Favorites</h3><span id="favorite-count">0</span></div>
        <div class="sensor-list compact" id="favorites-list"></div>
        <div class="section-heading"><h3>Nearby</h3></div>
        <div class="sensor-list" id="sensor-list"></div>
        <footer class="panel-footer"><span class="status-dot"></span><span id="data-source">Starting Airloom</span><button id="footer-refresh">Refresh</button></footer>
      </aside>

      <aside class="detail-card" id="detail-card" hidden>
        <div class="detail-topbar">
          <span id="updated-time">Waiting for data</span>
          <span class="detail-actions">
            <button class="favorite-button" id="favorite-button" title="Favorite this sensor" aria-label="Favorite this sensor">
              <svg viewBox="0 0 24 24"><path d="m12 17.3-6.2 3.5 1.4-6.9-5.1-4.7 7-.8L12 2l2.9 6.4 7 .8-5.1 4.7 1.4 6.9-6.2-3.5Zm0-2.3 3.5 2-.8-3.9 3-2.7-4.1-.5L12 6.2l-1.6 3.7-4.1.5 3 2.7-.8 3.9 3.5-2Z"/></svg>
            </button>
            <button class="icon-button" id="close-detail" aria-label="Close detail">×</button>
          </span>
        </div>
        <p class="sensor-name" id="sensor-name">Choose a sensor</p>
        <div class="aqi-row">
          <span class="aqi-number" id="aqi-number">—</span>
          <div><span class="aqi-unit">US AQI</span><strong id="aqi-category">Unavailable</strong></div>
        </div>
        <section class="chart-card">
          <div class="chart-heading"><div><span>Air quality trend</span><strong id="trend-direction">—</strong></div><span class="chart-period">Rolling averages</span></div>
          <div class="chart" id="chart"></div>
        </section>
        <section class="metrics-grid">
          <article><span>Temperature</span><strong id="temperature">—</strong></article>
          <article><span>Humidity</span><strong id="humidity">—</strong></article>
          <article><span>PM2.5</span><strong id="pm25">—</strong></article>
          <article><span>PM10</span><strong id="pm10">—</strong></article>
        </section>
        <section class="guidance-card" id="guidance-card">
          <div><span>Health guidance</span><p id="guidance">Select a nearby sensor to see current guidance.</p></div>
        </section>
      </aside>

      <div class="attribution">© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors</div>
    </main>

    <div class="toast-stack" id="toast-stack"></div>

    <dialog id="settings-dialog" class="settings-dialog">
      <!-- unchanged from 0.1.1 in this task; Task 8 replaces the location fields -->
    </dialog>
    <script src="app.js"></script>
  </body>
```

(Carry the 0.1.1 settings dialog markup over verbatim in this task.)

- [ ] **Step 2: Rewrite the layout CSS.** Keep the existing `:root` palette, fonts, toast, dialog, sensor-row, chart, and dark-mode blocks; replace the grid/sidebar/detail-panel/breakpoint sections with:

```css
.map-shell { position: fixed; inset: 0; }
.map-panel { position: absolute; inset: 0; overflow: hidden; background: #dfe7df; cursor: grab; }
.map-panel.dragging { cursor: grabbing; }

.overlay { position: absolute; z-index: 5; }
.search-overlay { top: 14px; left: 14px; width: min(340px, calc(100vw - 28px)); }
.search-pill { display: flex; align-items: center; gap: 8px; background: var(--surface, #fff); border-radius: 22px; padding: 9px 14px; box-shadow: 0 2px 10px rgba(0,0,0,.18); }
.search-pill input { border: 0; background: none; outline: none; flex: 1; font: inherit; }
.search-results { margin-top: 6px; background: var(--surface, #fff); border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,.22); max-height: 50vh; overflow-y: auto; padding: 4px; }
.search-results .group { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; opacity: .6; padding: 6px 10px 2px; }
.search-results button { display: block; width: 100%; text-align: left; border: 0; background: none; padding: 8px 10px; border-radius: 8px; font: inherit; cursor: pointer; }
.search-results button:hover, .search-results button:focus-visible { background: rgba(0,0,0,.06); }

.summary-chip { top: 14px; right: 14px; display: flex; align-items: center; gap: 10px; background: var(--surface, #fff); border: 0; border-radius: 22px; padding: 7px 14px 7px 8px; box-shadow: 0 2px 10px rgba(0,0,0,.18); font: inherit; cursor: default; }
.summary-chip .summary-score { min-width: 40px; height: 40px; border-radius: 50%; display: grid; place-items: center; font-weight: 800; background: #e7ece7; }
.summary-chip .summary-copy { display: flex; flex-direction: column; text-align: left; line-height: 1.2; }

.corner-buttons { left: 14px; bottom: 34px; display: flex; gap: 8px; }
.corner-buttons button { background: var(--surface, #fff); border: 0; border-radius: 18px; padding: 8px 13px; box-shadow: 0 2px 8px rgba(0,0,0,.18); font: inherit; cursor: pointer; }
.legend { position: absolute; left: 14px; bottom: 76px; z-index: 6; background: var(--surface, #fff); border-radius: 12px; padding: 10px 12px; box-shadow: 0 4px 16px rgba(0,0,0,.22); }
.legend div { display: flex; align-items: center; gap: 8px; padding: 2px 0; font-size: 12px; }
.legend i { width: 12px; height: 12px; border-radius: 4px; background: var(--c); }
.legend b { margin-left: auto; }

.map-controls { right: 14px; bottom: 34px; display: flex; flex-direction: column; gap: 6px; }
.map-controls button { width: 38px; height: 38px; border: 0; border-radius: 10px; background: var(--surface, #fff); box-shadow: 0 2px 8px rgba(0,0,0,.18); font-size: 18px; cursor: pointer; display: grid; place-items: center; }

.sensors-panel { position: absolute; z-index: 7; top: 70px; left: 14px; bottom: 34px; width: min(320px, calc(100vw - 28px)); background: var(--surface, #fff); border-radius: 14px; box-shadow: 0 6px 24px rgba(0,0,0,.25); display: flex; flex-direction: column; padding: 12px; }
.sensors-panel[hidden], .detail-card[hidden], .legend[hidden], .search-results[hidden] { display: none; }
.sensors-panel header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
.sensors-panel #sensor-list { flex: 1; overflow-y: auto; }
.sensors-panel #favorites-list { max-height: 130px; overflow-y: auto; }
.panel-footer { display: flex; align-items: center; gap: 8px; padding-top: 8px; font-size: 12px; }
.panel-footer button { margin-left: auto; border: 0; background: none; color: var(--accent, #2563eb); cursor: pointer; font: inherit; }

.detail-card { position: absolute; z-index: 7; top: 70px; right: 14px; bottom: 34px; width: min(340px, calc(100vw - 28px)); background: var(--surface, #fff); border-radius: 14px; box-shadow: 0 6px 24px rgba(0,0,0,.25); overflow-y: auto; padding: 14px; }

.attribution { position: absolute; right: 4px; bottom: 2px; z-index: 4; font-size: 10px; opacity: .75; }

@media (max-width: 640px) {
  .sensors-panel, .detail-card { left: 0; right: 0; top: auto; bottom: 0; width: auto; max-height: 70vh; border-radius: 14px 14px 0 0; }
}
```

(Adapt color variables to the file's existing `--surface`/`--accent` names — if the current CSS uses different variable names, use those; do not invent a second palette. Dark-mode block: extend the existing `prefers-color-scheme: dark` section so `.search-pill`, `.summary-chip`, panels, and legend use the existing dark surface color, and add `.tile { filter: brightness(.85) contrast(1.05); }` inside it.)

- [ ] **Step 3: Rewire app.js for the new DOM.**
  - `renderSummary()`: update `#summary-aqi` (background/color as before), `#summary-label` (category), and drop the `#summary-subtitle` line — instead set `$("#summary-chip").title = `${valid.length} sensors reporting``.
  - `renderLists()`: unchanged ids; remove the reference to the deleted `.empty-state` favorites markup only if selectors changed (they did not).
  - `renderDetail()`: on select also `$("#detail-card").hidden = false;` on the empty branch `$("#detail-card").hidden = true;` (replaces the placeholder-text reset from 0.1.1).
  - `selectSensor()`: replace `document.body.classList.add("show-detail")` with `$("#detail-card").hidden = false;`.
  - New listeners (replace the old `show-map`/`show-list`/`back-to-map` ones, which no longer exist):

```js
  $("#sensors-button").addEventListener("click", () => { $("#sensors-panel").hidden = !$("#sensors-panel").hidden; });
  $("#close-sensors").addEventListener("click", () => { $("#sensors-panel").hidden = true; });
  $("#close-detail").addEventListener("click", () => { $("#detail-card").hidden = true; });
  $("#legend-chip").addEventListener("click", () => {
    const legend = $("#legend");
    legend.hidden = !legend.hidden;
    $("#legend-chip").setAttribute("aria-expanded", String(!legend.hidden));
  });
```

  - Escape handler becomes: close search results, then detail, then panel (settings dialog still guards first):

```js
    if (event.key === "Escape" && !$("#settings-dialog").open) {
      if (!$("#search-results").hidden) $("#search-results").hidden = true;
      else if (!$("#detail-card").hidden) $("#detail-card").hidden = true;
      else $("#sensors-panel").hidden = true;
    }
```

  - Remove `document.body.classList` usages for `show-detail`/`show-map` everywhere.
  - `applySensors`: keep, but `#place-name` now lives in the summary chip (id unchanged, still works).

- [ ] **Step 4: Verify**

Run: `node --check airloom/resources/app.js && make check`
Browser preview: full-window map with demo sensors; search filters the (open) sensors panel; marker click opens the detail card; ✕/Esc close things; legend chip toggles; controls zoom/recenter; narrow window (devtools responsive mode) turns panels into bottom sheets; dark mode (devtools emulation) keeps everything legible.

- [ ] **Step 5: Commit**

```bash
git add airloom/resources/index.html airloom/resources/app.css airloom/resources/app.js
git commit -m "Rebuild UI as full-bleed map with floating overlays"
```

---

### Task 8: Dynamic behaviors — view-changed, place search, location glide, preferences rework

**Files:**
- Modify: `airloom/resources/app.js`
- Modify: `airloom/resources/index.html` (settings dialog location fields)

**Interfaces:**
- Consumes: Task 5 bridge contract (`view-changed`, `place-search`, `places`, `location`, reworked `save-settings`), Task 7 DOM.
- Produces: fully wired UI; no new interfaces.

- [ ] **Step 1: View-change debounce in app.js** (place near `panBy`):

```js
  let viewTimer = null;
  function scheduleViewChanged() {
    clearTimeout(viewTimer);
    viewTimer = setTimeout(sendViewChanged, 1200);
  }
  function sendViewChanged() {
    const view = mapViewport();
    if (!view.width) return;
    const nw = inverseWorld(view.left, view.top, state.zoom);
    const se = inverseWorld(view.left + view.width, view.top + view.height, state.zoom);
    bridge({ action: "view-changed", north: nw.lat, west: nw.lon, south: se.lat, east: se.lon, lat: state.center.lat, lon: state.center.lon, zoom: state.zoom });
  }
```

Call `scheduleViewChanged()` at the end of `panBy`, `zoom`, and `flyTo` (below). The recenter button flies home: replace its listener body with `flyTo(state.home.lat, state.home.lon)`.

- [ ] **Step 2: flyTo animation:**

```js
  function flyTo(lat, lon, durationMs = 600) {
    const from = { ...state.center };
    const start = performance.now();
    function step(now) {
      const t = Math.min(1, (now - start) / durationMs);
      const ease = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
      state.center = { lat: from.lat + (lat - from.lat) * ease, lon: from.lon + (lon - from.lon) * ease };
      renderMap();
      if (t < 1) requestAnimationFrame(step);
      else scheduleViewChanged();
    }
    requestAnimationFrame(step);
  }
```

- [ ] **Step 3: `location` event handling.** In `window.Airloom.receive` add `if (event === "location") applyLocation(payload);` and:

```js
  function applyLocation(payload) {
    const lat = Number(payload.latitude), lon = Number(payload.longitude);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
    state.home = { lat, lon };
    if (payload.name) $("#place-name").textContent = payload.name;
    if (payload.source === "geoclue" || payload.source === "fixed") flyTo(lat, lon);
  }
```

Also in `applyConfig`, stop resetting `state.center` (delete the `state.center = { ...state.home }` line and keep only `state.home` updates) — the map position is now owned by pans/flyTo.

- [ ] **Step 4: Place search.** Replace the `#search` input listener with a combined sensors+places flow:

```js
  let placeTimer = null;
  $("#search").addEventListener("input", (event) => {
    state.query = event.target.value;
    renderLists();
    renderMapMarkers();
    renderSearchResults([]);
    clearTimeout(placeTimer);
    if (state.query.trim().length >= 3) placeTimer = setTimeout(() => bridge({ action: "place-search", query: state.query.trim() }), 450);
  });
  $("#search").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && state.query.trim().length >= 2) bridge({ action: "place-search", query: state.query.trim() });
  });

  function renderSearchResults(places, error) {
    const box = $("#search-results");
    const query = state.query.trim().toLowerCase();
    const sensors = query ? state.sensors.filter((s) => s.name.toLowerCase().includes(query)).slice(0, 4) : [];
    if (!sensors.length && !places.length && !error) { box.hidden = true; box.innerHTML = ""; return; }
    let html = "";
    if (sensors.length) html += '<div class="group">Sensors</div>' + sensors.map((s) => `<button data-kind="sensor" data-id="${s.id}">${escapeHtml(s.name)} · AQI ${s.aqi ?? "—"}</button>`).join("");
    if (places.length) html += '<div class="group">Places</div>' + places.map((p, i) => `<button data-kind="place" data-index="${i}">${escapeHtml(p.name)}</button>`).join("");
    if (error) html += `<div class="group">${escapeHtml(error)}</div>`;
    box.innerHTML = html;
    box.hidden = false;
    state.placeResults = places;
    box.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => {
      if (button.dataset.kind === "sensor") { selectSensor(Number(button.dataset.id), true); }
      else { const place = state.placeResults[Number(button.dataset.index)]; if (place) flyTo(place.latitude, place.longitude); }
      box.hidden = true;
    }));
  }
```

In `window.Airloom.receive` add:

```js
      if (event === "places") { if (payload.query === state.query.trim()) renderSearchResults(payload.results || [], payload.error); }
```

(The query-match check drops stale responses.) Add `placeResults: []` to the `state` object literal.

- [ ] **Step 5: Preferences rework.** In `index.html`, inside the settings form replace the three location fields (`location_name`, `latitude`, `longitude` labels) with:

```html
          <fieldset class="wide"><legend>Home location</legend>
            <label><input name="home_mode" type="radio" value="auto" checked><span>Detect automatically</span></label>
            <label><input name="home_mode" type="radio" value="fixed"><span>Fixed home</span></label>
          </fieldset>
          <label class="wide" id="home-place-row" hidden><span>Fixed home place</span>
            <input id="home-place-input" type="search" placeholder="Search for a town or address" autocomplete="off">
            <small id="home-place-status">No fixed home chosen</small>
            <div id="home-place-results" class="search-results" hidden></div>
          </label>
          <input type="hidden" name="home_lat"><input type="hidden" name="home_lon"><input type="hidden" name="location_name">
```

In `app.js` `openSettings`: set the radio from `config.home_mode`, toggle `#home-place-row` visibility, prefill the hidden fields and status from current config:

```js
    form.elements.home_mode.value = config.home_mode || "auto";
    $("#home-place-row").hidden = form.elements.home_mode.value !== "fixed";
    form.elements.home_lat.value = config.latitude;
    form.elements.home_lon.value = config.longitude;
    form.elements.location_name.value = config.location_name || "";
    $("#home-place-status").textContent = config.home_mode === "fixed" ? `Fixed: ${config.location_name}` : "No fixed home chosen";
```

Add listeners (with the other dialog listeners):

```js
  document.querySelectorAll('input[name="home_mode"]').forEach((radio) => radio.addEventListener("change", () => {
    $("#home-place-row").hidden = $("#settings-form").elements.home_mode.value !== "fixed";
  }));
  let homePlaceTimer = null;
  $("#home-place-input").addEventListener("input", (event) => {
    clearTimeout(homePlaceTimer);
    const query = event.target.value.trim();
    if (query.length >= 3) homePlaceTimer = setTimeout(() => { state.homeSearchActive = true; bridge({ action: "place-search", query }); }, 450);
  });
```

Route `places` responses: change the receive handler to

```js
      if (event === "places") {
        if (state.homeSearchActive) renderHomePlaceResults(payload.results || [], payload.error);
        else if (payload.query === state.query.trim()) renderSearchResults(payload.results || [], payload.error);
      }
```

with:

```js
  function renderHomePlaceResults(places, error) {
    const box = $("#home-place-results");
    box.innerHTML = error ? `<div class="group">${escapeHtml(error)}</div>` : places.map((p, i) => `<button type="button" data-index="${i}">${escapeHtml(p.name)}</button>`).join("");
    box.hidden = false;
    box.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => {
      const place = places[Number(button.dataset.index)];
      const form = $("#settings-form");
      form.elements.home_lat.value = place.latitude;
      form.elements.home_lon.value = place.longitude;
      form.elements.location_name.value = place.name.split(",")[0];
      $("#home-place-status").textContent = `Fixed: ${place.name}`;
      box.hidden = true;
      state.homeSearchActive = false;
    }));
  }
```

Set `state.homeSearchActive = false` when the dialog closes (`close` event on `#settings-dialog`). Update the submit handler's bridge message to send `home_mode: form.get("home_mode"), home_lat: form.get("home_lat"), home_lon: form.get("home_lon"), location_name: form.get("location_name")` and drop `latitude`/`longitude`. Add `homeSearchActive: false` to the `state` literal. Update the privacy note paragraph to:

```html
        <p class="privacy-note">Your key and favorites stay in a private local config file. Location detection uses your system's GeoClue service; place search and labels use OpenStreetMap Nominatim; map tiles load from OpenStreetMap; your coordinates go to PurpleAir only as map bounds when live data is enabled.</p>
```

- [ ] **Step 6: Browser-preview fallback.** The preview (no bridge) must still work: guard `sendViewChanged`/`place-search` no-ops are inherent (bridge() is already a no-op there). Verify preview still renders demo data.

- [ ] **Step 7: Verify**

Run: `node --check airloom/resources/app.js && make check`
Then `./run`: launch → permission prompt (first run) → map glides to your location, label becomes your town; pan somewhere → sensors auto-load after ~1.2 s settle; pan back inside fetched bounds → no refetch (watch stderr/network); search "Truckee" → Places group → click → fly + fetch; prefs → Fixed home → search, pick, save → relaunch opens there with no GeoClue prompt; switch back to auto → save → glides to detected location.

- [ ] **Step 8: Commit**

```bash
git add airloom/resources/app.js airloom/resources/index.html
git commit -m "Wire auto-fetch on move, place search, location glide, home-mode prefs"
```

---

### Task 9: Packaging + demo-mode polish

**Files:**
- Modify: `packaging/ai.stealthvision.Airloom.yml` (finish-args)

- [ ] **Step 1: Add the GeoClue permission.** In `finish-args`, after `--talk-name=org.freedesktop.Notifications` add:

```yaml
  - --system-talk-name=org.freedesktop.GeoClue2
```

- [ ] **Step 2: Verify manifest**

Run: `python3 -c "import yaml, sys; yaml.safe_load(open('packaging/ai.stealthvision.Airloom.yml')); print('YAML OK')"` (or `bash -n` equivalent if PyYAML is absent — visual check acceptable).

- [ ] **Step 3: Commit**

```bash
git add packaging/ai.stealthvision.Airloom.yml
git commit -m "Grant GeoClue access to the Flatpak sandbox"
```

---

### Task 10: Release 0.2.0

**Files:**
- Modify: `airloom/__init__.py`, `CHANGELOG.md`, `packaging/ai.stealthvision.Airloom.metainfo.xml`

- [ ] **Step 1: Bump version** — `__version__ = "0.2.0"`. Add a `## 0.2.0 — <today>` CHANGELOG section covering: GeoClue auto-detection with fixed-home option, Nominatim place search + labels, map-first overlay UI, keyed tile renderer, auto-fetch on map move, home∪favorites alert refresh. Add the matching `<release version="0.2.0" date="<today>">` metainfo entry (one-sentence description).

- [ ] **Step 2: Full manual checklist** (from the spec — run the app):
  - first-run permission prompt appears once; denial → "Using last known location" toast, app usable
  - launch glide + reverse-geocoded label
  - pan auto-fetch, containment skip, demo-mode pan regeneration (without API key)
  - favorite a sensor, pan away, wait for the 5-min timer → alert still fires (temporarily set threshold below its AQI)
  - fixed-home flow + no GeoClue call in fixed mode
  - narrow-window sheets, dark mode, browser preview

- [ ] **Step 3: `make check`, commit, tag, dispatch**

```bash
make check
git add -A && git commit -m "Release 0.2.0: location detection and map-first UI"
git push origin main
git tag -a v0.2.0 -m "Airloom 0.2.0 — location detection and map-first UI"
git push origin v0.2.0
gh workflow run Flatpak --ref v0.2.0   # push events do not trigger workflows in this repo
gh run watch --exit-status <run-id>
gh release view v0.2.0                 # confirm both bundles + SHA256SUMS
```
