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
        self.assertEqual(occurrences["actions/checkout"][0][1:], ("actions/checkout", "abc123", "v4"))
        self.assertEqual(occurrences["docker/setup-buildx-action"][0][2], "v3")

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

        self.assertEqual(occurrences["actions/checkout"][0][1:], ("actions/checkout", "v4", "v4"))
        self.assertEqual(occurrences["actions/setup-python"][0][2], "v5")

    def test_preserves_action_subpaths_for_reporting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflows = Path(temp_dir)
            (workflows / "codeql.yml").write_text("- uses: github/codeql-action/init@v4\n")

            occurrences = audit.scan_workflows(workflows, ())

        self.assertEqual(set(occurrences), {"github/codeql-action"})
        self.assertEqual(occurrences["github/codeql-action"][0][1], "github/codeql-action/init")

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


class PatchWorkflowTests(unittest.TestCase):
    def test_patches_unquoted_and_quoted_uses_lines(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflows = Path(temp_dir)
            workflow = workflows / "audit.yml"
            workflow.write_text(
                """
steps:
  - uses: actions/checkout@v4
  - uses: "actions/checkout@abc123" # old
  - uses: 'actions/setup-python@v5'
""".lstrip()
            )

            patched = audit.patch_workflow_files(
                "actions/checkout",
                "a" * 40,
                "v6.0.3",
                [workflow],
            )

            self.assertEqual(patched, [workflow])
            self.assertEqual(
                workflow.read_text(),
                f"""
steps:
  - uses: actions/checkout@{"a" * 40} # v6.0.3
  - uses: "actions/checkout@{"a" * 40}" # v6.0.3
  - uses: 'actions/setup-python@v5'
""".lstrip(),
            )


class PullRequestTests(unittest.TestCase):
    @mock.patch.dict(audit.os.environ, {"GIT_PUSH_TOKEN": "app-token"})
    @mock.patch.object(audit, "gh_patch")
    @mock.patch.object(audit, "gh_post")
    @mock.patch.object(audit, "gh_get")
    def test_uses_app_token_for_pr_lookup_and_creation(self, gh_get, gh_post, gh_patch):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflow = Path(temp_dir) / ".github" / "workflows" / "audit.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("- uses: actions/checkout@%s # v7.0.0\n" % ("a" * 40))

            gh_get.side_effect = [
                {"object": {"sha": "base-sha"}},
                None,
                [],
            ]
            gh_post.side_effect = [
                {},
                {"data": {"createCommitOnBranch": {"commit": {"oid": "commit-sha"}}}},
                {"html_url": "https://github.com/owner/repo/pull/1"},
            ]

            with mock.patch.object(audit.Path, "cwd", return_value=Path(temp_dir)):
                pr_url = audit.create_pull_request(
                    "github-token",
                    "owner/repo",
                    "audit-branch",
                    "main",
                    [
                        {
                            "action": "actions/checkout",
                            "current_display": "v4",
                            "latest_tag": "v7.0.0",
                            "latest_sha": "a" * 40,
                        }
                    ],
                    None,
                    [workflow],
                )

        self.assertEqual(pr_url, "https://github.com/owner/repo/pull/1")
        self.assertEqual(gh_patch.call_count, 0)
        self.assertEqual([call.args[1] for call in gh_get.call_args_list], ["app-token"] * 3)
        self.assertEqual([call.args[1] for call in gh_post.call_args_list], ["app-token"] * 3)
        pr_body = gh_post.call_args_list[2].args[2]
        self.assertEqual(pr_body["title"], "Bump pinned GitHub Actions to latest SHAs")
        self.assertIn("This PR was opened by GitHub Actions Version Audit.", pr_body["body"])

    @mock.patch.object(audit, "gh_post")
    @mock.patch.object(audit, "gh_get")
    def test_pr_lookup_falls_back_to_github_token(self, gh_get, gh_post):
        gh_get.side_effect = [
            RuntimeError("primary failed"),
            [],
        ]
        gh_post.return_value = {"html_url": "https://github.com/owner/repo/pull/1"}

        prs = audit._get_existing_pull_requests(
            "owner",
            "repo",
            "audit-branch",
            "main",
            ["app-token", "github-token"],
        )

        self.assertEqual(prs, [])
        self.assertEqual([call.args[1] for call in gh_get.call_args_list], ["app-token", "github-token"])

    @mock.patch.object(audit, "gh_post")
    def test_pr_creation_falls_back_to_github_token(self, gh_post):
        gh_post.side_effect = [
            RuntimeError("primary failed"),
            {"html_url": "https://github.com/owner/repo/pull/1"},
        ]

        pr = audit._create_pull_request_with_fallback(
            "owner",
            "repo",
            ["app-token", "github-token"],
            {"title": "Title", "head": "audit-branch", "base": "main"},
        )

        self.assertEqual(pr["html_url"], "https://github.com/owner/repo/pull/1")
        self.assertEqual([call.args[1] for call in gh_post.call_args_list], ["app-token", "github-token"])


class ReportTests(unittest.TestCase):
    def test_copy_paste_updates_use_full_sha(self):
        findings = [
            {
                "action": "actions/checkout",
                "current_display": "v4",
                "latest_tag": "v6.0.3",
                "latest_sha": "a" * 40,
                "files_count": 2,
                "tag_pinned_count": 1,
                "action_refs": ["github/codeql-action/init"],
            }
        ]

        updates = audit.build_copy_paste_updates(findings)

        self.assertIn(
            f"- uses: github/codeql-action/init@{'a' * 40} # v6.0.3",
            updates,
        )

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
        self.assertIn(f"- uses: actions/checkout@{'a' * 40} # v6.0.3", summary)

    def test_slack_payload_contains_copy_paste_update(self):
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

        payload = audit.build_slack_payload(findings, "owner/repo", None)

        section = payload["blocks"][3]["text"]["text"]
        self.assertIn(f"uses: actions/checkout@{'a' * 40} # v6.0.3", section)


if __name__ == "__main__":
    unittest.main()
