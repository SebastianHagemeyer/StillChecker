#!/usr/bin/env python3
"""
LAN Cam Manager (manager2)
==========================
One-click NATIVE Windows launcher for the phone-camera monitor. No WSL, no
terminal. Double-click manager2.bat (or run `pythonw manager2.py`) and it:

  1. starts lancam_host.py  (serves the HTTPS camera page + writes frames into
     shared memory), and
  2. opens readnew2.py      (the monitor) with auto-connect, so it attaches to
     the stream by itself.

Then it shows the link to open in Safari on your phone, with a "Copy link"
button so you can paste it into a message and send it to yourself.

It does not touch publish.py / manager.py - this is the self-hosted LAN path,
separate from the VDO.Ninja one.
"""

import os
import sys
import queue
import socket
import threading
import subprocess

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

from appicon import apply_flame_icon

try:
    from multiprocessing import shared_memory
    from multiprocessing.resource_tracker import unregister as _shm_unregister
except Exception:
    shared_memory = None
    _shm_unregister = None

HERE = os.path.dirname(os.path.abspath(__file__))
HOST_PY = os.path.join(HERE, "lancam_host.py")
MONITOR_PY = os.path.join(HERE, "readnew2.py")
SHM_NAME = "psm_raspininja_streamid"
IS_WIN = os.name == "nt"


