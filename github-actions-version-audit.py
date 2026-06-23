#!/usr/bin/env python3
"""Audit GitHub Actions references in workflows and report version drift.

Scans every `uses: owner/repo@ref` reference, asks the GitHub API for each
action's latest release, and reports when any action is out of date or still
pinned to a mutable tag. Optionally patches workflow files and opens a pull
request with the latest immutable release SHAs.

Environment:
- GITHUB_TOKEN (required): used for GitHub API calls.
- GIT_PUSH_TOKEN (optional): token used to create commits that modify workflow
  files. Required for CREATE_PR=true when GITHUB_TOKEN lacks workflow write
  permission; use a GitHub App or PAT with contents:write and workflows.
- SLACK_WEBHOOK_URL (optional): if unset, the payload is printed but not sent.
- GITHUB_STEP_SUMMARY (optional): if set, writes a Markdown report to the 
  GitHub Actions job summary.
- WORKFLOWS_DIR (optional, default .github/workflows): override scan target.
- SKIP_PREFIXES (optional, comma-separated, default ./): action refs starting 
  with any of these are ignored.
- CREATE_PR (optional, default false): set to true to patch findings and open
  a pull request.
- PR_BRANCH (optional, default github-actions-version-audit): pull request head
  branch to create or update.
- PR_BASE (optional, default main): pull request base branch.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

GITHUB_API = "https://api.github.com"
USES_RE = re.compile(
    r"""^\s*-?\s*uses:\s+
        (?P<quote>["']?)
        (?P<full>[^/\s@'"]+/[^@\s'"]+)
        @(?P<ref>[^\s#'"]+)
        (?P=quote)
        (?:\s*\#\s*(?P<comment>\S+))?
        \s*$
    """,
    re.VERBOSE,
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def gh_get(path: str, token: str) -> dict | list | None:
    req = urllib.request.Request(
        f"{GITHUB_API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-actions-version-audit",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        details = e.read().decode()
        raise RuntimeError(f"GitHub GET {path} failed: {e.code} {e.reason}: {details}") from e


def gh_post(path: str, token: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{GITHUB_API}{path}",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "github-actions-version-audit",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        details = e.read().decode()
        raise RuntimeError(f"GitHub POST {path} failed: {e.code} {e.reason}: {details}") from e


def gh_patch(path: str, token: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{GITHUB_API}{path}",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "github-actions-version-audit",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        details = e.read().decode()
        raise RuntimeError(f"GitHub PATCH {path} failed: {e.code} {e.reason}: {details}") from e


def resolve_tag_sha(owner_repo: str, tag: str, token: str) -> str | None:
    ref = gh_get(f"/repos/{owner_repo}/git/refs/tags/{tag}", token)
    if not ref:
        return None
    obj = ref["object"]
    if obj["type"] == "commit":
        return obj["sha"]
    if obj["type"] == "tag":
        tag_obj = gh_get(f"/repos/{owner_repo}/git/tags/{obj['sha']}", token)
        return tag_obj["object"]["sha"] if tag_obj else None
    return None


def scan_workflows(workflows_dir: Path, skip_prefixes: tuple[str, ...]) -> dict[str, list[tuple[Path, str, str | None]]]:
    occurrences: dict[str, list[tuple[Path, str, str | None]]] = defaultdict(list)
    for path in sorted([*workflows_dir.rglob("*.yml"), *workflows_dir.rglob("*.yaml")]):
        try:
            content = path.read_text()
        except Exception:
            continue
        for line in content.splitlines():
            m = USES_RE.match(line)
            if not m:
                continue
            full = m.group("full")
            if any(full.startswith(p) for p in skip_prefixes):
                continue
            owner_repo = "/".join(full.split("/")[:2])
            occurrences[owner_repo].append((path, m.group("ref"), m.group("comment")))
    return occurrences


def patch_workflow_files(
    owner_repo: str,
    new_sha: str,
    new_tag: str,
    affected_paths: list[Path],
) -> list[Path]:
    """Patch matching uses lines for an action to the latest release SHA."""
    action_uses_re = re.compile(
        r"(uses:\s+(?P<quote>[\"']?)" + re.escape(owner_repo) + r"@)"
        r"([0-9a-f]{40}|[^\s#'\"\n]+)"
        r"(?P=quote)"
        r"(\s*(?:#[^\n]*)?)$",
        re.MULTILINE,
    )
    replacement = rf"\g<1>{new_sha}\g<quote> # {new_tag}"
    patched: list[Path] = []

    for path in affected_paths:
        original = path.read_text()
        updated = action_uses_re.sub(replacement, original)
        if updated != original:
            path.write_text(updated)
            patched.append(path)
            print(f"  [patched] {path}: {owner_repo} -> {new_sha[:7]} # {new_tag}")

    return patched


def create_pull_request(
    token: str,
    repo: str,
    head_branch: str,
    base_branch: str,
    findings: list[dict],
    run_url: str | None,
    patched_files: list[Path],
) -> str:
    """Commit patched workflow files through GitHub and open or reuse a PR."""
    if not patched_files:
        print("No workflow file changes; skipping PR creation.")
        return ""
    if "/" not in repo:
        raise ValueError("GITHUB_REPOSITORY must be set to owner/repo when CREATE_PR is true.")

    owner, repo_name = repo.split("/", 1)
    push_token = os.environ.get("GIT_PUSH_TOKEN") or token
    pr_tokens = [push_token]
    if token != push_token:
        pr_tokens.append(token)

    base_ref = gh_get(f"/repos/{owner}/{repo_name}/git/refs/heads/{base_branch}", push_token)
    if not base_ref:
        raise ValueError(f"Base branch '{base_branch}' not found.")
    base_commit_sha = base_ref["object"]["sha"]

    existing_ref = gh_get(f"/repos/{owner}/{repo_name}/git/refs/heads/{head_branch}", push_token)
    if existing_ref:
        gh_patch(
            f"/repos/{owner}/{repo_name}/git/refs/heads/{head_branch}",
            push_token,
            {"sha": base_commit_sha, "force": True},
        )
    else:
        gh_post(
            f"/repos/{owner}/{repo_name}/git/refs",
            push_token,
            {"ref": f"refs/heads/{head_branch}", "sha": base_commit_sha},
        )

    additions = []
    for path in sorted(set(patched_files)):
        rel_path = str(path.relative_to(Path.cwd())) if path.is_absolute() else str(path)
        additions.append(
            {
                "path": rel_path,
                "contents": base64.b64encode(path.read_bytes()).decode(),
            }
        )

    action_list = ", ".join(f["action"] for f in findings)
    mutation = """
    mutation($input: CreateCommitOnBranchInput!) {
      createCommitOnBranch(input: $input) {
        commit { oid }
      }
    }
    """
    result = gh_post(
        "/graphql",
        push_token,
        {
            "query": mutation,
            "variables": {
                "input": {
                    "branch": {
                        "repositoryNameWithOwner": f"{owner}/{repo_name}",
                        "branchName": head_branch,
                    },
                    "message": {
                        "headline": "Bump pinned GitHub Actions SHAs",
                        "body": (
                            f"Actions updated: {action_list}\n"
                            "Automated by github-actions-version-audit."
                        ),
                    },
                    "expectedHeadOid": base_commit_sha,
                    "fileChanges": {"additions": additions},
                }
            },
        },
    )
    errors = result.get("errors")
    if errors:
        raise ValueError(f"createCommitOnBranch failed: {errors[0].get('message')}")
    commit_sha = result["data"]["createCommitOnBranch"]["commit"]["oid"]
    print(f"  Commit: {commit_sha[:7]}")

    existing_prs = _get_existing_pull_requests(owner, repo_name, head_branch, base_branch, pr_tokens)
    if isinstance(existing_prs, list) and existing_prs:
        pr = existing_prs[0]
        print(f"  Existing open PR found: {pr['html_url']} - reusing it.")
        return pr.get("html_url", "")

    lines = [
        "## Automated GitHub Actions SHA bump",
        "",
        "This PR was opened by GitHub Actions Version Audit.",
        "",
        "### Changes",
    ]
    for f in findings:
        lines.append(
            f"- **`{f['action']}`**: `{f['current_display']}` -> "
            f"`{f['latest_tag']}` (`{f['latest_sha'][:7]}`)"
        )
    if run_url:
        lines.extend(["", f"[View audit run]({run_url})"])

    resp = _create_pull_request_with_fallback(
        owner,
        repo_name,
        pr_tokens,
        {
            "title": "Bump pinned GitHub Actions to latest SHAs",
            "body": "\n".join(lines),
            "head": head_branch,
            "base": base_branch,
        },
    )
    return resp.get("html_url", "")


def _get_existing_pull_requests(
    owner: str,
    repo_name: str,
    head_branch: str,
    base_branch: str,
    tokens: list[str],
) -> list:
    path = f"/repos/{owner}/{repo_name}/pulls?head={owner}:{head_branch}&base={base_branch}&state=open"
    errors: list[str] = []
    for index, token in enumerate(tokens):
        try:
            prs = gh_get(path, token)
            return prs if isinstance(prs, list) else []
        except RuntimeError as exc:
            label = "primary" if index == 0 else "fallback"
            errors.append(f"{label} token: {exc}")
            print(f"  PR lookup with {label} token failed; trying next token.", file=sys.stderr)
    raise RuntimeError("PR lookup failed with all available tokens:\n" + "\n".join(errors))


def _create_pull_request_with_fallback(
    owner: str,
    repo_name: str,
    tokens: list[str],
    body: dict,
) -> dict:
    path = f"/repos/{owner}/{repo_name}/pulls"
    errors: list[str] = []
    for index, token in enumerate(tokens):
        try:
            return gh_post(path, token, body)
        except RuntimeError as exc:
            label = "primary" if index == 0 else "fallback"
            errors.append(f"{label} token: {exc}")
            if index < len(tokens) - 1:
                print(f"  PR creation with {label} token failed; trying next token.", file=sys.stderr)
    raise RuntimeError("PR creation failed with all available tokens:\n" + "\n".join(errors))


def build_slack_payload(findings: list[dict], repo: str, run_url: str | None, pr_url: str | None = None) -> dict:
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "GitHub Actions Version Audit", "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{len(findings)} action(s)* need attention in `{repo}`.",
            },
        },
        {"type": "divider"},
    ]
    for f in findings:
        bullet = (
            f"*`{f['action']}`*\n"
            f"• Pinned: `{f['current_display']}`\n"
            f"• Latest: `{f['latest_tag']}` (`{f['latest_sha'][:7]}`)\n"
            f"• Files affected: {f['files_count']}"
        )
        if f["tag_pinned_count"]:
            bullet += f"\n:warning: `{f['tag_pinned_count']}` file(s) still pin a tag instead of a SHA"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": bullet}})

    context_text = f"Audit run: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    if pr_url:
        context_text += f"  •  <{pr_url}|View PR>"
    if run_url:
        context_text += f"  •  <{run_url}|View workflow run>"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": context_text}]})

    return {
        "text": f"{len(findings)} GitHub Action(s) need updating in {repo}",
        "blocks": blocks,
    }


def build_markdown_summary(findings: list[dict], repo: str) -> str:
    lines = [
        "## GitHub Actions Version Audit",
        f"**{len(findings)} action(s)** need attention in `{repo}`.",
        "",
        "| Action | Pinned | Latest | Files | Notes |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for f in findings:
        notes = ":warning: Tag pin found" if f["tag_pinned_count"] else ""
        lines.append(
            f"| `{f['action']}` | `{f['current_display']}` | `{f['latest_tag']}` (`{f['latest_sha'][:7]}`) | {f['files_count']} | {notes} |"
        )
    lines.extend(["", f"*Audit run: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*"])
    return "\n".join(lines)


def post_to_slack(webhook: str, payload: dict) -> None:
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode()
        if body.strip() not in ("ok", ""):
            print(f"Slack response: {resp.status} {body}")


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 2

    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip() or None
    repo = os.environ.get("GITHUB_REPOSITORY", "unknown/unknown")
    run_url = None
    if {"GITHUB_SERVER_URL", "GITHUB_RUN_ID"}.issubset(os.environ):
        run_url = f"{os.environ['GITHUB_SERVER_URL']}/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"

    workflows_dir = Path(os.environ.get("WORKFLOWS_DIR", ".github/workflows"))
    if not workflows_dir.is_dir():
        print(f"Workflows directory not found: {workflows_dir}", file=sys.stderr)
        return 2
    skip_prefixes = tuple(p.strip() for p in os.environ.get("SKIP_PREFIXES", "./").split(",") if p.strip())
    create_pr = os.environ.get("CREATE_PR", "false").lower() in ("1", "true", "yes", "on")
    pr_branch = os.environ.get("PR_BRANCH", "github-actions-version-audit")
    pr_base = os.environ.get("PR_BASE", "main")

    occurrences = scan_workflows(workflows_dir, skip_prefixes)
    if not occurrences:
        print(f"No public action references found under {workflows_dir}.")
        return 0

    findings: list[dict] = []
    patched_files: set[Path] = set()
    for owner_repo in sorted(occurrences):
        items = occurrences[owner_repo]
        latest = gh_get(f"/repos/{owner_repo}/releases/latest", token)
        if not latest:
            print(f"[skip] {owner_repo}: no latest release published")
            continue
        latest_tag = latest["tag_name"]
        latest_sha = resolve_tag_sha(owner_repo, latest_tag, token)
        if not latest_sha:
            print(f"[warn] {owner_repo}: could not resolve {latest_tag} to a commit")
            continue

        sha_refs = [ref for _, ref, _ in items if SHA_RE.match(ref)]
        tag_refs = [ref for _, ref, _ in items if not SHA_RE.match(ref)]
        sha_out_of_date = any(ref != latest_sha for ref in sha_refs)
        has_tag_pins = bool(tag_refs)

        if not sha_out_of_date and not has_tag_pins:
            print(f"[ok]   {owner_repo}: pinned to latest {latest_tag} ({latest_sha[:7]})")
            continue

        current_comment = next((c for _, _, c in items if c), None)
        if current_comment:
            current_display = current_comment
        elif sha_refs:
            current_display = sha_refs[0][:7]
        else:
            current_display = tag_refs[0]

        affected_paths = list({path for path, _, _ in items})
        findings.append(
            {
                "action": owner_repo,
                "current_display": current_display,
                "latest_tag": latest_tag,
                "latest_sha": latest_sha,
                "files_count": len(affected_paths),
                "tag_pinned_count": len({path for path, ref, _ in items if not SHA_RE.match(ref)}),
                "items": items,
            }
        )
        print(f"[drift] {owner_repo}: {current_display} -> {latest_tag} ({latest_sha[:7]})")

        # GitHub Actions Annotations
        for path, ref, _ in items:
            msg = f"{owner_repo} is out of date ({current_display} -> {latest_tag})"
            if not SHA_RE.match(ref):
                msg = f"{owner_repo} pins a mutable tag '{ref}' instead of a SHA. Latest: {latest_tag}"
            print(f"::warning file={path}::{msg}")

        if create_pr:
            newly_patched = patch_workflow_files(owner_repo, latest_sha, latest_tag, affected_paths)
            patched_files.update(newly_patched)

    if not findings:
        print(f"\nAll public actions are up to date across {sum(len(v) for v in occurrences.values())} reference(s).")
        return 0

    # Job Summary
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as f:
            f.write(build_markdown_summary(findings, repo))

    pr_url: str | None = None
    pr_error: str | None = None
    if create_pr:
        print("\nOpening pull request...")
        try:
            pr_url = create_pull_request(
                token, repo, pr_branch, pr_base, findings, run_url, list(patched_files)
            )
            if pr_url:
                print(f"PR opened: {pr_url}")
        except Exception as exc:
            pr_error = str(exc)
            print(f"PR creation failed: {pr_error}", file=sys.stderr)

    # Slack Post
    payload = build_slack_payload(findings, repo, run_url, pr_url)
    if not webhook:
        print("\n--- Slack payload (dry-run) ---")
        print(json.dumps(payload, indent=2))
        return 1 if pr_error else 0

    try:
        post_to_slack(webhook, payload)
        print(f"\nPosted to Slack: {len(findings)} finding(s).")
    except urllib.error.HTTPError as e:
        print(f"Slack post failed: {e.code} {e.reason}\n{e.read().decode()}", file=sys.stderr)
        return 1
    return 1 if pr_error else 0


if __name__ == "__main__":
    sys.exit(main())
