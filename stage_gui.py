"""
MAC 2000 Stage Controller - Visual GUI
========================================
Point-and-click interface for controlling the Ludl MAC 2000 stage.

Features:
  - Arrow buttons for X/Y movement
  - Adjustable step size (fine to coarse)
  - Speed control dropdown
  - Live position display
  - Home, Zero, Halt buttons
  - Keyboard arrow key support
  - Go-to-position input

Usage:
    python stage_gui.py
    python stage_gui.py --port COM4
    python stage_gui.py --simulate
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import time
import sys
import argparse

from mac2000_driver import MAC2000, MAC2000Error


class StageControlGUI:
    """Visual GUI for MAC 2000 stage control."""

    # Step size presets (in motor steps)
    STEP_PRESETS = [
        ("10 steps (tiny)", 10),
        ("50 steps", 50),
        ("100 steps", 100),
        ("500 steps", 500),
        ("1,000 steps", 1000),
        ("5,000 steps", 5000),
        ("10,000 steps", 10000),
        ("50,000 steps", 50000),
    ]

    SPEED_PRESETS = [
        ("Very Slow (5,000)", 5000),
        ("Slow (10,000)", 10000),
        ("Medium (25,000)", 25000),
        ("Fast (50,000)", 50000),
        ("Very Fast (100,000)", 100000),
    ]

    def __init__(self, port="COM3", simulate=False):
        self.port = port
        self.simulate = simulate
        self.stage = MAC2000(port, simulate=simulate)
        self._connected = False
        self._position_polling = False

        # Build GUI
        self.root = tk.Tk()
        self.root.title("MAC 2000 Stage Controller")
        self.root.resizable(False, False)
        self.root.configure(bg="#1e1e2e")

        # Style
        self.style = ttk.Style()
        self.style.theme_use("clam")
        self._configure_styles()

        self._build_ui()
        self._bind_keys()

        # Auto-connect on startup
        self.root.after(500, self._auto_connect)

    def _configure_styles(self):
        """Set up dark theme styles."""
        bg = "#1e1e2e"
        fg = "#cdd6f4"
        accent = "#89b4fa"
        surface = "#313244"
        red = "#f38ba8"
        green = "#a6e3a1"
        yellow = "#f9e2af"

        self.style.configure(".", background=bg, foreground=fg, font=("Segoe UI", 10))
        self.style.configure("TFrame", background=bg)
        self.style.configure("TLabel", background=bg, foreground=fg, font=("Segoe UI", 10))
        self.style.configure("TLabelframe", background=bg, foreground=accent, font=("Segoe UI", 10, "bold"))
        self.style.configure("TLabelframe.Label", background=bg, foreground=accent, font=("Segoe UI", 10, "bold"))
        self.style.configure("TButton", background=surface, foreground=fg, font=("Segoe UI", 10), padding=5)
        self.style.map("TButton", background=[("active", accent)])
        self.style.configure("TCombobox", fieldbackground=surface, background=surface, foreground=fg, font=("Segoe UI", 10))

        # Custom button styles
        self.style.configure("Arrow.TButton", font=("Segoe UI", 16, "bold"), padding=10, width=3)
        self.style.configure("Halt.TButton", background=red, foreground="#1e1e2e", font=("Segoe UI", 12, "bold"), padding=10)
        self.style.map("Halt.TButton", background=[("active", "#eba0ac")])
        self.style.configure("Home.TButton", background=yellow, foreground="#1e1e2e", font=("Segoe UI", 10, "bold"), padding=6)
        self.style.map("Home.TButton", background=[("active", "#f5e0dc")])
        self.style.configure("Zero.TButton", background=green, foreground="#1e1e2e", font=("Segoe UI", 10, "bold"), padding=6)
        self.style.map("Zero.TButton", background=[("active", "#b4e8b4")])
        self.style.configure("Connect.TButton", background=accent, foreground="#1e1e2e", font=("Segoe UI", 10, "bold"), padding=6)
        self.style.map("Connect.TButton", background=[("active", "#74c7ec")])

        self.style.configure("Position.TLabel", font=("Consolas", 14, "bold"), foreground=green, background=bg)
        self.style.configure("Status.TLabel", font=("Segoe UI", 10), foreground=yellow, background=bg)
        self.style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"), foreground=accent, background=bg)

        self.colors = {"bg": bg, "fg": fg, "accent": accent, "surface": surface, "red": red, "green": green, "yellow": yellow}

    def _build_ui(self):
        """Build the GUI layout."""
        root = self.root
        pad = {"padx": 8, "pady": 4}

        # ── Header ──
        header = ttk.Frame(root)
        header.pack(fill="x", padx=10, pady=(10, 5))
        ttk.Label(header, text="MAC 2000 Stage Controller", style="Header.TLabel").pack(side="left")
        self.status_label = ttk.Label(header, text="Disconnected", style="Status.TLabel")
        self.status_label.pack(side="right")

        # ── Connection Frame ──
        conn_frame = ttk.LabelFrame(root, text="Connection")
        conn_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(conn_frame, text="Port:").grid(row=0, column=0, **pad)
        self.port_var = tk.StringVar(value=self.port)
        port_entry = ttk.Entry(conn_frame, textvariable=self.port_var, width=10)
        port_entry.grid(row=0, column=1, **pad)

        self.connect_btn = ttk.Button(conn_frame, text="Connect", style="Connect.TButton", command=self._toggle_connect)
        self.connect_btn.grid(row=0, column=2, **pad)

        self.firmware_label = ttk.Label(conn_frame, text="")
        self.firmware_label.grid(row=0, column=3, **pad, sticky="w")

        # ── Position Display ──
        pos_frame = ttk.LabelFrame(root, text="Position (motor steps)")
        pos_frame.pack(fill="x", padx=10, pady=5)

        self.pos_x_var = tk.StringVar(value="X: ---")
        self.pos_y_var = tk.StringVar(value="Y: ---")
        ttk.Label(pos_frame, textvariable=self.pos_x_var, style="Position.TLabel").pack(side="left", padx=20, pady=8)
        ttk.Label(pos_frame, textvariable=self.pos_y_var, style="Position.TLabel").pack(side="left", padx=20, pady=8)

        self.busy_var = tk.StringVar(value="")
        ttk.Label(pos_frame, textvariable=self.busy_var, style="Status.TLabel").pack(side="right", padx=20)

        # ── Movement Controls ──
        move_frame = ttk.LabelFrame(root, text="Movement (arrow keys also work)")
        move_frame.pack(fill="x", padx=10, pady=5)

        # Step size selector
        step_row = ttk.Frame(move_frame)
        step_row.pack(fill="x", padx=5, pady=5)
        ttk.Label(step_row, text="Step Size:").pack(side="left", padx=5)
        self.step_var = tk.StringVar(value=self.STEP_PRESETS[3][0])  # Default 500
        step_combo = ttk.Combobox(step_row, textvariable=self.step_var,
                                   values=[p[0] for p in self.STEP_PRESETS],
                                   state="readonly", width=20)
        step_combo.pack(side="left", padx=5)

        # Arrow buttons in a grid
        arrow_frame = ttk.Frame(move_frame)
        arrow_frame.pack(pady=10)

        #         [  UP  ]
        # [ LEFT ] [ pos ] [ RIGHT ]
        #         [ DOWN ]

        btn_up = ttk.Button(arrow_frame, text="\u25B2", style="Arrow.TButton",
                           command=lambda: self._move(0, 1))
        btn_up.grid(row=0, column=1, padx=3, pady=3)

        btn_left = ttk.Button(arrow_frame, text="\u25C0", style="Arrow.TButton",
                             command=lambda: self._move(1, 0))
        btn_left.grid(row=1, column=0, padx=3, pady=3)

        btn_pos = ttk.Button(arrow_frame, text="\u25CE", style="Arrow.TButton",
                            command=self._update_position)
        btn_pos.grid(row=1, column=1, padx=3, pady=3)

        btn_right = ttk.Button(arrow_frame, text="\u25B6", style="Arrow.TButton",
                              command=lambda: self._move(-1, 0))
        btn_right.grid(row=1, column=2, padx=3, pady=3)

        btn_down = ttk.Button(arrow_frame, text="\u25BC", style="Arrow.TButton",
                             command=lambda: self._move(0, -1))
        btn_down.grid(row=2, column=1, padx=3, pady=3)

        # ── Speed Control ──
        speed_frame = ttk.LabelFrame(root, text="Speed")
        speed_frame.pack(fill="x", padx=10, pady=5)

        speed_row = ttk.Frame(speed_frame)
        speed_row.pack(fill="x", padx=5, pady=5)
        ttk.Label(speed_row, text="Speed:").pack(side="left", padx=5)
        self.speed_var = tk.StringVar(value=self.SPEED_PRESETS[2][0])  # Default Medium
        speed_combo = ttk.Combobox(speed_row, textvariable=self.speed_var,
                                    values=[p[0] for p in self.SPEED_PRESETS],
                                    state="readonly", width=25)
        speed_combo.pack(side="left", padx=5)
        speed_combo.bind("<<ComboboxSelected>>", self._on_speed_change)

        self.speed_display = ttk.Label(speed_row, text="")
        self.speed_display.pack(side="left", padx=10)

        # ── Go To Position ──
        goto_frame = ttk.LabelFrame(root, text="Go To Position")
        goto_frame.pack(fill="x", padx=10, pady=5)

        goto_row = ttk.Frame(goto_frame)
        goto_row.pack(fill="x", padx=5, pady=5)
        ttk.Label(goto_row, text="X:").pack(side="left", padx=5)
        self.goto_x_var = tk.StringVar(value="0")
        ttk.Entry(goto_row, textvariable=self.goto_x_var, width=10).pack(side="left", padx=2)
        ttk.Label(goto_row, text="Y:").pack(side="left", padx=5)
        self.goto_y_var = tk.StringVar(value="0")
        ttk.Entry(goto_row, textvariable=self.goto_y_var, width=10).pack(side="left", padx=2)
        ttk.Button(goto_row, text="Go", style="Connect.TButton", command=self._goto_position).pack(side="left", padx=10)

        # ── Action Buttons ──
        action_frame = ttk.Frame(root)
        action_frame.pack(fill="x", padx=10, pady=10)

        ttk.Button(action_frame, text="HOME", style="Home.TButton",
                   command=self._home).pack(side="left", padx=5)
        ttk.Button(action_frame, text="ZERO", style="Zero.TButton",
                   command=self._zero).pack(side="left", padx=5)
        ttk.Button(action_frame, text="HALT", style="Halt.TButton",
                   command=self._halt).pack(side="right", padx=5)

        # ── Log ──
        log_frame = ttk.LabelFrame(root, text="Log")
        log_frame.pack(fill="both", expand=True, padx=10, pady=(5, 10))
        self.log_text = tk.Text(log_frame, height=6, bg=self.colors["surface"],
                                fg=self.colors["fg"], font=("Consolas", 9),
                                insertbackground=self.colors["fg"], wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=3, pady=3)

    def _bind_keys(self):
        """Bind keyboard shortcuts."""
        self.root.bind("<Up>", lambda e: self._move(0, 1))
        self.root.bind("<Down>", lambda e: self._move(0, -1))
        self.root.bind("<Left>", lambda e: self._move(1, 0))
        self.root.bind("<Right>", lambda e: self._move(-1, 0))
        self.root.bind("<Escape>", lambda e: self._halt())
        self.root.bind("<space>", lambda e: self._update_position())

    # ─── Connection ───────────────────────────────────────────────────

    def _auto_connect(self):
        """Try to connect on startup."""
        self._toggle_connect()

    def _toggle_connect(self):
        """Connect or disconnect."""
        if self._connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        """Connect to the stage."""
        port = self.port_var.get().strip()
        self.stage = MAC2000(port, simulate=self.simulate)

        try:
            version = self.stage.connect()
            self._connected = True
            self.connect_btn.configure(text="Disconnect")
            self.status_label.configure(text="Connected", foreground=self.colors["green"])
            self.firmware_label.configure(text=version)
            self._log(f"Connected to {port}: {version}")

            # Read initial speed
            try:
                speed = self.stage.get_speed()
                self._log(f"Speed: {speed[0]} pulses/sec")
                self.speed_display.configure(text=f"Current: {speed[0]}")
            except Exception:
                pass

            # Start position polling
            self._position_polling = True
            self._poll_position()

        except Exception as e:
            self._log(f"Connection failed: {e}")
            self.status_label.configure(text="Connection Failed", foreground=self.colors["red"])
            messagebox.showerror("Connection Error", str(e))

    def _disconnect(self):
        """Disconnect from the stage."""
        self._position_polling = False
        self.stage.disconnect()
        self._connected = False
        self.connect_btn.configure(text="Connect")
        self.status_label.configure(text="Disconnected", foreground=self.colors["yellow"])
        self.firmware_label.configure(text="")
        self.pos_x_var.set("X: ---")
        self.pos_y_var.set("Y: ---")
        self._log("Disconnected")

    # ─── Position Polling ─────────────────────────────────────────────

    def _poll_position(self):
        """Poll stage position periodically."""
        if not self._position_polling or not self._connected:
            return

        try:
            pos = self.stage.get_position()
            self.pos_x_var.set(f"X: {pos.x:>10,}")
            self.pos_y_var.set(f"Y: {pos.y:>10,}")

            status = self.stage.get_status()
            if status == "B":
                self.busy_var.set("MOVING...")
            else:
                self.busy_var.set("")
        except Exception:
            pass

        # Poll every 500ms
        self.root.after(500, self._poll_position)

    def _update_position(self):
        """Force position update now."""
        if not self._connected:
            return
        try:
            pos = self.stage.get_position()
            self.pos_x_var.set(f"X: {pos.x:>10,}")
            self.pos_y_var.set(f"Y: {pos.y:>10,}")
            self._log(f"Position: X={pos.x}  Y={pos.y}")
        except Exception as e:
            self._log(f"Position read error: {e}")

    # ─── Movement ─────────────────────────────────────────────────────

    def _get_step_size(self) -> int:
        """Get current step size from dropdown."""
        label = self.step_var.get()
        for name, value in self.STEP_PRESETS:
            if name == label:
                return value
        return 500

    def _move(self, dx_dir: int, dy_dir: int):
        """Move stage in the given direction by the current step size."""
        if not self._connected:
            self._log("Not connected!")
            return

        step = self._get_step_size()
        dx = dx_dir * step
        dy = dy_dir * step

        def do_move():
            try:
                self.stage.move_relative(dx, dy, wait=True)
                self.root.after(0, self._update_position)
            except Exception as e:
                self.root.after(0, lambda: self._log(f"Move error: {e}"))

        # Direction labels for logging
        dirs = []
        if dx > 0: dirs.append(f"+X {dx}")
        elif dx < 0: dirs.append(f"-X {abs(dx)}")
        if dy > 0: dirs.append(f"+Y {dy}")
        elif dy < 0: dirs.append(f"-Y {abs(dy)}")
        self._log(f"Moving {', '.join(dirs)} steps...")

        # Run move in background thread so GUI doesn't freeze
        threading.Thread(target=do_move, daemon=True).start()

    def _goto_position(self):
        """Move to absolute position from input fields."""
        if not self._connected:
            self._log("Not connected!")
            return

        try:
            x = int(self.goto_x_var.get())
            y = int(self.goto_y_var.get())
        except ValueError:
            self._log("Invalid position values")
            return

        self._log(f"Going to X={x}  Y={y}...")

        def do_goto():
            try:
                self.stage.move_absolute(x, y, wait=True)
                self.root.after(0, self._update_position)
                self.root.after(0, lambda: self._log(f"Arrived at X={x}  Y={y}"))
            except Exception as e:
                self.root.after(0, lambda: self._log(f"Move error: {e}"))

        threading.Thread(target=do_goto, daemon=True).start()

    # ─── Speed ────────────────────────────────────────────────────────

    def _on_speed_change(self, event=None):
        """Handle speed dropdown change."""
        if not self._connected:
            return

        label = self.speed_var.get()
        for name, value in self.SPEED_PRESETS:
            if name == label:
                try:
                    self.stage.set_speed(value)
                    self.speed_display.configure(text=f"Set to: {value}")
                    self._log(f"Speed set to {value} pulses/sec")
                except Exception as e:
                    self._log(f"Speed error: {e}")
                return

    # ─── Actions ──────────────────────────────────────────────────────

    def _home(self):
        """Home the stage."""
        if not self._connected:
            return

        if not messagebox.askyesno("Home Stage", "Home X and Y axes?\nStage will move to limit switches."):
            return

        self._log("Homing X and Y... (this may take a while)")

        def do_home():
            try:
                self.stage.home("X Y", wait=True, timeout=120)
                self.root.after(0, self._update_position)
                self.root.after(0, lambda: self._log("Homing complete"))
            except Exception as e:
                self.root.after(0, lambda: self._log(f"Home error: {e}"))

        threading.Thread(target=do_home, daemon=True).start()

    def _zero(self):
        """Set current position as origin."""
        if not self._connected:
            return

        try:
            self.stage.set_origin(0, 0)
            self._update_position()
            self._log("Origin set to (0, 0)")
        except Exception as e:
            self._log(f"Zero error: {e}")

    def _halt(self):
        """Emergency stop."""
        if not self._connected:
            return

        try:
            self.stage.halt()
            self._log("HALT - all motors stopped")
            self._update_position()
        except Exception as e:
            self._log(f"Halt error: {e}")

    # ─── Logging ──────────────────────────────────────────────────────

    def _log(self, message: str):
        """Add message to log display."""
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")

    # ─── Run ──────────────────────────────────────────────────────────

    def run(self):
        """Start the GUI event loop."""
        # Center window on screen
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        x = (self.root.winfo_screenwidth() // 2) - (w // 2)
        y = (self.root.winfo_screenheight() // 2) - (h // 2)
        self.root.geometry(f"+{x}+{y}")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        """Clean up on window close."""
        self._position_polling = False
        if self._connected:
            self.stage.disconnect()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="MAC 2000 Stage Controller GUI")
    parser.add_argument("--port", default="COM3", help="COM port (default: COM3)")
    parser.add_argument("--simulate", action="store_true", help="Simulation mode")
    args = parser.parse_args()

    gui = StageControlGUI(port=args.port, simulate=args.simulate)
    gui.run()


if __name__ == "__main__":
    main()
