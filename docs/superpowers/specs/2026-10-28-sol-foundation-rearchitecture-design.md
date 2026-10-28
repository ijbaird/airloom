# Airloom Sol Foundation Rearchitecture Design

Date: 2026-10-28
Baseline: `ac83833577707f84e392b633fad8847321d308d8` (`v0.12.2`)
Target release: `v0.13.0`

## Scope authority

This document is the complete, authoritative translation of Sol's four review reports into implementation requirements. Builders do not need the original reports to plan or execute the work. Reproduced findings and actionable recommendations are incorporated below; unverified considerations are not obligations unless this document explicitly includes them as hardening or verification. If a later reading of a report appears to differ, this approved design controls the release scope.

## Intent

Apply all actionable feedback from Sol's four read-only reviews without continuing to patch shared symptoms independently. Airloom should present scientifically and geographically correct data, keep asynchronous work from overwriting newer user intent, make synthetic/stale provenance unmistakable, behave safely when persistence or release infrastructure fails, and become testable below the GTK/WebKit boundary.

The user explicitly selected the **foundation-first rearchitecture** rather than a minimal remediation patch. This is therefore a broad minor release, not a focused bug-fix patch.

## Goals

1. Establish GTK-free application orchestration and dependency-free frontend foundations before fixing individual races and presentation defects.
2. Make observation freshness, retrieval freshness, source provenance, measurement basis, coverage completeness, and correction version explicit.
3. Correct cache transactionality, metadata reconciliation, numeric handling, settings persistence, and resource ownership.
4. Make map navigation, antimeridian behavior, summaries, filters, trends, search, alerts, and accessibility consistent across live, cached, demo, and preview modes.
5. Make installation and release publication fail closed, with client-side installation/upgrade verification and provenance.
6. Add deterministic regression coverage for every corrected defect and run integrated UI checks in CI without live PurpleAir usage.
7. Preserve existing user configuration and favorites, and release all verified work as `v0.13.0`.

## Non-goals

- Adding pip, npm, map-library, hosted-backend, or other third-party runtime dependencies.
- Implementing a background monitoring daemon or regulatory AirNow NowCast.
- Supporting arbitrary geocoder provider protocols; one compatible endpoint override is sufficient.
- Promising bit-for-bit reproducible Flatpak builds. The release will record resolved inputs instead.
- Rotating the production signing key, changing GitHub branch protections, or changing repository secrets unless needed to execute the approved release.
- Live paid PurpleAir testing, real desktop-notification delivery, screen-reader certification, or claims beyond what automated/manual checks actually establish.

## Anti-goals / failure conditions

- Demo or expired readings producing genuine-looking health alerts.
- A superseded request changing visible state, charging for obsolete trend work, or overwriting a newer label/search result.
- A locked SQLite database being deleted, or a failed settings save appearing successful.
- Distant favorites changing the named area's AQI summary.
- Remote autocomplete against public Nominatim, or silent transmission of partial search text.
- Tests killing unrelated app instances or touching real config/cache data.
- Branch dispatch, malformed tags, mismatched versions, or unverified bundles reaching the stable release channel.
- A large controller rewrite that merely moves GTK-free logic while keeping the same ambiguous ownership and races.

## Constraints

- Python standard library plus system PyGObject only; handwritten vanilla JavaScript/CSS only.
- GTK/WebKit access remains on the main loop. Workers never touch GTK/WebKit directly.
- Browser preview must continue to work when `index.html` is opened directly, so frontend helpers cannot require a bundler or file-URL-unfriendly module loading.
- Existing configuration values and favorites remain valid. Cache schema/version changes may clear derived cached readings, consistent with current upgrade behavior.
- PurpleAir area fetches remain cost-conscious and delta-polled; no trend fields in area queries and no live requests in tests.
- Public Nominatim searches require explicit submission. Local sensor filtering may remain incremental.
- The release workflow must retain scoped permissions and must not expose signing secrets to untrusted PR code.

## Approaches considered

### Coordinated hardening release

Parallel subsystem fixes on one integration branch, adding only local shared abstractions. Lower implementation risk and faster, but leaves the monolithic orchestration and ambiguous request/data ownership that caused overlapping defects.

