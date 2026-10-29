#!/usr/bin/env bash
# Verify signed Airloom client installation and upgrade paths in disposable
# per-user Flatpak installations. The real Flatpak commands run in CI; local
# regression tests replace only that external command boundary.
set -euo pipefail

app_id="ai.stealthvision.Airloom"
expected_repo_url="${FLATPAK_REPO_URL:-https://ijbaird.github.io/airloom/repo}"
bundle=""
previous_bundle=""
repo=""
arch=""
version=""
previous_version=""

usage() {
  echo "usage: verify-flatpak-release.sh --bundle FILE --previous-bundle FILE --repo DIR --arch ARCH --version X.Y.Z --previous-version X.Y.Z" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle|--previous-bundle|--repo|--arch|--version|--previous-version)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      case "$1" in
        --bundle) bundle="$2" ;;
        --previous-bundle) previous_bundle="$2" ;;
        --repo) repo="$2" ;;
        --arch) arch="$2" ;;
        --version) version="$2" ;;
        --previous-version) previous_version="$2" ;;
      esac
      shift 2
      ;;
    *) usage; exit 2 ;;
  esac
done

[[ -n "${bundle}" && -n "${previous_bundle}" && -n "${repo}" && -n "${arch}" && -n "${version}" && -n "${previous_version}" ]] || { usage; exit 2; }
: "${FLATPAK_GPG_PUBLIC_KEY:?FLATPAK_GPG_PUBLIC_KEY is required}"
[[ -f "${bundle}" ]] || { echo "client verification denied: bundle not found: ${bundle}" >&2; exit 1; }
[[ -f "${previous_bundle}" ]] || { echo "client verification denied: previous bundle not found: ${previous_bundle}" >&2; exit 1; }
[[ -d "${repo}" ]] || { echo "client verification denied: repository not found: ${repo}" >&2; exit 1; }
[[ -s "${FLATPAK_GPG_PUBLIC_KEY}" ]] || { echo "client verification denied: public key not found" >&2; exit 1; }
[[ "${version}" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || { echo "client verification denied: invalid current version" >&2; exit 1; }
[[ "${previous_version}" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || { echo "client verification denied: invalid previous version" >&2; exit 1; }

bundle="$(realpath "${bundle}")"
previous_bundle="$(realpath "${previous_bundle}")"
repo="$(realpath "${repo}")"
public_key="$(realpath "${FLATPAK_GPG_PUBLIC_KEY}")"
repo_url="file://${repo}"
work="$(mktemp -d "${TMPDIR:-/tmp}/airloom-client-verify.XXXXXXXX")"
trap 'rm -rf "${work}"' EXIT
export HOME="${work}/home"
export XDG_CONFIG_HOME="${work}/config"
export XDG_CACHE_HOME="${work}/cache"
export XDG_DATA_HOME="${work}/data"
export LC_ALL=C
mkdir -p "${HOME}" "${XDG_CONFIG_HOME}" "${XDG_CACHE_HOME}" "${XDG_DATA_HOME}"

fail() {
  echo "client verification denied: $*" >&2
  exit 1
}

evidence() {
  echo "client verification: $*"
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    echo "$*" >> "${GITHUB_STEP_SUMMARY}"
  fi
}

installed_version() {
  flatpak info --user "${app_id}" | awk -F: '
    /^[[:space:]]*Version:/ && !found {
      value=$2
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      print value
      found=1
    }
    END { if (!found) exit 1 }
  '
}

installed_origin() {
  flatpak info --user --show-origin "${app_id}"
}

installed_commit() {
  flatpak info --user --show-commit "${app_id}"
}

remote_url() {
  local origin="$1"
  flatpak remotes --user --columns=name,url | awk -v origin="${origin}" '$1 == origin {print $2}'
}

assert_version() {
  local expected="$1"
  local label="$2"
  local actual
  actual="$(installed_version)" || fail "${label} version is unavailable"
  [[ "${actual}" == "${expected}" ]] || fail "${label} version ${actual} does not match expected ${expected}"
}

verify_bundle() (
  export FLATPAK_USER_DIR="${work}/bundle-client"
  mkdir -p "${FLATPAK_USER_DIR}"
  flatpak install --user --noninteractive --no-deps --arch="${arch}" --gpg-file="${public_key}" --bundle "${bundle}"
  local origin url
  origin="$(installed_origin)"
  [[ "${origin}" == "${app_id}-origin" ]] || fail "bundle origin ${origin} does not match ${app_id}-origin"
  url="$(remote_url "${origin}")"
  [[ "${url}" == "${expected_repo_url}" ]] || fail "bundle origin URL ${url} does not match ${expected_repo_url}"
  assert_version "${version}" "bundle"
  evidence "bundle_verified origin=${origin} url=${url} version=${version}"
)

verify_repository_install() (
  export FLATPAK_USER_DIR="${work}/repository-client"
  local remote="airloom-verify-current"
  mkdir -p "${FLATPAK_USER_DIR}"
  flatpak remote-add --user --gpg-import="${public_key}" "${remote}" "${repo_url}"
  flatpak install --user --noninteractive --no-deps --arch="${arch}" "${remote}" "${app_id}"
  [[ "$(installed_origin)" == "${remote}" ]] || fail "repository installation has wrong origin"
  assert_version "${version}" "repository installation"
  evidence "repository_install_verified origin=${remote} version=${version}"
)

verify_previous_upgrade() (
  export FLATPAK_USER_DIR="${work}/upgrade-client"
  mkdir -p "${FLATPAK_USER_DIR}"
  flatpak install --user --noninteractive --no-deps --arch="${arch}" --bundle "${previous_bundle}"
  assert_version "${previous_version}" "previous installation"
  local origin old_commit new_commit
  origin="$(installed_origin)"
  [[ "${origin}" == "${app_id}-origin" ]] || fail "previous installation origin ${origin} is unexpected"
  old_commit="$(installed_commit)"
  flatpak remote-modify --user --gpg-verify --gpg-import="${public_key}" --url="${repo_url}" "${origin}"
  flatpak update --user --noninteractive --no-deps --arch="${arch}" "${app_id}"
  assert_version "${version}" "upgrade"
  new_commit="$(installed_commit)"
  [[ -n "${new_commit}" && "${new_commit}" != "${old_commit}" ]] || fail "upgrade did not reach a new commit"
  evidence "upgrade_verified previous_version=${previous_version} version=${version} previous_commit=${old_commit} current_commit=${new_commit}"
)

verify_wrong_key_rejection() (
  export FLATPAK_USER_DIR="${work}/wrong-key-client"
  local wrong_home wrong_key remote output stage
  wrong_home="${work}/wrong-key-gpg"
  wrong_key="${work}/wrong-key.gpg"
  remote="airloom-verify-wrong-key"
  mkdir -p "${FLATPAK_USER_DIR}" "${wrong_home}"
  chmod 700 "${wrong_home}"
  GNUPGHOME="${wrong_home}" gpg --batch --pinentry-mode loopback --passphrase '' \
    --quick-generate-key "Airloom Wrong Key Verification <wrong-key@example.invalid>" ed25519 sign 1d
  GNUPGHOME="${wrong_home}" gpg --batch --export > "${wrong_key}"
  if output="$(flatpak remote-add --user --gpg-import="${wrong_key}" "${remote}" "${repo_url}" 2>&1)"; then
    if output="$(flatpak install --user --noninteractive --no-deps --arch="${arch}" "${remote}" "${app_id}" 2>&1)"; then
      fail "wrong signing key was accepted for repository installation"
    fi
    stage="install"
  else
    stage="remote-add"
  fi
  grep -Eiq 'gpg|signature|keyring|public key' <<< "${output}" || fail "wrong-key ${stage} failed for an unrelated reason: ${output}"
  evidence "wrong_key_rejected remote=${remote} stage=${stage}"
)

verify_bundle
verify_repository_install
verify_previous_upgrade
verify_wrong_key_rejection
evidence "client_verification_complete arch=${arch} version=${version}"
