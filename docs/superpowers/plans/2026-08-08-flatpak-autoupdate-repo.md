# Flatpak Auto-Update Repository Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish every release to a GPG-signed flatpak (OSTree) repository on GitHub Pages so installed flatpaks auto-update, and wire release bundles to that repo as their origin.

**Architecture:** The existing `Flatpak` workflow gains (a) an explicit bundle step that stamps `--repo-url` + the repo GPG key into each bundle, and (b) a `publish-repo` job that merges the per-arch OSTree repos, signs them, generates client files, and deploys the site statelessly to GitHub Pages via `actions/upload-pages-artifact`/`deploy-pages`. Site assembly lives in a standalone `scripts/publish-flatpak-repo.sh` so it can be smoke-tested locally without CI.

**Tech Stack:** GitHub Actions (existing GNOME-50 container), `ostree`/`flatpak` CLIs, `gpg`, bash. **No new project dependencies.**

Spec: `docs/superpowers/specs/2026-08-08-flatpak-autoupdate-repo-design.md` (read it first).

## Global Constraints

- Site URL is exactly `https://ijbaird.github.io/airloom`; the OSTree repo lives at `https://ijbaird.github.io/airloom/repo`.
- App id `ai.stealthvision.Airloom`, branch `stable`, runtime repo `https://dl.flathub.org/repo/flathub.flatpakrepo`.
- Secret name exactly `FLATPAK_GPG_PRIVATE_KEY`; committed public key exactly `packaging/airloom-flatpak.gpg` (binary export).
- The private key must NEVER enter the git repo, the worktree, or any file under the project directory.
- Publishing is stateless: each deploy contains only the current release's commits.
- All GitHub Actions are SHA-pinned with a `# vX.Y.Z` comment (repo convention; see existing `flatpak.yml`).
- The publish job runs only on `v*` tags and `workflow_dispatch`.
- `make check` must remain green (no app-code changes in this plan).
- Work happens in a worktree branch, not on `main`.

---

### Task 1: Signing key, repo secret, public key, GitHub Pages enablement

**Files:**
- Create: `packaging/airloom-flatpak.gpg` (binary public key — the ONLY key material that enters the repo)
- Create (outside the repo): `~/.airloom-flatpak-signing/` (GNUPGHOME, private key export, README)

**Interfaces:**
- Produces: repo secret `FLATPAK_GPG_PRIVATE_KEY` (armored private key, no passphrase); `packaging/airloom-flatpak.gpg` consumed by Task 2's script (`PUBKEY_FILE` default) and Task 3's bundle step; GitHub Pages enabled with `build_type: workflow` so Task 3's deploy job can publish.

- [ ] **Step 1: Generate the key** (dedicated GNUPGHOME, never the default keyring):

```bash
signing_home="$HOME/.airloom-flatpak-signing"
mkdir -p "$signing_home" && chmod 700 "$signing_home"
cat > "$signing_home/keygen.conf" <<'EOF'
%no-protection
Key-Type: eddsa
Key-Curve: ed25519
Key-Usage: sign
Name-Real: Airloom Flatpak Repository
Name-Email: ijbaird@ijbaird.me
Expire-Date: 0
%commit
EOF
GNUPGHOME="$signing_home" gpg --batch --generate-key "$signing_home/keygen.conf"
GNUPGHOME="$signing_home" gpg --batch --with-colons --list-secret-keys | awk -F: '/^fpr:/ {print $10; exit}'
```

Expected: the last command prints a 40-char fingerprint.

- [ ] **Step 2: Export both halves; set the secret**

```bash
GNUPGHOME="$signing_home" gpg --armor --export-secret-keys > "$signing_home/airloom-flatpak-private.asc"
chmod 600 "$signing_home/airloom-flatpak-private.asc"
GNUPGHOME="$signing_home" gpg --export > packaging/airloom-flatpak.gpg   # run from the worktree root
gh secret set FLATPAK_GPG_PRIVATE_KEY < "$signing_home/airloom-flatpak-private.asc"
cat > "$signing_home/README" <<'EOF'
Airloom flatpak repository signing key (generated 2026-08-08).
- This directory is the GNUPGHOME holding the ONLY copy of the private key
  outside the GitHub Actions secret FLATPAK_GPG_PRIVATE_KEY.
- BACK THIS DIRECTORY UP somewhere safe. Losing it means publishing under a
  new key and every user re-adding the airloom remote.
- Public key is committed as packaging/airloom-flatpak.gpg.
EOF
```

- [ ] **Step 3: Verify the secret and the public key**

