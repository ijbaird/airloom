# W4: Release, CI, Packaging, Docs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the release upgrade gate prove that existing installs trust a candidate without changing their trust (finding 19, with a pre-tag replay on real production artifacts). Also make `make install`/`uninstall` safe for shell-sensitive paths (finding 20), and close the W4 Minors: `claude.yml` bot guard, release docs, and `descendants()` in `make-screenshots`.
**Architecture:** `scripts/verify-flatpak-release.sh` gets two modes. Production is the default: it redirects only the previous origin's URL and asserts its signature settings and keyring before and after the upgrade. Development is selected by a new `--import-candidate-key` flag, which imports the candidate key and is refused for the committed production key. `.github/workflows/flatpak.yml` passes that flag only when `release-eligibility` is not `true`, the same condition that generates the ephemeral key. The other items are local fixes, each with a regression on the real failing path.
**Tech Stack:** Bash, GNU Make, GitHub Actions YAML, Python 3 stdlib `unittest` (fake `flatpak`/`ostree`/`gpg` executables), real Docker + flatpak 1.18.1 in the pinned image for the replay.
**Spec:** docs/superpowers/specs/2026-10-30-astra-review-v0-14-0-design.md

## Global Constraints

- Python stdlib plus system PyGObject only. Vanilla JS/CSS/HTML with no bundler.
- `make check` passes with ResourceWarnings as errors. JS tests run on Node 22. The hosted Python 3.12 job runs without PyGObject.
- Release gates, provenance, Turnstyle queue, and signing flow stay as in 0.13 except where finding 19 changes them.
- Anti-goal: Weakening an existing assertion or gate to make a fix pass.
- Anti-goal: Releasing with a production signing gate that trusts a key existing users don't already trust.
- Controller call: production client verification must not import the candidate key into the previous installation. A future signing-key rotation will therefore fail the gate until a reviewed client migration exists. This is intended and should be stated in RELEASE.md.
- Pre-tag proof: replay `verify-flatpak-release.sh` in production mode in the pinned image (real Docker), using real published production-signed artifacts. Previous = v0.12.3 (`gh release download`, read-only); candidate = local mirror of the published v0.13.0 Pages repository plus its bundle. Must pass; must fail when the candidate is re-signed with a throwaway key. Record both in the W4 report.
- Pinned image: `ghcr.io/flathub-infra/flatpak-github-actions@sha256:1de59efef01a946d9f28f1ec3458bf6f61dcee3507d7fecdd473f8134da4ec77`, run with `--privileged`, under real Docker (not podman).
- Never pass `--gpg-file` to `flatpak install --bundle` (flatpak 1.18.1 segfaults; flatpak#6842).
- Non-goals (do not touch): flatpak image ≥1.18.3, pinning `fedora:44` and its dnf packages, the `DebugPort` FUSE inode reuse, verify-client download retries, release-before-Pages ordering, `publish-flatpak-repo.sh` trailing-blank tolerance.
- Ownership: `scripts/make-screenshots` is W4; `scripts/test-ui-regression` is W3. `CHANGELOG.md`, AppStream, `__version__`, and screenshots: integration only. W4 edits only README's install sections ("Install from a source checkout", "Flatpak").
- `tests/`: W4 edits only `tests/test_release.py`, `tests/test_workflows.py`, `tests/test_install.py`, `tests/test_debug_scripts.py`. Shared helpers (`tests/gi_stub.py`, `tests/record_handoff.py`) are not edited.

## Review Focus

1. A tag run whose previous install trusts a different key than the candidate's signer (unmigrated key rotation) must fail. Pinned by `test_production_upgrade_rejects_a_candidate_the_previous_install_does_not_trust` (Task 1) and by the Docker negative replay (Task 7).
2. The development key-import flag leaking into a release run. Pinned by `test_only_development_runs_import_the_candidate_key_into_the_previous_install`, which executes the workflow step with `RELEASE_ELIGIBLE=true` (Task 2), and by `test_development_option_refuses_the_committed_production_key` (Task 1).
3. A previous origin that has signature verification switched off must not be "repaired" by the gate. Pinned by `test_previous_origin_must_require_signatures_before_the_upgrade` (Task 1).
4. Staging paths with spaces, glob characters, or single quotes must not delete siblings. Pinned by the three `StagedPathQuotingTest` cases (Task 4).
5. A bot actor whose sender is typed `User` (the Dependabot-synchronize shape), on each of the four mention events, must be skipped. Pinned by `test_claude_mentions_run_only_for_human_actors_on_every_event` (Task 5).

---

### Task 1: Production upgrade verification preserves existing trust (finding #19, script and fake)

**Files:**
- Modify: `scripts/verify-flatpak-release.sh` (arg parsing :9–38, `verify_previous_upgrade` :163–180, main :205–212)
- Test: `tests/test_release.py`, `ClientVerificationTest` (fake at :975–1171, `run_client_gate` :957–1213, new tests after :1376)

**Interfaces:**
- Produces: `verify-flatpak-release.sh --bundle FILE --previous-bundle FILE --repo DIR --arch ARCH --version X.Y.Z --previous-version X.Y.Z [--import-candidate-key]`. Without the flag it runs in production mode. Env: `FLATPAK_GPG_PUBLIC_KEY` (candidate public key, required), `FLATPAK_REPO_URL` (unchanged), `AIRLOOM_PUBLIC_KEY_FILE` (default `<project>/packaging/airloom-flatpak.gpg`, read only when the flag is given).
- Produces evidence line: `upgrade_verified previous_version=P version=V previous_commit=C0 current_commit=C1 trust=preserved previous_keys=<sorted fprs>`, or `... trust=candidate-key-imported previous_keys=<fprs before import>`. Task 2 and Task 7 rely on it.
- Produces denial messages (prefix `client verification denied: `), each exact:
  - `--import-candidate-key is development-only and refuses the committed production key <FPR>`
  - `previous installation origin <origin> does not require GPG signatures`
  - `previous installation (trusting <keys|none>) rejected the candidate release; a signing-key rotation needs a reviewed client migration`
  - `previous installation trust changed from <before> to <after>`

- [ ] **Step 1: Write the failing test.**
  1. Rework the fake so trust comes from the modeled keyring, removing the `key_ok` field everywhere (:1011–1024, :1067, :1074, :1088):
     - Add `FPRS = {b"correct-test-key": "CORRECTFPR", b"wrong-test-key": "WRONGFPR", b"production-test-key": "PRODUCTIONFPR"}` and `repo_signer = env.get("FAKE_REPO_SIGNER", "CORRECTFPR")`.
     - `accepts(remote) = not remote["gpg_verify"] or repo_signer in remote["keys"] or env.get("FAKE_ACCEPT_WRONG_KEY") == "1"`.
     - `remote-add`: `keys = [FPRS.get(bytes, "UNKNOWNFPR")]` for `--gpg-import`, and `gpg_verify = True`. The two `FAKE_WRONG_KEY_REMOTE_ADD_*` injections trigger on `repo_signer not in keys`.
     - `remote-modify`: set the URL from `--url`. `--gpg-import` appends its fingerprint (sorted, unique), and `--gpg-verify` sets `gpg_verify=True`.
     - Previous bundle (:1038–1040): `trusted = env.get("FAKE_PREVIOUS_TRUSTED_KEY", "CORRECTFPR")`, `keys, signer = [trusted], trusted`, `gpg_verify = env.get("FAKE_PREVIOUS_GPG_VERIFY", "1") == "1"`.
     - `remotes[origin] = {"url": url, "keys": keys, "gpg_verify": gpg_verify}`.
     - Remote install and `update` both reject with `GPG signatures found, but none are in trusted keyring` when `not accepts(remote)`.
     - `update` appends `env["FAKE_UPDATE_ADDS_KEY"]` to the origin's keys when that variable is set.
  2. Add `b'production-test-key': 'PRODUCTIONFPR'` to the fake `gpg --show-keys` map (:1126).
  3. Change the signature to `run_client_gate(self, *extra_args, committed_key="production", **fake_overrides)`:
     - Write `root / "production.gpg"` containing `b"production-test-key"`.
     - Set `AIRLOOM_PUBLIC_KEY_FILE` to that file, or to `correct_key` when `committed_key == "candidate"`.
     - Append `extra_args` to the command.
  4. Add the new tests:
  ```python
  def remote_modifications(self, entries):
      return [e["args"] for e in entries if "remote-modify" in e["args"]]

  def test_production_upgrade_redirects_only_the_previous_origin_url(self):
      result, entries, summary = self.run_client_gate()
      self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
      (modify,) = self.remote_modifications(entries)
      self.assertEqual(modify[:2], ["remote-modify", "--user"])
      self.assertEqual(modify[-1], "airloom-origin")
      self.assertEqual([a for a in modify[2:-1] if a.startswith("--")], [modify[2]])
      self.assertRegex(modify[2], r"^--url=file:///.+/repo$")
      self.assertIn("upgrade_verified previous_version=1.2.2 version=1.2.3", summary)
      self.assertIn("trust=preserved previous_keys=CORRECTFPR", summary)

  def test_production_upgrade_rejects_a_candidate_the_previous_install_does_not_trust(self):
      result, entries, summary = self.run_client_gate(FAKE_PREVIOUS_TRUSTED_KEY="PREVIOUSFPR")
      self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
      self.assertIn("previous installation (trusting PREVIOUSFPR) rejected the candidate release; "
                    "a signing-key rotation needs a reviewed client migration", result.stderr)
      self.assertIn("bundle_verified", summary)
      self.assertIn("repository_install_verified", summary)
      self.assertNotIn("upgrade_verified", summary)
      self.assertFalse(any(a.startswith("--gpg-import") for m in self.remote_modifications(entries) for a in m))

  def test_previous_origin_must_require_signatures_before_the_upgrade(self):
      result, entries, summary = self.run_client_gate(FAKE_PREVIOUS_GPG_VERIFY="0")
      self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
      self.assertIn("previous installation origin airloom-origin does not require GPG signatures", result.stderr)
      self.assertFalse(any("update" in e["args"] for e in entries))

  def test_production_upgrade_must_leave_previous_trust_unchanged(self):
      result, _, summary = self.run_client_gate(FAKE_UPDATE_ADDS_KEY="OTHERFPR")
      self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
      self.assertIn("previous installation trust changed from CORRECTFPR to CORRECTFPR OTHERFPR", result.stderr)
      self.assertNotIn("upgrade_verified", summary)

  def test_development_option_imports_the_candidate_key_into_the_previous_install(self):
      result, entries, summary = self.run_client_gate(
          "--import-candidate-key", FAKE_PREVIOUS_TRUSTED_KEY="PREVIOUSFPR")
      self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
      (modify,) = self.remote_modifications(entries)
      self.assertTrue(any(a.startswith("--gpg-import=") and a.endswith("correct.gpg") for a in modify))
      self.assertTrue(any(a.startswith("--url=file://") for a in modify))
      self.assertIn("trust=candidate-key-imported previous_keys=PREVIOUSFPR", summary)

  def test_development_option_refuses_the_committed_production_key(self):
      result, entries, summary = self.run_client_gate("--import-candidate-key", committed_key="candidate")
      self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
      self.assertIn("--import-candidate-key is development-only and refuses the committed "
                    "production key CORRECTFPR", result.stderr)
      self.assertEqual(entries, [])
      self.assertEqual(summary, "")
  ```
  All existing `ClientVerificationTest` tests stay unchanged and must still pass.
- [ ] **Step 2: Run it and confirm it fails.** Command: `python3 -m unittest tests.test_release.ClientVerificationTest -v`. Expected failures:
  - `..._redirects_only_the_previous_origin_url`: the `remote-modify` args include `--gpg-verify` and `--gpg-import=…`.
  - `..._rejects_a_candidate_the_previous_install_does_not_trust`: return code 0. This is the review's reproduction: the old script imports the new key.
  - `..._must_require_signatures_before_the_upgrade`: return code 0, because `--gpg-verify` turned verification on.
  - `..._must_leave_previous_trust_unchanged`: return code 0.
  - Both `development_option` tests: exit 2 (usage).
  - Every existing test passes against the reworked fake.
- [ ] **Step 3: Implement** in `scripts/verify-flatpak-release.sh`:
  - Parse `--import-candidate-key` (no value; `import_candidate_key=1`, `shift`) and add it to `usage`.
  - Set `project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"` as in `import-signing-key.sh`.
  - After `expected_key` is computed (:205–206) and before `verify_bundle`: when the flag is set, compute `committed_key="$(key_fingerprint "${AIRLOOM_PUBLIC_KEY_FILE:-${project_dir}/packaging/airloom-flatpak.gpg}")"`. Fail if it is empty, and fail with the refusal message if it equals `expected_key`.
  - In `verify_previous_upgrade`, after the previous-URL check:
    1. `remote_requires_signatures "${origin}"`, or fail.
    2. `previous_keys="$(remote_trusted_keys "${origin}")"`.
    3. Production: `flatpak remote-modify --user --url="${repo_url}" "${origin}"`. Development: `flatpak remote-modify --user --gpg-import="${public_key}" --url="${repo_url}" "${origin}"`. Neither form passes `--gpg-verify`.
    4. `flatpak update … || fail "<rotation message>"`.
    5. In production only, re-read `remote_trusted_keys` and `remote_requires_signatures` after the update, and fail on any change.
    6. Append `trust=… previous_keys=${previous_keys:-none}` to the existing `upgrade_verified` evidence.
  - Update the header comment to describe the two modes.
- [ ] **Step 4: Run and confirm it passes.** Command: `python3 -m unittest tests.test_release -v`
- [ ] **Step 5: Commit.** `git add scripts/verify-flatpak-release.sh tests/test_release.py && git commit -m "fix: verify release upgrades against the previous install's own trust"`

### Task 2: Only development runs import the candidate key (finding #19, workflow)

**Files:**
- Modify: `.github/workflows/flatpak.yml` :426–437 (step "Verify signed client installation and upgrade")
- Test: `tests/test_release.py`, `ReleaseWorkflowContractTest`

**Interfaces:**
- Consumes: the `--import-candidate-key` flag from Task 1.
- Produces: the step's env `RELEASE_ELIGIBLE: ${{ needs.release-eligibility.outputs.eligible }}`. The flag is added when that value is `!= "true"`, mirroring the `Generate ephemeral signing key` condition `needs.release-eligibility.outputs.eligible != 'true'`.

- [ ] **Step 1: Write the failing test.** Add a module helper `run_block(step: str) -> str`: `textwrap.dedent` of the text after `        run: |\n` to the end of the step block. Then add:
  ```python
  def test_only_development_runs_import_the_candidate_key_into_the_previous_install(self):
      verify = step_block(self.verify, "Verify signed client installation and upgrade")
      self.assertIn("RELEASE_ELIGIBLE: ${{ needs.release-eligibility.outputs.eligible }}", verify)
      self.assertEqual(self.source.count("--import-candidate-key"), 1)
      self.assertNotIn("--import-candidate-key", self.publish_source)
      script = run_block(verify).replace("${{ matrix.arch }}", "x86_64")
      for eligible, expected in (("true", False), ("false", True), ("", True)):
          with self.subTest(eligible=eligible), tempfile.TemporaryDirectory() as temporary:
              root = Path(temporary)
              stub = root / "scripts" / "verify-flatpak-release.sh"
              stub.parent.mkdir()
              stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGS_OUT"\n', encoding="utf-8")
              stub.chmod(0o755)
              env = dict(os.environ, RELEASE_ELIGIBLE=eligible, EXPECTED_VERSION="1.2.3",
                         PREVIOUS_VERSION="1.2.2", ARGS_OUT=str(root / "args"))
              subprocess.run(["bash", "-c", script], cwd=root, env=env, check=True)
              args = (root / "args").read_text(encoding="utf-8").splitlines()
              self.assertEqual("--import-candidate-key" in args, expected)
              self.assertEqual(args[args.index("--version") + 1], "1.2.3")
              self.assertEqual(args[args.index("--previous-bundle") + 1], "previous/Airloom-x86_64.flatpak")
  ```
- [ ] **Step 2: Run it and confirm it fails.** Command: `python3 -m unittest tests.test_release.ReleaseWorkflowContractTest.test_only_development_runs_import_the_candidate_key_into_the_previous_install -v`. Expected: `AssertionError: 'RELEASE_ELIGIBLE: …' not found`.
- [ ] **Step 3: Implement.**
  - Give the step `env: RELEASE_ELIGIBLE: ${{ needs.release-eligibility.outputs.eligible }}`.
  - Build `args=( --bundle "dist/Airloom-${arch}.flatpak" --previous-bundle "previous/Airloom-${arch}.flatpak" --repo _site/repo --arch "${arch}" --version "${EXPECTED_VERSION}" --previous-version "${PREVIOUS_VERSION}" )`.
  - Then `if [[ "${RELEASE_ELIGIBLE}" != "true" ]]; then args+=(--import-candidate-key); fi`.
  - Then `scripts/verify-flatpak-release.sh "${args[@]}"`.
  - Add a comment explaining that development fixtures are signed by the ephemeral key no release trusts, and that release runs prove the upgrade with the previous install's own trust.
- [ ] **Step 4: Run and confirm it passes.** Command: `python3 -m unittest tests.test_release tests.test_workflows -v`
- [ ] **Step 5: Commit.** `git add .github/workflows/flatpak.yml tests/test_release.py && git commit -m "ci: import the candidate key only for ephemeral-key verification runs"`

### Task 2a: webkit-ui provides XTEST for W3's real-keyboard scenario (cross-workstream, from W3)

W3's keyboard-panning scenario (03-frontend.md, finding #15) sends real key events through X11 XTEST via `ctypes`. It fails rather than skips when `libXtst` is missing. Today nothing in the job's package list pulls it in.

**Files:**
- Modify: `.github/workflows/flatpak.yml:60` (the webkit-ui `dnf install` line).
- Test: `tests/test_workflows.py` (`test_fedora_headless_webkit_job_runs_both_isolated_regressions`, ~185).

- [ ] **Step 1: Write the failing test.** In that test, add `self.assertIn("libXtst", ui_job)`. Run `python3 -m unittest tests.test_workflows -v`; expect FAIL.
- [ ] **Step 2: Implement.** Append `libXtst` to the webkit-ui `dnf install -y …` line. No other workflow change.
- [ ] **Step 3: Run and confirm it passes.** Run the same command; expect PASS. Run `bash -n` on the step and `docker run --rm -v "$PWD":/repo:ro -w /repo docker.io/rhysd/actionlint:latest` (or the podman equivalent); expect no new findings.
- [ ] **Step 4: Commit.** `git add .github/workflows/flatpak.yml tests/test_workflows.py && git commit -m "ci: install libXtst for the real-keyboard UI scenario"`

### Task 3: Release docs: signing policy, failed-tag recovery, asset names (finding #19 docs, decision 5, Minor: RELEASE.md/README asset names)

**Files:**
- Modify: `RELEASE.md` (paragraph at :23 "Before either publication job…", key paragraph at :29, install example :31–35; new section at end), `README.md` (:63–65 reinstall example only)
- Test: `tests/test_release.py`, new class `ReleaseDocumentationTest`

**Interfaces:** Produces the RELEASE.md section `## If a tag run fails`, which the integration publish step follows.

- [ ] **Step 1: Write the failing test.**
  ```python
  RELEASE_GUIDE = PROJECT_ROOT / "RELEASE.md"
  README = PROJECT_ROOT / "README.md"

  class ReleaseDocumentationTest(unittest.TestCase):
      def test_downloaded_bundle_examples_use_published_asset_names(self):
          self.assertIn("bundle: Airloom-${{ matrix.arch }}.flatpak", WORKFLOW.read_text(encoding="utf-8"))
          for path in (RELEASE_GUIDE, README):
              with self.subTest(doc=path.name):
                  text = path.read_text(encoding="utf-8")
                  self.assertIn("./Airloom-ARCH.flatpak", text)
                  for match in re.finditer(r"(\S*)Airloom-VERSION-ARCH\.flatpak", text):
                      self.assertTrue(match.group(1).endswith("dist/"), match.group(0))

      def test_release_guide_documents_failed_tag_recovery(self):
          text = RELEASE_GUIDE.read_text(encoding="utf-8")
          section = text[text.index("## If a tag run fails"):]
          for expected in (
              "gh release view vX.Y.Z --repo ijbaird/airloom",
              "deployments?environment=github-pages&sha=",
              "git push --delete origin vX.Y.Z",
              "git tag -d vX.Y.Z",
              "never move or delete the tag",
              "next patch version",
          ):
              self.assertIn(expected, section)

      def test_release_guide_states_the_signing_key_rotation_policy(self):
          text = RELEASE_GUIDE.read_text(encoding="utf-8")
          self.assertIn("--import-candidate-key", text)
          self.assertIn("only its origin URL is redirected", text)
          self.assertIn("fails the protected upgrade gate until a reviewed client migration", text)
  ```
- [ ] **Step 2: Run it and confirm it fails.** Command: `python3 -m unittest tests.test_release.ReleaseDocumentationTest -v`. Expected:
  - asset test: `'./' does not end with 'dist/'`, or `'./Airloom-ARCH.flatpak' not found`;
  - recovery test: `ValueError: substring not found`;
  - policy test: `'--import-candidate-key' not found`.
- [ ] **Step 3: Implement.**
  - **RELEASE.md, verify-client paragraph.** Add: "Protected releases verify the upgrade as an existing user receives it: the previous release's installation keeps its own keyring and signature settings, and only its origin URL is redirected to the candidate repository. Ordinary CI signs fixtures with an ephemeral key no release trusts, so only those runs pass the development-only `--import-candidate-key` option, and the verifier refuses that option for the committed production key."
  - **RELEASE.md, key paragraph.** Add: "A signing-key rotation therefore fails the protected upgrade gate until a reviewed client migration exists; this is intended."
  - **RELEASE.md, install example.** Change it to `flatpak install --user ./Airloom-ARCH.flatpak`, with the sentence: "GitHub release assets are named `Airloom-x86_64.flatpak` and `Airloom-aarch64.flatpak`; only local builds from `scripts/build-flatpak.sh` are named `dist/Airloom-VERSION-ARCH.flatpak`."
  - **RELEASE.md, new `## If a tag run fails` section.** Contents:
    1. The two checks:
       - `gh release view vX.Y.Z --repo ijbaird/airloom` must report `release not found`.
       - `gh api "repos/ijbaird/airloom/deployments?environment=github-pages&sha=$(git rev-parse 'vX.Y.Z^{commit}')" --jq length` must print `0`.
    2. **Nothing published:** fix via the normal PR path, wait for a green `main` run, then `git push --delete origin vX.Y.Z`, `git tag -d vX.Y.Z`, `git tag -a vX.Y.Z -m "Airloom X.Y.Z" <green-main-commit-sha>`, `git push origin vX.Y.Z`.
    3. **Anything published** (any GitHub release, including a draft or partial one, or a Pages deployment from the tag): "never move or delete the tag", fix forward, and release the next patch version.
  - **README :63–65.** Change it to `./Airloom-ARCH.flatpak`, "using the downloaded file's name". The build-from-source section keeps `dist/Airloom-VERSION-ARCH.flatpak`.
- [ ] **Step 4: Run and confirm it passes.** Command: `python3 -m unittest tests.test_release.ReleaseDocumentationTest -v`
- [ ] **Step 5: Commit.** `git add RELEASE.md README.md tests/test_release.py && git commit -m "docs: state the upgrade-trust policy, tag recovery rule, and real asset names"`

### Task 4: Quote every install destination (finding #20)

**Files:**
- Modify: `Makefile` :24–37
- Test: `tests/test_install.py`, new class `StagedPathQuotingTest`

**Interfaces:** Produces the Make function `shell_quote` and the recursive variable `dest = $(call shell_quote,$(DESTDIR)$(PREFIX))`. No other workstream consumes them.

- [ ] **Step 1: Write the failing test.**
  - Helper `copy_sources(root) -> Path`: copy `Makefile`, `run-installed`, `airloom/` (ignore `__pycache__`), and `packaging/ai.stealthvision.Airloom.{desktop,metainfo.xml,svg}` into `root/"src"`. Install runs there, so an unquoted path's relative fallout lands in the temp dir, never in the checkout.
  - Helper `make(cwd, makefile, target, stage)`: run `["make", "--no-print-directory", "-f", str(makefile), target, f"DESTDIR={stage}", "PREFIX=/usr"]` with `PREFIX`, `DESTDIR`, and `AIRLOOM_PREFIX` removed from the environment, `capture_output=True, text=True`; return the result.
  - `INSTALLED = ("usr/bin/airloom", "usr/share/airloom/airloom/__init__.py", "usr/share/applications/ai.stealthvision.Airloom.desktop", "usr/share/metainfo/ai.stealthvision.Airloom.metainfo.xml", "usr/share/icons/hicolor/scalable/apps/ai.stealthvision.Airloom.svg")`.
  ```python
  def test_uninstall_with_a_spaced_staging_path_leaves_a_sibling_file(self):
      # The review's reproduction: `package stage` next to an unrelated `package`.
      root = ...; sibling = root / "package"; sibling.write_text("keep\n")
      result = make(root, PROJECT_ROOT / "Makefile", "uninstall", root / "package stage")
      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertEqual(sibling.read_text(), "keep\n")

  def test_uninstall_with_a_glob_staging_path_leaves_matching_siblings(self):
      root = ...; launcher = root / "stagea/usr/bin/airloom"; kept = root / "stagea/usr/share/airloom/keep"
      # create both with "keep\n"
      result = make(root, PROJECT_ROOT / "Makefile", "uninstall", root / "stage[ab]*")
      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertEqual(launcher.read_text(), "keep\n")
      self.assertEqual(kept.read_text(), "keep\n")

  def test_install_and_uninstall_round_trip_in_shell_sensitive_staging_paths(self):
      for name in ("package stage", "stage[ab]*", "it's stage"):
          with self.subTest(stage=name), tempfile.TemporaryDirectory() as temporary:
              root = Path(temporary); src = copy_sources(root); stage = root / name
              (root / "package").write_text("keep\n")
              (root / "stagea/usr/bin").mkdir(parents=True); (root / "stagea/usr/bin/airloom").write_text("keep\n")
              other = stage / "usr/share/applications/other.desktop"
              other.parent.mkdir(parents=True); other.write_text("keep\n")
              self.assertEqual(make(src, src / "Makefile", "install", stage).returncode, 0)
              for relative in INSTALLED:
                  self.assertTrue((stage / relative).is_file(), relative)
              self.assertEqual(make(src, src / "Makefile", "uninstall", stage).returncode, 0)
              for relative in INSTALLED:
                  self.assertFalse((stage / relative).exists(), relative)
              self.assertFalse((stage / "usr/share/airloom").exists())
              for survivor in (other, root / "package", root / "stagea/usr/bin/airloom"):
                  self.assertEqual(survivor.read_text(), "keep\n")

  def test_every_destination_is_quoted_and_deletions_end_options(self):
      recipes = [l for l in (PROJECT_ROOT / "Makefile").read_text().splitlines() if l.startswith("\t")]
      self.assertFalse([l for l in recipes if "$(DESTDIR)" in l or "$(PREFIX)" in l])
      for line in (l for l in recipes if l.startswith("\trm ")):
          self.assertIn(" -- $(dest)/", line)
  ```
- [ ] **Step 2: Run it and confirm it fails.** Command: `python3 -m unittest tests.test_install.StagedPathQuotingTest -v`. Expected:
  - spaced: `FileNotFoundError` reading `package` (the sibling was deleted);
  - glob: the `stagea` sentinels are deleted;
  - round trip: `it's stage` fails install with a shell quote error, and the other two lose their siblings;
  - static: `$(DESTDIR)` is found in recipes.
- [ ] **Step 3: Implement.**
  - Add before `install:` a two-line comment and the definitions:
    ```make
    shell_quote = '$(subst ','\'',$(1))'
    dest = $(call shell_quote,$(DESTDIR)$(PREFIX))
    ```
  - Replace every `$(DESTDIR)$(PREFIX)` in recipes with `$(dest)`.
  - Use `install -d -- $(dest)/share/airloom`, `tar … -C $(dest)/share/airloom`, `install -Dm755 -- run-installed $(dest)/bin/airloom`, and `install -Dm644 -- <src> $(dest)/…` for each file.
  - Use `rm -f -- $(dest)/…` and `rm -rf -- $(dest)/share/airloom`.
- [ ] **Step 4: Run and confirm it passes.** Command: `python3 -m unittest tests.test_install -v`
- [ ] **Step 5: Commit.** `git add Makefile tests/test_install.py && git commit -m "fix: quote install and uninstall destinations"`

### Task 5: Skip bot senders and actors before checkout in `claude.yml` (Minor: `claude.yml` bot guard)

**Files:**
- Modify: `.github/workflows/claude.yml` :14–19 (job `claude` `if:`)
- Test: `tests/test_workflows.py` (`context_value` :147, `evaluate_condition` :154, new test after `test_claude_review_runs_only_for_same_repository_human_prs`)

**Interfaces:** Changes test helpers in `tests/test_workflows.py`:
- `context_value` now returns `None` for a missing key or a `None` parent, matching GitHub's null propagation.
- `evaluate_condition` provides case-insensitive `contains(str|None, str)` and `endsWith(str|None, str)`, both false for `None`.
- The existing `claude-review` test is unchanged.

- [ ] **Step 1: Write the failing test.**
  ```python
  MENTION_FIELDS = {"issue_comment": ("comment", "body"), "pull_request_review_comment": ("comment", "body"),
                    "pull_request_review": ("review", "body"), "issues": ("issue", "body")}

  def mention_context(event_name, *, actor, sender_type, text, field=None):
      obj, default_field = MENTION_FIELDS[event_name]
      return {"github": {"event_name": event_name, "actor": actor, "repository": "ijbaird/airloom",
                         "event": {"sender": {"type": sender_type}, obj: {field or default_field: text}}}}

  def test_claude_mentions_run_only_for_human_actors_on_every_event(self):
      source = workflow("claude.yml")
      on_block = re.search(r"(?ms)^on:\n(.*?)^\S", source).group(1)
      self.assertEqual(set(re.findall(r"(?m)^  ([a-z_]+):$", on_block)), set(MENTION_FIELDS))
      job = job_block(source, "claude")
      condition = job_condition(job)
      self.assertTrue(condition, "claude needs a job-level skip condition")
      self.assertLess(job.index("if: |"), job.index("actions/checkout@"))
      scenarios = (
          ("human mention", "octocat", "User", "@claude please look", True),
          ("human without mention", "octocat", "User", "looks good", False),
          ("bot sender and actor", "helper-app[bot]", "Bot", "@claude run", False),
          ("bot actor, user-typed sender", "helper-app[bot]", "User", "@claude run", False),
          ("bot sender, human actor", "octocat", "Bot", "@claude run", False),
      )
      for event_name in MENTION_FIELDS:
          for name, actor, sender_type, text, expected in scenarios:
              with self.subTest(event=event_name, scenario=name):
                  context = mention_context(event_name, actor=actor, sender_type=sender_type, text=text)
                  self.assertIs(evaluate_condition(condition, context), expected)
      title = mention_context("issues", actor="octocat", sender_type="User", text="@claude help", field="title")
      self.assertIs(evaluate_condition(condition, title), True)
  ```
- [ ] **Step 2: Run it and confirm it fails.** Command: `python3 -m unittest tests.test_workflows -v`. Expected: the bot subtests fail with `AssertionError: True is not False` for all four events, and `test_claude_review_runs_only_for_same_repository_human_prs` still passes.
- [ ] **Step 3: Implement.**
  - Prefix the job `if: |` with `github.event.sender.type != 'Bot' &&` and `!endsWith(github.actor, '[bot]') &&`, then wrap the four existing mention clauses in one parenthesized group.
  - Add a comment like the one in `claude-code-review.yml`: "The action needs an OAuth secret. Skip before checkout for bot senders and bot actors on every mention event; the pinned action's own human-actor check stays as a second layer."
- [ ] **Step 4: Run and confirm it passes.** Command: `python3 -m unittest tests.test_workflows -v`
- [ ] **Step 5: Commit.** `git add .github/workflows/claude.yml tests/test_workflows.py && git commit -m "ci: skip bot senders and actors before the Claude mention workflow checks out"`

### Task 6: `descendants()` in `make-screenshots` tolerates only vanished processes (Minor: `descendants()` in `scripts/make-screenshots`)

**Files:**
- Modify: `scripts/make-screenshots` :168–184
- Test: `tests/test_debug_scripts.py`, `DebugScriptIsolationTest` (`OWNER_DRIVER` :495–546, new tests after :633)

**Interfaces:** `descendants(pid: int) -> list[int]` keeps its signature. It now raises any `/proc` `OSError` other than `FileNotFoundError` or `ProcessLookupError`. `stop_descendants` lets the error propagate, so `main`'s `attempt("stopping leftover processes", …)` records it as a cleanup problem. That rule matches `proc_bytes` in `scripts/test-ui-regression` (W3's file; not edited here).

