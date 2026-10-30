import re
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
WORKFLOWS = PROJECT / ".github" / "workflows"
CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
DEPLOY_PAGES_SHA = "368f82528645a54fb793d4d04e342629a3f51346"
FLATPAK_BUILDER_SHA = "79327416609af08178ad73b352877e51450790b3"
TURNSTYLE_SHA = "3805450be63b8c80577dc253e8c17e9132036df4"


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
            r"^dbus-run-session -- xvfb-run -a (?:-s \"[^\"]*\" )?"
            r"(?:env (?:[A-Z_]+=\S+ )+)?python3 scripts/test-ui-regression$",
        )
        self.assertIn("webkit-ui", job_needs(source, "publication"))

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
