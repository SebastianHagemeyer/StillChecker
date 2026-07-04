import time
import os
import json
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox
from appicon import apply_flame_icon
import cv2

import numpy as np
from multiprocessing import shared_memory
from multiprocessing.resource_tracker import unregister

from PIL import Image, ImageTk

# Plotting (Tkinter-embedded matplotlib)
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg


RATTLE_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rattle_cadence.json")
RATTLE_SNR_LINE = 1.4   # "rattling" activity threshold, drawn on the graph


class RaspiNinjaFrameReader:
    """
    Reads frames from Raspberry.Ninja shared memory framebuffer.

    Expected layout:
      - first 5 bytes: [w_hi, w_lo, h_hi, h_lo, frame_counter]
      - then width*height*3 bytes: BGR image data
    """
    def __init__(self, shm_name: str):
        self.shm_name = shm_name
        self.shm = None
        self.buf = None
        self.width = None
        self.height = None

    def open(self):
        self.shm = shared_memory.SharedMemory(name=self.shm_name)
        try:
            unregister(self.shm._name, "shared_memory")  # prevent warnings on exit (POSIX only)
        except Exception:
            pass  # resource_tracker is POSIX-only; harmless to skip on native Windows
        self.buf = np.ndarray(self.shm.size, dtype=np.uint8, buffer=self.shm.buf)

    def close(self):
        if self.shm is not None:
            try:
                self.shm.close()
            except Exception:
                pass
        self.shm = None
        self.buf = None

    def read_latest(self, max_tries=6):
        """
        Returns (frame_bgr, frame_id, w, h) or (None, None, None, None).
        Tries to avoid tearing by ensuring meta is stable across the copy.
        """
        if self.shm is None:
            self.open()

        for _ in range(max_tries):
            m1 = self.buf[0:5].copy()
            w1 = int(m1[0]) * 255 + int(m1[1])
            h1 = int(m1[2]) * 255 + int(m1[3])
            fid1 = int(m1[4])

            if w1 <= 0 or h1 <= 0:
                time.sleep(0.001)
                continue

            nbytes = w1 * h1 * 3
            start = 5
            end = start + nbytes
            if end > self.buf.size:
                time.sleep(0.001)
                continue

            pix = self.buf[start:end].copy()

            m2 = self.buf[0:5].copy()
            w2 = int(m2[0]) * 255 + int(m2[1])
            h2 = int(m2[2]) * 255 + int(m2[3])
            fid2 = int(m2[4])

            if (w1, h1, fid1) != (w2, h2, fid2):
                time.sleep(0.001)
                continue

            img = pix.reshape((h1, w1, 3))
            self.width, self.height = w1, h1
            return img, fid1, w1, h1

        return None, None, None, None


