"""
Automated Die Mapper & Image Stitcher
=======================================
Drives the MAC 2000 stage in a grid pattern to capture overlapping images
of a semiconductor die, then stitches them into a single high-resolution map.

Workflow:
    1. User defines: die size, image FOV, overlap %, objective
    2. Program calculates grid positions (serpentine/raster pattern)
    3. Stage moves to each position, captures image
    4. Images are saved with position metadata
    5. Optional: auto-stitch into final composite

Usage:
    from die_mapper import DieMapper
    from mac2000_driver import MAC2000

    stage = MAC2000("COM3")
    stage.connect()

    mapper = DieMapper(
        stage=stage,
        die_width_um=5000,     # Die width in microns
        die_height_um=5000,    # Die height in microns
        fov_width_um=500,      # Camera field of view width
        fov_height_um=400,     # Camera field of view height
        overlap_pct=15,        # 15% overlap between tiles
        steps_per_um=20,       # Stage calibration
    )

    mapper.preview()           # Show grid plan
    mapper.run(output_dir="./scan_output")  # Execute scan
"""

import os
import json
import time
import logging
from dataclasses import dataclass, field, asdict
from typing import List, Tuple, Optional, Callable
from pathlib import Path

from mac2000_driver import MAC2000, StagePosition

logger = logging.getLogger(__name__)


@dataclass
class TileInfo:
    """Metadata for a single captured tile."""
    row: int
    col: int
    index: int
    stage_x: int           # Motor steps
    stage_y: int           # Motor steps
    x_um: float            # Microns
    y_um: float            # Microns
    filename: str = ""
    captured: bool = False
    capture_time: float = 0.0


@dataclass
class ScanConfig:
    """Configuration for a die scan."""
    die_width_um: float
    die_height_um: float
    fov_width_um: float
    fov_height_um: float
    overlap_pct: float       # 0-50
    steps_per_um: float
    settle_time: float = 0.3  # Seconds to wait after each move
    pattern: str = "serpentine"  # "serpentine" or "raster"
    origin_x_steps: int = 0  # Starting position X
    origin_y_steps: int = 0  # Starting position Y

    @property
    def step_x_um(self) -> float:
        """Effective step size in X (FOV minus overlap)."""
        return self.fov_width_um * (1 - self.overlap_pct / 100)

    @property
    def step_y_um(self) -> float:
        """Effective step size in Y (FOV minus overlap)."""
        return self.fov_height_um * (1 - self.overlap_pct / 100)

    @property
    def cols(self) -> int:
        """Number of columns in the grid."""
        import math
        return max(1, math.ceil(self.die_width_um / self.step_x_um))

    @property
    def rows(self) -> int:
        """Number of rows in the grid."""
        import math
        return max(1, math.ceil(self.die_height_um / self.step_y_um))

    @property
    def total_tiles(self) -> int:
        return self.rows * self.cols

    @property
    def step_x_steps(self) -> int:
        """Step size in motor steps (X)."""
        return int(round(self.step_x_um * self.steps_per_um))

    @property
    def step_y_steps(self) -> int:
        """Step size in motor steps (Y)."""
        return int(round(self.step_y_um * self.steps_per_um))


