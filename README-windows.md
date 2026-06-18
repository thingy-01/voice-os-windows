# Voice OS — Windows

Run your Windows PC with your voice. A Windows port of
[per-simmons/voice-os](https://github.com/per-simmons/voice-os): speak a command and
it opens apps, plays music, reads the screen back, controls OBS and Premiere, asks
Claude Desktop something.

- **Brain:** OpenAI `gpt-realtime-2` (speech-to-speech + tool calling)
- **Hands:** Windows UI Automation (`pywinauto` + `uiautomation`) + app APIs (OBS
  WebSocket, Windows SMTC, Spotify Web API). No Node, no `agent-desktop`, no AppleScript.
- **Glue:** this repo — the voice loop and a registry of voice "tools".

---

## Quickstart

**Requirements:** Windows 10/11, Python 3.10+, and an **OpenAI API key with Realtime
access** (billed ~pennies per command).

```bat
REM 1. add your key
copy .env.example .env        & REM paste your OPENAI_API_KEY into .env

REM 2. run it (creates a venv, installs deps, launches in hold-to-talk)
run.bat
```

Then **hold Right Ctrl**, talk, release: *"open Spotify," "play some Tchaikovsky,"
"what's on my screen?"*

> If global hotkeys don't register, run the terminal **as Administrator** (the
> `keyboard` hook needs elevation when an elevated app is focused), or use
> `run.bat --push-to-talk`.

---

## Ways to talk to it

| Mode | Command | Notes |
|---|---|---|
| **Hold-to-talk** (default) | `run.bat` | Hold **Right Ctrl** anywhere. Reliable, $0 idle. Change the key: `run.bat --hotkey f8`. |
| **Push-to-talk** | `run.bat --push-to-talk` | Press ENTER, talk. No global hook needed. |
| **Wake word "hey chat"** | `run.bat --wake` | Streams the mic continuously (~$1/hr idle). |

Pick a specific mic: `set VOICEOS_MIC=Scarlett` then `run.bat`.

---

## What it can do (the tools)

`open_app` · `open_thing` · `web_search` · `click_link` · `take_note` · `play_music` ·
`run_terminal` · `read_screen_aloud` · `start_obs_recording` · `stop_obs_recording` ·
`obs_scene` · `premiere_control` · `ask_claude` · `claude_chat` · `cider_control`

`claude_chat` sends a request to Claude via the Anthropic API and reads the reply
back aloud (set `ANTHROPIC_API_KEY` in `.env`); it keeps short conversation context
for follow-ups. `cider_control` drives the [Cider](https://cider.sh) music player
through its local API on `localhost:10767` — enable it and copy the token at
Cider → Settings → Connectivity → Manage External Application Access, then set
`CIDER_API_TOKEN`.

Every tool is a small function in `actions.py` and is **runnable standalone** (no
OpenAI key needed) — this is how you test each one:

```bat
python actions.py open_app Chrome
python actions.py play_music "Tchaikovsky"
python actions.py read_screen_aloud Notepad
python actions.py premiere_control left 2
```

Run the whole suite: `python test_actions.py` (or `python test_actions.py open_app play_music`).

---

## How it works

```
your voice ─▶ gpt-realtime-2 (decides which tool) ─▶ actions.py
                                                        ├─ desktop.py   (UIA: focus / read / click any app)
                                                        ├─ OBS WebSocket / Spotify SMTC+Web API
                                                        └─ pydirectinput (Premiere shortcuts)
                                                      ─▶ app does the thing ─▶ model speaks a confirmation
```

- `voice_agent.py` — the realtime loop (mic ↔ model ↔ tools) with hold-to-talk, PTT, and wake modes.
- `hotkey.py` — global hold-to-talk via the `keyboard` library (replaces the macOS Carbon shim).
- `desktop.py` — the Windows "accessibility" layer (UI Automation tree, window focus/geometry).
- `actions.py` — the tools (the hands). Each runnable standalone.
- `overlay.py` — the optional waveform HUD.

---

## Per-feature setup notes

- **Spotify exact-track play** needs the **Web API** (Premium): set `SPOTIPY_*` in
  `.env`. Without it, `play_music` still works — it resumes playback and reads
  now-playing via Windows SMTC.
- **OBS:** enable *Tools → WebSocket Server Settings* (port 4455). The password is
  read from OBS's config automatically, or set `OBS_WS_PASSWORD`.
- **Premiere:** shortcuts only land when a panel has focus, so the tool focuses the
  window and clicks the Program Monitor first. If your layout differs, tune
  `PREMIERE_FOCUS_X` / `PREMIERE_FOCUS_Y` in `.env`.
- **click_link** needs Playwright **and** a Chromium started with
  `--remote-debugging-port=9222`. Without that it returns a clear error (the rest
  of the browser flow still works via `web_search`).
- **ask_claude** drives Claude Desktop's Electron UI tree; it's the most fragile
  tool (as on macOS) and falls back to a known reply if the live read times out.

---

## Adding your own app

Same as the original: add a ~15-line function to `actions.py`, register it in the
`TOOLS` dict, and add a matching schema to `TOOLS` in `voice_agent.py`. Use the
`desktop.py` helpers (`focus_window`, `snapshot_text`, `find_control`,
`click_control`, `type_text`, `press_combo`) as your building blocks. Inspect any
app's tree with: `python -c "import desktop; print(desktop.snapshot_text('YourAppTitle'))"`.

License: MIT (same as upstream).
