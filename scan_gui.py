"""
Tile Scan GUI
=============
Interactive GUI for capturing tiled images of a semiconductor die.

Workflow:
    1. Connect to stage and camera
    2. Navigate to one corner of the die → click "Mark Corner 1"
    3. Navigate to the opposite corner → click "Mark Corner 2"
    4. Software calculates bounding box + overscan margin
    5. Review scan preview (grid size, total tiles, estimated time)
    6. Click "Start Scan" — typewriter (left-to-right raster) capture with TIFF output
    7. Outputs: tiles, scan_metadata.json, TileConfiguration.txt

Usage:
    python MAC2000/scan_gui.py
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import time
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageTk, ImageEnhance

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mac2000_driver import MAC2000
from teli_camera import TeliCamera
from stitcher import Stitcher

# ─── WILD Stereo Microscope Calibrations ────────────────────────────
# From DieAnalysisTool/calibrations.js — camera is 2448 × 2048

OBJECTIVES = {
    "<6.3x": {"um_per_pixel": 8.389},
    "6.3x":  {"um_per_pixel": 8.260},
    "7x":    {"um_per_pixel": 7.455},
    "8x":    {"um_per_pixel": 6.553},
    "10x":   {"um_per_pixel": 5.269},
    "12.5x": {"um_per_pixel": 4.237},
    "16x":   {"um_per_pixel": 3.324},
    "20x":   {"um_per_pixel": 2.668},
    "25x":   {"um_per_pixel": 2.139},
    "32x":   {"um_per_pixel": 1.671},
    ">32x":  {"um_per_pixel": 1.660},
}

CAMERA_WIDTH_PX = 2448
CAMERA_HEIGHT_PX = 2048

# ─── Histogram / Clipping Detection ────────────────────────────────
# 12-bit data left-shifted to 16-bit: max = 4095 << 4 = 65520
# Threshold slightly below max to catch near-clipping
CLIP_THRESHOLD_U16 = 65455
CLIP_THRESHOLD_U8 = 253
CLIP_WARN_PCT = 0.2  # percentage of clipped pixels to trigger warning


class TileScanGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Tile Scan — MAC2000 + Teli Camera")
        self.root.state("zoomed")  # maximize window
        self.root.configure(bg="#1e1e1e")

        # Hardware
        self.stage = None
        self.camera = None
        self.connected = False
        self.live_running = False
        self._pending_frame = None
        self._pending_histogram = None
        self._camera_settings_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "camera_settings.json")
        self._jog_repeating = False
        self.zoom_level = 1.0
        self._stage_lock = threading.Lock()  # serializes all stage serial I/O
        self._preview_size = (1150, 900)  # cached canvas size for bg thread

        # Image correction state
        self._clahe_on = False
        self._auto_levels_on = False

        # Scan corners (stage steps) — named die corners
        # UL=upper-left, UR=upper-right, LL=lower-left, LR=lower-right
        self.corner_ul = None  # (x, y) in steps
        self.corner_ur = None
        self.corner_ll = None
        self.corner_lr = None

        # Scan state
        self._scanning = False
        self._abort_scan = False
        self._step_focus_mode = False
        self._focus_confirmed = threading.Event()
        self._locked_gain = None       # locked gain (dB) during scan
        self._locked_exposure = None   # locked exposure (ms) during scan

        # Stage calibration
        self.steps_per_um = 2.5  # default from calibration
        self._load_stage_calibration()

        self._setup_styles()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _load_stage_calibration(self):
        """Load steps_per_um from stage_calibration.json if available."""
        cal_path = os.path.join(os.path.dirname(__file__), "stage_calibration.json")
        try:
            with open(cal_path) as f:
                cal = json.load(f)
            vals = []
            if "x_axis" in cal:
                vals.append(cal["x_axis"]["steps_per_um"])
            if "y_axis" in cal:
                vals.append(cal["y_axis"]["steps_per_um"])
            if vals:
                self.steps_per_um = sum(vals) / len(vals)
        except Exception:
            pass

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
        style.configure("Abort.TButton", font=("Segoe UI", 11, "bold"))

    # ─── UI Build ──────────────────────────────────────────────────────

    def _make_slider_row(self, parent, label_text, var, from_, to_, step,
                         fmt="{:.1f}", row=0, length=220, command=None):
        """Create a slider row with [<] slider [>] arrow buttons.

        Returns (slider, value_label, btn_dec, btn_inc) for external reference.
        """
        ttk.Label(parent, text=label_text, style="Dark.TLabel").grid(
            row=row, column=0, sticky=tk.W)

        def _dec():
            val = max(from_, var.get() - step)
            var.set(val)
            lbl.config(text=fmt.format(val))
            if command:
                command(val)

        def _inc():
            val = min(to_, var.get() + step)
            var.set(val)
            lbl.config(text=fmt.format(val))
            if command:
                command(val)

        btn_dec = ttk.Button(parent, text="\u25C0", width=2, command=_dec)
        btn_dec.grid(row=row, column=1, padx=(5, 0))

        slider = ttk.Scale(parent, from_=from_, to=to_, variable=var,
                           orient=tk.HORIZONTAL, length=length, command=command)
        slider.grid(row=row, column=2, padx=2)

        btn_inc = ttk.Button(parent, text="\u25B6", width=2, command=_inc)
        btn_inc.grid(row=row, column=3, padx=(0, 5))

        lbl = ttk.Label(parent, text=fmt.format(var.get()),
                        style="Dark.TLabel", width=7)
        lbl.grid(row=row, column=4)

        return slider, lbl, btn_dec, btn_inc

    def _build_ui(self):
        main = ttk.Frame(self.root, style="Dark.TFrame")
        main.pack(fill=tk.BOTH, expand=True)

        # ─── Left: Camera preview (fills all available space) ───
        left = ttk.Frame(main, style="Dark.TFrame")
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)

        ttk.Label(left, text="Camera Preview", style="Header.TLabel").pack(pady=(0, 5))

        self.canvas = tk.Canvas(left, bg="#000000",
                                highlightthickness=1, highlightbackground="#333")
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Double-Button-1>", self._on_canvas_double_click)

        ttk.Label(left, text="Red crosshair = alignment reference  |  Double-click to drive",
                  style="Dark.TLabel").pack(pady=3)

        # Step & Focus: confirmation area (below preview, always visible when active)
        self._focus_frame = ttk.Frame(left, style="Dark.TFrame")
        # Not packed initially — shown/hidden dynamically during step-and-focus mode

        self._btn_row = ttk.Frame(left, style="Dark.TFrame")
        self._btn_row.pack(pady=3)
        self.btn_live = ttk.Button(self._btn_row, text="Start Live", command=self._toggle_live)
        self.btn_live.pack(side=tk.LEFT, padx=3)
        ttk.Button(self._btn_row, text="Snapshot", command=self._snapshot).pack(side=tk.LEFT, padx=3)

        # Gain & Exposure sliders
        cam_ctrl = ttk.Frame(left, style="Dark.TFrame")
        cam_ctrl.pack(fill=tk.X, padx=10, pady=5)

        self.gain_var = tk.DoubleVar(value=0.0)
        self.gain_slider, self.lbl_gain, self._gain_dec, self._gain_inc = self._make_slider_row(
            cam_ctrl, "Gain (dB):", self.gain_var, 0.0, 24.0, 0.1,
            fmt="{:.1f} dB", row=0, length=220, command=self._on_gain_change)

        self.exposure_var = tk.DoubleVar(value=50.0)
        self.exposure_slider, self.lbl_exposure, self._exp_dec, self._exp_inc = self._make_slider_row(
            cam_ctrl, "Exposure (ms):", self.exposure_var, 1.0, 500.0, 1.0,
            fmt="{:.1f} ms", row=1, length=220, command=self._on_exposure_change)

        # Bit depth toggle
        self.bit_depth_var = tk.IntVar(value=12)
        bit_frame = ttk.Frame(cam_ctrl, style="Dark.TFrame")
        bit_frame.grid(row=2, column=0, columnspan=5, sticky=tk.W, pady=(5, 0))
        self.chk_bit_depth = ttk.Checkbutton(
            bit_frame, text="12-bit Capture", variable=self.bit_depth_var,
            onvalue=12, offvalue=8, command=self._toggle_bit_depth)
        self.chk_bit_depth.pack(side=tk.LEFT)
        self.lbl_bit_depth = ttk.Label(bit_frame, text="(12-bit)", style="Dark.TLabel")
        self.lbl_bit_depth.pack(side=tk.LEFT, padx=5)

        # Histogram widget
        hist_header = ttk.Frame(cam_ctrl, style="Dark.TFrame")
        hist_header.grid(row=3, column=0, columnspan=3, sticky=tk.W + tk.E, pady=(8, 2))
        ttk.Label(hist_header, text="Histogram", style="Dark.TLabel").pack(side=tk.LEFT)
        self.lbl_clip = ttk.Label(hist_header, text="", style="Dark.TLabel")
        self.lbl_clip.pack(side=tk.RIGHT)

        self.hist_canvas = tk.Canvas(cam_ctrl, height=100, bg="#111111",
                                      highlightthickness=1, highlightbackground="#333")
        self.hist_canvas.grid(row=4, column=0, columnspan=3, sticky=tk.W + tk.E, pady=(0, 5))

        # Zoom controls
        zoom_frame = ttk.Frame(left, style="Dark.TFrame")
        zoom_frame.pack(fill=tk.X, padx=10, pady=3)
        ttk.Label(zoom_frame, text="Zoom:", style="Dark.TLabel").pack(side=tk.LEFT)
        for z in ["1x", "2x", "4x", "8x"]:
            ttk.Button(zoom_frame, text=z, width=4,
                       command=lambda zv=z: self._set_zoom(zv)).pack(side=tk.LEFT, padx=2)
        self.lbl_zoom = ttk.Label(zoom_frame, text="1x", style="Dark.TLabel", width=6)
        self.lbl_zoom.pack(side=tk.LEFT, padx=5)

        # ─── Image Corrections ─────────────────────────────────
        ttk.Label(left, text="Image Corrections", style="Header.TLabel").pack(pady=(10, 3))

        corr_grid = ttk.Frame(left, style="Dark.TFrame")
        corr_grid.pack(fill=tk.X, padx=10)

        self.brightness_var = tk.DoubleVar(value=0.0)
        _, self.lbl_brightness, _, _ = self._make_slider_row(
            corr_grid, "Brightness:", self.brightness_var, -100.0, 100.0, 5.0,
            fmt="{:+.0f}", row=0, length=180)

        self.contrast_var = tk.DoubleVar(value=1.0)
        _, self.lbl_contrast, _, _ = self._make_slider_row(
            corr_grid, "Contrast:", self.contrast_var, 0.1, 3.0, 0.1,
            fmt="{:.2f}", row=1, length=180)

        self.gamma_var = tk.DoubleVar(value=1.0)
        _, self.lbl_gamma, _, _ = self._make_slider_row(
            corr_grid, "Gamma:", self.gamma_var, 0.2, 5.0, 0.1,
            fmt="{:.2f}", row=2, length=180)

        corr_btns = ttk.Frame(left, style="Dark.TFrame")
        corr_btns.pack(fill=tk.X, padx=10, pady=5)

        self.auto_levels_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(corr_btns, text="Auto Levels", variable=self.auto_levels_var,
                         ).pack(side=tk.LEFT, padx=3)

        self.clahe_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(corr_btns, text="CLAHE", variable=self.clahe_var,
                         ).pack(side=tk.LEFT, padx=3)

        ttk.Button(corr_btns, text="Reset All", command=self._reset_corrections,
                   width=8).pack(side=tk.LEFT, padx=8)

        self.save_corrected_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(corr_btns, text="Apply to saved tiles", variable=self.save_corrected_var,
                         ).pack(side=tk.LEFT, padx=3)

        # ─── Right: Controls (scrollable) ────────────────────
        right_outer = ttk.Frame(main, style="Dark.TFrame", width=560)
        right_outer.pack(side=tk.RIGHT, fill=tk.Y, padx=(5, 10), pady=10)
        right_outer.pack_propagate(False)

        self._right_canvas = tk.Canvas(right_outer, bg="#1e1e1e", highlightthickness=0, width=540)
        right_vscroll = ttk.Scrollbar(right_outer, orient=tk.VERTICAL, command=self._right_canvas.yview)
        right_vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._right_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._right_canvas.configure(yscrollcommand=right_vscroll.set)

        right = ttk.Frame(self._right_canvas, style="Dark.TFrame")
        self._right_canvas.create_window((0, 0), window=right, anchor=tk.NW)
        right.bind("<Configure>", lambda e: self._right_canvas.configure(
            scrollregion=self._right_canvas.bbox("all")))

        def _on_right_mousewheel(event):
            self._right_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self._right_canvas.bind("<Enter>", lambda e: self._right_canvas.bind_all("<MouseWheel>", _on_right_mousewheel))
        self._right_canvas.bind("<Leave>", lambda e: self._right_canvas.unbind_all("<MouseWheel>"))

        # --- 1. Connect ---
        ttk.Label(right, text="1. Connect", style="Header.TLabel").pack(anchor=tk.W, pady=(0, 5))
        conn_frame = ttk.Frame(right, style="Dark.TFrame")
        conn_frame.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(conn_frame, text="COM:", style="Dark.TLabel").pack(side=tk.LEFT)
        self.com_var = tk.StringVar(value="COM3")
        ttk.Entry(conn_frame, textvariable=self.com_var, width=7).pack(side=tk.LEFT, padx=3)
        ttk.Label(conn_frame, text="Cam#:", style="Dark.TLabel").pack(side=tk.LEFT)
        self.cam_index_var = tk.StringVar(value="1")
        ttk.Combobox(conn_frame, textvariable=self.cam_index_var,
                      values=["auto", "0", "1", "2", "3"],
                      state="readonly", width=5).pack(side=tk.LEFT, padx=3)
        self.btn_connect = ttk.Button(conn_frame, text="Connect", command=self._connect)
        self.btn_connect.pack(side=tk.LEFT, padx=5)
        self.lbl_status = ttk.Label(right, text="Disconnected", style="Dark.TLabel")
        self.lbl_status.pack(anchor=tk.W)
        self.lbl_pos = ttk.Label(right, text="Position: -- , --", style="Big.TLabel")
        self.lbl_pos.pack(anchor=tk.W, pady=(5, 10))

        # --- 2. Stage Movement ---
        ttk.Label(right, text="2. Stage Movement", style="Header.TLabel").pack(anchor=tk.W, pady=(5, 5))
        jog_frame = ttk.Frame(right, style="Dark.TFrame")
        jog_frame.pack()

        ttk.Label(jog_frame, text="Speed:", style="Dark.TLabel").grid(row=0, column=0, columnspan=3)
        self.speed_var = tk.StringVar(value="Slow")
        speed_names = ["1-Step", "Nudge", "Crawl", "Slow", "Fast"]
        ttk.Combobox(jog_frame, textvariable=self.speed_var, values=speed_names,
                      state="readonly", width=10).grid(row=1, column=0, columnspan=3, pady=3)

        for sym, r, c, dx, dy in [("\u25B2", 2, 1, 0, 1),
                                    ("\u25C0", 3, 0, -1, 0),
                                    ("\u25B6", 3, 2, 1, 0),
                                    ("\u25BC", 4, 1, 0, -1)]:
            btn = ttk.Button(jog_frame, text=sym, width=4)
            btn.grid(row=r, column=c)
            btn.bind("<ButtonPress-1>", lambda e, x=dx, y=dy: self._jog_start(x, y))
            btn.bind("<ButtonRelease-1>", lambda e: self._jog_stop())

        ttk.Label(jog_frame, text="Hold arrow = continuous move", style="Dark.TLabel"
                  ).grid(row=5, column=0, columnspan=3, pady=(5, 0))

        invert_frame = ttk.Frame(right, style="Dark.TFrame")
        invert_frame.pack(anchor=tk.W, pady=(3, 0))
        self.invert_x_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(invert_frame, text="Invert X", variable=self.invert_x_var
                         ).pack(side=tk.LEFT, padx=3)
        self.invert_y_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(invert_frame, text="Invert Y", variable=self.invert_y_var
                         ).pack(side=tk.LEFT, padx=3)

        # --- 3. Mark Die Corners ---
        ttk.Label(right, text="3. Mark Die Corners", style="Header.TLabel").pack(anchor=tk.W, pady=(15, 5))
        ttk.Label(right, text="Navigate to each corner and mark it.\n"
                  "UL/LR required (opposite corners). All 4 ideal.",
                  style="Dark.TLabel").pack(anchor=tk.W)

        corner_row1 = ttk.Frame(right, style="Dark.TFrame")
        corner_row1.pack(fill=tk.X, pady=(5, 2))
        ttk.Button(corner_row1, text="Mark UL",
                   command=lambda: self._mark_named_corner("UL"),
                   style="Action.TButton").pack(side=tk.LEFT, padx=2)
        ttk.Button(corner_row1, text="Mark UR",
                   command=lambda: self._mark_named_corner("UR"),
                   style="Action.TButton").pack(side=tk.LEFT, padx=2)
        ttk.Button(corner_row1, text="Clear", command=self._clear_corners).pack(side=tk.LEFT, padx=5)

        corner_row2 = ttk.Frame(right, style="Dark.TFrame")
        corner_row2.pack(fill=tk.X, pady=(0, 2))
        ttk.Button(corner_row2, text="Mark LL",
                   command=lambda: self._mark_named_corner("LL"),
                   style="Action.TButton").pack(side=tk.LEFT, padx=2)
        ttk.Button(corner_row2, text="Mark LR",
                   command=lambda: self._mark_named_corner("LR"),
                   style="Action.TButton").pack(side=tk.LEFT, padx=2)

        corner_check_row = ttk.Frame(right, style="Dark.TFrame")
        corner_check_row.pack(fill=tk.X, pady=(3, 2))
        ttk.Button(corner_check_row, text="Corner Check",
                   command=self._corner_check,
                   style="Action.TButton").pack(side=tk.LEFT, padx=2)

        self.lbl_corner_ul = ttk.Label(right, text="UL: not set", style="Dark.TLabel")
        self.lbl_corner_ul.pack(anchor=tk.W)
        self.lbl_corner_ur = ttk.Label(right, text="UR: not set", style="Dark.TLabel")
        self.lbl_corner_ur.pack(anchor=tk.W)
        self.lbl_corner_ll = ttk.Label(right, text="LL: not set", style="Dark.TLabel")
        self.lbl_corner_ll.pack(anchor=tk.W)
        self.lbl_corner_lr = ttk.Label(right, text="LR: not set", style="Dark.TLabel")
        self.lbl_corner_lr.pack(anchor=tk.W)
        self.lbl_area = ttk.Label(right, text="", style="Dark.TLabel")
        self.lbl_area.pack(anchor=tk.W, pady=(3, 0))

        # --- 4. Scan Settings ---
        ttk.Label(right, text="4. Scan Settings", style="Header.TLabel").pack(anchor=tk.W, pady=(15, 5))

        settings = ttk.Frame(right, style="Dark.TFrame")
        settings.pack(fill=tk.X)

        ttk.Label(settings, text="Objective:", style="Dark.TLabel").grid(row=0, column=0, sticky=tk.W)
        self.obj_var = tk.StringVar(value="20x")
        ttk.Combobox(settings, textvariable=self.obj_var,
                      values=list(OBJECTIVES.keys()),
                      state="readonly", width=8).grid(row=0, column=1, padx=5, sticky=tk.W)

        ttk.Label(settings, text="Overlap %:", style="Dark.TLabel").grid(row=1, column=0, sticky=tk.W)
        self.overlap_var = tk.DoubleVar(value=10.0)
        ttk.Spinbox(settings, from_=1, to=50, textvariable=self.overlap_var,
                     width=6, increment=1).grid(row=1, column=1, padx=5, sticky=tk.W)

        ttk.Label(settings, text="Overscan %:", style="Dark.TLabel").grid(row=2, column=0, sticky=tk.W)
        self.overscan_var = tk.DoubleVar(value=10.0)
        ttk.Spinbox(settings, from_=0, to=50, textvariable=self.overscan_var,
                     width=6, increment=5).grid(row=2, column=1, padx=5, sticky=tk.W)

        ttk.Label(settings, text="Settle (s):", style="Dark.TLabel").grid(row=3, column=0, sticky=tk.W)
        self.settle_var = tk.DoubleVar(value=0.3)
        ttk.Spinbox(settings, from_=0.0, to=2.0, textvariable=self.settle_var,
                     width=6, increment=0.1, format="%.1f").grid(row=3, column=1, padx=5, sticky=tk.W)

        # Update preview on setting change
        self.obj_var.trace_add("write", lambda *a: self._update_preview())
        self.overlap_var.trace_add("write", lambda *a: self._update_preview())
        self.overscan_var.trace_add("write", lambda *a: self._update_preview())

        # --- Save / Load Config ---
        cfg_btns = ttk.Frame(right, style="Dark.TFrame")
        cfg_btns.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(cfg_btns, text="Save Config", command=self._save_config,
                   style="Action.TButton").pack(side=tk.LEFT, padx=3)
        ttk.Button(cfg_btns, text="Load Config", command=self._load_config,
                   style="Action.TButton").pack(side=tk.LEFT, padx=3)

        # --- 5. Scan Preview ---
        ttk.Label(right, text="5. Scan Preview", style="Header.TLabel").pack(anchor=tk.W, pady=(15, 5))
        self.lbl_preview = ttk.Label(right, text="Mark both corners to see preview",
                                      style="Dark.TLabel", wraplength=400, justify=tk.LEFT)
        self.lbl_preview.pack(anchor=tk.W)

        # --- 6. Output ---
        ttk.Label(right, text="6. Output", style="Header.TLabel").pack(anchor=tk.W, pady=(15, 5))

        # Folder selection
        folder_frame = ttk.Frame(right, style="Dark.TFrame")
        folder_frame.pack(fill=tk.X, pady=(0, 3))
        ttk.Label(folder_frame, text="Folder:", style="Dark.TLabel").pack(side=tk.LEFT)
        default_output = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_output")
        self.output_folder_var = tk.StringVar(value=default_output)
        self.entry_folder = ttk.Entry(folder_frame, textvariable=self.output_folder_var, width=30)
        self.entry_folder.pack(side=tk.LEFT, padx=5)
        ttk.Button(folder_frame, text="Browse...", command=self._browse_output_folder,
                   width=8).pack(side=tk.LEFT, padx=2)

        # File name
        file_frame = ttk.Frame(right, style="Dark.TFrame")
        file_frame.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(file_frame, text="File Name:", style="Dark.TLabel").pack(side=tk.LEFT)
        self.file_name_var = tk.StringVar(value="stitched")
        ttk.Entry(file_frame, textvariable=self.file_name_var, width=20).pack(side=tk.LEFT, padx=5)
        ttk.Label(file_frame, text=".tif / .png", style="Dark.TLabel").pack(side=tk.LEFT)

        # --- 7. Scan ---
        ttk.Label(right, text="7. Scan", style="Header.TLabel").pack(anchor=tk.W, pady=(10, 5))

        scan_btns = ttk.Frame(right, style="Dark.TFrame")
        scan_btns.pack(fill=tk.X, pady=5)
        self.btn_scan = ttk.Button(scan_btns, text="Start Scan", command=self._start_scan,
                                    style="Action.TButton")
        self.btn_scan.pack(side=tk.LEFT, padx=3)
        self.btn_step_focus = ttk.Button(scan_btns, text="Step & Focus",
                                          command=self._start_step_focus,
                                          style="Action.TButton")
        self.btn_step_focus.pack(side=tk.LEFT, padx=3)
        self.btn_abort = ttk.Button(scan_btns, text="Abort", command=self._abort,
                                     style="Abort.TButton", state=tk.DISABLED)
        self.btn_abort.pack(side=tk.LEFT, padx=3)

        # Step & Focus: confirmation button and info label (in left panel focus frame)
        self.lbl_step_focus_info = ttk.Label(self._focus_frame, text="",
                                              style="Dark.TLabel",
                                              wraplength=600, justify=tk.CENTER)
        self.lbl_step_focus_info.pack(pady=(5, 2))
        self.btn_focus_confirmed = tk.Button(
            self._focus_frame, text="Focus Confirmed", bg="#00c853", fg="white",
            font=("Segoe UI", 16, "bold"), activebackground="#00e676",
            activeforeground="white", relief=tk.RAISED, bd=3,
            cursor="hand2", padx=30, pady=8,
            command=self._on_focus_confirmed)
        self.btn_focus_confirmed.pack(pady=(2, 5))

        self.lbl_tile_progress = ttk.Label(right, text="", style="Dark.TLabel")
        self.lbl_tile_progress.pack(anchor=tk.W, pady=(3, 0))

        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(right, variable=self.progress_var,
                                             maximum=100, length=400)
        self.progress_bar.pack(fill=tk.X, pady=5)

        self.lbl_timer = ttk.Label(right, text="", style="Dark.TLabel")
        self.lbl_timer.pack(anchor=tk.W)

        self.lbl_scan_status = ttk.Label(right, text="", style="Dark.TLabel")
        self.lbl_scan_status.pack(anchor=tk.W)

    # ─── Output Folder ─────────────────────────────────────────────

    def _browse_output_folder(self):
        """Open folder picker for scan output directory."""
        from tkinter import filedialog
        current = self.output_folder_var.get()
        if not os.path.isdir(current):
            current = os.path.dirname(os.path.abspath(__file__))
        folder = filedialog.askdirectory(
            initialdir=current,
            title="Select Output Folder",
        )
        if folder:
            self.output_folder_var.set(folder)

    # ─── Config Save / Load ──────────────────────────────────────────

    CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_config.json")

    def _save_config(self):
        """Save all settings to a JSON config file."""
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            initialdir=os.path.dirname(self.CONFIG_FILE),
            initialfile="scan_config.json",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            title="Save Configuration",
        )
        if not path:
            return

        config = {
            "objective": self.obj_var.get(),
            "overlap_pct": self.overlap_var.get(),
            "overscan_pct": self.overscan_var.get(),
            "settle_time": self.settle_var.get(),
            "gain_db": self.gain_var.get(),
            "exposure_ms": self.exposure_var.get(),
            "brightness": self.brightness_var.get(),
            "contrast": self.contrast_var.get(),
            "gamma": self.gamma_var.get(),
            "auto_levels": self.auto_levels_var.get(),
            "clahe": self.clahe_var.get(),
            "apply_corrections_to_tiles": self.save_corrected_var.get(),
            "invert_x": self.invert_x_var.get(),
            "invert_y": self.invert_y_var.get(),
            "speed": self.speed_var.get(),
            "com_port": self.com_var.get(),
            "cam_index": self.cam_index_var.get(),
            "output_folder": self.output_folder_var.get(),
            "file_name": self.file_name_var.get(),
        }
        try:
            with open(path, "w") as f:
                json.dump(config, f, indent=2)
            self.lbl_scan_status.config(text=f"Config saved: {os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Save Error", str(e))

    def _load_config(self):
        """Load settings from a JSON config file."""
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            initialdir=os.path.dirname(self.CONFIG_FILE),
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            title="Load Configuration",
        )
        if not path:
            return

        try:
            with open(path) as f:
                config = json.load(f)
        except Exception as e:
            messagebox.showerror("Load Error", str(e))
            return

        # Apply each setting if present in the config
        if "objective" in config and config["objective"] in OBJECTIVES:
            self.obj_var.set(config["objective"])
        if "overlap_pct" in config:
            self.overlap_var.set(config["overlap_pct"])
        if "overscan_pct" in config:
            self.overscan_var.set(config["overscan_pct"])
        if "settle_time" in config:
            self.settle_var.set(config["settle_time"])
        if "gain_db" in config:
            self.gain_var.set(config["gain_db"])
            if self.camera:
                self.camera.set_gain(config["gain_db"])
        if "exposure_ms" in config:
            self.exposure_var.set(config["exposure_ms"])
            if self.camera:
                self.camera.set_exposure(config["exposure_ms"] * 1000)
        if "brightness" in config:
            self.brightness_var.set(config["brightness"])
        if "contrast" in config:
            self.contrast_var.set(config["contrast"])
        if "gamma" in config:
            self.gamma_var.set(config["gamma"])
        if "auto_levels" in config:
            self.auto_levels_var.set(config["auto_levels"])
        if "clahe" in config:
            self.clahe_var.set(config["clahe"])
        if "apply_corrections_to_tiles" in config:
            self.save_corrected_var.set(config["apply_corrections_to_tiles"])
        if "invert_x" in config:
            self.invert_x_var.set(config["invert_x"])
        if "invert_y" in config:
            self.invert_y_var.set(config["invert_y"])
        if "speed" in config and config["speed"] in SPEED_PRESETS:
            self.speed_var.set(config["speed"])
        if "com_port" in config:
            self.com_var.set(config["com_port"])
        if "cam_index" in config:
            self.cam_index_var.set(config["cam_index"])
        if "output_folder" in config:
            self.output_folder_var.set(config["output_folder"])
        if "file_name" in config:
            self.file_name_var.set(config["file_name"])

        self.lbl_scan_status.config(text=f"Config loaded: {os.path.basename(path)}")

    # ─── Connection ────────────────────────────────────────────────────

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
            cam_idx_str = self.cam_index_var.get()
            cam_idx = None if cam_idx_str == "auto" else int(cam_idx_str)
            self.camera = TeliCamera(camera_index=cam_idx)
            self.camera.connect()
            cam_bits = self.camera.get_bit_depth()
            self.bit_depth_var.set(cam_bits)
            self.lbl_bit_depth.config(text=f"({cam_bits}-bit)")
            self.lbl_status.config(
                text=f"Stage: {port} OK | Camera: {self.camera.model} ({cam_bits}-bit)")
            try:
                cur_gain = self.camera.get_gain()
                self.gain_var.set(cur_gain)
                self.lbl_gain.config(text=f"{cur_gain:.1f} dB")
            except Exception:
                pass
            try:
                cur_exp = self.camera.get_exposure() / 1000.0
                self.exposure_var.set(cur_exp)
                self.lbl_exposure.config(text=f"{cur_exp:.1f} ms")
            except Exception:
                pass
        except Exception as e:
            self.lbl_status.config(text=f"Stage OK | Camera error: {e}")

        self.connected = True
        self.btn_connect.config(text="Disconnect")
        self._update_position()

    def _disconnect(self):
        self._auto_save_camera_settings()
        self.live_running = False
        self._scanning = False
        self._abort_scan = True
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

    # ─── Position ──────────────────────────────────────────────────────

    def _update_position(self):
        if not self.connected or not self.stage:
            return
        # Don't poll position while scanning — serial conflicts
        if self._scanning:
            self.root.after(1000, self._update_position)
            return
        # Read position in background thread to avoid blocking GUI
        threading.Thread(target=self._read_position_bg, daemon=True).start()
        self.root.after(500, self._update_position)

    def _read_position_bg(self):
        """Read stage position without blocking the main thread."""
        if not self._stage_lock.acquire(blocking=False):
            return  # serial busy, skip this poll
        try:
            pos = self.stage.get_position()
            self.root.after(0, lambda: self.lbl_pos.config(
                text=f"Position: {pos.x} , {pos.y}"))
        except Exception as e:
            print(f"Position read error: {e}")
        finally:
            self._stage_lock.release()

    # ─── Camera ────────────────────────────────────────────────────────

    def _toggle_live(self):
        if self.live_running:
            self.live_running = False
            self.btn_live.config(text="Start Live")
            self._pending_histogram = None
            self.hist_canvas.delete("all")
            self.lbl_clip.config(text="")
        else:
            if not self.camera:
                messagebox.showwarning("No Camera", "Camera not connected")
                return
            # Don't reload saved camera settings during a scan — keep locked values
            if not self._scanning:
                self._auto_load_camera_settings()
            self.live_running = True
            self.btn_live.config(text="Stop Live")
            self._update_preview_size()
            threading.Thread(target=self._capture_loop, daemon=True).start()
            self._display_loop()

    def _update_preview_size(self):
        """Update cached preview size from actual canvas dimensions."""
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w > 10 and h > 10:
            self._preview_size = (w, h)

    def _capture_loop(self):
        """Background thread: capture + crop + resize (heavy work off main thread)."""
        import cv2
        while self.live_running:
            try:
                frame = self.camera.capture()
                ih, iw = frame.shape[:2]

                # Compute histogram from raw frame (before zoom/conversion)
                if len(frame.shape) == 3:
                    sub = frame[::4, ::4, :]  # downsample 4x for speed
                    total_px = sub.shape[0] * sub.shape[1]
                    hists = []
                    if frame.dtype == np.uint16:
                        hist_range = (0, 65535)
                        clip_thresh = CLIP_THRESHOLD_U16
                    else:
                        hist_range = (0, 255)
                        clip_thresh = CLIP_THRESHOLD_U8
                    for ch in range(3):
                        h, _ = np.histogram(sub[:, :, ch], bins=256, range=hist_range)
                        hists.append(h)
                    clipped = int(np.any(sub >= clip_thresh, axis=2).sum())
                    clip_pct = (clipped / total_px) * 100.0
                    clip_r = float(np.sum(sub[:, :, 0] >= clip_thresh) / total_px * 100)
                    clip_g = float(np.sum(sub[:, :, 1] >= clip_thresh) / total_px * 100)
                    clip_b = float(np.sum(sub[:, :, 2] >= clip_thresh) / total_px * 100)
                    self._pending_histogram = {
                        "hists": hists,
                        "clip_pct": clip_pct,
                        "clip_r": clip_r, "clip_g": clip_g, "clip_b": clip_b,
                    }

                zoom = self.zoom_level

                # Zoom crop
                if zoom > 1.0:
                    crop_w = int(iw / zoom)
                    crop_h = int(ih / zoom)
                    x1 = (iw - crop_w) // 2
                    y1 = (ih - crop_h) // 2
                    frame = frame[y1:y1 + crop_h, x1:x1 + crop_w]

                # Convert uint16 to uint8 for display
                frame = self._to_uint8(frame)

                # Resize to fit canvas while preserving aspect ratio
                cw, ch = self._preview_size
                fh, fw = frame.shape[:2]
                scale = min(cw / fw, ch / fh)
                new_w = int(fw * scale)
                new_h = int(fh * scale)
                interp = cv2.INTER_NEAREST if zoom >= 4.0 else cv2.INTER_LINEAR
                frame = cv2.resize(frame, (new_w, new_h), interpolation=interp)

                self._pending_frame = frame
            except Exception as e:
                print(f"Capture error: {e}")
                time.sleep(0.1)

    def _display_loop(self):
        """Main thread: lightweight display of pre-resized frames."""
        if not self.live_running:
            return
        frame = self._pending_frame
        if frame is not None:
            # Corrections are fast on the small canvas-sized image
            frame = self._apply_corrections(frame)

            pil_img = Image.fromarray(frame)
            self._tk_img = ImageTk.PhotoImage(pil_img)
            self.canvas.delete("all")

            # Center image in canvas
            cw = self.canvas.winfo_width() or 1150
            ch_canvas = self.canvas.winfo_height() or 900
            img_w, img_h = pil_img.width, pil_img.height
            ox = (cw - img_w) // 2
            oy = (ch_canvas - img_h) // 2
            self.canvas.create_image(ox, oy, anchor=tk.NW, image=self._tk_img)

            # Crosshair at center of image
            cx = ox + img_w // 2
            cy = oy + img_h // 2
            self.canvas.create_line(cx - 30, cy, cx + 30, cy, fill="red", width=2)
            self.canvas.create_line(cx, cy - 30, cx, cy + 30, fill="red", width=2)
        # Draw histogram if available
        hist_data = self._pending_histogram
        if hist_data is not None:
            self._draw_histogram(hist_data)

        # Update preview size to track window resizes
        self._update_preview_size()
        self.root.after(50, self._display_loop)

    def _snapshot(self):
        """Single image acquisition — saves full-resolution in all formats."""
        if not self.camera:
            messagebox.showwarning("No Camera", "Camera not connected")
            return
        try:
            img = self.camera.capture()
            self._render_frame(img)
        except Exception as e:
            messagebox.showerror("Capture Error", str(e))
            return

        # Save in all formats to output folder
        def _do_save():
            try:
                import cv2
                from pathlib import Path

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                file_name = self.file_name_var.get().strip()
                if not file_name:
                    file_name = "snapshot"
                safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in file_name)

                base_folder = self.output_folder_var.get().strip()
                if not base_folder:
                    base_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_output")
                bit_label = "12bit" if img.dtype == np.uint16 else "8bit"
                output_dir = os.path.join(base_folder, f"{timestamp}_{safe_name}_{bit_label}")
                os.makedirs(output_dir, exist_ok=True)

                base = os.path.join(output_dir, f"{safe_name}_{bit_label}")

                # Convert to BGR for OpenCV saving
                if len(img.shape) == 3 and img.shape[2] == 3:
                    img_bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
                else:
                    img_bgr = img

                use_bigtiff = img.nbytes > 2_000_000_000

                # 1. Raw uncompressed TIFF
                raw_path = base + "_raw.tif"
                try:
                    import tifffile
                    tifffile.imwrite(raw_path, img, compression=None, bigtiff=use_bigtiff)
                except ImportError:
                    cv2.imwrite(raw_path, img_bgr)
                print(f"  Raw TIFF: {raw_path}")

                # 2. Compressed TIFF
                comp_path = base + ".tif"
                h, w = img.shape[:2]
                try:
                    import tifffile
                    tifffile.imwrite(comp_path, img, compression="zlib",
                                     tile=(min(512, h), min(512, w)), bigtiff=use_bigtiff)
                except ImportError:
                    cv2.imwrite(comp_path, img_bgr)
                print(f"  Compressed TIFF: {comp_path}")

                # 3. PNG
                png_path = base + ".png"
                cv2.imwrite(png_path, img_bgr, [cv2.IMWRITE_PNG_COMPRESSION, 3])
                print(f"  PNG: {png_path}")

                self.root.after(0, lambda: self.lbl_status.config(
                    text=f"Snapshot saved: {output_dir}"))
                print(f"Snapshot saved to {output_dir}")
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Save Error", str(e)))
                print(f"Snapshot save error: {e}")

        threading.Thread(target=_do_save, daemon=True).start()

    def _set_zoom(self, level_str):
        self.zoom_level = float(level_str.replace("x", ""))
        self.lbl_zoom.config(text=level_str)

    def _on_gain_change(self, val=None):
        if self._locked_gain is not None:
            return  # locked during scan
        gain = self.gain_var.get()
        self.lbl_gain.config(text=f"{gain:.1f} dB")
        if self.camera:
            threading.Thread(target=self._set_gain_bg, args=(gain,), daemon=True).start()

    def _set_gain_bg(self, gain):
        try:
            self.camera.set_gain(gain)
        except Exception as e:
            print(f"Gain error: {e}")

    def _on_exposure_change(self, val=None):
        if self._locked_exposure is not None:
            return  # locked during scan
        exp_ms = self.exposure_var.get()
        self.lbl_exposure.config(text=f"{exp_ms:.1f} ms")
        if self.camera:
            threading.Thread(target=self._set_exposure_bg, args=(exp_ms,), daemon=True).start()

    def _set_exposure_bg(self, exp_ms):
        try:
            self.camera.set_exposure(exp_ms * 1000)
        except Exception as e:
            print(f"Exposure error: {e}")

    def _toggle_bit_depth(self):
        """Toggle camera between 8-bit and 12-bit capture."""
        if not self.camera:
            return
        target = self.bit_depth_var.get()
        self.lbl_bit_depth.config(text=f"(switching to {target}-bit...)")

        def _do_switch():
            was_live = self.live_running
            if was_live:
                self.live_running = False
                time.sleep(0.3)  # let capture loop exit

            actual = self.camera.set_bit_depth(target)

            def _update_ui():
                self.bit_depth_var.set(actual)
                self.lbl_bit_depth.config(text=f"({actual}-bit)")
                # Update connection status label
                if self.connected:
                    port = self.com_var.get()
                    self.lbl_status.config(
                        text=f"Stage: {port} OK | Camera: {self.camera.model} ({actual}-bit)")
                if was_live:
                    self._toggle_live()
            self.root.after(0, _update_ui)

        threading.Thread(target=_do_switch, daemon=True).start()

    @staticmethod
    def _to_uint8(img):
        """Convert uint16 image to uint8 for display. Passes uint8 through unchanged."""
        if img is not None and img.dtype == np.uint16:
            return (img >> 8).astype(np.uint8)
        return img

    def _draw_histogram(self, hist_data):
        """Draw RGB histogram overlay and clipping indicator on hist_canvas."""
        canvas = self.hist_canvas
        canvas.delete("all")

        cw = canvas.winfo_width()
        ch = canvas.winfo_height()
        if cw < 20 or ch < 20:
            return

        hists = hist_data["hists"]  # [R, G, B] each 256 bins
        clip_pct = hist_data["clip_pct"]

        # Reserve bottom 6px for clipping bar
        bar_h = 6
        plot_h = ch - bar_h - 2

        # Find global max for log-scale normalization (skip bin 0)
        max_val = 1
        for h in hists:
            m = h[1:].max() if len(h) > 1 else 1
            if m > max_val:
                max_val = m
        log_max = math.log1p(max_val)

        # Draw each channel as a polyline
        colors = ["#ff4444", "#44ff44", "#4488ff"]
        for h, color in zip(hists, colors):
            points = []
            for i, count in enumerate(h):
                x = int(i / 255 * (cw - 1))
                if count > 0:
                    y = int(plot_h - (math.log1p(count) / log_max) * (plot_h - 4))
                else:
                    y = plot_h
                points.append(x)
                points.append(y)
            if len(points) >= 4:
                canvas.create_line(points, fill=color, width=1)

        # Clipping bar at bottom
        bar_y = ch - bar_h
        bar_color = "#ff2222" if clip_pct > CLIP_WARN_PCT else "#22aa22"
        canvas.create_rectangle(0, bar_y, cw, ch, fill=bar_color, outline="")

        # Update clipping label
        if clip_pct > CLIP_WARN_PCT:
            self.lbl_clip.config(
                text=f"CLIP: {clip_pct:.2f}%  R:{hist_data['clip_r']:.2f} G:{hist_data['clip_g']:.2f} B:{hist_data['clip_b']:.2f}",
                foreground="#ff4444")
        else:
            self.lbl_clip.config(text=f"Clip: {clip_pct:.2f}%", foreground="#44cc44")

    def _apply_corrections(self, img):
        """Apply image corrections to a numpy array. Returns corrected uint8 numpy array."""
        if img is None:
            return img

        # Convert to uint8 for display corrections (preserves original for saving)
        out = self._to_uint8(img).copy()

        # Auto Levels: stretch histogram to full 0-255 range
        if self.auto_levels_var.get():
            if out.ndim == 2:
                lo, hi = out.min(), out.max()
                if hi > lo:
                    out = ((out.astype(np.float32) - lo) / (hi - lo) * 255).clip(0, 255).astype(np.uint8)
            else:
                for ch in range(out.shape[2]):
                    lo, hi = out[:, :, ch].min(), out[:, :, ch].max()
                    if hi > lo:
                        out[:, :, ch] = ((out[:, :, ch].astype(np.float32) - lo) / (hi - lo) * 255).clip(0, 255).astype(np.uint8)

        # CLAHE (Contrast Limited Adaptive Histogram Equalization)
        if self.clahe_var.get():
            try:
                import cv2
                clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
                if out.ndim == 2:
                    out = clahe.apply(out)
                else:
                    lab = cv2.cvtColor(out, cv2.COLOR_RGB2LAB)
                    lab[:, :, 0] = clahe.apply(lab[:, :, 0])
                    out = cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)
            except ImportError:
                pass  # cv2 not available, skip CLAHE

        # Brightness (additive offset)
        brightness = self.brightness_var.get()
        if brightness != 0:
            out = (out.astype(np.float32) + brightness).clip(0, 255).astype(np.uint8)

        # Contrast (multiply around midpoint 128)
        contrast = self.contrast_var.get()
        if contrast != 1.0:
            out = ((out.astype(np.float32) - 128) * contrast + 128).clip(0, 255).astype(np.uint8)

        # Gamma correction
        gamma = self.gamma_var.get()
        if gamma != 1.0:
            inv_gamma = 1.0 / gamma
            table = (np.arange(256, dtype=np.float32) / 255.0) ** inv_gamma * 255
            lut = table.clip(0, 255).astype(np.uint8)
            out = lut[out]

        return out

    def _reset_corrections(self):
        """Reset all image correction sliders to defaults."""
        self.brightness_var.set(0.0)
        self.contrast_var.set(1.0)
        self.gamma_var.set(1.0)
        self.auto_levels_var.set(False)
        self.clahe_var.set(False)

    def _render_frame(self, img):
        if img is None:
            return

        canvas_w = self.canvas.winfo_width() or 1150
        canvas_h = self.canvas.winfo_height() or 900

        ih, iw = img.shape[:2]
        if self.zoom_level > 1.0:
            crop_w = int(iw / self.zoom_level)
            crop_h = int(ih / self.zoom_level)
            x1 = (iw - crop_w) // 2
            y1 = (ih - crop_h) // 2
            img = img[y1:y1 + crop_h, x1:x1 + crop_w]

        # Apply image corrections for preview
        img = self._apply_corrections(img)

        pil_img = Image.fromarray(img)
        # Preserve aspect ratio
        iw, ih = pil_img.size
        scale = min(canvas_w / iw, canvas_h / ih)
        new_w = int(iw * scale)
        new_h = int(ih * scale)
        resample = Image.NEAREST if self.zoom_level >= 4.0 else Image.BILINEAR
        pil_img = pil_img.resize((new_w, new_h), resample)

        self._tk_img = ImageTk.PhotoImage(pil_img)
        self.canvas.delete("all")

        # Center image in canvas
        ox = (canvas_w - new_w) // 2
        oy = (canvas_h - new_h) // 2
        self.canvas.create_image(ox, oy, anchor=tk.NW, image=self._tk_img)

        cx = ox + new_w // 2
        cy = oy + new_h // 2
        ch_len = 30
        self.canvas.create_line(cx - ch_len, cy, cx + ch_len, cy, fill="red", width=2)
        self.canvas.create_line(cx, cy - ch_len, cx, cy + ch_len, fill="red", width=2)

    # ─── Stage Movement ────────────────────────────────────────────────

    SPEED_PRESETS = {
        "1-Step": 85,
        "Nudge":  150,
        "Crawl":  500,
        "Slow":   5000,
        "Fast":   8000,
    }

    def _jog_start(self, dx, dy):
        if not self.connected or not self.stage:
            return
        self._jog_repeating = True
        # Read tkinter vars on main thread, then do serial I/O in background
        speed = self.SPEED_PRESETS.get(self.speed_var.get(), 5000)
        jdx = -dx if self.invert_x_var.get() else dx
        jdy = -dy if self.invert_y_var.get() else dy

        def _do_jog():
            with self._stage_lock:
                try:
                    self.stage.set_speed(speed)
                    self.stage.move_relative(jdx * 10_000_000, jdy * 10_000_000)
                except Exception as e:
                    print(f"Move error: {e}")
        threading.Thread(target=_do_jog, daemon=True).start()

    def _jog_stop(self):
        self._jog_repeating = False
        if not self.connected or not self.stage:
            return

        def _do_halt():
            with self._stage_lock:
                try:
                    self.stage.halt()
                except Exception as e:
                    print(f"Halt error: {e}")
        threading.Thread(target=_do_halt, daemon=True).start()

    # ─── Click-to-Drive ─────────────────────────────────────────────

    def _on_canvas_double_click(self, event):
        """Double-click on the camera preview to drive the stage so that
        the clicked point becomes the new center of the FOV."""
        if not self.connected or not self.stage:
            return
        if self._scanning:
            return

        canvas_w = self.canvas.winfo_width() or 1150
        canvas_h = self.canvas.winfo_height() or 900

        # The displayed image preserves aspect ratio and is centered
        cam_w = CAMERA_WIDTH_PX / self.zoom_level
        cam_h = CAMERA_HEIGHT_PX / self.zoom_level
        scale = min(canvas_w / cam_w, canvas_h / cam_h)
        img_w = cam_w * scale
        img_h = cam_h * scale
        ox = (canvas_w - img_w) / 2
        oy = (canvas_h - img_h) / 2

        # Offset from image center (pixels on screen)
        offset_canvas_x = event.x - (ox + img_w / 2)
        offset_canvas_y = event.y - (oy + img_h / 2)

        # Convert to camera sensor pixels
        scale_x = cam_w / img_w
        scale_y = cam_h / img_h

        offset_cam_x = offset_canvas_x * scale_x  # pixels on sensor
        offset_cam_y = offset_canvas_y * scale_y

        # Convert to microns
        um_per_pixel = OBJECTIVES[self.obj_var.get()]["um_per_pixel"]
        offset_um_x = offset_cam_x * um_per_pixel
        offset_um_y = offset_cam_y * um_per_pixel

        # Convert to stage steps
        # Microscope optics invert X between screen and stage coordinates,
        # so negate X to match the arrow button direction convention.
        raw_steps_x = -int(round(offset_um_x * self.steps_per_um))
        raw_steps_y = int(round(offset_um_y * self.steps_per_um))

        move_x = -raw_steps_x if self.invert_x_var.get() else raw_steps_x
        move_y = -raw_steps_y if self.invert_y_var.get() else raw_steps_y

        inv_x = "inv" if self.invert_x_var.get() else "norm"
        inv_y = "inv" if self.invert_y_var.get() else "norm"
        print(f"[CLICK] ({event.x},{event.y}) raw=({raw_steps_x},{raw_steps_y}) "
              f"move=({move_x},{move_y}) X:{inv_x} Y:{inv_y}")

        def _do_move():
            with self._stage_lock:
                try:
                    self.stage.move_relative(move_x, move_y)
                except Exception as e:
                    print(f"Click-to-drive error: {e}")
        threading.Thread(target=_do_move, daemon=True).start()

    # ─── Define Scan Area (named corners) ───────────────────────────

    def _mark_named_corner(self, name):
        """Mark a named die corner (UL/UR/LL/LR) at current stage position."""
        if not self.stage:
            messagebox.showwarning("Not Connected", "Connect to stage first")
            return

        def _do_mark():
            try:
                with self._stage_lock:
                    pos = self.stage.get_position()
            except Exception as e:
                # Never mark a corner from a failed/garbled position read —
                # a wrong corner silently corrupts the whole scan area.
                print(f"Mark corner error: {e}")
                self.root.after(0, lambda: messagebox.showerror(
                    "Mark Corner Failed",
                    f"Could not read stage position:\n{e}\n\n"
                    "Corner NOT marked. Try again."))
                return
            corner = (pos.x, pos.y)
            um_x = pos.x / self.steps_per_um
            um_y = pos.y / self.steps_per_um
            txt = f"{name}: ({pos.x}, {pos.y}) = ({um_x:.0f}, {um_y:.0f}) \u00b5m"

            def _update_ui():
                if name == "UL":
                    self.corner_ul = corner
                    self.lbl_corner_ul.config(text=txt)
                elif name == "UR":
                    self.corner_ur = corner
                    self.lbl_corner_ur.config(text=txt)
                elif name == "LL":
                    self.corner_ll = corner
                    self.lbl_corner_ll.config(text=txt)
                elif name == "LR":
                    self.corner_lr = corner
                    self.lbl_corner_lr.config(text=txt)
                self._update_area()
                self._update_preview()
            self.root.after(0, _update_ui)

        threading.Thread(target=_do_mark, daemon=True).start()

    def _clear_corners(self):
        self.corner_ul = None
        self.corner_ur = None
        self.corner_ll = None
        self.corner_lr = None
        self.lbl_corner_ul.config(text="UL: not set")
        self.lbl_corner_ur.config(text="UR: not set")
        self.lbl_corner_ll.config(text="LL: not set")
        self.lbl_corner_lr.config(text="LR: not set")
        self.lbl_area.config(text="")
        self.lbl_preview.config(text="Mark UL and LR corners to see preview")

    def _get_set_corners(self):
        """Return list of corners that have been marked."""
        return [c for c in [self.corner_ul, self.corner_ur,
                            self.corner_ll, self.corner_lr] if c is not None]

    def _corner_check(self):
        """Drive stage to each marked corner in clockwise order for visual verification."""
        if not self.connected or not self.stage:
            messagebox.showwarning("Not Connected", "Connect to stage first")
            return
        if self._scanning:
            return

        # Gather marked corners in clockwise order: UL → UR → LR → LL
        corners = []
        for name, pos in [("UL", self.corner_ul), ("UR", self.corner_ur),
                          ("LR", self.corner_lr), ("LL", self.corner_ll)]:
            if pos is not None:
                corners.append((name, pos))

        if len(corners) < 2:
            messagebox.showwarning("Need Corners", "Mark at least 2 corners first")
            return

        def _do_check():
            for name, (cx, cy) in corners:
                self.root.after(0, lambda n=name: self.lbl_scan_status.config(
                    text=f"Corner Check: driving to {n}..."))
                with self._stage_lock:
                    try:
                        self.stage.move_absolute(cx, cy, wait=False)
                    except Exception as e:
                        print(f"Corner check move error: {e}")
                        continue
                # Wait for arrival
                for _ in range(300):  # up to 60 seconds
                    time.sleep(0.2)
                    with self._stage_lock:
                        try:
                            if not self.stage.is_busy():
                                break
                        except Exception:
                            pass
                self.root.after(0, lambda n=name: self.lbl_scan_status.config(
                    text=f"Corner Check: at {n} — verify in preview"))
                time.sleep(2.0)  # pause for visual verification

            self.root.after(0, lambda: self.lbl_scan_status.config(
                text="Corner Check complete"))

        threading.Thread(target=_do_check, daemon=True).start()

    def _get_scan_bounds(self):
        """Get the scan bounding box from marked corners.
        Returns (min_x, max_x, min_y, max_y) in steps, or None."""
        marked = self._get_set_corners()
        if len(marked) < 2:
            return None
        xs = [c[0] for c in marked]
        ys = [c[1] for c in marked]
        return min(xs), max(xs), min(ys), max(ys)

    def _update_area(self):
        bounds = self._get_scan_bounds()
        if bounds is None:
            self.lbl_area.config(text="")
            return
        min_x, max_x, min_y, max_y = bounds
        dx_um = (max_x - min_x) / self.steps_per_um
        dy_um = (max_y - min_y) / self.steps_per_um
        n = len(self._get_set_corners())
        self.lbl_area.config(
            text=f"Die area ({n}/4 corners): {dx_um:.0f} \u00d7 {dy_um:.0f} \u00b5m  "
                 f"({dx_um / 1000:.2f} \u00d7 {dy_um / 1000:.2f} mm)"
        )

    # ─── Scan Calculation ──────────────────────────────────────────────

    def _detect_axis_directions(self):
        """Detect which direction stage X/Y maps to visual right/down.

        Uses named corners to figure out the relationship:
          - If UL has higher stage X than UR → stage +X = visual LEFT (x_sign = -1)
          - If UL has lower stage Y than LL  → stage +Y = visual DOWN  (y_sign = +1)

        Returns (x_sign, y_sign) where:
          x_sign: +1 if +stage_X = visual right, -1 if +stage_X = visual left
          y_sign: +1 if +stage_Y = visual down,  -1 if +stage_Y = visual up
        """
        x_sign = 1  # default
        y_sign = 1  # default

        # Determine X direction from left/right corner pairs
        left_corners = [c for c in [self.corner_ul, self.corner_ll] if c]
        right_corners = [c for c in [self.corner_ur, self.corner_lr] if c]
        if left_corners and right_corners:
            avg_left_x = sum(c[0] for c in left_corners) / len(left_corners)
            avg_right_x = sum(c[0] for c in right_corners) / len(right_corners)
            x_sign = 1 if avg_right_x > avg_left_x else -1

        # Determine Y direction from upper/lower corner pairs
        upper_corners = [c for c in [self.corner_ul, self.corner_ur] if c]
        lower_corners = [c for c in [self.corner_ll, self.corner_lr] if c]
        if upper_corners and lower_corners:
            avg_upper_y = sum(c[1] for c in upper_corners) / len(upper_corners)
            avg_lower_y = sum(c[1] for c in lower_corners) / len(lower_corners)
            y_sign = 1 if avg_lower_y > avg_upper_y else -1

        return x_sign, y_sign

    def _calc_scan_params(self):
        """Calculate all scan parameters from current settings.
        Returns dict with all needed values, or None if <2 corners set.

        Auto-detects stage axis directions from the named corners so
        the scan always goes visual left→right (col 0→N) and
        visual top→bottom (row 0→N).
        """
        bounds = self._get_scan_bounds()
        if bounds is None:
            return None
        min_x, max_x, min_y, max_y = bounds

        obj_name = self.obj_var.get()
        um_per_pixel = OBJECTIVES[obj_name]["um_per_pixel"]
        overlap_pct = self.overlap_var.get()
        overscan_pct = self.overscan_var.get()

        # Die size in µm
        die_w_um = (max_x - min_x) / self.steps_per_um
        die_h_um = (max_y - min_y) / self.steps_per_um

        # Add overscan margin
        margin_x_um = die_w_um * (overscan_pct / 100)
        margin_y_um = die_h_um * (overscan_pct / 100)

        scan_w_um = die_w_um + 2 * margin_x_um
        scan_h_um = die_h_um + 2 * margin_y_um

        # FOV in µm
        fov_w_um = CAMERA_WIDTH_PX * um_per_pixel
        fov_h_um = CAMERA_HEIGHT_PX * um_per_pixel

        # Effective step between tiles (magnitude, always positive)
        step_x_um = fov_w_um * (1 - overlap_pct / 100)
        step_y_um = fov_h_um * (1 - overlap_pct / 100)

        # Grid size — if scan area fits within one FOV, use 1 tile.
        # Otherwise, first tile covers one full FOV and we add tiles
        # for the remaining distance.
        if scan_w_um <= fov_w_um:
            cols = 1
        else:
            cols = math.ceil((scan_w_um - fov_w_um) / step_x_um) + 1
        if scan_h_um <= fov_h_um:
            rows = 1
        else:
            rows = math.ceil((scan_h_um - fov_h_um) / step_y_um) + 1

        # Detect axis directions from named corners
        x_sign, y_sign = self._detect_axis_directions()

        # Step sizes in motor steps (signed so col++ = visual right, row++ = visual down)
        step_x_steps = int(round(step_x_um * self.steps_per_um)) * x_sign
        step_y_steps = int(round(step_y_um * self.steps_per_um)) * y_sign

        # Center the tile grid over the die.
        # Grid positions: origin, origin+step, ..., origin+(N-1)*step
        # Grid center = origin + (N-1)*step/2
        # We want grid center = die center = (min+max)/2
        # So: origin = die_center - (N-1)*step/2
        center_x = (min_x + max_x) / 2
        center_y = (min_y + max_y) / 2
        origin_x = int(round(center_x - (cols - 1) * step_x_steps / 2))
        origin_y = int(round(center_y - (rows - 1) * step_y_steps / 2))

        return {
            "obj_name": obj_name,
            "um_per_pixel": um_per_pixel,
            "overlap_pct": overlap_pct,
            "overscan_pct": overscan_pct,
            "die_w_um": die_w_um,
            "die_h_um": die_h_um,
            "scan_w_um": scan_w_um,
            "scan_h_um": scan_h_um,
            "fov_w_um": fov_w_um,
            "fov_h_um": fov_h_um,
            "step_x_um": step_x_um,
            "step_y_um": step_y_um,
            "cols": cols,
            "rows": rows,
            "total_tiles": cols * rows,
            "origin_x": origin_x,
            "origin_y": origin_y,
            "step_x_steps": step_x_steps,
            "step_y_steps": step_y_steps,
            "x_sign": x_sign,
            "y_sign": y_sign,
            "settle_time": self.settle_var.get(),
        }

    def _update_preview(self):
        params = self._calc_scan_params()
        if params is None:
            self.lbl_preview.config(text="Mark UL and LR corners to see preview")
            return

        est_time_s = params["total_tiles"] * (0.5 + params["settle_time"])
        est_min = est_time_s / 60

        # Total coverage
        total_w = params["fov_w_um"] + (params["cols"] - 1) * params["step_x_um"]
        total_h = params["fov_h_um"] + (params["rows"] - 1) * params["step_y_um"]

        x_dir = "inverted" if params["x_sign"] < 0 else "normal"
        y_dir = "inverted" if params["y_sign"] < 0 else "normal"

        self.lbl_preview.config(
            text=f"Objective: {params['obj_name']}  |  "
                 f"FOV: {params['fov_w_um']:.0f} \u00d7 {params['fov_h_um']:.0f} \u00b5m\n"
                 f"Die: {params['die_w_um']:.0f} \u00d7 {params['die_h_um']:.0f} \u00b5m  "
                 f"({params['die_w_um']/1000:.1f} \u00d7 {params['die_h_um']/1000:.1f} mm)\n"
                 f"Total coverage: {total_w:.0f} \u00d7 {total_h:.0f} \u00b5m "
                 f"({total_w/1000:.1f} \u00d7 {total_h/1000:.1f} mm)\n"
                 f"Grid: {params['cols']} cols \u00d7 {params['rows']} rows  =  "
                 f"{params['total_tiles']} tiles\n"
                 f"Step: {params['step_x_um']:.0f} \u00d7 {params['step_y_um']:.0f} \u00b5m  "
                 f"({params['overlap_pct']:.0f}% overlap)\n"
                 f"Axes: X {x_dir}, Y {y_dir}\n"
                 f"Estimated time: ~{est_min:.1f} min"
        )

    # ─── Scan Execution ────────────────────────────────────────────────

    def _start_scan(self):
        if self._scanning:
            return
        if not self.stage or not self.camera:
            messagebox.showwarning("Not Connected", "Connect stage and camera first")
            return

        params = self._calc_scan_params()
        if params is None:
            messagebox.showwarning("No Area", "Mark at least 2 corners first")
            return

        n_corners = len(self._get_set_corners())
        # Confirm
        msg = (f"Ready to scan {params['total_tiles']} tiles "
               f"({params['cols']}×{params['rows']})?\n\n"
               f"Corners marked: {n_corners}/4\n"
               f"Die: {params['die_w_um']:.0f} × {params['die_h_um']:.0f} µm\n"
               f"Scan area: {params['scan_w_um']:.0f} × {params['scan_h_um']:.0f} µm\n"
               f"Objective: {params['obj_name']}\n"
               f"Overlap: {params['overlap_pct']:.0f}%")
        if not messagebox.askyesno("Confirm Scan", msg):
            return

        # Prompt for output folder and file name
        from tkinter import filedialog

        # Build default subfolder name from file name + timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_name = self.file_name_var.get().strip()
        if not file_name:
            file_name = "stitched"
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in file_name)
        bit_label = f"{self.camera.get_bit_depth()}bit" if self.camera else "8bit"
        default_subfolder = f"{timestamp}_{safe_name}_{bit_label}"
        base_folder = self.output_folder_var.get().strip()
        if not base_folder:
            base_folder = os.path.join(os.path.dirname(__file__), "scan_output")
        default_dir = os.path.join(base_folder, default_subfolder)

        # Let user confirm or change output folder
        output_dir = filedialog.askdirectory(
            initialdir=base_folder,
            title="Select or Confirm Output Folder (a subfolder will be created)",
        )
        if not output_dir:
            return  # user cancelled

        # Create a timestamped subfolder inside selected folder
        output_dir = os.path.join(output_dir, default_subfolder)
        os.makedirs(output_dir, exist_ok=True)

        # Store the chosen file name for the stitch output
        self._stitch_file_name = safe_name

        self._scanning = True
        self._abort_scan = False
        self._lock_camera_settings()
        self.btn_scan.config(state=tk.DISABLED)
        self.btn_step_focus.config(state=tk.DISABLED)
        self.btn_abort.config(state=tk.NORMAL)

        # Stop live view during scan to free camera
        was_live = self.live_running
        if was_live:
            self.live_running = False
            self.btn_live.config(text="Start Live")
            time.sleep(0.2)

        threading.Thread(target=self._scan_thread,
                         args=(params, output_dir, was_live),
                         daemon=True).start()

    def _start_step_focus(self):
        """Start a step-and-focus scan: move to each tile, let user adjust focus, then capture."""
        if self._scanning:
            return
        if not self.stage or not self.camera:
            messagebox.showwarning("Not Connected", "Connect stage and camera first")
            return

        params = self._calc_scan_params()
        if params is None:
            messagebox.showwarning("No Area", "Mark at least 2 corners first")
            return

        n_corners = len(self._get_set_corners())
        msg = (f"Step & Focus: {params['total_tiles']} tiles "
               f"({params['cols']}×{params['rows']})\n\n"
               f"Corners marked: {n_corners}/4\n"
               f"Die: {params['die_w_um']:.0f} × {params['die_h_um']:.0f} µm\n"
               f"Objective: {params['obj_name']}\n"
               f"Overlap: {params['overlap_pct']:.0f}%\n\n"
               f"The stage will move to each tile position and pause.\n"
               f"Adjust focus, then click 'Focus Confirmed' to capture.")
        if not messagebox.askyesno("Confirm Step & Focus", msg):
            return

        # Prompt for output folder
        from tkinter import filedialog
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_name = self.file_name_var.get().strip()
        if not file_name:
            file_name = "stitched"
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in file_name)
        bit_label = f"{self.camera.get_bit_depth()}bit" if self.camera else "8bit"
        default_subfolder = f"{timestamp}_{safe_name}_{bit_label}"
        base_folder = self.output_folder_var.get().strip()
        if not base_folder:
            base_folder = os.path.join(os.path.dirname(__file__), "scan_output")

        output_dir = filedialog.askdirectory(
            initialdir=base_folder,
            title="Select or Confirm Output Folder (a subfolder will be created)",
        )
        if not output_dir:
            return
        output_dir = os.path.join(output_dir, default_subfolder)
        os.makedirs(output_dir, exist_ok=True)

        self._stitch_file_name = safe_name
        self._scanning = True
        self._abort_scan = False
        self._step_focus_mode = True
        self._focus_confirmed.clear()
        # Don't lock gain/exposure yet — user adjusts on tile 1, locks after capture
        self.btn_scan.config(state=tk.DISABLED)
        self.btn_step_focus.config(state=tk.DISABLED)
        self.btn_abort.config(state=tk.NORMAL)

        threading.Thread(target=self._step_focus_thread,
                         args=(params, output_dir),
                         daemon=True).start()

    def _step_focus_thread(self, params, output_dir):
        """Step-and-focus scan thread: move, show live, wait for user, capture."""
        print(f"[STEP&FOCUS] Starting: {params['cols']}x{params['rows']} = {params['total_tiles']} tiles")
        total = params["total_tiles"]
        cols = params["cols"]
        rows = params["rows"]
        tiles_info = []
        start_time = time.time()

        # Initialize mosaic preview
        self.root.after(0, self._init_mosaic, params)
        time.sleep(0.3)

        # Set scan speed
        try:
            self.stage.set_speed(50000)
            time.sleep(0.1)
        except Exception:
            pass

        tile_index = 0
        for row in range(rows):
            # Typewriter (raster): every row scans left-to-right
            col_range = range(cols)

            for col in col_range:
                if self._abort_scan:
                    self._ui_update(f"Step & Focus aborted at tile {tile_index}/{total}")
                    break

                target_x = params["origin_x"] + col * params["step_x_steps"]
                target_y = params["origin_y"] + row * params["step_y_steps"]
                x_um = col * params["step_x_um"]
                y_um = row * params["step_y_um"]

                # Update timer
                elapsed = time.time() - start_time
                timer_text = f"Elapsed: {int(elapsed // 60)}:{int(elapsed % 60):02d}"
                self.root.after(0, lambda t=timer_text: self.lbl_timer.config(text=t))

                self._ui_update(
                    f"Tile {tile_index + 1}/{total}  "
                    f"(row {row}, col {col})  — Moving...",
                    progress=(tile_index / total) * 100,
                )

                # Move stage
                print(f"[STEP&FOCUS] Tile {tile_index}: moving to ({target_x}, {target_y})")
                try:
                    self.stage.move_absolute(target_x, target_y, wait=False)
                    if not self._wait_stage_interruptible():
                        break
                except Exception as e:
                    print(f"[STEP&FOCUS] Move error: {e}")
                    time.sleep(0.5)

                if self._abort_scan:
                    break

                # Settle
                time.sleep(params["settle_time"])

                # Start live view so user can see and adjust focus
                if not self.live_running:
                    self.root.after(0, self._toggle_live)
                    time.sleep(0.3)  # let live view start

                # Show the green button and info label
                self._ui_update(
                    f"Tile {tile_index + 1}/{total}  (row {row}, col {col})",
                    progress=(tile_index / total) * 100,
                )
                def _show_focus_ui(idx=tile_index, tot=total, r=row, c=col):
                    self.lbl_step_focus_info.config(
                        text=f"Tile {idx + 1}/{tot} (row {r}, col {c}) — "
                             f"Adjust focus, then click Focus Confirmed")
                    self._focus_frame.pack(fill=tk.X, pady=3, before=self._btn_row)
                self.root.after(0, _show_focus_ui)

                # Wait for user to click "Focus Confirmed" (or abort)
                self._focus_confirmed.clear()
                print(f"[STEP&FOCUS] Tile {tile_index}: waiting for focus confirmation...")
                while not self._focus_confirmed.is_set():
                    if self._abort_scan:
                        break
                    self._focus_confirmed.wait(timeout=0.2)

                if self._abort_scan:
                    break

                # Hide the green button
                def _hide_focus_ui():
                    self._focus_frame.pack_forget()
                self.root.after(0, _hide_focus_ui)

                # Stop live view before capture for clean frame
                if self.live_running:
                    self.root.after(0, self._toggle_live)
                    time.sleep(0.3)  # let live view stop

                # Capture
                filename = f"tile_r{row:03d}_c{col:03d}.tif"
                filepath = os.path.join(output_dir, filename)
                captured = False

                try:
                    # After first tile capture, lock gain/exposure for all remaining
                    if tile_index == 0:
                        self.root.after(0, self._lock_camera_settings)
                    self._apply_locked_camera_settings()
                    print(f"[STEP&FOCUS] Tile {tile_index}: capturing...")
                    tile_img = self.camera.capture()
                    if self.save_corrected_var.get():
                        tile_img = self._apply_corrections(tile_img)
                    self._save_tile_async(filepath, tile_img)
                    captured = True
                    print(f"[STEP&FOCUS] Tile {tile_index}: captured OK")
                    self._place_tile_on_mosaic(tile_img, col, row)
                except Exception as e:
                    print(f"[STEP&FOCUS] Capture error: {e}")

                tiles_info.append({
                    "row": row,
                    "col": col,
                    "index": tile_index,
                    "stage_x": target_x,
                    "stage_y": target_y,
                    "x_um": round(x_um, 2),
                    "y_um": round(y_um, 2),
                    "filename": filename,
                    "captured": captured,
                })

                tile_index += 1

            if self._abort_scan:
                break

        # Hide focus UI in case it's still showing
        def _cleanup_focus_ui():
            self._focus_frame.pack_forget()
        self.root.after(0, _cleanup_focus_ui)

        # Stop live view if still running
        if self.live_running:
            self.root.after(0, self._toggle_live)
            time.sleep(0.3)

        # Wait for background saves
        time.sleep(0.5)

        elapsed = time.time() - start_time
        captured_count = sum(1 for t in tiles_info if t["captured"])

        # Save metadata
        self._save_metadata(output_dir, params, tiles_info, elapsed)
        self._save_tile_config(output_dir, params, tiles_info)

        scan_min, scan_sec = divmod(int(elapsed), 60)
        self.root.after(0, lambda: self.lbl_timer.config(
            text=f"Step & Focus: {scan_min}:{scan_sec:02d}"))

        # Auto-stitch
        if not self._abort_scan and captured_count > 0:
            try:
                self._ui_update("Stitching tiles...", progress=100)
                self.root.after(0, lambda: self.lbl_timer.config(
                    text=f"Scan: {scan_min}:{scan_sec:02d}  |  Stitching..."))
                stitch_start = time.time()
                stitcher = Stitcher(output_dir)
                stitch_name = getattr(self, '_stitch_file_name', 'stitched')
                output_path = stitcher.stitch(output_path=f"{stitch_name}.tif",
                                              align=True, blend=True,
                                              correct_vignetting=False,
                                              match_brightness=True)
                stitch_elapsed = time.time() - stitch_start
                stitch_min, stitch_sec = divmod(int(stitch_elapsed), 60)
                total_elapsed = elapsed + stitch_elapsed
                total_min, total_sec = divmod(int(total_elapsed), 60)
                self.root.after(0, lambda: self.lbl_timer.config(
                    text=f"Scan: {scan_min}:{scan_sec:02d}  |  "
                         f"Stitch: {stitch_min}:{stitch_sec:02d}  |  "
                         f"Total: {total_min}:{total_sec:02d}"))
                self._ui_update(
                    f"Done! {captured_count} tiles in {elapsed:.1f}s\n"
                    f"Stitched: {output_path}",
                    progress=100,
                )
            except Exception as e:
                print(f"Stitch error: {e}")
                self._ui_update(
                    f"Step & Focus done ({captured_count} tiles) but stitch failed: {e}",
                    progress=100,
                )
        else:
            self._ui_update(
                f"Step & Focus {'aborted' if self._abort_scan else 'complete'}: "
                f"{captured_count}/{total} tiles in {elapsed:.1f}s\n"
                f"Saved to: {output_dir}",
                progress=100,
            )

        self._scanning = False
        self._step_focus_mode = False
        self._unlock_camera_settings()
        self.root.after(0, lambda: self.btn_scan.config(state=tk.NORMAL))
        self.root.after(0, lambda: self.btn_step_focus.config(state=tk.NORMAL))
        self.root.after(0, lambda: self.btn_abort.config(state=tk.DISABLED))

    def _on_focus_confirmed(self):
        """Called when user clicks the green Focus Confirmed button."""
        self._focus_confirmed.set()

    def _wait_stage_interruptible(self, timeout=60.0):
        """Wait for stage to finish moving, checking abort every 200ms.
        Uses slower polling to avoid flooding the serial port."""
        start = time.time()
        while time.time() - start < timeout:
            if self._abort_scan:
                try:
                    self.stage.halt()
                except Exception:
                    pass
                return False
            time.sleep(0.2)  # don't poll too fast
            try:
                if not self.stage.is_busy():
                    return True
            except Exception:
                pass
        return True  # timed out, keep going

    def _save_tile_async(self, filepath, tile_img):
        """Save a tile image in a background thread so it doesn't block scanning."""
        threading.Thread(target=self._do_save_tile, args=(filepath, tile_img),
                         daemon=True).start()

    def _do_save_tile(self, filepath, tile_img):
        try:
            self.camera.save(filepath, tile_img)
        except Exception as e:
            print(f"Save error {filepath}: {e}")

    # ─── Real-time Mosaic Preview ──────────────────────────────────

    def _init_mosaic(self, params):
        """Create a blank mosaic canvas sized to fit all tiles, downscaled
        to fit the camera preview area."""
        cols, rows = params["cols"], params["rows"]
        # Full mosaic size in pixels (at camera resolution)
        full_w = int(CAMERA_WIDTH_PX + (cols - 1) * (params["step_x_um"] / params["um_per_pixel"]))
        full_h = int(CAMERA_HEIGHT_PX + (rows - 1) * (params["step_y_um"] / params["um_per_pixel"]))

        # Scale to fit preview canvas
        canvas_w = self.canvas.winfo_width() or 1150
        canvas_h = self.canvas.winfo_height() or 900
        self._mosaic_scale = min(canvas_w / full_w, canvas_h / full_h)
        self._mosaic_w = int(full_w * self._mosaic_scale)
        self._mosaic_h = int(full_h * self._mosaic_scale)
        self._mosaic_tile_w = max(1, int(CAMERA_WIDTH_PX * self._mosaic_scale))
        self._mosaic_tile_h = max(1, int(CAMERA_HEIGHT_PX * self._mosaic_scale))
        self._mosaic_step_x = params["step_x_um"] / params["um_per_pixel"] * self._mosaic_scale
        self._mosaic_step_y = params["step_y_um"] / params["um_per_pixel"] * self._mosaic_scale

        # Create blank canvas (dark gray)
        self._mosaic_img = Image.new("RGB", (self._mosaic_w, self._mosaic_h), (30, 30, 30))

        # Draw grid lines to show expected tile positions
        from PIL import ImageDraw
        draw = ImageDraw.Draw(self._mosaic_img)
        for r in range(rows):
            for c in range(cols):
                x = int(c * self._mosaic_step_x)
                y = int(r * self._mosaic_step_y)
                draw.rectangle(
                    [x, y, x + self._mosaic_tile_w - 1, y + self._mosaic_tile_h - 1],
                    outline=(60, 60, 60), width=1
                )

        self._show_mosaic()

    def _place_tile_on_mosaic(self, tile_img, col, row):
        """Place a captured tile onto the mosaic preview at its grid position."""
        if self._mosaic_img is None:
            return
        # Downscale tile to mosaic size (convert uint16→uint8 for display)
        pil_tile = Image.fromarray(self._to_uint8(tile_img))
        thumb = pil_tile.resize((self._mosaic_tile_w, self._mosaic_tile_h), Image.BILINEAR)

        # Paste at grid position
        x = int(col * self._mosaic_step_x)
        y = int(row * self._mosaic_step_y)
        self._mosaic_img.paste(thumb, (x, y))

        self.root.after(0, self._show_mosaic)

    def _show_mosaic(self):
        """Render the mosaic canvas to the preview area."""
        if self._mosaic_img is None:
            return
        canvas_w = self.canvas.winfo_width() or 1150
        canvas_h = self.canvas.winfo_height() or 900

        # Center mosaic in canvas
        display = self._mosaic_img.copy()
        if display.width != canvas_w or display.height != canvas_h:
            bg = Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0))
            ox = (canvas_w - display.width) // 2
            oy = (canvas_h - display.height) // 2
            bg.paste(display, (ox, oy))
            display = bg

        self._tk_img = ImageTk.PhotoImage(display)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self._tk_img)

    # ─── Scan Thread ───────────────────────────────────────────────

    def _scan_thread(self, params, output_dir, resume_live):
        """Execute the tile scan with real-time mosaic preview.

        Serial discipline: only ONE serial command at a time, with
        small delays between commands to let the MAC2000 process them.
        Position polling is disabled during scan.
        """
        print(f"[SCAN] Starting scan thread: {params['cols']}x{params['rows']} = {params['total_tiles']} tiles")
        print(f"[SCAN] Origin: ({params['origin_x']}, {params['origin_y']})")
        print(f"[SCAN] Steps: ({params['step_x_steps']}, {params['step_y_steps']})")
        print(f"[SCAN] Output: {output_dir}")
        total = params["total_tiles"]
        cols = params["cols"]
        rows = params["rows"]
        tiles_info = []
        start_time = time.time()

        # Initialize mosaic preview
        self.root.after(0, self._init_mosaic, params)
        time.sleep(0.3)  # let mosaic render

        # Set scan speed — not too fast to avoid serial issues
        try:
            self.stage.set_speed(50000)
            time.sleep(0.1)
        except Exception:
            pass

        tile_index = 0
        for row in range(rows):
            # Typewriter (raster): every row scans left-to-right
            col_range = range(cols)

            for col in col_range:
                if self._abort_scan:
                    self._ui_update(f"Scan aborted at tile {tile_index}/{total}")
                    break

                # Target position in steps
                target_x = params["origin_x"] + col * params["step_x_steps"]
                target_y = params["origin_y"] + row * params["step_y_steps"]

                # Position in µm (relative to scan origin)
                x_um = col * params["step_x_um"]
                y_um = row * params["step_y_um"]

                # Update UI with timer
                elapsed = time.time() - start_time
                if tile_index > 0:
                    avg_per_tile = elapsed / tile_index
                    remaining = avg_per_tile * (total - tile_index)
                    eta_min, eta_sec = divmod(int(remaining), 60)
                    timer_text = (f"Elapsed: {int(elapsed // 60)}:{int(elapsed % 60):02d}  |  "
                                  f"Remaining: ~{eta_min}:{eta_sec:02d}")
                else:
                    timer_text = f"Elapsed: 0:00  |  Remaining: calculating..."
                self.root.after(0, lambda t=timer_text: self.lbl_timer.config(text=t))

                self._ui_update(
                    f"Tile {tile_index + 1}/{total}  "
                    f"(row {row}, col {col})  \u2192  ({target_x}, {target_y})",
                    progress=(tile_index / total) * 100,
                )

                # Move stage — non-blocking send, then poll with delays
                print(f"[SCAN] Tile {tile_index}: moving to ({target_x}, {target_y})...")
                try:
                    self.stage.move_absolute(target_x, target_y, wait=False)
                    if not self._wait_stage_interruptible():
                        print(f"[SCAN] Tile {tile_index}: aborted during move")
                        break  # aborted
                    print(f"[SCAN] Tile {tile_index}: move complete")
                except Exception as e:
                    print(f"[SCAN] Move error at tile {tile_index}: {e}")
                    import traceback; traceback.print_exc()
                    time.sleep(0.5)  # recovery delay

                if self._abort_scan:
                    print(f"[SCAN] Abort flag set after move")
                    break

                # Settle — let vibrations die down
                time.sleep(params["settle_time"])

                # Capture
                filename = f"tile_r{row:03d}_c{col:03d}.tif"
                filepath = os.path.join(output_dir, filename)
                captured = False

                try:
                    self._apply_locked_camera_settings()
                    print(f"[SCAN] Tile {tile_index}: capturing...")
                    tile_img = self.camera.capture()
                    if self.save_corrected_var.get():
                        tile_img = self._apply_corrections(tile_img)
                    # Save in background
                    self._save_tile_async(filepath, tile_img)
                    captured = True
                    print(f"[SCAN] Tile {tile_index}: captured OK, shape={tile_img.shape}")
                    # Place on mosaic preview
                    self._place_tile_on_mosaic(tile_img, col, row)
                except Exception as e:
                    print(f"[SCAN] Capture error at tile {tile_index}: {e}")
                    import traceback; traceback.print_exc()

                tiles_info.append({
                    "row": row,
                    "col": col,
                    "index": tile_index,
                    "stage_x": target_x,
                    "stage_y": target_y,
                    "x_um": round(x_um, 2),
                    "y_um": round(y_um, 2),
                    "filename": filename,
                    "captured": captured,
                })

                tile_index += 1

            if self._abort_scan:
                break

        # Wait for background saves to finish
        time.sleep(0.5)

        elapsed = time.time() - start_time
        captured_count = sum(1 for t in tiles_info if t["captured"])

        # Save metadata
        self._save_metadata(output_dir, params, tiles_info, elapsed)
        self._save_tile_config(output_dir, params, tiles_info)

        scan_min, scan_sec = divmod(int(elapsed), 60)
        self.root.after(0, lambda: self.lbl_timer.config(
            text=f"Scan: {scan_min}:{scan_sec:02d}"))
        self._ui_update(
            f"Scan done: {captured_count}/{total} tiles in {elapsed:.1f}s. Stitching...",
            progress=100,
        )

        # Auto-stitch if scan completed (not aborted) and we got tiles
        if not self._abort_scan and captured_count > 0:
            try:
                print(f"[SCAN] Starting stitch of {captured_count} tiles...")
                self._ui_update("Stitching tiles (aligning + blending)...", progress=100)
                self.root.after(0, lambda: self.lbl_timer.config(
                    text=f"Scan: {scan_min}:{scan_sec:02d}  |  Stitching..."))
                stitch_start = time.time()
                stitcher = Stitcher(output_dir)
                stitch_name = getattr(self, '_stitch_file_name', 'stitched')
                output_path = stitcher.stitch(output_path=f"{stitch_name}.tif", align=True, blend=True, correct_vignetting=False, match_brightness=True)
                stitch_elapsed = time.time() - stitch_start
                stitch_min, stitch_sec = divmod(int(stitch_elapsed), 60)
                total_elapsed = elapsed + stitch_elapsed
                total_min, total_sec = divmod(int(total_elapsed), 60)
                self.root.after(0, lambda: self.lbl_timer.config(
                    text=f"Scan: {scan_min}:{scan_sec:02d}  |  "
                         f"Stitch: {stitch_min}:{stitch_sec:02d}  |  "
                         f"Total: {total_min}:{total_sec:02d}"))
                self._ui_update(
                    f"Done! {captured_count} tiles in {elapsed:.1f}s\n"
                    f"Stitched: {output_path}",
                    progress=100,
                )
            except Exception as e:
                print(f"Stitch error: {e}")
                self._ui_update(
                    f"Scan done ({captured_count} tiles) but stitch failed: {e}",
                    progress=100,
                )
        else:
            self._ui_update(
                f"Scan {'aborted' if self._abort_scan else 'complete'}: "
                f"{captured_count}/{total} tiles in {elapsed:.1f}s\n"
                f"Saved to: {output_dir}",
                progress=100,
            )

        self._scanning = False
        self._unlock_camera_settings()
        self.root.after(0, lambda: self.btn_scan.config(state=tk.NORMAL))
        self.root.after(0, lambda: self.btn_step_focus.config(state=tk.NORMAL))
        self.root.after(0, lambda: self.btn_abort.config(state=tk.DISABLED))

        if resume_live and not self._abort_scan:
            self.root.after(500, self._toggle_live)

    def _ui_update(self, tile_text, progress=None):
        """Thread-safe UI update."""
        self.root.after(0, lambda: self.lbl_tile_progress.config(text=tile_text))
        if progress is not None:
            self.root.after(0, lambda: self.progress_var.set(progress))

    def _abort(self):
        self._abort_scan = True
        # Unblock step-and-focus thread if waiting for confirmation
        if self._step_focus_mode:
            self._focus_confirmed.set()
        if self.stage:
            def _do_halt():
                with self._stage_lock:
                    try:
                        self.stage.halt()
                    except Exception:
                        pass
            threading.Thread(target=_do_halt, daemon=True).start()
        self.lbl_scan_status.config(text="Aborting...")

    # ─── Metadata Output ──────────────────────────────────────────────

    def _save_metadata(self, output_dir, params, tiles_info, elapsed):
        metadata = {
            "scan_config": {
                "objective": params["obj_name"],
                "um_per_pixel": params["um_per_pixel"],
                "camera_width_px": CAMERA_WIDTH_PX,
                "camera_height_px": CAMERA_HEIGHT_PX,
                "fov_width_um": round(params["fov_w_um"], 1),
                "fov_height_um": round(params["fov_h_um"], 1),
                "die_width_um": round(params["die_w_um"], 1),
                "die_height_um": round(params["die_h_um"], 1),
                "scan_width_um": round(params["scan_w_um"], 1),
                "scan_height_um": round(params["scan_h_um"], 1),
                "overlap_pct": params["overlap_pct"],
                "overscan_pct": params["overscan_pct"],
                "step_x_um": round(params["step_x_um"], 1),
                "step_y_um": round(params["step_y_um"], 1),
                "cols": params["cols"],
                "rows": params["rows"],
                "total_tiles": params["total_tiles"],
                "steps_per_um": self.steps_per_um,
                "origin_x_steps": params["origin_x"],
                "origin_y_steps": params["origin_y"],
                "settle_time": params["settle_time"],
                "pattern": "typewriter",
                "die_corners_steps": {
                    "UL": list(self.corner_ul) if self.corner_ul else None,
                    "UR": list(self.corner_ur) if self.corner_ur else None,
                    "LL": list(self.corner_ll) if self.corner_ll else None,
                    "LR": list(self.corner_lr) if self.corner_lr else None,
                },
            },
            "tiles": tiles_info,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_seconds": round(elapsed, 1),
        }

        path = os.path.join(output_dir, "scan_metadata.json")
        with open(path, "w") as f:
            json.dump(metadata, f, indent=2)
        print(f"Metadata saved: {path}")

    def _save_tile_config(self, output_dir, params, tiles_info):
        """Save TileConfiguration.txt for Fiji Grid/Collection Stitching.

        Pixel positions are calculated from tile row/col and the effective
        tile step in pixels (step_um / um_per_pixel), which is what Fiji
        expects as initial placement estimates.
        """
        um_per_pixel = params["um_per_pixel"]
        step_x_px = params["step_x_um"] / um_per_pixel
        step_y_px = params["step_y_um"] / um_per_pixel

        lines = ["dim = 2"]
        for tile in tiles_info:
            if tile["filename"] and tile["captured"]:
                px_x = tile["col"] * step_x_px
                px_y = tile["row"] * step_y_px
                lines.append(f"{tile['filename']};;({px_x:.1f}, {px_y:.1f})")

        path = os.path.join(output_dir, "TileConfiguration.txt")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        print(f"Fiji tile config saved: {path}")

    # ─── Camera Settings Persistence ─────────────────────────────────

    def _auto_save_camera_settings(self):
        """Save current exposure and gain to camera_settings.json."""
        settings = {
            "exposure_ms": self.exposure_var.get(),
            "gain_db": self.gain_var.get(),
        }
        try:
            with open(self._camera_settings_path, "w") as f:
                json.dump(settings, f, indent=2)
        except Exception as e:
            print(f"Camera settings save error: {e}")

    def _auto_load_camera_settings(self):
        """Load exposure and gain from camera_settings.json and apply."""
        try:
            with open(self._camera_settings_path) as f:
                settings = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        except Exception as e:
            print(f"Camera settings load error: {e}")
            return

        if "exposure_ms" in settings:
            exp = float(settings["exposure_ms"])
            self.exposure_var.set(exp)
            self.lbl_exposure.config(text=f"{exp:.1f} ms")
            if self.camera:
                self.camera.set_exposure(exp * 1000)
        if "gain_db" in settings:
            gain = float(settings["gain_db"])
            self.gain_var.set(gain)
            self.lbl_gain.config(text=f"{gain:.1f} dB")
            if self.camera:
                self.camera.set_gain(gain)

    def _lock_camera_settings(self):
        """Snapshot current gain/exposure at scan start and disable sliders."""
        self._locked_gain = self.gain_var.get()
        self._locked_exposure = self.exposure_var.get()
        for w in (self.gain_slider, self._gain_dec, self._gain_inc,
                  self.exposure_slider, self._exp_dec, self._exp_inc):
            w.config(state=tk.DISABLED)
        print(f"[SCAN] Locked camera: gain={self._locked_gain:.1f} dB, "
              f"exposure={self._locked_exposure:.1f} ms")

    def _apply_locked_camera_settings(self):
        """Re-apply the locked gain/exposure to the camera (thread-safe)."""
        if self.camera and self._locked_gain is not None:
            try:
                self.camera.set_gain(self._locked_gain)
            except Exception as e:
                print(f"Locked gain apply error: {e}")
        if self.camera and self._locked_exposure is not None:
            try:
                self.camera.set_exposure(self._locked_exposure * 1000)
            except Exception as e:
                print(f"Locked exposure apply error: {e}")

    def _unlock_camera_settings(self):
        """Clear locked camera settings and re-enable sliders."""
        self._locked_gain = None
        self._locked_exposure = None
        def _enable():
            for w in (self.gain_slider, self._gain_dec, self._gain_inc,
                      self.exposure_slider, self._exp_dec, self._exp_inc):
                w.config(state=tk.NORMAL)
        self.root.after(0, _enable)

    # ─── Cleanup ───────────────────────────────────────────────────────

    def _on_close(self):
        self._auto_save_camera_settings()
        self.live_running = False
        self._abort_scan = True
        time.sleep(0.2)
        self._disconnect()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = TileScanGUI()
    app.run()
