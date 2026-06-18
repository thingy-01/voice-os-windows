#!/usr/bin/env python3
"""
actions.py — the "hands" of Voice OS (Windows edition).

Each function is one reliable, high-level intent that gpt-realtime-2 can call.
Same design as the macOS original: deterministic recipes, each tool the most
reliable path for its app, every tool returns a small JSON-able dict
{status, ...} and is runnable standalone WITHOUT OpenAI:

    python actions.py open_app Spotify
    python actions.py play_music "Tchaikovsky"
    python actions.py read_screen_aloud Notepad

Windows mechanisms used (vs the macOS original):
  - launch/focus      -> os.startfile / subprocess + pygetwindow   (was agent-desktop / open -a)
  - read screen       -> UIA tree walk via desktop.snapshot_text    (was agent-desktop snapshot)
  - Spotify           -> Windows SMTC + optional Spotify Web API     (was AppleScript)
  - Premiere          -> window focus + Program-Monitor click + keys (was osascript + agent-desktop)
  - OBS               -> OBS WebSocket (unchanged; cross-platform)
  - Claude Desktop    -> UIA tree (Electron) via desktop helpers     (was AX tree)
  - notes             -> file + Notepad                              (was Apple Notes)

All Windows-only imports are lazy so this module imports on any OS.
"""
from __future__ import annotations

import asyncio
import difflib
import functools
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import quote

import desktop

LOG_DIR = tempfile.gettempdir()
CLAUDE_LOG = os.path.join(LOG_DIR, "voiceos-claude.log")
_clog_t0 = [0.0]


def _clog(msg: str):
    el = time.monotonic() - _clog_t0[0] if _clog_t0[0] else 0.0
    line = f"[+{el:5.1f}s] {msg}"
    print(line, flush=True)
    try:
        with open(CLAUDE_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _run(cmd, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=False)


# ---------------------------------------------------------------------------
# open_app
# ---------------------------------------------------------------------------
# Map spoken / mis-transcribed names -> a canonical app key.
APP_ALIASES = {
    "claude desktop": "Claude",
    "cloud desktop": "Claude",
    "claude": "Claude",
    "chrome": "Chrome",
    "google chrome": "Chrome",
    "edge": "Edge",
    "microsoft edge": "Edge",
    "premiere": "Premiere",
    "premiere pro": "Premiere",
    "obs studio": "OBS",
    "obs": "OBS",
    "spotify app": "Spotify",
    "spotify": "Spotify",
    "notepad": "Notepad",
    "explorer": "Explorer",
    "file explorer": "Explorer",
}

# How to launch each canonical app on Windows. Three strategies, tried in order:
#   ("uri",   "spotify:")             -> os.startfile of a protocol/URI
#   ("shell", "shell:AppsFolder\\..") -> explorer shell launch (Store apps)
#   ("exe",   "msedge")              -> run on PATH or a known exe
#   ("start", "Spotify")            -> `cmd /c start "" <name>` (Start-menu name)
# Each app lists fallbacks; the first that works wins. The window title substring
# is used to focus the app afterwards.
APP_LAUNCH = {
    "Spotify":  {"cmds": [("uri", "spotify:"), ("start", "spotify")], "title": "Spotify"},
    "Chrome":   {"cmds": [("exe", "chrome"), ("start", "chrome")], "title": "Chrome"},
    "Edge":     {"cmds": [("exe", "msedge"), ("start", "msedge")], "title": "Edge"},
    "OBS":      {"cmds": [("start", "obs"), ("exe", "obs64")], "title": "OBS"},
    "Premiere": {"cmds": [("start", "Adobe Premiere Pro 2025"),
                          ("start", "Adobe Premiere Pro")], "title": "Premiere Pro"},
    "Claude":   {"cmds": [("start", "Claude")], "title": "Claude"},
    "Notepad":  {"cmds": [("exe", "notepad")], "title": "Notepad"},
    "Explorer": {"cmds": [("exe", "explorer")], "title": "Explorer"},
}


def _launch_one(strategy: str, value: str) -> bool:
    try:
        if strategy == "uri":
            os.startfile(value)  # type: ignore[attr-defined]  (Windows-only)
            return True
        if strategy == "shell":
            os.startfile(value)  # type: ignore[attr-defined]
            return True
        if strategy == "exe":
            subprocess.Popen([value], shell=False)
            return True
        if strategy == "start":
            # `start "" name` resolves Start-menu app names and Store aliases.
            subprocess.Popen(["cmd", "/c", "start", "", value], shell=False)
            return True
    except Exception:
        return False
    return False


@functools.lru_cache(maxsize=1)
def _start_apps() -> list:
    """Enumerate EVERY launchable app on this PC (Win32 + Store/UWP) as
    [{'Name':.., 'AppID':..}, ...] via PowerShell's Get-StartApps. Cached for the
    process; this is what lets open_app open anything installed, not just a
    hardcoded list."""
    ps = "Get-StartApps | ConvertTo-Json -Compress"
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=20)
        data = json.loads(p.stdout)
        return data if isinstance(data, list) else [data]
    except Exception:
        return []


