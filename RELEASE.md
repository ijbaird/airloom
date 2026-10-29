# Releasing Airloom

GitHub Actions builds Flatpak bundles for x86_64 and aarch64 on every push and pull request. Each bundle is retained as a GitHub Actions artifact for 30 days.

To publish a release:

1. Update `airloom/__init__.py`, `CHANGELOG.md`, and the AppStream release entry to the same semantic version.
2. Run `make check`. When the validators are installed, also run `desktop-file-validate packaging/ai.stealthvision.Airloom.desktop` and `appstreamcli validate --no-net packaging/ai.stealthvision.Airloom.metainfo.xml`.
3. Commit the release so the intended version and source are present in `HEAD`.
4. Where possible, run `./scripts/build-flatpak.sh`. The script tests and builds a pristine export of committed `HEAD`; uncommitted files and edits are excluded, and the bundle filename takes its version from that export rather than the dirty checkout.
5. Push a signed or annotated tag named `vX.Y.Z`. The workflow fetches the complete tag history and denies malformed tags, unknown tags, branch/manual dispatch, and any mismatch between the tag and the three version files before a signing key is imported or a stable repository is archived.

The `Flatpak` workflow downloads both architecture artifacts, generates `SHA256SUMS`, creates the GitHub release with generated notes, and attaches the bundles, checksums, and per-architecture `*.provenance.json` files. Ordinary branch pushes and pull requests still produce unsigned development artifacts, but cannot sign or publish stable releases.

Before either publication job, the `verify-client` matrix uses isolated per-user Flatpak installations to install each signed bundle with GPG verification enabled, assert its origin and version, assemble and install from a signed multi-architecture test repository, upgrade a previous supported release, and require a wrong signing key to be rejected. Evidence is recorded in the job summary. Ordinary CI generates an ephemeral key and signs only disposable verification fixtures; its uploaded development artifacts remain unsigned. Protected releases re-import the production key and fail unless its fingerprint matches the committed `packaging/airloom-flatpak.gpg`. These are real CI gates: the local dry-run tests do not replace them.

The tag push also publishes the signed flatpak update repository to GitHub Pages (https://ijbaird.github.io/airloom/), which is how installed copies auto-update. If the Pages deploy fails or needs re-running, trigger the `Flatpak` workflow manually (`workflow_dispatch`) from the current release tag — the publish jobs are idempotent. Replacing an older stable release additionally requires selecting that tag, setting `allow_older` to true, and typing its exact `vX.Y.Z` tag into `confirm_tag`; either safeguard alone is rejected.

Each provenance file records the source commit, the digest-pinned container image, resolved GNOME runtime and SDK refs/commits, Flatpak tool versions, immutable action refs, and the bundle's SHA-256 checksum. These resolved inputs provide an audit trail, not a promise of bit-for-bit reproducibility: rebuilding from them can still vary with mutable external repositories, timestamps, and build tools.

The repo signing key lives in the `FLATPAK_GPG_PRIVATE_KEY` Actions secret; the canonical copy is the 1Password item "Airloom Flatpak Signing Key" (personal vault), which can re-set the secret via `op document get "Airloom Flatpak Signing Key" | gh secret set FLATPAK_GPG_PRIVATE_KEY`. Track the key's expiry and extend it before expiration, refreshing both the canonical copy and Actions secret. Rotation or compromise requires a reviewed migration: deployed clients trust the committed `packaging/airloom-flatpak.gpg`, so merely replacing the secret does not establish trust. Coordinate the public key, newly signed bundles/repository, and client migration, retain the canonical old key until that migration is complete, and rebuild from known-good source after a compromise.

Install a downloaded bundle by replacing `VERSION` and `ARCH` with the values in its filename (`ARCH` is normally `x86_64` or `aarch64`):

```bash
flatpak install --user ./Airloom-VERSION-ARCH.flatpak
```

