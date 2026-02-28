import time
import tkinter as tk
from tkinter import ttk, messagebox
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
        self.last_frame_ts = None          # last time a NEW frame arrived
        self.stream_stall_s = 5.0          # seconds before alarm
        self.stream_alarm_active = False   # avoid spam

    def open(self):
        self.shm = shared_memory.SharedMemory(name=self.shm_name)
        unregister(self.shm._name, "shared_memory")  # prevent warnings on exit
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
            # Read meta directly from shared memory (not from a copied snapshot)
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

            # Copy only the pixel region (faster + less likely to race)
            pix = self.buf[start:end].copy()

            # Re-read meta after the pixel copy
            m2 = self.buf[0:5].copy()
            w2 = int(m2[0]) * 255 + int(m2[1])
            h2 = int(m2[2]) * 255 + int(m2[3])
            fid2 = int(m2[4])

            # Accept only if meta stayed stable during copy
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
    - Optional ROI and optional hidden video stream.
    """
    def __init__(
        self,
        shm_name_default="psm_raspininja_streamid",
        update_ms=250,
        history_seconds=60,
        change_threshold=2.0,        # "meaningful change" threshold
        stable_seconds_to_alarm=5.0,  # seconds below threshold before beep
        # Stream stall watchdog
        last_frame_ts = None,
        stream_stall_s = 5.0 ,         # seconds before considering stream stalled
        stream_alarm_active = False
    ):
        super().__init__()
        self.title("Raspberry.Ninja → Frame Change Plotter (ROI optional)")
        self.geometry("1200x800")

        # Stream stall watchdog
        self.last_frame_ts = None
        self.stream_stall_s = 5.0          # seconds before considering stream stalled
        self.stream_alarm_active = False

        self.update_ms = update_ms
        self.history_seconds = history_seconds

        self.stall_beep_every_s = 1.0   # seconds between beeps
        self.stall_last_beep_ts = 0.0

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
        self.img_item_id = None  # <-- add this

        # New-frame gating
        self.last_seen_frame_id = None

        # Change detection state
        self.prev_gray = None
        self.change_history_t = []
        self.change_history_v = []

        # "No-change" alarm state
        self.stable_start_ts = None      # when we entered "below threshold"
        self.last_beep_ts = 0.0          # rate limit beeps
        self.beep_cooldown_s = 1.0       # don't spam bell every UI tick

        # ROI state
        self.roi = None
        self.roi_rect_id = None
        self.drag_start = None

        # Display mapping
        self._disp_scale = None
        self._disp_size = (0, 0)

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.after(0, self.update_loop)

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(side=tk.TOP, fill=tk.X, padx=10, pady=10)

        ttk.Label(top, text="Shared memory name:").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.shm_name, width=34).pack(side=tk.LEFT, padx=8)
        ttk.Button(top, text="Connect", command=self.connect).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="Disconnect", command=self.disconnect).pack(side=tk.LEFT, padx=4)

        ttk.Label(top, text="Update (ms):").pack(side=tk.LEFT, padx=(20, 4))
        self.update_ms_var = tk.IntVar(value=self.update_ms)
        ttk.Entry(top, textvariable=self.update_ms_var, width=8).pack(side=tk.LEFT)
        ttk.Button(top, text="Apply", command=self.apply_update_rate).pack(side=tk.LEFT, padx=4)

        ttk.Label(top, text="History (s):").pack(side=tk.LEFT, padx=(20, 4))
        self.history_s_var = tk.IntVar(value=self.history_seconds)
        ttk.Entry(top, textvariable=self.history_s_var, width=8).pack(side=tk.LEFT)
        ttk.Button(top, text="Apply", command=self.apply_history_window).pack(side=tk.LEFT, padx=4)

        ttk.Checkbutton(top, text="Show video", variable=self.show_video_var, command=self.on_toggle_video).pack(
            side=tk.LEFT, padx=(20, 0)
        )

        ttk.Label(top, text="No-change threshold:").pack(side=tk.LEFT, padx=(20, 4))
        ttk.Entry(top, textvariable=self.change_threshold_var, width=8).pack(side=tk.LEFT)

        ttk.Label(top, text="Alarm after (s):").pack(side=tk.LEFT, padx=(12, 4))
        self.alarm_seconds_var = tk.DoubleVar(value=float(self.stable_seconds_to_alarm))
        ttk.Entry(top, textvariable=self.alarm_seconds_var, width=6).pack(side=tk.LEFT)

        ttk.Button(top, text="Apply alarm", command=self.apply_alarm_settings).pack(side=tk.LEFT, padx=6)


        ttk.Button(top, text="Clear plot", command=self.clear_plot).pack(side=tk.LEFT, padx=(20, 4))
        ttk.Button(top, text="Clear ROI", command=self.clear_roi).pack(side=tk.LEFT, padx=4)

        stats = ttk.Frame(self)
        stats.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 10))
        self.status_var = tk.StringVar(value="Status: not connected")
        ttk.Label(stats, textvariable=self.status_var).pack(side=tk.LEFT)

        self.metric_var = tk.StringVar(value="Change: —")
        ttk.Label(stats, textvariable=self.metric_var).pack(side=tk.LEFT, padx=20)

        self.roi_var = tk.StringVar(value="ROI: none (whole frame)")
        ttk.Label(stats, textvariable=self.roi_var).pack(side=tk.LEFT, padx=20)

        self.alarm_var = tk.StringVar(value=f"Alarm: beeps if change < threshold for {self.stable_seconds_to_alarm:.0f}s")
        ttk.Label(stats, textvariable=self.alarm_var).pack(side=tk.LEFT, padx=20)

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

        ttk.Label(self.right, text="Frame-to-frame change (mean abs diff in grayscale):").pack(side=tk.TOP, anchor="w")
        self.fig = Figure(figsize=(5, 4), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("Time (s ago)")
        self.ax.set_ylabel("Change (0..255)")
        self.ax.grid(True)

        self.line, = self.ax.plot([], [])
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
            self.img_item_id = None  # <-- add this
            if self.roi_rect_id is not None:
                self.canvas.delete(self.roi_rect_id)
                self.roi_rect_id = None

    def clear_plot(self):
        self.change_history_t.clear()
        self.change_history_v.clear()
        self.metric_var.set("Change: —")
        self.stable_start_ts = None
        self._redraw_plot()

    def clear_roi(self):
        self.roi = None
        self.roi_var.set("ROI: none (whole frame)")
        if self.roi_rect_id is not None:
            self.canvas.delete(self.roi_rect_id)
            self.roi_rect_id = None

    def connect(self):
        self.disconnect()
        name = self.shm_name.get().strip()
        if not name:
            messagebox.showerror("Missing", "Shared memory name is required.")
            return
        try:
            self.reader = RaspiNinjaFrameReader(name)
            self.reader.open()
            self.prev_gray = None
            self.last_seen_frame_id = None
            self.stable_start_ts = None
            self.status_var.set(f"Status: connected to {name}")
        except Exception as e:
            self.reader = None
            messagebox.showerror("Connect failed", f"Could not open shared memory:\n{name}\n\n{e}")

    def disconnect(self):
        if self.reader is not None:
            try:
                self.reader.close()
            except Exception:
                pass
        self.reader = None
        self.prev_gray = None
        self.last_seen_frame_id = None
        self.stable_start_ts = None

        self.canvas.delete("IMG")   # optional cleanup
        self.img_item_id = None     # <-- add this

        self.status_var.set("Status: not connected")

    def update_loop(self):
        self.after(self.update_ms, self.update_loop)

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

        # --- STALL WATCHDOG: run EVERY tick ---
        if self.last_frame_ts is not None:
            stalled_for = now - self.last_frame_ts
            if stalled_for >= self.stream_stall_s:
                if (now - self.stall_last_beep_ts) >= self.stall_beep_every_s:
                    self.stall_last_beep_ts = now
                    self.bell()
                self.stream_alarm_active = True
                self.alarm_var.set(f"STREAM STALLED: no new frames for {stalled_for:.1f}s")
        # --------------------------------------

        if frame_bgr is None:
            return

        self.status_var.set(f"Status: frame {frame_id} | {w}x{h}")

        is_new = (self.last_seen_frame_id is None) or (frame_id != self.last_seen_frame_id)
        if not is_new:
            if self.show_video_var.get() and self.frame_bgr is not None:
                self._draw_video(self.frame_bgr)
            return

        # New frame arrived: update last_frame_ts here (and clear alarm)
        self.last_seen_frame_id = frame_id
        self.last_frame_ts = now
        self.frame_bgr = frame_bgr   # <-- add this
        self.stream_alarm_active = False

        self.stall_last_beep_ts = 0.0

        self.alarm_var.set(f"Alarm: beeps if change < threshold for {self.stable_seconds_to_alarm:.0f}s")
        


        # Compute change vs previous frame (grayscale)
        gray = self._bgr_to_gray_u8(frame_bgr)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)


        change_val = None
        if self.prev_gray is not None and self.prev_gray.shape == gray.shape:
            if self.roi is not None:
                x1, y1, x2, y2 = self.roi
                x1 = max(0, min(x1, gray.shape[1] - 1))
                x2 = max(0, min(x2, gray.shape[1]))
                y1 = max(0, min(y1, gray.shape[0] - 1))
                y2 = max(0, min(y2, gray.shape[0]))
                if (x2 - x1) >= 2 and (y2 - y1) >= 2:
                    a = gray[y1:y2, x1:x2].astype(np.int16)
                    b = self.prev_gray[y1:y2, x1:x2].astype(np.int16)
                    #change_val = float(np.abs(a - b).mean())
                    d = np.abs(a - b)
                    d = d[d >= 2]              # per-pixel deadband (tune 2..8)
                    change_val = float(d.mean()) if d.size else 0.0
                else:
                    a = gray.astype(np.int16)
                    b = self.prev_gray.astype(np.int16)
                    d = np.abs(a - b)
                    d = d[d >= 2]              # per-pixel deadband (tune 2..8)
                    change_val = float(d.mean()) if d.size else 0.0
                    #change_val = float(np.abs(a - b).mean())
            else:
                a = gray.astype(np.int16)
                b = self.prev_gray.astype(np.int16)
                change_val = float(np.abs(a - b).mean())


        self.prev_gray = gray

        # Record + plot + alarm logic (only when we have a change value, i.e., after at least 2 frames)
        if change_val is not None:
            self.metric_var.set(f"Change: {change_val:.3f}")
            now = time.time()
            self.change_history_t.append(now)
            self.change_history_v.append(change_val)
            self._trim_history(now)
            self._redraw_plot()

            self._update_no_change_alarm(change_val, now)

        # Draw video if enabled (only on new frames to reduce CPU)
        if self.show_video_var.get():
            self._draw_video(frame_bgr)

    def _update_no_change_alarm(self, change_val: float, now: float):
        """
        Beep if change stays below threshold continuously for stable_seconds_to_alarm.
        """
        thr = float(self.change_threshold_var.get())

        if change_val < thr:
            if self.stable_start_ts is None:
                self.stable_start_ts = now

            stable_for = now - self.stable_start_ts
            if stable_for >= self.stable_seconds_to_alarm:
                # rate limit beeps
                if (now - self.last_beep_ts) >= self.beep_cooldown_s:
                    self.last_beep_ts = now
                    self.bell()  # Tk system beep
                    self.alarm_var.set(f"Alarm: NO CHANGE for {stable_for:.1f}s (< {thr:g})  🔔")
        else:
            # reset stable timer once change is above threshold
            self.stable_start_ts = None
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
        xs = [(now - t) for t in self.change_history_t]  # seconds ago
        ys = self.change_history_v

        self.line.set_data(xs, ys)
        self.ax.set_xlim(self.history_seconds, 0)

        y_max = max(5.0, float(max(ys)) * 1.2)
        self.ax.set_ylim(0, min(255.0, y_max))
        self.plot_canvas.draw_idle()

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

        # Create once; then update in-place (DO NOT delete/recreate each tick)
        if self.img_item_id is None:
            self.img_item_id = self.canvas.create_image(
                0, 0, anchor="nw", image=self.tk_img, tags="IMG"
            )
        else:
            self.canvas.itemconfig(self.img_item_id, image=self.tk_img)

        self._disp_scale = scale
        self._disp_size = (new_w, new_h)

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
                event.x, event.y, event.x, event.y,
                outline="#00ff00", width=2
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