# MAC2000 Tile Scanner

Automated tile scanning and stitching for semiconductor die imaging on the
**WILD stereo microscope**, driven by a **Ludl MAC2000** motorized stage and a
**Teli BU505MC** camera. The software captures a grid of overlapping image
tiles and stitches them into a single high-resolution composite.

## Hardware

- **Stage:** Ludl MAC2000 (serial `COM3`, ~2.5 steps/µm)
- **Camera:** Teli BU505MC (2448×2048, RGB, 8-bit or 12-bit BayerGR12)
- **Microscope:** WILD stereo, objectives from 6.3x to 32x

## Requirements

- Windows + Python 3.13
- Python packages: `pip install -r requirements.txt`
  (numpy, opencv-python, pillow, tifffile, pyserial, scipy)
- Camera SDK — install the bundled wheel:
  `pip install pytelicam-1.1.1-cp313-cp313-win_amd64.whl`

## Quick start

Launch the main scan GUI by double-clicking **`run_scan_gui.bat`**
(or run `python scan_gui.py`).

Other launchers:

| Batch file | Purpose |
|------------|---------|
| `MAC2000 Tile Scan.bat` / `run_scan_gui.bat` | Main tile-scan GUI |
| `run_calibration.bat` | Stage calibration routine |
| `run_diagnostics.bat` | Hardware / camera diagnostics |

## Key modules

| File | Purpose |
|------|---------|
| `scan_gui.py` | Main tile-scan GUI (tkinter) |
| `stitcher.py` | Tile stitcher with NCC alignment |
| `mac2000_driver.py` | Ludl MAC2000 stage serial driver |
| `teli_camera.py` | Camera driver (pytelicam + ctypes fallback) |
| `die_mapper.py` | Die mapping helper |
| `stage_calibration.json` | Steps-per-µm calibration data |
| `camera_settings.json` | Auto-saved gain/exposure settings |

## Scan modes

- **Start Scan (automatic):** captures all tiles with no intervention; gain and
  exposure lock at scan start.
- **Step & Focus (manual):** stage moves to each tile, you confirm focus, then
  the tile is captured — useful for samples needing per-tile focus.

## Documentation

- `Tile_Scan_and_Stitch_Guide.md` — full operating guide
- `Stage_Calibration_Guide.md` — calibration walkthrough
- `CLAUDE.md` — deep technical/engineering notes

## A note on large files

Scan outputs, test captures, the bundled Python runtime, and the camera SDK
installer are intentionally **excluded** from this repository (see
`.gitignore`). They are data and binaries, not source code, and live alongside
this folder on the instrument PC.
