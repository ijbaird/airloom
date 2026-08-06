#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
build_dir="${AIRLOOM_BUILD_DIR:-${project_dir}/build-dir}"
repo_dir="${AIRLOOM_REPO_DIR:-${project_dir}/flatpak-repo}"
dist_dir="${AIRLOOM_DIST_DIR:-${project_dir}/dist}"
manifest="${project_dir}/packaging/ai.stealthvision.Airloom.yml"
app_id="ai.stealthvision.Airloom"
remote="${FLATPAK_REMOTE:-flathub}"
version="$(PYTHONPATH="${project_dir}" python3 -c 'from airloom import __version__; print(__version__)')"
arch="$(flatpak --default-arch)"
bundle="${dist_dir}/Airloom-${version}-${arch}.flatpak"

if [[ -n "${FLATPAK_BUILDER:-}" ]]; then
  builder_command=("${FLATPAK_BUILDER}")
elif command -v flatpak-builder >/dev/null 2>&1; then
  builder_command=(flatpak-builder)
elif flatpak info --user org.flatpak.Builder >/dev/null 2>&1; then
  builder_command=(flatpak run org.flatpak.Builder)
else
  echo "flatpak-builder is required. On Fedora: sudo dnf install flatpak-builder" >&2
  exit 1
fi

mkdir -p "${dist_dir}"
python3 -m unittest discover -s "${project_dir}/tests" -v

"${builder_command[@]}" \
  --user \
  --force-clean \
  --install-deps-from="${remote}" \
  --default-branch=stable \
  --repo="${repo_dir}" \
  "${build_dir}" \
  "${manifest}"

flatpak build-bundle \
  "${repo_dir}" \
  "${bundle}" \
  "${app_id}" \
  stable \
  --runtime-repo="https://flathub.org/repo/flathub.flatpakrepo"

sha256sum "${bundle}" > "${bundle}.sha256"
echo "Built ${bundle}"
