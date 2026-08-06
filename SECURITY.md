# Security policy

## Supported versions

Security fixes are applied to the latest release.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature for this repository. Do not include PurpleAir API keys, precise private sensor locations, or other secrets in public issues.

Airloom stores its configuration in `$XDG_CONFIG_HOME/airloom/config.json` with mode `0600`. Reports involving local configuration should reproduce the issue with secrets removed.

