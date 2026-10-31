# Integration, Astra Re-verification, and 0.14.0 Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge the four reviewed workstreams, reconcile their contracts, prove the integrated branch against hosted-faithful conditions, close every Astra finding, and publish Airloom 0.14.0.

**Architecture:**
- Sequential integration on `astra-v0-14-0`: merge, run tests, reconcile contracts.
- Then the Astra reviewers re-verify their findings, a release-metadata task runs, and the established publication sequence follows.

**Tech Stack:** git, `make check`, live WebKit UI scripts, real Docker, podman, `gh`, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-30-astra-review-v0-14-0-design.md`

## Global Constraints

- Same as `README.md`.
- Nothing is pushed, merged to `main`, or tagged until its gate passes.
- Merging to `main` is a fast-forward to the exact reviewed SHA.
- Tag recovery follows design decision 5: delete and re-push `v0.14.0` only while nothing is published; otherwise ship 0.14.1.

## Review Focus

- **Contract drift between workstreams:** W1's freshness rule as consumed by W2 alerts and W3 "Old" badges; W2's shell-recovery event as consumed by W3's `app.js`. Integration tests prove one rule and one clock end to end.
- **Hosted-only behavior:** Docker seccomp, ext4 inode reuse, runner-owned workspaces, Python 3.12 without PyGObject. Replay in real Docker and in the ubuntu:24.04 reproduction, not podman alone.
- **Screenshots and notes vs reality:** regenerated screenshots show the new detail badge and no spurious badges. Release notes describe the narrowed provenance rule and the signing-gate policy accurately.
- **The production-only upgrade path:** W4's pre-tag replay evidence exists and is re-run if W4 changed after it was recorded.
- **Flakes:** any hosted flake gets a root-cause fix, not a silent rerun, before the tag.

---

### Task 1: Merge workstreams and reconcile contracts

**Files:** Only what contract reconciliation requires; add `tests/test_integration.py` cases.

- [ ] **Step 1:** Merge `astra-data-v0-14-0` with `--no-ff`. Run `make check PYTHON="python3 -W error::ResourceWarning"`; expect PASS.
- [ ] **Step 2:** Merge `astra-controller-v0-14-0`. Before resolving any conflict or contract mismatch with W1, write a failing integration test for it: the alert eligibility and the W1 freshness rule share one function and clock. Then run `make check`.
- [ ] **Step 3:** Merge `astra-frontend-v0-14-0`.
  - Resolve `app.js` around W2's shell-recovery lines.
  - Add a failing-first integration test for each mismatch:
    - the "Old" badge (`AirloomCore.isFreshObservation`) and alert eligibility (`models.is_fresh_observation`) agree for ages 0, 7200, 7201, −600, −601, and a missing `last_seen`. W3 deferred the −600/−601 cases to here because W1's function didn't exist on its branch;
    - the shell-recovery `restore-view` handshake restores an away-from-home view in the real page.
  - Run `make check`, plus `scripts/test-ui-regression`, `scripts/test-heatmap`, and `scripts/test-save-refresh` as non-root under a private headless display.
- [ ] **Step 4:** Merge `astra-release-v0-14-0`. Run `make check`.
- [ ] **Step 5:** Commit the reconciliation fixes, if any, as `fix: reconcile 0.14.0 workstream contracts`.
- [ ] **Step 6:** Sol scoped review of the reconciliation commits. Any Critical/Important finding gets a fix round.

### Task 2: Hosted-faithful integrated verification

- [ ] **Step 1:** Run the hosted test-job reproduction on a `git archive` of HEAD: ubuntu:24.04, Python 3.12, no PyGObject, Node 22, non-root, via `/tmp/opencode/ci-repro/run-test-job.sh`. Expect `make check rc=0`.
- [ ] **Step 2:** Replay the extracted `webkit-ui` job from the committed YAML in **real Docker**:
  - Docker default seccomp and capabilities;
  - empty machine-id;
  - workspace owned by uid 1001;
  - one `docker exec` per step.
  Expect all three scripts to PASS with no skips.
- [ ] **Step 3:** If W4's verifier changed after its pre-tag replay was recorded, re-run W4's production-mode replay and negative replay. Expect PASS and FAIL respectively.
- [ ] **Step 4:** Record the results in the integration ledger.

### Task 3: Astra re-verification

- [ ] **Step 1:** Build the review package for `f1f811d..HEAD`.
- [ ] **Step 2:** Resume each original Astra reviewer session (data-core, controller-shell, frontend, release-ci) with a scoped prompt: mark each of its findings ADDRESSED or NOT ADDRESSED with file:line, and report any new breakage in the files it owns. Run all four in parallel.
- [ ] **Step 3:** Open findings loop back to the owning workstream's implementer as a fix round, then Sol and the same Astra reviewer re-verify. Continue until all four report every finding addressed.

### Task 4: 0.14.0 metadata, notes, docs, screenshots

**Files:** `airloom/__init__.py`, `CHANGELOG.md`, `packaging/ai.stealthvision.Airloom.metainfo.xml`, `packaging/screenshots/*.png` (regenerated), `README.md`, `CLAUDE.md`.

- [ ] **Step 1:** Confirm the existing relationship tests already require version, changelog, and AppStream agreement and tag-pinned screenshot URLs. No new literal-version test.
- [ ] **Step 2:** Set `__version__ = "0.14.0"`, a `## 0.14.0 — <release date>` section, and `<release version="0.14.0" date="<release date>">` first in `<releases>`.
- [ ] **Step 3:** Write the release notes, user-facing and accurate:
  - stale or malformed or future-dated data and boolean values no longer look fresh or "Good";
  - alerts aren't lost or repeated;
  - Refresh, key removal, offline favorites, and lock freezes are fixed;
  - the narrowed provenance-badge rule;
  - contrast (colors unchanged);
  - keyboard panning and accessible trend values;
  - preview honesty;
  - narrow layouts;
  - tile retry;
  - settings durability;
  - the signing-gate policy for key rotation;
  - install-path quoting.
- [ ] **Step 4:** Regenerate screenshots with `scripts/make-screenshots`, pinned to tag `v0.14.0`. Look at each one:
  - the detail AQI badge is visible;
  - "Demo" badges are expected and correct, since the screenshots use demo data;
  - no Stale or Old badge appears.
- [ ] **Step 5:** Update the README and CLAUDE.md and check every claim against the code:
  - keyboard panning (keys, step) and preview behavior;
  - W2's `restore-view` bridge event, W1's freshness rule, and the signing policy;
  - that `scripts/test-ui-regression` needs an X11 display with XTEST (Xvfb, as in CI), since its keyboard scenario fails without it.
  - The data-source footer (`#data-source`) keeps showing Python's source string such as "PurpleAir · cached …" as attribution. That's intended: it describes delivery, not a per-reading badge.
- [ ] **Step 6:** Run validators: `make check`, `desktop-file-validate`, `appstreamcli validate --no-net`, `git diff --check`. Run `check-release.py` in a scratch clone with a local `v0.14.0` tag; expect `release_eligible=true`.
- [ ] **Step 7:** Commit `docs: …` and `Release 0.14.0`. Sol reviews the release diff.

### Task 5: Publish

- [ ] **Step 1:** Push `astra-v0-14-0` and open a PR to `main`. Wait for the full pull_request matrix to be green; any failure gets a root-cause fix with Sol review.
- [ ] **Step 2:** Fast-forward `main` to the reviewed SHA with `git push origin <sha>:refs/heads/main`. Wait for the `main` run to be green.
- [ ] **Step 3:** Create an annotated, unsigned `v0.14.0` tag ("Airloom 0.14.0 — <summary>", tagger Ian Baird). Push only the tag.
- [ ] **Step 4:** Monitor the tag run, including production-key client verification (now URL-only).
- [ ] **Step 5:** Verify the release assets, SHA256SUMS, provenance against the tag SHA, Pages `.flatpakrepo` key, GPG-verified HTTPS pulls of both architectures, wrong-key refusal, and screenshot URLs. A failed gate follows decision 5.
- [ ] **Step 6:** Give the final summary to the user: themes, evidence, reviewer verdicts, URLs, what wasn't exercised, and rulings made.
