#!/usr/bin/env python3
"""
StillChecker Manager
====================
One-click launcher for the raspberry_ninja framebuffer viewer (publish.py)
together with the readnew2.py monitor GUI.

Paste a VDO.Ninja stream ID or a full link, click Start, and this will:
  1. start publish.py in --framebuffer mode (the "server" that pulls the
     stream off VDO.Ninja and writes frames into shared memory), and
  2. open readnew2.py (the monitor "interface") as soon as the stream is
     live, so its Connect button works on the first try.

It does NOT modify publish.py or readnew2.py - it only runs them, so your
monitoring code stays exactly as it is.

Run it with the SAME Python you use for publish.py (e.g. your venv):
    python manager.py
"""

import os
import sys
import shlex
import queue
import threading
import subprocess
from urllib.parse import urlparse, parse_qs

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

try:
    from multiprocessing import shared_memory
    from multiprocessing.resource_tracker import unregister as _shm_unregister
except Exception:
    shared_memory = None
    _shm_unregister = None

HERE = os.path.dirname(os.path.abspath(__file__))
PUBLISH_PY = os.path.join(HERE, "publish.py")
MONITOR_PY = os.path.join(HERE, "readnew2.py")
SHM_NAME = "psm_raspininja_streamid"
IS_WIN = os.name == "nt"

# Stability defaults (the fix/framebuffer-reconnect-stability recommendation).
DEFAULT_STABILITY_ARGS = [
    "--viewer-retry-initial", "3",
    "--viewer-retry-short", "8",
    "--viewer-retry-long", "20",
]

# How long to wait for the stream to go live before opening the monitor anyway.
MONITOR_FALLBACK_MS = 25000


def parse_stream_input(text):
    """Pull (stream_id, room, password) out of a raw ID or a VDO.Ninja URL."""
    text = (text or "").strip()
    if not text:
        return None, None, None

    # A bare stream ID (no URL/query characters).
    if "://" not in text and "?" not in text and "=" not in text:
        return text, None, None

    parsed = urlparse(text)
    query = parsed.query or text.lstrip("?")
    qs = parse_qs(query, keep_blank_values=True)

    def first(*keys):
        for key in keys:
            for variant in (key, key.lower(), key.upper()):
                if variant in qs and qs[variant]:
                    return qs[variant][0]
        return None

    stream_id = first("view", "v", "push", "id", "streamid", "stream")
    room = first("room", "r")
    password = first("password", "pass")
    return stream_id, room, password


class ManagerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("StillChecker Manager")
        self.geometry("780x540")
        self.minsize(640, 420)

        self.engine_proc = None
        self.monitor_proc = None
        self._log_q = queue.Queue()
        self._shm_ready = threading.Event()
        self._monitor_launched = False
        self._stop_requested = False
        self._fallback_id = None
        self._pending_status = "stopped"

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(150, self._tick)

    # ------------------------------- UI -------------------------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self)
        frm.pack(side=tk.TOP, fill=tk.X, **pad)

        ttk.Label(frm, text="Stream ID or VDO.Ninja link:").grid(row=0, column=0, sticky="w")
        self.stream_var = tk.StringVar()
        self.stream_entry = ttk.Entry(frm, textvariable=self.stream_var, width=64)
        self.stream_entry.grid(row=0, column=1, sticky="we", padx=6)

        ttk.Label(frm, text="Password (optional):").grid(row=1, column=0, sticky="w")
        self.pass_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.pass_var, width=64).grid(row=1, column=1, sticky="we", padx=6)

        ttk.Label(frm, text="Extra publish.py options:").grid(row=2, column=0, sticky="w")
        self.extra_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.extra_var, width=64).grid(row=2, column=1, sticky="we", padx=6)

        frm.columnconfigure(1, weight=1)

        btns = ttk.Frame(self)
        btns.pack(side=tk.TOP, fill=tk.X, **pad)
        self.start_btn = ttk.Button(btns, text="Start", command=self.start)
        self.start_btn.pack(side=tk.LEFT)
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=6)

        self.status_var = tk.StringVar(value="Status: idle")
        ttk.Label(btns, textvariable=self.status_var).pack(side=tk.LEFT, padx=16)

        ttk.Label(self, text=f"Python: {sys.executable}", foreground="#666").pack(
            side=tk.TOP, anchor="w", padx=8)

        self.log = scrolledtext.ScrolledText(self, height=20, state=tk.DISABLED, wrap=tk.WORD)
        self.log.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=8)

    # ---------------------------- logging -----------------------------
    def _log(self, msg):
        self._log_q.put(str(msg).rstrip("\n"))

    def _append_log(self, msg):
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, msg + "\n")
        # Keep the widget light by trimming old lines.
        if int(self.log.index("end-1c").split(".")[0]) > 800:
            self.log.delete("1.0", "200.0")
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def _set_status(self, text):
        self.status_var.set(f"Status: {text}")

    @staticmethod
    def _quote(s):
        return f'"{s}"' if " " in s else s

    # ----------------------------- start ------------------------------
    def start(self):
        if self.engine_proc and self.engine_proc.poll() is None:
            messagebox.showinfo("Already running", "Stop the current session first.")
            return

        stream_id, room, link_pw = parse_stream_input(self.stream_var.get())
        if not stream_id:
            messagebox.showerror("Missing stream", "Enter a VDO.Ninja stream ID or link.")
            return
        if not os.path.exists(PUBLISH_PY):
            messagebox.showerror("Not found", f"Cannot find publish.py at:\n{PUBLISH_PY}")
            return

        password = self.pass_var.get().strip() or link_pw

        # A killed previous run can leave a stale shared-memory segment on Linux;
        # clear it so publish.py can re-create it cleanly.
        self._clear_stale_shm()

        cmd = [sys.executable, "-u", PUBLISH_PY, "--framebuffer", stream_id]
        cmd += DEFAULT_STABILITY_ARGS
        if room:
            cmd += ["--room", room]
        if password:
            cmd += ["--password", password]
        extra = self.extra_var.get().strip()
        if extra:
            try:
                cmd += shlex.split(extra, posix=not IS_WIN)
            except ValueError as exc:
                messagebox.showerror("Bad options", f"Could not parse extra options:\n{exc}")
                return

        self._stop_requested = False
        self._pending_status = "stopped"
        self._shm_ready.clear()
        self._monitor_launched = bool(self.monitor_proc and self.monitor_proc.poll() is None)

        flags = subprocess.CREATE_NO_WINDOW if IS_WIN else 0
        try:
            self.engine_proc = subprocess.Popen(
                cmd, cwd=HERE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, creationflags=flags,
            )
        except Exception as exc:
            messagebox.showerror("Start failed", f"Could not start publish.py:\n{exc}")
            return

        self._log("=" * 60)
        self._log("Starting engine: " + " ".join(self._quote(c) for c in cmd))
        self._set_status(f"engine running (stream {stream_id})")
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

        threading.Thread(target=self._read_engine, args=(self.engine_proc,), daemon=True).start()
        self._fallback_id = self.after(MONITOR_FALLBACK_MS, self._fallback_launch_monitor)

    def _read_engine(self, proc):
        try:
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                self._log_q.put(line.rstrip("\n"))
                if SHM_NAME in line:
                    self._shm_ready.set()
        except Exception as exc:
            self._log_q.put(f"[manager] engine reader error: {exc}")
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

    # ---------------------------- monitor -----------------------------
    def _launch_monitor(self, reason):
        if self._monitor_launched or self._stop_requested:
            return
        if not os.path.exists(MONITOR_PY):
            self._log(f"[manager] readnew2.py not found at {MONITOR_PY}; skipping monitor.")
            return
        flags = subprocess.CREATE_NO_WINDOW if IS_WIN else 0
        try:
            self.monitor_proc = subprocess.Popen(
                [sys.executable, MONITOR_PY], cwd=HERE, creationflags=flags)
            self._monitor_launched = True
            self._log(f"[manager] Monitor opened ({reason}). Click 'Connect' in its window.")
        except Exception as exc:
            self._log(f"[manager] Could not open monitor: {exc}")

    def _fallback_launch_monitor(self):
        self._fallback_id = None
        if self._monitor_launched or self._stop_requested:
            return
        if self.engine_proc and self.engine_proc.poll() is None:
            self._launch_monitor("stream not detected yet - connect once it is live")

    # ----------------------------- stop -------------------------------
    def stop(self):
        self._stop_requested = True
        self._pending_status = "stopped"
        self._cancel_fallback()
        for label, proc in (("monitor", self.monitor_proc), ("engine", self.engine_proc)):
            if proc and proc.poll() is None:
                self._log(f"[manager] stopping {label}...")
                try:
                    proc.terminate()
                except Exception:
                    pass
        self._set_status("stopping")
        self.after(2500, self._force_kill)

    def _force_kill(self):
        for proc in (self.monitor_proc, self.engine_proc):
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.monitor_proc = None
        self.engine_proc = None
        self._clear_stale_shm()
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self._set_status(self._pending_status)

    def _cancel_fallback(self):
        if self._fallback_id is not None:
            try:
                self.after_cancel(self._fallback_id)
            except Exception:
                pass
            self._fallback_id = None

    # ------------------------------ shm -------------------------------
    def _clear_stale_shm(self):
        if shared_memory is None:
            return
        try:
            shm = shared_memory.SharedMemory(name=SHM_NAME)
        except FileNotFoundError:
            return
        except Exception:
            return
        try:
            if _shm_unregister is not None:
                try:
                    _shm_unregister(shm._name, "shared_memory")
                except Exception:
                    pass
            shm.close()
            shm.unlink()
            self._log("[manager] cleared a stale shared-memory segment")
        except Exception:
            pass

    # ------------------------------ tick ------------------------------
    def _tick(self):
        # Drain queued log lines onto the widget (main thread only).
        try:
            while True:
                self._append_log(self._log_q.get_nowait())
        except queue.Empty:
            pass

        # Open the monitor the moment the engine reports the stream is live.
        if self._shm_ready.is_set() and not self._monitor_launched and not self._stop_requested:
            self._cancel_fallback()
            self._launch_monitor("stream is live")

        # Detect an unexpected engine exit and reset to idle.
        if self.engine_proc is not None and self.engine_proc.poll() is not None and not self._stop_requested:
            code = self.engine_proc.returncode
            self._log(f"[manager] engine exited (code {code}). Press Start to retry.")
            self._stop_requested = True
            self._pending_status = f"engine exited (code {code})"
            self._cancel_fallback()
            self.after(50, self._force_kill)

        self.after(150, self._tick)

    # ----------------------------- close ------------------------------
    def on_close(self):
        self._stop_requested = True
        self._cancel_fallback()
        for proc in (self.monitor_proc, self.engine_proc):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        for proc in (self.monitor_proc, self.engine_proc):
            if proc:
                try:
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        self._clear_stale_shm()
        self.destroy()


if __name__ == "__main__":
    ManagerApp().mainloop()