Run: `gh secret list | grep FLATPAK_GPG_PRIVATE_KEY && gpg --show-keys packaging/airloom-flatpak.gpg`
Expected: secret listed; public key shows `Airloom Flatpak Repository <ijbaird@ijbaird.me>`, ed25519, no expiry.

- [ ] **Step 4: Enable GitHub Pages (Actions deployment)**

```bash
gh api -X POST repos/ijbaird/airloom/pages -f build_type=workflow \
  || gh api -X PUT repos/ijbaird/airloom/pages -f build_type=workflow
gh api repos/ijbaird/airloom/pages --jq '{build_type, html_url}'
```

Expected: `build_type: "workflow"`, `html_url: "https://ijbaird.github.io/airloom/"`.

- [ ] **Step 5: Commit** (public key only — verify with `git status` that nothing else is staged and the private key path is outside the repo)

```bash
git add packaging/airloom-flatpak.gpg
git commit -m "Add flatpak repository public signing key"
```

---

### Task 2: `scripts/publish-flatpak-repo.sh` + local smoke test

**Files:**
- Create: `scripts/publish-flatpak-repo.sh` (executable)

**Interfaces:**
- Consumes: `packaging/airloom-flatpak.gpg` (Task 1); env `GPG_KEY_ID`, `GNUPGHOME` (required), `SITE_URL` (default `https://ijbaird.github.io/airloom`), `PUBKEY_FILE` (default `<repo>/packaging/airloom-flatpak.gpg`).
- Produces: `publish-flatpak-repo.sh OUT_DIR SRC_REPO [SRC_REPO...]` — assembles `OUT_DIR/{repo,airloom.flatpakrepo,airloom.flatpakref,index.html}`. Task 3's workflow calls it exactly as `scripts/publish-flatpak-repo.sh _site repos/airloom-repo-x86_64 repos/airloom-repo-aarch64`.

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# Assemble the GitHub Pages site for Airloom's flatpak update repository:
# merge one or more per-arch OSTree repos into one signed archive-mode repo
# and generate the .flatpakrepo/.flatpakref/index.html client files.
#
# Usage: publish-flatpak-repo.sh OUT_DIR SRC_REPO [SRC_REPO...]
# Env:   GPG_KEY_ID (required), GNUPGHOME (required, holds the private key),
#        SITE_URL (default https://ijbaird.github.io/airloom),
#        PUBKEY_FILE (default <repo>/packaging/airloom-flatpak.gpg)
set -euo pipefail

