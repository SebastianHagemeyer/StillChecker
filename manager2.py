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

The native Android app (FlameCam, android/ folder) needs no link at all: it
finds this PC on the LAN by itself and streams with the phone screen off. The
phone-status line shows its battery, temperature and Wi-Fi while it streams.
Stop asks the host to shut down cleanly first, so the phone knows the stop was
intentional and Telegram gets a final "host stopped" line.

It does not touch publish.py / manager.py - this is the self-hosted LAN path,
separate from the VDO.Ninja one.
"""

import os
import sys
import json
import time
import queue
import socket
import threading
import subprocess
import urllib.request

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
STATUS_JSON = os.path.join(HERE, "lancam_status.json")
PLAIN_PORT = 8080           # lancam_host --plain-port (Android app + /shutdown)
RESOLUTIONS = ["960x540", "1280x720", "1920x1080"]
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
        self.geometry("820x600")
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
        ttk.Label(opts, text="Resolution:").grid(row=1, column=0, sticky="w", padx=6, pady=(0, 6))
        self.res_var = tk.StringVar(value="1280x720")
        ttk.Combobox(opts, textvariable=self.res_var, values=RESOLUTIONS, width=11,
                     state="readonly").grid(row=1, column=1, columnspan=2, sticky="w", pady=(0, 6))
        self.telegram_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Telegram alerts (Empire bot)", variable=self.telegram_var).grid(
            row=1, column=3, columnspan=4, sticky="w", padx=(18, 6), pady=(0, 6))

        btns = ttk.Frame(self)
        btns.pack(side=tk.TOP, fill=tk.X, **pad)
        self.start_btn = ttk.Button(btns, text="Start", command=self.start)
        self.start_btn.pack(side=tk.LEFT)
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=6)
        self.status_var = tk.StringVar(value="Status: idle")
        ttk.Label(btns, textvariable=self.status_var).pack(side=tk.LEFT, padx=16)

        phone = ttk.LabelFrame(self, text="Phone")
        phone.pack(side=tk.TOP, fill=tk.X, **pad)
        self.phone_var = tk.StringVar(value="(host not running)")
        ttk.Label(phone, textvariable=self.phone_var, font=("Segoe UI", 10)).pack(
            side=tk.LEFT, padx=8, pady=6)

        link = ttk.LabelFrame(self, text="Android app connects by itself. iPhone / Safari: open this link")
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
        res = self.res_var.get() if self.res_var.get() in RESOLUTIONS else "1280x720"
        width, height = res.split("x")
        self.port_var.set(str(port))
        self.fps_var.set(str(fps))
        self.quality_var.set(f"{quality:g}")

        self._clear_stale_shm()
        self._stop_requested = False

        flags = subprocess.CREATE_NO_WINDOW if IS_WIN else 0
        host_cmd = [sys.executable, "-u", HOST_PY,
                    "--port", str(port), "--fps", str(fps), "--quality", str(quality),
                    "--width", width, "--height", height, "--plain-port", str(PLAIN_PORT)]
        if self.audio_var.get():
            host_cmd.append("--audio")
        if self.telegram_var.get():
            host_cmd.append("--telegram")
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
        self._log("Started. Android: open FlameCam and tap Start (it finds this PC).")
        self._log("iPhone: open " + self.url + " , accept the certificate once, tap 'Start camera'.")
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
    @staticmethod
    def _request_host_shutdown(reason="Stop pressed in LAN Cam Manager"):
        """Ask lancam_host to stop cleanly (tells the phone + sends the final
        Telegram line). Returns True if the host accepted."""
        try:
            q = urllib.request.quote(reason)
            req = urllib.request.Request(
                f"http://127.0.0.1:{PLAIN_PORT}/shutdown?reason={q}", data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=2) as r:
                r.read()
            return True
        except Exception:
            return False

    def stop(self):
        self._stop_requested = True
        if self.monitor_proc and self.monitor_proc.poll() is None:
            self._log("[manager2] stopping monitor...")
            try:
                self.monitor_proc.terminate()
            except Exception:
                pass
        if self.host_proc and self.host_proc.poll() is None:
            self._log("[manager2] asking host to stop cleanly...")
            threading.Thread(target=self._request_host_shutdown, daemon=True).start()
        self._set_status("stopping")
        # The host gets a few seconds to say goodbye; _force_kill handles stragglers.
        self.after(8000, self._force_kill)

    def _force_kill(self):
        for proc in (self.monitor_proc, self.host_proc):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=1)
                except Exception:
                    pass
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

        self._refresh_phone_status()

        if (self.host_proc is not None and self.host_proc.poll() is not None
                and not self._stop_requested):
            code = self.host_proc.returncode
            self._log(f"[manager2] host exited (code {code}). Press Start to retry.")
            self._stop_requested = True
            self.after(50, self._force_kill)

        self.after(150, self._tick)

    # ------------------------- phone status --------------------------
    def _refresh_phone_status(self):
        now = time.time()
        if now - getattr(self, "_phone_ts", 0.0) < 1.0:
            return
        self._phone_ts = now
        if not (self.host_proc and self.host_proc.poll() is None):
            self.phone_var.set("(host not running)")
            return
        try:
            with open(STATUS_JSON, "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            self.phone_var.set("waiting for host...")
            return
        if now - float(st.get("t", 0)) > 10:
            self.phone_var.set("host not responding")
            return
        if not st.get("sockets"):
            self.phone_var.set("not connected (open FlameCam on the phone and tap Start)")
            return
        who = (st.get("model") or "Android phone") + " (app)" if st.get("client") == "android" else "Safari camera"
        bits = [who]
        if st.get("streaming"):
            w, h = (st.get("size") or [0, 0])[:2]
            bits.append(f"streaming {w}x{h} @ {st.get('fps', 0):.0f} fps")
        else:
            bits.append("connected, NO FRAMES")
        tel = st.get("telemetry") or {}
        if tel and now - float(tel.get("rx", 0)) < 30:
            if tel.get("battery") is not None:
                bits.append(f"battery {tel['battery']}%" + (" charging" if tel.get("charging") else " on battery"))
            if tel.get("battery_temp_c") is not None:
                bits.append(f"{tel['battery_temp_c']:.1f}C")
            if tel.get("wifi_rssi") is not None:
                bits.append(f"Wi-Fi {tel['wifi_rssi']} dBm")
            if tel.get("screen_on") is False:
                bits.append("screen off")
        if st.get("alarm"):
            bits.append("FLAME ALARM")
        self.phone_var.set("  |  ".join(bits))

    # ----------------------------- close ------------------------------
    def on_close(self):
        self._stop_requested = True
        host_alive = self.host_proc and self.host_proc.poll() is None
        if self.monitor_proc and self.monitor_proc.poll() is None:
            try:
                self.monitor_proc.terminate()
            except Exception:
                pass
        if host_alive and self._request_host_shutdown("LAN Cam Manager closed"):
            try:
                self.host_proc.wait(timeout=8)
            except Exception:
                pass
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
