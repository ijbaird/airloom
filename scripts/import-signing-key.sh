#!/usr/bin/env bash
# Import the Flatpak signing key from $FLATPAK_GPG_PRIVATE_KEY into a fresh
# GNUPGHOME and export GNUPGHOME/GPG_KEY_ID to $GITHUB_ENV for later steps.
set -euo pipefail

test -n "${FLATPAK_GPG_PRIVATE_KEY:-}" || { echo "FLATPAK_GPG_PRIVATE_KEY secret is not set" >&2; exit 1; }
test -n "${GITHUB_ENV:-}" || { echo "GITHUB_ENV is not set (not running under GitHub Actions?)" >&2; exit 1; }

keydir="$(mktemp -d)"
chmod 700 "${keydir}"
printf '%s\n' "${FLATPAK_GPG_PRIVATE_KEY}" | GNUPGHOME="${keydir}" gpg --batch --import
key_id="$(GNUPGHOME="${keydir}" gpg --batch --with-colons --list-secret-keys | awk -F: '/^fpr:/ {print $10; exit}')"
test -n "${key_id}" || { echo "no secret key found after import" >&2; exit 1; }
echo "GNUPGHOME=${keydir}" >> "${GITHUB_ENV}"
echo "GPG_KEY_ID=${key_id}" >> "${GITHUB_ENV}"
