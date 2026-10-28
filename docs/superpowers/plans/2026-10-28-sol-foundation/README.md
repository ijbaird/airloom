# Sol Foundation Rearchitecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement every requirement in the approved Sol foundation design and publish the verified result as Airloom v0.13.0.

**Architecture:** Land dependency-free geography, request, and presentation contracts first. Then execute data, controller, frontend, and release-hardening plans in isolated worktrees with disjoint ownership before integrating and reviewing the complete branch.

**Tech Stack:** Python stdlib unittest, system PyGObject GTK4/libadwaita/WebKitGTK 6, handwritten JavaScript/CSS, Node built-in test runner, SQLite, Bash, Flatpak, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-28-sol-foundation-rearchitecture-design.md`

## Global Constraints

- No pip, npm, hosted-backend, map-library, or other third-party runtime dependencies.
- GTK/WebKit only on the main loop; workers use `GLib.idle_add` for delivery.
- Preserve user config/favorites; derived cache schema changes may clear cached readings.
- No live PurpleAir requests in tests; no public-Nominatim autocomplete.
- Every task is test-first, runs its focused verification, and commits before review.
- Sol reviews every workstream and the final base-to-release diff; Critical/Important findings block progress.

## Review Focus

- Out-of-order async completion: newer user intent wins; pinned by controller Task 1/3 and frontend Task 2/3 tests.
- Antimeridian, extreme but finite coordinates, and capped views: continuous map/fetch behavior with explicit coverage; pinned by foundation Task 1, data Task 4, and frontend Task 1 tests.
- Locked/corrupt storage, symlinks, ENOSPC, and PermissionError: no destructive recovery or false success; pinned by data Tasks 2/3 and controller Task 4 tests.
- Hostile numeric and bridge values: unavailable/error instead of crash or AQI 0; pinned by data Task 1 and release-hardening Task 1 tests.
- Wrong release ref/version/key or an older tag: no unintended stable publication; pinned by release-hardening Tasks 3–5 tests.

## Plan Dependency Graph

1. Execute `01-foundation.md` on `sol-foundation-v0-13-0`; review and commit its contracts.
2. Create separate worktrees from that foundation commit for:
   - `02-data.md` — branch `sol-data-v0-13-0`.
   - `03-controller.md` — branch `sol-controller-v0-13-0` (may start after data interfaces land; coordinate integration order).
   - `04-frontend.md` — branch `sol-frontend-v0-13-0`.
   - `05-release-hardening.md` — branch `sol-release-v0-13-0`.
3. Merge data → controller → frontend → release hardening into `sol-foundation-v0-13-0`, running each plan's focused checks and Sol review first.
4. Execute `06-integration-release.md` only after all four workstreams integrate green.

## Ownership Boundaries

- Data owns `aqi.py`, `purpleair.py`, `cache.py`, `store.py`, `geocode.py`, `demo.py` and their Python tests.
- Controller owns `controller.py`, `app.py`, `location.py`, controller/app/location tests; it consumes data interfaces but does not edit data-owned files.
- Frontend owns `resources/app.js`, `app.css`, `index.html`, `state.js`, and JS tests; foundation owns `resources/core.js` until integrated.
- Release hardening owns `debugport.py`, `scripts/`, `Makefile`, `.github/`, `packaging/`, launchers, README/release/security docs and their tests.
- Only integration tasks edit `__init__.py`, `CHANGELOG.md`, and final AppStream release metadata after release-hardening review.

## Execution Reporting

For each task record: failing-test evidence, passing focused tests, commit SHA, Sol verdict, and unresolved Minor items. Never claim local Flatpak verification when only CI ran it. Final completion requires the release workflow and Pages deployment to finish successfully.
