# gh-actions-version-audit
This action scans your repository's workflows for uses: references, checks the GitHub API for each action's latest release, and reports version drift or insecure tag pinning (e.g., pinning to v1 instead of a commit SHA).