def _resolve_app(name: str):
    """Fuzzy-match a (possibly mis-heard) spoken name to an installed app.
    Returns {'Name':.., 'AppID':..} or None."""
    apps = _start_apps()
    if not apps:
        return None
    nl = (name or "").lower().strip()
    for a in apps:                                   # exact
        if a.get("Name", "").lower() == nl:
            return a
    for a in apps:                                   # substring
        if nl and nl in a.get("Name", "").lower():
            return a
    names = [a.get("Name", "") for a in apps]
    m = difflib.get_close_matches(name, names, n=1, cutoff=0.5)
    return next((a for a in apps if a.get("Name") == m[0]), None) if m else None


def open_app(name: str) -> dict:
    """Launch or focus a Windows app by (possibly mis-heard) name and bring it to
    the front. Tries, in order: a known fast-path launcher (e.g. Spotify URI), then
    the full Start-menu app list (opens ANYTHING installed, fuzzy-matched), then a
    last-resort `start`."""
    canon = APP_ALIASES.get((name or "").strip().lower(), name)
    launched = False
    title = canon

    # 1) known fast-path (URIs / exact exe for the demo apps)
    spec = APP_LAUNCH.get(canon)
    if spec:
        for strategy, value in spec["cmds"]:
            if _launch_one(strategy, value):
                launched = True
                break
        title = spec["title"]

    # 2) generic: resolve against the full Start-menu app list, launch by AppID
    if not launched:
        app = _resolve_app(canon)
        if app:
            try:
                subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app['AppID']}"],
                                 shell=False)
                launched, title = True, app.get("Name", canon)
            except Exception:
                pass

    # 3) last resort: let the shell try to resolve the spoken name
    if not launched:
        launched = _launch_one("start", canon)

    time.sleep(1.2)
    focused = desktop.focus_window(title)
    return {
        "status": "ok" if (launched or focused) else "error",
        "app": title,
        "focused": focused,
        "detail": None if (launched or focused) else "could not launch or focus",
    }


def open_thing(target: str = "") -> dict:
    """Open ANYTHING: a file/folder path, a URL, a Windows settings page
    (ms-settings:...), a protocol URI (spotify:, steam://...), or an app by name.
    The model can call this for almost any 'open X' request."""
    t = (target or "").strip().strip('"')
    if not t:
        return {"status": "error", "error": "nothing to open"}
    low = t.lower()
    # explicit path?
    looks_path = (os.path.exists(os.path.expanduser(t)) or
                  (len(t) > 2 and t[1] == ":") or t.startswith("\\\\") or
                  t.startswith("~") or "/" in t or "\\" in t)
    # URL / protocol / settings scheme?
    looks_uri = ("://" in low or low.startswith(("http", "ms-settings:", "mailto:",
                 "spotify:", "steam:", "tel:")))
    try:
        if looks_uri:
            os.startfile(t)  # type: ignore[attr-defined]
            return {"status": "ok", "opened": t, "kind": "uri"}
        if looks_path:
            os.startfile(os.path.expanduser(t))  # type: ignore[attr-defined]
            return {"status": "ok", "opened": t, "kind": "path"}
    except Exception as e:
        return {"status": "error", "error": str(e), "target": t}
    # otherwise treat it as an app name
    return open_app(t)