- [ ] **Step 1: Write the failing test.**
  - Load the module with `SourceFileLoader` exactly as `test_mountinfo_paths_decode_octal_escapes_without_mangling_unicode` does. Then:
  ```python
  def test_screenshot_descendants_skip_only_vanished_processes_and_tasks(self):
      tasks = {"/proc/100/task": ["100", "101"], "/proc/200/task": FileNotFoundError(2, "gone")}
      children = {"/proc/100/task/100/children": "200 ", "/proc/100/task/101/children": ProcessLookupError(3, "gone")}
      # patch module.os.listdir and Path.read_text to serve these, raising exception values
      self.assertEqual(module.descendants(100), [200])

  def test_screenshot_descendants_surface_other_proc_errors(self):
      for where in ("listdir", "children"):
          for error in (PermissionError(13, "Permission denied"), OSError(5, "I/O error")):
              with self.subTest(where=where, error=type(error).__name__):
                  # raise `error` from that one call; everything else as above
                  with self.assertRaises(type(error)) as raised:
                      module.descendants(100)
                  self.assertEqual(raised.exception.errno, error.errno)

  def test_screenshot_cleanup_fails_when_process_enumeration_is_denied(self):
      outcome, stderr, roots = self._run_owner(mode="proc-denied")
      self.assertEqual(outcome["result"], "AssertionError", stderr)
      self.assertIn("cleanup was incomplete", outcome["message"])
      self.assertIn("stopping leftover processes: ", stderr)
      self.assertEqual(roots, [])
  ```
  - In `OWNER_DRIVER`, add `import fnmatch` and add `'proc-denied'` to the `mode in ('real', 'launch-fail')` tuple. Under `if mode == 'proc-denied':`, wrap `Path.read_text` so paths matching `fnmatch(str(path), '/proc/*/task/*/children')` raise `PermissionError(13, 'Permission denied', str(path))`.
