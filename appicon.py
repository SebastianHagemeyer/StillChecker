"""Shared helper to put the purple flame on the app windows (and taskbar)."""
import os
import tkinter as tk

_ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
_ICO = os.path.join(_ICON_DIR, "flame.ico")
_PNG = os.path.join(_ICON_DIR, "flame.png")


def apply_flame_icon(win, app_id="flame.cookmonitor"):
    """Best-effort: set the flame as the window + taskbar icon. Never raises."""
    if os.name == "nt":
        # Make Windows group/label the window under our own id so the taskbar
        # shows the flame instead of the generic python/pythonw icon.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        except Exception:
            pass
        try:
            if os.path.exists(_ICO):
                win.iconbitmap(_ICO)
        except Exception:
            pass
    try:
        if os.path.exists(_PNG):
            img = tk.PhotoImage(file=_PNG)
            win._flame_icon_ref = img  # keep a reference so Tk doesn't GC it
            win.iconphoto(True, img)
    except Exception:
        pass