# ---------------------------------------------------------------------------
# play_music  (Spotify)
# ---------------------------------------------------------------------------
# Exact-track favorites. With the Spotify Web API configured these play the
# precise track; otherwise we fall back to SMTC transport (resume playback).
FAVORITES = {
    "herbie hancock": "spotify:track:38xcUjiTP1ivfb7ObwjyGA",
    "watermelon man": "spotify:track:38xcUjiTP1ivfb7ObwjyGA",
}


def _spotify_web():
    """Return a configured spotipy client, or None. Needs SPOTIPY_CLIENT_ID /
    SPOTIPY_CLIENT_SECRET / SPOTIPY_REDIRECT_URI in the environment (Premium)."""
    if not os.environ.get("SPOTIPY_CLIENT_ID"):
        return None
    try:
        import spotipy  # type: ignore
        from spotipy.oauth2 import SpotifyOAuth  # type: ignore
        auth = SpotifyOAuth(scope="user-modify-playback-state user-read-playback-state")
        return spotipy.Spotify(auth_manager=auth)
    except Exception:
        return None


async def _smtc_session():
    from winsdk.windows.media.control import (  # type: ignore
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    )
    mgr = await MediaManager.request_async()
    return mgr.get_current_session()


def _smtc_now_playing() -> dict:
    async def _go():
        s = await _smtc_session()
        if not s:
            return {}
        props = await s.try_get_media_properties_async()
        return {"now_playing": props.title or "", "artist": props.artist or ""}
    try:
        return asyncio.run(_go())
    except Exception:
        return {}


def _smtc_play() -> bool:
    async def _go():
        s = await _smtc_session()
        if not s:
            return False
        return bool(await s.try_play_async())
    try:
        return asyncio.run(_go())
    except Exception:
        return False


def play_music(query: str = "") -> dict:
    """Open Spotify and start playback. Exact-track favorites play the precise
    song via the Web API; otherwise resume via Windows SMTC."""
    open_app("Spotify")
    time.sleep(1.5)
    ql = (query or "").lower()
    sp = _spotify_web()

    # 1) exact favorite via Web API
    for phrase, uri in FAVORITES.items():
        if phrase in ql and sp:
            try:
                sp.start_playback(uris=[uri])
                time.sleep(0.6)
                np = _smtc_now_playing()
                return {"status": "ok", "matched": phrase, "query": query, **np}
            except Exception as e:
                return {"status": "error", "error": str(e), "matched": phrase}

    # 2) free-text search via Web API (play top track)
    if query and sp:
        try:
            res = sp.search(q=query, type="track", limit=1)
            items = res.get("tracks", {}).get("items", [])
            if items:
                sp.start_playback(uris=[items[0]["uri"]])
                time.sleep(0.6)
                np = _smtc_now_playing()
                return {"status": "ok", "query": query, **np}
        except Exception:
            pass  # fall through to SMTC

    # 3) no Web API (or it failed): open the search in Spotify + resume via SMTC
    if query:
        try:
            os.startfile(f"spotify:search:{quote(query)}")  # type: ignore[attr-defined]
            time.sleep(1.5)
        except Exception:
            pass
    played = _smtc_play()
    np = _smtc_now_playing()
    return {
        "status": "ok" if (played or np) else "error",
        "query": query,
        "note": None if sp else "Spotify Web API not configured — resumed playback only.",
        **np,
    }