- [ ] **Step 2: Run it and confirm it fails.** Command: `python3 -m unittest tests.test_debug_scripts -k descendants -k enumeration -v`. Expected:
  - the surface test fails with `PermissionError not raised` / `OSError not raised`;
  - the cleanup test fails with `'success' != 'AssertionError'`;
  - the skip test passes on old code, and stays as a guard.
- [ ] **Step 3: Implement.** Change both `except OSError:` clauses in `descendants` to `except (FileNotFoundError, ProcessLookupError):`. Keep `Path(...).read_text()` for the children file. Add a docstring saying that a process or task that exits mid-walk is skipped, and that any other error propagates so cleanup never reports a partial walk as complete.
- [ ] **Step 4: Run and confirm it passes.** Command: `python3 -m unittest tests.test_debug_scripts -v`
- [ ] **Step 5: Commit.** `git add scripts/make-screenshots tests/test_debug_scripts.py && git commit -m "fix: surface /proc errors other than a vanished process in screenshot cleanup"`

### Task 7: Pre-tag production replay on real artifacts (finding #19, pre-tag proof)

This task runs Tasks 1–2's final script on published artifacts; it adds no repository files. Downloads go in their own directories, and driver scripts go in a separate directory. Every container mounts the source, artifacts, and mirror **read-only**, and the replay containers have no writable mount.

