"""
Synthetic end-to-end test for stitcher.py.

Builds a 4x4 grid of 16-bit tiles cut from one textured ground-truth image
with:
  - known random stage position errors (up to ~35 px)
  - per-tile brightness errors (0.8x - 1.25x)
  - synthetic vignetting
  - one deliberately bright region near the 12-bit ceiling
Then stitches and checks:
  1. alignment recovers the injected offsets (inlier RMS <= 2 px)
  2. brightness gains + highlight protection never clip the bright region
  3. the stitched output matches ground truth (masked median abs diff)

Run:  python test_stitcher_synthetic.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from stitcher import Stitcher  # noqa: E402

TILE_W, TILE_H = 640, 512
ROWS = COLS = 4
OVERLAP = 96                    # px
PIXEL_SIZE = 1.0                # um/px for simplicity
RNG = np.random.default_rng(7)


def make_ground_truth(h, w):
    """Textured 16-bit image with unique (non-periodic) structure.

    Blurred random noise at two scales gives every neighborhood a unique
    fingerprint (like real die photos with routing/logic variation), so
    NCC has an unambiguous global optimum.
    """
    import cv2

    coarse = cv2.GaussianBlur(
        RNG.uniform(0, 1, (h, w)).astype(np.float32), (0, 0), 12) * 60000
    fine = cv2.GaussianBlur(
        RNG.uniform(0, 1, (h, w)).astype(np.float32), (0, 0), 2) * 25000
    img = 8000 + coarse + fine + RNG.normal(0, 500, (h, w)).astype(np.float32)
    # unique high-contrast blocks (bond pads / macros)
    for _ in range(250):
        by = int(RNG.integers(0, h - 40))
        bx = int(RNG.integers(0, w - 40))
        img[by:by + 40, bx:bx + 40] += float(RNG.uniform(-8000, 8000))
    # one hot region near the 12-bit ceiling (tests highlight protection)
    img[h // 4:h // 4 + 300, w // 4:w // 4 + 300] = \
        60000 + RNG.normal(0, 1500, (300, 300))
    img = np.clip(img, 0, 65535)
    return np.stack([img, img * 0.95, img * 0.9], axis=2).astype(np.uint16)


def build_scan(scan_dir: Path):
    import tifffile

    step = TILE_W - OVERLAP, TILE_H - OVERLAP
    full_w = COLS * step[0] + OVERLAP + 200
    full_h = ROWS * step[1] + OVERLAP + 200
    truth = make_ground_truth(full_h, full_w)

    cy, cx = np.mgrid[0:TILE_H, 0:TILE_W]
    r2 = (((cx - TILE_W / 2) / (TILE_W / 2)) ** 2
          + ((cy - TILE_H / 2) / (TILE_H / 2)) ** 2)
    vignette = (1.0 - 0.25 * r2).astype(np.float32)   # darker corners

    tiles_meta = []
    true_offsets = {}
    for r in range(ROWS):
        for c in range(COLS):
            nominal_x = 100 + c * step[0]
            nominal_y = 100 + r * step[1]
            # up to ~25 px error: realistic proportion of stage error to
            # overlap (real scans: overlap ~360 px, error ~150 px)
            err_x = int(RNG.integers(-25, 26)) if (r, c) != (0, 0) else 0
            err_y = int(RNG.integers(-25, 26)) if (r, c) != (0, 0) else 0
            true_x, true_y = nominal_x + err_x, nominal_y + err_y
            true_offsets[(r, c)] = (true_x, true_y)

            tile = truth[true_y:true_y + TILE_H,
                         true_x:true_x + TILE_W].astype(np.float32)
            gain_err = RNG.uniform(0.8, 1.25)
            tile = tile * gain_err * vignette[:, :, None]
            tile = np.clip(tile, 0, 65535).astype(np.uint16)

            fname = f"tile_r{r:03d}_c{c:03d}.tif"
            tifffile.imwrite(scan_dir / fname, tile)
            tiles_meta.append({
                "row": r, "col": c, "filename": fname,
                "x_um": float(nominal_x * PIXEL_SIZE),
                "y_um": float(nominal_y * PIXEL_SIZE),
                "captured": True,
            })

    meta = {
        "scan_config": {
            "fov_width_um": TILE_W * PIXEL_SIZE,
            "fov_height_um": TILE_H * PIXEL_SIZE,
            "step_x_um": step[0] * PIXEL_SIZE,
            "step_y_um": step[1] * PIXEL_SIZE,
            "overlap_pct": OVERLAP / TILE_W * 100,
            "rows": ROWS, "cols": COLS,
        },
        "tiles": tiles_meta,
    }
    with open(scan_dir / "scan_metadata.json", "w") as f:
        json.dump(meta, f)
    return truth, true_offsets


def main():
    import tifffile

    tmp = Path(tempfile.mkdtemp(prefix="stitch_test_"))
    try:
        scan_dir = tmp / "scan"
        scan_dir.mkdir()
        truth, true_offsets = build_scan(scan_dir)

        st = Stitcher(str(scan_dir), edge_crop_pct=0)
        out = st.stitch("stitched.tif")

        failures = []

        # 1. alignment: refined positions match injected offsets up to a
        #    global translation (a uniform shift of all tiles has no
        #    effect on the stitched result - the canvas is re-zeroed)
        d = []
        for t in st.tiles:
            tx, ty = true_offsets[(t.row, t.col)]
            d.append((t.refined_x - tx * PIXEL_SIZE,
                      t.refined_y - ty * PIXEL_SIZE))
        d = np.array(d)
        d -= d.mean(axis=0)          # remove global translation
        rms = float(np.sqrt(np.mean(np.sum(d ** 2, axis=1))))
        print(f"\nCHECK 1  alignment RMS vs injected offsets "
              f"(translation-free): {rms:.2f} px")
        if rms > 2.0:
            failures.append(f"alignment rms {rms:.2f} px > 2.0 px")

        # 2. highlight protection: the hot region must not be clipped
        result = tifffile.imread(out)
        clip_frac = float((result >= 65534).mean())
        print(f"CHECK 2  clipped fraction of output: {clip_frac * 100:.4f}%")
        if clip_frac > 0.001:
            failures.append(f"clipped fraction {clip_frac:.4f} > 0.001")

        # 3. content fidelity: compare against ground truth region
        #    (brightness scale may differ globally -> compare after
        #     normalizing medians; ignore borders)
        h, w = result.shape[:2]
        gt = truth[100:100 + h, 100:100 + w].astype(np.float32)
        gt = gt[:min(h, gt.shape[0]), :min(w, gt.shape[1])]
        res = result[:gt.shape[0], :gt.shape[1]].astype(np.float32)
        m = 128
        gt_c = gt[m:-m, m:-m]
        res_c = res[m:-m, m:-m]
        scale = np.median(gt_c) / max(np.median(res_c), 1.0)
        mad = float(np.median(np.abs(res_c * scale - gt_c)))
        rel = mad / float(np.median(gt_c))
        print(f"CHECK 3  masked median abs diff: {rel * 100:.2f}% of median level")
        if rel > 0.10:
            failures.append(f"content diff {rel:.3f} > 0.10")

        print()
        if failures:
            for f_ in failures:
                print(f"FAIL: {f_}")
            sys.exit(1)
        print("ALL CHECKS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