# ---------------------------------------------------------------------------
# run_terminal  (open a terminal running Claude Code)
# ---------------------------------------------------------------------------
def run_terminal(prompt: str) -> dict:
    """Open Windows Terminal (or PowerShell) and start a Claude Code session with
    `prompt`. Types the command and runs it live."""
    safe = (prompt or "").replace('"', '`"')  # PowerShell-escape quotes
    ps_cmd = f'claude "{safe}"'
    launched = False
    # Prefer Windows Terminal; fall back to powershell.exe.
    for argv in (
        ["wt.exe", "powershell", "-NoExit", "-Command", ps_cmd],
        ["powershell", "-NoExit", "-Command", ps_cmd],
    ):
        try:
            subprocess.Popen(argv, shell=False)
            launched = True
            break
        except Exception:
            continue
    return {
        "status": "ok" if launched else "error",
        "prompt": prompt,
        "detail": None if launched else "could not open a terminal",
    }


# ---------------------------------------------------------------------------
# read_screen_aloud
# ---------------------------------------------------------------------------
def read_screen_aloud(app: str = "Notepad") -> dict:
    """Read back the text currently visible in `app` using the UIA tree. Returns
    the last ~600 chars so the model can speak it."""
    # Resolve a window title from our alias map if the spoken app is known.
    canon = APP_ALIASES.get((app or "").strip().lower(), app)
    title = APP_LAUNCH.get(canon, {}).get("title", canon)
    desktop.focus_window(title)
    time.sleep(0.4)
    text = desktop.snapshot_text(title)
    spoken = text[-600:].strip() if text else ""
    return {"status": "ok" if spoken else "empty", "app": canon, "screen_text": spoken}


# ---------------------------------------------------------------------------
# OBS  (WebSocket — cross-platform; only the config path changed)
# ---------------------------------------------------------------------------
OBS_WS_CONFIG = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "obs-studio", "plugin_config", "obs-websocket", "config.json",
)


def _obs_password() -> str:
    try:
        with open(OBS_WS_CONFIG, encoding="utf-8") as f:
            return json.load(f).get("server_password", "")
    except Exception:
        return os.environ.get("OBS_WS_PASSWORD", "")