**Files:** none committed. Scratch: `R=/tmp/opencode/w4-replay` (`artifacts/{prev,cand}`, `pages/`, `negative/`, `out/`); drivers in `D=/tmp/opencode/w4-replay-scripts`.
**Interfaces:**
- Consumes: the `verify-flatpak-release.sh` modes and evidence from Task 1.
- Produces: logs in `$R/out/`, and a "Pre-tag production replay" section in the W4 report.

- [ ] **Step 1: Prove the tooling is real Docker and the pinned image.**
  ```bash
  WT=/home/ian/.local/share/opencode/worktree/460fa0/astra-v0-14-0   # the W4 worktree at its final commit
  IMG=ghcr.io/flathub-infra/flatpak-github-actions@sha256:1de59efef01a946d9f28f1ec3458bf6f61dcee3507d7fecdd473f8134da4ec77
  R=/tmp/opencode/w4-replay; D=/tmp/opencode/w4-replay-scripts
  rm -rf "$R" "$D"; mkdir -p "$R"/artifacts/prev "$R"/artifacts/cand "$R"/pages "$R"/negative "$R"/out "$D"
  { docker --version; docker version --format 'server={{.Server.Version}} os={{.Server.Os}}/{{.Server.Arch}}';
    docker image inspect "$IMG" --format '{{index .RepoDigests 0}}';
    docker run --rm "$IMG" flatpak --version;
    git -C "$WT" rev-parse HEAD; sha256sum "$WT/scripts/verify-flatpak-release.sh"; } | tee "$R/out/environment.txt"
  ! grep -qi podman "$R/out/environment.txt"
  ```
  Pass when: no `podman`, the RepoDigest equals `$IMG`, and the output shows `Flatpak 1.18.1`.
