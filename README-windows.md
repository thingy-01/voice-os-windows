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

Then **hold F13**, talk, release: *"open Spotify," "play some Tchaikovsky,"
"what's on my screen?"*

**Radio-style hold-to-talk on a mouse button.** The default key is **F13** — a
key no physical keyboard has, so it never clashes with typing or shortcuts. Bind
a spare **mouse button to F13** in your mouse software (Logitech G HUB / Razer
Synapse), set to send the keystroke **"while held"**, and you've got a
walkie-talkie button. Not sure what your button emits? `python voice_agent.py
--detect-key`, hold the button, and use the printed name or scan code as
`--hotkey <value>` (or `VOICEOS_HOTKEY` in `.env`).

> If global hotkeys don't register, run the terminal **as Administrator** (the
> `keyboard` hook needs elevation when an elevated app is focused), or use
> `run.bat --push-to-talk`.

---

## Ways to talk to it

| Mode | Command | Notes |
|---|---|---|
| **Hold-to-talk** (default) | `run.bat` | Hold **F13** (bind it to a mouse button) anywhere. Reliable, $0 idle. Change the key: `run.bat --hotkey right_ctrl` (or any name/scan code). |
| **Push-to-talk** | `run.bat --push-to-talk` | Press ENTER, talk. No global hook needed. |
| **Wake word "hey chat"** | `run.bat --wake` | Streams the mic continuously (~$1/hr idle). |

Pick a specific mic: `set VOICEOS_MIC=Scarlett` then `run.bat`.

---

## What it can do (the tools)

`open_app` · `open_thing` · `web_search` · `click_link` · `take_note` · `play_music` ·
`run_terminal` · `read_screen_aloud` · `start_obs_recording` · `stop_obs_recording` ·
`obs_scene` · `premiere_control` · `ask_claude` · `claude_chat` · `cider_control` ·
`delegate_to_claude` · `check_claude` · `stop_claude` · `github_status` · `review_with_claude`

`claude_chat` sends a request to Claude via the Anthropic API and reads the reply
back aloud (set `ANTHROPIC_API_KEY` in `.env`); it keeps short conversation context
for follow-ups. `cider_control` drives the [Cider](https://cider.sh) music player
through its local API on `localhost:10767` — enable it and copy the token at
Cider → Settings → Connectivity → Manage External Application Access, then set
`CIDER_API_TOKEN`.

### Delegate real work to Claude Code (the two-way bridge)

`claude_chat` is a one-shot Q&A; **`delegate_to_claude` hands a task to a headless
Claude *Code* agent that actually does the work on this PC** — edits files, runs
commands, multi-step jobs — in the **background**, and `check_claude` reads
summarized progress back to you:

```
"hold F13" → "ask Claude to add a dark-mode toggle to the settings page"
        → ⚙ delegate_to_claude(...)  → "On it — I've started job 1."
   ...do something else...
"hold F13" → "how's it going?"
        → ⚙ check_claude()  → "It edited Settings.tsx and added the toggle; done."
```

`delegate_to_claude` returns instantly (the voice loop never blocks); `check_claude`
reports **only what's new since you last asked** plus Claude's final answer when
done. Say *"stop that"* to cancel. Needs the **`claude` CLI** on PATH
(`claude --version`; Windows: `irm https://claude.ai/install.ps1 | iex`, then a new
terminal). Config via `.env`: `VOICEOS_CLAUDE_CWD` (which project it works in),
`VOICEOS_CLAUDE_PERMISSION_MODE` (default `bypassPermissions` so it runs unattended —
point `CWD` at a project you trust). Try it without a mic or OpenAI key:

```bat
python agent_bridge.py start "create hello.txt with the word hi"
python agent_bridge.py wait
python test_agent_bridge.py
```

#### Recent work & *cloud* jobs — `github_status` / `review_with_claude`

`delegate_to_claude`/`check_claude` only see jobs **this PC** started by voice. A
job you fire off **in the cloud** — e.g. from the Claude phone app, running in a
cloud container that pushes to a repo — is invisible to them, but everything it
does lands on **GitHub**. Two layers read that back, with **nothing hardcoded**:

- **`github_status` (cheap, instant).** With *no repo* it auto-discovers your most
  recently active repos and summarizes them — *"what have I been working on?"*,
  *"catch me up."* With a repo it checks that one — *"how's the Jurytics update
  going?"*, *"is the PR done?"* — latest commit, open PRs, CI, spoken aloud. You
  only say the **name** (no `owner/`); it's fuzzy-matched against your real repos,
  so a misheard *"juridics"* still finds `…/juritix` (and asks if it's unsure).
- **`project_board` / `ticket_details`.** Talk through a **GitHub Projects board**
  (the new kanban boards at `github.com/users/<you>/projects/<n>`) — *"what's on my
  jurytics board?"*, *"what's in progress?"* lists tickets grouped by column;
  *"tell me about the login ticket"* / *"ticket 12"* reads one in depth (status,
  assignees, description, latest comments). Board name is fuzzy-matched too; set a
  default with `VOICEOS_GITHUB_PROJECT`. Needs the token's `read:project` scope.
- **`review_with_claude` (deep).** Auto-discovers the same recent work, then hands
  it to a background Claude Code agent that reasons over it and gives a briefing
  **plus the best next step per project** — *"have Claude review what I've been
  working on and tell me what's next."* Then ask *"what did Claude say?"*
  (`check_claude`), and — this is the nice part — *"go ahead and do the first thing
  you suggested"* (`continue_claude`) **resumes Claude's same session** so it acts
  on its own plan with full memory. (For a brand-new task, `delegate_to_claude`.)

All it needs is a token (the repo isn't hardcoded — discovery finds it):

```ini
GITHUB_TOKEN=ghp_...        # Settings → Developer settings → Tokens (repo scope)
# VOICEOS_GITHUB_REPO=owner/name   # optional default for "how's it going" with no name
```

```bat
python github_status.py recent          REM auto-discover your recent repos
python github_status.py owner/name       REM a specific repo (latest commit + PRs + CI)
python github_status.py owner/name 42    REM focus pull request #42
```

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
- `hotkey.py` — global hold-to-talk via the `keyboard` library (replaces the macOS Carbon shim). Default key **F13**.
- `desktop.py` — the Windows "accessibility" layer (UI Automation tree, window focus/geometry).
- `actions.py` — the tools (the hands). Each runnable standalone.
- `agent_bridge.py` — the Claude Code bridge: background jobs + summarized updates (`delegate_to_claude` / `check_claude` / `stop_claude`). Runnable standalone.
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