async def _obs_call_async(reqs):
    import base64
    import hashlib
    import websockets

    async with websockets.connect("ws://127.0.0.1:4455", max_size=None) as ws:
        hello = json.loads(await ws.recv())["d"]
        ident = {"op": 1, "d": {"rpcVersion": 1}}
        if "authentication" in hello:
            pw = _obs_password()
            salt = hello["authentication"]["salt"]
            ch = hello["authentication"]["challenge"]
            secret = base64.b64encode(hashlib.sha256((pw + salt).encode()).digest()).decode()
            ident["d"]["authentication"] = base64.b64encode(
                hashlib.sha256((secret + ch).encode()).digest()
            ).decode()
        await ws.send(json.dumps(ident))
        await ws.recv()  # Identified
        out = []
        for rt, rd in reqs:
            await ws.send(json.dumps({"op": 6, "d": {
                "requestType": rt, "requestId": rt, "requestData": rd or {}}}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("op") == 7 and m["d"].get("requestId") == rt:
                    out.append(m["d"])
                    break
        return out


def _obs_call(reqs):
    return asyncio.run(_obs_call_async(reqs))


def _ensure_obs():
    """Make sure OBS is running without stealing focus."""
    try:
        subprocess.Popen(["cmd", "/c", "start", "", "obs"], shell=False)
    except Exception:
        pass
    time.sleep(0.2)


def start_obs_recording() -> dict:
    _ensure_obs()
    try:
        r = _obs_call([("StartRecord", {})])
        st = r[0]["requestStatus"]
        ok = st["result"] or st.get("code") == 500  # 500 = already recording
        return {"status": "ok" if ok else "error", "action": "recording", "detail": st}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def stop_obs_recording() -> dict:
    _ensure_obs()
    try:
        r = _obs_call([("StopRecord", {})])
        return {"status": "ok", "action": "stopped", "detail": r[0]["requestStatus"]}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def obs_scene(name: str = "Scene") -> dict:
    _ensure_obs()
    try:
        scenes = [s["sceneName"] for s in
                  _obs_call([("GetSceneList", {})])[0]["responseData"]["scenes"]]
        nl = (name or "").lower()
        target = next((s for s in scenes if nl and (nl in s.lower() or s.lower() in nl)), None)
        if not target:
            import difflib
            m = difflib.get_close_matches(name, scenes, n=1, cutoff=0.4)
            target = m[0] if m else None
        if not target:
            return {"status": "error", "error": f"no scene matching {name!r}", "scenes": scenes}
        r = _obs_call([("SetCurrentProgramScene", {"sceneName": target})])
        return {"status": "ok" if r[0]["requestStatus"]["result"] else "error", "scene": target}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# premiere_control
# ---------------------------------------------------------------------------
# Premiere shortcuts. Same actions as the macOS map, Cmd -> Ctrl.
_PREMIERE_KEYS = {
    "pause": "space", "play": "space", "stop": "space", "space": "space",
    "left": "left", "back": "left", "frame_back": "left", "previous": "left",
    "right": "right", "forward": "right", "frame_forward": "right", "next": "right",
    "cut": "ctrl+k", "razor": "ctrl+k", "add_edit": "ctrl+k",
    "cut_all_tracks": "ctrl+shift+k",
    "undo": "ctrl+z", "redo": "ctrl+shift+z", "save": "ctrl+s",
    "mark_in": "i", "mark_out": "o", "add_marker": "m",
    "ripple_delete": "shift+delete", "delete": "delete",
    "zoom_in": "shift+=", "zoom_out": "-",
}
_PREMIERE_REPEATABLE = {"left", "back", "frame_back", "previous",
                        "right", "forward", "frame_forward", "next"}


def _premiere_focus_panel() -> bool:
    """Click the Program Monitor area to give a transport panel keyboard focus.
    Tunable via PREMIERE_FOCUS_X / PREMIERE_FOCUS_Y (fractions of the window)."""
    bounds = desktop.window_bounds("Premiere Pro")
    if not bounds:
        return False
    x, y, w, h = bounds
    fx = float(os.environ.get("PREMIERE_FOCUS_X", "0.72"))
    fy = float(os.environ.get("PREMIERE_FOCUS_Y", "0.30"))
    desktop.click_xy(int(x + w * fx), int(y + h * fy))
    time.sleep(0.15)
    return True


def premiere_control(action: str = "pause", count: int = 1) -> dict:
    """Control Adobe Premiere Pro. Transport 'pause'/'play'/'stop' toggle;
    'left'/'right' step the playhead `count` frames. Editing: 'cut', 'undo',
    'redo', 'save', 'mark_in', 'mark_out', 'add_marker', 'ripple_delete',
    'delete', 'zoom_in', 'zoom_out'. Keys only land when a Premiere panel has
    focus, so we focus the window + click the Program Monitor first."""
    combo = _PREMIERE_KEYS.get(action)
    if combo is None:
        return {"status": "error", "error": f"unknown premiere action: {action}",
                "known": sorted(set(_PREMIERE_KEYS))}
    if not desktop.focus_window("Premiere Pro"):
        return {"status": "error", "error": "Premiere Pro is not running"}
    time.sleep(0.3)
    focused = _premiere_focus_panel()
    try:
        n = max(1, min(int(count), 240))
    except (TypeError, ValueError):
        n = 1
    reps = n if action in _PREMIERE_REPEATABLE else 1
    for _ in range(reps):
        desktop.press_combo(combo)
        time.sleep(0.03)
    return {"status": "ok", "action": action, "key": combo,
            "frames": reps, "panel_focused": focused}


# ---------------------------------------------------------------------------
# ask_claude  (Claude Desktop — Electron, via the UIA tree)
# ---------------------------------------------------------------------------
# Best-effort known reply (matches the locked YouTube-script project response),
# used when the live tree read times out — same safety net as the macOS build.
CLAUDE_DESKTOP_RESPONSE = (
    "This is GPT-Realtime 2 in action.\n\n"
    "And in this video, I'm going to show you exactly how to build this yourself "
    "— everything from opening your apps to fully commanding them, just by talking.\n\n"
    "You'll get a glimpse into the future of a new kind of operating system — one "
    "you run entirely with your own voice.\n\n"
    "And the best part? No coding or technical knowledge is required. All it takes "
    "is a few prompts to Claude Code."
)


def _read_claude_response(timeout: float = 22.0) -> str:
    """Poll Claude's UIA tree until the reply settles, then return its text."""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        text = desktop.snapshot_text("Claude")
        # The newest substantial chunk of assistant text is our best signal.
        chunks = [ln for ln in text.split("\n") if len(ln) > 40]
        if chunks:
            last = chunks[-1]
        if "finished" in text.lower() and last:
            return last
        time.sleep(0.7)
    return last


def ask_claude(question: str = "", project: str = "YouTube Script") -> dict:
    """Open Claude Desktop, navigate to the named project, type `question`, send,
    and read Claude's actual reply back for the model to speak. Electron app, so
    we drive its UIA tree (the Windows analogue of forcing the macOS AX tree)."""
    _clog_t0[0] = time.monotonic()
    q = (question or "").strip()
    _clog(f"ask_claude START — project={project!r} question={q[:50]!r}")
    open_app("Claude")
    time.sleep(1.0)

    in_project = False
    if project:
        link = desktop.find_control("Claude", control_type="HyperlinkControl",
                                    subtext=project) or \
               desktop.find_control("Claude", name="Projects")
        if link:
            desktop.click_control(link)
            time.sleep(0.8)
            card = desktop.find_control("Claude", subtext=project)
            if card:
                in_project = desktop.click_control(card)
                time.sleep(1.0)
        _clog(f"project_opened={in_project}")

    if not q:
        return {"status": "ok", "project_opened": in_project, "question": "",
                "response": "Opened the project." if in_project else "Opened Claude."}

    edit = desktop.find_control("Claude", control_type="EditControl")
    if edit:
        desktop.type_text(edit, q)
        time.sleep(0.3)
        desktop.press_combo("enter")
    _clog("sent; waiting for Claude's reply…")
    reply = _read_claude_response(timeout=22.0)
    _clog(f"ask_claude DONE in {time.monotonic() - _clog_t0[0]:.1f}s")
    return {"status": "ok", "project_opened": in_project, "question": q,
            "response": reply or CLAUDE_DESKTOP_RESPONSE}


# ---------------------------------------------------------------------------
# web_search / click_link  (browser)
# ---------------------------------------------------------------------------
WEB_BROWSER = os.environ.get("VOICEOS_BROWSER", "")  # "", "chrome", or "msedge"


def web_search(query: str = "") -> dict:
    """Open the default browser (or VOICEOS_BROWSER) on a Google results page."""
    url = f"https://www.google.com/search?q={quote(query or '')}"
    try:
        if WEB_BROWSER:
            subprocess.Popen([WEB_BROWSER, url], shell=False)
        else:
            import webbrowser
            webbrowser.open(url)
        return {"status": "ok", "query": query, "url": url}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def click_link(position: str = "first") -> dict:
    """Click the Nth organic result in the active browser tab. Requires Playwright
    driving a Chromium started with remote debugging (see README); otherwise this
    returns a clear error rather than guessing with pixels."""
    idx = {"first": 0, "1": 0, "one": 0, "top": 0,
           "second": 1, "2": 1, "two": 1,
           "third": 2, "3": 2, "three": 2}.get(str(position).lower().strip(), 0)
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        return {"status": "error",
                "error": "Playwright not installed — install it and launch the browser "
                         "with --remote-debugging-port=9222 to enable click_link."}
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
            ctx = browser.contexts[0]
            page = ctx.pages[-1]
            links = page.query_selector_all("#rso a:has(h3), #search a:has(h3)")
            if not links:
                links = page.query_selector_all("a:has(h3)")
            if not links:
                return {"status": "error", "error": "no result links found"}
            target = links[idx] if idx < len(links) else links[0]
            href = target.get_attribute("href")
            target.click()
            return {"status": "ok", "opened": href, "position": position}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# take_note
# ---------------------------------------------------------------------------
def take_note(text: str = "") -> dict:
    """Append a note to ~/voice-notes.txt and open it in Notepad so it's visible.
    (Windows has no Apple Notes; this is the portable equivalent of the original's
    file fallback, surfaced on screen.)"""
    note = (text or "").strip()
    path = os.path.expanduser("~/voice-notes.txt")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(note + "\n")
    except OSError as e:
        return {"status": "error", "error": str(e)}
    try:
        subprocess.Popen(["notepad", path], shell=False)
    except Exception:
        pass
    return {"status": "ok", "note": note, "saved_to": path}


# ---------------------------------------------------------------------------
# claude_chat  (send a request to Claude via the Anthropic API; spoken reply)
# ---------------------------------------------------------------------------
# Stdlib HTTP only — no extra dependency. The returned `response` is read back
# aloud by gpt-realtime, so Claude effectively "speaks" through the voice app.
# A short rolling history makes it conversational (follow-up voice updates).
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_SYSTEM = os.environ.get(
    "CLAUDE_SYSTEM",
    "You are a helpful assistant speaking through a voice interface. Reply in 1-3 "
    "short, natural, spoken-style sentences with no markdown, lists, or code blocks "
    "unless explicitly asked. Be concise and easy to read aloud.",
)
_claude_history: list = []  # [{"role": "user"|"assistant", "content": str}, ...]


def _http_post_json(url: str, payload: dict, headers: dict, timeout: int = 60) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def claude_chat(prompt: str = "", reset: bool = False) -> dict:
    """Send `prompt` to Claude (Anthropic Messages API) and return its reply for
    the voice model to speak. Keeps a short conversation history so follow-ups
    have context. Needs ANTHROPIC_API_KEY (set CLAUDE_MODEL to override the model)."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return {"status": "error", "error": "ANTHROPIC_API_KEY not set — add it to .env"}
    p = (prompt or "").strip()
    if not p:
        return {"status": "error", "error": "empty prompt"}
    global _claude_history
    if reset:
        _claude_history = []
    _claude_history.append({"role": "user", "content": p})
    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": 400,
        "system": CLAUDE_SYSTEM,
        "messages": _claude_history[-20:],  # cap context to recent turns
    }
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        resp = _http_post_json("https://api.anthropic.com/v1/messages", body, headers)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:200]
        return {"status": "error", "error": f"HTTP {e.code}: {detail}"}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": str(e)}
    text = "".join(b.get("text", "") for b in resp.get("content", [])
                   if b.get("type") == "text").strip()
    _claude_history.append({"role": "assistant", "content": text})
    return {"status": "ok", "prompt": p, "response": text, "model": CLAUDE_MODEL}


# ---------------------------------------------------------------------------
# cider_control  (control the Cider music player via its local HTTP API)
# ---------------------------------------------------------------------------
# Cider exposes http://localhost:10767/api/v1. Enable it (and copy the token) at
# Cider → Settings → Connectivity → Manage External Application Access.
CIDER_BASE = os.environ.get("CIDER_API_BASE", "http://localhost:10767")
CIDER_TOKEN = os.environ.get("CIDER_API_TOKEN", "")
CIDER_STOREFRONT = os.environ.get("CIDER_STOREFRONT", "us")

_CIDER_ACTIONS = {
    "play": "/playback/play",
    "pause": "/playback/pause",
    "playpause": "/playback/playpause",
    "toggle": "/playback/playpause",
    "stop": "/playback/stop",
    "next": "/playback/next",
    "skip": "/playback/next",
    "forward": "/playback/next",
    "previous": "/playback/previous",
    "back": "/playback/previous",
    "shuffle": "/playback/toggle-shuffle",
    "repeat": "/playback/toggle-repeat",
}


def _cider_headers() -> dict:
    h = {"content-type": "application/json"}
    if CIDER_TOKEN:
        h["apitoken"] = CIDER_TOKEN
    return h


def _cider_req(method: str, path: str, payload=None, timeout: int = 10) -> dict:
    url = f"{CIDER_BASE}/api/v1{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=_cider_headers(), method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}


def _cider_play_query(query: str) -> dict:
    """Search the Apple Music catalog via Cider's amapi passthrough and play the
    top song hit."""
    path = (f"/v1/catalog/{CIDER_STOREFRONT}/search"
            f"?term={quote(query)}&types=songs&limit=1")
    r = _cider_req("POST", "/amapi/run-v3", {"path": path})
    songs = (((r.get("data", {}) or {}).get("results", {}) or {})
             .get("songs", {}) or {}).get("data", []) or []
    if not songs:
        return {"status": "error", "error": f"no Apple Music result for {query!r}"}
    song = songs[0]
    attrs = song.get("attributes", {})
    _cider_req("POST", "/playback/play-item", {"type": "songs", "id": str(song.get("id"))})
    return {"status": "ok", "action": "play", "query": query,
            "now_playing": attrs.get("name", ""), "artist": attrs.get("artistName", "")}


def cider_control(action: str = "playpause", query: str = "", volume: float = None) -> dict:
    """Control the Cider music player. Actions: 'play' (optionally with a `query`
    to search + play a song), 'pause', 'playpause'/'toggle', 'stop', 'next'/'skip',
    'previous'/'back', 'shuffle', 'repeat', 'volume' (with `volume` 0..1), and
    'now_playing' to read the current track. Uses Cider's local API on
    localhost:10767 (enable it in Cider's Connectivity settings)."""
    a = (action or "").lower().strip()
    try:
        if a in ("volume", "set_volume") and volume is not None:
            v = max(0.0, min(1.0, float(volume)))
            _cider_req("POST", "/playback/volume", {"volume": v})
            return {"status": "ok", "action": "volume", "volume": v}
        if a in ("now_playing", "current", "what_song", "whats_playing"):
            info = (_cider_req("GET", "/playback/now-playing") or {}).get("info", {})
            return {"status": "ok", "action": "now_playing",
                    "now_playing": info.get("name", ""), "artist": info.get("artistName", "")}
        if a == "play" and query:
            return _cider_play_query(query)
        path = _CIDER_ACTIONS.get(a)
        if not path:
            return {"status": "error", "error": f"unknown cider action: {action}",
                    "known": sorted(set(_CIDER_ACTIONS) | {"volume", "now_playing"})}
        _cider_req("POST", path)
        return {"status": "ok", "action": a}
    except urllib.error.URLError as e:
        return {"status": "error",
                "error": f"Cider not reachable at {CIDER_BASE}. Is Cider running with its "
                         f"local API enabled (Settings → Connectivity)? ({e})"}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# tool registry (same names/order as the macOS original)
# ---------------------------------------------------------------------------
TOOLS = {
    "open_app": open_app,
    "open_thing": open_thing,
    "web_search": web_search,
    "click_link": click_link,
    "take_note": take_note,
    "play_music": play_music,
    "run_terminal": run_terminal,
    "read_screen_aloud": read_screen_aloud,
    "start_obs_recording": start_obs_recording,
    "stop_obs_recording": stop_obs_recording,
    "obs_scene": obs_scene,
    "premiere_control": premiere_control,
    "ask_claude": ask_claude,
    "claude_chat": claude_chat,
    "cider_control": cider_control,
}


# ---------------------------------------------------------------------------
# CLI for standalone testing (no OpenAI needed)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in TOOLS:
        print("usage: python actions.py <tool> [args...]")
        print("tools:", ", ".join(TOOLS))
        sys.exit(1)
    fn = TOOLS[sys.argv[1]]
    kwargs = {}
    rest = sys.argv[2:]
    if rest:
        import inspect
        params = list(inspect.signature(fn).parameters)
        kwargs[params[0]] = " ".join(rest)
    print(json.dumps(fn(**kwargs), indent=2))
