"""
MAC2000 Stage Calibration Tool
===============================
Interactive GUI for calibrating stage steps-per-micron using a
calibration standard (grid slide).

Procedure:
  1. Place calibration grid on stage under microscope
  2. Focus camera on grid
  3. Mark position of a grid line (point A)
  4. Move stage until the NEXT grid line is at the same position (point B)
  5. The step difference / grid spacing = steps per unit length

Usage:
    python calibrate_stage.py
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import time
import json
import os
import sys
import numpy as np
from PIL import Image, ImageTk

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mac2000_driver import MAC2000
from teli_camera import TeliCamera


class StageCalibrator:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("MAC2000 Stage Calibration")
        self.root.geometry("1100x750")
        self.root.configure(bg="#1e1e1e")

        # State
        self.stage = None
        self.camera = None
        self.connected = False
        self.live_running = False
        self.current_img = None
        self._jog_repeating = False  # for continuous jog on hold
        self.zoom_level = 1.0  # 1x = full frame, higher = cropped center
        self._pending_frame = None  # thread-safe frame buffer

        # Calibration points
        self.point_a_x = None  # stage position at point A (X cal)
        self.point_b_x = None
        self.point_a_y = None  # stage position at point A (Y cal)
        self.point_b_y = None

        # Known grid spacings (in mm)
        self.grid_spacings = {
            "2.5 mm square grid": 2.5,
            "1.0 mm square grid": 1.0,
            "0.5 mm grid": 0.5,
            "0.25 mm grid": 0.25,
            "0.1 mm grid": 0.1,
            "Custom...": None,
        }

        self._setup_styles()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Dark.TFrame", background="#1e1e1e")
        style.configure("Dark.TLabel", background="#1e1e1e", foreground="#e0e0e0",
                         font=("Segoe UI", 10))
        style.configure("Header.TLabel", background="#1e1e1e", foreground="#4fc3f7",
                         font=("Segoe UI", 12, "bold"))
        style.configure("Big.TLabel", background="#1e1e1e", foreground="#76ff03",
                         font=("Consolas", 14, "bold"))
        style.configure("Dark.TButton", font=("Segoe UI", 10))
        style.configure("Action.TButton", font=("Segoe UI", 11, "bold"))

    def _build_ui(self):
        # Outer scrollable container
        outer = ttk.Frame(self.root, style="Dark.TFrame")
        outer.pack(fill=tk.BOTH, expand=True)

        h_scroll = ttk.Scrollbar(outer, orient=tk.HORIZONTAL)
        h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        v_scroll = ttk.Scrollbar(outer, orient=tk.VERTICAL)
        v_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self._main_canvas = tk.Canvas(outer, bg="#1e1e1e", highlightthickness=0,
                                       xscrollcommand=h_scroll.set, yscrollcommand=v_scroll.set)
        self._main_canvas.pack(fill=tk.BOTH, expand=True)
        h_scroll.config(command=self._main_canvas.xview)
        v_scroll.config(command=self._main_canvas.yview)

        main = ttk.Frame(self._main_canvas, style="Dark.TFrame")
        self._main_canvas.create_window((0, 0), window=main, anchor=tk.NW)
        main.bind("<Configure>", lambda e: self._main_canvas.configure(
            scrollregion=self._main_canvas.bbox("all")))

        def _on_mousewheel(event):
            self._main_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self._main_canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # ─── Left: Camera preview ─────────────────────────────
        left = ttk.Frame(main, style="Dark.TFrame")
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)

        ttk.Label(left, text="Camera Preview", style="Header.TLabel").pack(pady=(0, 5))

        self.canvas = tk.Canvas(left, width=612, height=512, bg="#000000",
                                highlightthickness=1, highlightbackground="#333")
        self.canvas.pack()

        # Crosshair label
        ttk.Label(left, text="Red crosshair = alignment reference point",
                  style="Dark.TLabel").pack(pady=5)

        # Live / Capture buttons
        btn_row = ttk.Frame(left, style="Dark.TFrame")
        btn_row.pack(pady=5)
        self.btn_live = ttk.Button(btn_row, text="Start Live", command=self._toggle_live)
        self.btn_live.pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_row, text="Snapshot", command=self._snapshot).pack(side=tk.LEFT, padx=3)

        # Gain & Exposure sliders
        cam_ctrl = ttk.Frame(left, style="Dark.TFrame")
        cam_ctrl.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(cam_ctrl, text="Gain (dB):", style="Dark.TLabel").grid(row=0, column=0, sticky=tk.W)
        self.gain_var = tk.DoubleVar(value=0.0)
        self.gain_slider = ttk.Scale(cam_ctrl, from_=0.0, to=24.0, variable=self.gain_var,
                                      orient=tk.HORIZONTAL, length=300,
                                      command=self._on_gain_change)
        self.gain_slider.grid(row=0, column=1, padx=5)
        self.lbl_gain = ttk.Label(cam_ctrl, text="0.0 dB", style="Dark.TLabel", width=8)
        self.lbl_gain.grid(row=0, column=2)

        ttk.Label(cam_ctrl, text="Exposure (ms):", style="Dark.TLabel").grid(row=1, column=0, sticky=tk.W)
        self.exposure_var = tk.DoubleVar(value=50.0)
        self.exposure_slider = ttk.Scale(cam_ctrl, from_=1.0, to=500.0, variable=self.exposure_var,
                                          orient=tk.HORIZONTAL, length=300,
                                          command=self._on_exposure_change)
        self.exposure_slider.grid(row=1, column=1, padx=5)
        self.lbl_exposure = ttk.Label(cam_ctrl, text="50.0 ms", style="Dark.TLabel", width=8)
        self.lbl_exposure.grid(row=1, column=2)

        # Zoom controls
        zoom_frame = ttk.Frame(left, style="Dark.TFrame")
        zoom_frame.pack(fill=tk.X, padx=10, pady=3)
        ttk.Label(zoom_frame, text="Zoom:", style="Dark.TLabel").pack(side=tk.LEFT)
        for z in ["1x", "2x", "4x", "8x"]:
            ttk.Button(zoom_frame, text=z, width=4,
                       command=lambda zv=z: self._set_zoom(zv)).pack(side=tk.LEFT, padx=2)
        self.lbl_zoom = ttk.Label(zoom_frame, text="1x", style="Dark.TLabel", width=6)
        self.lbl_zoom.pack(side=tk.LEFT, padx=5)


        # ─── Right: Controls ──────────────────────────────────
        right = ttk.Frame(main, style="Dark.TFrame", width=400)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=(15, 10), pady=10)

        # Connection
        ttk.Label(right, text="1. Connect", style="Header.TLabel").pack(anchor=tk.W, pady=(0, 5))
        conn_frame = ttk.Frame(right, style="Dark.TFrame")
        conn_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(conn_frame, text="COM Port:", style="Dark.TLabel").pack(side=tk.LEFT)
        self.com_var = tk.StringVar(value="COM3")
        ttk.Entry(conn_frame, textvariable=self.com_var, width=8).pack(side=tk.LEFT, padx=5)
        self.btn_connect = ttk.Button(conn_frame, text="Connect", command=self._connect)
        self.btn_connect.pack(side=tk.LEFT, padx=5)
        self.lbl_status = ttk.Label(right, text="Disconnected", style="Dark.TLabel")
        self.lbl_status.pack(anchor=tk.W)

        # Position display
        self.lbl_pos = ttk.Label(right, text="Position: -- , --", style="Big.TLabel")
        self.lbl_pos.pack(anchor=tk.W, pady=10)

        # Grid spacing
        ttk.Label(right, text="2. Grid Spacing", style="Header.TLabel").pack(anchor=tk.W, pady=(10, 5))
        self.grid_var = tk.StringVar(value="2.5 mm square grid")
        grid_menu = ttk.Combobox(right, textvariable=self.grid_var,
                                  values=list(self.grid_spacings.keys()),
                                  state="readonly", width=25)
        grid_menu.pack(anchor=tk.W)

        custom_frame = ttk.Frame(right, style="Dark.TFrame")
        custom_frame.pack(fill=tk.X, pady=5)
        ttk.Label(custom_frame, text="Custom (mm):", style="Dark.TLabel").pack(side=tk.LEFT)
        self.custom_spacing = tk.StringVar(value="")
        ttk.Entry(custom_frame, textvariable=self.custom_spacing, width=10).pack(side=tk.LEFT, padx=5)

        # Number of grid lines to span
        span_frame = ttk.Frame(right, style="Dark.TFrame")
        span_frame.pack(fill=tk.X, pady=5)
        ttk.Label(span_frame, text="Grid lines to span:", style="Dark.TLabel").pack(side=tk.LEFT)
        self.span_var = tk.StringVar(value="1")
        ttk.Spinbox(span_frame, from_=1, to=20, textvariable=self.span_var,
                     width=5).pack(side=tk.LEFT, padx=5)
        ttk.Label(span_frame, text="(more = more accurate)", style="Dark.TLabel").pack(side=tk.LEFT)

        # ─── X Calibration ────────────────────────────────────
        ttk.Label(right, text="3. Calibrate X Axis", style="Header.TLabel").pack(anchor=tk.W, pady=(15, 5))
        ttk.Label(right, text="Align a vertical grid line with the crosshair,\n"
                  "then click 'Mark A'. Move stage right until the\n"
                  "next grid line aligns, then click 'Mark B'.",
                  style="Dark.TLabel").pack(anchor=tk.W)

        x_btns = ttk.Frame(right, style="Dark.TFrame")
        x_btns.pack(fill=tk.X, pady=5)
        self.btn_ax = ttk.Button(x_btns, text="Mark A (X)", command=self._mark_ax, style="Action.TButton")
        self.btn_ax.pack(side=tk.LEFT, padx=3)
        self.btn_bx = ttk.Button(x_btns, text="Mark B (X)", command=self._mark_bx, style="Action.TButton")
        self.btn_bx.pack(side=tk.LEFT, padx=3)
        self.lbl_x_result = ttk.Label(right, text="X: not calibrated", style="Dark.TLabel")
        self.lbl_x_result.pack(anchor=tk.W)

        # ─── Y Calibration ────────────────────────────────────
        ttk.Label(right, text="4. Calibrate Y Axis", style="Header.TLabel").pack(anchor=tk.W, pady=(15, 5))
        ttk.Label(right, text="Same procedure but move stage up/down\n"
                  "using a horizontal grid line.",
                  style="Dark.TLabel").pack(anchor=tk.W)

        y_btns = ttk.Frame(right, style="Dark.TFrame")
        y_btns.pack(fill=tk.X, pady=5)
        self.btn_ay = ttk.Button(y_btns, text="Mark A (Y)", command=self._mark_ay, style="Action.TButton")
        self.btn_ay.pack(side=tk.LEFT, padx=3)
        self.btn_by = ttk.Button(y_btns, text="Mark B (Y)", command=self._mark_by, style="Action.TButton")
        self.btn_by.pack(side=tk.LEFT, padx=3)
        self.lbl_y_result = ttk.Label(right, text="Y: not calibrated", style="Dark.TLabel")
        self.lbl_y_result.pack(anchor=tk.W)

        # ─── Save ─────────────────────────────────────────────
        ttk.Label(right, text="5. Save", style="Header.TLabel").pack(anchor=tk.W, pady=(15, 5))
        self.btn_save = ttk.Button(right, text="Save Calibration", command=self._save,
                                    style="Action.TButton")
        self.btn_save.pack(anchor=tk.W)
        self.lbl_saved = ttk.Label(right, text="", style="Dark.TLabel")
        self.lbl_saved.pack(anchor=tk.W, pady=5)

        # ─── Jog controls ─────────────────────────────────────
        ttk.Label(right, text="Stage Movement", style="Header.TLabel").pack(anchor=tk.W, pady=(15, 5))
        jog_frame = ttk.Frame(right, style="Dark.TFrame")
        jog_frame.pack()

        ttk.Label(jog_frame, text="Speed:", style="Dark.TLabel").grid(row=0, column=0, columnspan=3)
        self.speed_var = tk.StringVar(value="Slow")
        speed_names = ["1-Step", "Nudge", "Crawl", "Slow"]
        ttk.Combobox(jog_frame, textvariable=self.speed_var, values=speed_names,
                      state="readonly", width=10).grid(row=1, column=0, columnspan=3, pady=3)

        ttk.Label(jog_frame, text="Hold arrow = continuous move", style="Dark.TLabel"
                  ).grid(row=5, column=0, columnspan=3, pady=(5, 0))

        for sym, r, c, dx, dy in [("\u25B2", 2, 1, 0, 1),
                                    ("\u25C0", 3, 0, -1, 0),
                                    ("\u25B6", 3, 2, 1, 0),
                                    ("\u25BC", 4, 1, 0, -1)]:
            btn = ttk.Button(jog_frame, text=sym, width=4)
            btn.grid(row=r, column=c)
            btn.bind("<ButtonPress-1>", lambda e, x=dx, y=dy: self._jog_start(x, y))
            btn.bind("<ButtonRelease-1>", lambda e: self._jog_stop())

    # ─── Connection ───────────────────────────────────────────

    def _connect(self):
        if self.connected:
            self._disconnect()
            return

        port = self.com_var.get()
        self.lbl_status.config(text="Connecting...")
        self.root.update()

        try:
            self.stage = MAC2000(port=port)
            self.stage.connect()
            self.lbl_status.config(text=f"Stage: {port} OK")
        except Exception as e:
            self.lbl_status.config(text=f"Stage error: {e}")
            return

        try:
            self.camera = TeliCamera()
            self.camera.connect()
            self.lbl_status.config(text=f"Stage: {port} OK | Camera: {self.camera.model}")
            # Read current gain/exposure from camera
            try:
                cur_gain = self.camera.get_gain()
                self.gain_var.set(cur_gain)
                self.lbl_gain.config(text=f"{cur_gain:.1f} dB")
            except Exception:
                pass
            try:
                cur_exp = self.camera.get_exposure() / 1000.0  # us to ms
                self.exposure_var.set(cur_exp)
                self.lbl_exposure.config(text=f"{cur_exp:.1f} ms")
            except Exception:
                pass
        except Exception as e:
            self.lbl_status.config(text=f"Stage OK | Camera error: {e}")
            # Continue without camera - user can still do manual calibration

        self.connected = True
        self.btn_connect.config(text="Disconnect")
        self._update_position()

    def _disconnect(self):
        self.live_running = False
        time.sleep(0.2)

        if self.camera:
            try:
                self.camera.disconnect()
            except Exception:
                pass
            self.camera = None

        if self.stage:
            try:
                self.stage.disconnect()
            except Exception:
                pass
            self.stage = None

        self.connected = False
        self.btn_connect.config(text="Connect")
        self.lbl_status.config(text="Disconnected")

    # ─── Position ─────────────────────────────────────────────

    def _update_position(self):
        if not self.connected or not self.stage:
            return
        try:
            pos = self.stage.get_position()
            self.lbl_pos.config(text=f"Position: {pos.x} , {pos.y}")
        except Exception as e:
            print(f"Position read error: {e}")
        self.root.after(500, self._update_position)

    # ─── Camera ───────────────────────────────────────────────

    def _toggle_live(self):
        if self.live_running:
            self.live_running = False
            self.btn_live.config(text="Start Live")
        else:
            if not self.camera:
                messagebox.showwarning("No Camera", "Camera not connected")
                return
            self.live_running = True
            self.btn_live.config(text="Stop Live")
            threading.Thread(target=self._capture_loop, daemon=True).start()
            self._display_loop()

    def _capture_loop(self):
        """Background thread: capture frames into buffer."""
        while self.live_running:
            try:
                self._pending_frame = self.camera.capture()
            except Exception as e:
                print(f"Capture error: {e}")
                time.sleep(0.1)

    def _display_loop(self):
        """Main thread: display buffered frame at steady rate."""
        if not self.live_running:
            return
        frame = self._pending_frame
        if frame is not None:
            self._render_frame(frame)
        self.root.after(50, self._display_loop)  # ~20 fps on main thread

    def _snapshot(self):
        if not self.camera:
            messagebox.showwarning("No Camera", "Camera not connected")
            return
        try:
            img = self.camera.capture()
            self._render_frame(img)
        except Exception as e:
            messagebox.showerror("Capture Error", str(e))

    def _set_zoom(self, level_str):
        """Set zoom level from button click."""
        self.zoom_level = float(level_str.replace("x", ""))
        self.lbl_zoom.config(text=level_str)

    def _on_gain_change(self, val):
        gain = float(val)
        self.lbl_gain.config(text=f"{gain:.1f} dB")
        if self.camera:
            try:
                self.camera.set_gain(gain)
            except Exception as e:
                print(f"Gain error: {e}")

    def _on_exposure_change(self, val):
        exp_ms = float(val)
        self.lbl_exposure.config(text=f"{exp_ms:.1f} ms")
        if self.camera:
            try:
                self.camera.set_exposure(exp_ms * 1000)  # convert ms to us
            except Exception as e:
                print(f"Exposure error: {e}")

    def _render_frame(self, img):
        """Render frame on canvas with crosshair. Called from main thread only."""
        if img is None:
            return

        canvas_w = self.canvas.winfo_width() or 612
        canvas_h = self.canvas.winfo_height() or 512

        # Zoom: crop center with numpy (fast) before converting to PIL
        ih, iw = img.shape[:2]
        if self.zoom_level > 1.0:
            crop_w = int(iw / self.zoom_level)
            crop_h = int(ih / self.zoom_level)
            x1 = (iw - crop_w) // 2
            y1 = (ih - crop_h) // 2
            img = img[y1:y1 + crop_h, x1:x1 + crop_w]

        # Convert to PIL and resize
        pil_img = Image.fromarray(img)
        pil_img = pil_img.resize((canvas_w, canvas_h), Image.NEAREST if self.zoom_level >= 4.0 else Image.BILINEAR)

        self._tk_img = ImageTk.PhotoImage(pil_img)

        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self._tk_img)

        # Draw crosshair at center
        cx, cy = canvas_w // 2, canvas_h // 2
        ch_len = 30
        self.canvas.create_line(cx - ch_len, cy, cx + ch_len, cy, fill="red", width=2)
        self.canvas.create_line(cx, cy - ch_len, cx, cy + ch_len, fill="red", width=2)

    # ─── Continuous Movement (hold arrow) ──────────────────────

    # Speed presets: name -> pulses per second
    # All speeds use the same approach: set_speed → move_far → halt on release
    SPEED_PRESETS = {
        "1-Step": 85,
        "Nudge":  150,
        "Crawl":  500,
        "Slow":   5000,
    }

    def _jog_start(self, dx, dy):
        """Start continuous movement: set speed, move far, halt on release."""
        if not self.connected or not self.stage:
            return
        self._jog_repeating = True
        try:
            speed = self.SPEED_PRESETS.get(self.speed_var.get(), 5000)
            self.stage.set_speed(speed)
            far = 10_000_000
            self.stage.move_relative(dx * far, dy * far)
        except Exception as e:
            print(f"Move error: {e}")

    def _jog_stop(self):
        """Stop movement on button release."""
        self._jog_repeating = False
        if not self.connected or not self.stage:
            return
        try:
            self.stage.halt()
        except Exception as e:
            print(f"Halt error: {e}")

    # ─── Calibration Marks ────────────────────────────────────

    def _get_grid_spacing_mm(self):
        """Get the grid spacing in mm."""
        name = self.grid_var.get()
        spacing = self.grid_spacings.get(name)
        if spacing is None:
            # Custom
            try:
                spacing = float(self.custom_spacing.get())
            except ValueError:
                messagebox.showwarning("Invalid", "Enter a custom grid spacing in mm")
                return None
        return spacing

    def _mark_ax(self):
        if not self.stage:
            return
        pos = self.stage.get_position()
        self.point_a_x = pos.x
        self.lbl_x_result.config(text=f"X: Point A = {pos.x} steps")

    def _mark_bx(self):
        if not self.stage or self.point_a_x is None:
            messagebox.showwarning("Missing", "Mark point A first")
            return

        pos = self.stage.get_position()
        self.point_b_x = pos.x

        spacing_mm = self._get_grid_spacing_mm()
        if spacing_mm is None:
            return

        n_lines = int(self.span_var.get())
        distance_mm = spacing_mm * n_lines
        distance_um = distance_mm * 1000

        step_diff = abs(self.point_b_x - self.point_a_x)
        if step_diff == 0:
            self.lbl_x_result.config(text="X: Error - no movement detected")
            return

        steps_per_um = step_diff / distance_um
        steps_per_mm = step_diff / distance_mm
        um_per_step = distance_um / step_diff

        self.lbl_x_result.config(
            text=f"X: {step_diff} steps / {distance_um:.0f} um = "
                 f"{steps_per_um:.4f} steps/um  ({um_per_step:.4f} um/step)"
        )

    def _mark_ay(self):
        if not self.stage:
            return
        pos = self.stage.get_position()
        self.point_a_y = pos.y
        self.lbl_y_result.config(text=f"Y: Point A = {pos.y} steps")

    def _mark_by(self):
        if not self.stage or self.point_a_y is None:
            messagebox.showwarning("Missing", "Mark point A first")
            return

        pos = self.stage.get_position()
        self.point_b_y = pos.y

        spacing_mm = self._get_grid_spacing_mm()
        if spacing_mm is None:
            return

        n_lines = int(self.span_var.get())
        distance_mm = spacing_mm * n_lines
        distance_um = distance_mm * 1000

        step_diff = abs(self.point_b_y - self.point_a_y)
        if step_diff == 0:
            self.lbl_y_result.config(text="Y: Error - no movement detected")
            return

        steps_per_um = step_diff / distance_um
        steps_per_mm = step_diff / distance_mm
        um_per_step = distance_um / step_diff

        self.lbl_y_result.config(
            text=f"Y: {step_diff} steps / {distance_um:.0f} um = "
                 f"{steps_per_um:.4f} steps/um  ({um_per_step:.4f} um/step)"
        )

    # ─── Save ─────────────────────────────────────────────────

    def _save(self):
        spacing_mm = self._get_grid_spacing_mm()
        n_lines = int(self.span_var.get())

        cal = {
            "grid_spacing_mm": spacing_mm,
            "grid_lines_spanned": n_lines,
        }

        if self.point_a_x is not None and self.point_b_x is not None:
            step_diff = abs(self.point_b_x - self.point_a_x)
            distance_um = spacing_mm * n_lines * 1000
            cal["x_axis"] = {
                "point_a_steps": self.point_a_x,
                "point_b_steps": self.point_b_x,
                "step_difference": step_diff,
                "distance_um": distance_um,
                "steps_per_um": step_diff / distance_um if distance_um else 0,
                "um_per_step": distance_um / step_diff if step_diff else 0,
            }

        if self.point_a_y is not None and self.point_b_y is not None:
            step_diff = abs(self.point_b_y - self.point_a_y)
            distance_um = spacing_mm * n_lines * 1000
            cal["y_axis"] = {
                "point_a_steps": self.point_a_y,
                "point_b_steps": self.point_b_y,
                "step_difference": step_diff,
                "distance_um": distance_um,
                "steps_per_um": step_diff / distance_um if distance_um else 0,
                "um_per_step": distance_um / step_diff if step_diff else 0,
            }

        out_path = os.path.join(os.path.dirname(__file__), "stage_calibration.json")
        with open(out_path, "w") as f:
            json.dump(cal, f, indent=2)

        self.lbl_saved.config(text=f"Saved to {out_path}")

        # Print summary
        print("\n" + "=" * 50)
        print("  STAGE CALIBRATION RESULTS")
        print("=" * 50)
        if "x_axis" in cal:
            xa = cal["x_axis"]
            print(f"  X: {xa['steps_per_um']:.4f} steps/um  ({xa['um_per_step']:.4f} um/step)")
        if "y_axis" in cal:
            ya = cal["y_axis"]
            print(f"  Y: {ya['steps_per_um']:.4f} steps/um  ({ya['um_per_step']:.4f} um/step)")
        print("=" * 50)

    # ─── Cleanup ──────────────────────────────────────────────

    def _on_close(self):
        self.live_running = False
        time.sleep(0.2)
        self._disconnect()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = StageCalibrator()
    app.run()
