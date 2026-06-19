#!/usr/bin/env python3
"""
agent_bridge.py — the two-way bridge between your voice and Claude Code.

This is what lets you SEND INSTRUCTIONS to Claude by voice and then RECEIVE
UPDATES that get summarized back to you. Unlike the old `ask_claude` (which
drove the Claude *Desktop* UI through the accessibility tree — slow and
fragile) this talks to the headless **`claude` CLI** directly:

    claude -p "<your instruction>" --output-format stream-json --verbose

Because real work can take a while (Claude might edit files, run commands,
search the web), we do NOT block the voice loop waiting for it. Instead:

  1. `start_job(instruction)` spawns Claude in the BACKGROUND, detached, and
     streams its JSONL events to /tmp/voiceos-agent/<job_id>/stream.jsonl.
     It returns immediately with a short, speakable job id ("job 3").
  2. `job_status(job_id)` reads that stream at any time and returns a compact,
     spoken-friendly digest: is it still working, what's NEW since you last
     asked (a per-job cursor tracks this), which tools it used, and — when it's
     done — Claude's own final answer. The voice model reads that back to you.

Claude's stream already emits natural-language progress (`post_turn_summary`)
and a final `result`, so "summarize what just happened" is mostly a matter of
surfacing those — the realtime voice model then phrases them out loud.

Cross-platform: works on macOS/Linux and on Windows (where `claude` is a .cmd
shim spawned via the shell, process liveness is checked through the Win32 API
instead of os.kill, and jobs are stopped with taskkill).

Standalone-testable, no OpenAI needed:
    python agent_bridge.py start "create hello.txt with the word hi"
    python agent_bridge.py check            # latest job
    python agent_bridge.py check job-1
    python agent_bridge.py list
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

CLAUDE_BIN = shutil.which("claude") or "claude"

# Where each job's stream + metadata live. One subdir per job. Defaults to the
# OS temp dir (/tmp on Unix, %TEMP% on Windows) so it's valid on every platform.
AGENT_DIR = os.environ.get("VOICEOS_AGENT_DIR") or os.path.join(
    tempfile.gettempdir(), "voiceos-agent")

# Defaults you can override in .env (see .env.example):
#   VOICEOS_CLAUDE_CWD            — directory Claude works in (default: $HOME)
#   VOICEOS_CLAUDE_PERMISSION_MODE— default | acceptEdits | plan | bypassPermissions
#   VOICEOS_CLAUDE_EXTRA_ARGS     — extra raw flags, space-split (e.g. "--model sonnet")
#
# WHY bypassPermissions BY DEFAULT: this is a *hands-free voice* loop — when you
# speak an instruction there is no one sitting at the keyboard to click "approve"
# on each tool call, so any mode that pauses for approval (default/acceptEdits)
# would just stall mid-task. bypassPermissions lets the delegated agent run to
# completion and report back, which is the whole point. It also means the agent
# can edit files and run commands on its own — point VOICEOS_CLAUDE_CWD at a
# project you trust, or set VOICEOS_CLAUDE_PERMISSION_MODE=acceptEdits if you'd
# rather it pause on anything outside the workspace.
# read at call time (see start_job) so a .env loaded after import still applies.
DEFAULT_PERMISSION_MODE = "bypassPermissions"


# ---------------------------------------------------------------------------
# small fs helpers
# ---------------------------------------------------------------------------
def _job_dir(job_id: str) -> str:
    return os.path.join(AGENT_DIR, job_id)


def _read_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f)


def _next_job_id() -> str:
    """Short, speakable ids: job-1, job-2, … (a counter survives in AGENT_DIR)."""
    os.makedirs(AGENT_DIR, exist_ok=True)
    counter = os.path.join(AGENT_DIR, "counter")
    n = _read_json(counter, {"n": 0})["n"] + 1
    _write_json(counter, {"n": n})
    return f"job-{n}"


def _set_latest(job_id: str) -> None:
    _write_json(os.path.join(AGENT_DIR, "latest.json"), {"job_id": job_id})


def latest_job_id() -> str | None:
    return _read_json(os.path.join(AGENT_DIR, "latest.json"), {}).get("job_id")


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours to signal -> still alive
    except OSError:
        return False
    return True


def _pid_alive_windows(pid: int) -> bool:
    # CAREFUL: os.kill(pid, 0) TERMINATES the process on Windows, so we must NOT
    # use it here. Instead ask the OS for the process's exit code — STILL_ACTIVE
    # (259) means it's still running.
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k = ctypes.windll.kernel32
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if not k.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k.CloseHandle(h)


# ---------------------------------------------------------------------------
# starting a job
# ---------------------------------------------------------------------------
def start_job(instruction: str, cwd: str | None = None) -> dict:
    """Spawn a headless Claude Code agent in the background for `instruction`.

    Returns immediately: {status, job_id, instruction}. The work continues
    detached; poll it with job_status(job_id)."""
    instruction = (instruction or "").strip()
    if not instruction:
        return {"status": "error", "error": "empty instruction"}
    if not (shutil.which("claude") or os.path.exists(CLAUDE_BIN)):
        return {"status": "error",
                "error": "the `claude` CLI is not installed (npm i -g @anthropic-ai/claude-code)"}

    job_id = _next_job_id()
    jd = _job_dir(job_id)
    os.makedirs(jd, exist_ok=True)
    stream_path = os.path.join(jd, "stream.jsonl")
    err_path = os.path.join(jd, "stderr.log")

    work_dir = cwd or os.environ.get("VOICEOS_CLAUDE_CWD") or os.path.expanduser("~")

    cmd = [CLAUDE_BIN, "-p", instruction,
           "--output-format", "stream-json", "--verbose"]
    perm = os.environ.get("VOICEOS_CLAUDE_PERMISSION_MODE", DEFAULT_PERMISSION_MODE)
    if perm:
        cmd += ["--permission-mode", perm]
    extra = os.environ.get("VOICEOS_CLAUDE_EXTRA_ARGS", "").strip()
    if extra:
        cmd += extra.split()

    stream_f = open(stream_path, "w")
    err_f = open(err_path, "w")
    popen_kwargs = dict(cwd=work_dir, stdout=stream_f, stderr=err_f,
                        stdin=subprocess.DEVNULL)
    if os.name == "nt":
        # On Windows the npm-installed `claude` is a .cmd shim, which CreateProcess
        # can't launch directly — so go through the shell. CREATE_NO_WINDOW keeps a
        # console from popping up; CREATE_NEW_PROCESS_GROUP detaches it so the job
        # outlives us and stays killable as a tree.
        popen_args = subprocess.list2cmdline(cmd)
        popen_kwargs["shell"] = True
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        popen_args = cmd
        popen_kwargs["start_new_session"] = True  # own session/group: detach + killpg
    try:
        proc = subprocess.Popen(popen_args, **popen_kwargs)
    except Exception as e:  # noqa: BLE001
        stream_f.close()
        err_f.close()
        return {"status": "error", "error": f"could not start claude: {e}"}

    _write_json(os.path.join(jd, "meta.json"), {
        "job_id": job_id,
        "instruction": instruction,
        "pid": proc.pid,
        "cwd": work_dir,
        "started_at": time.time(),
        "cmd": cmd,
    })
    _write_json(os.path.join(jd, "cursor.json"), {"events_seen": 0})
    _set_latest(job_id)
    return {"status": "ok", "job_id": job_id, "instruction": instruction,
            "message": f"Started {job_id}. Ask me for an update any time."}


# ---------------------------------------------------------------------------
# reading a job's progress
# ---------------------------------------------------------------------------
def _parse_stream(stream_path: str) -> list[dict]:
    """Parse the JSONL stream Claude writes; tolerate a half-written last line."""
    events = []
    try:
        with open(stream_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # last line may still be mid-write
    except OSError:
        pass
    return events


def _humanize_event(ev: dict) -> str | None:
    """Turn one raw stream event into a short, speakable line (or None to skip)."""
    t = ev.get("type")
    if t == "assistant":
        parts = []
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                parts.append(block["text"].strip())
            elif block.get("type") == "tool_use":
                name = block.get("name", "tool")
                inp = block.get("input", {})
                hint = inp.get("command") or inp.get("file_path") or inp.get("path") \
                    or inp.get("pattern") or inp.get("description") or ""
                hint = str(hint)
                if len(hint) > 60:
                    hint = hint[:57] + "…"
                parts.append(f"🔧 {name}: {hint}" if hint else f"🔧 {name}")
        return "\n".join(parts) or None
    if t == "system" and ev.get("subtype") == "post_turn_summary":
        return ev.get("status_detail") or None
    if t == "result":
        return None  # handled separately as the final answer
    return None


def _tools_used(events: list[dict]) -> list[str]:
    tools = []
    for ev in events:
        if ev.get("type") == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    name = block.get("name", "tool")
                    if name not in tools:
                        tools.append(name)
    return tools


_WORD_NUM = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
             "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}


def _normalize_job_id(raw: str) -> str:
    """Accept what a voice model might pass: 'job-2', 'job 2', 'job two', '2',
    'two' -> 'job-2'. Empty stays empty (caller falls back to latest)."""
    s = (raw or "").strip().lower()
    if not s:
        return ""
    s = s.replace("job", "").strip(" -_")
    s = _WORD_NUM.get(s, s)
    return f"job-{s}" if s.isdigit() else (raw or "").strip()


def job_status(job_id: str = "", wait: float = 0.0) -> dict:
    """Return a spoken-friendly digest of a job's progress.

    Only reports what's NEW since the last call (per-job cursor), so repeated
    "how's it going?" questions don't re-read the whole transcript. If `wait`
    > 0 and the job is still running, poll up to `wait` seconds for it to
    finish first (handy for "are you done yet?")."""
    job_id = _normalize_job_id(job_id) or latest_job_id() or ""
    if not job_id:
        return {"status": "error", "error": "no jobs yet — delegate something first"}
    jd = _job_dir(job_id)
    meta = _read_json(os.path.join(jd, "meta.json"), None)
    if not meta:
        return {"status": "error", "error": f"no such job {job_id!r}"}

    stream_path = os.path.join(jd, "stream.jsonl")
    deadline = time.monotonic() + max(0.0, float(wait or 0.0))
    while True:
        events = _parse_stream(stream_path)
        result_ev = next((e for e in events if e.get("type") == "result"), None)
        running = _pid_alive(meta.get("pid", 0)) and result_ev is None
        if result_ev is not None or not running or time.monotonic() >= deadline:
            break
        time.sleep(0.6)

    # figure out what the caller hasn't heard yet
    cursor = _read_json(os.path.join(jd, "cursor.json"), {"events_seen": 0})
    seen = cursor.get("events_seen", 0)
    new_events = events[seen:]
    updates = [line for ev in new_events if (line := _humanize_event(ev))]
    _write_json(os.path.join(jd, "cursor.json"), {"events_seen": len(events)})

    elapsed = round(time.time() - meta.get("started_at", time.time()), 1)
    out = {
        "job_id": job_id,
        "instruction": meta.get("instruction", ""),
        "tools_used": _tools_used(events),
        "elapsed_seconds": elapsed,
        "new_updates": updates,
    }

    if result_ev is not None:
        out["status"] = "error" if result_ev.get("is_error") else "done"
        out["result"] = (result_ev.get("result") or "").strip()
        cost = result_ev.get("total_cost_usd")
        if cost is not None:
            out["cost_usd"] = round(cost, 4)
        if result_ev.get("is_error"):
            out["error"] = out.get("result") or _tail(os.path.join(jd, "stderr.log"))
    elif running:
        out["status"] = "running"
        if not updates:
            out["new_updates"] = ["still working…"]
    else:
        # process gone but never wrote a result -> it crashed/was killed
        out["status"] = "stopped"
        out["error"] = _tail(os.path.join(jd, "stderr.log")) or "agent stopped without finishing"
    return out


def _tail(path: str, n: int = 400) -> str:
    try:
        with open(path) as f:
            return f.read()[-n:].strip()
    except OSError:
        return ""


def list_jobs() -> dict:
    """All known jobs, newest first, with their current status."""
    if not os.path.isdir(AGENT_DIR):
        return {"status": "ok", "jobs": []}
    ids = sorted(
        (d for d in os.listdir(AGENT_DIR)
         if d.startswith("job-") and os.path.isdir(_job_dir(d))),
        key=lambda d: int(d.split("-")[1]) if d.split("-")[1].isdigit() else 0,
        reverse=True,
    )
    jobs = []
    for jid in ids:
        meta = _read_json(os.path.join(_job_dir(jid), "meta.json"), {})
        events = _parse_stream(os.path.join(_job_dir(jid), "stream.jsonl"))
        result_ev = next((e for e in events if e.get("type") == "result"), None)
        if result_ev is not None:
            st = "error" if result_ev.get("is_error") else "done"
        elif _pid_alive(meta.get("pid", 0)):
            st = "running"
        else:
            st = "stopped"
        jobs.append({"job_id": jid, "status": st,
                     "instruction": meta.get("instruction", "")})
    return {"status": "ok", "jobs": jobs}


def stop_job(job_id: str = "") -> dict:
    """Kill a running job's process group."""
    job_id = _normalize_job_id(job_id) or latest_job_id() or ""
    meta = _read_json(os.path.join(_job_dir(job_id), "meta.json"), None)
    if not meta:
        return {"status": "error", "error": f"no such job {job_id!r}"}
    pid = meta.get("pid", 0)
    if not _pid_alive(pid):
        return {"status": "ok", "job_id": job_id, "stopped": False,
                "note": "already finished"}
    try:
        if os.name == "nt":
            # /T kills the whole tree (shell -> claude.cmd -> node).
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        return {"status": "ok", "job_id": job_id, "stopped": True}
    except (ProcessLookupError, OSError):
        return {"status": "ok", "job_id": job_id, "stopped": False,
                "note": "already finished"}


# ---------------------------------------------------------------------------
# CLI for standalone testing (no OpenAI needed)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "help"
    if cmd == "start" and len(args) > 1:
        print(json.dumps(start_job(" ".join(args[1:])), indent=2))
    elif cmd == "check":
        jid = args[1] if len(args) > 1 else ""
        print(json.dumps(job_status(jid, wait=0.0), indent=2))
    elif cmd == "wait":
        jid = args[1] if len(args) > 1 else ""
        print(json.dumps(job_status(jid, wait=120.0), indent=2))
    elif cmd == "list":
        print(json.dumps(list_jobs(), indent=2))
    elif cmd == "stop":
        print(json.dumps(stop_job(args[1] if len(args) > 1 else ""), indent=2))
    else:
        print("usage: python agent_bridge.py {start <instruction>|check [job]|"
              "wait [job]|list|stop [job]}")
        sys.exit(1)
