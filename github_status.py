#!/usr/bin/env python3
"""
github_status.py — check what a cloud Claude Code session pushed to GitHub.

When you kick off a Claude Code job "in the cloud" (e.g. from the Claude phone
app, running in a cloud container that pushes to a repo), the local voice bridge
(agent_bridge.py) can't see inside that container — but everything the session
does lands on GitHub. This reads that signal back: the latest commit, open pull
requests, and CI state, summarized for the voice loop to speak.

No dependencies — just the GitHub REST API over urllib.

Config (.env):
  VOICEOS_GITHUB_REPO=owner/name   default repo to check
  GITHUB_TOKEN=ghp_...             needed for private repos / higher rate limits

Standalone:
    python github_status.py owner/name
    python github_status.py owner/name 42        # a specific PR
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "voice-os-windows"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _api(path: str):
    """GET an API path. Returns parsed JSON, or {'_error': msg} on failure."""
    req = urllib.request.Request(API + path, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"_error": "repo or PR not found (if it's private, set GITHUB_TOKEN)"}
        if e.code in (401,):
            return {"_error": "GITHUB_TOKEN is invalid"}
        if e.code in (403, 429):
            return {"_error": "GitHub rate limit hit — set GITHUB_TOKEN for a higher limit"}
        return {"_error": f"GitHub API error {e.code}"}
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"_error": f"could not reach GitHub: {e}"}


def _rel_time(iso: str) -> str:
    """'2026-06-19T12:34:56Z' -> 'about 5 minutes ago' (speakable)."""
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    secs = (datetime.now(timezone.utc) - t).total_seconds()
    if secs < 90:
        return "just now"
    mins = secs / 60
    if mins < 60:
        return f"{round(mins)} minutes ago"
    hours = mins / 60
    if hours < 24:
        return f"{round(hours)} hours ago"
    return f"{round(hours / 24)} days ago"


def _ci_for_ref(repo: str, ref: str) -> str:
    """Summarize CI for a commit: passing / failing / running / none."""
    if not ref:
        return "none"
    data = _api(f"/repos/{repo}/commits/{ref}/check-runs")
    runs = data.get("check_runs") if isinstance(data, dict) else None
    if not runs:
        # fall back to the legacy combined status (older CI integrations)
        st = _api(f"/repos/{repo}/commits/{ref}/status")
        state = st.get("state") if isinstance(st, dict) else None
        if state == "success":
            return "passing"
        if state == "failure":
            return "failing"
        if state == "pending" and st.get("total_count"):
            return "running"
        return "none"
    if any(r.get("status") != "completed" for r in runs):
        return "running"
    if any(r.get("conclusion") in ("failure", "timed_out", "cancelled") for r in runs):
        return "failing"
    if all(r.get("conclusion") in ("success", "neutral", "skipped") for r in runs):
        return "passing"
    return "mixed"


def repo_status(repo: str = "", pr: str = "", branch: str = "") -> dict:
    """Summarize recent GitHub activity on `repo` (default VOICEOS_GITHUB_REPO):
    latest commit, open PRs, and CI state. Pass `pr` to focus one pull request,
    or `branch` to read a specific branch's tip."""
    repo = (repo or os.environ.get("VOICEOS_GITHUB_REPO") or "").strip().strip("/")
    if not repo or "/" not in repo:
        return {"status": "error",
                "error": "no repo — say a repo like 'owner/name' or set VOICEOS_GITHUB_REPO"}

    if pr:
        pr = str(pr).lstrip("#").strip()
        data = _api(f"/repos/{repo}/pulls/{pr}")
        if "_error" in data:
            return {"status": "error", "error": data["_error"]}
        sha = (data.get("head") or {}).get("sha", "")
        ci = _ci_for_ref(repo, sha)
        merged = data.get("merged")
        state = "merged" if merged else data.get("state", "?")
        return {
            "status": "ok", "repo": repo,
            "pr": {"number": data.get("number"), "title": data.get("title"),
                   "state": state, "mergeable_state": data.get("mergeable_state"),
                   "updated": _rel_time(data.get("updated_at", ""))},
            "ci": ci,
            "summary": f"PR #{data.get('number')} \"{data.get('title')}\" is {state}; "
                       f"CI {ci}; updated {_rel_time(data.get('updated_at', ''))}.",
        }

    ref = branch.strip() if branch else ""
    commits = _api(f"/repos/{repo}/commits?per_page=1" + (f"&sha={ref}" if ref else ""))
    if isinstance(commits, dict) and "_error" in commits:
        return {"status": "error", "error": commits["_error"]}
    latest = commits[0] if isinstance(commits, list) and commits else None
    commit_info = {}
    ci = "none"
    if latest:
        c = latest.get("commit", {})
        msg = (c.get("message") or "").splitlines()[0]
        commit_info = {
            "message": msg,
            "author": (c.get("author") or {}).get("name", ""),
            "when": _rel_time((c.get("author") or {}).get("date", "")),
            "sha": (latest.get("sha") or "")[:7],
        }
        ci = _ci_for_ref(repo, latest.get("sha", ""))

    prs = _api(f"/repos/{repo}/pulls?state=open&sort=updated&direction=desc&per_page=5")
    open_prs = []
    if isinstance(prs, list):
        for p in prs:
            open_prs.append({"number": p.get("number"), "title": p.get("title"),
                             "updated": _rel_time(p.get("updated_at", ""))})

    where = f" on {ref}" if ref else ""
    if commit_info:
        summary = (f"Latest commit{where}: \"{commit_info['message']}\" "
                   f"{commit_info['when']} by {commit_info['author']}; CI {ci}. ")
    else:
        summary = f"No commits found{where}. "
    if open_prs:
        summary += (f"{len(open_prs)} open PR" + ("s" if len(open_prs) != 1 else "")
                    + ": " + "; ".join(f"#{p['number']} {p['title']}" for p in open_prs[:3]) + ".")
    else:
        summary += "No open pull requests."

    return {"status": "ok", "repo": repo, "latest_commit": commit_info,
            "ci": ci, "open_prs": open_prs, "summary": summary}


if __name__ == "__main__":
    args = sys.argv[1:]
    repo = args[0] if args else ""
    pr = args[1] if len(args) > 1 else ""
    print(json.dumps(repo_status(repo, pr), indent=2))