- [ ] **Step 2: Download the published bundles read-only and bind them.**
  ```bash
  gh release download v0.12.3 -R ijbaird/airloom -p Airloom-x86_64.flatpak -p SHA256SUMS -D "$R/artifacts/prev"
  gh release download v0.13.0 -R ijbaird/airloom -p Airloom-x86_64.flatpak -p Airloom-x86_64.provenance.json -p SHA256SUMS -D "$R/artifacts/cand"
  (cd "$R/artifacts/prev" && sha256sum --check --ignore-missing SHA256SUMS) | tee "$R/out/checksums.txt"
  (cd "$R/artifacts/cand" && sha256sum --check --ignore-missing SHA256SUMS) | tee -a "$R/out/checksums.txt"
  python3 -I "$WT/scripts/check-release-provenance.py" --source-sha "$(git -C "$WT" rev-parse 'v0.13.0^{commit}')" \
    --provenance "$R/artifacts/cand/Airloom-x86_64.provenance.json" --bundle "$R/artifacts/cand/Airloom-x86_64.flatpak" | tee -a "$R/out/checksums.txt"
  chmod -R a-w "$R/artifacts"
  curl -fsS https://ijbaird.github.io/airloom/repo/refs/heads/app/ai.stealthvision.Airloom/x86_64/stable | tee "$R/out/pages-x86_64-ref.txt"
  ```
