#!/usr/bin/env bash
# Create an isolated signing home for later workflow steps. Protected releases
# import the production secret and prove it matches the committed public key;
# ordinary CI generates a disposable key instead.
set -euo pipefail

test -n "${GITHUB_ENV:-}" || { echo "GITHUB_ENV is not set (not running under GitHub Actions?)" >&2; exit 1; }
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
public_key_file="${AIRLOOM_PUBLIC_KEY_FILE:-${project_dir}/packaging/airloom-flatpak.gpg}"
ephemeral="${AIRLOOM_EPHEMERAL_SIGNING_KEY:-0}"
production="${AIRLOOM_REQUIRE_PRODUCTION_KEY:-1}"
[[ "${ephemeral}" == "0" || "${ephemeral}" == "1" ]] || { echo "AIRLOOM_EPHEMERAL_SIGNING_KEY must be 0 or 1" >&2; exit 1; }
[[ "${production}" == "0" || "${production}" == "1" ]] || { echo "AIRLOOM_REQUIRE_PRODUCTION_KEY must be 0 or 1" >&2; exit 1; }
[[ "${ephemeral}" == "0" || "${production}" == "0" ]] || { echo "ephemeral and production signing modes are mutually exclusive" >&2; exit 1; }

keydir="$(mktemp -d "${TMPDIR:-/tmp}/airloom-signing.XXXXXXXX")"
chmod 700 "${keydir}"
if [[ "${ephemeral}" == "1" ]]; then
  GNUPGHOME="${keydir}" gpg --batch --pinentry-mode loopback --passphrase '' \
    --quick-generate-key "Airloom CI Ephemeral <ci@example.invalid>" ed25519 sign 1d
else
  test -n "${FLATPAK_GPG_PRIVATE_KEY:-}" || { echo "FLATPAK_GPG_PRIVATE_KEY secret is not set" >&2; exit 1; }
  printf '%s\n' "${FLATPAK_GPG_PRIVATE_KEY}" | GNUPGHOME="${keydir}" gpg --batch --import
fi

key_id="$(GNUPGHOME="${keydir}" gpg --batch --with-colons --list-secret-keys | awk -F: '/^fpr:/ {print $10; exit}')"
test -n "${key_id}" || { echo "no secret key found after import" >&2; exit 1; }
if [[ "${production}" == "1" ]]; then
  expected_id="$(GNUPGHOME="${keydir}" gpg --batch --with-colons --show-keys "${public_key_file}" | awk -F: '/^fpr:/ {print $10; exit}')"
  test -n "${expected_id}" || { echo "no fingerprint found in committed public key" >&2; exit 1; }
  [[ "${key_id}" == "${expected_id}" ]] || {
    echo "imported signing key fingerprint ${key_id} does not match committed public key fingerprint ${expected_id}" >&2
    exit 1
  }
fi
exported_public_key="${keydir}/airloom-flatpak.gpg"
GNUPGHOME="${keydir}" gpg --batch --export "${key_id}" > "${exported_public_key}"
{
  echo "GNUPGHOME=${keydir}"
  echo "GPG_KEY_ID=${key_id}"
  echo "FLATPAK_GPG_PUBLIC_KEY=${exported_public_key}"
} >> "${GITHUB_ENV}"
