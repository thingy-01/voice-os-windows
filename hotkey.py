#!/usr/bin/env python3
"""
hotkey.py — global hold-to-talk for Windows.

Replaces the macOS `voice_app.py` Carbon `RegisterEventHotKey` shim. On Windows
the `keyboard` library installs a low-level `SetWindowsHookEx` hook, so we can
watch a single key globally (even when another app is focused) WITHOUT the
event-tap freeze risk that made the macOS build avoid pynput.

It feeds plain "down" / "up" strings into the same `key_events` queue that
voice_agent.hotkey_console() already consumes — so the realtime loop is unchanged.

Hold the key  -> "down"  (start capturing mic)
Release       -> "up"    (commit buffer, ask the model to respond)

The key may be a friendly name ("f13", "right_ctrl") OR a raw scan code number
(e.g. "100"). Scan codes are the fallback when the `keyboard` library doesn't
recognise a key by name — find yours with `python voice_agent.py --detect-key`.
"""
from __future__ import annotations

import queue

# Friendly names (mirroring the macOS --hotkey vocabulary) -> `keyboard` names.
HOTKEY_ALIASES = {
    "right_ctrl": "right ctrl",
    "right_control": "right ctrl",
    "rctrl": "right ctrl",
    "right_option": "right alt",   # macOS right_option ≈ Windows right alt
    "right_alt": "right alt",
    "ralt": "right alt",
    "right_shift": "right shift",
    "f8": "f8",
    "f9": "f9",
    "f13": "f13",
    "scroll_lock": "scroll lock",
    "pause": "pause",
}

# F13 is the default: a key no physical keyboard has, so it never clashes with
# typing or shortcuts — ideal to bind to a spare mouse button (Logitech G HUB /
# Razer Synapse, set to send the keystroke "while held") for hold-to-talk.
DEFAULT_KEY = "f13"


def resolve_key(name: str | None) -> str:
    if not name:
        return DEFAULT_KEY
    n = name.strip().lower()
    return HOTKEY_ALIASES.get(n, n)  # pass through raw `keyboard` names / scan codes


def start_hotkey_listener(key_events: "queue.Queue[str]", key_name: str | None = None) -> str:
    """Begin watching the global hold-to-talk key. Returns the resolved key name.

    Matches by `keyboard` key name OR raw scan code (if `key` is digits), via a
    global hook — so it works even for keys the library can't name (like F13 on
    some layouts). Non-fatal if the `keyboard` library is missing: prints guidance
    and returns an empty string so the caller can fall back to push-to-talk."""
    key = resolve_key(key_name)
    try:
        import keyboard  # type: ignore
    except Exception:
        print("⚠  `keyboard` not installed — hold-to-talk unavailable. "
              "Run: pip install keyboard   (or use --push-to-talk)")
        return ""

    want = key.strip().lower()
    want_code = int(want) if want.isdigit() else None
    state = {"down": False}

    def _matches(event) -> bool:
        if want_code is not None:
            return event.scan_code == want_code
        return (event.name or "").lower() == want

    def handler(event):
        # keyboard delivers repeated 'down' events while held; collapse to one.
        if not _matches(event):
            return
        if event.event_type == "down" and not state["down"]:
            state["down"] = True
            key_events.put("down")
        elif event.event_type == "up" and state["down"]:
            state["down"] = False
            key_events.put("up")

    try:
        keyboard.hook(handler, suppress=False)
    except Exception as e:
        print(f"⚠  could not hook key {key!r}: {e}. "
              "On some setups global hooks need an elevated terminal.")
        return ""
    return key