out="${1:?usage: publish-flatpak-repo.sh OUT_DIR SRC_REPO...}"; shift
[[ $# -ge 1 ]] || { echo "at least one source repo is required" >&2; exit 2; }
: "${GPG_KEY_ID:?GPG_KEY_ID is required}"
: "${GNUPGHOME:?GNUPGHOME is required}"
site_url="${SITE_URL:-https://ijbaird.github.io/airloom}"
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
pubkey_file="${PUBKEY_FILE:-${project_dir}/packaging/airloom-flatpak.gpg}"
app_id="ai.stealthvision.Airloom"

mkdir -p "${out}"
repo="${out}/repo"
ostree init --repo="${repo}" --mode=archive

for src in "$@"; do
  # Publish app refs only: flatpak-builder's export can also carry
  # .Debug/.Sources refs that would bloat the repo for no client benefit.
  while IFS= read -r ref; do
    ostree pull-local --repo="${repo}" "${src}" "${ref}"
  done < <(ostree refs --repo="${src}" | grep '^app/' | grep -v -e '\.Debug/' -e '\.Sources/')
done

[[ -n "$(ostree refs --repo="${repo}")" ]] || { echo "no app refs were pulled — refusing to publish an empty repo" >&2; exit 1; }

while IFS= read -r ref; do
  commit="$(ostree rev-parse --repo="${repo}" "${ref}")"
  ostree gpg-sign --repo="${repo}" --gpg-homedir="${GNUPGHOME}" "${commit}" "${GPG_KEY_ID}"
done < <(ostree refs --repo="${repo}")

flatpak build-update-repo \
  --gpg-sign="${GPG_KEY_ID}" \
  --gpg-homedir="${GNUPGHOME}" \
  --generate-static-deltas \
  "${repo}"

gpg_base64="$(base64 -w0 < "${pubkey_file}")"

cat > "${out}/airloom.flatpakrepo" <<EOF
[Flatpak Repo]
Title=Airloom
Url=${site_url}/repo
Homepage=https://github.com/ijbaird/airloom
Comment=Hyperlocal PurpleAir air quality for GNOME
GPGKey=${gpg_base64}
EOF

cat > "${out}/airloom.flatpakref" <<EOF
[Flatpak Ref]
Name=${app_id}
Branch=stable
Title=Airloom
Url=${site_url}/repo
SuggestRemoteName=airloom
Homepage=https://github.com/ijbaird/airloom
IsRuntime=false
RuntimeRepo=https://dl.flathub.org/repo/flathub.flatpakrepo
GPGKey=${gpg_base64}
EOF

cat > "${out}/index.html" <<EOF
<!doctype html>
<meta charset="utf-8">
<title>Airloom flatpak repository</title>
<h1>Airloom</h1>
<p>Hyperlocal PurpleAir air quality for GNOME. Install with:</p>
<pre>flatpak install --user ${site_url}/airloom.flatpakref</pre>
<p>Installed this way, updates arrive automatically via GNOME Software or
<code>flatpak update</code>. Project home:
<a href="https://github.com/ijbaird/airloom">github.com/ijbaird/airloom</a></p>
EOF

echo "Site assembled at ${out} ($(ostree refs --repo="${repo}" | tr '\n' ' '))"
```

`chmod +x scripts/publish-flatpak-repo.sh` and `bash -n scripts/publish-flatpak-repo.sh` (also run `shellcheck` if installed).

- [ ] **Step 2: Local smoke test with a synthetic source repo** (host has `ostree`, `flatpak`, `gpg`; uses the real signing key from Task 1)

```bash
work="$(mktemp -d)"
mkdir -p "$work/tree/files" "$work/tree/export"
echo demo > "$work/tree/files/demo.txt"
printf '[Application]\nname=ai.stealthvision.Airloom\nruntime=org.gnome.Platform/x86_64/50\ncommand=airloom\n' > "$work/tree/metadata"
ostree init --repo="$work/src" --mode=archive
ostree commit --repo="$work/src" --branch=app/ai.stealthvision.Airloom/x86_64/stable \
  --add-metadata-string=xa.metadata="$(cat "$work/tree/metadata")" --tree=dir="$work/tree"
ostree commit --repo="$work/src" --branch=app/ai.stealthvision.Airloom/x86_64/stable.Debug --tree=dir="$work/tree" || true

GNUPGHOME="$HOME/.airloom-flatpak-signing" \
GPG_KEY_ID="$(GNUPGHOME="$HOME/.airloom-flatpak-signing" gpg --batch --with-colons --list-secret-keys | awk -F: '/^fpr:/ {print $10; exit}')" \
  scripts/publish-flatpak-repo.sh "$work/site" "$work/src"
```

Expected assertions (run each):
- `ostree refs --repo="$work/site/repo"` → exactly `app/ai.stealthvision.Airloom/x86_64/stable` (the `.Debug`-suffixed ref is a stable-branch sibling, not filtered by the `.Debug/` pattern — if it appears, the filter in the script is wrong; fix the grep to `-e '\.Debug' -e '\.Sources'` and re-run).
- `ostree show --repo="$work/site/repo" --gpg-homedir="$HOME/.airloom-flatpak-signing" app/ai.stealthvision.Airloom/x86_64/stable` → prints a `Found 1 signature` / good-signature block.
- `grep -c GPGKey "$work/site"/airloom.flatpakrepo "$work/site"/airloom.flatpakref` → 1 each; `test -s "$work/site/index.html"`.
- `flatpak remote-ls --user "$work/site/repo"` is NOT expected to work over `file://` without adding a remote — skip; the CI e2e (Task 5) covers client behavior.

- [ ] **Step 3: Commit**

```bash
git add scripts/publish-flatpak-repo.sh
git commit -m "Add site-assembly script for the flatpak update repository"
```

---

### Task 3: Workflow — bundle origin wiring + publish-repo job

**Files:**
- Modify: `.github/workflows/flatpak.yml`

**Interfaces:**
- Consumes: `packaging/airloom-flatpak.gpg` (Task 1), `scripts/publish-flatpak-repo.sh` (Task 2), secret `FLATPAK_GPG_PRIVATE_KEY` (Task 1).
- Produces: per-arch artifacts `airloom-repo-<arch>` (tag/dispatch builds); bundles whose embedded origin is `https://ijbaird.github.io/airloom/repo`; a `publish-repo` job deploying to Pages.

Background you need: the pinned `flatpak-builder` action exports the build into a local OSTree repo dir whose `repo-dir` input defaults to `repo` (verified in the action's `dist/index.js`: `getInput('repo-dir') || 'repo'`). The action also builds a bundle, which our new step rebuilds with origin flags (same filename, overwriting).

- [ ] **Step 1: Resolve SHA pins for the two new actions**

```bash
gh api 'repos/actions/upload-pages-artifact/tags?per_page=10' --jq '.[] | "\(.name) \(.commit.sha)"'
gh api 'repos/actions/deploy-pages/tags?per_page=10' --jq '.[] | "\(.name) \(.commit.sha)"'
```

Pick the newest stable `vX.Y.Z` of each (not a moving major tag) and use `uses: actions/<name>@<sha> # vX.Y.Z` below.

- [ ] **Step 2: Edit the `flatpak` job** — after the `Build Flatpak bundle` action step, add:

```yaml
      - name: Rebuild bundle with update-repo origin
        run: |
          flatpak build-bundle repo "Airloom-${{ matrix.arch }}.flatpak" \
            ai.stealthvision.Airloom stable \
            --arch="${{ matrix.arch }}" \
            --repo-url="https://ijbaird.github.io/airloom/repo" \
            --runtime-repo="https://dl.flathub.org/repo/flathub.flatpakrepo" \
            --gpg-keys="packaging/airloom-flatpak.gpg"
```

and after the existing `Upload workflow artifact` step, add (reusing the same pinned `actions/upload-artifact` SHA already in the file):

```yaml
      - name: Upload update-repo artifact
        if: startsWith(github.ref, 'refs/tags/v') || github.event_name == 'workflow_dispatch'
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
        with:
          name: airloom-repo-${{ matrix.arch }}
          path: repo
          if-no-files-found: error
          include-hidden-files: true
          retention-days: 7
```

- [ ] **Step 3: Add the `publish-repo` job** at the end of the file (checkout/download pins are the same SHAs already used in this workflow; Pages action pins from Step 1):

```yaml
  publish-repo:
    name: Publish update repository
    if: startsWith(github.ref, 'refs/tags/v') || github.event_name == 'workflow_dispatch'
    needs: flatpak
    runs-on: ubuntu-24.04
    container:
      image: ghcr.io/flathub-infra/flatpak-github-actions:gnome-50
      options: --privileged
    permissions:
      contents: read
      pages: write
      id-token: write
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1
        with:
          pattern: airloom-repo-*
          path: repos
      - name: Import signing key
        env:
          FLATPAK_GPG_PRIVATE_KEY: ${{ secrets.FLATPAK_GPG_PRIVATE_KEY }}
        run: |
          test -n "${FLATPAK_GPG_PRIVATE_KEY}" || { echo "FLATPAK_GPG_PRIVATE_KEY secret is not set" >&2; exit 1; }
          keydir="$(mktemp -d)"
          chmod 700 "${keydir}"
          printf '%s' "${FLATPAK_GPG_PRIVATE_KEY}" | GNUPGHOME="${keydir}" gpg --batch --import
          key_id="$(GNUPGHOME="${keydir}" gpg --batch --with-colons --list-secret-keys | awk -F: '/^fpr:/ {print $10; exit}')"
          test -n "${key_id}" || { echo "no secret key found after import" >&2; exit 1; }
          echo "GNUPGHOME=${keydir}" >> "${GITHUB_ENV}"
          echo "GPG_KEY_ID=${key_id}" >> "${GITHUB_ENV}"
      - name: Assemble site
        run: scripts/publish-flatpak-repo.sh _site repos/airloom-repo-x86_64 repos/airloom-repo-aarch64
      - name: Upload Pages artifact
        uses: actions/upload-pages-artifact@SHA_FROM_STEP_1 # vX.Y.Z
        with:
          path: _site
      - name: Deploy to GitHub Pages
        id: deployment
        uses: actions/deploy-pages@SHA_FROM_STEP_1 # vX.Y.Z
```

(`SHA_FROM_STEP_1` is filled with the real SHAs you resolved — never commit the literal placeholder.)

- [ ] **Step 4: Validate**

Run: `python3 -c "import yaml, sys; yaml.safe_load(open('.github/workflows/flatpak.yml')); print('yaml ok')"` — if PyYAML is missing on the host, run `actionlint` if available; if neither exists, state so in your report (the PR build itself is the binding validation, since this workflow runs on pull_request).
Also run `make check` (must stay green — nothing app-side changed).

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/flatpak.yml
git commit -m "Publish a signed flatpak update repo to GitHub Pages on release"
```

---

### Task 4: Documentation

**Files:**
- Modify: `README.md` (installation section — read it first; add the repo install as the primary method)
- Modify: `RELEASE.md`

**Interfaces:**
- Consumes: the site URL and client-file names from earlier tasks. Nothing consumes this task.

- [ ] **Step 1: README** — in the installation section, add (adapting to the file's existing tone/structure; keep the bundle instructions as the secondary/offline path):

```markdown
### Install (recommended — auto-updating)

    flatpak install --user https://ijbaird.github.io/airloom/airloom.flatpakref

Updates then arrive automatically through GNOME Software, or manually with
`flatpak update`.

Already installed from a downloaded bundle? Reinstall once from any current
bundle (`flatpak install --user --reinstall ./Airloom-x86_64.flatpak`) or add
the remote directly (`flatpak remote-add --user airloom
https://ijbaird.github.io/airloom/airloom.flatpakrepo`) — after that you're
on automatic updates too.
```

- [ ] **Step 2: RELEASE.md** — append to the workflow description paragraph:

```markdown
The tag push also publishes the signed flatpak update repository to GitHub
Pages (https://ijbaird.github.io/airloom/), which is how installed copies
auto-update. If the Pages deploy fails or needs re-running, trigger the
`Flatpak` workflow manually (workflow_dispatch) from the release tag — the
publish job is idempotent. The repo signing key lives in the
`FLATPAK_GPG_PRIVATE_KEY` Actions secret (backup: `~/.airloom-flatpak-signing`
on the maintainer's machine).
```

- [ ] **Step 3: Commit**

```bash
git add README.md RELEASE.md
git commit -m "Document the auto-updating flatpak repository"
```

---

### Task 5: End-to-end verification (post-merge, on the maintainer's machine)

**Files:** none (runbook task; any failure loops back to Tasks 2–3 as fixes on a new branch or the same PR).

**Interfaces:**
- Consumes: everything above, merged to `main` (the PR build must already be green — it exercises the new bundle step on both arches).

- [ ] **Step 1: Dispatch the workflow on main and watch it**

```bash
gh workflow run flatpak.yml --ref main
sleep 10 && gh run list --workflow=flatpak.yml --limit 1
gh run watch <run-id> --exit-status
```

Expected: test, both flatpak arch builds, and `publish-repo` all succeed; the run page shows a `github-pages` deployment. (If `deploy-pages` fails with an environment-protection error, the `github-pages` environment is restricted to `main` — confirm the dispatch ran on `main`; branch runs are not supported for deploys.)

- [ ] **Step 2: Verify the published site**

```bash
curl -fsSL https://ijbaird.github.io/airloom/airloom.flatpakrepo | head -5
curl -fsSL -o /dev/null -w '%{http_code}\n' https://ijbaird.github.io/airloom/repo/summary
```

Expected: `[Flatpak Repo]` header with `Url=https://ijbaird.github.io/airloom/repo`; summary returns 200. (First deploy can take a minute to go live.)

- [ ] **Step 3: Verify as a client (this migrates the local install to the remote — intended)**

```bash
flatpak remote-add --user --if-not-exists airloom https://ijbaird.github.io/airloom/airloom.flatpakrepo
flatpak remote-ls --user airloom
flatpak install --user -y --reinstall airloom ai.stealthvision.Airloom
flatpak info --user ai.stealthvision.Airloom | grep -E 'Origin|Version'
flatpak update --user --appstream airloom && flatpak update --user -y
```

Expected: `remote-ls` lists `ai.stealthvision.Airloom` (x86_64 at least; aarch64 visible with `--all --arch=aarch64`); GPG verification is on (remote-add would fail loudly on a bad signature); origin is `airloom`; the app launches (`flatpak run ai.stealthvision.Airloom`, close it after). Note: the installed build now comes from the dispatch-published main build of the current version — the next real release updates it normally.

- [ ] **Step 4: Verify bundle origin wiring**

```bash
gh run download <run-id> -n airloom-x86_64 -D /tmp/airloom-bundle-test
flatpak install --user -y --reinstall /tmp/airloom-bundle-test/Airloom-x86_64.flatpak
flatpak info --user ai.stealthvision.Airloom | grep Origin
flatpak remotes --user --columns=name,url | grep airloom
```

Expected: installing the bundle creates/uses an origin remote pointing at `https://ijbaird.github.io/airloom/repo` (flatpak names it `airloom-origin` unless the `airloom` remote with the same URL already exists — either is a pass as long as the URL matches).

- [ ] **Step 5: Record the outcome** in the PR/branch conversation (versions seen, origin URLs). Full auto-update proof arrives with the next real release.