### Foundation-first rearchitecture — selected

First establish a GTK-free controller, explicit request identities, provenance-aware data boundaries, and pure frontend helpers. Then port behavior and fixes onto those foundations in dependency order. This is larger and requires more integration testing, but directly addresses the root causes and matches the user's decision.

### Minimal remediation patch

Fix isolated defects and defer architecture. Rejected because it conflicts with the requested all-actionable scope and would duplicate later work.

## Architecture

### 1. GTK-free application controller

Add a small controller layer, exercised entirely with stdlib `unittest`, with `app.py` acting as the GTK/WebKit adapter.

The controller owns:

- Current home, viewport, filters, and configuration generation.
- Separate visible-area and favorite-monitoring collections.
- Refresh request IDs/generations and pending replacement work.
- Decisions about whether a completion is authoritative, which sensors to publish, which trends to request, and which alerts are eligible.
- Transactional preference candidate construction and post-persistence commit/rollback decisions.
- Explicit lifecycle state so shutdown rejects new work and invalidates callbacks.

The GTK adapter retains actual threads, `GLib.idle_add`, GTK signals, notifications, and WebKit evaluation. Injected clock, persistence, fetch, and notification interfaces make ordering and failure tests deterministic. Network and database code stay outside the controller.

A refresh descriptor carries request ID, configuration generation, home/view identity, normalized query bounds, force flag, and favorite IDs. A superseded completion may populate the raw cache, but cannot publish sensors, change selection, update visible freshness, emit alerts, or start trends. Pending replacements and completion cleanup run in exception-safe finalization.

Manual and automatic refresh target the current viewport when one exists, falling back to home only before a view exists. Loading/recentering home remains a separate action. Settings saves persist a validated candidate first; only success publishes configuration and closes/acknowledges Preferences. Favorite/hide failures retain consistent state and show an actionable error. Alert-state save failures are reported without interrupting refresh finalization.

### 2. Request coordination and bridge contracts

Introduce a dependency-free request registry for trend, place-search, reverse-geocode, and refresh work:

- Every request has an ID and context; callbacks verify both identity and generation.
- Trend requests are single-flight per sensor and expose `idle`, `loading`, `loaded`, `unavailable`, and `error` states. Repeated selection reuses in-flight work; failures clear loading and offer bounded manual retry.
- Place results carry `{request_id, context}` (`main` versus `home`). Closing Preferences cancels its local work and invalidates outstanding home searches. Obsolete queued geocoder requests are dropped before network I/O where possible.
- Home reverse-geocode completions carry coordinates and a home generation; only the latest matching fix/mode may persist a label.
- Location requests own a `Gio.Cancellable`; leaving automatic mode cancels focus waits and in-flight location work. Deferred callbacks recheck mode before starting.

Application shutdown stops timers/listeners/debug serving, prevents new workers, and closes the cache after active database operations drain or safely reject further work.

### 3. Provenance, freshness, filtering, and alerts

Internal sensor state distinguishes:

- Observation time (`last_seen`) from retrieval/cache-write time.
- Origin (`live` or `demo`) and delivery source (`network` or `cache`).
- Area collection versus favorite-monitoring collection.
- Measurement basis (`CF1` or `ATM`) and correction-model version.
- Coverage/fetch bounds and completeness.

A single Python presentation boundary applies hidden, confidence, and indoor/outdoor filters to network, cached-startup, stale-fallback, and favorite data. Favorites may still be fetched for monitoring, but opposite-type favorites do not bypass the selected display filter. Distant favorites never contribute to the local summary.

Alerts require non-demo data, an eligible real observation no more than two hours old, a favorite, threshold crossing, and existing hidden/confidence rules. Demo results neither notify nor mutate real alert state. Cached data with a fresh real observation remains eligible; expired observations do not. Alert operation only while Airloom is running is documented.

Demo and stale states appear persistently near the summary/detail AQI, not only inside the initially hidden sensor panel. Demo fallback remains available but cannot be mistaken for live data. The UI distinguishes sensor AQI from regulatory NowCast and identifies capped/limited data coverage; empty heat-map space is not described as clean air.

### 4. Scientific and numeric correctness

