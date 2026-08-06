# Contributing

Thanks for helping improve Airloom.

1. Fork the repository and create a focused branch.
2. Run `make check` before opening a pull request.
3. For packaging changes, run `./scripts/build-flatpak.sh` on Fedora or let the Flatpak GitHub Actions job validate the manifest.
4. Keep changes small, explain user impact, and include tests for data or AQI behavior.

Bug reports should include the Fedora version, display session (Wayland or X11), whether demo or live PurpleAir data was used, and any terminal output that does not contain an API key.

