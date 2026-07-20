# GitHub Actions Version Audit

Find outdated GitHub Actions, mutable action tags, and CI/CD supply-chain drift before they become production risk.

This action scans your repository's workflows for `uses:` references, checks the GitHub API for each action's latest release, and reports version drift or insecure tag pinning (e.g., pinning to `v1` instead of a commit SHA).

## Why Pin to a Commit SHA?

GitHub Actions run with access to your repository, secrets, and often your cloud environments. Pinning an action to a mutable tag like `@v4` means you're trusting that the tag won't be silently moved — but tags can be overwritten by the action author or by an attacker who compromises their account. This is a well-documented **supply chain attack vector**.

Pinning to a **full-length commit SHA** (e.g. `@df4cb1c...`) is immutable — it always resolves to exactly the same code, regardless of what happens upstream.

**Real-world incidents:**
- **tj-actions/changed-files** (March 2025): a compromised action was used to exfiltrate CI secrets from thousands of repositories.
- **reviewdog/action-setup** (same campaign): the tag was silently moved to point to malicious code.

This action helps you detect and remediate both problems: outdated pins and mutable tag references.

## GitHub SHA Pinning Enforcement

GitHub also provides a repository Actions setting named **Require actions to be pinned to a full-length commit SHA**. We validated this behavior in a test repository by running a workflow with a tagged action reference, enabling the setting, and running the same workflow again.

Observations from the test:

- Tagged action references such as `owner/action@v1` continue to run while `sha_pinning_required` is disabled.
- After `sha_pinning_required` is enabled, workflows with tagged action references fail during job setup before any workflow steps run.
- The failure message identifies the exact `uses:` reference that must be changed to a full-length commit SHA.
- Existing workflow files are not automatically migrated by GitHub. Repositories need a separate audit or remediation process before enforcement is enabled broadly.

Use this custom action when you need to:

- Inventory mutable action references across repositories before enabling enforcement.
- Keep SHA-pinned actions current with the latest released commit.
- Send Slack notifications and job summaries without blocking development workflows.
- Open reviewable pull requests that update workflow files to full-length commit SHAs.

Use GitHub's SHA pinning enforcement setting when you need to:

- Prevent new or existing workflows from running unless every external action is pinned to a full-length commit SHA.
- Enforce the policy at runtime after repositories have been audited and remediated.
- Add a hard guardrail for repositories that run sensitive workloads or have access to sensitive secrets.

You can confirm the repository setting with:

```bash
gh api repos/OWNER/REPO/actions/permissions
```

The relevant response field is `sha_pinning_required`.

## Features

- **Drift Detection:** Identifies when a pinned SHA is no longer the latest release.
- **Security Auditing:** Flags actions pinned to mutable tags instead of immutable commit SHAs.
- **Slack Notifications:** Sends a summary of findings to a Slack channel via Block Kit.
- **Job Summary:** Automatically generates a Markdown report on the GitHub Actions run page.
- **Workflow Annotations:** Adds warnings directly to the workflow files in the GitHub UI for easy fixing.
- **Optional Pull Requests:** Can patch stale or mutable `uses:` references to the latest release SHA and open a PR.
- **Zero Dependencies:** Runs on standard runners without needing additional setup.

## Audit Outdated GitHub Actions

Use this action as a lightweight GitHub Actions version checker for repositories that want Dependabot-style visibility plus SHA pinning awareness. It is designed for CI/CD security reviews, workflow maintenance, and supply-chain hardening.

## Usage

Create a workflow (e.g., `.github/workflows/audit.yml`) to run the audit on a schedule.

The GitHub Marketplace **Use latest version** button may only show the single action step. Use the full workflow below so the repository is checked out before the audit runs.

```yaml
name: GitHub Actions Version Audit

on:
  workflow_dispatch:
  schedule:
    - cron: '30 4 * * 1'

permissions:
  contents: read

jobs:
  audit:
    name: Audit GitHub Actions versions
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - name: Check out repository
        uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0

      - name: Audit outdated GitHub Actions
        uses: varunchandak/gh-actions-version-audit@3fc4f4c93cf4079cbcc071ca3c2c9849c5e8b508 # v1.1.7
        with:
          slack_webhook_url: ${{ secrets.SLACK_WEBHOOK_URL }}
```

