"""
Tile Image Stitcher
====================
Takes captured tiles from the die_mapper and assembles them into a single
high-resolution stitched image.

Features:
  - Multi-patch NCC alignment (5 sub-pixel patches per seam, median
    aggregated) - robust against repetitive semiconductor die patterns
  - Global position solve with iterative outlier rejection
  - Auto-widening search range when the pair match rate is poor
    (handles larger-than-expected stage error)
  - Per-channel flat-field from the median of the brightest tiles
  - Seam brightness matching with highlight protection (bright tiles are
    never amplified into clipping; 12-bit highlight detail is preserved)
  - Linear blending at overlap regions (no visible seams)
  - Handles grayscale and color images
  - Outputs 8-bit or 16-bit TIFF
  - Streaming band compositor for gigapixel canvases (disk-backed,
    bounded RAM)
  - Reads scan_metadata.json from die_mapper output

Usage:
    # From command line:
    python stitcher.py ./scan_output --output stitched.tif

    # From Python:
    from stitcher import Stitcher
    result = Stitcher("./scan_output").stitch("stitched.tif")

    # Preview grid without stitching:
    Stitcher("./scan_output").preview()
"""

import json
import logging
import time
from pathlib import Path
from typing import List, Tuple, Optional, Dict

import numpy as np

logger = logging.getLogger(__name__)


