"""
Tile Image Stitcher
====================
Takes captured tiles from the die_mapper and assembles them into a single
high-resolution stitched image.

Features:
  - Phase correlation alignment (sub-pixel refinement of tile positions)
  - Linear blending at overlap regions (no visible seams)
  - Handles grayscale and color images
  - Outputs 8-bit or 16-bit TIFF
  - Memory-efficient: processes overlap strips, not whole canvas at once
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

        The WILD stage has ~250 µm worst-case positioning error.
        Convert that to pixels and add margin, with a floor of 80 px.
        Cap at 75% of the (effective, post-crop) overlap so the search
        region stays within the physically overlapping area.
        """
        self._get_tile_shape()  # ensures pixel_size_um is set
        max_error_um = 250.0
        px = int(np.ceil(max_error_um / self.pixel_size_um)) + 20
        overlap_x, _ = self._get_overlap_px()
        cap = max(overlap_x * 3 // 4, 80)
        return min(max(px, 80), cap)

    def align(self, max_shift_px: int = 0):
        """
        Refine tile positions using NCC template matching on overlap regions.

        For each tile, compares with left and top neighbors by extracting a
        vertical/horizontal strip from the overlap zone and searching for
        the best match within ±max_shift_px of the nominal position.

        This is more robust than phase correlation for semiconductor dies
        which have repetitive patterns and small overlap.

        Parameters
        ----------
        max_shift_px : int
            Maximum allowed shift from nominal position (rejects bad matches).
            0 = auto-compute from pixel size (~250 µm worst-case stage error).
        """
        import cv2

        tile_shape = self._get_tile_shape()
        tile_h, tile_w = tile_shape[0], tile_shape[1]

        if max_shift_px <= 0:
            max_shift_px = self._auto_max_shift()

        overlap_x_px, overlap_y_px = self._get_overlap_px()

        if overlap_x_px < 16 or overlap_y_px < 16:
            logger.warning("Overlap too small for alignment, using nominal positions")
            return

        tile_map = {(t.row, t.col): t for t in self.tiles}
        img_cache = {}

        def subpixel_peak(result, ix, iy):
            """Fit parabola around integer NCC peak for subpixel precision."""
            h, w = result.shape
            fx, fy = float(ix), float(iy)
            # Horizontal subpixel
            if 0 < ix < w - 1:
                left = float(result[iy, ix - 1])
                center = float(result[iy, ix])
                right = float(result[iy, ix + 1])
                denom = 2.0 * (2.0 * center - left - right)
                if abs(denom) > 1e-7:
                    fx = ix + (left - right) / denom
            # Vertical subpixel
            if 0 < iy < h - 1:
                top = float(result[iy - 1, ix])
                center = float(result[iy, ix])
                bottom = float(result[iy + 1, ix])
                denom = 2.0 * (2.0 * center - top - bottom)
                if abs(denom) > 1e-7:
                    fy = iy + (top - bottom) / denom
            return fx, fy

        def get_gray(filename):
            if filename not in img_cache:
                img = self._load_and_crop(filename)
                if len(img.shape) == 3:
                    img = np.mean(img, axis=2).astype(np.float32)
                else:
                    img = img.astype(np.float32)
                img_cache[filename] = img
            return img_cache[filename]

        n_aligned = 0
        total_pairs = 0
        shifts = {}  # (row, col) -> list of (dx_px, dy_px, conf, direction) measurements

        print(f"Aligning tiles (overlap: {overlap_x_px}x{overlap_y_px} px, search: +/-{max_shift_px} px)...")

        for tile in self.tiles:
            measurements = []

            # --- Compare with left neighbor ---
            left = tile_map.get((tile.row, tile.col - 1))
            if left:
                total_pairs += 1
                try:
                    img_left = get_gray(left.filename)
                    img_curr = get_gray(tile.filename)

                    # Template: right edge of left tile (use center 60% vertically
                    # to avoid edge vignetting artifacts)
                    margin_y = tile_h // 5
                    tmpl_w = min(overlap_x_px, tile_w // 4)
                    template = img_left[margin_y:tile_h - margin_y,
                                        tile_w - tmpl_w:]

                    # Search region: left portion of current tile, expanded by max_shift
                    search_w = tmpl_w + 2 * max_shift_px
                    search_h_pad = max_shift_px
                    sy0 = max(0, margin_y - search_h_pad)
                    sy1 = min(tile_h, tile_h - margin_y + search_h_pad)
                    search = img_curr[sy0:sy1, :search_w]

                    result = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, max_loc = cv2.minMaxLoc(result)

                    if max_val > 0.5:
                        # Subpixel refinement around integer peak
                        sub_x, sub_y = subpixel_peak(result, max_loc[0], max_loc[1])
                        dx_px = -sub_x
                        dy_px = -(sub_y - (margin_y - sy0))

                        if abs(dx_px) <= max_shift_px and abs(dy_px) <= max_shift_px:
                            measurements.append((dx_px, dy_px, max_val, "left"))
                            n_aligned += 1
                            print(f"  [{tile.row},{tile.col}] left: dx={dx_px:.1f} dy={dy_px:.1f} conf={max_val:.3f}")
                except Exception as e:
                    logger.warning(f"  [{tile.row},{tile.col}] left align failed: {e}")

            # --- Compare with top neighbor ---
            top = tile_map.get((tile.row - 1, tile.col))
            if top:
                total_pairs += 1
                try:
                    img_top = get_gray(top.filename)
                    img_curr = get_gray(tile.filename)

                    margin_x = tile_w // 5
                    tmpl_h = min(overlap_y_px, tile_h // 4)
                    template = img_top[tile_h - tmpl_h:,
                                       margin_x:tile_w - margin_x]

                    search_h = tmpl_h + 2 * max_shift_px
                    search_w_pad = max_shift_px
                    sx0 = max(0, margin_x - search_w_pad)
                    sx1 = min(tile_w, tile_w - margin_x + search_w_pad)
                    search = img_curr[:search_h, sx0:sx1]

                    result = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, max_loc = cv2.minMaxLoc(result)

                    if max_val > 0.5:
                        sub_x, sub_y = subpixel_peak(result, max_loc[0], max_loc[1])
                        dx_px = -(sub_x - (margin_x - sx0))
                        dy_px = -sub_y

                        if abs(dx_px) <= max_shift_px and abs(dy_px) <= max_shift_px:
                            measurements.append((dx_px, dy_px, max_val, "top"))
                            n_aligned += 1
                            print(f"  [{tile.row},{tile.col}] top:  dx={dx_px:.1f} dy={dy_px:.1f} conf={max_val:.3f}")
                except Exception as e:
                    logger.warning(f"  [{tile.row},{tile.col}] top align failed: {e}")

            if measurements:
                shifts[(tile.row, tile.col)] = measurements
                tile.confidence = max(m[2] for m in measurements)
                tile.aligned = True

        img_cache.clear()
        print(f"Alignment: {n_aligned}/{total_pairs} pairs refined successfully")

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
            # stage error removed.
            for i, tile in enumerate(self.tiles):
                row_idx = len(pair_constraints) + i
                A_x[row_idx, i] = 1.0
                A_y[row_idx, i] = 1.0
                cn = corrected_nominal.get((tile.row, tile.col),
                                           (tile.nominal_x, tile.nominal_y))
                b_x[row_idx] = cn[0]
                b_y[row_idx] = cn[1]
                # Tile 0 gets strong anchor, others get moderate anchor
                W[row_idx] = 10.0 if i == 0 else 0.3

            # Weighted least squares: minimize sum(w_i * (A_i @ x - b_i)^2)
            W_sqrt = np.sqrt(W)
            Aw_x = A_x * W_sqrt[:, np.newaxis]
            bw_x = b_x * W_sqrt
            Aw_y = A_y * W_sqrt[:, np.newaxis]
            bw_y = b_y * W_sqrt

            pos_x, _, _, _ = np.linalg.lstsq(Aw_x, bw_x, rcond=None)
            pos_y, _, _, _ = np.linalg.lstsq(Aw_y, bw_y, rcond=None)

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
                # Ramp: 0 below 0.5, linear to 1.0 at conf=0.9
                alpha = np.clip((conf - 0.5) / 0.4, 0.0, 1.0)

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

    # ─── Brightness Matching ─────────────────────────────────────────

    def _compute_brightness_gains(self) -> Dict[str, float]:
        """
        Compute per-tile brightness correction factors so that adjacent tiles
        have matching brightness in their overlap regions.

        Uses the overlap strips between neighboring tiles to compute relative
        brightness ratios, then solves for per-tile gain factors that minimize
        brightness differences across all seams.

        Returns
        -------
        dict
            {filename: gain_factor} where gain_factor ~1.0 for most tiles.
        """
        tile_shape = self._get_tile_shape()
        tile_h, tile_w = tile_shape[0], tile_shape[1]

        overlap_x, overlap_y = self._get_overlap_px()

        if overlap_x < 8 or overlap_y < 8:
            return {t.filename: 1.0 for t in self.tiles}

        tile_map = {(t.row, t.col): t for t in self.tiles}

        # Collect brightness ratios between neighbors
        # Each ratio: mean_brightness(tile_a_overlap) / mean_brightness(tile_b_overlap)
        ratios = []  # (idx_a, idx_b, ratio)
        rc_to_idx = {(t.row, t.col): i for i, t in enumerate(self.tiles)}

        print("Computing brightness matching...")

        for tile in self.tiles:
            idx_b = rc_to_idx[(tile.row, tile.col)]

            # Check left neighbor
            left = tile_map.get((tile.row, tile.col - 1))
            if left:
                idx_a = rc_to_idx[(left.row, left.col)]
                try:
                    img_a = self._load_and_crop(left.filename, np.float64)
                    img_b = self._load_and_crop(tile.filename, np.float64)
                    strip_a = img_a[:, -overlap_x:]  # right strip of left tile
                    strip_b = img_b[:, :overlap_x]   # left strip of right tile
                    mean_a = strip_a.mean()
                    mean_b = strip_b.mean()
                    if mean_a > 1 and mean_b > 1:
                        ratios.append((idx_a, idx_b, mean_a / mean_b))
                except Exception:
                    pass

            # Check top neighbor
            top = tile_map.get((tile.row - 1, tile.col))
            if top:
                idx_a = rc_to_idx[(top.row, top.col)]
                try:
                    img_a = self._load_and_crop(top.filename, np.float64)
                    img_b = self._load_and_crop(tile.filename, np.float64)
                    strip_a = img_a[-overlap_y:, :]
                    strip_b = img_b[:overlap_y, :]
                    mean_a = strip_a.mean()
                    mean_b = strip_b.mean()
                    if mean_a > 1 and mean_b > 1:
                        ratios.append((idx_a, idx_b, mean_a / mean_b))
                except Exception:
                    pass

        if not ratios:
            return {t.filename: 1.0 for t in self.tiles}

        # Solve for per-tile log-gains using least squares
        # For each ratio: log(gain_a) - log(gain_b) = log(ratio) means
        # tile_b should be brighter by ratio to match tile_a
        # We want gain_b * mean_b ≈ gain_a * mean_a
        # => log(gain_b) - log(gain_a) = log(mean_a/mean_b) = log(ratio)
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

        # Clamp gains to reasonable range
        gains = np.clip(gains, 0.5, 2.0)

        gain_range = gains.max() / gains.min()
        print(f"  Brightness gain range: {gains.min():.3f} - {gains.max():.3f} (ratio: {gain_range:.3f})")

        return {tile.filename: gains[i] for i, tile in enumerate(self.tiles)}

    # ─── Stitching ────────────────────────────────────────────────────

    def _compute_vignetting_map(self) -> Optional[np.ndarray]:
        """
        Compute a vignetting correction map by averaging all tiles.

        The die content averages out across tiles, leaving only the
        illumination non-uniformity (vignetting) pattern. Each tile
        is then divided by this map to flatten illumination before blending.

        Returns
        -------
        np.ndarray or None
            Correction multiplier map (same shape as a tile, per-channel).
            Multiply each tile by this to correct vignetting.
            Returns None if fewer than 4 tiles (not enough to average out content).
        """
        import cv2

        if len(self.tiles) < 4:
            return None

        tile_shape = self._get_tile_shape()
        tile_h, tile_w = tile_shape[0], tile_shape[1]
        is_color = len(tile_shape) == 3

        print("Computing vignetting correction map...")

        # Accumulate mean tile
        if is_color:
            accum = np.zeros((tile_h, tile_w, tile_shape[2]), dtype=np.float64)
        else:
            accum = np.zeros((tile_h, tile_w), dtype=np.float64)

        count = 0
        for tile in self.tiles:
            img = self._load_and_crop(tile.filename, np.float64)
            if img.shape[:2] == (tile_h, tile_w):
                accum += img
                count += 1

        if count == 0:
            return None

        mean_tile = accum / count

        # Heavy Gaussian blur to extract only the low-frequency illumination profile
        # Use kernel size ~1/4 of tile dimension to smooth out all content
        ksize = max(tile_w, tile_h) // 4
        ksize = ksize | 1  # ensure odd
        if is_color:
            for c in range(tile_shape[2]):
                mean_tile[:, :, c] = cv2.GaussianBlur(
                    mean_tile[:, :, c].astype(np.float32), (ksize, ksize), 0
                )
        else:
            mean_tile = cv2.GaussianBlur(
                mean_tile.astype(np.float32), (ksize, ksize), 0
            )

        # Normalize so center = 1.0 (correction is relative)
        if is_color:
            for c in range(tile_shape[2]):
                ch = mean_tile[:, :, c]
                center_val = ch[tile_h // 2, tile_w // 2]
                if center_val > 0:
                    mean_tile[:, :, c] = center_val / np.maximum(ch, 1.0)
                else:
                    mean_tile[:, :, c] = 1.0
        else:
            center_val = mean_tile[tile_h // 2, tile_w // 2]
            if center_val > 0:
                mean_tile = center_val / np.maximum(mean_tile, 1.0)
            else:
                mean_tile = np.ones_like(mean_tile)

        # Clamp correction to reasonable range (0.5x to 2.0x)
        mean_tile = np.clip(mean_tile, 0.5, 2.0).astype(np.float32)

        print(f"  Vignetting correction range: {mean_tile.min():.3f} - {mean_tile.max():.3f}")
        return mean_tile

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

        # Run alignment
        if align and self._overlap_pct > 0:
            self.align(max_shift_px=max_shift_px)

        # Compute vignetting correction
        vignette_corr = None
        if correct_vignetting:
            vignette_corr = self._compute_vignetting_map()

        # Compute brightness matching gains
        brightness_gains = None
        if match_brightness:
            brightness_gains = self._compute_brightness_gains()

        # Calculate canvas size from refined positions
        positions = self._compute_pixel_positions()
        max_x = max(p[0] + tile_w for p in positions.values())
        max_y = max(p[1] + tile_h for p in positions.values())
        canvas_w = int(np.ceil(max_x))
        canvas_h = int(np.ceil(max_y))

        dtype_size = np.dtype(self._tile_dtype).itemsize
        canvas_mb = (canvas_w * canvas_h * n_channels * dtype_size) / (1024 * 1024)
        print(f"\nStitching {len(self.tiles)} tiles into {canvas_w} x {canvas_h} canvas ({canvas_mb:.0f} MB, {self._tile_dtype})...")

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
