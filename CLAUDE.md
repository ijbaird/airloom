# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Airloom is a Fedora/GNOME desktop app showing hyperlocal PurpleAir air-quality readings. It deliberately has **zero third-party dependencies**: Python stdlib + system PyGObject (GTK4, libadwaita, WebKitGTK 6.0, libnotify) on the backend, and hand-written vanilla JS/CSS on the frontend (no map library — OSM tile math is done manually in `app.js`). Keep it that way; don't add pip packages or JS dependencies.

## Commands

```bash
./run                                    # launch the app (or: make run)
make test                                # all unit tests (python3 -m unittest discover -s tests -v)
python3 -m unittest tests.test_aqi -v    # single test module
python3 -m unittest tests.test_aqi.TruncateTest.test_name  # single test
make check                               # tests + compileall + node --check on app.js — run before PRs
./scripts/build-flatpak.sh               # Flatpak bundle → dist/ (needs GNOME 50 runtime/SDK)
```

Tests are pure stdlib `unittest` and only cover the GUI-free modules (`aqi`, `purpleair`, `store`, bridge encoding); they run without GTK installed.

## Architecture

Two halves connected by a JSON message bridge:

- **Python shell** (`airloom/app.py`): `Adw.Application` owning the window, header bar, preferences persistence, PurpleAir fetching, and desktop notifications. All state of record (sensors, selection, favorites, config) lives here.
- **Web UI** (`airloom/resources/index.html` + `app.js` + `app.css`): rendered in an embedded `WebKit.WebView`; draws the map, lists, detail pane, and settings dialog from state pushed by Python.

**Bridge protocol** — the pair of functions to know:
- JS → Python: `bridge()` in `app.js` posts a JSON string to the `airloom` script-message handler; `_on_script_message` in `app.py` dispatches on `action` (`ready`, `refresh`, `select`, `favorite`, `save-settings`, `view-changed`, `place-search`). Note the quirk: JSC's `to_json()` wraps a posted string in an extra JSON encoding layer, so Python decodes twice (covered by `tests/test_bridge.py`).
- Python → JS: `_send(event, payload)` evaluates `window.Airloom.receive(event, payload)` in the webview; events are `config`, `sensors`, `loading`, `error`, `open-settings`, `location`, `places`.

**Threading**: network fetches run in a daemon thread; results re-enter the GTK main loop via `GLib.idle_add` (`refresh()` → `_finish_refresh()`). Never touch GTK/WebKit from a worker thread.

**Data flow**: with an API key, `purpleair.py` queries the PurpleAir REST API within `bounds_around()` the configured center; without one (or on any `PurpleAirError`), `demo.py` generates deterministic labeled demo sensors. `aqi.py` holds the US EPA 2024 PM2.5 breakpoints and the EPA wildfire-smoke correction for PurpleAir CF=1 data — changes to AQI/data behavior need tests (per CONTRIBUTING.md). `store.py` persists config/favorites/alert state atomically to `~/.config/airloom/config.json` with mode 0600; the API key never leaves `public_config()` except as a masked hint.

`app.js` also has a browser-preview fallback (bottom of file): opening `index.html` in a plain browser renders demo data without the bridge, useful for UI iteration.

**Debug port**: launching with `AIRLOOM_DEBUG_SOCKET=/path/to.sock ./run` opens a local 0600 Unix socket (newline-delimited JSON: `ping`, `eval` (JS in the webview), `pinch` (synthesize begin/change/end)) so an agent can drive and inspect the running app — see `airloom/debugport.py`. Trackpad pinch is intercepted as a capture-phase `Gtk.GestureZoom` in `app.py` (claimed before WebKitGTK's internal page-scale gesture can consume it) and forwarded over the bridge as `pinch` events; `window.visualViewport.scale` staying 1.0 is the probe that page-scale zoom stayed off.

## Releasing

Version lives in `airloom/__init__.py` and must match `CHANGELOG.md` and the AppStream release entry in `packaging/ai.stealthvision.Airloom.metainfo.xml`. Pushing a `vX.Y.Z` tag triggers the Flatpak workflow to build x86_64/aarch64 bundles and publish the GitHub release. See RELEASE.md.