def load_image(filepath: str) -> np.ndarray:
    """Load an image file, supporting TIFF, PNG, JPG, and numpy .npy."""
    path = Path(filepath)
    ext = path.suffix.lower()

    if ext == ".npy":
        return np.load(filepath)

    if ext in (".tif", ".tiff"):
        try:
            import tifffile
            return tifffile.imread(filepath)
        except ImportError:
            pass

    # Fall back to OpenCV (handles most formats)
    import cv2
    img = cv2.imread(filepath, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(f"Could not load image: {filepath}")
    # OpenCV loads color as BGR, convert to RGB
    if len(img.shape) == 3 and img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    return img


def phase_correlate(ref: np.ndarray, moving: np.ndarray) -> Tuple[float, float, float]:
    """
    Find the sub-pixel shift between two overlapping images using phase correlation.

    Parameters
    ----------
    ref : np.ndarray
        Reference image (grayscale, float or uint8)
    moving : np.ndarray
        Moving image to align against reference

    Returns
    -------
    (dy, dx, confidence) : tuple
        Shift in pixels (sub-pixel precision) and correlation confidence (0-1)
    """
    import cv2

    # Convert to float32 grayscale if needed
    if ref.dtype != np.float32:
        ref = ref.astype(np.float32)
    if moving.dtype != np.float32:
        moving = moving.astype(np.float32)

    if len(ref.shape) == 3:
        ref = np.mean(ref, axis=2)
    if len(moving.shape) == 3:
        moving = np.mean(moving, axis=2)

    # Ensure same size (crop to smaller)
    h = min(ref.shape[0], moving.shape[0])
    w = min(ref.shape[1], moving.shape[1])
    ref = ref[:h, :w]
    moving = moving[:h, :w]

    # OpenCV phase correlation returns (dx, dy)
    shift, response = cv2.phaseCorrelate(ref, moving)
    dx, dy = shift
    return (dy, dx, response)


class TilePosition:
    """Position of a tile in the stitched canvas."""

    def __init__(self, row: int, col: int, filename: str,
                 nominal_x: float, nominal_y: float):
        self.row = row
        self.col = col
        self.filename = filename
        # Nominal position from stage coordinates (pixels in output space)
        self.nominal_x = nominal_x
        self.nominal_y = nominal_y
        # Refined position after alignment
        self.refined_x = nominal_x
        self.refined_y = nominal_y
        # Alignment quality
        self.confidence = 0.0
        self.aligned = False


class Stitcher:
    """
    Assembles tiles from a die_mapper scan into a single stitched image.

    Parameters
    ----------
    scan_dir : str
        Directory containing tiles and scan_metadata.json
    pixel_size_um : float
        Pixel size in microns (used to convert stage positions to pixels).
        If 0, auto-calculated from metadata.
    """

    def __init__(self, scan_dir: str, pixel_size_um: float = 0,
                 edge_crop_pct: float = 7.5):
        """
        Parameters
        ----------
        scan_dir : str
            Directory containing tiles and scan_metadata.json
        pixel_size_um : float
            Pixel size in microns (0 = auto from metadata).
        edge_crop_pct : float
            Percent of each tile edge to crop before alignment/blending.
            Removes the worst vignetting/blur artifacts at tile boundaries
            for cleaner seams. Auto-capped to leave at least 50 px of
            overlap for alignment. Set to 0 to disable. Default 7.5
            (matches the simple_batch approach: 15% total per dimension).
        """
        self.scan_dir = Path(scan_dir)
        self.pixel_size_um = pixel_size_um
        self._edge_crop_pct = float(edge_crop_pct)
        self.tiles: List[TilePosition] = []
        self.metadata: Dict = {}
        self._tile_shape: Optional[Tuple[int, ...]] = None      # effective (cropped) shape
        self._raw_tile_shape: Optional[Tuple[int, ...]] = None  # full shape on disk
        self._crop_x: int = 0
        self._crop_y: int = 0
        self._tile_dtype: np.dtype = np.dtype(np.uint8)  # default, updated on first tile load
        self._ds_stack: Optional[np.ndarray] = None  # cached 1/8 photometric stack

        self._load_metadata()

    def _load_metadata(self):
        """Load scan_metadata.json and build tile list."""
        meta_path = self.scan_dir / "scan_metadata.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"No scan_metadata.json in {self.scan_dir}")

        with open(meta_path) as f:
            self.metadata = json.load(f)

        config = self.metadata["scan_config"]

        # Calculate pixel size if not provided
        # pixel_size = FOV_um / image_width_pixels
        # We'll determine image_width from the first tile
        self._fov_width_um = config["fov_width_um"]
        self._fov_height_um = config["fov_height_um"]
        self._step_x_um = config["step_x_um"]
        self._step_y_um = config["step_y_um"]
        self._overlap_pct = config["overlap_pct"]
        self._rows = config["rows"]
        self._cols = config["cols"]

        # Build tile positions
        for tile_meta in self.metadata["tiles"]:
            if not tile_meta.get("captured", False) and not tile_meta.get("filename"):
                continue

            filepath = self.scan_dir / tile_meta["filename"]
            if not filepath.exists():
                logger.warning(f"Tile not found: {filepath}")
                continue

            self.tiles.append(TilePosition(
                row=tile_meta["row"],
                col=tile_meta["col"],
                filename=tile_meta["filename"],
                nominal_x=tile_meta["x_um"],
                nominal_y=tile_meta["y_um"],
            ))

        if not self.tiles:
            raise ValueError("No valid tiles found in scan directory")

        logger.info(f"Loaded {len(self.tiles)} tiles from {self.scan_dir}")

    def _get_tile_shape(self) -> Tuple[int, ...]:
        """Effective tile shape after edge cropping.

        Loads the first tile on first call to determine raw dimensions/dtype,
        computes the safe edge crop, and caches both shapes.
        """
        if self._tile_shape is None:
            first_tile = load_image(str(self.scan_dir / self.tiles[0].filename))
            self._raw_tile_shape = first_tile.shape
            self._tile_dtype = first_tile.dtype

            # Auto-calculate pixel size from RAW tile width and FOV
            if self.pixel_size_um <= 0:
                tile_width_px = first_tile.shape[1]
                self.pixel_size_um = self._fov_width_um / tile_width_px

            # Compute safe edge crop
            self._crop_x, self._crop_y = self._compute_crop_px(first_tile.shape)

            shape = list(first_tile.shape)
            shape[0] -= 2 * self._crop_y
            shape[1] -= 2 * self._crop_x
            self._tile_shape = tuple(shape)

            logger.info(
                f"Tile shape: raw={first_tile.shape}, "
                f"effective={self._tile_shape}, "
                f"crop=({self._crop_x}, {self._crop_y}) px, "
                f"dtype={self._tile_dtype}"
            )
            if self.pixel_size_um > 0 and first_tile.shape[1] != self._tile_shape[1]:
                logger.info(f"Edge crop active: {self._edge_crop_pct}% per side requested")

        return self._tile_shape

    def _compute_crop_px(self, raw_shape) -> Tuple[int, int]:
        """Compute (crop_x, crop_y) in pixels for the given raw tile shape.

        Caps the requested crop to leave at least ``min_remaining_overlap``
        pixels of overlap between adjacent (cropped) tiles, so alignment
        and blending still have something to work with.
        """
        if self._edge_crop_pct <= 0:
            return 0, 0
        raw_h, raw_w = raw_shape[:2]
        step_x_px = self._step_x_um / self.pixel_size_um
        step_y_px = self._step_y_um / self.pixel_size_um
        full_overlap_x = int(raw_w - step_x_px)
        full_overlap_y = int(raw_h - step_y_px)
        requested_x = int(raw_w * self._edge_crop_pct / 100)
        requested_y = int(raw_h * self._edge_crop_pct / 100)
        min_remaining = 50
        max_safe_x = max(0, (full_overlap_x - min_remaining) // 2)
        max_safe_y = max(0, (full_overlap_y - min_remaining) // 2)
        return min(requested_x, max_safe_x), min(requested_y, max_safe_y)

    def _load_and_crop(self, filename: str, dtype=None) -> np.ndarray:
        """Load a tile and apply the configured edge crop."""
        self._get_tile_shape()  # ensure crop is computed
        img = load_image(str(self.scan_dir / filename))
        cy, cx = self._crop_y, self._crop_x
        h, w = img.shape[:2]
        if cy > 0:
            img = img[cy:h - cy, :]
        if cx > 0:
            img = img[:, cx:w - cx]
        if dtype is not None:
            img = img.astype(dtype)
        return img

    def _get_overlap_px(self) -> Tuple[int, int]:
        """Effective overlap between cropped tiles, in pixels."""
        shape = self._get_tile_shape()
        tile_h, tile_w = shape[0], shape[1]
        step_x_px = int(round(self._step_x_um / self.pixel_size_um))
        step_y_px = int(round(self._step_y_um / self.pixel_size_um))
        return max(tile_w - step_x_px, 0), max(tile_h - step_y_px, 0)

    def _um_to_px(self, um: float) -> float:
        """Convert microns to pixels."""
        return um / self.pixel_size_um

    # ─── Preview ──────────────────────────────────────────────────────

    def preview(self):
        """Print stitching plan without doing any work."""
        tile_shape = self._get_tile_shape()
        tile_h, tile_w = tile_shape[0], tile_shape[1]
        is_color = len(tile_shape) == 3

        # Calculate canvas size
        positions = self._compute_pixel_positions()
        max_x = max(p[0] + tile_w for p in positions.values())
        max_y = max(p[1] + tile_h for p in positions.values())

        canvas_w = int(np.ceil(max_x))
        canvas_h = int(np.ceil(max_y))

        n_tiles = len(self.tiles)
        bytes_per_pixel = tile_shape[2] if is_color else 1
        dtype_size = np.dtype(self._tile_dtype).itemsize
        canvas_mb = (canvas_w * canvas_h * bytes_per_pixel * dtype_size) / (1024 * 1024)

        overlap_x_px, overlap_y_px = self._get_overlap_px()
        raw_h, raw_w = self._raw_tile_shape[:2]

        print("=" * 60)
        print("  STITCHING PLAN")
        print("=" * 60)
        print(f"  Scan directory:   {self.scan_dir}")
        print(f"  Tiles found:      {n_tiles} / {self._rows * self._cols}")
        print(f"  Raw tile size:    {raw_w} x {raw_h} px")
        print(f"  Effective size:   {tile_w} x {tile_h} px ({'color' if is_color else 'grayscale'})")
        if self._crop_x or self._crop_y:
            print(f"  Edge crop:        {self._crop_x} x {self._crop_y} px per side ({self._edge_crop_pct:.1f}% requested)")
        print(f"  Pixel size:       {self.pixel_size_um:.4f} um/px")
        print(f"  Grid:             {self._cols} x {self._rows}")
        print(f"  Overlap (eff):    {overlap_x_px} x {overlap_y_px} px")
        print(f"  Output canvas:    {canvas_w} x {canvas_h} px")
        print(f"  Output size:      ~{canvas_mb:.0f} MB (uncompressed)")
        print(f"  Output um:        {canvas_w * self.pixel_size_um:.0f} x {canvas_h * self.pixel_size_um:.0f} um")
        print("=" * 60)

    def _compute_pixel_positions(self) -> Dict[str, Tuple[float, float]]:
        """Convert tile um positions to pixel positions, shifted so min = 0."""
        self._get_tile_shape()
        positions = {}
        for tile in self.tiles:
            px = self._um_to_px(tile.refined_x)
            py = self._um_to_px(tile.refined_y)
            positions[tile.filename] = (px, py)

        # Shift so minimum position is (0, 0)
        min_x = min(p[0] for p in positions.values())
        min_y = min(p[1] for p in positions.values())
        for key in positions:
            x, y = positions[key]
            positions[key] = (x - min_x, y - min_y)

        return positions

    # ─── Alignment ────────────────────────────────────────────────────

    def _auto_max_shift(self) -> int:
        """Compute max NCC search range from pixel size and overlap.

        The WILD stage has ~250 µm typical worst-case positioning error,
        but larger errors have been observed in the field (~600 µm), so
        ``align`` auto-retries with a doubled range when the match rate
        is poor. Floor of 80 px; capped so the search region cannot grow
        past a third of the tile.
        """
        self._get_tile_shape()  # ensures pixel_size_um is set
        max_error_um = 250.0
        px = int(np.ceil(max_error_um / self.pixel_size_um)) + 20
        tile_shape = self._get_tile_shape()
        cap = max(tile_shape[1] // 3, 80)
        return min(max(px, 80), cap)

    @staticmethod
    def _subpixel_peak(result, ix, iy):
        """Fit parabola around integer NCC peak for subpixel precision."""
        h, w = result.shape
        fx, fy = float(ix), float(iy)
        if 0 < ix < w - 1:
            left = float(result[iy, ix - 1])
            center = float(result[iy, ix])
            right = float(result[iy, ix + 1])
            denom = 2.0 * (2.0 * center - left - right)
            if abs(denom) > 1e-7:
                fx = ix + (left - right) / denom
        if 0 < iy < h - 1:
            top = float(result[iy - 1, ix])
            center = float(result[iy, ix])
            bottom = float(result[iy + 1, ix])
            denom = 2.0 * (2.0 * center - top - bottom)
            if abs(denom) > 1e-7:
                fy = iy + (top - bottom) / denom
        return fx, fy

    def _match_pair_multi(self, img_a, img_b, direction, max_shift_px,
                          n_patches=5, min_conf=0.5):
        """Measure the relative shift between two adjacent tiles using
        several small NCC patches spread along the seam.

        A single large template can lock onto the wrong period of
        repetitive die patterns (SRAM/logic arrays); the median over
        multiple small patches at different positions along the seam is
        far more robust, and patch-to-patch agreement gives an honest
        confidence signal.

        Parameters
        ----------
        img_a : grayscale float32 tile (left neighbor for "left",
            top neighbor for "top")
        img_b : grayscale float32 current tile
        direction : "left" (img_a is left of img_b) or "top"
        max_shift_px : search range around the nominal position

        Returns
        -------
        (dx_px, dy_px, conf, n_used) or None
            Correction relative to the nominal step, in pixels, using the
            same sign convention as the original single-template code.
        """
        import cv2

        th, tw = img_a.shape
        overlap_x, overlap_y = self._get_overlap_px()
        results = []

        # Template placement: use the INNER half of the overlap strip.
        # If tile B is shifted away from tile A by stage error, content at
        # the outer edge of A's overlap strip falls off B's edge entirely
        # and can never match; content from the inner half stays inside B
        # for relative errors up to ~half the overlap width.
        if direction == "left":
            ox = min(overlap_x, tw // 2)
            inset = max(4, ox // 12)
            tmpl_w = min(max(16, ox // 2 - inset), 256)
            nominal_xb = ox - tmpl_w - inset     # expected x in tile B
            x_a = tw - ox + nominal_xb           # template x in tile A
            if tmpl_w < 16 or nominal_xb < 0:
                return None
            patch_h = min(400, max(48, (th - 2 * (th // 8)) // n_patches))
            for i in range(n_patches):
                f = (i + 1) / (n_patches + 1)
                py = int(f * (th - patch_h))
                tmpl = img_a[py:py + patch_h, x_a:x_a + tmpl_w]
                if tmpl.std() < 1e-3:
                    continue
                sy0 = max(0, py - max_shift_px)
                sy1 = min(th, py + patch_h + max_shift_px)
                sx0 = max(0, nominal_xb - max_shift_px)
                sx1 = min(tw, nominal_xb + tmpl_w + max_shift_px)
                search = img_b[sy0:sy1, sx0:sx1]
                if search.shape[0] < tmpl.shape[0] or search.shape[1] < tmpl.shape[1]:
                    continue
                r = cv2.matchTemplate(search, tmpl, cv2.TM_CCOEFF_NORMED)
                _, conf, _, loc = cv2.minMaxLoc(r)
                if conf < min_conf:
                    continue
                sub_x, sub_y = self._subpixel_peak(r, loc[0], loc[1])
                found_xb = sx0 + sub_x
                found_yb = sy0 + sub_y
                # correction relative to nominal step
                dx = -(found_xb - nominal_xb)
                dy = -(found_yb - py)
                results.append((dx, dy, conf))
        else:  # "top"
            oy = min(overlap_y, th // 2)
            inset = max(4, oy // 12)
            tmpl_h = min(max(16, oy // 2 - inset), 256)
            nominal_yb = oy - tmpl_h - inset
            y_a = th - oy + nominal_yb
            if tmpl_h < 16 or nominal_yb < 0:
                return None
            patch_w = min(400, max(48, (tw - 2 * (tw // 8)) // n_patches))
            for i in range(n_patches):
                f = (i + 1) / (n_patches + 1)
                px = int(f * (tw - patch_w))
                tmpl = img_a[y_a:y_a + tmpl_h, px:px + patch_w]
                if tmpl.std() < 1e-3:
                    continue
                sx0 = max(0, px - max_shift_px)
                sx1 = min(tw, px + patch_w + max_shift_px)
                sy0 = max(0, nominal_yb - max_shift_px)
                sy1 = min(th, nominal_yb + tmpl_h + max_shift_px)
                search = img_b[sy0:sy1, sx0:sx1]
                if search.shape[0] < tmpl.shape[0] or search.shape[1] < tmpl.shape[1]:
                    continue
                r = cv2.matchTemplate(search, tmpl, cv2.TM_CCOEFF_NORMED)
                _, conf, _, loc = cv2.minMaxLoc(r)
                if conf < min_conf:
                    continue
                sub_x, sub_y = self._subpixel_peak(r, loc[0], loc[1])
                found_xb = sx0 + sub_x
                found_yb = sy0 + sub_y
                dx = -(found_xb - px)
                dy = -(found_yb - nominal_yb)
                results.append((dx, dy, conf))

        if not results:
            return None
        arr = np.array(results, dtype=np.float64)
        dx = float(np.median(arr[:, 0]))
        dy = float(np.median(arr[:, 1]))
        conf = float(arr[:, 2].mean())
        if abs(dx) > max_shift_px or abs(dy) > max_shift_px:
            return None
        # Patch agreement check: on repetitive patterns individual patches
        # can lock onto different pattern periods. If the patches do not
        # agree on a single shift, the seam is ambiguous - reject it and
        # let the global solve position this tile from its other seams.
        if len(results) >= 3:
            mad_dx = float(np.median(np.abs(arr[:, 0] - dx)))
            mad_dy = float(np.median(np.abs(arr[:, 1] - dy)))
            if mad_dx > 3.0 or mad_dy > 3.0:
                return None
        return dx, dy, conf, len(results)

    def align(self, max_shift_px: int = 0, flat_field: Optional[np.ndarray] = None,
              _retry_depth: int = 0):
        """
        Refine tile positions using multi-patch NCC matching on overlaps.

        For each tile, compares with left and top neighbors using several
        small NCC patches per seam (median-aggregated), which is robust
        against the repetitive patterns of semiconductor dies. If fewer
        than 40% of pairs match, the search range is doubled and alignment
        is retried (handles larger-than-expected stage error, observed up
        to ~600 µm in the field).

        Parameters
        ----------
        max_shift_px : int
            Maximum allowed shift from nominal position (rejects bad matches).
            0 = auto-compute from pixel size (~250 µm worst-case stage error).
        """
        tile_shape = self._get_tile_shape()
        tile_h, tile_w = tile_shape[0], tile_shape[1]

        auto_shift = max_shift_px <= 0
        if auto_shift:
            max_shift_px = self._auto_max_shift() * (2 ** _retry_depth)
            max_shift_px = min(max_shift_px, max(tile_w // 3, 80))

        overlap_x_px, overlap_y_px = self._get_overlap_px()

        if overlap_x_px < 16 or overlap_y_px < 16:
            logger.warning("Overlap too small for alignment, using nominal positions")
            return

        tile_map = {(t.row, t.col): t for t in self.tiles}
        img_cache = {}

        # Flat-field the tiles before measuring NCC: the vignette gradient
        # at tile edges (where the overlap strips live) is a smooth ramp of
        # the same order as the die texture and measurably degrades the
        # correlation peak; matching on corrected tiles is far more reliable.
        ff_lum = None
        if flat_field is not None:
            ff_lum = flat_field.mean(axis=2).astype(np.float32) \
                if flat_field.ndim == 3 else flat_field.astype(np.float32)

        def get_gray(filename):
            if filename not in img_cache:
                img = self._load_and_crop(filename)
                if len(img.shape) == 3:
                    img = np.mean(img, axis=2).astype(np.float32)
                else:
                    img = img.astype(np.float32)
                if ff_lum is not None and img.shape == ff_lum.shape:
                    img = img * ff_lum
                img_cache[filename] = img
                # keep roughly two rows of tiles in memory
                if len(img_cache) > 2 * self._cols + 4:
                    img_cache.pop(next(iter(img_cache)))
            return img_cache[filename]

        n_aligned = 0
        total_pairs = 0
        shifts = {}  # (row, col) -> list of (dx_px, dy_px, conf, direction)

        print(f"Aligning tiles (overlap: {overlap_x_px}x{overlap_y_px} px, "
              f"search: +/-{max_shift_px} px, multi-patch NCC)...")

        for tile in sorted(self.tiles, key=lambda t: (t.row, t.col)):
            measurements = []

            for direction, drc in (("left", (0, -1)), ("top", (-1, 0))):
                neighbor = tile_map.get((tile.row + drc[0], tile.col + drc[1]))
                if not neighbor:
                    continue
                total_pairs += 1
                try:
                    img_a = get_gray(neighbor.filename)
                    img_b = get_gray(tile.filename)
                    m = self._match_pair_multi(img_a, img_b, direction, max_shift_px)
                    if m:
                        dx_px, dy_px, conf, n_used = m
                        measurements.append((dx_px, dy_px, conf, direction))
                        n_aligned += 1
                        logger.debug(
                            f"  [{tile.row},{tile.col}] {direction}: dx={dx_px:.1f} "
                            f"dy={dy_px:.1f} conf={conf:.3f} patches={n_used}")
                except Exception as e:
                    logger.warning(f"  [{tile.row},{tile.col}] {direction} align failed: {e}")

            if measurements:
                shifts[(tile.row, tile.col)] = measurements
                tile.confidence = max(m[2] for m in measurements)
                tile.aligned = True

        img_cache.clear()
        print(f"Alignment: {n_aligned}/{total_pairs} pairs refined successfully")

        # Auto-retry with a wider search when the match rate is poor: the
        # most common cause is stage error exceeding the assumed 250 µm,
        # which previously made every match fail silently and produced a
        # nominal-position (badly seamed) stitch.
        if (auto_shift and total_pairs > 0 and _retry_depth < 2
                and n_aligned < 0.4 * total_pairs):
            print(f"  Low match rate ({n_aligned}/{total_pairs}); retrying with "
                  f"doubled search range...")
            for t in self.tiles:
                t.confidence = 0.0
                t.aligned = False
                t.refined_x = t.nominal_x
                t.refined_y = t.nominal_y
            return self.align(max_shift_px=0, flat_field=flat_field,
                              _retry_depth=_retry_depth + 1)

        # --- Global least-squares optimization ---
        # Each NCC measurement gives the ideal relative position between
        # two adjacent tiles. Solve for all positions simultaneously to
        # minimize total weighted error across all pairs.
        #
        # For each pair (A, B) with measured shift (dx, dy, conf):
        #   position_B = position_A + nominal_step + correction
        #   => position_B - position_A = nominal_step + (dx, dy) * pixel_size
        #
        # We fix tile[0] at its nominal position and solve for all others.

        # Build list of pairwise constraints
        pair_constraints = []  # (idx_a, idx_b, target_x, target_y, weight)

        # Map (row, col) -> tile index
        rc_to_idx = {(t.row, t.col): i for i, t in enumerate(self.tiles)}

        for tile in self.tiles:
            if (tile.row, tile.col) not in shifts:
                continue
            idx_b = rc_to_idx[(tile.row, tile.col)]

            for meas in shifts[(tile.row, tile.col)]:
                dx_px, dy_px, conf, direction = meas

                # Use the tagged direction to find the correct neighbor
                if direction == "left":
                    neighbor = tile_map.get((tile.row, tile.col - 1))
                elif direction == "top":
                    neighbor = tile_map.get((tile.row - 1, tile.col))
                else:
                    continue

                if neighbor is None:
                    continue

                idx_a = rc_to_idx.get((neighbor.row, neighbor.col))
                if idx_a is None:
                    continue

                # Target: position_B - position_A should equal:
                #   nominal_step + NCC_correction
                nominal_dx_um = tile.nominal_x - neighbor.nominal_x
                nominal_dy_um = tile.nominal_y - neighbor.nominal_y
                target_x = nominal_dx_um + dx_px * self.pixel_size_um
                target_y = nominal_dy_um + dy_px * self.pixel_size_um

                # Use conf^2 so low-confidence matches have much less
                # influence (0.5 -> 0.25 weight, 0.8 -> 0.64 weight)
                pair_constraints.append((idx_a, idx_b, target_x, target_y, conf ** 2))

        n_tiles = len(self.tiles)

        if pair_constraints and n_tiles > 1:
            # ── Compute median systematic correction ──
            # The stage has consistent positioning error. Compute the median
            # NCC correction for horizontal and vertical steps separately,
            # then apply as corrected nominal positions for all tiles.
            # This ensures tiles without NCC data still get the systematic
            # error removed, preventing choppy edges at tile boundaries.
            h_corrections_x = []
            h_corrections_y = []
            v_corrections_x = []
            v_corrections_y = []

            for tile in self.tiles:
                if (tile.row, tile.col) not in shifts:
                    continue
                for meas in shifts[(tile.row, tile.col)]:
                    dx_px, dy_px, conf, direction = meas
                    if conf < 0.7:  # only use high-confidence for median
                        continue
                    corr_x = dx_px * self.pixel_size_um
                    corr_y = dy_px * self.pixel_size_um
                    if direction == "left":
                        h_corrections_x.append(corr_x)
                        h_corrections_y.append(corr_y)
                    elif direction == "top":
                        v_corrections_x.append(corr_x)
                        v_corrections_y.append(corr_y)

            median_h_cx = float(np.median(h_corrections_x)) if h_corrections_x else 0.0
            median_h_cy = float(np.median(h_corrections_y)) if h_corrections_y else 0.0
            median_v_cx = float(np.median(v_corrections_x)) if v_corrections_x else 0.0
            median_v_cy = float(np.median(v_corrections_y)) if v_corrections_y else 0.0

            print(f"  Median H correction: dx={median_h_cx:.1f} dy={median_h_cy:.1f} um "
                  f"({len(h_corrections_x)} samples)")
            print(f"  Median V correction: dx={median_v_cx:.1f} dy={median_v_cy:.1f} um "
                  f"({len(v_corrections_x)} samples)")

            # Build corrected nominal positions by accumulating median
            # corrections from tile (0,0) outward
            corrected_nominal = {}
            tile0 = self.tiles[0]
            corrected_nominal[(tile0.row, tile0.col)] = (tile0.nominal_x, tile0.nominal_y)

            # Get unique sorted rows and cols
            all_rows = sorted(set(t.row for t in self.tiles))
            all_cols = sorted(set(t.col for t in self.tiles))

            # Fill corrected positions row by row
            for r in all_rows:
                for c in all_cols:
                    if (r, c) in corrected_nominal:
                        continue
                    tile = tile_map.get((r, c))
                    if tile is None:
                        continue

                    # Try to derive from left neighbor
                    if c > all_cols[0] and (r, c - 1) in corrected_nominal:
                        prev_x, prev_y = corrected_nominal[(r, c - 1)]
                        prev_tile = tile_map.get((r, c - 1))
                        if prev_tile:
                            nom_step_x = tile.nominal_x - prev_tile.nominal_x
                            nom_step_y = tile.nominal_y - prev_tile.nominal_y
                            corrected_nominal[(r, c)] = (
                                prev_x + nom_step_x + median_h_cx,
                                prev_y + nom_step_y + median_h_cy
                            )
                            continue

                    # Try to derive from top neighbor
                    if r > all_rows[0] and (r - 1, c) in corrected_nominal:
                        prev_x, prev_y = corrected_nominal[(r - 1, c)]
                        prev_tile = tile_map.get((r - 1, c))
                        if prev_tile:
                            nom_step_x = tile.nominal_x - prev_tile.nominal_x
                            nom_step_y = tile.nominal_y - prev_tile.nominal_y
                            corrected_nominal[(r, c)] = (
                                prev_x + nom_step_x + median_v_cx,
                                prev_y + nom_step_y + median_v_cy
                            )
                            continue

                    # Fallback: use raw nominal
                    corrected_nominal[(r, c)] = (tile.nominal_x, tile.nominal_y)

            # Solve via weighted least squares
            # +n_tiles for corrected-nominal anchors on every tile
            n_constraints = len(pair_constraints) + n_tiles
            A_x = np.zeros((n_constraints, n_tiles), dtype=np.float64)
            b_x = np.zeros(n_constraints, dtype=np.float64)
            A_y = np.zeros((n_constraints, n_tiles), dtype=np.float64)
            b_y = np.zeros(n_constraints, dtype=np.float64)
            W = np.zeros(n_constraints, dtype=np.float64)

            for i, (idx_a, idx_b, tgt_x, tgt_y, conf) in enumerate(pair_constraints):
                A_x[i, idx_b] = 1.0
                A_x[i, idx_a] = -1.0
                b_x[i] = tgt_x
                A_y[i, idx_b] = 1.0
                A_y[i, idx_a] = -1.0
                b_y[i] = tgt_y
                W[i] = conf

            # Corrected-nominal anchors for ALL tiles — uses median NCC
            # correction so even tiles without NCC data get the systematic
            # stage error removed. Tiles WITH pair measurements only get a
            # token anchor: a moderate anchor would fight correct NCC
            # constraints whenever the true stage error deviates from the
            # median-corrected grid (and would then poison the outlier
            # reweighting below).
            measured = set()
            for idx_a, idx_b, _tx, _ty, _w in pair_constraints:
                measured.add(idx_a)
                measured.add(idx_b)
            for i, tile in enumerate(self.tiles):
                row_idx = len(pair_constraints) + i
                A_x[row_idx, i] = 1.0
                A_y[row_idx, i] = 1.0
                cn = corrected_nominal.get((tile.row, tile.col),
                                           (tile.nominal_x, tile.nominal_y))
                b_x[row_idx] = cn[0]
                b_y[row_idx] = cn[1]
                if i == 0:
                    W[row_idx] = 10.0
                elif i in measured:
                    # token anchor: keeps the system well-conditioned but
                    # must not bend the geometry away from NCC constraints
                    W[row_idx] = 0.002
                else:
                    W[row_idx] = 0.3

            # Weighted least squares with iterative outlier rejection:
            # minimize sum(w_i * (A_i @ x - b_i)^2), then downweight pair
            # constraints whose residuals are large (Cauchy weights) and
            # re-solve. A single wrong NCC lock on a repetitive pattern can
            # otherwise warp the whole grid; 3-4 iterations are enough to
            # neutralize such outliers (observed 32 px -> ~1 px RMS on a
            # 493-tile production scan).
            n_pairs = len(pair_constraints)
            W_pairs0 = W[:n_pairs].copy()
            err_scale_um = 3.0 * self.pixel_size_um  # ~3 px inlier threshold
            inlier_rms_px = 0.0
            for it in range(4):
                W_sqrt = np.sqrt(W)
                Aw_x = A_x * W_sqrt[:, np.newaxis]
                bw_x = b_x * W_sqrt
                Aw_y = A_y * W_sqrt[:, np.newaxis]
                bw_y = b_y * W_sqrt

                pos_x, _, _, _ = np.linalg.lstsq(Aw_x, bw_x, rcond=None)
                pos_y, _, _, _ = np.linalg.lstsq(Aw_y, bw_y, rcond=None)

                res_x = A_x[:n_pairs] @ pos_x - b_x[:n_pairs]
                res_y = A_y[:n_pairs] @ pos_y - b_y[:n_pairs]
                err_um = np.sqrt(res_x ** 2 + res_y ** 2)
                err_px = err_um / self.pixel_size_um
                inliers = err_px <= 3.0
                n_out = int((~inliers).sum())
                inlier_rms_px = float(np.sqrt((err_px[inliers] ** 2).mean())) \
                    if inliers.any() else 0.0
                print(f"  solve iter {it}: inlier rms {inlier_rms_px:.2f} px, "
                      f"outliers(>3px) {n_out}")
                if n_out == 0:
                    break
                W[:n_pairs] = W_pairs0 / (1.0 + (err_um / err_scale_um) ** 2)

            for i, tile in enumerate(self.tiles):
                tile.refined_x = pos_x[i]
                tile.refined_y = pos_y[i]

            print(f"Global optimization: {len(pair_constraints)} constraints, {n_tiles} tiles, {n_tiles} corrected-nominal anchors")

            # ── 2D bilinear grid smoothing with NCC preservation ──
            # The stage moves in straight lines, so the baseline grid
            # should be bilinear: pos = a*col + b*row + c.
            # But tiles with good NCC matches have accurate local
            # corrections that should be preserved for interior seam
            # quality. Solution: blend between bilinear grid and WLS
            # positions based on each tile's NCC confidence.
            #   - Low/no NCC (edges, background) → stay on smooth grid
            #   - High NCC (interior, features) → keep WLS corrections
            wls_x = pos_x.copy()
            wls_y = pos_y.copy()

            tile_cols = np.array([t.col for t in self.tiles], dtype=np.float64)
            tile_rows = np.array([t.row for t in self.tiles], dtype=np.float64)
            A_fit = np.column_stack([tile_cols, tile_rows,
                                     np.ones(n_tiles)])
            fit_x, _, _, _ = np.linalg.lstsq(A_fit, pos_x, rcond=None)
            fit_y, _, _, _ = np.linalg.lstsq(A_fit, pos_y, rcond=None)

            # Compute per-tile NCC confidence: average of best H and V
            # confidence for that tile (0 if no NCC data)
            tile_conf = np.zeros(n_tiles)
            for tile in self.tiles:
                if (tile.row, tile.col) not in shifts:
                    continue
                idx = rc_to_idx[(tile.row, tile.col)]
                confs = [m[2] for m in shifts[(tile.row, tile.col)]]
                tile_conf[idx] = max(confs) if confs else 0.0

            # Also propagate confidence from neighbors: a tile with no
            # NCC but whose neighbors all have high NCC should also get
            # some confidence (its WLS position is constrained by them).
            neighbor_conf = np.zeros(n_tiles)
            for tile in self.tiles:
                idx = rc_to_idx[(tile.row, tile.col)]
                n_confs = []
                for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                    nb = tile_map.get((tile.row + dr, tile.col + dc))
                    if nb:
                        nb_idx = rc_to_idx[(nb.row, nb.col)]
                        n_confs.append(tile_conf[nb_idx])
                if n_confs:
                    neighbor_conf[idx] = np.mean(n_confs)

            # Blend weight: ramp from 0 (pure grid) to 1 (pure WLS)
            # based on tile's own confidence + neighbor confidence
            # Threshold at 0.6: below this, trust the grid more
            max_correction = 0.0
            n_grid = 0
            n_ncc = 0
            for i, t in enumerate(self.tiles):
                grid_x = fit_x[0] * t.col + fit_x[1] * t.row + fit_x[2]
                grid_y = fit_y[0] * t.col + fit_y[1] * t.row + fit_y[2]

                # Use own confidence primarily, neighbor conf as fallback
                conf = max(tile_conf[i], neighbor_conf[i] * 0.5)
                # Ramp: 0 below 0.35, full WLS trust at conf >= 0.6.
                # Multi-patch median measurements are reliable well below
                # the previous 0.9 threshold; pulling well-measured tiles
                # toward a bilinear grid re-introduces seam error when the
                # stage error is not smooth.
                alpha = np.clip((conf - 0.35) / 0.25, 0.0, 1.0)

                new_x = grid_x + alpha * (wls_x[i] - grid_x)
                new_y = grid_y + alpha * (wls_y[i] - grid_y)

                correction = max(abs(new_x - wls_x[i]),
                                 abs(new_y - wls_y[i]))
                max_correction = max(max_correction, correction)
                pos_x[i] = new_x
                pos_y[i] = new_y
                t.refined_x = new_x
                t.refined_y = new_y

                if alpha < 0.1:
                    n_grid += 1
                else:
                    n_ncc += 1

            print(f"  Grid smoothing: {n_grid} tiles on grid, {n_ncc} tiles NCC-corrected, "
                  f"max grid correction {max_correction:.1f} um")
        else:
            print("No alignment data for global optimization, using nominal positions")

    # ─── Photometrics (flat-field + brightness matching) ─────────────

    _DS = 8  # downsample factor for the photometric tile stack

    def _get_ds_stack(self) -> np.ndarray:
        """Downsampled (1/8) stack of all tiles, cached, float32.

        Both the flat-field estimate and the brightness gains work on
        low-frequency content, so a 1/8 stack is statistically identical
        and avoids re-reading every tile from disk multiple times.
        Shape: (n_tiles, h, w[, ch]).
        """
        import cv2

        if getattr(self, "_ds_stack", None) is None:
            tile_shape = self._get_tile_shape()
            h = max(2, tile_shape[0] // self._DS)
            w = max(2, tile_shape[1] // self._DS)
            stack = []
            for tile in self.tiles:
                img = self._load_and_crop(tile.filename, np.float32)
                stack.append(cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA))
            self._ds_stack = np.stack(stack)
        return self._ds_stack

    def _dtype_ceiling(self) -> float:
        return 65535.0 if self._tile_dtype == np.uint16 else 255.0

    def _compute_brightness_gains(self, vignette_corr=None) -> Dict[str, float]:
        """
        Compute per-tile brightness correction factors so that adjacent tiles
        have matching brightness in their overlap regions.

        Uses the overlap strips between neighboring tiles to compute relative
        brightness ratios (on flat-field-corrected data, so vignetting does
        not bias them), solves for per-tile gains in the log domain, then
        applies HIGHLIGHT PROTECTION: one global rescale of all gains so
        that no tile's bright content (p99.5) is amplified past the dtype
        ceiling. Without this, bright tiles get pushed into saturation and
        real highlight detail is destroyed (observed on bright HBM dies).

        Returns
        -------
        dict
            {filename: gain_factor}.
        """
        import cv2

        tile_shape = self._get_tile_shape()
        overlap_x, overlap_y = self._get_overlap_px()

        if overlap_x < 8 or overlap_y < 8:
            return {t.filename: 1.0 for t in self.tiles}

        tile_map = {(t.row, t.col): t for t in self.tiles}
        rc_to_idx = {(t.row, t.col): i for i, t in enumerate(self.tiles)}

        print("Computing brightness matching...")

        stack = self._get_ds_stack()
        ds = self._DS
        h, w = stack.shape[1], stack.shape[2]
        ox = max(1, int(round(overlap_x / ds)))
        oy = max(1, int(round(overlap_y / ds)))

        # luminance stack, flat-field corrected so vignetting doesn't bias ratios
        if stack.ndim == 4:
            lum = stack.mean(axis=3)
        else:
            lum = stack
        if vignette_corr is not None:
            vc = cv2.resize(vignette_corr, (w, h), interpolation=cv2.INTER_AREA)
            vc_lum = vc.mean(axis=2) if vc.ndim == 3 else vc
            lum = lum * vc_lum[np.newaxis, :, :]

        ratios = []  # (idx_a, idx_b, ratio)
        for tile in self.tiles:
            idx_b = rc_to_idx[(tile.row, tile.col)]
            left = tile_map.get((tile.row, tile.col - 1))
            if left:
                idx_a = rc_to_idx[(left.row, left.col)]
                mean_a = lum[idx_a][:, -ox:].mean()
                mean_b = lum[idx_b][:, :ox].mean()
                if mean_a > 1 and mean_b > 1:
                    ratios.append((idx_a, idx_b, mean_a / mean_b))
            top = tile_map.get((tile.row - 1, tile.col))
            if top:
                idx_a = rc_to_idx[(top.row, top.col)]
                mean_a = lum[idx_a][-oy:, :].mean()
                mean_b = lum[idx_b][:oy, :].mean()
                if mean_a > 1 and mean_b > 1:
                    ratios.append((idx_a, idx_b, mean_a / mean_b))

        if not ratios:
            return {t.filename: 1.0 for t in self.tiles}

        # Solve for per-tile log-gains using least squares:
        # log(gain_b) - log(gain_a) = log(mean_a/mean_b) = log(ratio)
        n = len(self.tiles)
        A = np.zeros((len(ratios) + 1, n), dtype=np.float64)
        b = np.zeros(len(ratios) + 1, dtype=np.float64)
        for i, (idx_a, idx_b, ratio) in enumerate(ratios):
            A[i, idx_b] = 1.0
            A[i, idx_a] = -1.0
            b[i] = np.log(ratio)
        # Anchor: mean of all log-gains = 0 (preserve overall brightness)
        A[-1, :] = 1.0 / n
        b[-1] = 0.0

        log_gains, _, _, _ = np.linalg.lstsq(A, b, rcond=None)
        gains = np.exp(log_gains)
        gains = np.clip(gains, 0.5, 2.0)

        # ── Highlight protection ──
        # Rescale ALL gains by one global constant so the corrected bright
        # content of every tile stays below the dtype ceiling. A global
        # constant keeps tile-to-tile matching intact; the image is just
        # uniformly darker and no highlight information is clipped away.
        #
        # Peaks are measured PER CHANNEL (clipping happens per channel, and
        # luminance understates e.g. a saturated red) on the 1/8 stack, at
        # p99.8 with a 1.15x headroom factor: the area-downsampled stack
        # suppresses narrow bright features, and the headroom compensates.
        # A high percentile (not the max) is used deliberately so a few
        # pixels that are already clipped in the source cannot force a
        # needless global darkening of the whole canvas.
        ceiling = self._dtype_ceiling()
        corr = stack
        if vignette_corr is not None:
            corr = stack * vc[np.newaxis]
        hi = np.array([np.percentile(corr[i], 99.8) for i in range(n)])
        peak_max = float((hi * gains).max()) * 1.15
        if peak_max > 0.98 * ceiling:
            s = 0.98 * ceiling / peak_max
            gains = gains * s
            print(f"  Highlight protection: global gain scale {s:.3f}")

        gain_range = gains.max() / max(gains.min(), 1e-9)
        print(f"  Brightness gain range: {gains.min():.3f} - {gains.max():.3f} "
              f"(ratio: {gain_range:.3f})")

        return {tile.filename: float(gains[i]) for i, tile in enumerate(self.tiles)}

    # ─── Stitching ────────────────────────────────────────────────────

    def _compute_vignetting_map(self) -> Optional[np.ndarray]:
        """
        Compute a per-channel flat-field (vignetting) correction map from
        the MEDIAN of the BRIGHTEST ~40% of tiles.

        Bright periphery tiles act as near-blank fields, giving a much
        cleaner illumination estimate than averaging all tiles: dark,
        structured die tiles bias a mean-based estimate and leave a
        residual tile grid in bright regions of the stitched result.
        The median also rejects content outliers within the bright subset.

        Returns
        -------
        np.ndarray or None
            Correction multiplier map (same shape as a tile, per-channel).
            Multiply each tile by this to correct vignetting.
            Returns None if fewer than 4 tiles.
        """
        import cv2

        if len(self.tiles) < 4:
            return None

        tile_shape = self._get_tile_shape()
        tile_h, tile_w = tile_shape[0], tile_shape[1]
        is_color = len(tile_shape) == 3

        print("Computing vignetting correction map...")

        stack = self._get_ds_stack()

        # select the brightest ~40% of tiles (near-blank field tiles);
        # fall back to all tiles when the scan is small
        means = stack.reshape(len(stack), -1).mean(axis=1)
        sel = means >= np.percentile(means, 60)
        if sel.sum() < min(20, len(stack)):
            sel = np.ones(len(stack), dtype=bool)
        med = np.median(stack[sel], axis=0).astype(np.float32)

        h, w = med.shape[:2]
        ksize = (max(h, w) // 3) | 1
        if is_color:
            field = np.empty_like(med)
            for c in range(med.shape[2]):
                blur = cv2.GaussianBlur(med[:, :, c], (ksize, ksize), 0)
                center_val = float(blur[h // 2, w // 2])
                if center_val > 0:
                    field[:, :, c] = center_val / np.maximum(blur, 1.0)
                else:
                    field[:, :, c] = 1.0
        else:
            blur = cv2.GaussianBlur(med, (ksize, ksize), 0)
            center_val = float(blur[h // 2, w // 2])
            if center_val > 0:
                field = center_val / np.maximum(blur, 1.0)
            else:
                field = np.ones_like(med)

        field = np.clip(field, 0.4, 2.5).astype(np.float32)
        field = cv2.resize(field, (tile_w, tile_h), interpolation=cv2.INTER_LINEAR)

        print(f"  Flat-field from {int(sel.sum())}/{len(stack)} brightest tiles, "
              f"range {field.min():.3f} - {field.max():.3f}")
        return field

    def stitch(
        self,
        output_path: str = "stitched.tif",
        align: bool = True,
        blend: bool = True,
        max_shift_px: int = 0,
        correct_vignetting: bool = True,
        match_brightness: bool = True,
    ) -> str:
        """
        Stitch all tiles into a single image.

        Parameters
        ----------
        output_path : str
            Output file path (.tif recommended)
        align : bool
            If True, run phase correlation alignment before stitching
        blend : bool
            If True, use linear blending at overlaps. If False, just overwrite.
        max_shift_px : int
            Maximum alignment correction in pixels.
            0 = auto-compute from pixel size (~250 µm worst-case stage error).
        correct_vignetting : bool
            If True, apply flat-field vignetting correction before blending
        match_brightness : bool
            If True, equalize brightness across tile seams before blending

        Returns
        -------
        str
            Path to the stitched output file
        """
        start_time = time.time()

        tile_shape = self._get_tile_shape()
        tile_h, tile_w = tile_shape[0], tile_shape[1]
        is_color = len(tile_shape) == 3
        n_channels = tile_shape[2] if is_color else 1

        # Compute vignetting correction first: alignment NCC works much
        # better on flat-fielded tiles (the vignette ramp at tile edges
        # otherwise degrades the correlation peaks in the overlap strips)
        vignette_corr = None
        if correct_vignetting:
            vignette_corr = self._compute_vignetting_map()

        # Run alignment
        if align and self._overlap_pct > 0:
            self.align(max_shift_px=max_shift_px, flat_field=vignette_corr)

        # Compute brightness matching gains (flat-field aware, with
        # highlight protection)
        brightness_gains = None
        if match_brightness:
            brightness_gains = self._compute_brightness_gains(vignette_corr)

        # free the photometric stack before allocating the canvas
        self._ds_stack = None

        # Calculate canvas size from refined positions
        positions = self._compute_pixel_positions()
        max_x = max(p[0] + tile_w for p in positions.values())
        max_y = max(p[1] + tile_h for p in positions.values())
        canvas_w = int(np.ceil(max_x))
        canvas_h = int(np.ceil(max_y))

        dtype_size = np.dtype(self._tile_dtype).itemsize
        canvas_mb = (canvas_w * canvas_h * n_channels * dtype_size) / (1024 * 1024)
        print(f"\nStitching {len(self.tiles)} tiles into {canvas_w} x {canvas_h} canvas ({canvas_mb:.0f} MB, {self._tile_dtype})...")

        # For gigapixel canvases the in-RAM float accumulator (float canvas
        # + weight map = 4*(ch+1) bytes/px) would exhaust memory; switch to
        # a streaming band compositor that writes the final dtype directly
        # to a disk-backed memmap.
        accum_bytes = canvas_w * canvas_h * (n_channels + 1) * 4
        if accum_bytes > 6_000_000_000:
            try:
                import tifffile  # noqa: F401  (required for memmap output)
                return self._stitch_streaming(
                    output_path, blend, vignette_corr, brightness_gains,
                    positions, canvas_w, canvas_h, start_time)
            except ImportError:
                logger.warning(
                    "tifffile not available; falling back to in-RAM stitch "
                    "of a very large canvas (may exhaust memory)")

        # Allocate canvas and weight map
        if is_color:
            canvas = np.zeros((canvas_h, canvas_w, n_channels), dtype=np.float32)
        else:
            canvas = np.zeros((canvas_h, canvas_w), dtype=np.float32)
        weight_map = np.zeros((canvas_h, canvas_w), dtype=np.float32)

        # Generate blending weights for a single tile
        if blend:
            tile_weight = self._make_blend_weight(tile_h, tile_w)
        else:
            tile_weight = np.ones((tile_h, tile_w), dtype=np.float32)

        # Place each tile
        for i, tile in enumerate(self.tiles):
            px, py = positions[tile.filename]
            ix, iy = int(round(px)), int(round(py))

            # Load tile (with edge crop applied)
            img = self._load_and_crop(tile.filename, np.float32)

            # Apply vignetting correction (flatten illumination before blending)
            if vignette_corr is not None and img.shape[:2] == vignette_corr.shape[:2]:
                img = img * vignette_corr

            # Apply brightness matching
            if brightness_gains is not None and tile.filename in brightness_gains:
                img = img * brightness_gains[tile.filename]

            # Determine placement bounds (handle edge cases)
            src_y0, src_x0 = 0, 0
            dst_y0, dst_x0 = iy, ix
            h, w = tile_h, tile_w

            # Clip to canvas bounds
            if dst_y0 < 0:
                src_y0 = -dst_y0
                h += dst_y0
                dst_y0 = 0
            if dst_x0 < 0:
                src_x0 = -dst_x0
                w += dst_x0
                dst_x0 = 0
            if dst_y0 + h > canvas_h:
                h = canvas_h - dst_y0
            if dst_x0 + w > canvas_w:
                w = canvas_w - dst_x0

            if h <= 0 or w <= 0:
                continue

            # Extract relevant portions
            src_region = img[src_y0:src_y0 + h, src_x0:src_x0 + w]
            wt_region = tile_weight[src_y0:src_y0 + h, src_x0:src_x0 + w]

            # Weighted accumulation
            if is_color:
                for c in range(n_channels):
                    canvas[dst_y0:dst_y0 + h, dst_x0:dst_x0 + w, c] += src_region[..., c] * wt_region
            else:
                canvas[dst_y0:dst_y0 + h, dst_x0:dst_x0 + w] += src_region * wt_region

            weight_map[dst_y0:dst_y0 + h, dst_x0:dst_x0 + w] += wt_region

            pct = (i + 1) / len(self.tiles) * 100
            print(f"  [{i + 1}/{len(self.tiles)}] ({pct:.0f}%) Placed {tile.filename} at ({ix}, {iy})")

        # Normalize by weights (avoid division by zero)
        mask = weight_map > 0
        if is_color:
            for c in range(n_channels):
                canvas[..., c][mask] /= weight_map[mask]
        else:
            canvas[mask] /= weight_map[mask]

        # Convert back to original tile dtype (uint8 for 8-bit, uint16 for 12/16-bit)
        if self._tile_dtype == np.uint16:
            canvas = np.clip(canvas, 0, 65535).astype(np.uint16)
        else:
            canvas = np.clip(canvas, 0, 255).astype(np.uint8)

        # Save
        output_full = str(self.scan_dir / output_path) if not Path(output_path).is_absolute() else output_path
        self._save_image(canvas, output_full)

        elapsed = time.time() - start_time
        print(f"\nStitched image saved: {output_full}")
        print(f"  Size: {canvas_w} x {canvas_h} px")
        print(f"  Physical: {canvas_w * self.pixel_size_um:.0f} x {canvas_h * self.pixel_size_um:.0f} um")
        print(f"  Time: {elapsed:.1f}s")

        return output_full

    def _stitch_streaming(self, output_path, blend, vignette_corr,
                          brightness_gains, positions, canvas_w, canvas_h,
                          start_time):
        """Streaming band compositor for gigapixel canvases.

        Composes the canvas in horizontal bands, accumulating weighted tile
        contributions per band and writing the final dtype directly into a
        disk-backed BigTIFF memmap. Peak memory is ~2 bands of float
        accumulators instead of the whole canvas. Outputs are then saved
        (compressed TIFF + PNG) by streaming from the memmap.
        """
        import gc
        import tifffile

        tile_shape = self._get_tile_shape()
        tile_h, tile_w = tile_shape[0], tile_shape[1]
        is_color = len(tile_shape) == 3
        band_h = 2048

        if blend:
            tile_weight = self._make_blend_weight(tile_h, tile_w)
        else:
            tile_weight = np.ones((tile_h, tile_w), dtype=np.float32)

        output_full = str(self.scan_dir / output_path) \
            if not Path(output_path).is_absolute() else output_path
        base = str(Path(output_full).with_suffix(""))
        raw_path = base + "_raw.tif"

        shape = (canvas_h, canvas_w, 3) if is_color else (canvas_h, canvas_w)
        canvas_mm = tifffile.memmap(raw_path, shape=shape,
                                    dtype=self._tile_dtype, bigtiff=True)

        # integer tile placements
        placed = []
        for tile in self.tiles:
            px, py = positions[tile.filename]
            placed.append((tile, int(round(px)), int(round(py))))

        # corrected-tile LRU cache (~2 rows of tiles)
        cache = {}

        def corrected(tile):
            if tile.filename not in cache:
                img = self._load_and_crop(tile.filename, np.float32)
                if vignette_corr is not None and img.shape[:2] == vignette_corr.shape[:2]:
                    img = img * vignette_corr
                if brightness_gains is not None and tile.filename in brightness_gains:
                    img = img * brightness_gains[tile.filename]
                cache[tile.filename] = img
                if len(cache) > 2 * self._cols + 4:
                    cache.pop(next(iter(cache)))
            return cache[tile.filename]

        ceiling = self._dtype_ceiling()
        n_bands = (canvas_h + band_h - 1) // band_h
        print(f"  Streaming compositor: {n_bands} bands of {band_h} px")

        for y0 in range(0, canvas_h, band_h):
            y1 = min(canvas_h, y0 + band_h)
            if is_color:
                acc = np.zeros((y1 - y0, canvas_w, 3), dtype=np.float32)
            else:
                acc = np.zeros((y1 - y0, canvas_w), dtype=np.float32)
            wacc = np.zeros((y1 - y0, canvas_w), dtype=np.float32)

            for tile, ix, iy in placed:
                if iy + tile_h <= y0 or iy >= y1 or ix >= canvas_w or ix + tile_w <= 0:
                    continue
                a0, a1 = max(iy, y0), min(iy + tile_h, y1)
                s0, s1 = a0 - iy, a1 - iy
                x0 = max(ix, 0)
                sx0 = x0 - ix
                w_eff = min(ix + tile_w, canvas_w) - x0
                img = corrected(tile)
                wpart = tile_weight[s0:s1, sx0:sx0 + w_eff]
                src = img[s0:s1, sx0:sx0 + w_eff]
                if is_color:
                    acc[a0 - y0:a1 - y0, x0:x0 + w_eff] += src * wpart[:, :, np.newaxis]
                else:
                    acc[a0 - y0:a1 - y0, x0:x0 + w_eff] += src * wpart
                wacc[a0 - y0:a1 - y0, x0:x0 + w_eff] += wpart

            if is_color:
                band_px = acc / np.maximum(wacc[:, :, np.newaxis], 1e-6)
            else:
                band_px = acc / np.maximum(wacc, 1e-6)
            canvas_mm[y0:y1] = np.clip(band_px, 0, ceiling).astype(self._tile_dtype)
            del acc, wacc, band_px
            gc.collect()
            print(f"  band {y0}-{y1} composed")

        canvas_mm.flush()
        del canvas_mm
        cache.clear()
        gc.collect()
        print(f"  Raw TIFF saved: {raw_path}")

        self._save_from_memmap(raw_path, output_full)

        elapsed = time.time() - start_time
        print(f"\nStitched image saved: {output_full}")
        print(f"  Size: {canvas_w} x {canvas_h} px")
        print(f"  Physical: {canvas_w * self.pixel_size_um:.0f} x "
              f"{canvas_h * self.pixel_size_um:.0f} um")
        print(f"  Time: {elapsed:.1f}s")
        return output_full

    def _save_from_memmap(self, raw_path: str, filepath: str):
        """Save compressed TIFF + PNG by streaming from the raw memmap.

        The PNG encoder needs a BGR array; staging that channel-swapped
        copy on disk (instead of RAM) avoids an out-of-memory stall on
        10+ GB canvases.
        """
        import os
        import gc
        import cv2
        import tifffile

        mm = tifffile.memmap(raw_path)
        h, w = mm.shape[:2]
        is_color = mm.ndim == 3

        tifffile.imwrite(filepath, mm, compression="zlib",
                         tile=(min(512, h), min(512, w)), bigtiff=True)
        comp_mb = Path(filepath).stat().st_size / (1024 * 1024)
        print(f"  Compressed TIFF saved: {filepath} ({comp_mb:.0f} MB)")

        max_dim = 65500
        png_path = str(Path(filepath).with_suffix(".png"))
        band_h = 2048
        if h > max_dim or w > max_dim:
            # downscale band-by-band into a disk-staged buffer so the full
            # gigapixel memmap is never materialized in RAM
            scale = min(max_dim / h, max_dim / w)
            new_w, new_h = int(w * scale), int(h * scale)
            swap_path = raw_path + ".png_small.dat"
            shape = (new_h, new_w, 3) if is_color else (new_h, new_w)
            small = np.memmap(swap_path, dtype=mm.dtype, mode="w+", shape=shape)
            out_band = max(256, int(band_h * scale))
            for oy0 in range(0, new_h, out_band):
                oy1 = min(new_h, oy0 + out_band)
                iy0 = int(oy0 / scale)
                iy1 = min(h, int(np.ceil(oy1 / scale)))
                band = np.asarray(mm[iy0:iy1])
                band_small = cv2.resize(band, (new_w, oy1 - oy0),
                                        interpolation=cv2.INTER_AREA)
                small[oy0:oy1] = band_small[:, :, ::-1] if is_color else band_small
                del band, band_small
            small.flush()
            cv2.imwrite(png_path, small, [cv2.IMWRITE_PNG_COMPRESSION, 3])
            print(f"  PNG preview saved: {png_path} ({new_w}x{new_h})")
            del small
            gc.collect()
            os.remove(swap_path)
        else:
            swap_path = raw_path + ".bgr.dat"
            bgr = np.memmap(swap_path, dtype=mm.dtype, mode="w+", shape=mm.shape)
            for y0 in range(0, h, band_h):
                y1 = min(h, y0 + band_h)
                bgr[y0:y1] = mm[y0:y1, :, ::-1] if is_color else mm[y0:y1]
            bgr.flush()
            ok = cv2.imwrite(png_path, bgr, [cv2.IMWRITE_PNG_COMPRESSION, 3])
            png_mb = Path(png_path).stat().st_size / (1024 * 1024) if ok else 0
            print(f"  PNG saved: {png_path} ({png_mb:.0f} MB)")
            del bgr
            gc.collect()
            os.remove(swap_path)
        del mm
        gc.collect()

    def _make_blend_weight(self, h: int, w: int) -> np.ndarray:
        """
        Create overlap-region blending weight map using cosine taper.

        Pixels in the overlap zone (edges) ramp from 0 to 1,
        non-overlap center pixels are all 1.0.
        """
        overlap_x, overlap_y = self._get_overlap_px()

        # Ensure minimum overlap for taper
        overlap_x = max(overlap_x, 4)
        overlap_y = max(overlap_y, 4)

        # 1D horizontal taper
        wx = np.ones(w, dtype=np.float32)
        taper_x = 0.5 * (1 - np.cos(np.linspace(0, np.pi, overlap_x)))
        wx[:overlap_x] = taper_x
        wx[-overlap_x:] = taper_x[::-1]

        # 1D vertical taper
        wy = np.ones(h, dtype=np.float32)
        taper_y = 0.5 * (1 - np.cos(np.linspace(0, np.pi, overlap_y)))
        wy[:overlap_y] = taper_y
        wy[-overlap_y:] = taper_y[::-1]

        # 2D weight = outer product
        weight = wy[:, np.newaxis] * wx[np.newaxis, :]
        return weight

    def _save_image(self, img: np.ndarray, filepath: str):
        """Save raw uncompressed TIFF + compressed TIFF + PNG."""
        ext = Path(filepath).suffix.lower()

        if ext in (".tif", ".tiff"):
            h, w = img.shape[:2]
            base = str(Path(filepath).with_suffix(""))
            use_bigtiff = img.nbytes > 2_000_000_000

            # 1. Raw uncompressed TIFF (archival quality, corruption-resistant)
            raw_path = base + "_raw.tif"
            try:
                import tifffile
                tifffile.imwrite(raw_path, img, compression=None, bigtiff=use_bigtiff)
                raw_mb = Path(raw_path).stat().st_size / (1024 * 1024)
                print(f"  Raw TIFF saved: {raw_path} ({raw_mb:.0f} MB)")
            except ImportError:
                import cv2
                raw_bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR) if (len(img.shape) == 3 and img.shape[2] == 3) else img
                cv2.imwrite(raw_path, raw_bgr)
                print(f"  Raw TIFF saved: {raw_path}")

            # 2. Compressed TIFF (tiled ZLIB, for image viewers)
            try:
                import tifffile
                tifffile.imwrite(
                    filepath, img,
                    compression="zlib",
                    tile=(min(512, h), min(512, w)),
                    bigtiff=use_bigtiff,
                )
                comp_mb = Path(filepath).stat().st_size / (1024 * 1024)
                print(f"  Compressed TIFF saved: {filepath} ({comp_mb:.0f} MB)")
            except ImportError:
                import cv2
                comp_bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR) if (len(img.shape) == 3 and img.shape[2] == 3) else img
                cv2.imwrite(filepath, comp_bgr)
                print(f"  Compressed TIFF saved: {filepath}")

            # 3. PNG (lossless, compressed, for viewers)
            # PNG has max ~65500 px per dimension, so save downscaled
            # preview if needed, plus full-res version
            import cv2
            img_bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR) if (len(img.shape) == 3 and img.shape[2] == 3) else img
            max_dim = 65500

            if h > max_dim or w > max_dim:
                # Save downscaled PNG preview
                scale = min(max_dim / h, max_dim / w)
                new_w, new_h = int(w * scale), int(h * scale)
                img_small = cv2.resize(img_bgr, (new_w, new_h), interpolation=cv2.INTER_AREA)
                png_path = str(Path(filepath).with_suffix(".png"))
                try:
                    cv2.imwrite(png_path, img_small, [cv2.IMWRITE_PNG_COMPRESSION, 3])
                    png_mb = Path(png_path).stat().st_size / (1024 * 1024)
                    print(f"  PNG preview saved: {png_path} ({png_mb:.0f} MB, {new_w}x{new_h})")
                except Exception as e:
                    print(f"  PNG preview failed: {e}")

                # Save full-res PNG separately
                full_png_path = base + "_full.png"
                try:
                    cv2.imwrite(full_png_path, img_bgr, [cv2.IMWRITE_PNG_COMPRESSION, 3])
                    full_mb = Path(full_png_path).stat().st_size / (1024 * 1024)
                    print(f"  Full-res PNG saved: {full_png_path} ({full_mb:.0f} MB)")
                except Exception as e:
                    print(f"  Full-res PNG failed: {e}")
            else:
                # Image fits in PNG limits, save directly
                png_path = str(Path(filepath).with_suffix(".png"))
                try:
                    cv2.imwrite(png_path, img_bgr, [cv2.IMWRITE_PNG_COMPRESSION, 3])
                    png_mb = Path(png_path).stat().st_size / (1024 * 1024)
                    print(f"  PNG saved: {png_path} ({png_mb:.0f} MB)")
                except Exception as e:
                    print(f"  PNG save failed: {e}")
            return

        import cv2
        if len(img.shape) == 3 and img.shape[2] == 3:
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        cv2.imwrite(filepath, img)


# ─── CLI ──────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Stitch tile images from a die mapper scan into a single image"
    )
    parser.add_argument("scan_dir", help="Directory containing tiles + scan_metadata.json")
    parser.add_argument("--output", "-o", default="stitched.tif", help="Output filename (default: stitched.tif)")
    parser.add_argument("--pixel-size", type=float, default=0, help="Pixel size in um (auto if 0)")
    parser.add_argument("--no-align", action="store_true", help="Skip alignment refinement")
    parser.add_argument("--no-blend", action="store_true", help="No blending (just overwrite)")
    parser.add_argument("--no-brightness-match", action="store_true", help="Skip brightness equalization")
    parser.add_argument("--max-shift", type=int, default=0, help="Max alignment shift in pixels (0 = auto from pixel size)")
    parser.add_argument("--edge-crop-pct", type=float, default=7.5, help="Percent of each tile edge to crop before stitching (default: 7.5, set 0 to disable)")
    parser.add_argument("--preview", action="store_true", help="Preview only, don't stitch")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    stitcher = Stitcher(args.scan_dir, pixel_size_um=args.pixel_size,
                        edge_crop_pct=args.edge_crop_pct)

    if args.preview:
        stitcher.preview()
    else:
        stitcher.stitch(
            output_path=args.output,
            align=not args.no_align,
            blend=not args.no_blend,
            max_shift_px=args.max_shift,
            match_brightness=not args.no_brightness_match,
        )


if __name__ == "__main__":
    main()
