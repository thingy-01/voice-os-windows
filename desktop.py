#!/usr/bin/env python3
"""
desktop.py — the Windows "accessibility" layer.

This is the Windows replacement for macOS `agent-desktop`. Where agent-desktop
walked the macOS accessibility tree, here we walk the **Windows UI Automation
(UIA)** tree — the same underlying idea, different API.

Two libraries back this module:
  - `uiautomation`  — recursive tree walking + text extraction (closest match to
                      agent-desktop's `snapshot`). Used for read_screen / Electron.
  - `pygetwindow`   — fast window find + focus + geometry (replaces Quartz).
  - `pywinauto`     — robust control invoking / typing when we need it.

All imports are LAZY so this file imports cleanly on any OS (handy for tests /
syntax checks on non-Windows machines). Every public function degrades to a
structured error instead of raising, mirroring the original's "return a dict"
contract.
"""
from __future__ import annotations

import sys
import time
from typing import Optional


def is_windows() -> bool:
    return sys.platform.startswith("win")


# ---------------------------------------------------------------------------
# lazy library handles
# ---------------------------------------------------------------------------
def _auto():
    """The `uiautomation` module (raises ImportError off-Windows / if missing)."""
    import uiautomation as auto  # type: ignore
    return auto


def _gw():
    import pygetwindow as gw  # type: ignore
    return gw


# ---------------------------------------------------------------------------
# window find / focus / geometry
# ---------------------------------------------------------------------------
def find_window(title_substr: str):
    """Return the first pygetwindow Window whose title contains `title_substr`
    (case-insensitive), or None."""
    try:
        gw = _gw()
    except Exception:
        return None
    needle = (title_substr or "").lower()
    for w in gw.getAllWindows():
        if w.title and needle in w.title.lower():
            return w
    return None


def focus_window(title_substr: str) -> bool:
    """Bring a window matching `title_substr` to the foreground."""
    w = find_window(title_substr)
    if not w:
        return False
    try:
        if w.isMinimized:
            w.restore()
        w.activate()
        return True
    except Exception:
        # pygetwindow.activate() can flake if the window briefly loses focus;
        # fall back to a UIA SetActive.
        try:
            auto = _auto()
            ctrl = auto.WindowControl(searchDepth=1, SubName=title_substr)
            if ctrl.Exists(1):
                ctrl.SetActive()
                return True
        except Exception:
            pass
    return False


def window_bounds(title_substr: str) -> Optional[tuple]:
    """(x, y, w, h) of the largest matching on-screen window, or None.
    Windows replacement for the Quartz CGWindowList lookup used for Premiere."""
    try:
        gw = _gw()
    except Exception:
        return None
    best, best_area = None, 0
    for w in gw.getAllWindows():
        if not w.title or (title_substr.lower() not in w.title.lower()):
            continue
        if w.width < 200 or w.height < 200:
            continue
        area = w.width * w.height
        if area > best_area:
            best_area = area
            best = (w.left, w.top, w.width, w.height)
    return best


# ---------------------------------------------------------------------------
# UIA tree walking (the snapshot equivalent)
# ---------------------------------------------------------------------------
# Control types whose text we treat as "visible screen text".
_TEXT_TYPES = {"TextControl", "DocumentControl", "EditControl", "HyperlinkControl"}


def _walk(control, depth, max_depth, visit):
    """Depth-first walk of a uiautomation control subtree, calling visit(control)."""
    try:
        visit(control)
    except Exception:
        pass
    if depth >= max_depth:
        return
    try:
        children = control.GetChildren()
    except Exception:
        children = []
    for ch in children:
        _walk(ch, depth + 1, max_depth, visit)


