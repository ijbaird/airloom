# Releasing Airloom

GitHub Actions builds Flatpak bundles for x86_64 and aarch64 on every push and pull request. Each bundle is retained as a GitHub Actions artifact for 30 days.

To publish a release:

1. Update `airloom/__init__.py`, `CHANGELOG.md`, and the AppStream release entry to the same semantic version.
2. Run `make check` and, where possible, `./scripts/build-flatpak.sh`.
3. Commit the release and push a signed or annotated tag named `vX.Y.Z`.

The `Flatpak` workflow downloads both architecture artifacts, generates `SHA256SUMS`, creates the GitHub release with generated notes, and attaches the bundles and checksums.

The tag push also publishes the signed flatpak update repository to GitHub Pages (https://ijbaird.github.io/airloom/), which is how installed copies auto-update. If the Pages deploy fails or needs re-running, trigger the `Flatpak` workflow manually (workflow_dispatch) from the release tag — the publish job is idempotent. The repo signing key lives in the `FLATPAK_GPG_PRIVATE_KEY` Actions secret (backup: `~/.airloom-flatpak-signing` on the maintainer's machine).

Install a downloaded bundle with:

```bash
flatpak install --user ./Airloom-x86_64.flatpak
```

