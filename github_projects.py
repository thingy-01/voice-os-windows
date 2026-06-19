#!/usr/bin/env python3
"""
github_projects.py — read a GitHub Projects (v2) board by voice.

The new GitHub Projects ("Projects v2", the board at github.com/users/<you>/
projects/<n> or github.com/orgs/<org>/projects/<n>) is GraphQL-only — the REST
API in github_status.py can't see it. This module talks to the GraphQL API so the
voice loop can answer "what's on my jurytics board?", "what's in progress?", and
"tell me about the login ticket".

No dependencies — GraphQL over urllib, same token as github_status.

Config (.env):
  GITHUB_TOKEN=ghp_...                            needs the read:project scope
  VOICEOS_GITHUB_PROJECT=https://github.com/users/<owner>/projects/3
                                                  (a URL, 'owner/number', or just a
                                                   number — the default board)
  VOICEOS_GITHUB_OWNER=<login>                    default board owner (optional)

Standalone:
    python github_projects.py                      # the default board (or list yours)
    python github_projects.py jurytics             # find a board by name
    python github_projects.py "owner/3"            # a specific board
    python github_projects.py 3 "in progress"      # filter by status column
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
import urllib.error
import urllib.request

# Reuse the REST plumbing (auth headers, token check, name-normalizer, rel time).
from github_status import _headers, _have_token, _norm, _rel_time, _api  # noqa: F401

API_GQL = "https://api.github.com/graphql"


# ---------------------------------------------------------------------------
# GraphQL plumbing
# ---------------------------------------------------------------------------
def _graphql(query: str, variables: dict) -> dict:
    """POST a GraphQL query. Returns parsed `data`, or {'_error': msg}."""
    if not _have_token():
        return {"_error": "set GITHUB_TOKEN in .env (with the read:project scope) "
                          "so I can read your project boards"}
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        API_GQL, data=body,
        headers={**_headers(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401,):
            return {"_error": "GITHUB_TOKEN is invalid"}
        if e.code in (403, 429):
            return {"_error": "GitHub rate limit hit, or your token lacks the "
                              "read:project scope"}
        return {"_error": f"GitHub GraphQL error {e.code}"}
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"_error": f"could not reach GitHub: {e}"}
    if payload.get("errors"):
        return {"_error": _gql_error(payload["errors"])}
    return payload.get("data") or {}


def _gql_error(errors: list) -> str:
    """Turn a GraphQL errors array into one speakable line."""
    msg = (errors[0].get("message") if errors else "") or "GraphQL request failed"
    types = {e.get("type", "") for e in errors}
    if "INSUFFICIENT_SCOPES" in types or "scope" in msg.lower():
        return "your GITHUB_TOKEN needs the read:project scope to read project boards"
    return msg


def _owner_query(template: str, variables: dict, field: str) -> dict:
    """Run a query that hangs off an owner, trying `user` then `organization`
    (a board can belong to either). `template` contains the literal OWNER, which
    we swap for each kind. Returns {field: value, '_kind': kind} or {'_error':...}."""
    last_err = None
    for kind in ("user", "organization"):
        data = _graphql(template.replace("OWNER", kind), variables)
        if "_error" in data:
            # auth/scope problems won't improve by trying the other kind — bail now.
            if "scope" in data["_error"] or "token" in data["_error"] or "invalid" in data["_error"]:
                return data
            last_err = data["_error"]
            continue
        node = data.get(kind)
        if node and node.get(field) is not None:
            return {field: node[field], "_kind": kind}
    return {"_error": last_err or
            "not found — check the owner, the project number, and the token's read:project scope"}


# ---------------------------------------------------------------------------
# which board?
# ---------------------------------------------------------------------------
def _parse_project_ref(s: str) -> tuple[str, int]:
    """Pull (owner, number) out of a project URL, 'owner/number', or bare number."""
    s = (s or "").strip()
    m = re.search(r"/(?:users|orgs)/([^/]+)/projects/(\d+)", s)
    if m:
        return m.group(1), int(m.group(2))
    m = re.match(r"^([^/\s]+)/(\d+)$", s)
    if m:
        return m.group(1), int(m.group(2))
    m = re.match(r"^#?(\d+)$", s)
    if m:
        return "", int(m.group(1))
    return "", 0


def _viewer_login() -> str:
    me = _api("/user")
    return me.get("login", "") if isinstance(me, dict) and "_error" not in me else ""


_LIST_Q = """
query($owner:String!){
  OWNER(login:$owner){
    projectsV2(first:50){ nodes{ number title url closed shortDescription } }
  }
}"""


def _list_projects(owner: str) -> list | dict:
    res = _owner_query(_LIST_Q, {"owner": owner}, "projectsV2")
    if "_error" in res:
        return res
    nodes = (res["projectsV2"] or {}).get("nodes") or []
    return [p for p in nodes if not p.get("closed")]


def _best_project(text: str, projects: list) -> tuple[int, list]:
    """Fuzzy-match a spoken board name to a project number. Returns
    (number_or_0, candidate_labels) — same confident/ambiguous shape as the repo
    resolver in github_status."""
    nt = _norm(text)
    if not nt:
        return 0, []
    scored = []
    for p in projects:
        title = _norm(p.get("title", ""))
        if not title:
            continue
        if title == nt:
            return p["number"], []
        score = difflib.SequenceMatcher(None, nt, title).ratio()
        if nt in title or title in nt:
            score = max(score, 0.85)
        scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [p for s, p in scored if s >= 0.5][:4]
    labels = [f'#{p["number"]} {p["title"]}' for p in top]
    if top and scored[0][0] >= 0.8 and (len(scored) < 2 or scored[0][0] - scored[1][0] >= 0.15):
        return top[0]["number"], labels
    return 0, labels


def _resolve_project(query: str = "", owner: str = "") -> dict:
    """Decide which board to read. Returns one of:
      {"owner", "number"}                  -> go read it
      {"candidates": [...], "summary"}     -> ask which board
      {"error": "..."}                     -> can't."""
    qo, qn = _parse_project_ref(query) if query else ("", 0)
    eo, en = _parse_project_ref(os.environ.get("VOICEOS_GITHUB_PROJECT", ""))
    owner = (owner or qo or eo or os.environ.get("VOICEOS_GITHUB_OWNER", "")
             or _viewer_login())
    if not owner:
        return {"error": "I couldn't tell whose board to read — set GITHUB_TOKEN "
                         "(and optionally VOICEOS_GITHUB_OWNER)"}
    number = qn or en
    # A bare-number query overrides the env default; a name query needs lookup.
    name_query = query.strip() if (query and not qn) else ""
    if number and not name_query:
        return {"owner": owner, "number": number}

    projects = _list_projects(owner)
    if isinstance(projects, dict):  # error
        return {"error": projects["_error"]}
    if not projects:
        return {"error": f"{owner} has no open project boards"}
    if name_query:
        n, labels = _best_project(name_query, projects)
        if n:
            return {"owner": owner, "number": n}
        if labels:
            return {"candidates": labels,
                    "summary": f"I couldn't pin down '{name_query}'. Did you mean "
                               + " or ".join(labels) + "?"}
        return {"error": f"{owner} has no board like '{name_query}'"}
    if number:
        return {"owner": owner, "number": number}
    if len(projects) == 1:
        return {"owner": owner, "number": projects[0]["number"]}
    labels = [f'#{p["number"]} {p["title"]}' for p in projects[:6]]
    return {"candidates": labels,
            "summary": "Which board? " + " or ".join(labels) + "."}


