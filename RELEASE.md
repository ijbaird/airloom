# Releasing Airloom

GitHub Actions builds Flatpak bundles for x86_64 and aarch64 on every push and pull request. Each bundle is retained as a GitHub Actions artifact for 30 days.

To publish a release:

1. Update `airloom/__init__.py`, `CHANGELOG.md`, and the AppStream release entry to the same semantic version and date. Point every AppStream screenshot URL at the new tag (`https://raw.githubusercontent.com/ijbaird/airloom/vX.Y.Z/packaging/screenshots/...`); if the interface changed, regenerate the images first with `python3 scripts/make-screenshots`, which runs its own private headless mutter and session bus and shows deterministic demo data.
2. Run `make check`. When the validators are installed, also run `desktop-file-validate packaging/ai.stealthvision.Airloom.desktop` and `appstreamcli validate --no-net packaging/ai.stealthvision.Airloom.metainfo.xml`.
3. Commit the release so the intended version and source are present in `HEAD`. Where possible, run `./scripts/build-flatpak.sh`; it tests and builds a pristine export of committed `HEAD`, so uncommitted files and edits are excluded, and the bundle filename takes its version from that export rather than the dirty checkout.
4. Open a pull request (or push to `main`) and wait for the full `Flatpak` workflow to pass: `test`, `webkit-ui`, both `flatpak` architectures, and the `verify-client` signed-install checks. The required `webkit-ui` job runs `scripts/test-heatmap`, `scripts/test-save-refresh`, and the cross-layer `scripts/test-ui-regression` (including its stripped-debug, search, focus, and alert scenarios) in a `fedora:44` container under Xvfb, the last as an unprivileged user on a private session bus (its save-failure checks refuse to run as root), so any failing UI scenario blocks publication.
5. Merge, then wait for the `Flatpak` run on `main` to go green for the merge commit.
6. Create an annotated (or signed) tag named `vX.Y.Z` on that exact commit and push only the tag:

   ```bash
   git fetch origin main
   git tag -a vX.Y.Z -m "Airloom X.Y.Z" <green-main-commit-sha>
   git push origin vX.Y.Z
   ```

   The workflow fetches the complete tag history and denies malformed tags, unknown tags, branch/manual dispatch, and any mismatch between the tag and the three version files before a signing key is imported or a stable repository is archived.

Publication is queued: the tag's run waits behind any earlier `Flatpak` publication still in progress rather than cancelling it, and when its turn comes it revalidates release eligibility and the captured tag/source commit before publishing anything.

The `Flatpak` workflow downloads both architecture artifacts, generates `SHA256SUMS`, creates the GitHub release with generated notes, and attaches the bundles, checksums, and per-architecture `*.provenance.json` files. Ordinary branch pushes and pull requests still produce unsigned development artifacts, but cannot sign or publish stable releases.

Before either publication job, the `verify-client` matrix uses isolated per-user Flatpak installations to install each signed bundle with GPG verification enabled, assert its origin and version, assemble and install from a signed multi-architecture test repository, upgrade a previous supported release, and require a wrong signing key to be rejected. Evidence is recorded in the job summary. Ordinary CI generates an ephemeral key and signs only disposable verification fixtures; its uploaded development artifacts remain unsigned. Protected releases re-import the production key and fail unless its fingerprint matches the committed `packaging/airloom-flatpak.gpg`. These are real CI gates: the local dry-run tests do not replace them.

The tag push also publishes the signed flatpak update repository to GitHub Pages (https://ijbaird.github.io/airloom/), which is how installed copies auto-update. One synchronous publication coordinator holds the only cross-tag publication slot while the GitHub release and Pages jobs run sequentially with separately scoped permissions. Both channels revalidate eligibility and the captured tag/source commit immediately before publication, and reject artifacts whose provenance or bundle checksum does not match that source. If the Pages deploy fails or needs re-running, trigger the `Flatpak` workflow manually (`workflow_dispatch`) from the current release tag — the publish jobs are idempotent. Replacing an older stable release additionally requires selecting that tag, setting `allow_older` to true, and typing its exact `vX.Y.Z` tag into `confirm_tag`; either safeguard alone is rejected.

Each provenance file records the source commit, the digest-pinned container image, resolved GNOME runtime and SDK refs/commits, Flatpak tool versions, immutable action refs, and the bundle's SHA-256 checksum. These resolved inputs provide an audit trail, not a promise of bit-for-bit reproducibility: rebuilding from them can still vary with mutable external repositories, timestamps, and build tools.

The repo signing key lives in the `FLATPAK_GPG_PRIVATE_KEY` Actions secret; the canonical copy is the 1Password item "Airloom Flatpak Signing Key" (personal vault), which can re-set the secret via `op document get "Airloom Flatpak Signing Key" | gh secret set FLATPAK_GPG_PRIVATE_KEY`. Track the key's expiry and extend it before expiration, refreshing both the canonical copy and Actions secret. Rotation or compromise requires a reviewed migration: deployed clients trust the committed `packaging/airloom-flatpak.gpg`, so merely replacing the secret does not establish trust. Coordinate the public key, newly signed bundles/repository, and client migration, retain the canonical old key until that migration is complete, and rebuild from known-good source after a compromise.

Install a downloaded bundle by replacing `VERSION` and `ARCH` with the values in its filename (`ARCH` is normally `x86_64` or `aarch64`):

```bash
flatpak install --user ./Airloom-VERSION-ARCH.flatpak
```

