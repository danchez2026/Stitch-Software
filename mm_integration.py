"""
Micro-Manager Integration Layer
=================================
Bridges pymmcore (Micro-Manager Core) with the MAC 2000 driver and die mapper.

Two operating modes:
  1. FULL MM MODE - Micro-Manager controls both stage AND camera
     (Uses Ludl device adapter built into MM)
  2. HYBRID MODE  - Our MAC 2000 driver controls stage, MM controls camera only
     (Useful if you want finer stage control or MM's Ludl adapter has issues)

Usage (Full MM mode):
    from mm_integration import MicroManagerSystem
    system = MicroManagerSystem()
    system.setup_ludl(com_port="COM3")
    system.setup_camera()  # Auto-detects or uses demo
    system.initialize()
    system.snap_and_save("test.tif")

Usage (Hybrid mode):
    from mm_integration import MicroManagerSystem
    from mac2000_driver import MAC2000
    stage = MAC2000("COM3")
    system = MicroManagerSystem(external_stage=stage)
    system.setup_camera()
    system.initialize()
    # Use stage directly, use system for camera

Usage (with DieMapper):
    from mm_integration import MicroManagerSystem
    from die_mapper import DieMapper
    system = MicroManagerSystem()
    system.setup_ludl("COM3")
    system.setup_camera()
    system.initialize()
    mapper = DieMapper(
        stage=system,             # MicroManagerSystem acts as a stage
        capture_func=system.snap_and_save,
        ...
    )

Usage (Teli camera direct + MAC2000 driver, no Micro-Manager):
    from mac2000_driver import MAC2000
    from teli_camera import TeliCamera
    from die_mapper import DieMapper

    stage = MAC2000("COM3")
    stage.connect()
    cam = TeliCamera()
    cam.connect()

    mapper = DieMapper(
        stage=stage,
        die_width_um=5000, die_height_um=5000,
        fov_width_um=..., fov_height_um=...,
        capture_func=cam.capture_and_save,
        steps_per_um=20,
    )
    mapper.run(output_dir="./scan_output")
    mapper.run()
"""

import os
import time
import logging
from pathlib import Path
from typing import Optional, Tuple, List

import numpy as np
import pymmcore

from mac2000_driver import MAC2000, StagePosition

logger = logging.getLogger(__name__)

# Path to Micro-Manager device adapter DLLs
MM_INSTALL_DIR = r"C:\Program Files\Micro-Manager-2.0"