Adopt and document two explicit correction paths, with independently calculated fixtures from the cited primary sources:

- Current CF1 readings and indoor running averages: Barkjohn et al.'s final CF1 equations (linear below raw 570, blended 570–611, quadratic from 611), from *Correction and Accuracy of PurpleAir PM2.5 Measurements for Extreme Wildfire Smoke*, §3.1.3.
- Outdoor running averages: the published five-piece CF=ATM correction used by PurpleAir/AirNow. PurpleAir confirms simple running averages are ATM outdoors and CF1 indoors (PurpleAir API discussion, posts 4, 6, 17–19).

Trend fetch fields include `location_type`; conversion selects the basis from it. Current humidity applied to historical averages is identified as an approximation. The correction version is recorded and the cache schema/version is bumped so derived trend values cannot survive under the old model.

All external numeric values are validated at ingestion. Non-finite, fractional/invalid IDs, invalid coordinates, correction overflow, and Decimal-domain errors become unavailable data rather than crashes or AQI 0. Valid above-scale concentrations clamp to the AQI scale without allowing infinity. Cache-ingested rows receive the same checks as direct API rows. Geocoder places require finite in-range coordinates.

The chart labels its actual comparison (for example, “Current vs 1-week average”) rather than claiming a temporal trend from rolling windows. Demo trend `Now` exactly equals the displayed demo AQI.

### 5. Cache transactions and lifecycle

Refactor area fetch/commit sequencing so required unknown-ID metadata backfills complete before rows, freshness, coverage, and the delta checkpoint are published transactionally. Failed backfill leaves coverage stale and retries unknown IDs instead of advancing past them.

Separate observation expiration from request freshness:

- Empty delta responses do not rejuvenate old observations.
- Pruning occurs on all relevant fetch paths, not only full fetches.
- If pruning removes rows, affected coverage is invalidated; the conservative implementation may invalidate all region coverage rather than risk an incomplete delta resume.
- Regions older than a defined metadata-snapshot TTL (initially 24 hours) require a full snapshot to reconcile names, coordinates, location type, and membership. Delta polling remains available within that TTL.
- Absent rows are not globally deleted merely because they are absent from one overlapping region.

SQLite recovery is destructive only for confirmed corruption/not-a-database errors. Busy/locked, permission, storage, and I/O errors never unlink the file; they surface as cache failures and allow a nonpersistent/degraded path. Failed initialization connections close before any recovery/replacement.

`SensorCache` gains an idempotent, lock-protected `close()` and context-manager support. Every test and application owner closes it; shutdown coordinates workers before closure. Tests force garbage collection with warnings captured to prove no unclosed SQLite connections remain.

### 6. Defensive settings persistence

Use one exception-safe sensor-ID validator for favorites and hidden IDs. Reject booleans, non-finite/fractional numbers, Unicode strings not accepted by `int()`, and out-of-domain values while preserving valid entries from damaged config.

Save via a unique exclusively created temporary file in the destination directory (`mkstemp`), mode `0600`, with flush/fsync, atomic `os.replace`, failed-temp cleanup, and per-store serialization. Existing permissive temp files or symlinks are never followed. Destination config remains private after every save. Persistence exceptions cross UI boundaries as explicit errors, not tracebacks or false success.

### 7. Frontend foundations and behavior

Extract pure, dependency-free helpers for AQI bands, projection/wrapping, bounds splitting, summary selection, formatting, and request/navigation state. Use a small namespace/UMD pattern compatible with direct file preview and Node's built-in test runner; no build step or package dependency.

#### Geography and summary

- Maintain an unwrapped horizontal map-center coordinate.
- Project sensors/popups/heat-map points into the nearest world copy while tiles wrap normally.
- Represent antimeridian-crossing bounds explicitly and split backend/cache queries at ±180°, merging and deduplicating results.
- Keep latitude/longitude validation finite and in range; retain the existing 200 km fetch cap.
- Compute the named area summary from eligible area sensors in the relevant viewport/fetch area, never remote favorites. Keep favorite presentation separate.
- Display limited-coverage status when the viewport exceeds fetched coverage.

#### Navigation and rendering

