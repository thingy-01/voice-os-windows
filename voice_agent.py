#!/usr/bin/env python3
"""
voice_agent.py — gpt-realtime-2 speech-to-speech control of Windows.

Ported from the macOS original. The realtime loop (mic <-> model <-> tools) is
unchanged in spirit; the only Windows-specific swap is the global hotkey backend
(hotkey.py, using the `keyboard` library instead of the macOS Carbon shim) and
temp-file paths.

Modes:
  --hotkey [KEY]    HOLD a global key to talk (default). KEY defaults to f13.
  --detect-key      print the name/scan code of any key you press, then exit
                    (use it to find what your mouse button emits).
  --push-to-talk    press ENTER to talk
  (no flag)         wake word "hey chat"  (streams mic continuously)

Requires OPENAI_API_KEY with Realtime access. Run via run.bat. Ctrl-C to quit.
"""
from __future__ import annotations

import array
import asyncio
import base64
import json
import math
import os
import queue
import re
import sys
import tempfile
import time

import hotkey

_t_release = 0.0


def _load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from a local .env into os.environ (no dependency).
    Lets `python voice_agent.py` work without the run.bat/run.ps1 launcher."""
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(here):
        return
    try:
        with open(here, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_dotenv()

try:
    import sounddevice as sd
    import websockets
except ImportError:
    sys.exit("Missing deps. Run: pip install -r requirements-windows.txt")

import actions


def _arg_value(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("--"):
            return sys.argv[i + 1]
    return default


PTT = "--push-to-talk" in sys.argv
# Default experience for this build is HOLD-TO-TALK on F13 — a key no physical
# keyboard has, so it never clashes; bind it to a spare mouse button for
# radio-style push-to-talk. Override with --hotkey <key|scancode> or VOICEOS_HOTKEY.
_explicit_wake = "--wake" in sys.argv
_HOTKEY_DEFAULT = os.environ.get("VOICEOS_HOTKEY", "f13")
HOTKEY_NAME = _arg_value("--hotkey", _HOTKEY_DEFAULT) if ("--hotkey" in sys.argv) else None
if not PTT and not _explicit_wake and HOTKEY_NAME is None:
    HOTKEY_NAME = _HOTKEY_DEFAULT  # default mode
HOTKEY_MODE = HOTKEY_NAME is not None and not PTT
WAKE_MODE = not (PTT or HOTKEY_MODE)

MIC_NAME = _arg_value("--mic", os.environ.get("VOICEOS_MIC"))

MODEL = "gpt-realtime-2"
URL = f"wss://api.openai.com/v1/realtime?model={MODEL}"
SAMPLE_RATE = 24000
CHANNELS = 1
BLOCK = 2400
OUT_BLOCK = 4800
PRIME_BYTES = SAMPLE_RATE * 2 * 300 // 1000
_TMP = tempfile.gettempdir()
EVENT_LOG = os.path.join(_TMP, "voiceos-events.log")
HUD_FILE = os.path.join(_TMP, "voiceos-hud.json")

VOICE = "marin"
WAKE_WORD = "hey chat"
_WAKE_RE = re.compile(r"^\s*(hey|hay|a|hi)\s+(chat|chad|chap|chats|chatt|chett|chet|jack)\b")

INSTRUCTIONS = (
    "You are the voice operating system for this Windows PC. The user speaks a "
    "command to control the computer. Call exactly one matching tool, then give a "
    "short, natural spoken confirmation.\n"
    "ROUTING RULES (follow exactly):\n"
    "- LAUNCH or open an app (including the Claude app): open_app. Just opening the "
    "Claude app with no question = open_app('Claude').\n"
    "- 'Open the YouTube Script project' (a Claude PROJECT, not an app): call "
    "ask_claude with an EMPTY question — it just navigates into the project.\n"
    "- If the user wants Claude to WRITE, REWRITE, SUGGEST, or answer anything: call "
    "ask_claude with the request as the question. NEVER answer on Claude's behalf.\n"
    "- a general question, explanation, idea, or a quick 'ask Claude ...' with NO "
    "machine work: claude_chat — its reply is Claude's; read it back aloud naturally.\n"
    "- DELEGATE REAL WORK to Claude Code (build/edit/run code, multi-step tasks on "
    "this PC): 'ask Claude to build/fix/write/refactor ...', 'have Claude make ...', "
    "'tell the agent to ...' -> delegate_to_claude with the full request. It starts a "
    "BACKGROUND job and returns immediately; say you've started and the user can ask "
    "for an update. NEVER do the task yourself or invent results.\n"
    "- GET AN UPDATE on a delegated job: 'how's it going', 'is it done', 'what did "
    "Claude do', 'any update' -> check_claude (no id = latest job). 'stop that' / "
    "'cancel the agent' -> stop_claude.\n"
    "- RECENT WORK / CLOUD JOB / GITHUB: check_claude is ONLY for jobs started here "
    "by voice; for anything pushed to GitHub (incl. cloud jobs from the phone app) "
    "use github_status. 'what have I been working on', 'catch me up', 'any recent "
    "activity' -> github_status with NO repo (it auto-discovers your recent repos). "
    "'how's the <project> update going', 'is the PR done', 'status of <owner/repo>' "
    "-> github_status with that repo. 'have Claude review my recent work', 'what "
    "should I work on next', 'catch me up and tell me what to do next' -> "
    "review_with_claude, then check_claude a few seconds later to read its briefing.\n"
    "- ACT ON CLAUDE'S SUGGESTION: after you've relayed a Claude review or proposed "
    "next step, if the user says 'do the first thing you suggested', 'go ahead with "
    "that', 'okay do it', 'make it happen', 'continue' -> continue_claude (it resumes "
    "Claude's SAME session so it acts on its own plan). Use delegate_to_claude only "
    "for a brand-new task unrelated to what Claude was just doing.\n"
    "- music in CIDER (the user says 'Cider', or 'next/pause/play X in Cider'): cider_control. "
    "Plain 'play music' with no app named = play_music (Spotify).\n"
    "- play music: play_music. control Premiere: premiere_control. read the screen: "
    "read_screen_aloud. start recording: start_obs_recording. switch OBS scene: obs_scene.\n"
    "- search the web / 'open the X docs' / look something up: web_search.\n"
    "- 'click the first link' / 'open the first result': click_link.\n"
    "- 'take a note: ...' / 'note that ...' / 'write this down': take_note.\n"
    "Only call a tool when the command is clear; if it's just a fragment, ask the "
    "user to repeat it. 'Throw on / put on a song' means play_music. Keep replies brief."
)

# Tool schemas — identical contract to the macOS build so nothing downstream changes.
TOOLS = [
    {"type": "function", "name": "open_app",
     "description": "Launch or focus any installed Windows app by name (fuzzy-matched against the whole Start menu — Spotify, OBS, Chrome, Premiere, Discord, Steam, Word, Calculator, Settings, any game, etc.).",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"type": "function", "name": "open_thing",
     "description": "Open ANYTHING by target: a file or folder path, a URL, a Windows settings page (ms-settings:bluetooth), a protocol URI (spotify:, steam://run/<id>), or an app name. Use for 'open my downloads folder', 'open bluetooth settings', 'open C:\\\\report.pdf'. For a plain app name, open_app is also fine.",
     "parameters": {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}},
    {"type": "function", "name": "web_search",
     "description": "Open the browser and search the web. Use for 'search for X', 'look up X', 'google X', 'open the X docs'.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"type": "function", "name": "click_link",
     "description": "Click a search result in the browser — 'click the first link', 'open the second result'.",
     "parameters": {"type": "object", "properties": {"position": {"type": "string", "description": "first / second / third"}}, "required": []}},
    {"type": "function", "name": "take_note",
     "description": "Save a note. Use for 'take a note: ...', 'note that ...', 'write this down: ...'.",
     "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"type": "function", "name": "play_music",
     "description": "Open Spotify and play music. Pass a query like 'Tchaikovsky', or empty to resume.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": []}},
    {"type": "function", "name": "run_terminal",
     "description": "Open a terminal and start a Claude Code session with the given prompt.",
     "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]}},
    {"type": "function", "name": "read_screen_aloud",
     "description": "Read back the text currently visible in an app (default Notepad).",
     "parameters": {"type": "object", "properties": {"app": {"type": "string"}}, "required": []}},
    {"type": "function", "name": "start_obs_recording",
     "description": "Open OBS and start recording.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "function", "name": "stop_obs_recording",
     "description": "Stop the OBS recording.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "function", "name": "obs_scene",
     "description": "Switch OBS to a scene by name (fuzzy). 'switch to my talking head scene', 'change the scene to X'.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"type": "function", "name": "ask_claude",
     "description": "Ask Claude (in Claude Desktop's project) to write/rewrite/suggest something and get its reply. Opens the project itself, then asks. Use whenever the request includes asking Claude to produce something. Do NOT use it merely to launch the Claude app with no question (that is open_app('Claude')).",
     "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": []}},
    {"type": "function", "name": "premiere_control",
     "description": "Control Premiere Pro. Transport: 'pause'/'play'/'stop'; 'left'/'right' step the playhead by frames (count). Editing: 'cut', 'cut_all_tracks', 'undo', 'redo', 'save', 'mark_in', 'mark_out', 'add_marker', 'ripple_delete', 'delete', 'zoom_in', 'zoom_out'.",
     "parameters": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["pause", "play", "stop", "left", "right", "cut", "cut_all_tracks", "undo", "redo", "save", "mark_in", "mark_out", "add_marker", "ripple_delete", "delete", "zoom_in", "zoom_out"]},
         "count": {"type": "integer", "description": "frames to step for left/right (default 1)"}}, "required": []}},
    {"type": "function", "name": "claude_chat",
     "description": "Send a request to Claude (Anthropic API) and read its reply back aloud. Use for general questions, explanations, brainstorming, writing/rewriting text, or 'ask Claude ...', 'what does Claude think ...', 'have Claude ...'. Keeps conversation context for follow-ups. (This is the general AI assistant; ask_claude instead drives the on-screen Claude Desktop app.)",
     "parameters": {"type": "object", "properties": {
         "prompt": {"type": "string", "description": "what to ask Claude"},
         "reset": {"type": "boolean", "description": "true to start a fresh conversation"}}, "required": ["prompt"]}},
    {"type": "function", "name": "cider_control",
     "description": "Control the Cider music player. action: 'play' (with a query to search+play a song), 'pause', 'playpause'/'toggle', 'stop', 'next'/'skip', 'previous'/'back', 'shuffle', 'repeat', 'volume' (with volume 0..1), or 'now_playing'. Use whenever the user mentions Cider, e.g. 'play <song> in Cider', 'pause Cider', 'next track in Cider'.",
     "parameters": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["play", "pause", "playpause", "toggle", "stop", "next", "skip", "previous", "back", "shuffle", "repeat", "volume", "now_playing"]},
         "query": {"type": "string", "description": "song/artist to search when action is 'play'"},
         "volume": {"type": "number", "description": "0.0–1.0 when action is 'volume'"}}, "required": ["action"]}},
    {"type": "function", "name": "delegate_to_claude",
     "description": "Hand a task to a headless Claude Code agent that runs in the BACKGROUND and actually does the work on this PC — build/edit/run code, research, multi-step jobs. Use for 'ask Claude to build/fix/write/refactor ...', 'have Claude ...', 'tell the agent to ...'. Returns immediately with a job id (work keeps going); confirm you've started and that the user can ask for an update. Don't wait or invent results. (For a quick spoken Q&A with no machine work, use claude_chat instead.)",
     "parameters": {"type": "object", "properties": {"instruction": {"type": "string", "description": "the full task to hand to Claude, in the user's own words"}}, "required": ["instruction"]}},
    {"type": "function", "name": "check_claude",
     "description": "Get an update on a delegated Claude Code job and summarize it back. Use for 'how's it going', 'is it done yet', 'what did Claude do', 'any update', 'check on that'. Leave job_id empty for the most recent job, or pass it when the user names one ('check job two'). Read `result` aloud when done, else summarize `new_updates`.",
     "parameters": {"type": "object", "properties": {"job_id": {"type": "string", "description": "e.g. 'job-2' (optional; default = latest job)"}}, "required": []}},
    {"type": "function", "name": "stop_claude",
     "description": "Cancel a running delegated Claude Code job. Use for 'stop that', 'cancel the agent', 'never mind, stop Claude'. Default = the latest job.",
     "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": []}},
    {"type": "function", "name": "continue_claude",
     "description": "Continue the previous Claude Code job in the SAME session (Claude keeps full memory of what it just did/said). Use to ACT ON A PLAN Claude proposed — after a review_with_claude briefing or any suggestion, when the user says 'do the first thing you suggested', 'go ahead with that', 'okay do it', 'continue', 'make it happen'. Pass the directive as instruction (short is fine — Claude remembers its own plan). Returns a new job id; then check_claude for results. (Use delegate_to_claude instead for a brand-new, unrelated task.)",
     "parameters": {"type": "object", "properties": {
         "instruction": {"type": "string", "description": "what to do next, e.g. 'go ahead and do the first next-step you recommended'"},
         "job_id": {"type": "string", "description": "which job to continue (optional; default = latest)"}}, "required": []}},
    {"type": "function", "name": "github_status",
     "description": "Check GitHub activity and read the `summary` aloud. With NO repo it AUTO-DISCOVERS the user's most recently active repos and summarizes them — use for 'what have I been working on', 'catch me up', 'any recent activity' (nothing hardcoded). With a repo it checks that one — use for 'how's the <project> update going', 'is the PR done', 'status of <owner/repo>', including a CLOUD Claude Code job that pushes to a repo. Pass repo as 'owner/name' only if the user names one; pr for a specific pull request.",
     "parameters": {"type": "object", "properties": {
         "repo": {"type": "string", "description": "owner/name (optional; omit to auto-discover recent repos)"},
         "pr": {"type": "string", "description": "a pull request number to focus (optional)"},
         "branch": {"type": "string", "description": "a branch to read the tip of (optional)"}}, "required": []}},
    {"type": "function", "name": "review_with_claude",
     "description": "Deep review: auto-discover the user's recent GitHub work, then have a background Claude Code agent reason over it and give a spoken briefing plus the best next step per project. Use for 'have Claude review what I've been working on', 'what should I work on next', 'catch me up and tell me what to do next'. Returns a job id — then call check_claude (after a few seconds) to read Claude's summary aloud. Use github_status (no repo) instead for a quick factual overview without the deeper reasoning.",
     "parameters": {"type": "object", "properties": {
         "focus": {"type": "string", "description": "optional: a project/area to focus on"}}, "required": []}},
]


def is_wake(transcript: str) -> bool:
    norm = re.sub(r"[^a-z0-9\s]", " ", (transcript or "").lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    return bool(_WAKE_RE.match(norm))


def session_config() -> dict:
    audio_in = {
        "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
        "transcription": {"model": "whisper-1"},
    }
    if WAKE_MODE:
        audio_in["turn_detection"] = {
            "type": "semantic_vad", "eagerness": "high",
            "create_response": False, "interrupt_response": False,
        }
    elif HOTKEY_MODE:
        audio_in["turn_detection"] = None  # we commit manually on key release
    else:  # PTT
        audio_in["turn_detection"] = {
            "type": "server_vad", "threshold": 0.5,
            "prefix_padding_ms": 300, "silence_duration_ms": 1500,
        }
    return {
        "type": "session.update",
        "session": {
            "type": "realtime", "model": MODEL, "instructions": INSTRUCTIONS,
            "output_modalities": ["audio"],
            "audio": {"input": audio_in,
                      "output": {"format": {"type": "audio/pcm", "rate": SAMPLE_RATE}, "voice": VOICE}},
            "tools": TOOLS, "tool_choice": "auto",
        },
    }


mic_q: "queue.Queue[bytes]" = queue.Queue()
play_q: "queue.Queue[bytes]" = queue.Queue()
key_events: "queue.Queue[str]" = queue.Queue()
_play_buf = bytearray()
_primed = False
_speaking = False
_listening = WAKE_MODE
_in_stream = None
_in_dev = None
_audio_frames = 0  # mic frames forwarded since the current hold started (anti empty-commit)
# Each mic frame is BLOCK samples = 100ms; the Realtime API rejects a commit with
# <100ms buffered, so require a small floor before we commit on key release.
MIN_AUDIO_FRAMES = 2


def _open_mic():
    global _in_stream
    if _in_stream is not None:
        return
    while not mic_q.empty():
        try:
            mic_q.get_nowait()
        except queue.Empty:
            break
    _in_stream = sd.RawInputStream(
        samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
        blocksize=BLOCK, callback=_mic_cb, device=_in_dev,
    )
    _in_stream.start()


def _close_mic():
    global _in_stream
    if _in_stream is not None:
        try:
            _in_stream.stop()
            _in_stream.close()
        except Exception:
            pass
        _in_stream = None


def _log(msg: str):
    try:
        with open(EVENT_LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def _write_hud(active: bool, level: float):
    try:
        with open(HUD_FILE, "w", encoding="utf-8") as f:
            json.dump({"active": bool(active), "level": float(level)}, f)
    except OSError:
        pass


def _frame_level(data: bytes) -> float:
    a = array.array("h")
    a.frombytes(data)
    if not a:
        return 0.0
    rms = math.sqrt(sum(x * x for x in a) / len(a))
    return min(1.0, rms / 8000.0)


def _mic_cb(indata, frames, t, status):
    mic_q.put(bytes(indata))


def _spk_cb(outdata, frames, t, status):
    global _primed
    need = len(outdata)
    while True:
        try:
            _play_buf.extend(play_q.get_nowait())
        except queue.Empty:
            break
    if not _primed:
        if len(_play_buf) >= PRIME_BYTES:
            _primed = True
        else:
            outdata[:] = b"\x00" * need
            return
    if len(_play_buf) >= need:
        outdata[:] = bytes(_play_buf[:need])
        del _play_buf[:need]
    else:
        n = len(_play_buf)
        outdata[:n] = bytes(_play_buf)
        outdata[n:] = b"\x00" * (need - n)
        del _play_buf[:]
        _primed = False


async def dispatch_tool(name: str, args: dict) -> dict:
    fn = actions.TOOLS.get(name)
    if not fn:
        return {"status": "error", "error": f"unknown tool {name}"}
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, lambda: fn(**args))
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def mic_pump(ws):
    global _audio_frames
    loop = asyncio.get_event_loop()
    while True:
        data = await loop.run_in_executor(None, mic_q.get)
        active = _listening and not _speaking and not _play_buf
        _write_hud(active, _frame_level(data) if active else 0.0)
        if not _listening or _speaking or _play_buf:
            continue
        await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                  "audio": base64.b64encode(data).decode()}))
        _audio_frames += 1  # count what we actually sent, so a too-short hold won't commit empty


async def ptt_console(ws):
    global _listening
    loop = asyncio.get_event_loop()
    while True:
        await loop.run_in_executor(None, sys.stdin.readline)
        if _speaking or _play_buf:
            continue
        await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
        _listening = True
        print("🎙  listening… (speak, then pause)", flush=True)


def _start_hotkey_listener():
    """Start the global hold-to-talk hook (keyboard lib). Feeds key_events."""
    resolved = hotkey.start_hotkey_listener(key_events, HOTKEY_NAME)
    if resolved:
        print(f"   global key: hold [{resolved}] to talk", flush=True)


async def hotkey_console(ws):
    """Hold the global key to talk; release to send. Pressing while the model is
    talking barges in."""
    global _listening, _speaking, _t_release, _audio_frames
    loop = asyncio.get_event_loop()
    while True:
        ev = await loop.run_in_executor(None, key_events.get)
        if ev == "down":
            # BARGE-IN: only CANCEL when a response is actually active (guarding on
            # _speaking avoids the 'response_cancel_not_active' error); always stop
            # any audio that's still playing/draining.
            if _speaking:
                await ws.send(json.dumps({"type": "response.cancel"}))
                print("⏹  interrupted", flush=True)
            if _speaking or _play_buf:
                _play_buf.clear()
                while not play_q.empty():
                    try:
                        play_q.get_nowait()
                    except queue.Empty:
                        break
                _speaking = False
            t0 = time.monotonic()
            _open_mic()
            await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
            _audio_frames = 0  # fresh hold: count audio from here
            _listening = True
            print(f"🎙  listening… (mic open {(time.monotonic()-t0)*1000:.0f}ms)", flush=True)
        elif ev == "up":
            if not _listening:
                continue
            _close_mic()
            await asyncio.sleep(0.15)  # let the tail of speech flush while still listening
            _listening = False
            _write_hud(False, 0.0)
            # Too short / no speech captured -> don't commit an empty buffer (avoids
            # 'input_audio_buffer_commit_empty'); just drop it and wait for the next hold.
            if _audio_frames < MIN_AUDIO_FRAMES:
                await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                print("·  (too short — hold the key while you speak)", flush=True)
                continue
            _t_release = time.monotonic()
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            await ws.send(json.dumps({"type": "response.create"}))
            print("⏳ thinking…", flush=True)


async def receive(ws):
    global _speaking, _listening
    async for raw in ws:
        ev = json.loads(raw)
        t = ev.get("type", "")
        if t not in ("response.output_audio.delta", "response.output_audio_transcript.delta"):
            _log(t)

        if t == "response.created":
            _speaking = True
            if PTT:
                _listening = False
        elif t == "response.output_audio.delta":
            _speaking = True
            play_q.put(base64.b64decode(ev["delta"]))
        elif t == "response.output_audio_transcript.delta":
            print(ev.get("delta", ""), end="", flush=True)
        elif t == "response.output_audio_transcript.done":
            print()
        elif t in ("response.done", "response.output_audio.done"):
            _speaking = False
            if PTT and t == "response.done":
                print("\n— press ENTER to talk —", flush=True)
        elif t == "conversation.item.input_audio_transcription.completed":
            heard = (ev.get("transcript") or "").strip()
            if WAKE_MODE:
                if is_wake(heard):
                    print(f"\n🗣  HEARD (wake ✓): {heard!r}", flush=True)
                    _log(f"WAKE {heard!r}")
                    await ws.send(json.dumps({"type": "response.create"}))
                else:
                    print(f"\n·  ignored (no wake word): {heard!r}", flush=True)
                    _log(f"IGNORED {heard!r}")
            else:
                print(f"\n🗣  HEARD: {heard!r}", flush=True)
                _log(f"HEARD {heard!r}")
        elif t == "input_audio_buffer.speech_started":
            while not play_q.empty():
                try:
                    play_q.get_nowait()
                except queue.Empty:
                    break
            _play_buf.clear()
        elif t == "response.function_call_arguments.done":
            name = ev["name"]
            call_id = ev["call_id"]
            try:
                args = json.loads(ev.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            lat = (time.monotonic() - _t_release) if _t_release else 0.0
            arg_str = json.dumps(args, ensure_ascii=False)
            if len(arg_str) > 70:
                arg_str = arg_str[:67] + "…}"
            print(f"\n⚙  {name}({arg_str})", flush=True)
            _log(f"TOOL {name}({args}) latency={lat:.2f}s")
            result = await dispatch_tool(name, args)
            status = result.get("status", "?")
            print(f"✓  {status}" if status == "ok" else f"✗  {status}", flush=True)
            await ws.send(json.dumps({"type": "conversation.item.create",
                                      "item": {"type": "function_call_output",
                                               "call_id": call_id,
                                               "output": json.dumps(result)}}))
            await ws.send(json.dumps({"type": "response.create"}))
        elif t == "error":
            print("\n[realtime error]", json.dumps(ev.get("error", ev)), flush=True)
            _log("ERROR " + json.dumps(ev.get("error", ev)))


def resolve_input_device():
    if MIC_NAME:
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and MIC_NAME.lower() in d["name"].lower():
                return i, d["name"]
        print(f"⚠  no input device matches {MIC_NAME!r}; using system default.")
    try:
        return None, sd.query_devices(sd.default.device[0])["name"]
    except Exception:
        return None, "default input"


async def main():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("OPENAI_API_KEY not set. Put it in .env or export it first.")
    headers = {"Authorization": f"Bearer {key}"}
    in_dev, mic_name = resolve_input_device()
    print("=" * 60)
    if WAKE_MODE:
        print(f"  🎙  VOICE OS (Windows) — WAKE WORD: say “{WAKE_WORD}, …”")
        print("  e.g. “hey chat, open Spotify”. Ctrl-C to quit.")
    elif HOTKEY_MODE:
        print("  🎙  VOICE OS (Windows) — HOLD-TO-TALK")
        print("  hold the key, speak, release to send. Ctrl-C to quit.")
    else:
        print("  🎙  VOICE OS (Windows) — PUSH-TO-TALK (press ENTER to talk)")
    print(f"  mic: {mic_name}   ·   brain: {MODEL}   ·   log: {EVENT_LOG}")
    print("=" * 60, flush=True)
    _log(f"--- start ({'WAKE' if WAKE_MODE else 'HOTKEY' if HOTKEY_MODE else 'PTT'}) ---")
    if HOTKEY_MODE:
        _start_hotkey_listener()

    global _speaking, _listening, _primed, _in_dev
    _in_dev = in_dev
    out_stream = sd.RawOutputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                                    dtype="int16", blocksize=OUT_BLOCK, callback=_spk_cb)
    out_stream.start()
    if not HOTKEY_MODE:
        _open_mic()
    try:
        while True:
            try:
                async with websockets.connect(URL, additional_headers=headers, max_size=None) as ws:
                    await ws.send(json.dumps(session_config()))
                    tasks = [asyncio.ensure_future(mic_pump(ws)),
                             asyncio.ensure_future(receive(ws))]
                    if PTT:
                        print("\n— press ENTER to talk —", flush=True)
                        tasks.append(asyncio.ensure_future(ptt_console(ws)))
                    elif HOTKEY_MODE:
                        print(f"\n— hold [{hotkey.resolve_key(HOTKEY_NAME)}] to talk —", flush=True)
                        tasks.append(asyncio.ensure_future(hotkey_console(ws)))
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for tk in tasks:
                        tk.cancel()
            except (websockets.ConnectionClosed, OSError) as e:
                _log(f"conn err {getattr(e, 'code', '')}")
            _speaking = False
            _listening = WAKE_MODE
            _primed = False
            _play_buf.clear()
            while not mic_q.empty():
                try:
                    mic_q.get_nowait()
                except queue.Empty:
                    break
            print("\n↻ reconnecting…", flush=True)
            _log("RECONNECT")
            await asyncio.sleep(0.5)
    finally:
        _close_mic()
        try:
            out_stream.stop()
            out_stream.close()
        except Exception:
            pass


def _detect_key():
    """Print the name + scan code of every key you press, so you can confirm what
    your mouse button is bound to. Hold the button, read the value, then run with
    --hotkey <name-or-scancode>. Ctrl-C to quit."""
    try:
        import keyboard
    except Exception:
        sys.exit("needs the `keyboard` package: pip install keyboard")
    print("Press/hold the key (or the mouse button bound to it). Ctrl-C to quit.\n"
          "Use the printed name (preferred) or scan_code as --hotkey <value>.\n")
    seen = set()

    def show(e):
        if e.event_type == "down" and (e.name, e.scan_code) not in seen:
            seen.add((e.name, e.scan_code))
            print(f"  name={e.name!r}   scan_code={e.scan_code}")

    keyboard.hook(show)
    keyboard.wait()


if __name__ == "__main__":
    if "--detect-key" in sys.argv:
        try:
            _detect_key()
        except KeyboardInterrupt:
            print("\nbye.")
        sys.exit(0)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye.")
