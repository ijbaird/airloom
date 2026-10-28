# Releasing Airloom

GitHub Actions builds Flatpak bundles for x86_64 and aarch64 on every push and pull request. Each bundle is retained as a GitHub Actions artifact for 30 days.

To publish a release:

1. Update `airloom/__init__.py`, `CHANGELOG.md`, and the AppStream release entry to the same semantic version.
2. Run `make check`. When the validators are installed, also run `desktop-file-validate packaging/ai.stealthvision.Airloom.desktop` and `appstreamcli validate --no-net packaging/ai.stealthvision.Airloom.metainfo.xml`.
3. Commit the release so the intended version and source are present in `HEAD`.
4. Where possible, run `./scripts/build-flatpak.sh`. The script tests and builds a pristine export of committed `HEAD`; uncommitted files and edits are excluded, and the bundle filename takes its version from that export rather than the dirty checkout.
5. Push a signed or annotated tag named `vX.Y.Z`.

The `Flatpak` workflow downloads both architecture artifacts, generates `SHA256SUMS`, creates the GitHub release with generated notes, and attaches the bundles and checksums.

The tag push also publishes the signed flatpak update repository to GitHub Pages (https://ijbaird.github.io/airloom/), which is how installed copies auto-update. If the Pages deploy fails or needs re-running, trigger the `Flatpak` workflow manually (workflow_dispatch) from the release tag — the publish job is idempotent. The repo signing key lives in the `FLATPAK_GPG_PRIVATE_KEY` Actions secret; the canonical copy is the 1Password item "Airloom Flatpak Signing Key" (personal vault), which can re-set the secret via `op document get "Airloom Flatpak Signing Key" | gh secret set FLATPAK_GPG_PRIVATE_KEY`.

Install a downloaded bundle by replacing `VERSION` and `ARCH` with the values in its filename (`ARCH` is normally `x86_64` or `aarch64`):

```bash
flatpak install --user ./Airloom-VERSION-ARCH.flatpak
```