class ChangePlotGUI(tk.Tk):
    """
    - Computes frame-to-frame change (mean absolute diff in grayscale).
    - Only computes/stores change when a NEW frame_id arrives.
    - Beeps if change stays below threshold for N seconds.
    - Stream stall watchdog (repeating beeps).
    - ROI selection on video.
    - Pop-out plot window (uses a second FigureCanvasTkAgg so it keeps updating).
    """
    def __init__(
        self,
        shm_name_default="psm_raspininja_streamid",
        update_ms=250,
        history_seconds=60,
        change_threshold=2.0,
        stable_seconds_to_alarm=5.0,
    ):
        super().__init__()
        apply_flame_icon(self)
        self.title("Raspberry.Ninja → Frame Change Plotter (ROI optional)")
        self.geometry("1200x800")

        # Stream stall watchdog
        self.last_frame_ts = None
        self.stream_stall_s = 30
        self.stream_alarm_active = False
        self.stall_beep_every_s = 1.0
        self.stall_last_beep_ts = 0.0

        self.update_ms = update_ms
        self.history_seconds = history_seconds

        # Alarm config
        self.change_threshold_var = tk.DoubleVar(value=float(change_threshold))
        self.stable_seconds_to_alarm = float(stable_seconds_to_alarm)

        # Reader
        self.reader = None
        self.shm_name = tk.StringVar(value=shm_name_default)

        # Video toggle
        self.show_video_var = tk.BooleanVar(value=True)

        # Frame state
        self.frame_bgr = None
        self.frame_id = None
        self.tk_img = None
        self.img_item_id = None

        # New-frame gating
        self.last_seen_frame_id = None
        # Last *distinct* frame (raw BGR) used to reject duplicate frames
        self.last_distinct_bgr = None

        # Change detection state
        self.prev_gray = None
        self.change_history_t = []
        self.change_history_v = []

        # "No-change" alarm state
        self.stable_start_ts = None
        self.last_beep_ts = 0.0
        self.beep_cooldown_s = 1.0
        self.no_change_alarm_active = False

        # ROI state
        self.roi = None
        self.roi_rect_id = None
        self.drag_start = None

        # Display mapping
        self._disp_scale = None
        self._disp_size = (0, 0)

        # ---- RATTLE (audio cadence) state ----
        self.rattle_history_t = []
        self.rattle_history_v = []
        self._rattle_mtime = None
        self._rattle_last_on_ts = None
        self._rattle_stopped_alerted = False
        self.rattle_stop_seconds = 45


         # ---- INDICATOR POP-OUT ----
        self.indicator_popup = None
        self.indicator_label = None

        # How often "red beep" is allowed (reuse your cooldown if you want)
        self.indicator_size_px = 70

        # ---- COOK TIMER ----
        # cook_start_ts is the epoch seconds of the active cook, or None when idle.
        # Persisted to JSON so the timer keeps running across program restarts.
        self.cook_start_ts = None
        self.cook_history = []

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Restore any cook that was still running when the program was last closed.
        self._load_cook_state()

        self.after(0, self.update_loop)

        # Opt-in hands-off connect (set by manager2). Backwards compatible:
        # without RN_AUTOCONNECT in the environment, Connect stays manual.
        _auto = os.environ.get("RN_AUTOCONNECT")
        if _auto:
            if _auto not in ("1", "true", "True", "yes"):
                self.shm_name.set(_auto)
            self.after(400, self._autoconnect_tick)

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(side=tk.TOP, fill=tk.X, padx=10, pady=10)

        row1 = ttk.Frame(top)
        row1.pack(side=tk.TOP, fill=tk.X)

        row2 = ttk.Frame(top)
        row2.pack(side=tk.TOP, fill=tk.X, pady=(6, 0))

        # ----- Row 1: connection + sampling + alarm inputs -----
        ttk.Label(row1, text="Shared memory name:").pack(side=tk.LEFT)
        ttk.Entry(row1, textvariable=self.shm_name, width=34).pack(side=tk.LEFT, padx=8)
        ttk.Button(row1, text="Connect", command=self.connect).pack(side=tk.LEFT, padx=4)
        ttk.Button(row1, text="Disconnect", command=self.disconnect).pack(side=tk.LEFT, padx=4)

        ttk.Label(row1, text="Update (ms):").pack(side=tk.LEFT, padx=(20, 4))
        self.update_ms_var = tk.IntVar(value=self.update_ms)
        ttk.Entry(row1, textvariable=self.update_ms_var, width=8).pack(side=tk.LEFT)
        ttk.Button(row1, text="Apply", command=self.apply_update_rate).pack(side=tk.LEFT, padx=4)

        ttk.Label(row1, text="History (s):").pack(side=tk.LEFT, padx=(20, 4))
        self.history_s_var = tk.IntVar(value=self.history_seconds)
        ttk.Entry(row1, textvariable=self.history_s_var, width=8).pack(side=tk.LEFT)
        ttk.Button(row1, text="Apply", command=self.apply_history_window).pack(side=tk.LEFT, padx=4)

        ttk.Checkbutton(
            row1, text="Show video", variable=self.show_video_var, command=self.on_toggle_video
        ).pack(side=tk.LEFT, padx=(20, 0))

        ttk.Label(row1, text="No-change threshold:").pack(side=tk.LEFT, padx=(20, 4))
        ttk.Entry(row1, textvariable=self.change_threshold_var, width=8).pack(side=tk.LEFT)

        ttk.Label(row1, text="Alarm after (s):").pack(side=tk.LEFT, padx=(12, 4))
        self.alarm_seconds_var = tk.DoubleVar(value=float(self.stable_seconds_to_alarm))
        ttk.Entry(row1, textvariable=self.alarm_seconds_var, width=6).pack(side=tk.LEFT)

        ttk.Button(row1, text="Apply alarm", command=self.apply_alarm_settings).pack(side=tk.LEFT, padx=6)

        # ----- Row 2: actions -----
        ttk.Button(row2, text="Clear plot", command=self.clear_plot).pack(side=tk.LEFT, padx=4)
        ttk.Button(row2, text="Clear ROI", command=self.clear_roi).pack(side=tk.LEFT, padx=4)
        ttk.Button(row2, text="Indicator", command=self.toggle_indicator).pack(side=tk.LEFT, padx=4)

        # ----- Row 3: cook timer -----
        row3 = ttk.Frame(top)
        row3.pack(side=tk.TOP, fill=tk.X, pady=(6, 0))

        self.start_cook_btn = ttk.Button(row3, text="Start cooking", command=self.start_cooking)
        self.start_cook_btn.pack(side=tk.LEFT, padx=4)

        self.finish_cook_btn = ttk.Button(row3, text="Finished cooking", command=self.finish_cooking)
        self.finish_cook_btn.pack(side=tk.LEFT, padx=4)

        self.cook_var = tk.StringVar(value="Cook: idle")
        ttk.Label(row3, textvariable=self.cook_var).pack(side=tk.LEFT, padx=12)

        stats = ttk.Frame(self)
        stats.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 10))
        self.status_var = tk.StringVar(value="Status: not connected")
        ttk.Label(stats, textvariable=self.status_var).pack(side=tk.LEFT)

        self.metric_var = tk.StringVar(value="Change: —")
        ttk.Label(stats, textvariable=self.metric_var).pack(side=tk.LEFT, padx=20)

        self.roi_var = tk.StringVar(value="ROI: none (whole frame)")
        ttk.Label(stats, textvariable=self.roi_var).pack(side=tk.LEFT, padx=20)

        self.alarm_var = tk.StringVar(
            value=f"Alarm: beeps if change < threshold for {self.stable_seconds_to_alarm:.0f}s"
        )
        ttk.Label(stats, textvariable=self.alarm_var).pack(side=tk.LEFT, padx=20)

        self.rattle_var = tk.StringVar(value="Rattle: (audio off)")
        ttk.Label(stats, textvariable=self.rattle_var).pack(side=tk.LEFT, padx=20)

        main = ttk.Frame(self)
        main.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.left = ttk.Frame(main)
        self.left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.right = ttk.Frame(main)
        self.right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        ttk.Label(self.left, text="Video (drag to select ROI):").pack(side=tk.TOP, anchor="w")
        self.canvas = tk.Canvas(self.left, bg="#111", highlightthickness=0)
        self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(6, 0))

        self.canvas.bind("<ButtonPress-1>", self.on_mouse_down)
        self.canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_mouse_up)

        ttk.Label(self.right, text="Change (top) and rattle rate (bottom):").pack(
            side=tk.TOP, anchor="w"
        )
        self.fig = Figure(figsize=(5, 5), dpi=100)
        self.ax = self.fig.add_subplot(211)
        self.ax.set_ylabel("Change (0..255)")
        self.ax.grid(True)
        self.line, = self.ax.plot([], [])

        self.ax_rattle = self.fig.add_subplot(212)
        self.ax_rattle.set_xlabel("Time (s ago)")
        self.ax_rattle.set_ylabel("Rattle activity (×floor)")
        self.ax_rattle.grid(True)
        self.ax_rattle.axhline(RATTLE_SNR_LINE, color="#e5484d", ls="--", lw=0.8)
        self.line_rattle, = self.ax_rattle.plot([], [], color="#7C3AED")

        self.fig.subplots_adjust(hspace=0.35, left=0.16, right=0.97, top=0.97, bottom=0.1)
        self.plot_canvas = FigureCanvasTkAgg(self.fig, master=self.right)
        self.plot_canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(6, 0))

    def apply_update_rate(self):
        try:
            v = int(self.update_ms_var.get())
            if v < 50:
                v = 50
            self.update_ms = v
            self.status_var.set(f"Status: update interval set to {self.update_ms} ms")
        except Exception:
            messagebox.showerror("Invalid", "Update rate must be an integer (ms).")

    def apply_history_window(self):
        try:
            v = int(self.history_s_var.get())
            if v < 5:
                v = 5
            self.history_seconds = v
            self.status_var.set(f"Status: history window set to {self.history_seconds} seconds")
        except Exception:
            messagebox.showerror("Invalid", "History window must be an integer (seconds).")

    def apply_alarm_settings(self):
        try:
            thr = float(self.change_threshold_var.get())
            secs = float(self.alarm_seconds_var.get())
            if thr < 0:
                thr = 0.0
            if secs < 0.5:
                secs = 0.5

            self.change_threshold_var.set(thr)
            self.stable_seconds_to_alarm = secs
            self.alarm_var.set(f"Alarm: beeps if change < threshold for {self.stable_seconds_to_alarm:.1f}s")
            self.status_var.set(f"Status: alarm updated (threshold={thr:g}, seconds={secs:g})")
        except Exception:
            messagebox.showerror("Invalid", "Threshold and seconds must be numbers.")

    def on_toggle_video(self):
        if not self.show_video_var.get():
            self.canvas.delete("IMG")
            self.img_item_id = None
            if self.roi_rect_id is not None:
                self.canvas.delete(self.roi_rect_id)
                self.roi_rect_id = None


    # ================= INDICATOR POP-OUT =================

    def toggle_indicator(self):
        if self.indicator_popup is not None and self.indicator_popup.winfo_exists():
            self.indicator_popup.destroy()
            self.indicator_popup = None
            self.indicator_label = None
            return

        self.indicator_popup = tk.Toplevel(self)
        # Borderless floating dot (no title bar, not in the taskbar).
        try:
            self.indicator_popup.overrideredirect(True)
        except Exception:
            pass
        s = int(self.indicator_size_px)
        self.indicator_popup.geometry(f"{s}x{s}+40+40")
        self.indicator_popup.resizable(False, False)

        # Stay above everything, and keep re-asserting it below.
        try:
            self.indicator_popup.attributes("-topmost", True)
        except Exception:
            pass

        # A single square whose bg we change to show state.
        self.indicator_label = tk.Label(self.indicator_popup, bg="#777777", bd=6,
                                        relief="raised", cursor="fleur")
        self.indicator_label.pack(fill="both", expand=True)

        # Drag from anywhere on the dot to move it; double-click raises the main
        # window; right-click hides the dot (there is no title bar to close it).
        self._ind_off_x = 0
        self._ind_off_y = 0
        self.indicator_label.bind("<Button-1>", self._ind_press)
        self.indicator_label.bind("<B1-Motion>", self._ind_motion)
        self.indicator_label.bind("<Double-Button-1>", lambda e: self.lift())
        self.indicator_label.bind("<Button-3>", lambda e: self.toggle_indicator())

        self.indicator_popup.update_idletasks()
        self.indicator_popup.lift()
        self._ind_keep_top()

    def _ind_press(self, e):
        # Remember where on the dot we grabbed it.
        self._ind_off_x = e.x
        self._ind_off_y = e.y

    def _ind_motion(self, e):
        if self.indicator_popup is None or not self.indicator_popup.winfo_exists():
            return
        x = self.indicator_popup.winfo_pointerx() - self._ind_off_x
        y = self.indicator_popup.winfo_pointery() - self._ind_off_y
        self.indicator_popup.geometry(f"+{x}+{y}")

    def _ind_keep_top(self):
        # Re-assert topmost on a timer so nothing can bury the dot. Self-cancels
        # when the popup is closed.
        if self.indicator_popup is None or not self.indicator_popup.winfo_exists():
            return
        try:
            self.indicator_popup.attributes("-topmost", True)
        except Exception:
            pass
        self.after(1500, self._ind_keep_top)

    def _set_indicator_colour(self, state: str):
        # state: "grey" | "red" | "green"
        if self.indicator_label is None or not self.indicator_label.winfo_exists():
            return
        col = {
            "grey":  "#777777",
            "red":   "#cc0000",
            "green": "#00cc44",
        }.get(state, "#777777")
        self.indicator_label.configure(bg=col)

    def _update_indicator_from_state(self, stalled: bool, alarm_active: bool):
        # Priority: stalled overrides everything
        if stalled:
            self._set_indicator_colour("grey")
        elif alarm_active:
            self._set_indicator_colour("red")
        else:
            self._set_indicator_colour("green")


    def clear_plot(self):
        self.change_history_t.clear()
        self.change_history_v.clear()
        self.rattle_history_t.clear()
        self.rattle_history_v.clear()
        self.metric_var.set("Change: —")
        self.stable_start_ts = None
        self._redraw_plot()

    def clear_roi(self):
        self.roi = None
        self.roi_var.set("ROI: none (whole frame)")
        if self.roi_rect_id is not None:
            self.canvas.delete(self.roi_rect_id)
            self.roi_rect_id = None

    # ================= COOK TIMER =================

    def _cook_json_path(self):
        # Keep the state file next to this script so it is found regardless of cwd.
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "cook_timer.json")

    @staticmethod
    def _fmt_duration(seconds):
        seconds = int(max(0, seconds))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h:d}:{m:02d}:{s:02d}"

    def _save_cook_state(self):
        active = self.cook_start_ts is not None
        data = {
            "active": active,
            "start": self.cook_start_ts,
            "start_iso": (
                datetime.fromtimestamp(self.cook_start_ts).isoformat(timespec="seconds")
                if active else None
            ),
            "history": self.cook_history,
        }
        try:
            with open(self._cook_json_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.status_var.set(f"Status: could not save cook timer: {e}")

    def _load_cook_state(self):
        path = self._cook_json_path()
        if not os.path.exists(path):
            self._refresh_cook_ui()
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.cook_history = data.get("history", []) or []
            if data.get("active") and data.get("start"):
                self.cook_start_ts = float(data["start"])
        except Exception as e:
            self.status_var.set(f"Status: could not load cook timer: {e}")
        self._refresh_cook_ui()

    def start_cooking(self):
        if self.cook_start_ts is not None:
            messagebox.showinfo(
                "Cooking in progress",
                "A cook is already running.\nPress 'Finished cooking' before starting a new one.",
            )
            return
        self.cook_start_ts = time.time()
        self._save_cook_state()
        self._refresh_cook_ui()

    def finish_cooking(self):
        if self.cook_start_ts is None:
            messagebox.showinfo("No cook running", "Press 'Start cooking' first.")
            return
        end = time.time()
        start = self.cook_start_ts
        duration = end - start
        self.cook_history.append({
            "start": start,
            "start_iso": datetime.fromtimestamp(start).isoformat(timespec="seconds"),
            "end": end,
            "end_iso": datetime.fromtimestamp(end).isoformat(timespec="seconds"),
            "duration_s": round(duration, 1),
            "duration_hms": self._fmt_duration(duration),
        })
        self.cook_start_ts = None
        self._save_cook_state()
        self._refresh_cook_ui()
        messagebox.showinfo("Cook finished", f"Total cooking time: {self._fmt_duration(duration)}")

    def _refresh_cook_ui(self):
        # Enable/disable buttons so a new cook can't start until this one is finished.
        cooking = self.cook_start_ts is not None
        self.start_cook_btn.config(state=("disabled" if cooking else "normal"))
        self.finish_cook_btn.config(state=("normal" if cooking else "disabled"))
        self._update_cook_label()

    def _update_cook_label(self):
        if self.cook_start_ts is None:
            self.cook_var.set("Cook: idle")
        else:
            elapsed = time.time() - self.cook_start_ts
            self.cook_var.set(f"Cook: running {self._fmt_duration(elapsed)}")

    def connect(self, silent=False):
        self.disconnect()
        name = self.shm_name.get().strip()
        if not name:
            if not silent:
                messagebox.showerror("Missing", "Shared memory name is required.")
            return False
        try:
            self.reader = RaspiNinjaFrameReader(name)
            self.reader.open()
            self.prev_gray = None
            self.last_seen_frame_id = None
            self.last_distinct_bgr = None
            self.stable_start_ts = None
            self.last_frame_ts = None
            self.stream_alarm_active = False
            self.stall_last_beep_ts = 0.0
            self.status_var.set(f"Status: connected to {name}")
            return True
        except Exception as e:
            self.reader = None
            if not silent:
                messagebox.showerror("Connect failed", f"Could not open shared memory:\n{name}\n\n{e}")
            return False

    def _autoconnect_tick(self):
        # Opt-in (RN_AUTOCONNECT): quietly retry until the shared memory exists,
        # so a launcher can bring the monitor up fully hands-off.
        if self.reader is None and not self.connect(silent=True):
            self.after(1000, self._autoconnect_tick)

    def disconnect(self):
        if self.reader is not None:
            try:
                self.reader.close()
            except Exception:
                pass
        self.reader = None
        self.prev_gray = None
        self.last_seen_frame_id = None
        self.last_distinct_bgr = None
        self.stable_start_ts = None
        self.last_frame_ts = None
        self.stream_alarm_active = False
        self.stall_last_beep_ts = 0.0

        self._set_indicator_colour("grey")

        self.canvas.delete("IMG")
        self.img_item_id = None

        self.status_var.set("Status: not connected")

    def update_loop(self):
        self.after(self.update_ms, self.update_loop)

        # Keep the cook timer ticking regardless of stream/connection state.
        self._update_cook_label()
        self._poll_rattle()

        if self.reader is None:
            return

        now = time.time()

        try:
            frame_bgr, frame_id, w, h = self.reader.read_latest()
        except FileNotFoundError:
            self.status_var.set("Status: shared memory not found (is publish.py running?)")
            frame_bgr = None
        except Exception as e:
            self.status_var.set(f"Status: read error: {e}")
            frame_bgr = None

        # --- STALL WATCHDOG: run EVERY tick (repeat-beep while stalled) ---
        stalled = (self.last_frame_ts is not None) and ((now - self.last_frame_ts) >= self.stream_stall_s)
        self._update_indicator_from_state(stalled=stalled, alarm_active=self.no_change_alarm_active)
        if self.last_frame_ts is not None:
            stalled_for = now - self.last_frame_ts
            if stalled_for >= self.stream_stall_s:
                if (now - self.stall_last_beep_ts) >= self.stall_beep_every_s:
                    self.stall_last_beep_ts = now
                    self.bell()
                self.stream_alarm_active = True
                self.alarm_var.set(f"STREAM STALLED: no new frames for {stalled_for:.1f}s")
        # ------------------------------------------------------------------

        if frame_bgr is None:
            return

        self.status_var.set(f"Status: frame {frame_id} | {w}x{h}")

        is_new = (self.last_seen_frame_id is None) or (frame_id != self.last_seen_frame_id)
        if not is_new:
            if self.show_video_var.get() and self.frame_bgr is not None:
                self._draw_video(self.frame_bgr)
            return

        # New frame arrived: update last_frame_ts here (and clear stall alarm)
        self.last_seen_frame_id = frame_id
        self.last_frame_ts = now
        self.frame_bgr = frame_bgr
        self.stream_alarm_active = False
        self.stall_last_beep_ts = 0.0
        self.no_change_alarm_active = False
        self.alarm_var.set(f"Alarm: beeps if change < threshold for {self.stable_seconds_to_alarm:.0f}s")

        # Duplicate-frame guard: at low framerates the capture pipeline can hand us the
        # same decoded frame again with a fresh counter. Two identical frames produce a
        # fake 0.0 difference that real encoding noise never does, so skip duplicates and
        # keep comparing the last *distinct* frame with the next *distinct* one.
        if (self.last_distinct_bgr is not None
                and frame_bgr.shape == self.last_distinct_bgr.shape
                and np.array_equal(frame_bgr, self.last_distinct_bgr)):
            self.status_var.set(f"Status: frame {frame_id} | {w}x{h} (duplicate, skipped)")
            if self.show_video_var.get():
                self._draw_video(frame_bgr)
            return
        self.last_distinct_bgr = frame_bgr

       # Compute change vs previous frame (COLOUR chroma in Lab a/b)
        lab = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2LAB)
        lab = cv2.GaussianBlur(lab, (5, 5), 0)

        change_val = None
        prev_lab = getattr(self, "prev_lab", None)

        if prev_lab is not None and prev_lab.shape == lab.shape:
            H, W = lab.shape[:2]

            if self.roi is not None:
                x1, y1, x2, y2 = self.roi

                x1 = max(0, min(int(x1), W - 1))
                x2 = max(0, min(int(x2), W))
                y1 = max(0, min(int(y1), H - 1))
                y2 = max(0, min(int(y2), H))

                if (x2 - x1) >= 2 and (y2 - y1) >= 2:
                    cur = lab[y1:y2, x1:x2]
                    prev = prev_lab[y1:y2, x1:x2]
                else:
                    cur = lab
                    prev = prev_lab
            else:
                cur = lab
                prev = prev_lab

            # Chroma-only diff catches colour shifts that grayscale misses
            da = cv2.absdiff(cur[:, :, 1], prev[:, :, 1])  # a channel
            db = cv2.absdiff(cur[:, :, 2], prev[:, :, 2])  # b channel
            d = cv2.max(da, db)  # per-pixel chroma change (0..255)

            # Suppress tiny noise
            d = d[d >= 3]        # tune 2..8 depending on codec noise
            change_val = float(d.mean()) if d.size else 0.0

        # Store previous for next frame
        self.prev_lab = lab

        

        if change_val is not None:
            self.metric_var.set(f"Change: {change_val:.3f}")
            tnow = time.time()
            self.change_history_t.append(tnow)
            self.change_history_v.append(change_val)
            self._trim_history(tnow)
            self._redraw_plot()
            self._update_no_change_alarm(change_val, tnow)

        if self.show_video_var.get():
            self._draw_video(frame_bgr)

    def _update_no_change_alarm(self, change_val: float, now: float):
        thr = float(self.change_threshold_var.get())

        if change_val < thr:
            if self.stable_start_ts is None:
                self.stable_start_ts = now

            stable_for = now - self.stable_start_ts

            if stable_for >= self.stable_seconds_to_alarm:
                self.no_change_alarm_active = True

                if (now - self.last_beep_ts) >= self.beep_cooldown_s:
                    self.last_beep_ts = now
                    self.bell()
                    self.alarm_var.set(f"Alarm: NO CHANGE for {stable_for:.1f}s (< {thr:g})")
            else:
                # below threshold but not long enough to alarm yet
                self.no_change_alarm_active = False
        else:
            self.stable_start_ts = None
            self.no_change_alarm_active = False
            self.alarm_var.set(f"Alarm: beeps if change < threshold for {self.stable_seconds_to_alarm:.0f}s")

    def _trim_history(self, now):
        cutoff = now - self.history_seconds
        while self.change_history_t and self.change_history_t[0] < cutoff:
            self.change_history_t.pop(0)
            self.change_history_v.pop(0)

    def _redraw_plot(self):
        if not self.change_history_t:
            self.line.set_data([], [])
            self.ax.set_xlim(0, self.history_seconds)
            self.ax.set_ylim(0, 255)
            self.plot_canvas.draw_idle()
            return

        now = time.time()
        xs = [(now - t) for t in self.change_history_t]
        ys = self.change_history_v

        self.line.set_data(xs, ys)
        self.ax.set_xlim(self.history_seconds, 0)

        y_max = max(5.0, float(max(ys)) * 1.2)
        self.ax.set_ylim(0, min(255.0, y_max))

        self.plot_canvas.draw_idle()

    def _poll_rattle(self):
        """Read rattle_cadence.json (written ~1/s by lancam_host --audio) and
        update the rattle readout + graph. No-op when audio isn't running."""
        try:
            mtime = os.path.getmtime(RATTLE_JSON)
        except OSError:
            if not self.rattle_history_t:
                self.rattle_var.set("Rattle: (audio off)")
            return
        if mtime == self._rattle_mtime:
            return
        self._rattle_mtime = mtime
        try:
            with open(RATTLE_JSON) as f:
                data = json.load(f)
        except Exception:
            return
        now = time.time()
        snr = float(data.get("snr", 0.0))
        rattling = bool(data.get("rattling"))
        if rattling:
            pm = float(data.get("per_min", 0.0))
            conf = float(data.get("confidence", 0.0))
            if conf >= 0.2 and pm > 0:
                self.rattle_var.set(f"Rattle: ON  (x{snr:.1f}, ~{pm:.0f}/min)")
            else:
                self.rattle_var.set(f"Rattle: ON  (x{snr:.1f})")
            self._rattle_last_on_ts = now
            self._rattle_stopped_alerted = False
        elif "snr" in data:
            self.rattle_var.set(f"Rattle: quiet  (x{snr:.1f})")
        else:
            self.rattle_var.set(f"Rattle: {data.get('reason', '-')}")
        self.rattle_history_t.append(now)
        self.rattle_history_v.append(snr)
        cutoff = now - self.history_seconds
        while self.rattle_history_t and self.rattle_history_t[0] < cutoff:
            self.rattle_history_t.pop(0)
            self.rattle_history_v.pop(0)
        self._check_rattle_stopped(now, rattling)
        self._redraw_rattle()

    def _redraw_rattle(self):
        now = time.time()
        if not self.rattle_history_t:
            self.line_rattle.set_data([], [])
        else:
            xs = [(now - t) for t in self.rattle_history_t]
            self.line_rattle.set_data(xs, self.rattle_history_v)
        self.ax_rattle.set_xlim(self.history_seconds, 0)
        y_max = max(3.0, (float(max(self.rattle_history_v)) * 1.2) if self.rattle_history_v else 3.0)
        self.ax_rattle.set_ylim(0, y_max)
        self.plot_canvas.draw_idle()

    def _check_rattle_stopped(self, now, rattling):
        # Beep once (and keep showing it) if the rattle was going and then stops.
        if rattling or self._rattle_last_on_ts is None:
            return
        elapsed = now - self._rattle_last_on_ts
        if elapsed >= self.rattle_stop_seconds:
            self.rattle_var.set(f"Rattle: STOPPED for {elapsed:.0f}s")
            if not self._rattle_stopped_alerted:
                self._rattle_stopped_alerted = True
                try:
                    self.bell()
                except Exception:
                    pass

    def _draw_video(self, frame_bgr):
        frame_rgb = frame_bgr[:, :, ::-1]
        img = Image.fromarray(frame_rgb)

        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        iw, ih = img.size

        scale = min(cw / iw, ch / ih)
        new_w = max(1, int(iw * scale))
        new_h = max(1, int(ih * scale))
        img_disp = img.resize((new_w, new_h), Image.BILINEAR)

        self.tk_img = ImageTk.PhotoImage(img_disp)

        if self.img_item_id is None:
            self.img_item_id = self.canvas.create_image(0, 0, anchor="nw", image=self.tk_img, tags="IMG")
        else:
            self.canvas.itemconfig(self.img_item_id, image=self.tk_img)

        self._disp_scale = scale
        self._disp_size = (new_w, new_h)

        # Keep the green ROI box aligned with the (re)scaled video. The ROI is
        # stored in image coords, so re-project it every draw - this is what
        # makes it follow the video when the window is resized.
        if self.roi is not None and self.roi_rect_id is not None:
            x_min, y_min, x_max, y_max = self.roi
            self.canvas.coords(
                self.roi_rect_id,
                x_min * scale, y_min * scale,
                x_max * scale, y_max * scale,
            )

        if self.roi_rect_id is not None:
            self.canvas.tag_raise(self.roi_rect_id)

    @staticmethod
    def _bgr_to_gray_u8(bgr):
        b = bgr[:, :, 0].astype(np.uint16)
        g = bgr[:, :, 1].astype(np.uint16)
        r = bgr[:, :, 2].astype(np.uint16)
        gray = (29 * b + 150 * g + 77 * r) >> 8
        return gray.astype(np.uint8)

    def canvas_to_image_coords(self, x, y):
        if self.frame_bgr is None:
            return None
        scale = self._disp_scale
        if not scale or scale <= 0:
            return None

        dw, dh = self._disp_size
        x = min(max(x, 0), dw - 1)
        y = min(max(y, 0), dh - 1)

        ix = int(x / scale)
        iy = int(y / scale)

        h, w, _ = self.frame_bgr.shape
        ix = min(max(ix, 0), w - 1)
        iy = min(max(iy, 0), h - 1)
        return ix, iy

    def on_mouse_down(self, event):
        if not self.show_video_var.get():
            return
        if self.frame_bgr is None:
            return

        self.drag_start = (event.x, event.y)
        if self.roi_rect_id is None:
            self.roi_rect_id = self.canvas.create_rectangle(
                event.x, event.y, event.x, event.y, outline="#00ff00", width=2
            )
        else:
            self.canvas.coords(self.roi_rect_id, event.x, event.y, event.x, event.y)

    def on_mouse_drag(self, event):
        if not self.show_video_var.get():
            return
        if self.drag_start is None or self.roi_rect_id is None:
            return
        x0, y0 = self.drag_start
        self.canvas.coords(self.roi_rect_id, x0, y0, event.x, event.y)

    def on_mouse_up(self, event):
        if not self.show_video_var.get():
            return
        if self.drag_start is None or self.roi_rect_id is None:
            return

        x0, y0 = self.drag_start
        x1, y1 = event.x, event.y
        self.drag_start = None

        p0 = self.canvas_to_image_coords(x0, y0)
        p1 = self.canvas_to_image_coords(x1, y1)
        if p0 is None or p1 is None:
            return

        ix0, iy0 = p0
        ix1, iy1 = p1
        x_min, x_max = sorted([ix0, ix1])
        y_min, y_max = sorted([iy0, iy1])

        if (x_max - x_min) < 2 or (y_max - y_min) < 2:
            self.clear_roi()
            return

        self.roi = (x_min, y_min, x_max, y_max)
        self.roi_var.set(f"ROI: ({x_min},{y_min})→({x_max},{y_max})")

    def on_close(self):
        self.disconnect()
        self.destroy()


if __name__ == "__main__":
    app = ChangePlotGUI(shm_name_default="psm_raspininja_streamid", update_ms=250, history_seconds=60)
    app.mainloop()