# ---------------------------------------------------------------------------
# reading the board
# ---------------------------------------------------------------------------
_ITEMS_Q = """
query($owner:String!, $number:Int!){
  OWNER(login:$owner){
    projectV2(number:$number){
      title
      url
      items(first:100){
        nodes{
          content{
            __typename
            ... on Issue { number title state url repository{nameWithOwner}
                           assignees(first:5){nodes{login}} }
            ... on PullRequest { number title state url repository{nameWithOwner}
                                 assignees(first:5){nodes{login}} }
            ... on DraftIssue { title body }
          }
          status: fieldValueByName(name:"Status"){
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
        }
      }
    }
  }
}"""


def _flatten_items(project: dict) -> list:
    """One flat list of tickets: {title, number, status, state, type, repo, assignees, url, body}."""
    out = []
    for node in ((project.get("items") or {}).get("nodes") or []):
        c = node.get("content") or {}
        if not c:
            continue  # redacted / inaccessible item
        status = (node.get("status") or {}).get("name") or "No status"
        out.append({
            "title": c.get("title", "(untitled)"),
            "number": c.get("number"),
            "status": status,
            "state": (c.get("state") or "").lower(),  # open/closed/merged for issues & PRs
            "type": c.get("__typename", ""),
            "repo": (c.get("repository") or {}).get("nameWithOwner", ""),
            "assignees": [a.get("login") for a in ((c.get("assignees") or {}).get("nodes") or [])],
            "url": c.get("url", ""),
            "body": (c.get("body") or "").strip(),
        })
    return out


def _ticket_phrase(t: dict) -> str:
    """A short speakable phrase for one ticket."""
    head = f"#{t['number']} {t['title']}" if t.get("number") else t["title"]
    who = f" ({', '.join(t['assignees'])})" if t["assignees"] else ""
    return head + who


