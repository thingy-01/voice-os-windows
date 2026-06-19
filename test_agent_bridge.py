#!/usr/bin/env python3
"""
test_agent_bridge.py — real end-to-end proof of the voice↔Claude-Code bridge.

No OpenAI / no mic needed: this drives agent_bridge.py against the actual
`claude` CLI to prove the whole loop you asked for —
    send an instruction  ->  it runs in the background  ->  poll for updates
    ->  it finishes  ->  we get a summarized result back.

It runs Claude in a throwaway temp dir with acceptEdits so the test is safe and
works on any account (including CI / root, where bypassPermissions is refused).

    python test_agent_bridge.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

if not shutil.which("claude"):
    sys.exit("SKIP: `claude` CLI not installed — nothing to test.")

# sandbox: throwaway workspace + agent state dir, and a safe permission mode so
# in-workspace writes auto-accept without a human approving each tool call.
work = tempfile.mkdtemp(prefix="bridge-test-work-")
state = tempfile.mkdtemp(prefix="bridge-test-state-")
os.environ["VOICEOS_CLAUDE_CWD"] = work
os.environ["VOICEOS_AGENT_DIR"] = state
os.environ["VOICEOS_CLAUDE_PERMISSION_MODE"] = "acceptEdits"

import agent_bridge  # noqa: E402  (after env is set)

FAILS = []


def check(name, cond, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILS.append(name)


try:
    print("1. delegate an instruction (non-blocking start)")
    started = agent_bridge.start_job(
        "Create a file named proof.txt in the current directory whose only "
        "contents are the word BRIDGE_OK, then confirm in one short sentence.")
    print("   ->", started)
    check("start returns a job id", started.get("status") == "ok" and started.get("job_id"))
    job_id = started.get("job_id")

    print("2. poll for updates until it finishes (wait up to 120s)")
    final = agent_bridge.job_status(job_id, wait=120.0)
    print("   status:", final.get("status"))
    print("   tools_used:", final.get("tools_used"))
    print("   result:", (final.get("result") or "")[:120])
    check("job reached done", final.get("status") == "done", final.get("status"))
    check("it actually used a tool", bool(final.get("tools_used")))
    check("we got a summarized result back", bool(final.get("result")))

    print("3. the work really happened (file on disk)")
    proof = os.path.join(work, "proof.txt")
    content = ""
    if os.path.exists(proof):
        content = open(proof).read().strip()
    check("proof.txt exists with expected content", "BRIDGE_OK" in content, content[:40])

    print("4. cursor: asking again shows no repeated updates")
    again = agent_bridge.job_status(job_id)
    check("no new updates on re-check", again.get("new_updates") == [])

    print("5. spoken job-id normalization + 'latest' fallback")
    n = agent_bridge._normalize_job_id
    check("'job two' -> job-2", n("job two") == "job-2")
    check("'2' -> job-2", n("2") == "job-2")
    check("empty id falls back to latest", agent_bridge.job_status("").get("job_id") == job_id)

    print("6. list shows the job as done")
    jobs = agent_bridge.list_jobs().get("jobs", [])
    check("list includes our finished job",
          any(j["job_id"] == job_id and j["status"] == "done" for j in jobs))
finally:
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(state, ignore_errors=True)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
    sys.exit(1)
print("ALL CHECKS PASSED ✅  the voice↔Claude bridge works end to end.")
