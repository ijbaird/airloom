# Integration and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate all approved workstreams, prove the complete product/release behavior, and publish Airloom v0.13.0.

**Architecture:** Integrate in dependency order, run deterministic cross-layer UI scenarios and Sol whole-branch review, then update synchronized release metadata and use the guarded CI publication path.

**Tech Stack:** Full Airloom stack, GitHub Actions, Flatpak, Git/GitHub CLI.

**Spec:** `docs/superpowers/specs/2026-10-28-sol-foundation-rearchitecture-design.md`

## Global Constraints

No unresolved Critical/Important Sol findings; no unsupported success claims; no tag before final checks and approval; release commit and all three version locations must match.

## Review Focus

Cross-workstream contract drift, real WebKit gesture/focus behavior, stale/demo presentation, installation upgrade from v0.12.2, and current-tag idempotent recovery require final evidence.

---

### Task 1: Integrate workstreams and complete cross-layer scenarios

**Files:** Modify only files needed to resolve reviewed contract drift; add `scripts/test-ui-regression` and `tests/test_integration.py`.

- [ ] Merge data → controller → frontend → release hardening, writing failing regression tests for every integration conflict before changing shared code.
- [ ] Run `make check`; investigate and fix failures by root cause with focused tests.
- [ ] Add/execute headless scenarios for away-from-home refresh, delayed search, interrupted fly/zoom/pinch, keyboard focus, cached filters, trend retry, demo/stale alerts/labels, reduced motion, preview actions, popup/search/narrow layouts, stripped debug module, and sentinel process/config/cache safety.
- [ ] Run complete local matrix and record exact results; require zero ResourceWarnings.
- [ ] Commit `test: complete cross-layer regression coverage`.

### Task 2: Sol whole-branch remediation review

**Files:** Review `ac83833577707f84e392b633fad8847321d308d8..integration HEAD`; modify only findings with regression tests.

- [ ] Dispatch Sol with spec, plan set, base/head SHAs, and verification evidence; request Critical/Important/Minor findings and readiness verdict.
- [ ] Write failing tests for each valid Critical/Important finding and fix it; assess/push back on incorrect feedback with technical evidence.
- [ ] Re-run all affected checks and request Sol confirmation that blockers are closed.
- [ ] Run `git diff --check` and full `make check`; expect PASS.
- [ ] Commit `fix: address final Sol review findings` if changes were needed.

### Task 3: Screenshots, changelog, and synchronized v0.13.0 metadata

**Files:** Add deterministic demo screenshot(s) under `packaging/`; modify AppStream, `airloom/__init__.py`, `CHANGELOG.md`, README if needed.

- [ ] Extend release tests to require `0.13.0`, today's release entry, screenshot URLs/captions, and matching versions; confirm they fail on 0.12.2 metadata.
- [ ] Generate representative screenshot(s) using isolated demo config/cache without live network or unrelated app processes; validate image files and AppStream metadata.
- [ ] Update all three version locations and summarize scientific, async, cache, accessibility, geocoder, and release changes accurately.
- [ ] Run release tests, metadata validators, `git diff --check`, and full `make check`; expect PASS.
- [ ] Commit `Release 0.13.0` and request final Sol release-diff verdict.

### Task 4: Publish and verify the release

**Files:** No planned source changes; any CI fix returns through test-first Task 2 review.

- [ ] Push `sol-foundation-v0-13-0` and confirm the complete PR/push CI matrix, including real Flatpak signed-client gates, is green before tagging.
- [ ] Fast-forward `main` to the reviewed release commit (or merge through the repository's normal PR path) and push `main`.
- [ ] Create and push annotated `v0.13.0` tag on the exact reviewed release commit; do not tag a later unreviewed SHA.
- [ ] Monitor Flatpak release and Pages workflows to completion; verify release assets, SHA256SUMS, provenance, both architectures, signed repo deployment, and published URLs.
- [ ] Report implemented themes, test evidence, Sol verdict, commit/tag, release/workflow URLs, and any behavior not directly exercised.