- A navigation coordinator makes direct manipulation, fly-to, and zoom/settle animation explicitly cancel or supersede one another. Drag, wheel, and pinch cancel flight; a new flight cancels incompatible zoom state.
- Honor reduced-motion preferences and provide immediate transitions where requested.
- Reconcile sensor/marker controls by stable sensor ID rather than blindly replacing focused nodes. Preserve/restore logical focus by ID and originating surface; deliberately move/restore focus for popup/detail dismissal and hidden sensors.
- Add popup viewport collision handling, search overlay stacking above ordinary panels, narrow-layout top-control reflow, and meaningful keyboard operation.
- Add accessible search text, visible focus, dialog naming, live status/error regions, `aria-pressed` favorite state, and persistent interface scaling without allowing map pinch to scale the whole document.
- Use all six EPA categories in markers, heat map, and legend, including boundaries 150/151, 200/201, and 300/301.

#### Tiles, search, trends, and preview

- Separate tile request settlement from successful image load. Failed tiles stay hidden and do not retire usable older coverage as though successful.
- Ignore zero/predominantly horizontal wheel input for zoom.
- Remote geocoding occurs only on explicit submit; typing filters local sensors only. Requests carry context/identity and duplicate queries are coalesced/cached.
- Trend state and retry behavior match the backend protocol; unavailable is never rendered as indefinite loading.
- Centralize desktop/preview actions behind adapters. Preview favorite, Refresh, and supported preferences either work locally or are explicitly disabled/explained—never silently pretend to succeed.

### 8. Geocoder, shell hardening, and documentation

Default to public Nominatim with explicit-submit searches, request throttling/coalescing, and bounded repeated-query caching. Support a compatible endpoint override through documented environment/configuration so the service can change without an application update. No partial query text is sent remotely.

Document GeoClue, Nominatim search/reverse traffic, PurpleAir/OSM traffic, demo-mode networking, fixed-home behavior, alert lifecycle, AQI/correction semantics, and Flatpak config/cache locations.

Restrict bridge-bearing WebKit navigation to the bundled entry document; open approved external links externally and reject unrelated file navigation. Add explicit load-failure/web-process failure feedback/recovery rather than a silent blank view.

Debug startup rejects non-socket paths, refuses live listeners, replaces only demonstrably stale sockets, tracks the bound inode for cleanup, and stops on application shutdown. Command names must be strings before lookup; asynchronous dispatcher exceptions always produce an immediate error reply.

GUI scripts launch their own non-unique child directly, isolate both config and cache, and terminate only that child. They never kill unrelated Airloom instances.

### 9. Installation, CI, and release safety

- Make the installed launcher derive its runtime prefix from its installed `bin` location (or equivalently generate the configured prefix), so default, `/usr`, custom `PREFIX`, and staged `DESTDIR` installs resolve their own `share/airloom`. `DESTDIR` never becomes a compiled runtime prefix.
- Read local bundle version from the exported committed tree with a controlled import path, never dirty working-tree code.
- Update stale README build/install instructions, MVP wording, offline/network expectations, and use version-neutral bundle examples.
- Align CI with full `make check`, including compileall, JS syntax, Python tests, and new Node tests. Add a headless WebKit UI job for deterministic startup, stripped-debug-module, map/settings/refresh/search/focus/alert scenarios with fake data.
- CI regression jobs isolate config/cache and do not contact live PurpleAir.
- Credential-dependent Claude review skips cleanly for Dependabot/fork actors unless narrowly configured for that trust model; do not broadly allow bots or elevate untrusted code.
- Adopt the offered checkout, deploy-pages, and Flatpak action updates where compatible, using immutable commit SHAs for third-party action references and retaining dependency automation.

#### Publication gates

Branch/manual builds may produce unsigned development artifacts but cannot sign/publish stable. Signing and publication require a validated `vX.Y.Z` tag whose version matches `airloom/__init__.py`, `CHANGELOG.md`, and AppStream metadata.

Current-tag reruns remain idempotent. Publishing an older tag requires an explicit typed confirmation input and is otherwise rejected; branch dispatch and malformed/mismatched tags fail before signing/deployment.

Add client-side verification before GitHub release or Pages deployment:

