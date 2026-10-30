# Airloom

[![Flatpak](https://github.com/ijbaird/airloom/actions/workflows/flatpak.yml/badge.svg)](https://github.com/ijbaird/airloom/actions/workflows/flatpak.yml)
[![Latest release](https://img.shields.io/github/v/release/ijbaird/airloom)](https://github.com/ijbaird/airloom/releases/latest)

Airloom is a fast, focused air-quality app built for the GNOME desktop, developed and tested on Fedora. It turns hyperlocal PurpleAir readings into an interactive map, clear trends, practical health guidance, and local alerts. The Flatpak runs on any Linux distribution with the GNOME runtime.

Airloom includes:

- an interactive OpenStreetMap view with color-coded sensor markers;
- a sensor browser, search, favorites, responsive detail view, and rolling-average chart;
- US EPA 2024 PM2.5 AQI breakpoints and the EPA's corrections for PurpleAir data (see [AQI and alerts](#aqi-and-alerts));
- demo, cached, and stale readings labeled next to the AQI, and a "Limited coverage" note when the view extends past fetched data;
- temperature, humidity, PM2.5, and PM10 readings;
- local desktop notifications when a favorite crosses a chosen AQI threshold;
- a no-account demo mode and live mode with your own PurpleAir read key;
- keyboard navigation, screen-reader labels and announcements, an Interface size preference (100%, 125%, or 150%), and reduced motion when GNOME's animations are turned off;
- light/dark GNOME styling and desktop/Flatpak metadata.

## Run on Fedora

Airloom intentionally uses the GTK, libadwaita, WebKitGTK, and PyGObject packages shipped with Fedora instead of vendoring a second browser runtime.

```bash
sudo dnf install python3-gobject gtk4 libadwaita webkitgtk6.0 libnotify
./run
```

It launches in demo mode. Open Preferences, enter your [PurpleAir API read key](https://develop.purpleair.com/), and choose automatic location or a fixed home place to load live public sensors. Place searches (in Preferences and the map's search box) are sent only when you press Enter.

## Test

```bash
make check
```

## Install from a source checkout

```bash
sudo make install
airloom
```

The default prefix is `/usr/local`. Distribution packages can select `/usr` and stage files without baking the staging directory into the launcher:

```bash
make install PREFIX=/usr DESTDIR="$pkgdir"
```

The installed launcher finds `share/airloom` relative to its own `bin` directory. To remove a native install, run `sudo make uninstall` with the same `PREFIX` and `DESTDIR` values used for installation.

## Flatpak

### Install (recommended — auto-updating)

    flatpak install --user https://ijbaird.github.io/airloom/airloom.flatpakref

Updates then arrive automatically through GNOME Software, or manually with
`flatpak update`.

Already installed from a downloaded bundle? Reinstall once from any current
bundle (`flatpak install --user --reinstall ./Airloom-VERSION-ARCH.flatpak`,
substituting the filename values) or add
the remote and reinstall from it (`flatpak remote-add --user airloom
https://ijbaird.github.io/airloom/airloom.flatpakrepo && flatpak install
--user --reinstall airloom ai.stealthvision.Airloom`) — either way rebinds
the app's origin to the remote, so after that you're on automatic updates
too.

### Build a Flatpak from source

The manifest at `packaging/ai.stealthvision.Airloom.yml` targets the GNOME 50 runtime used by Fedora 44. Install the matching runtime and SDK, then build from the repository root:

```bash
./scripts/build-flatpak.sh
```

Flatpak may need network access to fetch the runtime, SDK, and dependencies; this is not an offline build recipe. Airloom itself adds no pip or npm dependencies. The script tests and builds a pristine export of committed `HEAD`, so uncommitted files and edits cannot enter the bundle or change its version. Commit the intended source before building a release.

The bundle is written to `dist/Airloom-<version>-<architecture>.flatpak` with a matching SHA-256 file. Replace `VERSION` and `ARCH` with the values in the generated filename and install it with:

```bash
flatpak install --user ./dist/Airloom-VERSION-ARCH.flatpak
```

GitHub Actions builds x86_64 and aarch64 bundles for every pull request and push. Tagged versions (`vX.Y.Z`) automatically become GitHub releases containing both bundles and `SHA256SUMS`, and also publish the auto-update Flatpak repository above to GitHub Pages; see [RELEASE.md](RELEASE.md).

## AQI and alerts

Airloom reports sensor-based PM2.5 AQI estimates using the US EPA 2024 breakpoints, not regulatory AirNow NowCast values. Valid concentrations above the public scale are capped at AQI 500. Its correction model has two explicit paths:

- Current PurpleAir CF=1 readings and indoor running averages use Barkjohn et al.'s final CF1 equations: linear below raw 570, blended from 570–611, and quadratic from 611.
- Outdoor running averages use the published five-piece CF=ATM correction used by PurpleAir and AirNow. Applying current humidity to historical averages is an approximation.

Desktop alerts operate only while Airloom is running; there is no background monitoring daemon. An alert requires a favorite to cross the configured threshold with a non-demo observation no more than two hours old. Expired observations and demo results never notify or change real alert state, and hidden or low-confidence sensors follow the same filtering rules as the display.

## Data and privacy

Demo readings are generated locally and labeled as demo data, but **demo mode is not offline mode**. Airloom can make the following network requests in either demo or live mode:

- **OpenStreetMap tiles:** `tile.openstreetmap.org` receives the tile coordinates needed to draw the map.
- **Nominatim search and reverse geocoding:** explicitly submitted place searches send the completed query; location and viewed-area labels send coordinates. Typing a partial query only filters local sensors and is not sent remotely. Public Nominatim is the default; set `AIRLOOM_NOMINATIM_URL` to a compatible base URL to use another endpoint.
- **GeoClue:** automatic-home mode asks the system's GeoClue service for a location fix. Any network access used by GeoClue is controlled by the operating system and its location settings. Choosing a fixed home disables these automatic GeoClue requests, but does not disable map tiles, explicit place searches, or reverse geocoding.

With a PurpleAir read key, `api.purpleair.com` also receives the key, requested map bounds, favorite sensor IDs, and selected sensor IDs used for rolling-average comparisons. The key is not sent to OpenStreetMap or Nominatim. Without a key, Airloom makes no PurpleAir API requests. External providers also receive normal connection metadata such as the client's IP address and are covered by their own privacy policies. Cached sensor readings can be shown when the network is unavailable, but live updates, searches, and uncached map tiles cannot.

The API key, preferences, home coordinates, favorites, and alert state are stored locally with mode `0600` in `$XDG_CONFIG_HOME/airloom/config.json` (or `~/.config/airloom/config.json`). Flatpak normally stores this at `~/.var/app/ai.stealthvision.Airloom/config/airloom/config.json`. Sensor data is cached in `$XDG_CACHE_HOME/airloom/cache.db` (or `~/.cache/airloom/cache.db`), normally `~/.var/app/ai.stealthvision.Airloom/cache/airloom/cache.db` in Flatpak.

Airloom is an independent project and is not affiliated with PurpleAir, OpenStreetMap, or the US EPA.

## License

MIT. Map data © OpenStreetMap contributors. PurpleAir data is subject to PurpleAir's terms.
