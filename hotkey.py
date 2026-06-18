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
    "scroll_lock": "scroll lock",
    "pause": "pause",
}

DEFAULT_KEY = "right ctrl"


def resolve_key(name: str | None) -> str:
    if not name:
        return DEFAULT_KEY
    n = name.strip().lower()
    return HOTKEY_ALIASES.get(n, n)  # pass through raw `keyboard` names too


def start_hotkey_listener(key_events: "queue.Queue[str]", key_name: str | None = None) -> str:
    """Begin watching the global hold-to-talk key. Returns the resolved key name.

    Non-fatal if the `keyboard` library is missing — prints guidance and returns
    an empty string so the caller can fall back to push-to-talk."""
    key = resolve_key(key_name)
    try:
        import keyboard  # type: ignore
    except Exception:
        print("⚠  `keyboard` not installed — hold-to-talk unavailable. "
              "Run: pip install keyboard   (or use --push-to-talk)")
        return ""

    state = {"down": False}

    def handler(event):
        # keyboard delivers repeated 'down' events while held; collapse to one.
        if event.event_type == "down" and not state["down"]:
            state["down"] = True
            key_events.put("down")
        elif event.event_type == "up" and state["down"]:
            state["down"] = False
            key_events.put("up")

    try:
        keyboard.hook_key(key, handler, suppress=False)
    except Exception as e:
        print(f"⚠  could not hook key {key!r}: {e}. "
              "On some setups global hooks need an elevated terminal.")
        return ""
    return key