class DieMapper:
    """
    Automated die mapping system.

    Parameters
    ----------
    stage : MAC2000
        Connected stage controller
    die_width_um, die_height_um : float
        Total area to scan in microns
    fov_width_um, fov_height_um : float
        Camera field of view in microns
    overlap_pct : float
        Overlap between adjacent tiles (10-20% typical)
    steps_per_um : float
        Stage calibration factor
    capture_func : callable, optional
        Function to call for image capture: capture_func(filepath) -> bool
        If None, a placeholder is used (no actual capture)
    settle_time : float
        Wait time after stage move before capture (seconds)
    pattern : str
        "serpentine" (back-and-forth) or "raster" (always left-to-right)
    """

    def __init__(
        self,
        stage: MAC2000,
        die_width_um: float,
        die_height_um: float,
        fov_width_um: float,
        fov_height_um: float,
        overlap_pct: float = 15.0,
        steps_per_um: float = 20.0,
        capture_func: Optional[Callable] = None,
        settle_time: float = 0.3,
        pattern: str = "serpentine",
    ):
        self.stage = stage
        self.capture_func = capture_func

        self.config = ScanConfig(
            die_width_um=die_width_um,
            die_height_um=die_height_um,
            fov_width_um=fov_width_um,
            fov_height_um=fov_height_um,
            overlap_pct=overlap_pct,
            steps_per_um=steps_per_um,
            settle_time=settle_time,
            pattern=pattern,
        )

        self._tiles: List[TileInfo] = []
        self._build_tile_grid()

    def _build_tile_grid(self):
        """Calculate all tile positions."""
        cfg = self.config
        self._tiles = []
        index = 0

        for row in range(cfg.rows):
            # Determine column order based on scan pattern
            if cfg.pattern == "serpentine" and row % 2 == 1:
                col_range = range(cfg.cols - 1, -1, -1)  # Reverse on odd rows
            else:
                col_range = range(cfg.cols)

            for col in col_range:
                x_um = col * cfg.step_x_um
                y_um = row * cfg.step_y_um
                x_steps = cfg.origin_x_steps + int(round(x_um * cfg.steps_per_um))
                y_steps = cfg.origin_y_steps + int(round(y_um * cfg.steps_per_um))

                tile = TileInfo(
                    row=row,
                    col=col,
                    index=index,
                    stage_x=x_steps,
                    stage_y=y_steps,
                    x_um=x_um,
                    y_um=y_um,
                )
                self._tiles.append(tile)
                index += 1

    def set_origin_here(self):
        """Set the current stage position as the scan origin (top-left corner)."""
        pos = self.stage.get_position()
        self.config.origin_x_steps = pos.x
        self.config.origin_y_steps = pos.y
        self._build_tile_grid()
        logger.info(f"Origin set to stage position ({pos.x}, {pos.y})")

    @property
    def tiles(self) -> List[TileInfo]:
        return self._tiles

    # ─── Preview & Info ───────────────────────────────────────────────

    def preview(self):
        """Print scan plan summary."""
        cfg = self.config
        print("=" * 60)
        print("  DIE MAPPING SCAN PLAN")
        print("=" * 60)
        print(f"  Die size:      {cfg.die_width_um} x {cfg.die_height_um} um")
        print(f"  FOV:           {cfg.fov_width_um} x {cfg.fov_height_um} um")
        print(f"  Overlap:       {cfg.overlap_pct}%")
        print(f"  Effective step: {cfg.step_x_um:.1f} x {cfg.step_y_um:.1f} um")
        print(f"  Grid:          {cfg.cols} cols x {cfg.rows} rows")
        print(f"  Total tiles:   {cfg.total_tiles}")
        print(f"  Pattern:       {cfg.pattern}")
        print(f"  Settle time:   {cfg.settle_time}s")
        print(f"  Calibration:   {cfg.steps_per_um} steps/um")
        print(f"  Step (motor):  {cfg.step_x_steps} x {cfg.step_y_steps} steps")
        print(f"  Origin:        ({cfg.origin_x_steps}, {cfg.origin_y_steps}) steps")

        # Estimated time
        move_time_per_tile = 1.0  # Rough estimate: 1 sec per move
        total_time = cfg.total_tiles * (move_time_per_tile + cfg.settle_time)
        mins = total_time / 60
        print(f"  Est. time:     ~{mins:.1f} minutes")
        print("=" * 60)

        # Show grid map
        self._print_grid_map()

    def _print_grid_map(self):
        """Print ASCII grid showing scan order."""
        cfg = self.config
        if cfg.cols > 20 or cfg.rows > 20:
            print(f"  (Grid too large to display: {cfg.cols}x{cfg.rows})")
            return

        print(f"\n  Scan order ({cfg.pattern}):")

        # Build grid with tile indices
        grid = {}
        for tile in self._tiles:
            grid[(tile.row, tile.col)] = tile.index

        for row in range(cfg.rows):
            line = "  "
            for col in range(cfg.cols):
                idx = grid.get((row, col), -1)
                if idx >= 0:
                    line += f"[{idx:3d}]"
                else:
                    line += "[   ]"
            print(line)
        print()

    # ─── Scan Execution ───────────────────────────────────────────────

    def run(
        self,
        output_dir: str = "./scan_output",
        image_prefix: str = "tile",
        image_ext: str = ".tif",
        dry_run: bool = False,
    ) -> str:
        """
        Execute the full die mapping scan.

        Parameters
        ----------
        output_dir : str
            Directory to save captured images
        image_prefix : str
            Filename prefix for tiles
        image_ext : str
            Image file extension (.tif, .png, .jpg)
        dry_run : bool
            If True, move stage but don't capture images

        Returns
        -------
        str
            Path to the scan metadata JSON file
        """
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        self.preview()

        if dry_run:
            print("\n  *** DRY RUN - no images will be captured ***\n")

        total = self.config.total_tiles
        start_time = time.time()

        print(f"\nStarting scan: {total} tiles")
        print("-" * 40)

        for tile in self._tiles:
            tile_start = time.time()

            # Generate filename
            filename = f"{image_prefix}_r{tile.row:03d}_c{tile.col:03d}{image_ext}"
            filepath = str(output_path / filename)
            tile.filename = filename

            # Move stage
            print(f"  [{tile.index + 1}/{total}] "
                  f"Row {tile.row}, Col {tile.col} -> "
                  f"({tile.stage_x}, {tile.stage_y}) ... ", end="", flush=True)

            self.stage.move_absolute(tile.stage_x, tile.stage_y, wait=True)
            time.sleep(self.config.settle_time)

            # Capture image
            if not dry_run:
                if self.capture_func:
                    success = self.capture_func(filepath)
                    tile.captured = success
                    if success:
                        print(f"captured -> {filename}")
                    else:
                        print(f"CAPTURE FAILED")
                else:
                    # No capture function - create placeholder
                    tile.captured = False
                    print(f"(no camera) {filename}")
            else:
                tile.captured = False
                print("(dry run)")

            tile.capture_time = time.time() - tile_start

        elapsed = time.time() - start_time
        captured_count = sum(1 for t in self._tiles if t.captured)

        print("-" * 40)
        print(f"Scan complete: {captured_count}/{total} tiles captured")
        print(f"Total time: {elapsed:.1f}s ({elapsed/60:.1f} min)")

        # Save metadata
        metadata_path = self._save_metadata(output_path)
        print(f"Metadata saved: {metadata_path}")

        return str(metadata_path)

    def _save_metadata(self, output_path: Path) -> Path:
        """Save scan metadata as JSON for use by the stitcher."""
        metadata = {
            "scan_config": {
                "die_width_um": self.config.die_width_um,
                "die_height_um": self.config.die_height_um,
                "fov_width_um": self.config.fov_width_um,
                "fov_height_um": self.config.fov_height_um,
                "overlap_pct": self.config.overlap_pct,
                "step_x_um": self.config.step_x_um,
                "step_y_um": self.config.step_y_um,
                "cols": self.config.cols,
                "rows": self.config.rows,
                "steps_per_um": self.config.steps_per_um,
                "pattern": self.config.pattern,
                "origin_x_steps": self.config.origin_x_steps,
                "origin_y_steps": self.config.origin_y_steps,
            },
            "tiles": [
                {
                    "row": t.row,
                    "col": t.col,
                    "index": t.index,
                    "stage_x": t.stage_x,
                    "stage_y": t.stage_y,
                    "x_um": t.x_um,
                    "y_um": t.y_um,
                    "filename": t.filename,
                    "captured": t.captured,
                    "capture_time": round(t.capture_time, 3),
                }
                for t in self._tiles
            ],
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        metadata_path = output_path / "scan_metadata.json"
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)

        # Also save an ImageJ/Fiji-compatible grid descriptor
        self._save_fiji_grid(output_path)

        return metadata_path

    def _save_fiji_grid(self, output_path: Path):
        """
        Save a TileConfiguration.txt file compatible with
        Fiji's Grid/Collection Stitching plugin.

        Format:
            dim = 2
            tile_0.tif;;(0.0, 0.0)
            tile_1.tif;;(425.0, 0.0)
            ...
        """
        cfg = self.config
        lines = [f"dim = 2"]

        for tile in self._tiles:
            if tile.filename:
                # Pixel positions based on FOV dimensions
                # The stitcher uses these as initial estimates
                px_x = tile.col * cfg.step_x_um  # Using um as "pixels" -
                px_y = tile.row * cfg.step_y_um   # stitcher will refine
                lines.append(f"{tile.filename};;({px_x:.1f}, {px_y:.1f})")

        grid_path = output_path / "TileConfiguration.txt"
        with open(grid_path, "w") as f:
            f.write("\n".join(lines) + "\n")

        logger.info(f"Fiji grid config saved: {grid_path}")

    # ─── Stitching ────────────────────────────────────────────────────

    def generate_fiji_macro(self, output_dir: str, output_name: str = "stitched.tif") -> str:
        """
        Generate a Fiji/ImageJ macro for stitching the captured tiles.
        Uses the Grid/Collection Stitching plugin.

        Parameters
        ----------
        output_dir : str
            Directory containing the captured tiles
        output_name : str
            Output filename for the stitched image

        Returns
        -------
        str
            The macro text (also saved to output_dir/stitch_macro.ijm)
        """
        cfg = self.config
        output_path = Path(output_dir)

        # Fiji Grid/Collection Stitching plugin macro
        # Uses "Positions from file" mode with our TileConfiguration.txt
        macro = f"""// Auto-generated Fiji Stitching Macro
// Die Mapper - {time.strftime("%Y-%m-%d %H:%M:%S")}
// Grid: {cfg.cols}x{cfg.rows} tiles, {cfg.overlap_pct}% overlap

dir = "{output_path.as_posix()}/";

run("Grid/Collection stitching",
    "type=[Positions from file] " +
    "order=[Defined by TileConfiguration] " +
    "directory=" + dir + " " +
    "layout_file=TileConfiguration.txt " +
    "fusion_method=[Linear Blending] " +
    "regression_threshold=0.30 " +
    "max/avg_displacement_threshold=2.50 " +
    "absolute_displacement_threshold=3.50 " +
    "compute_overlap " +
    "computation_parameters=[Save computation time (but use more RAM)] " +
    "image_output=[Fuse and display]");

// Save result
selectWindow("Fused");
saveAs("Tiff", dir + "{output_name}");
print("Stitching complete: " + dir + "{output_name}");
"""

        macro_path = output_path / "stitch_macro.ijm"
        with open(macro_path, "w") as f:
            f.write(macro)

        print(f"Fiji stitching macro saved: {macro_path}")
        print("To stitch: open Fiji, Plugins > Macros > Run..., select stitch_macro.ijm")

        return macro


