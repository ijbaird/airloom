import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
WORKFLOWS = PROJECT / ".github" / "workflows"
CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
DEPLOY_PAGES_SHA = "368f82528645a54fb793d4d04e342629a3f51346"
FLATPAK_BUILDER_SHA = "79327416609af08178ad73b352877e51450790b3"
TURNSTYLE_SHA = "3805450be63b8c80577dc253e8c17e9132036df4"
FLATPAK_IMAGE = "ghcr.io/flathub-infra/flatpak-github-actions@sha256:"


SANDBOX_VARIABLE = "WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS"
UI_SCRIPT_STEPS = ("Run isolated WebKit UI regressions", "Run cross-layer UI regression")
# Only these repository-root directories are exempt; nested directories with
# the same names ship with the app. Bytecode caches mirror scanned sources.
SANDBOX_EXEMPT_ROOTS = {".git", "tests", "docs"}


def repository_files(root):
    """Every file the repository would ship, tracked or new, minus root tests/ and docs/.

    With git, ignored local-only files (build output, agent notes) are
    skipped; a pristine export has no git and no ignored files to skip.
    """
    if (root / ".git").exists():
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            check=True, capture_output=True,
        ).stdout.decode().split("\0")
        paths = [root / name for name in listed if name]
    else:
        paths = [Path(top) / name for top, _dirs, names in os.walk(root) for name in names]
    for path in paths:
        relative = path.relative_to(root)
        if relative.parts[0] in SANDBOX_EXEMPT_ROOTS or "__pycache__" in relative.parts:
            continue
        if path.is_file():
            yield path


def permitted_sandbox_step(relative, lines, index):
    """Name of the webkit-ui UI-script step whose env: sets the variable on
    lines[index], or None when that occurrence is anywhere else."""
    if relative != Path(".github/workflows/flatpak.yml"):
        return None
    if lines[index] != f'          {SANDBOX_VARIABLE}: "1"':
        return None
    # The enclosing step's env: block: only deeper-indented lines or comments.
    cursor = index - 1
    while cursor >= 0 and (lines[cursor].startswith(" " * 10) or lines[cursor].strip().startswith("#")):
        cursor -= 1
    if cursor < 0 or lines[cursor] != "        env:":
        return None
    # The enclosing step and job.
    step_start = cursor
    while step_start >= 0 and not lines[step_start].startswith("      - "):
        if lines[step_start] and not lines[step_start].startswith(" " * 8):
            return None
        step_start -= 1
    job_start = step_start
    while job_start >= 0 and not re.match(r"^  \S", lines[job_start]):
        if re.match(r"^\S", lines[job_start]):
            return None
        job_start -= 1
    if step_start < 0 or job_start < 0 or lines[job_start] != "  webkit-ui:":
        return None
    step_end = step_start + 1
    while step_end < len(lines) and lines[step_end].startswith(" " * 8):
        step_end += 1
    for line in lines[step_start:step_end]:
        name = re.match(r"^      (?:- |  )name: (.*)$", line)
        if name and name.group(1).strip() in UI_SCRIPT_STEPS:
            return name.group(1).strip()
    return None


def sandbox_variable_occurrences(root):
    """(permitted step names, other locations) for every occurrence of the
    variable anywhere in the repository outside tests/ and docs/."""
    permitted, elsewhere = [], []
    for path in repository_files(root):
        data = path.read_bytes()
        if SANDBOX_VARIABLE.encode() not in data:
            continue
        relative = path.relative_to(root)
        lines = data.decode("utf-8", errors="replace").splitlines()
        found = False
        for index, line in enumerate(lines):
            if SANDBOX_VARIABLE not in line:
                continue
            found = True
            step = permitted_sandbox_step(relative, lines, index)
            if step:
                permitted.append(step)
            else:
                elsewhere.append(f"{relative}:{index + 1}")
        if not found:  # e.g. split across a binary boundary
            elsewhere.append(str(relative))
    return sorted(permitted), elsewhere