- [ ] **Step 3: Mirror the published Pages repository.**
  1. Write `$D/read-summary-index.py`. It parses argv[1] as `GLib.Variant.new_from_bytes(GLib.VariantType("(a{s(ayaaya{sv})}a{sv})"), GLib.Bytes.new(data), False)` and prints `bytes(digest).hex()` for each entry of element 0. This format was verified against the live `summary.idx`, which lists `x86_64` and `aarch64`.
  2. Write `$D/mirror-pages.sh` (`set -euo pipefail`):
     - `ostree init --repo=/pages/repo --mode=archive-z2`
     - `ostree remote add --repo=/pages/repo --gpg-import=/src/packaging/airloom-flatpak.gpg --set=gpg-verify-summary=true pages https://ijbaird.github.io/airloom/repo`
     - `ostree pull --repo=/pages/repo --mirror pages`. This mirrors all refs, with gpg-verified commits plus the published `summary` and `summary.sig`.
     - `curl -fsS -o` each of `summary.idx` and `summary.idx.sig` into the repo.
     - `mkdir -p /pages/repo/summaries`, then `python3 -P -s /replay/read-summary-index.py /pages/repo/summary.idx | while read -r d; do curl -fsS -o "/pages/repo/summaries/$d.gz" "https://ijbaird.github.io/airloom/repo/summaries/$d.gz"; done`
     - `ostree remote delete --repo=/pages/repo pages`
     - `ostree rev-parse --repo=/pages/repo app/ai.stealthvision.Airloom/x86_64/stable`
  3. Run it:
  ```bash
  docker run --rm --security-opt label=disable --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$WT:/src:ro" -v "$D:/replay:ro" -v "$R/pages:/pages" "$IMG" bash /replay/mirror-pages.sh | tee "$R/out/mirror.txt"
  test "$(tail -n1 "$R/out/mirror.txt")" = "$(cat "$R/out/pages-x86_64-ref.txt")"
  chmod -R a-w "$R/pages"
  ```
  Pass when: the mirrored x86_64 commit equals the live Pages ref.