def primary_lan_ip():
    """Best-effort outbound LAN IPv4 (the address the phone shares)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))          # no packets are actually sent
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class LanCamManager(tk.Tk):
    def __init__(self):
        super().__init__()
        apply_flame_icon(self)
        self.title("LAN Cam Manager")
        self.geometry("760x540")
        self.minsize(640, 460)

        self.host_proc = None
        self.monitor_proc = None
        self._log_q = queue.Queue()
        self._stop_requested = False
        self.url = ""

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(150, self._tick)

    # ------------------------------- UI -------------------------------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        opts = ttk.LabelFrame(self, text="Options")
        opts.pack(side=tk.TOP, fill=tk.X, **pad)
        ttk.Label(opts, text="Port:").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.port_var = tk.StringVar(value="8443")
        ttk.Entry(opts, textvariable=self.port_var, width=8).grid(row=0, column=1, sticky="w")
        ttk.Label(opts, text="FPS:").grid(row=0, column=2, sticky="w", padx=(18, 6))
        self.fps_var = tk.StringVar(value="10")
        ttk.Entry(opts, textvariable=self.fps_var, width=6).grid(row=0, column=3, sticky="w")
        ttk.Label(opts, text="JPEG quality:").grid(row=0, column=4, sticky="w", padx=(18, 6))
        self.quality_var = tk.StringVar(value="0.8")
        ttk.Entry(opts, textvariable=self.quality_var, width=6).grid(row=0, column=5, sticky="w")
        self.audio_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Audio (rattle)", variable=self.audio_var).grid(
            row=0, column=6, sticky="w", padx=(18, 6))

        btns = ttk.Frame(self)
        btns.pack(side=tk.TOP, fill=tk.X, **pad)
        self.start_btn = ttk.Button(btns, text="Start", command=self.start)
        self.start_btn.pack(side=tk.LEFT)
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=6)
        self.status_var = tk.StringVar(value="Status: idle")
        ttk.Label(btns, textvariable=self.status_var).pack(side=tk.LEFT, padx=16)

        link = ttk.LabelFrame(self, text="Open this on your phone (Safari)")
        link.pack(side=tk.TOP, fill=tk.X, **pad)
        self.url_var = tk.StringVar(value="(press Start)")
        url_entry = ttk.Entry(link, textvariable=self.url_var, state="readonly",
                              font=("Consolas", 14))
        url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8, pady=8)
        self.copy_btn = ttk.Button(link, text="Copy link", command=self.copy_link,
                                   state=tk.DISABLED)
        self.copy_btn.pack(side=tk.LEFT, padx=8)

        ttk.Label(self, text=f"Python: {sys.executable}", foreground="#666").pack(
            side=tk.TOP, anchor="w", padx=10)

        self.log = scrolledtext.ScrolledText(self, height=14, state=tk.DISABLED, wrap=tk.WORD)
        self.log.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=8)

    # ---------------------------- logging -----------------------------
    def _log(self, msg):
        self._log_q.put(str(msg).rstrip("\n"))

    def _append_log(self, msg):
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, msg + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 800:
            self.log.delete("1.0", "200.0")
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def _set_status(self, text):
        self.status_var.set(f"Status: {text}")

    # ----------------------------- start ------------------------------
    def start(self):
        if self.host_proc and self.host_proc.poll() is None:
            messagebox.showinfo("Already running", "Stop the current session first.")
            return
        if not os.path.exists(HOST_PY):
            messagebox.showerror("Not found", f"Cannot find lancam_host.py at:\n{HOST_PY}")
            return

        port = self._clean_int(self.port_var.get(), 8443, 1, 65535)
        fps = self._clean_int(self.fps_var.get(), 10, 1, 60)
        quality = self._clean_float(self.quality_var.get(), 0.8, 0.1, 1.0)
        self.port_var.set(str(port))
        self.fps_var.set(str(fps))
        self.quality_var.set(f"{quality:g}")

        self._clear_stale_shm()
        self._stop_requested = False

        flags = subprocess.CREATE_NO_WINDOW if IS_WIN else 0
        host_cmd = [sys.executable, "-u", HOST_PY,
                    "--port", str(port), "--fps", str(fps), "--quality", str(quality)]
        if self.audio_var.get():
            host_cmd.append("--audio")
        try:
            self.host_proc = subprocess.Popen(
                host_cmd, cwd=HERE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, creationflags=flags)
        except Exception as exc:
            messagebox.showerror("Start failed", f"Could not start lancam_host.py:\n{exc}")
            return

        # Monitor, with hands-off auto-connect to the shared memory.
        if os.path.exists(MONITOR_PY):
            env = dict(os.environ, RN_AUTOCONNECT=SHM_NAME)
            try:
                self.monitor_proc = subprocess.Popen(
                    [sys.executable, MONITOR_PY], cwd=HERE, env=env, creationflags=flags)
            except Exception as exc:
                self._log(f"[manager2] could not open monitor: {exc}")
        else:
            self._log(f"[manager2] readnew2.py not found at {MONITOR_PY}; skipping monitor.")

        self.url = f"https://{primary_lan_ip()}:{port}"
        self.url_var.set(self.url)
        self.copy_btn.config(state=tk.NORMAL)

        self._log("=" * 60)
        self._log("Started. On the phone open: " + self.url)
        self._log("Accept the certificate warning once, then tap 'Start camera'.")
        self._set_status(f"running on port {port}")
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

        threading.Thread(target=self._read_host, args=(self.host_proc,), daemon=True).start()

    def _read_host(self, proc):
        try:
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                self._log_q.put(line.rstrip("\n"))
        except Exception as exc:
            self._log_q.put(f"[manager2] host reader error: {exc}")
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

    # ----------------------------- copy -------------------------------
    def copy_link(self):
        if not self.url:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(self.url)
            self.update()  # flush clipboard so it survives even if unfocused
            self._set_status("link copied to clipboard")
        except Exception as exc:
            self._log(f"[manager2] copy failed: {exc}")

    # ------------------------- input helpers --------------------------
    @staticmethod
    def _clean_int(text, default, lo, hi):
        try:
            return max(lo, min(hi, int(str(text).strip())))
        except Exception:
            return default

    @staticmethod
    def _clean_float(text, default, lo, hi):
        try:
            return max(lo, min(hi, float(str(text).strip())))
        except Exception:
            return default

    # ------------------------------ stop ------------------------------
    def stop(self):
        self._stop_requested = True
        for label, proc in (("monitor", self.monitor_proc), ("host", self.host_proc)):
            if proc and proc.poll() is None:
                self._log(f"[manager2] stopping {label}...")
                try:
                    proc.terminate()
                except Exception:
                    pass
        self._set_status("stopping")
        self.after(2000, self._force_kill)

    def _force_kill(self):
        for proc in (self.monitor_proc, self.host_proc):
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.monitor_proc = None
        self.host_proc = None
        self._clear_stale_shm()
        self.copy_btn.config(state=tk.DISABLED)
        self.url_var.set("(press Start)")
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self._set_status("stopped")

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
            self._log("[manager2] cleared a stale shared-memory segment")
        except Exception:
            pass

    # ------------------------------ tick ------------------------------
    def _tick(self):
        try:
            while True:
                self._append_log(self._log_q.get_nowait())
        except queue.Empty:
            pass

        if (self.host_proc is not None and self.host_proc.poll() is not None
                and not self._stop_requested):
            code = self.host_proc.returncode
            self._log(f"[manager2] host exited (code {code}). Press Start to retry.")
            self._stop_requested = True
            self.after(50, self._force_kill)

        self.after(150, self._tick)

    # ----------------------------- close ------------------------------
    def on_close(self):
        self._stop_requested = True
        for proc in (self.monitor_proc, self.host_proc):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        for proc in (self.monitor_proc, self.host_proc):
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
    try:
        LanCamManager().mainloop()
    except Exception:
        # Running under pythonw (no console), so surface fatal errors in a
        # dialog + a log file instead of failing silently.
        import traceback
        tb = traceback.format_exc()
        try:
            with open(os.path.join(HERE, "manager2_error.log"), "w") as fh:
                fh.write(tb)
        except Exception:
            pass
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("LAN Cam Manager crashed", tb)
            root.destroy()
        except Exception:
            pass
        raise
