# Frontend Rearchitecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the Web UI to pure shared math/state, correct geography and async behavior, and make all primary workflows keyboard/accessibility safe.

**Architecture:** `core.js` supplies deterministic math; a new `state.js` supplies navigation/request/keyed-reconciliation helpers. The existing application remains handwritten and direct-file-preview compatible.

**Tech Stack:** Vanilla JavaScript/CSS/HTML, Node built-in test runner, existing WebKit debug port.

**Spec:** `docs/superpowers/specs/2026-10-28-sol-foundation-rearchitecture-design.md`

## Global Constraints

No dependencies/bundler/ES-module file-preview regression; consume controller bridge identities/provenance; preserve desktop and preview modes.

## Review Focus

Wrapped sensor copies, interrupted animations, failed tiles, stale search contexts, focus destruction during refresh, zero vertical wheel deltas, and unavailable trends require explicit regression tests.

---

### Task 1: Geography, AQI, summaries, and coverage

**Files:** Modify `airloom/resources/app.js`, `index.html`, `app.css`, `tests/js/core.test.js`; consume foundation `core.js`.

- [x] Extend failing Node tests for nearest-world markers/popups/heat points, crossing view payloads, six heat/legend bands at 150/151, 200/201, 300/301, viewport-only area medians, distant favorite exclusion, and capped-coverage status.
- [x] Run `node --test tests/js/core.test.js`; confirm failures.
- [x] Load core before app, maintain unwrapped center longitude, use nearest world copies everywhere, send explicit crossing bounds, use all six bands, and separate area/favorite rendering/summary inputs.
- [x] Run Node tests and `node --check` on all JS; expect PASS.
- [x] Commit `fix: make map geography and AQI presentation consistent`.

### Task 2: Navigation and tile lifecycle

**Files:** Create `airloom/resources/state.js`, `tests/js/state.test.js`; modify `app.js`, `index.html`.

**Interfaces:** `NavigationGate` tokens for `fly`, `zoom`, and `direct`; `begin(kind)`, `cancel(kind)`, `isCurrent(kind, token)`; reduced-motion query injectable.

- [x] Write failing state tests for drag/wheel/pinch canceling fly, fly superseding zoom, reduced-motion immediate completion, horizontal/zero wheel ignoring, and failed tile settlement not providing successful coverage.
- [x] Run `node --test tests/js/state.test.js`; expect missing export/current race failures.
- [x] Implement the gate and port animation/gesture/tile callbacks; distinguish successful `onload` from `onerror`, keep failed imagery hidden, and honor reduced motion.
- [x] Re-run Node tests/syntax; expect PASS.
- [x] Commit `fix: coordinate navigation and tile completion`.

### Task 3: Stable controls and accessible layouts

**Files:** Modify `app.js`, `index.html`, `app.css`, `tests/js/state.test.js`.

**Interfaces:** `reconcileKeyed(container, items, key, create, update)` preserves existing nodes and logical focus; returns focus metadata for removed active items.

- [x] Add failing DOM-stub tests for keyed list/marker updates, focus after select/trend/refresh/hide, popup/detail focus restoration; add contract checks for dialog name, search label/focus, live region, favorite `aria-pressed`, popup clamping, search z-index, and narrow top controls.
- [x] Run focused Node tests; confirm failures.
- [x] Implement keyed reconciliation and deliberate focus, accessible metadata/live statuses/interface-scale hooks, popup collision placement, overlay stacking, responsive reflow, and persistent demo/stale/coverage indicators.
- [x] Run Node tests and syntax checks; expect PASS.
- [x] Commit `fix: preserve focus and accessible sensor workflows`.

### Task 4: Search, trends, and preview adapters

**Files:** Modify `app.js`, `index.html`, `app.css`, `tests/js/state.test.js`.

**Interfaces:** Desktop/preview adapter methods `favorite`, `refresh`, `saveSettings`, `placeSearch`; remote place search only on submit and always carries `{request_id, context}`.

- [ ] Write failing fake-timer/bridge tests proving typing sends zero remote requests, submit sends one, Preferences close cancels/invalidates home work, out-of-order responses cannot cross contexts, trends reach terminal states/retry, and preview favorite/refresh/settings are truthful.
- [ ] Run focused Node tests; reproduce debounce/global-context/loading issues.
- [ ] Implement explicit-submit remote search with local incremental sensor filter, request identities, trend status/retry UI, rolling-average comparison labels, and centralized desktop/preview adapters.
- [ ] Run all JS tests and `make check`; expect PASS.
- [ ] Commit `fix: make frontend asynchronous actions explicit and truthful`; request Sol frontend-workstream review and list live-WebKit scenarios for integration.