class MicroManagerSystem:
    """
    Unified system controller for stage + camera via Micro-Manager.

    Can operate in Full MM mode (MM controls everything) or Hybrid mode
    (external MAC2000 driver for stage, MM for camera only).

    This class implements the same position/movement interface as MAC2000,
    so it can be passed directly to DieMapper as the 'stage' parameter.
    """

    def __init__(
        self,
        mm_dir: str = MM_INSTALL_DIR,
        external_stage: Optional[MAC2000] = None,
        simulate: bool = False,
    ):
        """
        Parameters
        ----------
        mm_dir : str
            Path to Micro-Manager installation directory
        external_stage : MAC2000, optional
            If provided, use this driver for stage control instead of MM's Ludl adapter.
        simulate : bool
            If True, use DemoCamera and skip real hardware
        """
        self.mm_dir = mm_dir
        self.external_stage = external_stage
        self.simulate = simulate

        self._mmc = pymmcore.CMMCore()
        self._mmc.setDeviceAdapterSearchPaths([mm_dir])

        self._initialized = False
        self._has_camera = False
        self._has_stage = False
        self._camera_label = ""
        self._xy_stage_label = ""
        self._z_stage_label = ""

        # Calibration
        self.steps_per_um = 0.0
        self._pixel_size_um = 0.0

    @property
    def core(self) -> pymmcore.CMMCore:
        """Access the raw MMCore object for advanced operations."""
        return self._mmc

    # ─── Device Setup ─────────────────────────────────────────────────

    def setup_ludl(
        self,
        com_port: str = "COM3",
        baudrate: int = 9600,
        controller_label: str = "LudlController",
        xy_label: str = "LudlXY",
        z_label: str = "LudlZ",
    ):
        """
        Configure the Ludl MAC 2000 stage via Micro-Manager's device adapter.
        Call this BEFORE initialize().

        Parameters
        ----------
        com_port : str
            Serial port for the MAC 2000
        baudrate : int
            Baud rate (default 9600)
        controller_label : str
            Label for the controller device in MM config
        xy_label : str
            Label for the XY stage device
        z_label : str
            Label for the Z (focus) stage device
        """
        mmc = self._mmc

        # Create serial port
        mmc.loadDevice("LudlPort", "SerialManager", com_port)
        mmc.setProperty("LudlPort", "BaudRate", str(baudrate))
        mmc.setProperty("LudlPort", "StopBits", "2")
        mmc.setProperty("LudlPort", "Parity", "None")
        mmc.setProperty("LudlPort", "DataBits", "8")
        mmc.setProperty("LudlPort", "Handshaking", "Off")
        mmc.setProperty("LudlPort", "DelayBetweenCharsMs", "10")
        mmc.setProperty("LudlPort", "AnswerTimeout", "2000.0")
        mmc.initializeDevice("LudlPort")

        # Load Ludl controller
        mmc.loadDevice(controller_label, "Ludl", "LudlController")
        mmc.setProperty(controller_label, "Port", "LudlPort")
        mmc.initializeDevice(controller_label)

        # Load XY stage
        mmc.loadDevice(xy_label, "Ludl", "XYStage")
        mmc.setProperty(xy_label, "Port", "LudlPort")
        mmc.initializeDevice(xy_label)
        mmc.setProperty("Core", "XYStage", xy_label)
        self._xy_stage_label = xy_label

        # Load Z (focus) stage
        try:
            mmc.loadDevice(z_label, "Ludl", "Stage")
            mmc.setProperty(z_label, "Port", "LudlPort")
            mmc.initializeDevice(z_label)
            mmc.setProperty("Core", "Focus", z_label)
            self._z_stage_label = z_label
            logger.info(f"Z stage loaded: {z_label}")
        except Exception as e:
            logger.warning(f"Z stage not available: {e}")

        self._has_stage = True
        logger.info(f"Ludl stage configured on {com_port}")

    def setup_camera(
        self,
        camera_label: str = "",
        adapter_name: str = "",
        device_name: str = "",
        exposure_ms: float = 50.0,
    ):
        """
        Configure a camera. If no adapter specified, uses DemoCamera.
        Call this BEFORE initialize().

        Parameters
        ----------
        camera_label : str
            Label for the camera device (auto-generated if empty)
        adapter_name : str
            MM device adapter library name (e.g., "AmScope", "ToupCam", "DemoCamera")
            If empty and simulate=True, uses DemoCamera
        device_name : str
            Device name within the adapter (e.g., "DCam")
        exposure_ms : float
            Default exposure time in milliseconds
        """
        mmc = self._mmc

        if not adapter_name:
            if self.simulate:
                adapter_name = "DemoCamera"
                device_name = "DCam"
                camera_label = camera_label or "DemoCamera"
                # Demo camera needs a hub
                mmc.loadDevice("DHub", "DemoCamera", "DHub")
                mmc.initializeDevice("DHub")
            else:
                # List available camera adapters for the user
                print("No camera adapter specified. Available adapters:")
                self._list_camera_adapters()
                return

        if not camera_label:
            camera_label = adapter_name

        mmc.loadDevice(camera_label, adapter_name, device_name)
        mmc.initializeDevice(camera_label)
        mmc.setProperty("Core", "Camera", camera_label)
        mmc.setExposure(exposure_ms)

        self._camera_label = camera_label
        self._has_camera = True
        logger.info(f"Camera configured: {camera_label} ({adapter_name}/{device_name})")

    def setup_camera_auto(self, exposure_ms: float = 50.0):
        """
        Try to auto-detect a connected camera from common adapters.
        Falls back to DemoCamera if nothing found.
        """
        common_cameras = [
            ("AmScope", "AmScope"),
            ("ToupCam", "ToupCam"),
            ("OpenCVgrabber", "OpenCVgrabber"),
            ("IIDC", "IIDC"),
        ]

        for adapter, device in common_cameras:
            try:
                self._mmc.loadDevice("TestCam", adapter, device)
                self._mmc.initializeDevice("TestCam")
                self._mmc.setProperty("Core", "Camera", "TestCam")
                self._mmc.setExposure(exposure_ms)
                self._camera_label = "TestCam"
                self._has_camera = True
                logger.info(f"Auto-detected camera: {adapter}/{device}")
                print(f"Camera detected: {adapter}/{device}")
                return
            except Exception:
                try:
                    self._mmc.unloadDevice("TestCam")
                except Exception:
                    pass

        logger.warning("No camera auto-detected, using DemoCamera")
        print("No camera found - using DemoCamera (simulated)")
        self.setup_camera(adapter_name="DemoCamera", device_name="DCam",
                         exposure_ms=exposure_ms)

    def _list_camera_adapters(self):
        """List likely camera adapters available in MM."""
        camera_adapters = [
            "AmScope", "AlliedVisionCamera", "Andor", "DemoCamera",
            "HamamatsuHam", "IDS_uEye", "Microchip", "OpenCVgrabber",
            "PCO_Camera", "PointGrey", "ThorlabsUSBCamera", "ToupCam",
            "Basler", "IIDC", "SpotCamera", "QCam",
        ]
        for name in camera_adapters:
            dll = os.path.join(self.mm_dir, f"mmgr_dal_{name}.dll")
            exists = "  [installed]" if os.path.exists(dll) else ""
            print(f"  {name}{exists}")
        print("\nUse: system.setup_camera(adapter_name='...', device_name='...')")

    def initialize(self):
        """
        Finalize initialization. Call after setup_ludl() and setup_camera().
        """
        if self.external_stage and not self.external_stage.connected:
            self.external_stage.connect()

        self._initialized = True

        # Get pixel size if calibrated in MM
        try:
            self._pixel_size_um = self._mmc.getPixelSizeUm()
        except Exception:
            self._pixel_size_um = 0.0

        logger.info("System initialized")
        self._print_system_info()

    def _print_system_info(self):
        """Print system configuration summary."""
        mmc = self._mmc
        print("\n" + "=" * 60)
        print("  MICRO-MANAGER SYSTEM INFO")
        print("=" * 60)
        print(f"  MMCore: {mmc.getVersionInfo()}")

        if self._has_camera:
            print(f"  Camera: {self._camera_label}")
            w = mmc.getImageWidth()
            h = mmc.getImageHeight()
            bpp = mmc.getBytesPerPixel()
            exp = mmc.getExposure()
            print(f"    Resolution: {w} x {h}")
            print(f"    Bit depth:  {bpp * 8} bit ({bpp} bytes/pixel)")
            print(f"    Exposure:   {exp} ms")
            if self._pixel_size_um > 0:
                fov_w = w * self._pixel_size_um
                fov_h = h * self._pixel_size_um
                print(f"    Pixel size: {self._pixel_size_um} um/pixel")
                print(f"    FOV:        {fov_w:.0f} x {fov_h:.0f} um")

        if self._has_stage:
            print(f"  XY Stage: {self._xy_stage_label} (via MM Ludl adapter)")
            if self._z_stage_label:
                print(f"  Z Stage:  {self._z_stage_label}")
        elif self.external_stage:
            print(f"  XY Stage: {self.external_stage} (direct serial driver)")

        print("=" * 60 + "\n")

    def shutdown(self):
        """Unload all devices and clean up."""
        if self.external_stage and self.external_stage.connected:
            self.external_stage.disconnect()
        try:
            self._mmc.unloadAllDevices()
        except Exception:
            pass
        self._initialized = False
        logger.info("System shut down")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.shutdown()

    # ─── Camera Operations ────────────────────────────────────────────

    def set_exposure(self, ms: float):
        """Set camera exposure time in milliseconds."""
        self._mmc.setExposure(ms)

    def get_exposure(self) -> float:
        """Get current exposure time in milliseconds."""
        return self._mmc.getExposure()

    def snap_image(self) -> np.ndarray:
        """
        Capture a single image and return as numpy array.

        Returns
        -------
        np.ndarray
            Image data (2D for grayscale, 3D for color)
        """
        self._mmc.snapImage()
        img = self._mmc.getImage()

        # Reshape based on image dimensions
        w = self._mmc.getImageWidth()
        h = self._mmc.getImageHeight()
        n_components = self._mmc.getNumberOfComponents()

        if n_components > 1:
            img = img.reshape(h, w, n_components)
        else:
            img = img.reshape(h, w)

        return img

    def snap_and_save(self, filepath: str, exposure_ms: Optional[float] = None) -> bool:
        """
        Capture an image and save to file.
        This is the capture_func for DieMapper.

        Parameters
        ----------
        filepath : str
            Output file path (.tif, .png, .jpg)
        exposure_ms : float, optional
            Override exposure for this capture

        Returns
        -------
        bool
            True if capture and save succeeded
        """
        try:
            if exposure_ms is not None:
                self._mmc.setExposure(exposure_ms)

            img = self.snap_image()

            # Save using tifffile if available, otherwise fall back to PIL or raw
            ext = Path(filepath).suffix.lower()
            self._save_image(img, filepath, ext)
            return True

        except Exception as e:
            logger.error(f"Capture failed: {e}")
            return False

    def _save_image(self, img: np.ndarray, filepath: str, ext: str):
        """Save numpy image array to file."""
        # Try tifffile first (best for microscopy TIFF)
        if ext in (".tif", ".tiff"):
            try:
                import tifffile
                tifffile.imwrite(filepath, img)
                return
            except ImportError:
                pass

        # Try PIL/Pillow
        try:
            from PIL import Image
            pil_img = Image.fromarray(img)
            pil_img.save(filepath)
            return
        except ImportError:
            pass

        # Fall back to raw numpy save
        np.save(filepath.replace(ext, ".npy"), img)
        logger.warning(f"Saved as .npy (install tifffile or Pillow for {ext})")

    def get_image_size(self) -> Tuple[int, int]:
        """Get camera image dimensions (width, height) in pixels."""
        return (self._mmc.getImageWidth(), self._mmc.getImageHeight())

    def get_pixel_size_um(self) -> float:
        """Get calibrated pixel size in microns."""
        return self._pixel_size_um

    def get_fov_um(self) -> Tuple[float, float]:
        """
        Get field of view in microns (requires pixel size calibration).
        Returns (width_um, height_um).
        """
        if self._pixel_size_um <= 0:
            raise ValueError(
                "Pixel size not calibrated. Set it in MM config or with "
                "set_pixel_size_um()"
            )
        w, h = self.get_image_size()
        return (w * self._pixel_size_um, h * self._pixel_size_um)

    def set_pixel_size_um(self, pixel_size: float):
        """Manually set pixel size calibration (um per pixel)."""
        self._pixel_size_um = pixel_size

    # ─── Stage Interface (compatible with MAC2000 / DieMapper) ────────

    def get_position(self) -> StagePosition:
        """Get current XY position (delegates to MM or external driver)."""
        if self.external_stage:
            return self.external_stage.get_position()
        x = self._mmc.getXPosition(self._xy_stage_label)
        y = self._mmc.getYPosition(self._xy_stage_label)
        return StagePosition(x=int(x), y=int(y))

    def move_absolute(self, x: int, y: int, wait: bool = False):
        """Move stage to absolute position."""
        if self.external_stage:
            self.external_stage.move_absolute(x, y, wait=wait)
            return
        self._mmc.setXYPosition(self._xy_stage_label, float(x), float(y))
        if wait:
            self._mmc.waitForDevice(self._xy_stage_label)

    def move_relative(self, dx: int, dy: int, wait: bool = False):
        """Move stage relative to current position."""
        if self.external_stage:
            self.external_stage.move_relative(dx, dy, wait=wait)
            return
        self._mmc.setRelativeXYPosition(self._xy_stage_label, float(dx), float(dy))
        if wait:
            self._mmc.waitForDevice(self._xy_stage_label)

    def set_origin(self, x: int = 0, y: int = 0):
        """Set current position as origin."""
        if self.external_stage:
            self.external_stage.set_origin(x, y)
            return
        self._mmc.setOriginXY(self._xy_stage_label)

    def is_busy(self, axis: str = "") -> bool:
        """Check if stage is moving."""
        if self.external_stage:
            return self.external_stage.is_busy(axis)
        return self._mmc.deviceBusy(self._xy_stage_label)

    def wait_until_idle(self, timeout: float = 60.0, poll_interval: float = None):
        """Wait for stage to stop moving."""
        if self.external_stage:
            self.external_stage.wait_until_idle(timeout, poll_interval)
            return
        self._mmc.waitForDevice(self._xy_stage_label)

    def halt(self):
        """Emergency stop."""
        if self.external_stage:
            self.external_stage.halt()
            return
        self._mmc.stop(self._xy_stage_label)

    def home(self, axes: str = "X Y", wait: bool = True, timeout: float = 120.0):
        """Home the stage."""
        if self.external_stage:
            self.external_stage.home(axes, wait, timeout)
            return
        self._mmc.home(self._xy_stage_label)
        if wait:
            self._mmc.waitForDevice(self._xy_stage_label)

    # ─── Convenience: Create DieMapper ────────────────────────────────

    def create_mapper(
        self,
        die_width_um: float,
        die_height_um: float,
        overlap_pct: float = 15.0,
        steps_per_um: float = 20.0,
        settle_time: float = 0.3,
        pattern: str = "serpentine",
    ):
        """
        Create a DieMapper configured with this system's camera FOV.

        Requires pixel size calibration (set_pixel_size_um or MM config).

        Parameters
        ----------
        die_width_um, die_height_um : float
            Area to scan in microns
        overlap_pct : float
            Overlap between tiles (default 15%)
        steps_per_um : float
            Stage calibration (motor steps per micron)

        Returns
        -------
        DieMapper
            Configured die mapper ready to run
        """
        from die_mapper import DieMapper

        fov_w, fov_h = self.get_fov_um()
        logger.info(f"Using FOV: {fov_w:.0f} x {fov_h:.0f} um")

        return DieMapper(
            stage=self,
            die_width_um=die_width_um,
            die_height_um=die_height_um,
            fov_width_um=fov_w,
            fov_height_um=fov_h,
            overlap_pct=overlap_pct,
            steps_per_um=steps_per_um,
            capture_func=self.snap_and_save,
            settle_time=settle_time,
            pattern=pattern,
        )


