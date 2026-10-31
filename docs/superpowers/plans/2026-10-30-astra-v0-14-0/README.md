# Airloom 0.14.0 Astra-Fix Implementation Plan Set

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement each plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 21 Important findings and the listed Minors from the GPT-6 Astra whole-codebase review of v0.13.0, and publish the verified result as Airloom 0.14.0.

**Architecture:**
- Four workstreams run in parallel worktrees branched from `astra-v0-14-0`, each with disjoint file ownership. The design's shared-file rules cover the few overlaps.
- The workstreams merge W1 → W2 → W3 → W4, then go through integrated verification.
- The original Astra reviewers re-verify their own findings, then the release task runs.

**Tech Stack:** Python stdlib `unittest`, system PyGObject (GTK4/libadwaita/WebKitGTK 6), handwritten JS/CSS (Node built-in test runner), SQLite, Bash, Flatpak, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-30-astra-review-v0-14-0-design.md` (approved by the user 2026-10-30).

**Review reports (evidence):** `/tmp/opencode/astra-review/{data-core,controller-shell,frontend,release-ci}.md`.

## Global Constraints

- No pip, npm, hosted-backend, map-library, or other third-party runtime dependencies. No build step. Direct-file browser preview keeps working.
- GTK/WebKit only on the main loop; workers deliver via `GLib.idle_add`. No new blocking I/O on the main loop.
- No live PurpleAir, Nominatim, or OSM requests in tests.
- `make check PYTHON="python3 -W error::ResourceWarning"` passes. JS runs on Node 22. The hosted Python 3.12 test job runs without PyGObject.
- Every task is test-first: the regression fails on `f1f811d` through the real failing path, then passes. Commit per task.
- The six EPA band background colors, the unavailable gray, and the accent color are unchanged.
- Provenance badges mark only degraded readings: Demo, Stale, Old (>2 h).
- Sol reviews every workstream. Critical/Important findings block merge.

## Plan dependency graph

| Plan | Branch / worktree | Starts | Merges |
|---|---|---|---|
| `01-data.md` | `astra-data-v0-14-0` | immediately | 1st |
| `02-controller.md` | `astra-controller-v0-14-0` | immediately | 2nd |
| `03-frontend.md` | `astra-frontend-v0-14-0` | immediately | 3rd |
| `04-release.md` | `astra-release-v0-14-0` | immediately | 4th |
| `05-integration-release.md` | `astra-v0-14-0` | after all four merge | — |

- **Merge timing:** W1 merges into `astra-v0-14-0` as soon as Sol approves it, without waiting for the others. W2's Task 8 (`SettingsDurabilityError` handling) first merges `astra-v0-14-0` into its branch to get W1's code.
- **Cross-workstream contracts** (each plan's closing section) are reconciled at integration with a failing test first, never by guessing.
- **Freshness rule:** W1 publishes it in `airloom/models.py`: `MAX_OBSERVATION_AGE = 7200`, `FUTURE_SKEW_TOLERANCE = 600`, and `is_fresh_observation(last_seen, now)`, fresh when −600 ≤ age ≤ 7200 inclusive and a missing `last_seen` is not fresh.
  - W2 alert eligibility consumes it.
  - W3's "Old" badge consumes the JS mirror `AirloomCore.isFreshObservation`.
- **Durability error:** W1's `store.SettingsDurabilityError` (raised after a successful rename) is handled by W2 as a committed save with a warning.
- **Shell recovery:** W2 owns the shell-recovery handshake lines in `app.js`.
- **Band colors:** W3 owns the band `foreground` values in `aqi.py` and `core.js`.
