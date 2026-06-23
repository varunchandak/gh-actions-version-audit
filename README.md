# GitHub Actions Version Audit

This action scans your repository's workflows for `uses:` references, checks the GitHub API for each action's latest release, and reports version drift or insecure tag pinning (e.g., pinning to `v1` instead of a commit SHA).

## Why Pin to a Commit SHA?

GitHub Actions run with access to your repository, secrets, and often your cloud environments. Pinning an action to a mutable tag like `@v4` means you're trusting that the tag won't be silently moved — but tags can be overwritten by the action author or by an attacker who compromises their account. This is a well-documented **supply chain attack vector**.

Pinning to a **full-length commit SHA** (e.g. `@df4cb1c...`) is immutable — it always resolves to exactly the same code, regardless of what happens upstream.

**Real-world incidents:**
- **tj-actions/changed-files** (March 2025): a compromised action was used to exfiltrate CI secrets from thousands of repositories.
- **reviewdog/action-setup** (same campaign): the tag was silently moved to point to malicious code.

This action helps you detect and remediate both problems: outdated pins and mutable tag references.

## Features

- **Drift Detection:** Identifies when a pinned SHA is no longer the latest release.
- **Security Auditing:** Flags actions pinned to mutable tags instead of immutable commit SHAs.
- **Slack Notifications:** Sends a summary of findings to a Slack channel via Block Kit.
- **Job Summary:** Automatically generates a Markdown report on the GitHub Actions run page.
- **Workflow Annotations:** Adds warnings directly to the workflow files in the GitHub UI for easy fixing.
- **Optional Pull Requests:** Can patch stale or mutable `uses:` references to the latest release SHA and open a PR.
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

## Automated Pull Requests

PR creation is disabled by default. Enable it when you want the action to patch workflow files from findings and open a reviewable PR.

Because these changes modify files under `.github/workflows`, the token that commits the branch must have permission to edit workflow files. The default `GITHUB_TOKEN` is usually not enough for that write path. Use a GitHub App installation token or a fine-grained PAT with:

- `contents: write`
- `workflows: write`
- `pull-requests: write`

Example using `actions/create-github-app-token`:

```yaml
name: GitHub Actions Version Audit
on:
  schedule:
    - cron: '0 0 * * 1'
  workflow_dispatch:

permissions:
  contents: write
  pull-requests: write

jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10 # v6.0.3

      - name: Generate GitHub App token
        id: app-token
        uses: actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1 # v3.2.0
        with:
          client-id: ${{ vars.GH_SECURITY_AUTOMATION_APP_CLIENT_ID }}
          private-key: ${{ secrets.GH_SECURITY_AUTOMATION_APP_PRIVATE_KEY }}
          owner: ${{ github.repository_owner }}
          repositories: ${{ github.event.repository.name }}

      - uses: varunchandak/gh-actions-version-audit@63bffd07c344e008a1030f01f0a176544b1525fe # v1
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          create_pr: 'true'
          git_push_token: ${{ steps.app-token.outputs.token }}
          pr_branch: github-actions-version-audit
          pr_base: main
```

The audit token (`github_token`) is still used for release lookups and PR creation. The push token (`git_push_token`) is used for the commit that updates workflow files.

## Inputs

| Input | Description | Default |
|-------|-------------|---------|
| `github_token` | Required. Used to query public release metadata. | `${{ github.token }}` |
| `slack_webhook_url` | Optional. Slack incoming webhook URL. If omitted, results are printed to logs. | — |
| `workflows_dir` | Optional. The directory to scan for workflow files. | `.github/workflows` |
| `skip_prefixes` | Optional. Comma-separated prefixes to ignore (e.g., `./` for local actions). | `./` |
| `create_pr` | Optional. Set to `true` to patch findings and open a pull request. | `false` |
| `git_push_token` | Optional unless `create_pr` is enabled. GitHub App or PAT token used to commit workflow file updates. | — |
| `pr_branch` | Optional. Branch to create or update for automated PRs. | `github-actions-version-audit` |
| `pr_base` | Optional. Base branch for automated PRs. | `main` |

## License
MIT
