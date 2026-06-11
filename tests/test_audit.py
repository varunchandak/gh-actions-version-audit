import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "github-actions-version-audit.py"
SPEC = importlib.util.spec_from_file_location("github_actions_version_audit", MODULE_PATH)
audit = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(audit)


class ScanWorkflowsTests(unittest.TestCase):
    def test_scans_public_actions_and_skips_local_actions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflows = Path(temp_dir)
            (workflows / "audit.yml").write_text(
                """
steps:
  - uses: actions/checkout@abc123 # v4
  - uses: docker/setup-buildx-action@v3
  - uses: ./.github/actions/local
""".lstrip()
            )

            occurrences = audit.scan_workflows(workflows, ("./",))

        self.assertEqual(set(occurrences), {"actions/checkout", "docker/setup-buildx-action"})
        self.assertEqual(occurrences["actions/checkout"][0][1:], ("abc123", "v4"))
        self.assertEqual(occurrences["docker/setup-buildx-action"][0][1], "v3")

    def test_scans_yml_and_yaml_recursively(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflows = Path(temp_dir)
            nested = workflows / "nested"
            nested.mkdir()
            (workflows / "first.yml").write_text("- uses: actions/checkout@v4\n")
            (nested / "second.yaml").write_text("- uses: actions/setup-python@v5\n")

            occurrences = audit.scan_workflows(workflows, ())

        self.assertEqual(set(occurrences), {"actions/checkout", "actions/setup-python"})

    def test_scans_single_and_double_quoted_action_references(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflows = Path(temp_dir)
            (workflows / "quoted.yml").write_text(
                """
steps:
  - uses: "actions/checkout@v4" # v4
  - uses: 'actions/setup-python@v5'
""".lstrip()
            )

            occurrences = audit.scan_workflows(workflows, ())

        self.assertEqual(occurrences["actions/checkout"][0][1:], ("v4", "v4"))
        self.assertEqual(occurrences["actions/setup-python"][0][1], "v5")

    def test_does_not_scan_mismatched_quotes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflows = Path(temp_dir)
            (workflows / "invalid.yml").write_text(
                '- uses: "actions/checkout@v4\'\n'
            )

            occurrences = audit.scan_workflows(workflows, ())

        self.assertEqual(occurrences, {})


class GitHubApiTests(unittest.TestCase):
    @mock.patch.object(audit, "gh_get")
    def test_resolves_annotated_tag_to_commit(self, gh_get):
        gh_get.side_effect = [
            {"object": {"type": "tag", "sha": "tag-object"}},
            {"object": {"type": "commit", "sha": "commit-sha"}},
        ]

        result = audit.resolve_tag_sha("actions/checkout", "v4", "token")

        self.assertEqual(result, "commit-sha")
        self.assertEqual(gh_get.call_count, 2)


class ReportTests(unittest.TestCase):
    def test_markdown_summary_contains_finding(self):
        findings = [
            {
                "action": "actions/checkout",
                "current_display": "v4",
                "latest_tag": "v6.0.3",
                "latest_sha": "a" * 40,
                "files_count": 2,
                "tag_pinned_count": 1,
            }
        ]

        summary = audit.build_markdown_summary(findings, "owner/repo")

        self.assertIn("`actions/checkout`", summary)
        self.assertIn("Tag pin found", summary)
        self.assertIn("owner/repo", summary)


if __name__ == "__main__":
    unittest.main()
