#!/usr/bin/env python3
"""
overlay.py — optional waveform HUD (experimental, same as the macOS original).

Reads {active, level} from %TEMP%/voiceos-hud.json (written by voice_agent.py)
and draws a small black-and-white waveform strip at the top of the screen while
you talk.

⚠  Like the macOS version, this tkinter overlay is always-on-top and can intercept
clicks near the top of the screen. A native click-through rebuild is a TODO. Run it
only while demoing:  python overlay.py
"""
from __future__ import annotations

import json
import os
import tempfile
import tkinter as tk

HUD_FILE = os.path.join(tempfile.gettempdir(), "voiceos-hud.json")
WIDTH, HEIGHT, BARS = 600, 60, 48


def _read():
    try:
        with open(HUD_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return bool(d.get("active")), float(d.get("level", 0.0))
    except Exception:
        return False, 0.0


def main():
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    try:
        root.attributes("-alpha", 0.85)
    except tk.TclError:
        pass
    sw = root.winfo_screenwidth()
    x = (sw - WIDTH) // 2
    root.geometry(f"{WIDTH}x{HEIGHT}+{x}+0")
    canvas = tk.Canvas(root, width=WIDTH, height=HEIGHT, bg="black", highlightthickness=0)
    canvas.pack()

    levels = [0.0] * BARS

    def tick():
        active, level = _read()
        levels.pop(0)
        levels.append(level if active else 0.0)
        canvas.delete("all")
        bw = WIDTH / BARS
        for i, lv in enumerate(levels):
            h = max(2, lv * (HEIGHT - 8))
            cx = i * bw + bw / 2
            canvas.create_rectangle(cx - bw * 0.3, HEIGHT / 2 - h / 2,
                                    cx + bw * 0.3, HEIGHT / 2 + h / 2,
                                    fill="white", outline="")
        root.after(33, tick)

    root.bind("<Escape>", lambda e: root.destroy())
    tick()
    root.mainloop()


if __name__ == "__main__":
    main()