def project_board(query: str = "", status: str = "") -> dict:
    """List the tickets on a GitHub Projects board, grouped by Status column, and
    summarize them for the voice loop. `query` picks the board (a name like
    'jurytics', a URL, 'owner/number', or a bare number; omit for your default /
    only board). `status` optionally filters to one column, e.g. 'in progress'."""
    chosen = _resolve_project(query)
    if "error" in chosen:
        return {"status": "error", "error": chosen["error"]}
    if "candidates" in chosen:
        return {"status": "needs_disambiguation", "candidates": chosen["candidates"],
                "summary": chosen["summary"]}

    res = _owner_query(_ITEMS_Q, {"owner": chosen["owner"], "number": chosen["number"]},
                       "projectV2")
    if "_error" in res:
        return {"status": "error", "error": res["_error"]}
    project = res["projectV2"]
    if not project:
        return {"status": "error",
                "error": f"I couldn't open project #{chosen['number']} for {chosen['owner']}"}

    items = _flatten_items(project)
    title = project.get("title", "board")

    # group by status, preserving first-seen column order
    groups: dict[str, list] = {}
    for t in items:
        groups.setdefault(t["status"], []).append(t)

    wanted = _norm(status)
    if wanted:
        groups = {k: v for k, v in groups.items()
                  if wanted in _norm(k) or _norm(k) in wanted}
        if not groups:
            cols = ", ".join(dict.fromkeys(t["status"] for t in items)) or "none"
            return {"status": "ok", "board": title, "tickets": [],
                    "summary": f"Nothing on {title} is in '{status}'. Columns: {cols}."}

    parts = []
    for col, ts in groups.items():
        names = "; ".join(_ticket_phrase(t) for t in ts[:5])
        more = f" (+{len(ts) - 5} more)" if len(ts) > 5 else ""
        parts.append(f"{col}: {len(ts)} — {names}{more}")

    shown = sum(len(v) for v in groups.values())
    lead = (f"{title}: {shown} ticket" + ("s" if shown != 1 else "")
            + (f" matching '{status}'" if wanted else "") + ". ")
    summary = lead + ". ".join(parts) if parts else f"{title} is empty."

    return {"status": "ok", "board": title, "url": project.get("url", ""),
            "owner": chosen["owner"], "number": chosen["number"],
            "tickets": [t for ts in groups.values() for t in ts],
            "summary": summary}


def ticket_details(which: str = "", board: str = "") -> dict:
    """Read one ticket in depth: its status, state, assignees, body, and latest
    comments. `which` is a ticket number ('12', '#12') or part of its title
    ('the login bug'); `board` optionally names which project board to look on."""
    res = project_board(board)
    if res["status"] != "ok":
        return res  # disambiguation / error bubbles up
    items = res["tickets"]
    if not items:
        return {"status": "error", "error": f"{res['board']} has no tickets to open"}

    target = (which or "").strip().lstrip("#")
    t = None
    if target.isdigit():
        t = next((i for i in items if str(i.get("number")) == target), None)
    if t is None and target:
        nt = _norm(target)
        scored = sorted(
            ((difflib.SequenceMatcher(None, nt, _norm(i["title"])).ratio(), i) for i in items),
            key=lambda x: x[0], reverse=True)
        if scored and scored[0][0] >= 0.4:
            t = scored[0][1]
    if t is None:
        return {"status": "error",
                "error": f"I couldn't find a ticket matching '{which}' on {res['board']}"}

    detail = {"title": t["title"], "number": t.get("number"), "status": t["status"],
              "state": t["state"], "assignees": t["assignees"], "repo": t["repo"],
              "url": t["url"], "type": t["type"]}

    body = t["body"]
    comments = []
    # Real issues/PRs have a body + comments over REST; draft items carry their own body.
    if t["repo"] and t.get("number"):
        issue = _api(f"/repos/{t['repo']}/issues/{t['number']}")
        if isinstance(issue, dict) and "_error" not in issue:
            body = (issue.get("body") or "").strip()
            if issue.get("comments"):
                cs = _api(f"/repos/{t['repo']}/issues/{t['number']}/comments?per_page=100")
                if isinstance(cs, list):
                    for c in cs[-2:]:  # the two most recent
                        comments.append({
                            "who": (c.get("user") or {}).get("login", ""),
                            "when": _rel_time(c.get("created_at", "")),
                            "text": (c.get("body") or "").strip()[:400]})
    detail["body"] = body[:600]

    who = f", assigned to {', '.join(t['assignees'])}" if t["assignees"] else ""
    head = (f"#{t['number']} {t['title']}" if t.get("number") else t["title"])
    summary = f"{head} — {t['status']}" + (f", {t['state']}" if t["state"] else "") + who + ". "
    if body:
        summary += body[:300].replace("\n", " ")
        if len(body) > 300:
            summary += "…"
    if comments:
        last = comments[-1]
        summary += f" Latest comment, {last['who']} {last['when']}: {last['text'][:200]}"

    detail["recent_comments"] = comments
    detail["summary"] = summary.strip()
    detail["status"] = "ok"
    return detail


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] in ("list", "boards", "--list"):
        owner = args[1] if len(args) > 1 else (os.environ.get("VOICEOS_GITHUB_OWNER") or _viewer_login())
        print(json.dumps(_list_projects(owner), indent=2))
    elif len(args) >= 2 and args[0] in ("ticket", "show"):
        print(json.dumps(ticket_details(args[1], args[2] if len(args) > 2 else ""), indent=2))
    else:
        q = args[0] if args else ""
        st = args[1] if len(args) > 1 else ""
        print(json.dumps(project_board(q, st), indent=2))
