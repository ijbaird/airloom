# Airloom 0.14.0: fix the GPT-6 Astra whole-codebase review

Date: 2026-10-30. Base: `main` at `f1f811d` (v0.13.0 plus test-only flake fixes). Integration branch: `astra-v0-14-0`.

## Intent

After v0.13.0 shipped, four independent GPT-6 Astra reviewers read the whole codebase, each owning one area: data core, controller and shell, frontend, and release/CI. Their full reports, with reproductions and file:line evidence, are in `/tmp/opencode/astra-review/{data-core,controller-shell,frontend,release-ci}.md`.

After controller verification, the calibrated result is **0 Critical, 21 Important, 10 Minor**:
- One reported Critical, a `claude.yml` bot bypass, was refuted: the pinned action's tag mode runs `checkHumanActor` before Claude.
- The other Critical (finding 1) was recalibrated to Important: it requires a malformed or changed provider response.

The user asked to **fix all 21 Important findings plus the cheap Minors, in one release**.

Why it matters: several findings touch health information (stale or malformed data presented as fresh, lost or duplicated alerts), PurpleAir billing, release trust for existing installs, and accessibility for low-vision and keyboard users. The release notes already promise these properties.

## Goals

- Every Important finding below is fixed test-first. Each fix gets a regression that fails on `f1f811d` through the real failing path, not just a helper, and passes after.
- The listed Minors are fixed the same way.
- Ship as **Airloom 0.14.0** through the established gates: hosted PR matrix, fast-forward `main`, green `main` run, annotated tag, verified publication.
- Release notes accurately describe what changed. No claim exceeds what the code does.

## Non-goals

- Previously deferred items not listed here: flatpak image ≥1.18.3, pinning `fedora:44` and its dnf packages, the `DebugPort` FUSE inode reuse, verify-client download retries, release-before-Pages ordering, `publish-flatpak-repo.sh` trailing-blank tolerance.
- A fixed-sleep audit beyond the scenarios named below.
- New features beyond what a finding requires.
- Changing the EPA AQI band colors, breakpoints, or correction equations; the reviewer verified they're correct.
- Exactly-once notification delivery across process crashes; libnotify can't guarantee it.

## Anti-goals (failure even if tests pass)

- Any change that lets demo, cached, stale, expired, or malformed data look like a fresh live reading, or become alert-eligible.
- Any new path that spends PurpleAir credits without a key the user currently has configured, or that multiplies billed requests.
- Weakening an existing assertion or gate to make a fix pass.
- Changing what color a given AQI is shown in.
- New third-party runtime dependencies, a build step, or breaking direct-file browser preview.
- Releasing with a production signing gate that trusts a key existing users don't already trust.

## Constraints (unchanged from 0.13)

- Python stdlib plus system PyGObject only. Vanilla JS/CSS/HTML with no bundler. Helpers stay UMD/namespaced so direct-file preview works.
- GTK and WebKit calls only on the main loop. Workers deliver via `GLib.idle_add`. New blocking I/O must not run on the main loop.
- No live PurpleAir, Nominatim, or OSM calls in tests. Fixtures and the existing debug-only seams only.
- `make check` passes with ResourceWarnings as errors. JS tests run on Node 22. The hosted Python 3.12 job runs without PyGObject.
- Release gates, provenance, Turnstyle queue, and signing flow stay as in 0.13 except where finding 19 changes them.

## Approaches considered

- **A. Four parallel workstreams (chosen by the user).**
  - Data, controller/shell, frontend, and release/scripts, each in its own worktree branched from `astra-v0-14-0`.
  - Opus 5.5 implements; Sol reviews each workstream; the four Astra reviewers re-verify their own findings on the integrated branch before tagging.
  - Fastest. File ownership is mostly disjoint, and the few shared files have one assigned owner (below).
- **B. One branch, one implementer, in sequence.** No merge coordination, but roughly 3–4× slower with one long review chain.
- **C. Two waves in one release** (health/billing/trust first, then accessibility/UX). Adds an interim checkpoint but no extra release; not chosen.

## Decisions made by the user

