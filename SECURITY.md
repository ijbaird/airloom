# Security policy

## Supported versions

Security fixes are applied to the latest release.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature for this repository. Do not include PurpleAir API keys, precise private sensor locations, or other secrets in public issues.

Airloom stores its configuration with mode `0600`. For a native install it is `$XDG_CONFIG_HOME/airloom/config.json` (or `~/.config/airloom/config.json`); in Flatpak it is normally `~/.var/app/ai.stealthvision.Airloom/config/airloom/config.json`. The configuration can contain the PurpleAir API key, home coordinates, favorites, and alert state.

The sensor cache is `$XDG_CACHE_HOME/airloom/cache.db` (or `~/.cache/airloom/cache.db`) for a native install and normally `~/.var/app/ai.stealthvision.Airloom/cache/airloom/cache.db` in Flatpak. Cached readings and locations can also be sensitive. Reproduce reports with secrets and precise private locations removed, and attach only sanitized configuration, cache excerpts, and network URLs. See [Data and privacy](README.md#data-and-privacy) for the external-service inventory.

