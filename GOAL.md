# Goal Prompt — Voice OS for Windows

> Use this as the framing prompt for the build (hand it to a coding agent, or keep
> it as the project's north star). It defines *what done looks like*, not the steps.

## Objective

Build a Windows 10/11 port of [per-simmons/voice-os](https://github.com/per-simmons/voice-os):
a hackable, voice-controlled "operating system" where you speak a command and your
PC does it — open apps, play music, read the screen back, control OBS and Premiere,
ask Claude Desktop something. Keep the original's architecture and philosophy
(small, deterministic, each tool a ~15-line function you can clone and extend), but
replace every macOS automation primitive with a Windows-native equivalent.

## Brain / Hands / Glue (unchanged concept)

- **Brain:** OpenAI `gpt-realtime-2` — speech-to-speech + tool calling.
- **Hands:** Windows UI Automation (via `pywinauto` + `uiautomation`), plus
  `pydirectinput`/`pyautogui` for raw input and app-specific APIs (OBS WebSocket,
  Windows SMTC, Spotify Web API). **No `agent-desktop`, no Node, no AppleScript.**
- **Glue:** this repo — the voice loop and a registry of voice "tools".

## Primary interaction

**Hold-to-talk** is the headline trigger: hold a global hotkey (default **Right Ctrl**)
anywhere, speak, release to send. Also keep **push-to-talk (Enter)** and **wake word**
modes from the original. Use the `keyboard` library's low-level hook for the global
hotkey (Windows allows this cleanly — no event-tap freeze risk like macOS).

## Tool parity (all 12 must exist and return `{"status": ...}`)

`open_app` · `web_search` · `click_link` · `take_note` · `play_music` ·
`run_terminal` · `read_screen_aloud` · `start_obs_recording` · `stop_obs_recording` ·
`obs_scene` · `premiere_control` · `ask_claude`

## Hard requirements

1. **Standalone-testable:** every tool runs without an OpenAI key, e.g.
   `python actions.py open_app Chrome`. This is how each is validated.
2. **Same tool registry + JSON contract** as the macOS version so `voice_agent.py`'s
   tool schemas line up unchanged.
3. **Graceful degradation:** a missing optional dep (Spotify Web API, Playwright)
   disables only that path and returns a clear `status:"error"` — it never crashes
   the loop.
4. **No hardcoded macOS paths:** use `%APPDATA%`, `tempfile`, `expanduser`.
5. **Windows-only deps isolated** behind lazy imports so the modules import on any OS.

## Definition of done

Running `run.bat`, holding Right Ctrl and speaking the README demo works end-to-end:
"open Spotify" → "play some Tchaikovsky" → "what's on my screen?" → "start recording"
→ a Premiere frame-nudge → "ask Claude to rewrite the intro". Each tool also passes
its standalone `test_actions.py` check (`status == "ok"` on a machine with the target
app present).

## Non-goals

Pixel-based screen scraping, training a custom wake word (Picovoice is optional/out
of scope for v1), and mobile/Linux support.