1. Verify the imported signing key matches the committed public key on production releases.
2. Install each signed bundle with GPG verification enabled and assert origin/version.
3. Assemble a signed repository and verify installation from it.
4. Verify upgrade from a previous supported installation.
5. Assert a wrong signing key fails; never use `--no-gpg-verify` to make checks pass.
6. Use an ephemeral test key for ordinary CI and the production key only in protected release contexts.

Record resolved container image digest, Flatpak SDK/runtime refs/commits, action/tool versions, source SHA, and bundle checksums as provenance. Document key expiry/rotation/recovery and the limits of reproducibility.

Add representative AppStream screenshots and useful project/help links, generated from deterministic demo data and referenced at stable project URLs.

## Implementation order and parallelism

1. **Foundation contracts:** controller state/events, request registry, provenance models, pure frontend helpers, test harnesses.
2. **Parallel subsystem workstreams after contracts land:**
   - Controller/application async, alerts, settings transactions, location, lifecycle.
   - Cache/store/AQI/geocode correctness and resource ownership.
   - Frontend geography, navigation, rendering, accessibility, preview.
   - Debug/shell/test-script hardening and integration scenarios.
   - Installation, CI, Dependabot updates, packaging, provenance, documentation.
3. Integrate in dependency order: data models/cache → controller → frontend bridge/UI → shell/debug → release/docs.
4. Sol reviews each workstream diff. Resolve Critical/Important feedback before integration; assess Minor feedback rather than ignoring it silently.
5. Run the full integrated verification matrix, generate deterministic screenshots, update release metadata, and have Sol review the complete base-to-release diff.
6. Merge/push the verified integration branch, create the annotated `v0.13.0` tag, and monitor the release and Pages workflows through completion.

Worktrees must have disjoint ownership. Shared contracts land before parallel edits; integration conflict resolution stays on the integration branch.

## Verification matrix

- Baseline and final `make check`; all pre-existing tests retained or deliberately updated for corrected behavior.
- Python controller: superseded refresh, filter/config change during fetch, pending drain after save failure, current-view refresh, alert provenance/freshness, favorite separation, reverse-geocode generations.
- Cache: failed backfill retry/rollback, pruning/revisit, repeated empty deltas, metadata relocation/reclassification, overlapping regions, locked versus corrupt DB, close/reconnect warnings.
- Store/AQI: malformed favorites, hostile numerics, independent correction fixtures, permissive/symlink temp files, overlapping saves, ENOSPC/PermissionError.
- Node: AQI boundaries, nearest-world projection, bounds splitting, summary population, navigation cancellation, search context/generation, focus reconciliation, tile settlement.
- Headless UI: away-from-home refresh, delayed/out-of-order search, interrupted flight, keyboard focus, filter/cache fallback, trend failure/retry, demo/stale labeling, reduced motion, preview actions, popup/search/narrow layouts.
- Installation: staged default, `/usr`, custom prefixes; launcher resolution outside the checkout; bundle filename/version from dirty-tree fixture.
- CI/release: branch dispatch, malformed/mismatched tags, current rerun, unconfirmed older rerun, signed install/origin/upgrade/wrong-key failure, provenance presence.
- Test safety: sentinel unrelated process/config/cache survives every regression script.
- Documentation: network-call inventory matches fresh demo, fixed-home, search, and live behavior.
- Where local Flatpak tooling is unavailable, required bundle/client gates run in CI and must pass before publication; local success is never claimed for unrun checks.

## Builder decisions

These are implementation calls, not additional product requirements:

- Use conservative region invalidation after pruning instead of adding region-membership tables in this release.
- Use a two-hour maximum real observation age for notifications, with stale display still allowed.
- Use a 24-hour authoritative metadata-snapshot TTL.
- Use small namespaced JavaScript helper files rather than ES modules to preserve direct file preview.
- Prefer launcher-relative prefix resolution over maintaining a generated launcher template.
- Existing cache invalidation strategy is acceptable for correction/provenance schema changes; user settings are not cleared.

## Release evidence and reporting

The final report will list implemented themes, verification actually run (including any unavailable local checks), Sol's re-review outcome, release commit/tag, and GitHub release/workflow URLs. It will distinguish successful CI artifact publication from any local GUI/manual behavior not directly exercised.
