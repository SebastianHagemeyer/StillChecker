#AcUnJQm

# python3 publish.py --view AcUnJQm --framebuffer stream 

# python3 publish.py --view AcUnJQm --record out.webm


# python3 publish.py --framebuffer yLMwvzc


# https://vdo.ninja/?view=yLMwvzc

# source .venv/bin/activate


# unset DISPLAY WAYLAND_DISPLAY
# python3 publish.py --framebuffer yLMwvzc --noaudio

import time
import tkinter as tk
from tkinter import ttk, messagebox

import numpy as np
from multiprocessing import shared_memory
from multiprocessing.resource_tracker import unregister

from PIL import Image, ImageTk


class RaspiNinjaFrameReader:
    """
    Reads frames from Raspberry.Ninja shared memory framebuffer.
    Expected layout (as in your code / basic_recv.py):
      - first 5 bytes: [w_hi, w_lo, h_hi, h_lo, frame_counter]
      - then width*height*3 bytes: BGR image data
    """
    def __init__(self, shm_name: str):
        self.shm_name = shm_name
        self.shm = None
        self.buf = None
        self.last_frame_id = None
        self.width = None
        self.height = None

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

    def read_latest(self):
        """
        Returns (frame_bgr, frame_id) or (None, None) if no new frame yet.
        """
        if self.shm is None:
            self.open()

        # copy out the whole buffer quickly (safe snapshot)
        raw = self.buf.copy()

        meta = raw[0:5]
        w = int(meta[0]) * 255 + int(meta[1])
        h = int(meta[2]) * 255 + int(meta[3])
        frame_id = int(meta[4])

        if w <= 0 or h <= 0:
            return None, None

        # If frame id hasn't changed, you can still return the last one,
        # but for "once per second" UI it's fine to accept same frame too.
        # We'll still parse it.
        nbytes = w * h * 3
        start = 5
        end = start + nbytes
        if end > raw.size:
            return None, None

        img = raw[start:end].reshape((h, w, 3))  # BGR
        self.width, self.height = w, h
        self.last_frame_id = frame_id
        return img, frame_id


