# Airloom

[![Flatpak](https://github.com/ijbaird/airloom/actions/workflows/flatpak.yml/badge.svg)](https://github.com/ijbaird/airloom/actions/workflows/flatpak.yml)
[![Latest release](https://img.shields.io/github/v/release/ijbaird/airloom)](https://github.com/ijbaird/airloom/releases/latest)

Airloom is a fast, focused air-quality app built for the GNOME desktop, developed and tested on Fedora. It turns hyperlocal PurpleAir readings into an interactive map, clear trends, practical health guidance, and local alerts. The Flatpak runs on any Linux distribution with the GNOME runtime.

The 0.1 MVP includes:

- an interactive OpenStreetMap view with color-coded sensor markers;
- a sensor browser, search, favorites, responsive detail view, and rolling-average chart;
- US EPA 2024 PM2.5 AQI breakpoints and PurpleAir's EPA correction;
- temperature, humidity, PM2.5, and PM10 readings;
- local desktop notifications when a favorite crosses a chosen AQI threshold;
- a no-account demo mode and live mode with your own PurpleAir read key;
- light/dark GNOME styling and desktop/Flatpak metadata.

## Run on Fedora

Airloom intentionally uses the GTK, libadwaita, WebKitGTK, and PyGObject packages shipped with Fedora instead of vendoring a second browser runtime.

```bash
sudo dnf install python3-gobject gtk4 libadwaita webkitgtk6.0 libnotify
./run
```

It launches in demo mode. Open Preferences, enter your [PurpleAir API read key](https://develop.purpleair.com/), and set the map center coordinates to load live public outdoor sensors.

## Test

```bash
make check
```

## Install for the current system

```bash
sudo make install
airloom
```

The default prefix is `/usr/local`; package builds can set `PREFIX` and `DESTDIR` normally. To uninstall the manually installed files, run `sudo make uninstall`.

## Flatpak

### Install (recommended — auto-updating)

    flatpak install --user https://ijbaird.github.io/airloom/airloom.flatpakref

Updates then arrive automatically through GNOME Software, or manually with
`flatpak update`.

Already installed from a downloaded bundle? Reinstall once from any current
bundle (`flatpak install --user --reinstall ./Airloom-x86_64.flatpak`) or add
the remote and reinstall from it (`flatpak remote-add --user airloom
https://ijbaird.github.io/airloom/airloom.flatpakrepo && flatpak install
--user --reinstall airloom ai.stealthvision.Airloom`) — either way rebinds
the app's origin to the remote, so after that you're on automatic updates
too.

### Build from source (offline)

The manifest at `packaging/ai.stealthvision.Airloom.yml` targets the GNOME 50 runtime used by Fedora 44. Build it from the repository root after installing the matching runtime and SDK:

```bash
./scripts/build-flatpak.sh
```

The bundle is written to `dist/Airloom-<version>-<architecture>.flatpak` with a matching SHA-256 file. Install it with:

```bash
flatpak install --user ./dist/Airloom-0.1.0-x86_64.flatpak
```

GitHub Actions builds x86_64 and aarch64 bundles for every pull request and push. Tagged versions (`vX.Y.Z`) automatically become GitHub releases containing both bundles and `SHA256SUMS`, and also publish the auto-update Flatpak repository above to GitHub Pages; see [RELEASE.md](RELEASE.md).

## Data and privacy

- Without a key, every reading is deterministic demo data and is labeled as such.
- With a key, the current map bounds are sent to PurpleAir and OpenStreetMap supplies map tiles.
- The API key, preferences, favorites, and alert state are stored locally in `$XDG_CONFIG_HOME/airloom/config.json` (or `~/.config/airloom/config.json`) with mode `0600`.
- Airloom is an independent project and is not affiliated with PurpleAir, OpenStreetMap, or the US EPA.

## License

MIT. Map data © OpenStreetMap contributors. PurpleAir data is subject to PurpleAir's terms.
