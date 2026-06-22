# Tile Scan and Stitch Guide

## Overview

The tile scan system captures a grid of overlapping microscope images across a semiconductor die (or any large area), then automatically stitches them into a single high-resolution composite image. This is used for die shots, defect inspection, and full-die documentation.

**Equipment Required:**
- Ludl LEP MAC2000 motorized stage (calibrated — see `Stage_Calibration_Guide.md`)
- Teli BU505MC camera (USB3, 2448x2048 resolution)
- WILD stereo microscope with objective lens

**Software:**
- `scan_gui.py` — Main scanning GUI (captures tiles + auto-stitches)
- `stitcher.py` — Standalone stitcher (can re-stitch existing scans)

---

## Part 1: Running a Tile Scan

### Step 1: Launch the Scan GUI

```bash
python MAC2000/scan_gui.py
```

The GUI opens with:
- **Left:** Live camera preview with red crosshair
- **Right:** Control panels for connection, stage movement, scan area, and settings

> **Reference:** The scan GUI after connecting.
> See: `Screenshots/Screenshot 2026-02-20 113857.png`

---

### Step 2: Connect to Hardware

1. Enter **COM3** in the COM Port field (or your stage's COM port)
2. Click **Connect**
3. Status should show: `Stage: COM3 OK | Camera: BU505MC`
4. The camera feed appears with a red crosshair at center
5. Stage position updates in green (e.g., `Position: -7757 , -73326`)

---

### Step 3: Navigate to Your Sample

Use the stage controls to find your die/sample:

**Movement Controls:**
| Speed | Pulses/sec | Use For |
|-------|-----------|---------|
| 1-Step | 85 | Fine alignment |
| Nudge | 150 | Precise positioning |
| Crawl | 500 | Slow movement |
| Slow | 5,000 | Fast navigation |

- Click and hold the **arrow buttons** for continuous movement
- **Double-click on the camera preview** to drive the stage to that point (the clicked location moves to center)
- Use **Zoom** buttons (1x/2x/4x/8x) to get a closer look without moving the stage

> **Tip:** Use the Invert X / Invert Y checkboxes if the arrow directions don't match your visual expectation. The X axis is typically inverted on this stage.

---

### Step 4: Select Objective

In section **3. Scan Settings**, select your microscope objective from the dropdown:

| Objective | um/pixel | FOV Width | FOV Height |
|-----------|----------|-----------|------------|
| 6.3x      | 8.260    | 20,220 um | 16,913 um  |
| 10x       | 5.269    | 12,899 um | 10,791 um  |
| 20x       | 2.668    | 6,530 um  | 5,465 um   |
| 25x       | 2.139    | 5,236 um  | 4,381 um   |
| 32x       | 1.671    | 4,090 um  | 3,422 um   |

> **Note:** Higher magnification = smaller FOV = more tiles needed = longer scan but higher detail.

---

### Step 5: Define the Scan Area

The scan area is defined by marking two opposite corners of your die.

1. **Navigate to one corner** of the die (e.g., upper-left)
2. Click **Mark Corner 1**
   - The position is recorded and displayed

3. **Navigate to the opposite corner** (e.g., lower-right)
4. Click **Mark Corner 2**
   - The software calculates the bounding box

After marking both corners, the **Scan Preview** section shows:
- Die dimensions in um and mm
- Scan area (die + overscan margin)
- Grid size (columns x rows)
- Total number of tiles
- Estimated scan time

**Example:**
```
Die: 9395 x 12559 um (9.4 x 12.6 mm)
Scan: 11274 x 15070 um (with 10% overscan)
Grid: 4 x 5 = 20 tiles
```

---

### Step 6: Adjust Scan Settings

| Setting | Default | Description |
|---------|---------|-------------|
| Overlap % | 10% | How much adjacent tiles overlap. More overlap = better stitching but more tiles |
| Overscan % | 10% | Extra margin beyond the die corners. Ensures the full die is captured even with positioning error |
| Settle time | 0.3 s | Delay after stage movement before capturing. Increase if you see motion blur (try 1.0-3.0 s) |

> **Tip for best results:** If the stitching has alignment issues, try increasing the settle time to 1.0 s or more. Stage vibration after a move can cause slight blur that degrades the alignment algorithm.

---

### Step 7: Start the Scan

1. Click **Start Scan**
2. The scan proceeds automatically:
   - Stage moves to each tile position in a **serpentine pattern** (left-to-right on even rows, right-to-left on odd rows — minimizes total stage travel)
   - After each move, waits for settle time
   - Captures the frame and saves as TIFF
   - Updates the **real-time mosaic preview** on the left panel
   - Progress bar shows completion percentage

3. **To abort:** Click **Abort Scan** at any time. Captured tiles are preserved.

4. After all tiles are captured:
   - Metadata is saved (`scan_metadata.json`)
   - Fiji tile configuration is saved (`TileConfiguration.txt`)
   - **Auto-stitching begins** — aligns and blends all tiles into a single image

5. When complete, the output path is displayed. Open the stitched image in your preferred viewer.

> **Reference:** Real-time mosaic preview during a 13x9 = 117 tile scan of a V100 die.
> See: `Screenshots/Screenshot 2026-02-20 131548.png`

---

### Step 8: View the Results

The scan output is saved to:
```
MAC2000/scan_output/YYYYMMDD_HHMMSS/
```

> **Reference:** Scan output folder showing all files.
> See: `Screenshots/Screenshot 2026-02-20 210431.png`

---

## Part 2: Understanding Scan Output

Each scan creates a timestamped folder with these files:

| File | Size (typical) | Description |
|------|----------------|-------------|
| `tile_r000_c000.tif` | ~15 MB each | Individual tile images. Named by row and column. |
| `scan_metadata.json` | ~5 KB | Complete scan configuration and tile positions |
| `TileConfiguration.txt` | ~1 KB | Fiji-compatible tile layout (for manual re-stitching) |
| `stitched.tif` | ~250 MB | Final stitched image (uncompressed TIFF) |
| `stitched.jpg` | ~25 MB | JPEG preview (95% quality, for easy sharing) |

### Tile Naming Convention
```
tile_r{row:03d}_c{col:03d}.tif
```
- `tile_r000_c000.tif` = top-left tile (row 0, column 0)
- `tile_r000_c001.tif` = one column to the right
- `tile_r001_c000.tif` = one row down

### scan_metadata.json Structure

Key fields:
```json
{
  "scan_config": {
    "objective": "32x",
    "um_per_pixel": 1.671,
    "camera_width_px": 2448,
    "camera_height_px": 2048,
    "fov_width_um": 4090.6,
    "fov_height_um": 3422.2,
    "overlap_pct": 10.0,
    "overscan_pct": 10.0,
    "cols": 4,
    "rows": 5,
    "total_tiles": 20,
    "settle_time": 3.0,
    "pattern": "serpentine"
  },
  "tiles": [
    {
      "row": 0, "col": 0,
      "stage_x": 6224, "stage_y": -108017,
      "x_um": 0.0, "y_um": 0.0,
      "filename": "tile_r000_c000.tif",
      "captured": true
    }
  ],
  "timestamp": "2026-02-20T16:47:47",
  "elapsed_seconds": 85.9
}
```

### Sharing Scan Results

- **Share the JPEG** (`stitched.jpg`, ~25 MB) for quick viewing — universally compatible
- **Share the TIFF** (`stitched.tif`, ~250 MB+) for full-quality analysis
  - The TIFF is saved **uncompressed** to prevent corruption if bytes are lost during file transfer (OneDrive, email, etc.)
  - Wait for OneDrive to show a **green checkmark** before downloading/sharing (not the blue sync arrows)

---

## Part 3: Re-Stitching Existing Scans

If you want to re-stitch a previous scan (e.g., with different settings), use the standalone stitcher.

### Command Line Usage

```bash
# Basic stitch (uses all defaults)
python MAC2000/stitcher.py MAC2000/scan_output/20260220_164747

# Preview without stitching (shows grid size, estimated output)
python MAC2000/stitcher.py MAC2000/scan_output/20260220_164747 --preview

# Custom output filename
python MAC2000/stitcher.py MAC2000/scan_output/20260220_164747 -o my_stitch.tif

# No alignment (just place tiles at nominal positions)
python MAC2000/stitcher.py MAC2000/scan_output/20260220_164747 --no-align

# No blending (hard edges between tiles)
python MAC2000/stitcher.py MAC2000/scan_output/20260220_164747 --no-blend

# Larger search range for alignment (if tiles are misaligned by more than 50px)
python MAC2000/stitcher.py MAC2000/scan_output/20260220_164747 --max-shift 100
```

### Python API Usage

```python
from MAC2000.stitcher import Stitcher

# Create stitcher from scan directory
s = Stitcher("MAC2000/scan_output/20260220_164747")

# Preview the stitching plan
s.preview()

# Stitch with all defaults
result = s.stitch()

# Stitch with custom settings
result = s.stitch(
    output_path="stitched.tif",   # Output filename
    align=True,                    # NCC alignment refinement
    blend=True,                    # Cosine taper blending
    max_shift_px=50,              # Max alignment search range
    correct_vignetting=False       # Vignetting correction (disabled by default)
)
```

### CLI Arguments Reference

| Argument | Default | Description |
|----------|---------|-------------|
| `scan_dir` | (required) | Path to scan folder with tiles + scan_metadata.json |
| `--output, -o` | `stitched.tif` | Output filename |
| `--pixel-size` | auto | Pixel size in um (auto-detected from metadata) |
| `--no-align` | False | Skip NCC alignment, use nominal positions only |
| `--no-blend` | False | No blending, tiles overwrite each other |
| `--max-shift` | 50 | Maximum alignment correction in pixels |
| `--preview` | False | Preview only, don't stitch |
| `-v, --verbose` | False | Verbose logging |

---

## Part 4: How the Stitcher Works

### Alignment Algorithm

1. **NCC Template Matching** — For each tile pair that shares an overlap region, the stitcher uses Normalized Cross-Correlation to find the best alignment. NCC is robust against brightness variations between tiles.

2. **Subpixel Refinement** — After finding the integer-pixel NCC peak, a parabola is fitted around the peak to achieve ~0.5 pixel extra precision.

3. **Global Least-Squares Optimization** — Instead of correcting each tile independently (which can accumulate drift), all tile positions are solved simultaneously using weighted least squares. Each NCC measurement becomes a constraint, weighted by its confidence score. This distributes alignment error evenly across the entire mosaic.

### Blending Algorithm

**Overlap-only cosine taper blending:**
- Pixels in the center of each tile have full weight (1.0)
- Pixels in the overlap zones ramp smoothly from 0 to 1 using a cosine curve
- Where two tiles overlap, both contribute proportionally to the final pixel value
- This eliminates hard seam lines at tile boundaries

### Output

The stitcher produces:
- **stitched.tif** — Uncompressed TIFF (full quality, no compression artifacts, corruption-resistant)
- **stitched.jpg** — JPEG preview at 95% quality (auto-generated alongside TIFF)

Console output example:
```
Aligning tiles (overlap: 244x204 px, search: +/-50 px)...
  [0,1] left: dx=-10.1 dy=24.8 conf=0.898
  [0,2] left: dx=-7.2 dy=24.9 conf=0.936
  ...
Alignment: 31/31 pairs refined successfully
Global optimization: 31 constraints, 20 tiles

Stitching 20 tiles into 9123 x 9476 canvas (247 MB)...
  [1/20] (5%) Placed tile_r000_c000.tif at (92, 0)
  ...
  [20/20] (100%) Placed tile_r004_c003.tif at (6586, 7427)
  JPEG preview saved: stitched.jpg

Stitched image saved: stitched.tif
  Size: 9123 x 9476 px
  Physical: 15245 x 15834 um
  Time: 8.6s
```

### Alignment Output Interpretation

Each line like `[0,1] left: dx=-10.1 dy=24.8 conf=0.898` means:
- `[0,1]` — Tile at row 0, column 1
- `left` — Compared against its left neighbor (column 0)
- `dx=-10.1` — Horizontal correction of -10.1 pixels from nominal position
- `dy=24.8` — Vertical correction of 24.8 pixels
- `conf=0.898` — NCC confidence (0-1, higher = more reliable match)

Confidence values:
- **> 0.9** — Excellent match
- **0.7-0.9** — Good match
- **0.5-0.7** — Fair match (may be less reliable)
- **< 0.5** — Poor match (featureless area, edge of die)

---

## Part 5: Fiji/ImageJ Stitching (Alternative)

Each scan also produces a `TileConfiguration.txt` file compatible with Fiji's Grid/Collection Stitching plugin, if you prefer to use Fiji for stitching instead.

### Using Fiji

1. Open Fiji
2. Go to **Plugins > Stitching > Grid/Collection Stitching**
3. Select **Type: Positions from file**
4. Set **Order: Defined by TileConfiguration**
5. Browse to the scan output folder
6. Select `TileConfiguration.txt`
7. Click OK to stitch

The TileConfiguration.txt format:
```
dim = 2
tile_r000_c000.tif;;(0.0, 0.0)
tile_r000_c001.tif;;(2203.2, 0.0)
tile_r000_c002.tif;;(4406.4, 0.0)
...
```

---

## Part 6: Tips for Best Results

### Before Scanning
- **Calibrate the stage** if you haven't recently (see `Stage_Calibration_Guide.md`)
- **Focus carefully** — the stitcher can correct position errors but not focus errors
- **Clean the sample** — dust and debris create artifacts in the stitch
- **Set appropriate illumination** — consistent brightness across the FOV reduces seam visibility

### Scan Settings
- **Overlap 10%** works well for most samples. Increase to 15-20% if your sample has very repetitive patterns (the alignment needs enough unique features to match)
- **Settle time 0.3s** is fine for small moves. For larger scans or heavy stage loads, increase to 1.0-3.0s to let vibrations damp out
- **32x objective** gives a good balance of detail and scan size for die shots

### After Scanning
- **Check the confidence values** in the console output. If many are below 0.7, the alignment may be poor in those areas
- **Re-stitch with different settings** if needed — the tiles are preserved, you can re-run the stitcher any time
- **Share the JPEG** for quick review, keep the TIFF for analysis

### Common Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| Visible seams between tiles | Poor alignment or brightness mismatch | Increase overlap %, increase settle time |
| Tiles shifted/misaligned | Stage vibration during capture | Increase settle time to 1.0-3.0s |
| Blurry tiles | Out of focus or motion during capture | Refocus, increase settle time |
| Very large output file | High magnification + large area | Expected. Use JPEG for sharing. |
| Scan aborted mid-way | Serial port collision | Restart GUI, avoid double-clicking during scan |
| "Position read error" in console | Serial port timing issue | Does not affect scan quality, can be ignored |

---

## File Reference

| File | Location | Purpose |
|------|----------|---------|
| `scan_gui.py` | `MAC2000/` | Main tile scan GUI |
| `stitcher.py` | `MAC2000/` | Tile stitcher (standalone + library) |
| `calibrate_stage.py` | `MAC2000/` | Stage calibration tool |
| `mac2000_driver.py` | `MAC2000/` | Stage serial driver |
| `teli_camera.py` | `MAC2000/` | Camera driver |
| `stage_calibration.json` | `MAC2000/` | Calibration data (auto-loaded) |
| Scan outputs | `MAC2000/scan_output/` | Timestamped scan folders |

---

*Last updated: February 2026*
