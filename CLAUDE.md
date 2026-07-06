# MAC2000 Tile Scanner — Project Context

## Overview
Automated tile scanning system for semiconductor die imaging on the WILD stereo microscope. Uses a Ludl MAC2000 motorized stage and Teli BU505MC camera to capture tiled images, then stitches them into a single high-resolution composite.

## Hardware
- **Stage:** Ludl MAC2000, serial COM3, ~2.5 steps/um
- **Camera:** Teli BU505MC, 2448x2048, RGB, 8-bit or 12-bit (BayerGR12)
- **Microscope:** WILD stereo with objectives from <6.3x to >32x

## Key Files
| File | Purpose |
|------|---------|
| `scan_gui.py` | Main tile scan GUI (tkinter) |
| `stitcher.py` | Tile stitcher with NCC alignment |
| `mac2000_driver.py` | Stage serial driver |
| `teli_camera.py` | Camera driver (pytelicam + ctypes fallback) |
| `stage_calibration.json` | Steps-per-um calibration data |
| `camera_settings.json` | Auto-saved gain/exposure settings |

## Scan Modes

### Start Scan (automatic)
Captures all tiles automatically with no user intervention. Gain/exposure lock immediately at scan start.

### Step & Focus (manual)
For samples requiring per-tile focus adjustment:
1. Stage moves to tile position, live preview starts
2. User adjusts focus (and gain/exposure on tile 1 only)
3. User clicks green "Focus Confirmed" button below preview
4. Tile captured, gain/exposure sliders lock (grayed out) for remaining tiles
5. Repeats for all tiles, then auto-stitches

## Stage Axis Convention
- **X axis INVERTED:** +X = visual left, -X = visual right
- **Y axis normal:** -Y = visual top, +Y = visual bottom
- `_detect_axis_directions()` auto-detects from named corners (UL/UR/LL/LR)

## Stitcher Settings (locked in)
- NCC template matching (phase correlation fails on repetitive semiconductor patterns)
- Global least-squares with subpixel NCC refinement (parabola fitting)
- Overlap-only cosine taper blending, brightness matching enabled
- Median systematic correction for stage positioning error (~28-33px)
- 2D bilinear grid smoothing with NCC preservation (v6)
- NCC confidence threshold: 0.5, weighting: conf^2
- Output: Raw TIFF (uncompressed), Compressed TIFF, PNG
- No vignetting correction (made results worse)

## Tile Grid Centering
Origin is centered over the die: `origin = die_center - (N-1)*step/2`. This makes overscan symmetric on all sides.

## Serial Discipline
- NEVER poll stage position during scan — causes garbled serial responses
- `_stage_lock` (threading.Lock) serializes all stage I/O
- Position polling disabled when `_scanning` is True

## Camera Settings Locking
- `_lock_camera_settings()` snapshots gain/exposure and disables sliders + arrow buttons
- `_unlock_camera_settings()` re-enables after scan completes
- `_toggle_live()` skips `_auto_load_camera_settings()` when scanning
- `_make_slider_row()` returns (slider, label, btn_dec, btn_inc) for enable/disable control

## Calibrations (um_per_pixel)
| Objective | um/pixel |
|-----------|----------|
| <6.3x | 8.389 |
| 6.3x | 8.260 |
| 10x | 5.269 |
| 20x | 2.668 |
| 25x | 2.139 |
| 32x | 1.671 |

## Backups
- `_SOFTWARE\MAC2000 Image Software_3-12-26\` — March 12, 2026 backup (excludes scan_output)

## Known Issues
- pytelicam rewrite needs camera-connected testing on lab PC

## Fixed Issues
- Stage "lost position" / corner check way off (fixed in stage-loss-fix):
  torn serial reads let a truncated `WHERE` reply (e.g. `:A 103` from
  `:A 103800 -141191`) be parsed as a valid position. Driver now rejects
  incomplete responses, resyncs the serial line after timeouts, retries
  `WHERE` with strict two-integer validation, and the GUI refuses to mark
  a corner from a failed position read. Verified against hardware with
  `test_stage_loss_fix.py` (requires stage on COM3).

---

*Last updated: March 2026*
