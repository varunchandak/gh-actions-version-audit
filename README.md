# GitHub Actions Version Audit

This action scans your repository's workflows for `uses:` references, checks the GitHub API for each action's latest release, and reports version drift or insecure tag pinning (e.g., pinning to `v1` instead of a commit SHA).

## Features

- **Drift Detection:** Identifies when a pinned SHA is no longer the latest release.
- **Security Auditing:** Flags actions pinned to mutable tags instead of immutable commit SHAs.
- **Slack Notifications:** Sends a summary of findings to a Slack channel via Block Kit.
- **Job Summary:** Automatically generates a Markdown report on the GitHub Actions run page.
- **Workflow Annotations:** Adds warnings directly to the workflow files in the GitHub UI for easy fixing.
- **Zero Dependencies:** Runs on standard runners without needing additional setup.

## Usage

Create a workflow (e.g., `.github/workflows/audit.yml`) to run the audit on a schedule:

```yaml
name: GitHub Actions Version Audit
on:
  schedule:
    - cron: '0 0 * * 1' # Every Monday at midnight
  workflow_dispatch:

permissions:
  contents: read

jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10 # v6.0.3
      - uses: varunchandak/gh-actions-version-audit@63bffd07c344e008a1030f01f0a176544b1525fe # v1
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          slack_webhook_url: ${{ secrets.SLACK_WEBHOOK_URL }}
```

## Inputs

| Input | Description | Default |
|-------|-------------|---------|
| `github_token` | Required. Used to query public release metadata. | `${{ github.token }}` |
| `slack_webhook_url` | Optional. Slack incoming webhook URL. If omitted, results are printed to logs. | — |
| `workflows_dir` | Optional. The directory to scan for workflow files. | `.github/workflows` |
| `skip_prefixes` | Optional. Comma-separated prefixes to ignore (e.g., `./` for local actions). | `./` |

## License
MIT