def workflow(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def job_block(source, job_name):
    match = re.search(
        rf"(?ms)^  {re.escape(job_name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        source,
    )
    return match.group(1) if match else ""


def job_condition(block):
    match = re.search(r"(?m)^    if: \|\n((?:^      [^\n]*(?:\n|$))+)", block)
    return " ".join(line.strip() for line in match.group(1).splitlines()) if match else ""


def job_needs(source, job_name):
    match = re.search(r"(?m)^    needs: \[([^]]*)\]$", job_block(source, job_name))
    return [item.strip() for item in match.group(1).split(",")] if match else []


def job_ancestors(source, job_name):
    ancestors = set()
    pending = list(job_needs(source, job_name))
    while pending:
        ancestor = pending.pop()
        if ancestor in ancestors:
            continue
        ancestors.add(ancestor)
        pending.extend(job_needs(source, ancestor))
    return ancestors


def context_value(context, path):
    value = context
    for part in path.split("."):
        value = value[part]
    return value


def evaluate_condition(expression, context):
    """Evaluate the boolean operators and github.* values used by job gates."""
    if not expression:
        return False
    paths = re.findall(r"\bgithub(?:\.[A-Za-z_][A-Za-z0-9_]*)+", expression)
    for path in sorted(paths, key=len, reverse=True):
        expression = expression.replace(path, repr(context_value(context, path)))
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", " not ", expression)
    return bool(eval(expression, {"__builtins__": {}}, {}))


def pull_request_context(*, actor, author_type, head_repository):
    return {
        "github": {
            "actor": actor,
            "repository": "ijbaird/airloom",
            "event": {
                "sender": {"type": "Bot" if actor.endswith("[bot]") else "User"},
                "pull_request": {
                    "user": {"type": author_type},
                    "head": {"repo": {"full_name": head_repository}},
                }
            },
        }
    }


class WorkflowContractTest(unittest.TestCase):
    def test_test_job_runs_the_complete_make_check_gate(self):
        test_job = job_block(workflow("flatpak.yml"), "test")
        self.assertIn("make check", test_job)

    def test_fedora_headless_webkit_job_runs_both_isolated_regressions(self):
        ui_job = job_block(workflow("flatpak.yml"), "webkit-ui")
        self.assertTrue(ui_job, "webkit-ui job is missing")
        self.assertRegex(ui_job, r"(?m)^      image: fedora:\d+$")
        self.assertIn("webkitgtk6.0", ui_job)
        self.assertIn("gobject-introspection", ui_job)
        self.assertIn("xorg-x11-server-Xvfb", ui_job)
        self.assertIn("xvfb-run", ui_job)
        self.assertIn("scripts/test-heatmap", ui_job)
        self.assertIn("scripts/test-save-refresh", ui_job)

        for script_name in ("test-heatmap", "test-save-refresh"):
            with self.subTest(script=script_name):
                script = (PROJECT / "scripts" / script_name).read_text(encoding="utf-8")
                self.assertIn('env["XDG_CONFIG_HOME"] = str(workdir / "config")', script)
                self.assertIn('env["XDG_CACHE_HOME"] = str(workdir / "cache")', script)

    def test_headless_webkit_job_has_no_live_api_credentials(self):
        ui_job = job_block(workflow("flatpak.yml"), "webkit-ui")
        self.assertTrue(ui_job, "webkit-ui job is missing")
        self.assertNotIn("secrets.", ui_job)
        self.assertNotRegex(ui_job, r"(?i)purpleair.*api.*key|api.*key.*purpleair")

    def test_webkit_ui_is_an_ancestor_of_both_publication_channels(self):
        source = workflow("flatpak.yml")
        coordinator = job_block(source, "publication")
        self.assertIn("webkit-ui", job_ancestors(source, "publication"))
        self.assertIn("uses: ./.github/workflows/publish.yml", coordinator)

        publish_path = WORKFLOWS / "publish.yml"
        publish = publish_path.read_text(encoding="utf-8") if publish_path.exists() else ""
        self.assertTrue(job_block(publish, "release"), "GitHub release channel is missing")
        self.assertTrue(job_block(publish, "publish-repo"), "Pages channel is missing")
        self.assertEqual(job_needs(publish, "publish-repo"), ["release"])

    def test_webkit_ui_gates_publication_on_the_cross_layer_regression(self):
        source = workflow("flatpak.yml")
        ui_job = job_block(source, "webkit-ui")
        self.assertTrue(ui_job, "webkit-ui job is missing")
        install = re.search(r"(?m)^\s*run: dnf install -y (.*)$", ui_job)
        self.assertTrue(install, "webkit-ui must install its Fedora packages")
        packages = install.group(1).split()
        # dbus-daemon provides dbus-run-session on Fedora.
        for package in ("dbus-daemon", "xorg-x11-server-Xvfb", "xorg-x11-xauth", "webkitgtk6.0"):
            with self.subTest(package=package):
                self.assertIn(package, packages)

        commands = [
            line.strip()
            for line in ui_job.splitlines()
            if "scripts/test-ui-regression" in line and not line.strip().startswith("#")
        ]
        self.assertEqual(len(commands), 1, commands)
        # A private session bus outside Xvfb, with the script itself under Xvfb.
        self.assertRegex(
            commands[0],
            r"^(?:runuser -u \S+ -- env (?:[A-Z_]+=\S+ )+)?dbus-run-session -- xvfb-run -a (?:-s \"[^\"]*\" )?"
            r"(?:env (?:[A-Z_]+=\S+ )+)?python3 scripts/test-ui-regression$",
        )
        self.assertIn("webkit-ui", job_needs(source, "publication"))

    def test_cross_layer_regression_runs_unprivileged_with_its_own_writable_dirs(self):
        # Root ignores directory permissions, so save-failure checks need a
        # regular user; it gets a writable copy of the checkout and runtime dir.
        ui_job = job_block(workflow("flatpak.yml"), "webkit-ui")
        install = re.search(r"(?m)^\s*run: dnf install -y (.*)$", ui_job)
        packages = install.group(1).split() if install else []
        for package in ("util-linux", "shadow-utils"):  # runuser, useradd
            with self.subTest(package=package):
                self.assertIn(package, packages)

        command = re.search(
            r"(?m)^\s*runuser -u (?P<user>[a-z_][a-z0-9_-]*) -- env (?P<env>(?:[A-Z_]+=\S+ )+)"
            r"dbus-run-session -- xvfb-run -a (?:-s \"[^\"]*\" )?"
            r"(?:env (?:[A-Z_]+=\S+ )+)?python3 scripts/test-ui-regression$",
            ui_job,
        )
        self.assertTrue(command, "test-ui-regression must run via runuser on a private bus under Xvfb")
        user = command.group("user")
        self.assertNotEqual(user, "root")
        self.assertRegex(command.group("env"), r"\bXDG_RUNTIME_DIR=\S+ ")
        self.assertRegex(ui_job, rf"(?m)^\s*useradd\b[^\n]*\s{re.escape(user)}$")
        self.assertRegex(ui_job, rf"(?m)^\s*chown -R {re.escape(user)}:{re.escape(user)} \S+$")

    def test_bare_distro_containers_install_git_before_a_verified_checkout(self):
        # Without git in the container, actions/checkout falls back to a REST
        # tarball with no .git, and `git rev-parse HEAD` cannot verify it.
        for path in WORKFLOWS.glob("*.yml"):
            source = path.read_text(encoding="utf-8")
            for job_name in re.findall(r"(?m)^  ([A-Za-z0-9_-]+):\n", source):
                block = job_block(source, job_name)
                image = re.search(r"(?m)^    container:\n(?:      [^\n]*\n)*?      image: (\S+)", block)
                if not image or image.group(1).startswith(FLATPAK_IMAGE):
                    continue
                if "git rev-parse" not in block:
                    continue
                with self.subTest(workflow=path.name, job=job_name, image=image.group(1)):
                    checkout = re.search(r"(?m)^\s*(?:- )?uses: actions/checkout@", block)
                    self.assertTrue(checkout, "a verified job needs a checkout")
                    install = re.search(
                        r"(?m)^\s*run: (?:dnf|microdnf|apt-get|apk) (?:install|add)\b[^\n]*\sgit(?:-core)?(?:\s|$)",
                        block,
                    )
                    self.assertTrue(install, "git must be installed in the container")
                    self.assertLess(install.start(), checkout.start(), "git must be installed before checkout")

    def test_container_jobs_trust_their_workspace_before_any_git_use(self):
        # Container steps run as root against a workspace the runner's host
        # user owns, so git refuses it as dubious unless a later-persisting
        # global safe.directory names exactly that container path. checkout
        # only trusts it for its own commands under a temporary HOME.
        git_use = re.compile(
            r"\bgit (?!config --global --add safe\.directory)[a-z-]+"
            r"|[\"']git[\"'],|scripts/check-release\.py"
        )
        trust = re.compile(r'(?m)^\s*run: git config --global --add safe\.directory "\$\{GITHUB_WORKSPACE\}"$')
        checked = []
        for name in ("flatpak.yml", "publish.yml"):
            source = workflow(name)
            for job_name in re.findall(r"(?m)^  ([A-Za-z0-9_-]+):\n", source):
                block = job_block(source, job_name)
                if not re.search(r"(?m)^    container:$", block):
                    continue
                code = "\n".join(
                    "" if line.strip().startswith("#") else line for line in block.splitlines()
                )
                first_use = git_use.search(code)
                if not first_use:
                    continue
                checked.append(job_name)
                with self.subTest(workflow=name, job=job_name):
                    self.assertNotRegex(code, r"safe\.directory\s+['\"]?\*")
                    self.assertNotRegex(code, r"safe\.directory[^\n]*github\.workspace")
                    checkout = re.search(r"(?m)^\s*(?:- )?uses: actions/checkout@", code)
                    trusted = trust.search(code)
                    self.assertTrue(checkout, "a git-using container job needs a checkout")
                    self.assertTrue(trusted, "the job must add $GITHUB_WORKSPACE to safe.directory")
                    self.assertLess(checkout.start(), trusted.start(), "trust the workspace after checkout")
                    self.assertLess(trusted.start(), first_use.start(),
                                    f"workspace must be trusted before {first_use.group(0)!r}")
        self.assertEqual(sorted(checked), ["flatpak", "publish-repo", "verify-client", "webkit-ui"])

    def test_pr_ci_exercises_the_publication_tag_fetch(self):
        # publish-repo only runs after a tag push; verify-client runs the
        # identical fetch in the same image and setup on every PR.
        fetch = re.compile(r"(?m)^\s*(?:run: )?git fetch --force --tags origin$")
        trust = re.compile(r'(?m)^\s*run: git config --global --add safe\.directory "\$\{GITHUB_WORKSPACE\}"$')
        for name, job_name, before in (
            ("flatpak.yml", "verify-client", "- name: Select previous supported release"),
            ("publish.yml", "publish-repo", None),
        ):
            with self.subTest(job=job_name):
                block = job_block(workflow(name), job_name)
                fetched, trusted = fetch.search(block), trust.search(block)
                self.assertTrue(fetched, "the job must run `git fetch --force --tags origin`")
                self.assertTrue(trusted)
                self.assertLess(trusted.start(), fetched.start(), "fetch only after trusting the workspace")
                if before:
                    self.assertLess(fetched.start(), block.index(before), "refresh tags before selecting by tag")

    def test_only_the_ci_ui_script_steps_disable_the_webkit_sandbox(self):
        # Docker's default seccomp blocks bubblewrap, so WebKit's web-process
        # sandbox cannot start in the webkit-ui container. Exactly those two
        # steps may turn it off: not the workflow, a job, another step or
        # workflow, an action, the app, packaging, or any script.
        permitted, elsewhere = sandbox_variable_occurrences(PROJECT)
        self.assertEqual(elsewhere, [], f"{SANDBOX_VARIABLE} is set outside the webkit-ui UI script steps")
        self.assertEqual(permitted, sorted(UI_SCRIPT_STEPS))

        ui_job = job_block(workflow("flatpak.yml"), "webkit-ui")
        for step_name in UI_SCRIPT_STEPS:
            with self.subTest(step=step_name):
                step = re.search(rf"(?ms)^      - name: {re.escape(step_name)}\n(.*?)(?=^      - |\Z)", ui_job)
                self.assertTrue(step, "both UI script steps must exist")
                code = "\n".join(line for line in step.group(1).splitlines() if not line.strip().startswith("#"))
                self.assertRegex(code, rf'(?m)^        env:\n(?:          [^\n]*\n)*?          {SANDBOX_VARIABLE}: "1"$')

        runuser = re.search(r"(?m)^\s*runuser .*scripts/test-ui-regression$", ui_job)
        self.assertTrue(runuser)
        # The unprivileged step must hand the inherited environment on.
        self.assertNotRegex(runuser.group(0), r"runuser (?:-l|--login|-)\s|\benv -i\b|--ignore-environment")

    def test_sandbox_guard_rejects_every_other_setting_location(self):
        injections = {
            "workflow-level env": ("flatpak.yml", "permissions:\n  contents: read\n\njobs:\n",
                                   f'permissions:\n  contents: read\n\nenv:\n  {SANDBOX_VARIABLE}: "1"\n\njobs:\n'),
            "flatpak job-level env": ("flatpak.yml", "    needs: [test, webkit-ui, release-eligibility]\n",
                                      f'    needs: [test, webkit-ui, release-eligibility]\n    env:\n      {SANDBOX_VARIABLE}: "1"\n'),
            "webkit-ui job-level env": ("flatpak.yml", "    container:\n      image: fedora:44\n",
                                        f'    container:\n      image: fedora:44\n    env:\n      {SANDBOX_VARIABLE}: "1"\n'),
            "another webkit-ui step": ("flatpak.yml", "      - name: Install headless WebKit dependencies\n",
                                       "      - name: Install headless WebKit dependencies\n"
                                       f'        env:\n          {SANDBOX_VARIABLE}: "1"\n'),
            "publish workflow env": ("publish.yml", "permissions:\n  contents: read\n",
                                     f'permissions:\n  contents: read\n\nenv:\n  {SANDBOX_VARIABLE}: "1"\n'),
        }
        added_files = {
            "new .yaml workflow": (".github/workflows/extra.yaml",
                                   "name: Extra\non: [push]\njobs:\n  extra:\n    runs-on: ubuntu-24.04\n    steps:\n"
                                   f'      - name: Anything\n        env:\n          {SANDBOX_VARIABLE}: "1"\n        run: "true"\n'),
            "composite action": (".github/actions/setup/action.yml",
                                 f'runs:\n  using: composite\n  steps:\n    - run: export {SANDBOX_VARIABLE}=1\n      shell: bash\n'),
            "script": ("scripts/ci-env", f"#!/bin/sh\nexport {SANDBOX_VARIABLE}=1\n"),
            "app module": ("airloom/sandbox.py", f'import os\nos.environ["{SANDBOX_VARIABLE}"] = "1"\n'),
            "packaging": ("packaging/extra.yml", f"finish-args:\n  - --env={SANDBOX_VARIABLE}=1\n"),
            "Makefile": ("Makefile", f"export {SANDBOX_VARIABLE} = 1\n"),
            # Only the repository-root tests/ and docs/ are exempt; nested
            # directories with those names ship (Makefile, Flatpak manifest).
            "nested app docs/": ("airloom/docs/sandbox.py", f'import os\nos.environ["{SANDBOX_VARIABLE}"] = "1"\n'),
            "nested action tests/": (".github/actions/tests/action.yaml",
                                     f"runs:\n  using: composite\n  steps:\n    - run: export {SANDBOX_VARIABLE}=1\n      shell: bash\n"),
            "nested scripts tests/": ("scripts/tests/x", f"export {SANDBOX_VARIABLE}=1\n"),
        }
        cases = [(name, edit, None) for name, edit in injections.items()]
        cases += [(name, None, added) for name, added in added_files.items()]
        for name, edit, added in cases:
            with self.subTest(injection=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for entry in (".github", "airloom", "packaging", "scripts", "run", "Makefile"):
                    source = PROJECT / entry
                    if source.is_dir():
                        shutil.copytree(source, root / entry, ignore=shutil.ignore_patterns("__pycache__"))
                    else:
                        shutil.copy2(source, root / entry)
                self.assertEqual(sandbox_variable_occurrences(root), (sorted(UI_SCRIPT_STEPS), []))
                if edit:
                    target = root / ".github" / "workflows" / edit[0]
                    text = target.read_text(encoding="utf-8")
                    self.assertEqual(text.count(edit[1]), 1)
                    target.write_text(text.replace(edit[1], edit[2]), encoding="utf-8")
                else:
                    target = root / added[0]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("a", encoding="utf-8") as stream:
                        stream.write(added[1])
                _permitted, elsewhere = sandbox_variable_occurrences(root)
                self.assertEqual(len(elsewhere), 1, elsewhere)

    def test_sandbox_guard_exempts_only_the_root_tests_and_docs_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("tests/test_sandbox.py", "tests/nested/case.py", "docs/ci.md"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"{SANDBOX_VARIABLE}=1\n", encoding="utf-8")
            self.assertEqual(sandbox_variable_occurrences(root), ([], []))

    def test_publication_queue_retries_transient_github_api_errors(self):
        publish = workflow("publish.yml")
        step = re.search(
            r"(?ms)uses: softprops/turnstyle@[0-9a-f]{40}[^\n]*\n\s*with:\n((?:\s{10}[^\n]*\n)+)",
            publish,
        )
        self.assertTrue(step, "Turnstyle step needs a with: block")
        self.assertRegex(step.group(1), r"(?m)^\s*retries: 3$")

    def test_claude_review_runs_only_for_same_repository_human_prs(self):
        review_job = job_block(workflow("claude-code-review.yml"), "claude-review")
        condition = job_condition(review_job)
        self.assertTrue(condition, "claude-review needs a job-level skip condition")
        scenarios = (
            ("same-repository human", "octocat", "User", "ijbaird/airloom", True),
            ("Dependabot", "dependabot[bot]", "Bot", "ijbaird/airloom", False),
            ("Dependabot synchronize on human PR", "dependabot[bot]", "User", "ijbaird/airloom", False),
            ("another bot", "other[bot]", "Bot", "ijbaird/airloom", False),
            ("fork human", "octocat", "User", "octocat/airloom", False),
        )
        for name, actor, author_type, head_repository, expected in scenarios:
            with self.subTest(scenario=name):
                context = pull_request_context(
                    actor=actor,
                    author_type=author_type,
                    head_repository=head_repository,
                )
                self.assertIs(evaluate_condition(condition, context), expected)

    def test_offered_checkout_update_is_immutable_in_every_workflow(self):
        for path in WORKFLOWS.glob("*.yml"):
            with self.subTest(workflow=path.name):
                source = path.read_text(encoding="utf-8")
                references = re.findall(r"actions/checkout@([^\s#]+)", source)
                self.assertTrue(references, "expected at least one checkout reference")
                self.assertEqual(references, [CHECKOUT_SHA] * len(references))

    def test_every_action_reference_uses_an_immutable_commit_sha(self):
        for path in WORKFLOWS.glob("*.yml"):
            with self.subTest(workflow=path.name):
                source = path.read_text(encoding="utf-8")
                references = re.findall(r"(?m)^\s*-?\s*uses:\s*([^\s#]+)", source)
                self.assertTrue(references, "expected action references")
                for reference in references:
                    if reference.startswith("./"):
                        self.assertRegex(reference, r"^\./\.github/workflows/[A-Za-z0-9_.-]+\.yml$")
                        continue
                    self.assertRegex(reference, r"@[0-9a-f]{40}$")

    def test_offered_deploy_pages_update_is_pinned_to_the_offered_sha(self):
        source = workflow("flatpak.yml") + workflow("publish.yml")
        self.assertIn(f"actions/deploy-pages@{DEPLOY_PAGES_SHA}", source)

    def test_offered_flatpak_builder_update_is_pinned_to_the_offered_sha(self):
        source = workflow("flatpak.yml")
        self.assertIn(
            f"flatpak/flatpak-github-actions/flatpak-builder@{FLATPAK_BUILDER_SHA}",
            source,
        )

    def test_publication_queue_uses_the_reviewed_turnstyle_sha(self):
        source = workflow("publish.yml")
        self.assertIn(f"softprops/turnstyle@{TURNSTYLE_SHA}", source)


if __name__ == "__main__":
    unittest.main()