# ─── Config File Generator ───────────────────────────────────────────

def generate_ludl_config(
    com_port: str = "COM3",
    output_path: str = None,
    camera_adapter: str = "",
    camera_device: str = "",
) -> str:
    """
    Generate a Micro-Manager .cfg file for the Ludl MAC 2000 setup.

    Parameters
    ----------
    com_port : str
        Serial port for the MAC 2000
    output_path : str
        Where to save the config file (default: MAC2000/MMConfig_Ludl.cfg)
    camera_adapter : str
        Camera adapter name (leave empty to skip camera config)
    camera_device : str
        Camera device name within the adapter

    Returns
    -------
    str
        Path to the generated config file
    """
    if output_path is None:
        output_path = os.path.join(
            os.path.dirname(__file__), "MMConfig_Ludl.cfg"
        )

    lines = [
        "# Micro-Manager Configuration for Ludl MAC 2000",
        f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "# SemiAnalysis - Die Mapping System",
        "",
        "# Reset",
        "Property,Core,Initialize,0",
        "",
        "# Serial Port",
        f"Device,LudlPort,SerialManager,{com_port}",
        f"Property,LudlPort,BaudRate,9600",
        "Property,LudlPort,StopBits,2",
        "Property,LudlPort,Parity,None",
        "Property,LudlPort,DataBits,8",
        "Property,LudlPort,Handshaking,Off",
        "Property,LudlPort,DelayBetweenCharsMs,10",
        "Property,LudlPort,AnswerTimeout,2000.0",
        "",
        "# Ludl Controller",
        "Device,LudlController,Ludl,LudlController",
        "Property,LudlController,Port,LudlPort",
        "",
        "# XY Stage",
        "Device,LudlXY,Ludl,XYStage",
        "Property,LudlXY,Port,LudlPort",
        "",
        "# Z (Focus) Stage",
        "Device,LudlZ,Ludl,Stage",
        "Property,LudlZ,Port,LudlPort",
        "",
    ]

    if camera_adapter and camera_device:
        lines.extend([
            "# Camera",
            f"Device,Camera,{camera_adapter},{camera_device}",
            "",
        ])

    lines.extend([
        "# Initialize all devices",
        "Property,Core,Initialize,1",
        "",
        "# Roles",
        "Property,Core,XYStage,LudlXY",
        "Property,Core,Focus,LudlZ",
    ])

    if camera_adapter:
        lines.append("Property,Core,Camera,Camera")

    lines.extend([
        "",
        "# Focus direction",
        "FocusDirection,LudlZ,0",
        "",
    ])

    with open(output_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Config saved: {output_path}")
    print(f"Load in Micro-Manager: Tools > Load Hardware Configuration...")
    return output_path


def list_installed_adapters():
    """List all device adapters installed in Micro-Manager."""
    print(f"\nDevice adapters in {MM_INSTALL_DIR}:")
    print("-" * 50)

    dll_dir = Path(MM_INSTALL_DIR)
    adapters = sorted(dll_dir.glob("mmgr_dal_*.dll"))

    for dll in adapters:
        name = dll.stem.replace("mmgr_dal_", "")
        print(f"  {name}")

    print(f"\nTotal: {len(adapters)} adapters")


# ─── CLI ──────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Micro-Manager Integration Utilities")
    sub = parser.add_subparsers(dest="command")

    # Generate config
    gen = sub.add_parser("genconfig", help="Generate Ludl MM config file")
    gen.add_argument("--port", default="COM3", help="COM port")
    gen.add_argument("--output", default=None, help="Output .cfg path")
    gen.add_argument("--camera-adapter", default="", help="Camera adapter name")
    gen.add_argument("--camera-device", default="", help="Camera device name")

    # List adapters
    sub.add_parser("list-adapters", help="List installed device adapters")

    # Test system
    test = sub.add_parser("test", help="Test system with DemoCamera")
    test.add_argument("--output", default="./test_capture", help="Output dir")

    # List cameras
    sub.add_parser("detect-camera", help="Try to detect connected cameras")

    args = parser.parse_args()

    if args.command == "genconfig":
        generate_ludl_config(
            com_port=args.port,
            output_path=args.output,
            camera_adapter=args.camera_adapter,
            camera_device=args.camera_device,
        )

    elif args.command == "list-adapters":
        list_installed_adapters()

    elif args.command == "test":
        print("Testing with DemoCamera (simulated)...")
        system = MicroManagerSystem(simulate=True)
        system.setup_camera()
        system.initialize()
        system.set_pixel_size_um(0.5)  # 0.5 um/pixel for demo

        out_dir = Path(args.output)
        out_dir.mkdir(exist_ok=True)

        filepath = str(out_dir / "test_snap.tif")
        print(f"Capturing test image to {filepath}...")
        success = system.snap_and_save(filepath)
        print(f"Capture {'succeeded' if success else 'FAILED'}")

        img = system.snap_image()
        print(f"Image shape: {img.shape}, dtype: {img.dtype}")
        print(f"Image size: {system.get_image_size()}")
        print(f"FOV: {system.get_fov_um()} um")

        system.shutdown()
        print("Test complete!")

    elif args.command == "detect-camera":
        system = MicroManagerSystem()
        system.setup_camera_auto()
        if system._has_camera:
            img = system.snap_image()
            print(f"Image: {img.shape}, dtype: {img.dtype}")
        system.shutdown()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
