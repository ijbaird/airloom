# Release and Quality Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make debug/test/install/release infrastructure safe, deterministic, verified, and well documented.

**Architecture:** Local scripts provide testable policy and verification logic consumed by GitHub Actions; publication is denied by default and client installation gates both release channels.

**Tech Stack:** Python stdlib, Bash, Flatpak, GitHub Actions, AppStream.

**Spec:** `docs/superpowers/specs/2026-10-28-sol-foundation-rearchitecture-design.md`

## Global Constraints

No secrets for untrusted PRs; no broad bot permission; no `--no-gpg-verify`; no test touching real config/cache/processes; CI-only checks must not be claimed as locally run.

## Review Focus

Regular-file socket paths, live sockets, unrelated running apps, custom DESTDIR/PREFIX, dirty versions, Dependabot/forks, branch dispatch, mismatched/older tags, and wrong signing keys must fail safely.

---

### Task 1: Debug endpoint and GUI-test isolation

**Files:** Modify `airloom/debugport.py`, `tests/test_debugport.py`, `scripts/debug-run`, `scripts/test-heatmap`, `scripts/test-save-refresh`; add focused script tests under `tests/`.

- [x] Add failing tests for regular-file preservation, live-socket refusal, stale-only replacement, inode-safe stop, non-string cmd errors, sentinel unrelated process/config/cache survival, and private XDG_CACHE_HOME.
- [x] Run focused Python/shell tests; reproduce destructive behavior in disposable fixtures only.
- [x] Implement socket lstat/type/live/stale/inode handling and direct non-unique child launch with isolated config/cache and child-only cleanup. Controller owns app-side stop wiring.
- [x] Re-run focused tests; expect PASS and sentinels unchanged.
- [x] Commit `fix: isolate debug endpoints and GUI regression tests`.

### Task 2: Installation and documentation correctness

**Files:** Modify `run-installed`, `Makefile`, `scripts/build-flatpak.sh`, `README.md`, `RELEASE.md`, `SECURITY.md`, `packaging/ai.stealthvision.Airloom.metainfo.xml`; add `tests/test_install.py` and release metadata checks.

- [x] Add failing staged-install tests for default, `/usr`, custom PREFIX/DESTDIR and outside-checkout resolution; dirty-version filename fixture; network/privacy documentation inventory; version-neutral install examples.
- [x] Run focused tests; confirm hardcoded `/usr/local` and dirty-version import.
- [x] Resolve prefix relative to installed launcher, read version from exported cwd, update privacy/AQI/alert/build docs, and add useful AppStream links (screenshots land in integration).
- [x] Run focused tests plus desktop/AppStream validation; expect PASS.
- [x] Commit `fix: make installation and operational documentation reliable`.

### Task 3: Complete CI and dependency automation

**Files:** Create `tests/test_workflows.py`; modify `.github/workflows/flatpak.yml`, `.github/workflows/claude-code-review.yml`, other Claude workflows/Dependabot config as needed, `Makefile` only to consume already-landed checks.

- [x] Add failing `tests/test_workflows.py` contract tests asserting `make check`, headless WebKit UI job, isolated env, no live API key, Dependabot/fork clean skip, and immutable action SHAs for the offered checkout/deploy-pages/Flatpak updates.
- [x] Run workflow tests against current YAML; confirm missing gates/actor failure.
- [x] Align CI, add Fedora/WebKit headless job for existing isolated UI scripts, safely skip credential review for untrusted actors, and adopt compatible action updates pinned by SHA.
- [x] Run workflow/shell/Python checks locally; expect PASS. Actual CI result remains pending until pushed.
- [x] Commit `ci: complete regression coverage and actor gating`.

### Task 4: Fail-closed release eligibility and provenance

**Files:** Create `scripts/check-release.py`, `scripts/record-provenance.sh`, `tests/test_release.py`; modify `.github/workflows/flatpak.yml`, `RELEASE.md`.

**Interfaces:** `check-release.py --ref REF --version-root PATH [--allow-older --confirm-tag TAG]`; validates semver, three version files, branch/tag context, and latest-tag rules.

- [x] Write failing tests for branch dispatch, malformed/mismatched versions, latest/current rerun, unconfirmed/wrongly confirmed older tag, confirmed older tag, and provenance required fields.
- [x] Run `python3 -m unittest tests.test_release -v`; expect missing script/current unsafe workflow failures.
- [x] Implement deny-by-default checks and workflow inputs; only valid tags sign/publish, older stable replacement needs exact typed confirmation. Record source SHA, image digest, runtime/tool refs/commits, and checksums.
- [x] Run focused/workflow tests; expect PASS.
- [x] Commit `ci: gate stable publication on release eligibility`.

### Task 5: Signed client installation and upgrade gates

**Files:** Create `scripts/verify-flatpak-release.sh`, `tests/test_release.py`; modify `.github/workflows/flatpak.yml`, `scripts/import-signing-key.sh`, `RELEASE.md`.

- [ ] Add failing static/dry-run tests asserting production public-key fingerprint match, ephemeral PR key, verified bundle origin/version, signed-repo install, previous-version upgrade, wrong-key rejection, and both release jobs depend on verification.
- [ ] Run focused tests; confirm missing client gate.
- [ ] Implement isolated client verification with no `--no-gpg-verify`, using ephemeral keys outside protected releases and production key matching on tags. Assemble test repos before publication and record explicit evidence.
- [ ] Run all locally runnable script/workflow tests; expect PASS. Push only after Sol approves; CI must execute real Flatpak gates.
- [ ] Commit `ci: verify signed Flatpak installation before publication`; request Sol release-workstream review.
