#!/usr/bin/env python3
"""Audit GitHub Actions references in workflows and report version drift.

Scans every `uses: owner/repo@ref` reference, asks the GitHub API for each
action's latest release, and reports when any action is out of date or 
still pinned to a mutable tag.

Environment:
- GITHUB_TOKEN (required): used for GitHub API calls.
- SLACK_WEBHOOK_URL (optional): if unset, the payload is printed but not sent.
- GITHUB_STEP_SUMMARY (optional): if set, writes a Markdown report to the 
  GitHub Actions job summary.
- WORKFLOWS_DIR (optional, default .github/workflows): override scan target.
- SKIP_PREFIXES (optional, comma-separated, default ./): action refs starting 
  with any of these are ignored.
"""
from __future__ import annotations

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
        (?P<full>[^/\s@]+/[^@\s]+)
        @(?P<ref>\S+)
        (?:\s*\#\s*(?P<comment>\S+))?
    """,
    re.VERBOSE,
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def gh_get(path: str, token: str) -> dict | None:
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
        raise


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


def build_slack_payload(findings: list[dict], repo: str, run_url: str | None) -> dict:
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

    occurrences = scan_workflows(workflows_dir, skip_prefixes)
    if not occurrences:
        print(f"No public action references found under {workflows_dir}.")
        return 0

    findings: list[dict] = []
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

        findings.append(
            {
                "action": owner_repo,
                "current_display": current_display,
                "latest_tag": latest_tag,
                "latest_sha": latest_sha,
                "files_count": len({path for path, _, _ in items}),
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

    if not findings:
        print(f"\nAll public actions are up to date across {sum(len(v) for v in occurrences.values())} reference(s).")
        return 0

    # Job Summary
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as f:
            f.write(build_markdown_summary(findings, repo))

    # Slack Post
    payload = build_slack_payload(findings, repo, run_url)
    if not webhook:
        print("\n--- Slack payload (dry-run) ---")
        print(json.dumps(payload, indent=2))
        return 0

    try:
        post_to_slack(webhook, payload)
        print(f"\nPosted to Slack: {len(findings)} finding(s).")
    except urllib.error.HTTPError as e:
        print(f"Slack post failed: {e.code} {e.reason}\n{e.read().decode()}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