- [ ] **Step 4: Positive replay, production mode (must pass).**
  ```bash
  docker run --rm --privileged -v "$WT:/src:ro" -v "$R/artifacts:/artifacts:ro" -v "$R/pages:/pages:ro" \
    -e FLATPAK_GPG_PUBLIC_KEY=/src/packaging/airloom-flatpak.gpg -w /src "$IMG" \
    scripts/verify-flatpak-release.sh --bundle /artifacts/cand/Airloom-x86_64.flatpak \
      --previous-bundle /artifacts/prev/Airloom-x86_64.flatpak --repo /pages/repo \
      --arch x86_64 --version 0.13.0 --previous-version 0.12.3 > "$R/out/positive.log" 2>&1; echo "exit=$?" >> "$R/out/positive.log"
  PROD=$(docker run --rm -v "$WT:/src:ro" "$IMG" bash -c 'GNUPGHOME=$(mktemp -d) gpg --batch --with-colons --show-keys /src/packaging/airloom-flatpak.gpg | awk -F: "/^fpr:/{print \$10; exit}"')
  ```
  Pass when all of the following hold:
  - the log ends `exit=0`;
  - it contains `bundle_verified … key=$PROD version=0.13.0`, `repository_install_verified … version=0.13.0`, and `upgrade_verified previous_version=0.12.3 version=0.13.0 … current_commit=<pages ref> trust=preserved previous_keys=$PROD`;
  - it contains `wrong_key_rejected` and `client_verification_complete arch=x86_64 version=0.13.0`;
  - `grep -c -- '--gpg-file' "$WT/scripts/verify-flatpak-release.sh"` prints `0`.
- [ ] **Step 5: Build the negative fixture: the candidate re-signed with a throwaway key.** Write `$D/build-negative.sh` (`set -euo pipefail`; work dir `/negative`):
  1. `cp -a /pages/repo /negative/repo`, and create a fresh 0700 `GNUPGHOME=/negative/gnupg`.
  2. `gpg --batch --pinentry-mode loopback --passphrase '' --quick-generate-key "Airloom Replay Throwaway <throwaway@example.invalid>" ed25519 sign 1d`. Set `THROW` to its fingerprint, export it to `/negative/throwaway.gpg`, and set `PROD` from `/src/packaging/airloom-flatpak.gpg` using a scratch GnuPG home.
  3. For every `app/` ref in `ostree refs --repo=/negative/repo`, with `c=$(ostree rev-parse …)`, run:
     - `ostree gpg-sign --repo=/negative/repo --delete "$c" "$PROD"`
     - `ostree gpg-sign --repo=/negative/repo --gpg-homedir="$GNUPGHOME" "$c" "$THROW"`
  4. `flatpak build-update-repo --no-update-appstream --gpg-sign="$THROW" --gpg-homedir="$GNUPGHOME" /negative/repo`
  5. `flatpak build-bundle /negative/repo /negative/Airloom-x86_64.flatpak ai.stealthvision.Airloom stable --arch=x86_64 --repo-url=https://ijbaird.github.io/airloom/repo --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo --gpg-keys=/negative/throwaway.gpg`
  6. Sanity-check the x86_64 commit with the script's own `ostree init` / `remote add --gpg-import=KEY` / `pull-local --gpg-verify --commit-metadata-only` technique: it must verify with `throwaway.gpg` and must **fail** with the production key. Exit 1 otherwise.
  7. Print `throwaway=$THROW production=$PROD commit=$c`.

  Run it:
  ```bash
  docker run --rm --security-opt label=disable --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$WT:/src:ro" -v "$D:/replay:ro" -v "$R/pages:/pages:ro" -v "$R/negative:/negative" "$IMG" \
    bash /replay/build-negative.sh | tee "$R/out/negative-build.txt"
  chmod -R a-w "$R/negative"
  ```
- [ ] **Step 6: Negative replay, production mode (must fail at the upgrade).**
  ```bash
  neg() { docker run --rm --privileged -v "$1:/verify.sh:ro" -v "$WT:/src:ro" -v "$R/artifacts:/artifacts:ro" \
    -v "$R/negative:/negative:ro" -e FLATPAK_GPG_PUBLIC_KEY=/negative/throwaway.gpg -w /src "$IMG" \
    /verify.sh --bundle /negative/Airloom-x86_64.flatpak --previous-bundle /artifacts/prev/Airloom-x86_64.flatpak \
      --repo /negative/repo --arch x86_64 --version 0.13.0 --previous-version 0.12.3 "${@:2}"; }
  neg "$WT/scripts/verify-flatpak-release.sh" > "$R/out/negative.log" 2>&1; echo "exit=$?" >> "$R/out/negative.log"
  ```
  Pass when all of the following hold:
  - the exit is nonzero;
  - the log contains `bundle_verified … key=<THROW>` and `repository_install_verified`;
  - it contains `client verification denied: previous installation (trusting $PROD) rejected the candidate release; a signing-key rotation needs a reviewed client migration`;
  - it contains no `upgrade_verified`.