1. Fix all 21 Important findings plus the cheap Minors, in one release.
2. **Contrast: keep the current colors.** The six EPA band background colors (`#35b779`, `#f6c945`, `#f39c3d`, `#e65b65`, `#9b6bc3`, `#7e394d`), the unavailable gray, and the accent color all stay exactly as they are. Contrast is fixed by what's drawn on them. The detail card's big AQI number moves onto a band-colored badge, and every text-on-color foreground is chosen per color to pass contrast. Controller interpretation, played back to the user: "keep the colors" means the color values, not the bare-colored-number layout.
3. Approach A.
4. **Provenance badges mark only degraded readings.**
   - Badges: **Demo** (synthetic data); **Stale** (cache fallback after a failed refresh, the existing `stale` flag); **Old** (the sensor's last observation is more than 2 hours old, the same rule that already makes a reading alert-ineligible).
   - Readings served from the normal in-interval cache, or fetched live, get no badge.
   - The summary and detail card follow the same rule, so the current "Cached data" label for ordinary in-interval cache delivery goes away and every surface agrees.
   - This narrows the 0.13 changelog wording ("Demo, cached, and stale cached readings are labeled"); the 0.14.0 notes must state the new rule.
5. **Failed tag recovery: delete and re-push.**
   - If a gate fails in the `v0.14.0` tag run and nothing has been published (no GitHub release, no Pages deployment), fix it through the normal PR path, delete the remote `v0.14.0` tag, and re-tag the fixed, green `main` commit.
   - If anything has been published, ship 0.14.1 instead and never move the tag.
   - RELEASE.md documents this rule.

## Calls made by the controller

- **Version 0.14.0**, not 0.13.1. It adds keyboard map panning and accessible trend values, and changes preview and signing-gate behavior.
- **Reviewers:** Sol reviews each workstream, including fix rounds. The original Astra reviewer of each area does a scoped re-verification of its own findings on the integrated branch. Any Critical/Important still open blocks the release.
- **Signing-gate policy (finding 19):** production client verification must not import the candidate key into the previous installation. A future signing-key rotation will therefore fail the gate until a reviewed client migration exists. This is intended and should be stated in RELEASE.md.
- **Include the deferred fixed-home search-field styling** (from the 0.13 review). It's trivial, it sits in the Preferences screenshot being regenerated, and it's frontend-owned.
- Regenerate all three screenshots for 0.14.0, since the detail card's appearance changes.

## Requirements by workstream

Each item lists the finding number, its location, and what must be true afterwards. The reports' "Fix" sections are guidance; where they offer options, the choice is the builder's unless stated here.

### W1: Data core (`purpleair.py`, `cache.py`, `store.py`, `geocode.py`, `demo.py`, `models.py`, `geography.py`, `validation.py`, `location.py`, and the data alert-freshness guard in `presentation.py`)

1. **Short or malformed rows** (`purpleair.py:165`, `cache.py:221`).
   - Before any authoritative cache commit, validate the response against the requested fields: each row's length equals `len(fields)`, and every requested measurement field is present in `fields`.
   - A response that fails validation is rejected as a whole: no rows committed, checkpoint not advanced, previous cache intact.
   - Never attach a new `last_seen` to a retained old measurement.
   - Add a parser→cache→alert regression plus a malformed-full-snapshot preservation regression.
2. **Booleans** (`purpleair.py:236`, `_integer` at `:249`).
   - JSON booleans are invalid for every numeric field: measurements, humidity, temperature, coordinates, confidence, `last_seen`, and integer metadata.
   - Measurements become unavailable. Humidity falls back to the documented neutral assumption. Invalid coordinates drop the row, as other invalid coordinates do.
   - Cover current values, trend windows, coordinates, and integer metadata, through both direct and cached paths.
3. **Future timestamps** (`purpleair.py:199`, `cache.py:230`, `presentation.py:62`).
   - Observation and checkpoint times later than now plus a **10-minute skew tolerance** (controller's call) are rejected. A future observation is treated as missing; a future response `time_stamp` isn't used as a delta checkpoint.
   - At the alert boundary, a negative age beyond the tolerance is never alert-eligible.
   - Use an injected clock. Test milliseconds-as-seconds values and future checkpoints.
7. **Offline favorites invalidating coverage** (`cache.py:354`, reached via `fetch_favorites` at `:526` and `upsert_rows` at `:179`).
   - A newly fetched observation that is already past the prune age must not delete unrelated area coverage. Keep conservative invalidation for genuine expiry of covered rows.
   - The user-visible behavior of an offline favorite must not regress.
   - Test several area+favorites refresh cycles and assert the delta-polling request shape.
9. **Rollback on every mutating transaction** (`cache.py:371`, `:390`). `store_trend`, `clear`, and any other mutating transaction roll back on exception, like `store_fetch`. Test a real read-lock-induced COMMIT failure, then reads, another writer, and a successful retry.
13. **Settings durability** (`store.py:142`).
   - After `os.replace`, fsync the parent directory. Also fsync it when the config directory is first created.
   - If the directory fsync fails after a successful rename, the new settings are already visible and stay applied (no rollback). The failure is reported as a durability warning through the existing error/toast path. Controller's call.
   - Add ordering and fault-injection tests.
- **Minor: demo origin** (`demo.py:54`). Generated sensors carry `origin='demo'`; add a provenance assertion to `test_demo.py`.
- **Minor: reverse geocoding cache and coalescing** (`geocode.py:86`). Use a bounded cache and coalesce in-flight identical requests, keyed by endpoint plus the rounded query actually sent. Never cache transient failures as labels.

### W2: Controller and shell (`controller.py`, `requests.py`, the non-color parts of `presentation.py`, `app.py`, `bridge.py`, `debugport.py`, `__main__.py`, and the `app.js` shell-recovery handshake only)

4. **Alert delivery** (`controller.py:544`, `app.py:1438`).
   - Separate the observed threshold state from the delivery state.
   - Notification submission reports success or failure to the controller. A failed delivery stays pending and is retried on later evaluations, at most once per evaluation cycle, until delivered or until the crossing resets.
   - When persistence fails, an in-session delivered ledger prevents re-notifying the same crossing.
   - Test both directions: delivery fails then recovers, and persistence fails across repeated evaluations.
8. **API-key revocation** (`controller.py:597`, `:769`; `app.py:1364`).
   - Trend authorization includes the credential/live-mode generation. A successful key change or clear revokes queued and in-flight trend tokens.
   - The worker rechecks authority immediately before paid I/O, and the completion path rechecks before publishing or caching.
   - Test: clear the key while a worker is queued, then let it run; no request is made.
10. **No main-loop SQLite** (`app.py:1342`, `:1406`, `:1256`).
    - Trend cache reads and writes, and the startup cache paint read, move to tracked workers with bounded admission. Results return through GLib with token and reading checks.
    - Shutdown waits for these workers before the cache closes.
    - Test with a real held lock: the main loop is never blocked longer than a small bound (the builder picks the bound and the measurement method).
11. **Manual Refresh intent** (`controller.py:309`, `:333`).
    - Automatic refreshes and duplicate same-target viewport reports never downgrade or cancel an unsatisfied forced refresh.
    - A covered-view optimization must not cancel a same-target manual refresh. Force stays sticky until a forced fetch for that target completes.
    - Add permutation tests: manual → auto, manual → identical covered view, manual → genuinely new target.
12. **Shell recovery viewport** (`app.py:791`, `:1013`; `app.js:89` receive path).
    - After `reload-shell`, the reloaded page and the controller agree on the viewport before any refresh. The controller's current view is restored into the page; it is not reset to home.
    - Outstanding page-origin requests from the old shell can't apply to the new one.
    - W2 owns the minimal `app.js` change for this handshake; W3 must not edit that handler.
    - Test: crash and reload while away from home.
- **Minor: strict settings payload validation** (`controller.py:760`, `:783`). Finite-number checks before clamping, real booleans, and a string API key. Keep the form's numeric-string support. Malformed values produce the matching `settings-save-result` error and persist nothing.
- **Minor: reverse-label workers** (`app.py:1093`, `:961`). Check the request token before geocoder I/O and after the throttle wait. Coalesce queued label work to the newest target, and bound worker admission.

### W3: Frontend (`index.html`, `app.css`, `app.js` except the W2 handshake, `core.js`, `state.js`, `aqi.py` band **foreground** values only, `scripts/test-ui-regression`, `test-heatmap`, `test-save-refresh`, `tests/js/*`)

5. **Per-reading provenance** (`app.js:479`, `:424`, `:1014`, `:449`), using the user's degraded-only rule (decision 4).
   - Every surface that shows an individual reading carries that reading's badge when it is Demo, Stale, or Old: popup, sensor list rows, marker accessible name, selection announcement, and detail card.
   - Show the badge visibly on the popup, rows, and detail card. Marker accessible names and the selection announcement include the AQI category and the badge text.
   - Live and in-interval-cached readings get no badge, and the summary and detail card stop labeling ordinary in-interval cache delivery as "Cached".
   - Provenance is per reading, not per batch, because area and favorite collections can differ.
   - Use mixed-origin fixtures: live area + demo favorite, stale area + fresh favorite, fresh area + Old favorite. Old uses the same 2-hour freshness rule and clock as alert eligibility (shared with W1's finding 3).
6. **Contrast, keeping colors** (`core.js:56`, `app.js:527`, `app.css:211`).
   - Background colors are unchanged everywhere, per the user's decision.
   - The detail card's AQI number sits on a badge filled with the band color, with a per-band foreground.
   - All text-on-band and text-on-accent foregrounds meet WCAG 2.x contrast: ≥4.5:1 for normal text and ≥3:1 for large text (≥24px, or ≥18.66px bold). This covers badges, the detail badge, legend text if any, and dark-mode Save.
   - Update `foreground` identically in `aqi.py` and `core.js`.
   - Add an automated contrast test over every band in both themes. Notifications and other Python uses of `foreground` must stay consistent.
14. **Failed tile recovery** (`app.js:855`).
    - Failed current tiles are retried with bounded backoff, and immediately on manual Refresh.
    - Retained older imagery stays until a replacement actually loads.
    - Test failure then success at an unchanged center and zoom.
15. **Keyboard map panning** (`index.html:13`, `app.js:1349`).
    - The map is a named, focusable region with a visible focus ring.
    - Arrow keys pan by a fraction of the viewport (builder picks the step and an optional larger Shift step). `+`/`-` zoom.
    - Keys are not intercepted when focus is in inputs, dialogs, or popups. Panning uses the same navigation cancellation and view-fetch scheduling as pointer panning.
    - Discoverable via the map's accessible description and the README.
    - Add a real keyboard-traversal test in the live harness: Tab to the map, then arrow keys.
16. **Accessible trend values** (`app.js:593`, `index.html:109`).
    - The chart has an accessible name and description plus a visually hidden table or list of each rolling window's AQI and category.
    - Sighted users can read individual values: per-point labels or tooltips on hover and focus (builder's choice). The wording keeps "current vs rolling average", not "history".
    - Assert the values in the accessibility-facing markup.
17. **Narrow and scaled overflow** (`app.css:75`, `:146`).
    - At 320px and 375px, at 100/125/150%, and in both themes, the expanded detail card shows a three-digit AQI with a long category and provenance, and a long place name, without horizontal overflow or clipping.
    - Extend the live layout matrix to cover expanded details, long labels, and the theme matrix.
18. **Preview honesty** (`app.js:65`, `:1115`).
    - In browser preview, settings the preview doesn't implement (refresh interval, radius, automatic location) are disabled with a visible explanation.
    - Implemented settings keep working, and each enabled preview setting has an observable-effect test.
21. **Trend-retry false positive** (`scripts/test-ui-regression:592`). The scenario drives a real failure and retry through the real request identity, waits for a terminal rendered state, and asserts the chart or message, not only the Retry button's visibility.
- **Minor: search dismissal** (`app.js:1354`). Escape, clearing, or replacing a submitted query invalidates and cancels the pending remote search via `cancel-place-search`.
- **Minor: hidden-sensors list** (`app.js:1140`). Keyed reconciliation keeps focus through background refreshes. Unhide moves focus to a logical successor.
- **Minor: ticking ages** (`app.js:1214`). Visible relative times update on a minute timer without refetching or recomputing AQI.
- **Minor: `descendants()` in `scripts/test-ui-regression:235`.** Tolerate only vanished processes; surface other errors.
- **Minor: fixed completion sleeps** in `scripts/test-ui-regression` (~466, 473, 489, 502, 512, 927, 1027, and layout pumps ~1237/1240). Replace with explicit completion conditions where an observable condition exists; justify any sleep that remains.
- **Deferred styling:** give the fixed-home `type="search"` input the shared input styling.

### W4: Release, CI, packaging, docs (`.github/`, `scripts/` release scripts, `make-screenshots`, `Makefile`, launchers, `packaging/` except screenshots, `RELEASE.md`, `README.md` install sections)

19. **Upgrade verification preserves existing trust** (`scripts/verify-flatpak-release.sh:174`; `flatpak.yml:430`).
    - In production mode, the previous installation's remote gets only its URL redirected. Its keyring and verification settings stay intact, and required signature settings are asserted.
    - Candidate-key import into the previous install is a distinct, development-only option, used only by the ephemeral-key PR path.
    - Add a negative fixture (previous trusted key ≠ candidate signing key) that production mode rejects.
    - Fix the fake `key_ok` hardcoding (`tests/test_release.py:1038`, `:1067`).
    - Document in RELEASE.md that key rotation needs a reviewed client migration and will fail the gate otherwise.
    - **Pre-tag proof (controller's call):** PR CI still uses ephemeral keys, so the production URL-only path otherwise runs for real only in the tag run. Before tagging, replay `verify-flatpak-release.sh` in production mode in the pinned image (real Docker), using real published production-signed artifacts. The previous bundle is v0.12.3 (`gh release download`, read-only), and the candidate is a local mirror of the published v0.13.0 Pages repository plus its bundle. The replay must pass. It must also fail in a negative replay, where the candidate is re-signed with a throwaway key. Record both in the W4 report.
20. **Makefile quoting** (`Makefile:25–37`).
    - Every destination path is quoted, with `--` where appropriate.
    - Add staged install and uninstall tests with a path containing spaces and glob characters, plus a sibling sentinel that must survive.
- **Minor: `claude.yml` bot guard.** Add an explicit workflow-level guard so bot senders and actors are skipped before checkout and any token use, consistent with `claude-code-review.yml`. Add a contract test over its event types.
- **RELEASE.md recovery rule:** document decision 5. Delete and re-push the tag only while nothing is published; otherwise ship the next patch version.
- **Minor: RELEASE.md and README asset names.** Downloaded release assets are `Airloom-ARCH.flatpak`; keep `dist/Airloom-VERSION-ARCH.flatpak` only for local builds.
- **Minor: `descendants()` in `scripts/make-screenshots:174,179`.** Tolerate only vanished processes and tasks; surface other errors. Cleanup must not report success after an incomplete enumeration.

## Shared-file ownership (collision rules)

- `app.js`: W3 owns it, except the shell-recovery receive and handshake lines (W2). W2 keeps that edit minimal and states the exact lines in its report. W3 rebases around it at integration.
- `presentation.py`: W1 owns the alert-freshness guard (finding 3); W2 owns everything else. Band colors live in `aqi.py` and `core.js`, not here.
- `aqi.py`: W3 changes only the band `foreground` values. No other edits by anyone.
- `scripts/test-ui-regression`: W3. `scripts/make-screenshots`: W4.
- `tests/`: each workstream adds or edits only tests for its own modules. Shared helpers (`tests/gi_stub.py`, `tests/record_handoff.py`) are edited only if necessary, and the edit is called out in the report.
- `CHANGELOG.md`, AppStream, `__version__`, and screenshots: integration only.

## Process and verification

1. W1–W4 run in parallel worktrees from `astra-v0-14-0`. Each finding gets a failing test first, then the fix, then a focused run. The workstream ends with `make check` (ResourceWarnings as errors). Workstreams touching UI also run the live UI scripts as a non-root user under a private headless display.
2. Sol reviews each workstream diff. Critical/Important findings get fix rounds with scoped re-review.
3. Merge into `astra-v0-14-0` in order: W1 → W2 → W3 → W4. After each merge run `make check`; after W3, also the live UI scripts.
4. Integrated verification:
   - full `make check`;
   - live UI scripts;
   - the hosted test-job reproduction (ubuntu:24.04, Python 3.12, no PyGObject);
   - the webkit-ui job replayed in **real Docker** (not podman);
   - where W4 changed the client verifier, a real `verify-flatpak-release.sh` run in the pinned image with real artifacts.
5. The four Astra reviewers re-verify their own findings on the integrated diff. Anything still open loops back.
6. Release task:
   - 0.14.0 in `__version__`, CHANGELOG, and AppStream, dated on the release day;
   - regenerated screenshots;
   - docs updated for keyboard panning, preview behavior, and signing policy.
   - Sol reviews the release diff.
7. Publish: PR to `main` → full hosted matrix green → fast-forward `main` → green `main` run → annotated `v0.14.0` → verify assets, checksums, provenance, Pages signatures, and screenshot URLs. If a tag-run gate fails, follow decision 5.

## Left to the builder

- Exact keyboard pan step sizes, focus-ring styling, and the tooltip-vs-label choice for chart values.
- The exact dark foregrounds per band, provided the contrast thresholds are met and the background colors are unchanged.
- Alert retry bookkeeping structure, provided the semantics above hold.
- Worker admission bounds and the main-loop blocking measurement method.
- Tile retry backoff schedule.