class ROIAvgGUI(tk.Tk):
    def __init__(self, shm_name_default="psm_raspininja_streamid", update_ms=1000):
        super().__init__()
        self.title("Raspberry.Ninja → OpenCV/Tk ROI Average Colour")
        self.geometry("1100x750")

        self.update_ms = update_ms

        # Reader
        self.reader = None
        self.shm_name = tk.StringVar(value=shm_name_default)

        # Frame state
        self.frame_bgr = None
        self.frame_id = None
        self.tk_img = None

        # ROI state (canvas coords)
        self.roi_rect_id = None
        self.drag_start = None
        self.roi = None  # (x1,y1,x2,y2) in image coords

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Start periodic updates
        self.after(0, self.update_frame_loop)

    def _build_ui(self):
        # Top controls
        top = ttk.Frame(self)
        top.pack(side=tk.TOP, fill=tk.X, padx=10, pady=10)

        ttk.Label(top, text="Shared memory name:").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.shm_name, width=30).pack(side=tk.LEFT, padx=8)

        ttk.Button(top, text="Connect", command=self.connect).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="Disconnect", command=self.disconnect).pack(side=tk.LEFT, padx=4)

        ttk.Label(top, text="Update (ms):").pack(side=tk.LEFT, padx=(20, 4))
        self.update_ms_var = tk.IntVar(value=self.update_ms)
        ttk.Entry(top, textvariable=self.update_ms_var, width=8).pack(side=tk.LEFT)

        ttk.Button(top, text="Apply", command=self.apply_update_rate).pack(side=tk.LEFT, padx=4)

        # Stats panel
        stats = ttk.Frame(self)
        stats.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 10))

        self.status_var = tk.StringVar(value="Status: not connected")
        ttk.Label(stats, textvariable=self.status_var).pack(side=tk.LEFT)

        self.avg_var = tk.StringVar(value="ROI avg RGB: (—, —, —)")
        ttk.Label(stats, textvariable=self.avg_var).pack(side=tk.LEFT, padx=20)

        # Colour swatch
        self.swatch = tk.Canvas(stats, width=40, height=20, highlightthickness=1, highlightbackground="#999")
        self.swatch.pack(side=tk.LEFT)
        self.swatch_rect = self.swatch.create_rectangle(0, 0, 40, 20, fill="#000000", outline="")

        # Instructions
        instr = ttk.Label(self, text="Drag on the image to select ROI. Average colour updates each refresh.")
        instr.pack(side=tk.TOP, anchor="w", padx=10, pady=(0, 6))

        # Canvas for image
        self.canvas = tk.Canvas(self, bg="#111", highlightthickness=0)
        self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Mouse bindings for ROI select
        self.canvas.bind("<ButtonPress-1>", self.on_mouse_down)
        self.canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_mouse_up)

    def apply_update_rate(self):
        try:
            v = int(self.update_ms_var.get())
            if v < 50:
                v = 50
            self.update_ms = v
            self.status_var.set(f"Status: update interval set to {self.update_ms} ms")
        except Exception:
            messagebox.showerror("Invalid", "Update rate must be an integer (ms).")

    def connect(self):
        self.disconnect()
        name = self.shm_name.get().strip()
        if not name:
            messagebox.showerror("Missing", "Shared memory name is required.")
            return
        try:
            self.reader = RaspiNinjaFrameReader(name)
            self.reader.open()
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
        self.status_var.set("Status: not connected")

    def update_frame_loop(self):
        # Re-schedule first (so UI keeps going even on errors)
        self.after(self.update_ms, self.update_frame_loop)

        if self.reader is None:
            return

        try:
            frame_bgr, frame_id = self.reader.read_latest()
        except FileNotFoundError:
            self.status_var.set("Status: shared memory not found (is publish.py running?)")
            return
        except Exception as e:
            self.status_var.set(f"Status: read error: {e}")
            return

        if frame_bgr is None:
            return

        self.frame_bgr = frame_bgr
        self.frame_id = frame_id

        # Convert BGR -> RGB for display
        frame_rgb = frame_bgr[:, :, ::-1]
        img = Image.fromarray(frame_rgb)

        # Fit to canvas while keeping aspect ratio
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        iw, ih = img.size

        scale = min(cw / iw, ch / ih)
        new_w = max(1, int(iw * scale))
        new_h = max(1, int(ih * scale))
        img_disp = img.resize((new_w, new_h), Image.BILINEAR)

        self.tk_img = ImageTk.PhotoImage(img_disp)
        self.canvas.delete("IMG")
        self.canvas.create_image(0, 0, anchor="nw", image=self.tk_img, tags="IMG")

        # Store display scale for ROI mapping
        self._disp_scale = scale
        self._disp_size = (new_w, new_h)

        # Ensure ROI rect is on top
        if self.roi_rect_id is not None:
            self.canvas.tag_raise(self.roi_rect_id)

        # Update stats
        self.status_var.set(f"Status: frame {frame_id} | {self.reader.width}x{self.reader.height}")

        # If ROI exists, compute average
        if self.roi is not None:
            avg = self.compute_roi_average_rgb()
            if avg is not None:
                r, g, b = avg
                self.avg_var.set(f"ROI avg RGB: ({r:.1f}, {g:.1f}, {b:.1f})")
                hexcol = f"#{int(r):02x}{int(g):02x}{int(b):02x}"
                self.swatch.itemconfig(self.swatch_rect, fill=hexcol)

    def canvas_to_image_coords(self, x, y):
        # Map canvas display coords to original image coords
        if self.frame_bgr is None:
            return None
        scale = getattr(self, "_disp_scale", None)
        if scale is None or scale <= 0:
            return None

        # Clamp within displayed image
        dw, dh = getattr(self, "_disp_size", (0, 0))
        x = min(max(x, 0), dw - 1)
        y = min(max(y, 0), dh - 1)

        ix = int(x / scale)
        iy = int(y / scale)

        # Clamp within source image
        h, w, _ = self.frame_bgr.shape
        ix = min(max(ix, 0), w - 1)
        iy = min(max(iy, 0), h - 1)
        return ix, iy

    def on_mouse_down(self, event):
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
        if self.drag_start is None or self.roi_rect_id is None:
            return
        x0, y0 = self.drag_start
        self.canvas.coords(self.roi_rect_id, x0, y0, event.x, event.y)

    def on_mouse_up(self, event):
        if self.drag_start is None or self.roi_rect_id is None:
            return
        x0, y0 = self.drag_start
        x1, y1 = event.x, event.y
        self.drag_start = None

        # Convert to image coords
        p0 = self.canvas_to_image_coords(x0, y0)
        p1 = self.canvas_to_image_coords(x1, y1)
        if p0 is None or p1 is None:
            return

        ix0, iy0 = p0
        ix1, iy1 = p1
        x_min, x_max = sorted([ix0, ix1])
        y_min, y_max = sorted([iy0, iy1])

        # Avoid zero-size
        if (x_max - x_min) < 2 or (y_max - y_min) < 2:
            self.roi = None
            self.avg_var.set("ROI avg RGB: (—, —, —)")
            self.swatch.itemconfig(self.swatch_rect, fill="#000000")
            return

        self.roi = (x_min, y_min, x_max, y_max)

    def compute_roi_average_rgb(self):
        if self.frame_bgr is None or self.roi is None:
            return None
        x1, y1, x2, y2 = self.roi
        roi_bgr = self.frame_bgr[y1:y2, x1:x2, :]
        if roi_bgr.size == 0:
            return None

        # Average in BGR then convert to RGB
        mean_bgr = roi_bgr.reshape(-1, 3).mean(axis=0)
        b, g, r = mean_bgr
        return (r, g, b)

    def on_close(self):
        self.disconnect()
        self.destroy()


if __name__ == "__main__":
    # IMPORTANT:
    # The shared memory name is usually of the form:
    #   psm_raspininja_<something>
    # If you’re unsure, look at basic_recv.py and copy its shm_name.
    app = ROIAvgGUI(shm_name_default="psm_raspininja_streamid", update_ms=1000)
    app.mainloop()