- [ ] **Step 7: Controls showing the rejection comes from trust preservation, not a broken fixture.**
  ```bash
  neg "$WT/scripts/verify-flatpak-release.sh" --import-candidate-key > "$R/out/negative-dev-control.log" 2>&1; echo "exit=$?" >> "$R/out/negative-dev-control.log"
  git -C "$WT" show f1f811d:scripts/verify-flatpak-release.sh > "$D/verify-f1f811d.sh"; chmod 755 "$D/verify-f1f811d.sh"
  neg "$D/verify-f1f811d.sh" > "$R/out/negative-prefix-baseline.log" 2>&1; echo "exit=$?" >> "$R/out/negative-prefix-baseline.log"
  ```
  Pass when:
  - both logs end `exit=0`;
  - the dev control shows `trust=candidate-key-imported previous_keys=$PROD`;
  - the pre-fix baseline shows `upgrade_verified`, reproducing finding 19 on real artifacts.

  If either control fails, stop and report: the negative fixture would not be isolating the trust path.
- [ ] **Step 8: Record in the W4 report** under "Pre-tag production replay":
  - the contents of `environment.txt`, `checksums.txt`, `pages-x86_64-ref.txt`, the `mirror.txt` tail, and `negative-build.txt`;
  - the evidence lines and the exit line from all four logs;
  - the following statement of scope, verbatim.

  **Proves:**
  1. On real flatpak 1.18.1, in the pinned image under real Docker with `--privileged`, an install of the published production-signed v0.12.3 bundle upgrades to the published production-signed v0.13.0 repository content. Its keyring and signature settings are unchanged; only its origin URL is redirected. This is the production path the v0.14.0 tag run takes.
  2. The production gate rejects, at the upgrade stage, a candidate whose signing key the previous install doesn't trust, even when the candidate's own bundle and repository verify.
  3. The same fixture passes with candidate-key import (both the development option and the pre-fix script), so the rejection is caused by trust preservation.
  4. The mirror's signed metadata and x86_64 commit matched live Pages at replay time.

  **Does not prove:**
  - that the v0.14.0 artifacts pass (the tag run's `verify-client` is that gate);
  - aarch64 (the host replays x86_64 only);
  - HTTPS/Pages CDN transport and static deltas (the mirror pulls objects over `file://`);
  - GitHub-hosted runner differences, other flatpak versions, GNOME Software, or system installations;
  - the v0.13.0→v0.14.0 pair specifically.
- [ ] **Step 9: Commit.** Nothing to commit. Confirm `git -C "$WT" status --short` is empty for this task.

### Task 8: Workstream verification

- [ ] **Step 1:** `make check PYTHON="python3 -W error::ResourceWarning"`. Expected: all tests OK, `node --test` passes.
- [ ] **Step 2:** `python3 -m unittest tests.test_release tests.test_workflows tests.test_install tests.test_debug_scripts -v`. Expected: OK, with no skips beyond the pre-existing ones.
- [ ] **Step 3:** Run a real screenshot cleanup as a non-root user, outputting to a temp dir (not `packaging/screenshots/`): `out="$(mktemp -d)"; python3 scripts/make-screenshots --output "$out"`. Expected: `PASS: screenshots in …`, and no `warning:` lines on stderr.
- [ ] **Step 4:** Re-run Task 7 Steps 1–8 against the W4 worktree's final commit, if any commit landed after the earlier replay. Expected: the same pass and fail outcomes, with `environment.txt` showing the final `HEAD`.
- [ ] **Step 5:** `git diff --stat astra-v0-14-0...HEAD`. It must list only:
  - `scripts/verify-flatpak-release.sh`, `scripts/make-screenshots`
  - `.github/workflows/flatpak.yml`, `.github/workflows/claude.yml`
  - `Makefile`, `RELEASE.md`, `README.md`
  - `tests/test_release.py`, `tests/test_workflows.py`, `tests/test_install.py`, `tests/test_debug_scripts.py`

## Cross-workstream contracts

- **Integration (release task and publish):**
  - Follow RELEASE.md `## If a tag run fails` (decision 5).
  - The 0.14.0 CHANGELOG must state that production upgrade verification no longer imports the candidate key, so a key rotation fails the gate until a reviewed client migration exists.
  - Integrated verification step 4 ("a real `verify-flatpak-release.sh` run in the pinned image with real artifacts") reruns Task 7 on the integrated branch before tagging.
- **Integration (screenshots):** regenerating screenshots uses `scripts/make-screenshots`. W4 changes only its `/proc` error handling: a denied or I/O-failing enumeration now fails the run as "cleanup was incomplete".
- **W3:** owns the keyboard-panning and preview README text outside the install sections. W4 edits only README :63–65. `scripts/test-ui-regression` `descendants()` is W3's (spec Minor); W4 does not touch it, and Task 6 uses the same vanished-only rule as its `proc_bytes`.
- **None consumed from W1–W3.**

## Self-review

- **Spec coverage:**
  - 19, URL-only production mode, asserted signature settings, dev-only import, negative fixture, `key_ok` fix → Task 1
  - 19, workflow selection from release eligibility → Task 2
  - 19, RELEASE.md rotation policy → Task 3
  - 19, pre-tag replay (positive and negative, with controls) → Task 7
  - 20, Makefile quoting with `--`, spaced/glob/quote paths, sibling sentinels, install and uninstall → Task 4
  - Decision 5, recovery rule → Task 3
  - Minor, asset names → Task 3
  - Minor, `claude.yml` guard plus contract over all four events → Task 5
  - Minor, `make-screenshots` `descendants()` → Task 6
- **Type consistency:**
  - The flag `--import-candidate-key` is identical in the script (Task 1), the workflow and its test (Task 2), RELEASE.md (Task 3), and the replay (Task 7).
  - The evidence `trust=preserved|candidate-key-imported previous_keys=…` is identical in Tasks 1 and 7.
  - The rotation message is identical in Tasks 1 and 7.
  - `RELEASE_ELIGIBLE` uses the same name as the existing "Select previous supported release" step.
- **Review Focus coverage:**
  1. Task 1 `…rejects_a_candidate_the_previous_install_does_not_trust`, plus Task 7 Step 6
  2. Task 2 executable eligibility test, plus Task 1 committed-key refusal
  3. Task 1 `…must_require_signatures_before_the_upgrade`
  4. Task 4 three `StagedPathQuotingTest` path cases
  5. Task 5 "bot actor, user-typed sender" subtests across all four events
