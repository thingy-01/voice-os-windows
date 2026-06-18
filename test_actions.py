#!/usr/bin/env python3
"""
test_actions.py — standalone smoke tests for the Windows tools.

No OpenAI key needed. Each test calls a tool with safe arguments and checks it
returns a dict with a 'status' field (and that nothing throws). Most tools need
their target app present to return status == "ok"; on a bare machine they should
still return a structured error, NOT crash — that's what we assert by default.

Usage:
    python test_actions.py                 # run the safe subset
    python test_actions.py open_app play_music   # run only these
    python test_actions.py --strict        # require status == "ok" (real machine)
"""
from __future__ import annotations

import json
import sys

import actions

STRICT = "--strict" in sys.argv
names = [a for a in sys.argv[1:] if not a.startswith("--")]

# (tool, kwargs) — safe, non-destructive invocations.
CASES = {
    "open_app": {"name": "Notepad"},
    "take_note": {"text": "voice-os windows smoke test"},
    "read_screen_aloud": {"app": "Notepad"},
    "web_search": {"query": "openai realtime api"},
    "play_music": {"query": ""},
    "obs_scene": {"name": "Scene"},
    "start_obs_recording": {},
    "premiere_control": {"action": "pause"},
    "click_link": {"position": "first"},
    "ask_claude": {"question": ""},
    "run_terminal": {"prompt": "echo hi"},
}
# Skipped from the default run because they change state / need a live app.
DESTRUCTIVE = {"start_obs_recording", "run_terminal"}


def run(name: str) -> bool:
    fn = actions.TOOLS[name]
    kwargs = CASES.get(name, {})
    try:
        result = fn(**kwargs)
    except Exception as e:
        print(f"✗  {name}: RAISED {type(e).__name__}: {e}")
        return False
    if not isinstance(result, dict) or "status" not in result:
        print(f"✗  {name}: bad return shape: {result!r}")
        return False
    status = result.get("status")
    ok = (status == "ok") if STRICT else (status in ("ok", "error", "empty"))
    mark = "✓" if ok else "✗"
    print(f"{mark}  {name}: status={status}  {json.dumps(result, ensure_ascii=False)[:120]}")
    return ok


def main():
    selected = names or [n for n in CASES if n not in DESTRUCTIVE]
    print(f"Running {len(selected)} tool test(s)  (strict={STRICT})\n")
    results = {n: run(n) for n in selected if n in actions.TOOLS}
    passed = sum(results.values())
    print(f"\n{passed}/{len(results)} passed.")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