# ─── Helper: Quick Scan Function ──────────────────────────────────────

def quick_scan(
    port: str = "COM3",
    die_width_um: float = 5000,
    die_height_um: float = 5000,
    fov_width_um: float = 500,
    fov_height_um: float = 400,
    overlap_pct: float = 15,
    steps_per_um: float = 20,
    output_dir: str = "./scan_output",
    simulate: bool = False,
    dry_run: bool = False,
    capture_func: Optional[Callable] = None,
):
    """
    One-call convenience function to run a die scan.

    Example:
        quick_scan(simulate=True, dry_run=True)  # Preview mode
        quick_scan(port="COM3", die_width_um=3000, die_height_um=3000)
    """
    stage = MAC2000(port, simulate=simulate, steps_per_um=steps_per_um)
    stage.connect()

    mapper = DieMapper(
        stage=stage,
        die_width_um=die_width_um,
        die_height_um=die_height_um,
        fov_width_um=fov_width_um,
        fov_height_um=fov_height_um,
        overlap_pct=overlap_pct,
        steps_per_um=steps_per_um,
        capture_func=capture_func,
    )

    mapper.run(output_dir=output_dir, dry_run=dry_run)
    mapper.generate_fiji_macro(output_dir)

    stage.disconnect()


# ─── CLI Entry Point ──────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Automated Die Mapper")
    parser.add_argument("--port", default="COM3", help="COM port")
    parser.add_argument("--die-width", type=float, required=True, help="Die width (um)")
    parser.add_argument("--die-height", type=float, required=True, help="Die height (um)")
    parser.add_argument("--fov-width", type=float, required=True, help="FOV width (um)")
    parser.add_argument("--fov-height", type=float, required=True, help="FOV height (um)")
    parser.add_argument("--overlap", type=float, default=15, help="Overlap %% (default: 15)")
    parser.add_argument("--steps-per-um", type=float, default=20, help="Steps/um calibration")
    parser.add_argument("--output", default="./scan_output", help="Output directory")
    parser.add_argument("--simulate", action="store_true", help="Simulate (no hardware)")
    parser.add_argument("--dry-run", action="store_true", help="Move stage but don't capture")
    parser.add_argument("--preview", action="store_true", help="Preview only, don't scan")
    parser.add_argument("--settle", type=float, default=0.3, help="Settle time (seconds)")
    parser.add_argument("--pattern", choices=["serpentine", "raster"], default="serpentine")
    args = parser.parse_args()

    stage = MAC2000(args.port, simulate=args.simulate, steps_per_um=args.steps_per_um)
    stage.connect()

    mapper = DieMapper(
        stage=stage,
        die_width_um=args.die_width,
        die_height_um=args.die_height,
        fov_width_um=args.fov_width,
        fov_height_um=args.fov_height,
        overlap_pct=args.overlap,
        steps_per_um=args.steps_per_um,
        settle_time=args.settle,
        pattern=args.pattern,
    )

    if args.preview:
        mapper.preview()
    else:
        mapper.run(output_dir=args.output, dry_run=args.dry_run)
        mapper.generate_fiji_macro(args.output)

    stage.disconnect()


if __name__ == "__main__":
    main()
