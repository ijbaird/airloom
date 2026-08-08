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
  # Capture and check refs outside of a process substitution: a failing
  # producer inside `< <(...)` does NOT trip set -e/pipefail in the
  # enclosing script, so a bad path/empty/corrupt source would otherwise
  # silently contribute zero refs with no error.
  if ! src_refs="$(ostree refs --repo="${src}")"; then
    echo "failed to list refs in source repo: ${src}" >&2
    exit 1
  fi

  # Publish app refs only: flatpak-builder's export can also carry
  # .Debug/.Sources refs that would bloat the repo for no client benefit.
  app_refs="$(printf '%s\n' "${src_refs}" | grep '^app/' | grep -v -e '\.Debug' -e '\.Sources' || true)"
  [[ -n "${app_refs}" ]] || { echo "source repo contributed no app refs: ${src}" >&2; exit 1; }

  while IFS= read -r ref; do
    ostree pull-local --repo="${repo}" "${src}" "${ref}"
  done <<< "${app_refs}"
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