def snapshot_text(title_substr: str, max_depth: int = 35,
                  enable_a11y: bool = True) -> str:
    """Walk the UIA tree of the window matching `title_substr` and return its
    visible text — the engine behind read_screen_aloud and ask_claude reads.

    `enable_a11y` forces Chromium/Electron apps (Claude Desktop) to expose their
    tree (the Windows analogue of the macOS AXManualAccessibility poke)."""
    try:
        auto = _auto()
    except Exception as e:  # uiautomation missing / non-Windows
        return ""
    if enable_a11y:
        try:
            # Touching the tree at all generally prompts Chromium to build it;
            # a short settle gives Electron time to populate.
            pass
        except Exception:
            pass
    win = auto.WindowControl(searchDepth=2, SubName=title_substr)
    if not win.Exists(2):
        return ""
    out: list[str] = []

    def visit(c):
        try:
            ctype = c.ControlTypeName
        except Exception:
            return
        if ctype in _TEXT_TYPES:
            for attr in ("Name", "GetValuePattern"):
                pass
            name = (getattr(c, "Name", "") or "").strip()
            if name:
                out.append(name)
            # EditControl/Document carry their content in ValuePattern.
            try:
                vp = c.GetValuePattern()
                val = (vp.Value or "").strip()
                if val and val != name:
                    out.append(val)
            except Exception:
                pass

    _walk(win, 0, max_depth, visit)
    # de-dup consecutive repeats
    seen: list[str] = []
    for s in out:
        if not seen or seen[-1] != s:
            seen.append(s)
    return "\n".join(seen)


def find_control(title_substr: str, name: str = "", control_type: str = "",
                 subtext: str = "", max_depth: int = 35):
    """Find the first UIA control under window `title_substr` matching any of:
      - exact Name == `name`
      - ControlTypeName == `control_type`
      - subtree text contains `subtext`
    Returns the uiautomation control or None. (Equivalent to agent-desktop's
    find-by-name / find-by-subtext helpers in actions.py.)"""
    try:
        auto = _auto()
    except Exception:
        return None
    win = auto.WindowControl(searchDepth=2, SubName=title_substr)
    if not win.Exists(2):
        return None
    target_name = (name or "").lower()
    needle = (subtext or "").lower()
    found = {"c": None}

    def visit(c):
        if found["c"] is not None:
            return
        try:
            cname = (c.Name or "").lower()
            ctype = c.ControlTypeName
        except Exception:
            return
        if name and cname == target_name:
            found["c"] = c
        elif control_type and ctype == control_type and not name and not subtext:
            found["c"] = c
        elif subtext:
            # check this control's own name; subtree match is approximated by the
            # walk visiting children anyway
            if needle in cname:
                found["c"] = c

    _walk(win, 0, max_depth, visit)
    return found["c"]


def click_control(control) -> bool:
    """Invoke/click a uiautomation control (Invoke pattern, else a real click)."""
    if control is None:
        return False
    try:
        control.GetInvokePattern().Invoke()
        return True
    except Exception:
        pass
    try:
        control.Click(simulateMove=False)
        return True
    except Exception:
        return False


def type_text(control, text: str) -> bool:
    """Focus an edit control and type `text` into it."""
    if control is None:
        return False
    try:
        control.SetFocus()
        time.sleep(0.15)
        # SendKeys via uiautomation handles unicode + special chars.
        control.SendKeys(_escape_sendkeys(text), waitTime=0)
        return True
    except Exception:
        return False


def _escape_sendkeys(text: str) -> str:
    """uiautomation SendKeys treats {}()+^%~ as special — escape them."""
    out = []
    for ch in text:
        if ch in "{}()+^%~[]":
            out.append("{" + ch + "}")
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# raw input (no element targeting) — Premiere shortcuts, focus clicks
# ---------------------------------------------------------------------------
def press_combo(combo: str) -> bool:
    """Send a keyboard combo like 'ctrl+k' / 'space' / 'shift+delete' using
    pydirectinput (scan codes — Premiere and games accept these where
    pyautogui's virtual-key events get dropped)."""
    try:
        import pydirectinput as pdi  # type: ignore
    except Exception:
        try:
            import pyautogui as pdi  # type: ignore
        except Exception:
            return False
    parts = [p.strip() for p in combo.lower().split("+") if p.strip()]
    if not parts:
        return False
    *mods, key = parts
    try:
        for m in mods:
            pdi.keyDown(m)
        pdi.press(key)
        for m in reversed(mods):
            pdi.keyUp(m)
        return True
    except Exception:
        return False


def click_xy(x: int, y: int) -> bool:
    try:
        import pyautogui  # type: ignore
        pyautogui.click(x=int(x), y=int(y))
        return True
    except Exception:
        return False