## Automated Pull Requests

PR creation is disabled by default. Enable it when you want the action to patch workflow files from findings and open a reviewable PR.

Because this action usually changes files under `.github/workflows`, the token that commits the PR branch must have permission to edit workflow files. This applies even though the action opens a PR instead of committing directly to the default branch. GitHub validates workflow-file writes at commit time for any branch.

The default `GITHUB_TOKEN` is usually not enough for that write path. If PR creation fails with `Resource not accessible by integration`, use a GitHub App installation token or a fine-grained PAT with:

- `contents: write`
- `pull-requests: write`
- `workflows: write`

If the action uses `GITHUB_TOKEN` for PR creation, the target repository must also allow workflows to create pull requests:

1. Go to **Settings** → **Actions** → **General**.
2. Under **Workflow permissions**, choose **Read and write permissions**.
3. Enable **Allow GitHub Actions to create and approve pull requests**.

Example using `actions/create-github-app-token`:

```yaml
name: GitHub Actions Version Audit

on:
  workflow_dispatch:
  schedule:
    - cron: '30 4 * * 1'

permissions:
  contents: write       # needed to push the bump branch
  pull-requests: write  # needed to open the PR

jobs:
  audit:
    name: Audit GitHub Actions versions
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - name: Check out repository
        uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0

      - name: Generate GitHub App token
        id: app-token
        uses: actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1 # v3.2.0
        with:
          client-id: ${{ vars.GH_SECURITY_AUTOMATION_APP_CLIENT_ID }}
          private-key: ${{ secrets.GH_SECURITY_AUTOMATION_APP_PRIVATE_KEY }}
          owner: ${{ github.repository_owner }}
          repositories: ${{ github.event.repository.name }}

      - name: Audit outdated GitHub Actions
        uses: varunchandak/gh-actions-version-audit@3fc4f4c93cf4079cbcc071ca3c2c9849c5e8b508 # v1.1.7
        with:
          github_token: ${{ steps.app-token.outputs.token }}
          create_pr: 'true'
          pr_branch: github-actions-version-audit
          pr_base: ${{ github.event.repository.default_branch }}
```

The push token (`git_push_token`) is used for the commit that updates workflow files and is tried first for PR lookup and creation. `github_token` is used for release lookups and as a fallback when `git_push_token` is not provided. If you are using the same app token for all operations (as shown above), omit `git_push_token` - the code falls back to `github_token` automatically. Passing the same value to both inputs is redundant. Use a GitHub App token as `github_token` when auditing internal repositories so release metadata is visible across the app installation.

## Inputs

| Input | Description | Default |
|-------|-------------|---------|
| `github_token` | Token used to query release metadata and as a fallback for push and PR creation when `git_push_token` is not set. Defaults to `GITHUB_TOKEN`. | `${{ github.token }}` |
| `slack_webhook_url` | Optional. Slack incoming webhook URL. If omitted, results are printed to logs. | None |
| `workflows_dir` | Optional. The directory to scan for workflow files. | `.github/workflows` |
| `skip_prefixes` | Optional. Comma-separated prefixes to ignore (e.g., `./` for local actions). | `./` |
| `create_pr` | Optional. Set to `true` to patch findings and open a pull request. Also accepts `1`, `yes`, `on`. | `false` |
| `git_push_token` | Optional. GitHub App or PAT token with `contents:write` and `workflows` permission, used to commit workflow file updates and tried first for PR lookup and creation. If omitted or identical to `github_token`, `github_token` is used for all operations. | None |
| `pr_branch` | Optional. Branch to create or update for automated PRs. | `github-actions-version-audit` |
| `pr_base` | Optional. Base branch for automated PRs. Defaults to `main` - recommend passing `${{ github.event.repository.default_branch }}` to avoid hardcoding the branch name. | `main` |

## License
MIT
