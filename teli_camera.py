"""
Toshiba Teli BU505 Camera Driver
=====================================
Python interface for the Toshiba Teli BU505MCF / BU505MCG USB3 Vision cameras.

Camera specs:
  - Models: BU505MCF (Sony IMX250) and BU505MCG (Sony IMX264)
  - Both: 2/3" global shutter CMOS, 3.45 um pixels, so um/px
    calibrations carry over unchanged between the two models
  - Resolution: 2448 x 2048 (5 Megapixel)
  - Interface: USB3 Vision (NOT a serial/COM device)
  - Color: Yes

Which model(s) are accepted is governed by camera_config.json
("accepted_models" list, "preferred_serial" string) next to this file.

Requires TeliCamSDK to be installed:
  Download from: https://www.toshiba-teli.co.jp/en/products/industrial-camera/software-telicamsdk.htm

Three integration paths (tried in order):
  Path A: pytelicam (official Teli SDK Python wheel)
  Path B: Native TeliCamApi64.dll via ctypes (works even when GenTL/Harvester can't)
  Path C: Harvester + TeliCamTL64.cti (generic GenTL)

Usage:
    from teli_camera import TeliCamera

    cam = TeliCamera()
    cam.connect()
    cam.set_exposure(50000)  # 50ms in microseconds
    img = cam.capture()      # Returns numpy array
    cam.save("image.tif")    # Capture and save
    cam.disconnect()
"""

import ctypes
import json
import os
import time
import logging
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ─── Camera Identification Config ─────────────────────────────────────

CAMERA_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "camera_config.json"
)

DEFAULT_ACCEPTED_MODELS = ("BU505MCG", "BU505MCF")


def _load_camera_config() -> dict:
    """Load camera_config.json; missing/broken file falls back to defaults."""
    try:
        with open(CAMERA_CONFIG_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        if os.path.exists(CAMERA_CONFIG_PATH):
            logger.warning(f"camera_config.json unreadable ({e}); using defaults")
        return {}

# Default TeliCamSDK install paths
TELI_SDK_PATHS = [
    r"C:\Program Files\TOSHIBA TELI\TeliCamSDK",
    r"C:\Program Files (x86)\TOSHIBA TELI\TeliCamSDK",
]

TELI_DLL_DIRS = [
    r"C:\Program Files\TOSHIBA TELI\TeliCamSDK\TeliCamApi\bin\x64",
    r"C:\Program Files (x86)\TOSHIBA TELI\TeliCamSDK\TeliCamApi\bin\x86",
]

CTI_PATHS = [
    r"C:\Program Files\TOSHIBA TELI\TeliCamSDK\TeliCamApi\bin\x64\TeliCamTL64.cti",
    r"C:\Program Files (x86)\TOSHIBA TELI\TeliCamSDK\TeliCamApi\bin\x86\TeliCamTL.cti",
]

# ─── Native API Constants ─────────────────────────────────────────────

CAM_TYPE_U3V = 0x01
CAM_TYPE_ALL = 0xFFFF
CAM_ACCESS_MODE_OPEN = 0
CAM_ACCESS_MODE_CONTROL = 1
CAM_ACCESS_MODE_EXCLUSIVE = 3
CAM_ACQ_MODE_CONTINUOUS = 8
CAM_ACQ_MODE_SINGLE_FRAME = 109

# Pixel format codes
PXL_FMT_MONO8 = 0x01080001
PXL_FMT_RGB8 = 0x02180014
PXL_FMT_BGR8 = 0x02180015
PXL_FMT_BAYERGR8 = 0x01080008
PXL_FMT_BAYERRG8 = 0x01080009
PXL_FMT_BAYERGB8 = 0x0108000A
PXL_FMT_BAYERBG8 = 0x0108000B

# 12-bit Bayer formats (PFNC standard)
PXL_FMT_BAYERGR12 = 0x01100010
PXL_FMT_BAYERRG12 = 0x01100011
PXL_FMT_BAYERGB12 = 0x01100012
PXL_FMT_BAYERBG12 = 0x01100013
PXL_FMT_MONO12 = 0x01100005

# 10-bit Bayer formats
PXL_FMT_BAYERGR10 = 0x0110000C
PXL_FMT_BAYERRG10 = 0x0110000D

# API status codes
API_STATUS = {
    0x00000000: "SUCCESS",
    0x00000001: "NOT_INITIALIZED",
    0x00000002: "ALREADY_INITIALIZED",
    0x00000003: "NOT_FOUND",
    0x00000004: "ALREADY_OPENED",
    0x0000000D: "INVALID_PARAMETER",
    0x00000011: "NOT_IMPLEMENTED",
    0x00000012: "TIMEOUT",
    0x00000014: "EMPTY_COMPLETE_QUEUE",
    0x00000015: "NOT_READY",
    0x00000030: "NOT_CONNECTED_TO_USB3",
    0x00000101: "XML_LOAD_ERR",
    0x00000102: "GENICAM_ERR",
    0x00000103: "DLL_LOAD_ERR",
    0x000005AA: "NO_SYSTEM_RESOURCES",
    0x00000804: "ACCESS_DENIED",
    0x00000805: "BUSY",
    0xFFFFFFFF: "UNSUCCESSFUL",
}


def _status_str(code):
    return API_STATUS.get(code, f"0x{code:08X}")


# ─── Native API Structures ────────────────────────────────────────────

class CAM_IMAGE_INFO(ctypes.Structure):
    _fields_ = [
        ("ullTimestamp", ctypes.c_uint64),
        ("uiPixelFormat", ctypes.c_uint32),
        ("uiSizeX", ctypes.c_uint32),
        ("uiSizeY", ctypes.c_uint32),
        ("uiOffsetX", ctypes.c_uint32),
        ("uiOffsetY", ctypes.c_uint32),
        ("uiPaddingX", ctypes.c_uint32),
        ("ullBlockId", ctypes.c_uint64),
        ("pvBuf", ctypes.c_void_p),
        ("uiSize", ctypes.c_uint32),
        ("ullImageId", ctypes.c_uint64),
        ("uiStatus", ctypes.c_uint32),
    ]


# ─── Helper Functions ─────────────────────────────────────────────────

def find_teli_dll_dir() -> Optional[str]:
    """Find the Teli DLL directory."""
    for dll_dir in TELI_DLL_DIRS:
        dll_path = os.path.join(dll_dir, "TeliCamApi64.dll")
        if os.path.exists(dll_path):
            return dll_dir
    return None


def find_teli_cti() -> Optional[str]:
    """Find the Teli GenTL producer .cti file."""
    gentl_path = os.environ.get("GENICAM_GENTL64_PATH", "")
    if gentl_path:
        cti = os.path.join(gentl_path, "TeliCamTL64.cti")
        if os.path.exists(cti):
            return cti
    for cti_path in CTI_PATHS:
        if os.path.exists(cti_path):
            return cti_path
    return None


def check_sdk_installed() -> dict:
    """Check what Teli components are available."""
    result = {
        "pytelicam": False,
        "harvester": False,
        "native_dll": False,
        "cti_file": None,
        "sdk_path": None,
    }

    try:
        import pytelicam
        result["pytelicam"] = True
    except ImportError:
        pass

    try:
        import harvesters
        result["harvester"] = True
    except ImportError:
        pass

    result["native_dll"] = find_teli_dll_dir() is not None
    result["cti_file"] = find_teli_cti()

    for sdk_path in TELI_SDK_PATHS:
        if os.path.isdir(sdk_path):
            result["sdk_path"] = sdk_path
            break

    return result


# ─── Native DLL Wrapper ───────────────────────────────────────────────

class _TeliNativeAPI:
    """Thin wrapper around TeliCamApi64.dll loaded via ctypes."""

    def __init__(self, dll_dir: str):
        os.add_dll_directory(dll_dir)
        os.environ["PATH"] = dll_dir + ";" + os.environ.get("PATH", "")
        self._dll = ctypes.WinDLL(os.path.join(dll_dir, "TeliCamApi64.dll"))
        self._setup_functions()

    def _setup_functions(self):
        d = self._dll

        d.Sys_Initialize.argtypes = [ctypes.c_uint32]
        d.Sys_Initialize.restype = ctypes.c_uint32

        d.Sys_GetNumOfCameras.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
        d.Sys_GetNumOfCameras.restype = ctypes.c_uint32

        d.Sys_Terminate.argtypes = []
        d.Sys_Terminate.restype = ctypes.c_uint32

        d.Cam_Open.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_void_p, ctypes.c_uint8, ctypes.c_void_p, ctypes.c_uint32,
        ]
        d.Cam_Open.restype = ctypes.c_uint32

        d.Cam_Close.argtypes = [ctypes.c_uint64]
        d.Cam_Close.restype = ctypes.c_uint32

        d.Strm_OpenSimple.argtypes = [
            ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32,
        ]
        d.Strm_OpenSimple.restype = ctypes.c_uint32

        d.Strm_Start.argtypes = [ctypes.c_uint64, ctypes.c_uint32]
        d.Strm_Start.restype = ctypes.c_uint32

        d.Strm_Stop.argtypes = [ctypes.c_uint64]
        d.Strm_Stop.restype = ctypes.c_uint32

        d.Strm_Close.argtypes = [ctypes.c_uint64]
        d.Strm_Close.restype = ctypes.c_uint32

        d.Strm_ReadCurrentImage.argtypes = [
            ctypes.c_uint64, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(CAM_IMAGE_INFO),
        ]
        d.Strm_ReadCurrentImage.restype = ctypes.c_uint32

        # GenApi functions (only available when camera opened with GenICam=true)
        d.GenApi_GetIntValue.argtypes = [
            ctypes.c_uint64, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int64),
        ]
        d.GenApi_GetIntValue.restype = ctypes.c_uint32

        d.GenApi_SetIntValue.argtypes = [
            ctypes.c_uint64, ctypes.c_char_p, ctypes.c_int64,
        ]
        d.GenApi_SetIntValue.restype = ctypes.c_uint32

        d.GenApi_GetFloatValue.argtypes = [
            ctypes.c_uint64, ctypes.c_char_p, ctypes.POINTER(ctypes.c_double),
        ]
        d.GenApi_GetFloatValue.restype = ctypes.c_uint32

        d.GenApi_SetFloatValue.argtypes = [
            ctypes.c_uint64, ctypes.c_char_p, ctypes.c_double,
        ]
        d.GenApi_SetFloatValue.restype = ctypes.c_uint32

        d.GenApi_GetStrValue.argtypes = [
            ctypes.c_uint64, ctypes.c_char_p,
            ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32),
        ]
        d.GenApi_GetStrValue.restype = ctypes.c_uint32

    def __getattr__(self, name):
        return getattr(self._dll, name)


# ─── Camera Class ─────────────────────────────────────────────────────

class TeliCamera:
    """
    Unified camera driver for the Toshiba Teli BU505MCF / BU505MCG
    (accepted models configurable in camera_config.json).

    Automatically selects the best available backend:
    1. pytelicam (official SDK Python wheel)
    2. Native TeliCamApi64.dll via ctypes
    3. Harvester + GenTL
    4. Simulation mode (no hardware)

    Parameters
    ----------
    backend : str
        Force a specific backend: "pytelicam", "native", "harvester", or "simulate".
        If "auto" (default), tries each in order.
    exposure_us : float
        Default exposure time in microseconds (default: 50000 = 50ms)
    """

    def __init__(
        self,
        backend: str = "auto",
        exposure_us: float = 50000,
        camera_index: Optional[int] = None,
        bit_depth: int = 12,
    ):
        self.exposure_us = exposure_us
        self._backend_name = backend
        self._camera_index = camera_index  # None=auto (try all), 0/1/2=specific
        self._bit_depth = bit_depth  # 8, 10, or 12
        self._bayer_pattern = "BayerBG"  # BU505MCF default, updated on connect
        self._connected = False

        # Backend-specific handles
        self._pytelicam_system = None
        self._pytelicam_device = None
        self._pytelicam_streaming = False
        self._harvester = None
        self._harvester_ia = None

        # Native API handles
        self._native_api: Optional[_TeliNativeAPI] = None
        self._native_cam = ctypes.c_uint64(0)
        self._native_strm = ctypes.c_uint64(0)
        self._native_payload_size = 0
        self._native_buf = None
        self._native_streaming = False
        self._native_genicam = False

        # Simulation state
        self._simulate = False
        self._sim_width = 2448
        self._sim_height = 2048

        # Camera info (populated on connect)
        self.width = 0
        self.height = 0
        self.model = ""
        self.serial = ""

        # Which camera model(s)/serial identify the microscope camera
        # (overridable via camera_config.json)
        cfg = _load_camera_config()
        models = cfg.get("accepted_models") or DEFAULT_ACCEPTED_MODELS
        if isinstance(models, str):        # tolerate a bare string in the json
            models = [models]
        models = [str(m) for m in models if str(m).strip()]
        self.accepted_models = tuple(models) or DEFAULT_ACCEPTED_MODELS
        self.preferred_serial = str(cfg.get("preferred_serial") or "")

    def connect(self) -> str:
        """
        Connect to the camera using the best available backend.

        Returns
        -------
        str
            Description of the connection (backend + camera info)
        """
        if self._backend_name == "simulate":
            return self._connect_simulated()

        if self._backend_name in ("auto", "pytelicam"):
            try:
                return self._connect_pytelicam()
            except Exception as e:
                if self._backend_name == "pytelicam":
                    raise
                print(f"  [camera] pytelicam not available: {e}")

        if self._backend_name in ("auto", "native"):
            try:
                return self._connect_native()
            except Exception as e:
                if self._backend_name == "native":
                    raise
                print(f"  [camera] Native API failed: {e}")

        if self._backend_name in ("auto", "harvester"):
            try:
                return self._connect_harvester()
            except Exception as e:
                if self._backend_name == "harvester":
                    raise
                print(f"  [camera] Harvester not available: {e}")

        if self._backend_name == "auto":
            print("  [camera] WARNING: No camera backend available, using simulation")
            return self._connect_simulated()

        raise RuntimeError(f"Backend '{self._backend_name}' not available")

    # ─── Native Backend ───────────────────────────────────────────

    # Selection ranks (lower wins): preferred serial beats any model match;
    # model matches rank by their position in accepted_models, so the
    # first-listed model (the camera currently on the scope) is chosen
    # even when a retired camera is still plugged into the PC.
    _RANK_PREFERRED_SERIAL = -1
    _RANK_UNRECOGNIZED = 900

    def _camera_rank(self, model: str, serial: str) -> int:
        if self.preferred_serial and serial == self.preferred_serial:
            return self._RANK_PREFERRED_SERIAL
        for i, m in enumerate(self.accepted_models):
            if m in model:
                return i
        return self._RANK_UNRECOGNIZED

    def _try_open_camera(self, api, cam_indices):
        """Try to open and stream from one of the given camera indices.
        Prioritizes the preferred serial (if configured), then accepted
        models in their camera_config.json order (BU505MCG before BU505MCF
        by default). Returns (cam, genicam, strm, max_payload) on success,
        or raises RuntimeError."""
        last_error = None
        locked_indices = []
        wrong_serial = []
        fallback = None  # (cam, genicam, strm, max_payload, cam_idx) best so far
        fallback_rank = None

        for cam_idx in cam_indices:
            print(f"\n--- Trying camera index {cam_idx} ---")

            cam = ctypes.c_uint64(0)
            genicam = False

            for access_mode, mode_name in [
                (CAM_ACCESS_MODE_CONTROL, "CONTROL"),
                (CAM_ACCESS_MODE_EXCLUSIVE, "EXCLUSIVE"),
            ]:
                st = api.Cam_Open(cam_idx, ctypes.byref(cam), None, 1, None, access_mode)
                if st == 0:
                    genicam = True
                    print(f"  Camera {cam_idx}: GenICam opened ({mode_name})")
                    break
                else:
                    print(f"  Camera {cam_idx}: GenICam {mode_name} failed: {_status_str(st)}")
                    cam = ctypes.c_uint64(0)

            if not genicam:
                print(f"  Camera {cam_idx}: Trying without GenICam...")
                st = api.Cam_Open(cam_idx, ctypes.byref(cam), None, 0, None, CAM_ACCESS_MODE_CONTROL)
                if st != 0:
                    print(f"  Camera {cam_idx}: LOCKED by another application")
                    locked_indices.append(cam_idx)
                    last_error = f"Camera {cam_idx} locked by another application"
                    continue

            # Check serial number if GenICam available
            serial = ""
            model = "unknown"
            if genicam:
                buf = ctypes.create_string_buffer(256)
                buf_len = ctypes.c_uint32(256)
                st_sn = api.GenApi_GetStrValue(cam, b"DeviceSerialNumber", buf, ctypes.byref(buf_len))
                if st_sn == 0:
                    serial = buf.value.decode()
                buf2 = ctypes.create_string_buffer(256)
                buf_len2 = ctypes.c_uint32(256)
                st_mn = api.GenApi_GetStrValue(cam, b"DeviceModelName", buf2, ctypes.byref(buf_len2))
                model = buf2.value.decode() if st_mn == 0 else "unknown"
                print(f"  Camera {cam_idx}: Model={model}, Serial={serial}")

            # Try opening stream
            strm = ctypes.c_uint64(0)
            max_payload = ctypes.c_uint32(0)
            stream_ok = False
            for num_bufs in (8, 4, 2):
                strm = ctypes.c_uint64(0)
                max_payload = ctypes.c_uint32(0)
                st = api.Strm_OpenSimple(cam, ctypes.byref(strm), ctypes.byref(max_payload), None, num_bufs, 0)
                if st == 0:
                    print(f"  Camera {cam_idx}: Stream opened (buffers={num_bufs})")
                    stream_ok = True
                    break
                print(f"  Camera {cam_idx}: Strm_OpenSimple (bufs={num_bufs}): {_status_str(st)}")

            if not stream_ok:
                print(f"  Camera {cam_idx}: Stream failed, closing...")
                api.Cam_Close(cam)
                last_error = f"Strm_OpenSimple failed on camera {cam_idx}"
                continue

            # Stream opened! Rank this camera against what we have so far
            rank = self._camera_rank(model, serial)
            if rank == self._RANK_PREFERRED_SERIAL:
                print(f"  Camera {cam_idx}: preferred serial (S/N {serial}) — using this one!")
            elif rank < self._RANK_UNRECOGNIZED:
                print(f"  Camera {cam_idx}: Model matches microscope camera "
                      f"({model}, priority {rank + 1} of {len(self.accepted_models)})")
            else:
                if serial:
                    wrong_serial.append(f"cam{cam_idx}={serial}")
                print(f"  Camera {cam_idx}: Unrecognized model (got {model}, "
                      f"want one of {'/'.join(self.accepted_models)})")

            if fallback is None or rank < fallback_rank:
                if fallback is not None:
                    prev_cam, prev_gc, prev_strm, prev_mp, prev_idx = fallback
                    api.Strm_Stop(prev_strm)
                    api.Strm_Close(prev_strm)
                    api.Cam_Close(prev_cam)
                fallback = (cam, genicam, strm, max_payload, cam_idx)
                fallback_rank = rank
                if rank == self._RANK_PREFERRED_SERIAL:
                    break  # exact serial match — no need to keep looking
            else:
                # Not better than what we already hold — close it
                api.Strm_Stop(strm)
                api.Strm_Close(strm)
                api.Cam_Close(cam)

        # Microscope camera not found — report clearly
        if locked_indices:
            print(f"\n  WARNING: Camera(s) {locked_indices} locked by other software.")
            print(f"  The microscope camera ({'/'.join(self.accepted_models)}) "
                  f"may be among them.")
            print(f"  Close other microscope software to free the camera.")

        if fallback is not None:
            cam, genicam, strm, max_payload, idx = fallback
            if fallback_rank >= self._RANK_UNRECOGNIZED:
                print(f"\n  Using camera {idx} as fallback "
                      f"(not confirmed as microscope camera)")
            else:
                print(f"\n  Using camera {idx}")
            return cam, genicam, strm, max_payload

        raise RuntimeError(
            f"Microscope camera ({'/'.join(self.accepted_models)}) not available. "
            f"Locked cameras: {locked_indices}. Other serials found: {wrong_serial}"
        )

    def _connect_native(self) -> str:
        """Connect using native TeliCamApi64.dll via ctypes."""
        dll_dir = find_teli_dll_dir()
        if not dll_dir:
            raise RuntimeError("TeliCamApi64.dll not found")

        api = _TeliNativeAPI(dll_dir)

        # Retry outer loop: terminate + re-init if first pass fails
        MAX_RETRIES = 3
        for retry in range(MAX_RETRIES):
            if retry > 0:
                print(f"\n=== Retry {retry}/{MAX_RETRIES-1}: re-initializing USB3 system... ===")
                try:
                    api.Sys_Terminate()
                except Exception:
                    pass
                time.sleep(1.5)

            # Initialize system
            st = api.Sys_Initialize(CAM_TYPE_U3V)
            if st not in (0, 0x02):  # SUCCESS or ALREADY_INITIALIZED
                raise RuntimeError(f"Sys_Initialize failed: {_status_str(st)}")

            # Count cameras
            n = ctypes.c_uint32(0)
            st = api.Sys_GetNumOfCameras(ctypes.byref(n))
            if st != 0 or n.value == 0:
                api.Sys_Terminate()
                raise RuntimeError("No USB3 Vision cameras found")

            num_cameras = n.value
            print(f"Found {num_cameras} USB3 Vision camera(s) (attempt {retry+1}/{MAX_RETRIES})")

            self._native_api = api

            if self._camera_index is not None:
                cam_indices = [self._camera_index]
                print(f"Using specified camera index: {self._camera_index}")
            else:
                cam_indices = list(range(num_cameras))
                print(f"Auto-detecting: will try all {num_cameras} cameras")

            try:
                cam, genicam, strm, max_payload = self._try_open_camera(api, cam_indices)
                # Success!
                self._native_cam = cam
                self._native_genicam = genicam
                self._native_strm = strm
                self._native_payload_size = max_payload.value
                self._native_buf = (ctypes.c_uint8 * max_payload.value)()
                break
            except RuntimeError as e:
                print(f"  Attempt {retry+1} failed: {e}")
                if retry == MAX_RETRIES - 1:
                    api.Sys_Terminate()
                    self._native_api = None
                    raise

        # Start continuous streaming
        st = api.Strm_Start(strm, CAM_ACQ_MODE_CONTINUOUS)
        if st != 0:
            api.Strm_Close(strm)
            api.Cam_Close(cam)
            api.Sys_Terminate()
            self._native_api = None
            raise RuntimeError(f"Strm_Start failed: {_status_str(st)}")

        self._native_streaming = True

        # Wait for first frame to determine resolution
        time.sleep(0.3)
        read_size = ctypes.c_uint32(self._native_payload_size)
        img_info = CAM_IMAGE_INFO()
        st = api.Strm_ReadCurrentImage(
            strm, ctypes.cast(self._native_buf, ctypes.c_void_p),
            ctypes.byref(read_size), ctypes.byref(img_info),
        )

        if st == 0:
            self.width = img_info.uiSizeX
            self.height = img_info.uiSizeY
        else:
            # Fallback defaults
            self.width = 2448
            self.height = 2048

        # Try to read model/serial via GenApi if available
        if self._native_genicam:
            self.model = self._native_get_str(b"DeviceModelName") or self.accepted_models[0]
            self.serial = self._native_get_str(b"DeviceSerialNumber") or ""
        else:
            self.model = self.accepted_models[0]
            self.serial = ""

        self._backend_name = "native"
        self._connected = True

        # Set exposure control to Manual so sliders work via IIDC2
        # CAM_EXPOSURE_TIME_CONTROL_MANUAL = 1
        st = api.SetCamExposureTimeControl(self._native_cam, ctypes.c_int(1))
        if st == 0:
            print("Exposure control set to Manual (IIDC2)")
        else:
            print(f"SetCamExposureTimeControl(Manual) failed: {_status_str(st)}")

        genicam_str = "+GenICam" if self._native_genicam else "(no GenICam)"
        info = f"Native {genicam_str}: {self.model} ({self.width}x{self.height})"
        if self.serial:
            info += f" S/N: {self.serial}"
        logger.info(f"Connected via {info}")
        print(f"Camera connected: {info}")
        return info

    def _native_get_str(self, feature: bytes) -> str:
        """Read a string feature via GenApi."""
        if not self._native_genicam or not self._native_api:
            return ""
        buf = ctypes.create_string_buffer(256)
        buf_len = ctypes.c_uint32(256)
        st = self._native_api.GenApi_GetStrValue(
            self._native_cam, feature, buf, ctypes.byref(buf_len),
        )
        return buf.value.decode() if st == 0 else ""

    def _native_get_int(self, feature: bytes) -> Optional[int]:
        """Read an integer feature via GenApi."""
        if not self._native_genicam or not self._native_api:
            return None
        val = ctypes.c_int64(0)
        st = self._native_api.GenApi_GetIntValue(
            self._native_cam, feature, ctypes.byref(val),
        )
        return val.value if st == 0 else None

    def _native_get_float(self, feature: bytes) -> Optional[float]:
        """Read a float feature via GenApi."""
        if not self._native_genicam or not self._native_api:
            return None
        val = ctypes.c_double(0)
        st = self._native_api.GenApi_GetFloatValue(
            self._native_cam, feature, ctypes.byref(val),
        )
        return val.value if st == 0 else None

    def _capture_native(self) -> np.ndarray:
        """Capture via native TeliCamApi."""
        api = self._native_api
        read_size = ctypes.c_uint32(self._native_payload_size)
        img_info = CAM_IMAGE_INFO()

        # Try up to 3 times with brief waits
        for attempt in range(3):
            st = api.Strm_ReadCurrentImage(
                self._native_strm,
                ctypes.cast(self._native_buf, ctypes.c_void_p),
                ctypes.byref(read_size),
                ctypes.byref(img_info),
            )
            if st == 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(f"Strm_ReadCurrentImage failed: {_status_str(st)}")

        w = img_info.uiSizeX
        h = img_info.uiSizeY
        fmt = img_info.uiPixelFormat
        data = np.ctypeslib.as_array(
            self._native_buf, shape=(self._native_payload_size,)
        )[:read_size.value].copy()

        pixels = w * h

        if fmt == PXL_FMT_MONO8:
            return data[:pixels].reshape(h, w)
        elif fmt in (PXL_FMT_RGB8, PXL_FMT_BGR8):
            img = data[:pixels * 3].reshape(h, w, 3)
            if fmt == PXL_FMT_BGR8:
                img = img[:, :, ::-1].copy()  # BGR -> RGB
            return img
        elif fmt in (PXL_FMT_BAYERGR8, PXL_FMT_BAYERRG8,
                     PXL_FMT_BAYERGB8, PXL_FMT_BAYERBG8):
            # Debayer 8-bit using OpenCV
            raw = data[:pixels].reshape(h, w)
            try:
                import cv2
                # OpenCV Bayer naming quirk: ..2BGR yields true RGB for a
                # PFNC-named sensor (..2RGB would swap R<->B). See _get_bayer_code.
                bayer_map = {
                    PXL_FMT_BAYERGR8: cv2.COLOR_BAYER_GR2BGR,
                    PXL_FMT_BAYERRG8: cv2.COLOR_BAYER_RG2BGR,
                    PXL_FMT_BAYERGB8: cv2.COLOR_BAYER_GB2BGR,
                    PXL_FMT_BAYERBG8: cv2.COLOR_BAYER_BG2BGR,
                }
                return cv2.cvtColor(raw, bayer_map[fmt])
            except ImportError:
                return raw  # Return raw Bayer as mono
        elif fmt in (PXL_FMT_BAYERGR12, PXL_FMT_BAYERRG12,
                     PXL_FMT_BAYERGB12, PXL_FMT_BAYERBG12):
            # Debayer 12-bit: data is uint16 (2 bytes per pixel)
            raw16 = np.frombuffer(data[:pixels * 2].tobytes(), dtype=np.uint16).reshape(h, w)
            raw16 = raw16.astype(np.uint16) << 4  # 12-bit → 16-bit range
            try:
                import cv2
                # ..2BGR yields true RGB for a PFNC-named sensor. See _get_bayer_code.
                bayer_map_12 = {
                    PXL_FMT_BAYERGR12: cv2.COLOR_BAYER_GR2BGR,
                    PXL_FMT_BAYERRG12: cv2.COLOR_BAYER_RG2BGR,
                    PXL_FMT_BAYERGB12: cv2.COLOR_BAYER_GB2BGR,
                    PXL_FMT_BAYERBG12: cv2.COLOR_BAYER_BG2BGR,
                }
                return cv2.cvtColor(raw16, bayer_map_12[fmt])
            except ImportError:
                return raw16
        elif fmt in (PXL_FMT_BAYERGR10, PXL_FMT_BAYERRG10):
            # Debayer 10-bit: data is uint16
            raw16 = np.frombuffer(data[:pixels * 2].tobytes(), dtype=np.uint16).reshape(h, w)
            raw16 = raw16.astype(np.uint16) << 6  # 10-bit → 16-bit range
            try:
                import cv2
                # ..2BGR yields true RGB for a PFNC-named sensor. See _get_bayer_code.
                return cv2.cvtColor(raw16, cv2.COLOR_BAYER_GR2BGR)
            except ImportError:
                return raw16
        else:
            # Unknown format, try to interpret based on data size
            if read_size.value >= pixels * 3:
                return data[:pixels * 3].reshape(h, w, 3)
            elif read_size.value >= pixels:
                return data[:pixels].reshape(h, w)
            else:
                raise RuntimeError(
                    f"Unknown pixel format 0x{fmt:08X}, "
                    f"data={read_size.value} bytes for {w}x{h}"
                )

    def _disconnect_native(self):
        """Clean up native API resources."""
        api = self._native_api
        if api is None:
            return

        if self._native_streaming:
            try:
                api.Strm_Stop(self._native_strm)
            except Exception:
                pass
            self._native_streaming = False

        if self._native_strm.value != 0:
            try:
                api.Strm_Close(self._native_strm)
            except Exception:
                pass
            self._native_strm = ctypes.c_uint64(0)

        if self._native_cam.value != 0:
            try:
                api.Cam_Close(self._native_cam)
            except Exception:
                pass
            self._native_cam = ctypes.c_uint64(0)

        try:
            api.Sys_Terminate()
        except Exception:
            pass

        self._native_api = None
        self._native_buf = None
        self._native_genicam = False

    # ─── pytelicam Backend ────────────────────────────────────────

    def _connect_pytelicam(self) -> str:
        """Connect using official pytelicam SDK with 12-bit support."""
        import pytelicam

        self._pytelicam_system = pytelicam.get_camera_system()
        cam_num = self._pytelicam_system.get_num_of_cameras()

        if cam_num == 0:
            self._pytelicam_system.terminate()
            self._pytelicam_system = None
            raise RuntimeError("No Teli cameras found")

        print(f"Found {cam_num} Teli camera(s) via pytelicam")

        # Find the microscope camera — match accepted model names
        # (BU505MCG/BU505MCF) when readable; pytelicam can't always read
        # model names, so 12-bit Bayer support is kept as a fallback tell
        device = None
        wild_rank = None
        for idx in range(cam_num):
            try:
                dev = self._pytelicam_system.create_device_object(idx)
                dev.open()

                # Check available pixel formats to identify camera
                avail_fmts = ()
                try:
                    avail_fmts = dev.genapi.get_available_enum_entry_names("PixelFormat")
                except Exception:
                    pass

                # Try to get model/serial
                model, serial = "", ""
                try:
                    st, val = dev.genapi.get_str_value("DeviceModelName")
                    if isinstance(val, str) and val:
                        model = val
                except Exception:
                    pass
                try:
                    st, val = dev.genapi.get_str_value("DeviceSerialNumber")
                    if isinstance(val, str) and val:
                        serial = val
                except Exception:
                    pass

                has_bayer12 = any(
                    str(f).startswith("Bayer") and "12" in str(f)
                    for f in avail_fmts
                )
                # Rank: preferred serial / accepted-model order first.
                # A camera whose model name pytelicam CANNOT read but that
                # supports 12-bit Bayer ranks between the first- and
                # second-listed models (0.5): it is more likely the scope
                # camera than a readable second-priority (retired) model,
                # but loses to a readable first-priority match. Set
                # preferred_serial in camera_config.json to disambiguate
                # definitively when multiple cameras are attached.
                rank = self._camera_rank(model, serial)
                if rank >= self._RANK_UNRECOGNIZED and not model and has_bayer12:
                    rank = 0.5
                print(f"  Camera {idx}: Model={model}, Serial={serial}, "
                      f"Formats={avail_fmts}, "
                      f"Bayer12={'YES' if has_bayer12 else 'NO'}, "
                      f"Rank={rank}")

                if device is None or rank < wild_rank:
                    if device is not None:
                        device.close()
                    device = dev
                    wild_rank = rank
                    if model:
                        self.model = model
                    elif rank < self._RANK_UNRECOGNIZED:
                        self.model = self.accepted_models[0]
                    else:
                        self.model = "Teli Camera"
                    self.serial = serial
                    print(f"  -> Camera {idx} is current best candidate")
                else:
                    dev.close()
            except Exception as e:
                print(f"  Camera {idx}: open failed: {e}")

        if device is not None and wild_rank >= self._RANK_UNRECOGNIZED:
            print(f"  WARNING: using unrecognized camera (Model={self.model})")

        if device is None:
            self._pytelicam_system.terminate()
            self._pytelicam_system = None
            raise RuntimeError("No Teli cameras could be opened")

        self._pytelicam_device = device

        # Get resolution via cam_control
        cam_ctrl = device.cam_control
        try:
            st, w = cam_ctrl.get_width()
            st2, h = cam_ctrl.get_height()
            self.width = w
            self.height = h
        except Exception:
            self.width = 2448
            self.height = 2048

        # List available pixel formats
        try:
            avail = device.genapi.get_available_enum_entry_names("PixelFormat")
            print(f"  Available pixel formats: {avail}")
        except Exception as e:
            print(f"  Could not list pixel formats: {e}")

        # Set pixel format for desired bit depth
        # BU505MCF uses BayerBG pattern — try BG first, then GR/RG/GB as fallbacks
        actual_bit_depth = 8
        self._bayer_pattern = None  # track which pattern for debayering

        if self._bit_depth >= 12:
            for fmt_name in ("BayerBG12", "BayerGR12", "BayerRG12", "BayerGB12"):
                try:
                    device.genapi.set_enum_str_value("PixelFormat", fmt_name)
                    # Verify it actually changed
                    st, cur = device.genapi.get_enum_str_value("PixelFormat")
                    if fmt_name in str(cur):
                        actual_bit_depth = 12
                        self._bayer_pattern = fmt_name.replace("12", "")  # e.g. "BayerBG"
                        print(f"  Pixel format set: {fmt_name} (12-bit) [verified: {cur}]")
                        break
                    else:
                        print(f"  {fmt_name}: set_enum_str_value succeeded but format is still {cur}")
                except Exception as e:
                    print(f"  {fmt_name} not available: {e}")
        if self._bit_depth >= 10 and actual_bit_depth < 10:
            for fmt_name in ("BayerBG10", "BayerGR10", "BayerRG10", "BayerGB10"):
                try:
                    device.genapi.set_enum_str_value("PixelFormat", fmt_name)
                    st, cur = device.genapi.get_enum_str_value("PixelFormat")
                    if fmt_name in str(cur):
                        actual_bit_depth = 10
                        self._bayer_pattern = fmt_name.replace("10", "")
                        print(f"  Pixel format set: {fmt_name} (10-bit) [verified: {cur}]")
                        break
                    else:
                        print(f"  {fmt_name}: set succeeded but format is still {cur}")
                except Exception as e:
                    print(f"  {fmt_name} not available: {e}")
        if actual_bit_depth == 8:
            # Try explicit 8-bit Bayer, or keep camera default
            for fmt_name in ("BayerBG8", "BayerGR8", "BayerRG8", "BayerGB8"):
                try:
                    device.genapi.set_enum_str_value("PixelFormat", fmt_name)
                    st, cur = device.genapi.get_enum_str_value("PixelFormat")
                    if fmt_name in str(cur):
                        self._bayer_pattern = fmt_name.replace("8", "")
                        print(f"  Pixel format set: {fmt_name} (8-bit)")
                        break
                except Exception:
                    pass
            else:
                print("  Pixel format: using camera default")
        self._bit_depth = actual_bit_depth

        # Show final pixel format
        try:
            st, cur_fmt = device.genapi.get_enum_str_value("PixelFormat")
            print(f"  Active pixel format: {cur_fmt}")
        except Exception:
            pass

        # Set exposure control to Manual (IIDC2)
        try:
            cam_ctrl.set_exposure_time_control(pytelicam.CameraExposureTimeCtrl.Manual)
            print("  Exposure control: Manual (IIDC2)")
        except Exception as e:
            print(f"  Could not set Manual exposure control: {e}")

        # Set initial exposure
        try:
            cam_ctrl.set_exposure_time(self.exposure_us)
            print(f"  Exposure: {self.exposure_us:.0f} us")
        except Exception as e:
            print(f"  Could not set exposure: {e}")

        # Open stream and start continuous acquisition
        try:
            cam_stream = device.cam_stream
            cam_stream.open()
            cam_stream.start(pytelicam.CameraAcquisitionMode.Continuous)
            self._pytelicam_streaming = True
            print("  Stream: continuous acquisition started")
        except Exception as e:
            print(f"  Stream start failed: {e}")
            # Stream may still work via get_next_image on demand

        # Wait for first frame to settle
        time.sleep(0.3)

        self._backend_name = "pytelicam"
        self._connected = True

        info = f"pytelicam ({self._bit_depth}-bit): {self.model} ({self.width}x{self.height}) S/N: {self.serial}"
        logger.info(f"Connected via {info}")
        print(f"Camera connected: {info}")
        return info

    def _capture_pytelicam(self) -> np.ndarray:
        """Capture via pytelicam. Returns uint16 RGB for 12-bit, uint8 RGB for 8-bit."""
        import pytelicam
        import cv2

        dev = self._pytelicam_device
        cam_stream = dev.cam_stream

        # Ensure stream is running
        if not self._pytelicam_streaming:
            try:
                cam_stream.open()
                cam_stream.start(pytelicam.CameraAcquisitionMode.Continuous)
                self._pytelicam_streaming = True
            except Exception:
                pass

        # Get the latest buffered frame (continuous mode)
        try:
            image_data = cam_stream.get_current_buffered_image()
        except Exception:
            # Fallback to get_next_image if get_current_buffered_image not available
            image_data = cam_stream.get_next_image()

        try:
            if self._bit_depth > 8:
                # 12-bit or 10-bit: get raw Bayer data as uint16
                # Use OutputImageType.Raw to get native format data
                try:
                    raw = image_data.get_ndarray(pytelicam.OutputImageType.Raw)
                except Exception:
                    try:
                        raw = image_data.get_ndarray()
                    except Exception:
                        raw = None

                if raw is not None and raw.dtype == np.uint16 and raw.ndim == 2:
                    # Got raw uint16 Bayer data — debayer with OpenCV
                    h, w = raw.shape
                    if self._bit_depth == 12:
                        # Left-shift 12-bit → fill 16-bit range (0-4095 → 0-65520)
                        raw = raw.astype(np.uint16) << 4
                    elif self._bit_depth == 10:
                        # Left-shift 10-bit → fill 16-bit range (0-1023 → 0-65472)
                        raw = raw.astype(np.uint16) << 6
                    # Debayer using detected Bayer pattern
                    bayer_code = self._get_bayer_code()
                    img = cv2.cvtColor(raw, bayer_code)
                    return img
                elif raw is not None and raw.dtype == np.uint16 and raw.ndim == 3:
                    # Already debayered uint16 color — convert BGR→RGB if needed
                    if raw.shape[2] == 3:
                        return raw[:, :, ::-1].copy()  # BGR → RGB
                    return raw
                else:
                    # Fallback: try Bgr24 (loses bit depth but at least works)
                    print(f"  [camera] Raw capture unexpected: dtype={raw.dtype if raw is not None else 'None'}, "
                          f"ndim={raw.ndim if raw is not None else 'None'}. Falling back to 8-bit.")
                    img = image_data.get_ndarray(pytelicam.OutputImageType.Bgr24)
                    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            else:
                # 8-bit: use standard Bgr24 output
                try:
                    img = image_data.get_ndarray(pytelicam.OutputImageType.Bgr24)
                    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                except Exception:
                    try:
                        img = image_data.get_ndarray(pytelicam.OutputImageType.Mono8)
                        return img
                    except Exception:
                        img = image_data.get_ndarray()
                        if img.ndim == 3 and img.shape[2] == 3:
                            return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                        return img
        finally:
            try:
                image_data.release()
            except Exception:
                pass

    # ─── Harvester Backend ────────────────────────────────────────

    def _connect_harvester(self) -> str:
        """Connect using Harvester + GenTL."""
        from harvesters.core import Harvester

        cti_path = find_teli_cti()
        if not cti_path:
            raise RuntimeError("Teli CTI file not found")

        self._harvester = Harvester()
        self._harvester.add_file(cti_path)
        self._harvester.update()

        if len(self._harvester.device_info_list) == 0:
            self._harvester.reset()
            self._harvester = None
            raise RuntimeError("No cameras found via Harvester/GenTL")

        dev_info = self._harvester.device_info_list[0]
        self.model = dev_info.model or "Teli Camera"
        self.serial = dev_info.serial_number or ""

        self._harvester_ia = self._harvester.create(0)

        try:
            nm = self._harvester_ia.remote_device.node_map
            self.width = nm.Width.value
            self.height = nm.Height.value
            nm.ExposureTime.value = self.exposure_us
        except Exception:
            self.width = 2448
            self.height = 2048

        self._harvester_ia.start()

        self._backend_name = "harvester"
        self._connected = True
        info = f"Harvester: {self.model} ({self.width}x{self.height}) S/N: {self.serial}"
        logger.info(f"Connected via {info}")
        print(f"Camera connected: {info}")
        return info

    def _capture_harvester(self) -> np.ndarray:
        """Capture via Harvester."""
        with self._harvester_ia.fetch(timeout=5.0) as buffer:
            component = buffer.payload.components[0]
            w = component.width
            h = component.height
            data = component.data.copy()

            pixels = w * h
            if len(data) == pixels * 3:
                return data.reshape(h, w, 3)
            elif len(data) == pixels:
                return data.reshape(h, w)
            else:
                return data[:pixels].reshape(h, w)

    # ─── Simulated Backend ────────────────────────────────────────

    def _connect_simulated(self) -> str:
        """Simulated camera (no hardware)."""
        self._simulate = True
        self.width = self._sim_width
        self.height = self._sim_height
        self.model = f"{self.accepted_models[0]} (SIMULATED)"
        self.serial = "SIM-0510129"
        self._backend_name = "simulate"
        self._connected = True
        info = f"Simulated: {self.model} ({self.width}x{self.height})"
        logger.info(info)
        print(f"Camera connected: {info}")
        return info

    def _capture_simulated(self) -> np.ndarray:
        """Generate a synthetic test pattern."""
        img = np.zeros((self.height, self.width), dtype=np.uint8)
        img[::64, :] = 80
        img[:, ::64] = 80
        cy, cx = self.height // 2, self.width // 2
        h4, w4 = self.height // 4, self.width // 4
        img[cy - h4:cy + h4, cx - w4:cx + w4] = 120
        noise = np.random.randint(0, 30, img.shape, dtype=np.uint8)
        img = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        return img

    # ─── Common Interface ─────────────────────────────────────────

    def disconnect(self):
        """Disconnect from the camera."""
        if self._backend_name == "native":
            self._disconnect_native()

        if self._pytelicam_device:
            if self._pytelicam_streaming:
                try:
                    self._pytelicam_device.cam_stream.stop()
                except Exception:
                    pass
                try:
                    self._pytelicam_device.cam_stream.close()
                except Exception:
                    pass
                self._pytelicam_streaming = False
            try:
                self._pytelicam_device.close()
            except Exception:
                pass
            self._pytelicam_device = None
        if self._pytelicam_system:
            try:
                self._pytelicam_system.terminate()
            except Exception:
                pass
            self._pytelicam_system = None

        if self._harvester_ia:
            self._harvester_ia.stop()
            self._harvester_ia.destroy()
            self._harvester_ia = None
        if self._harvester:
            self._harvester.reset()
            self._harvester = None

        self._connected = False
        self._simulate = False
        logger.info("Camera disconnected")

    @property
    def connected(self) -> bool:
        return self._connected

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()

    def capture(self) -> np.ndarray:
        """
        Capture a single image.

        Returns
        -------
        np.ndarray
            Image as numpy array (H x W for mono, H x W x 3 for color)
        """
        if not self._connected:
            raise RuntimeError("Camera not connected")

        if self._simulate:
            return self._capture_simulated()
        if self._backend_name == "pytelicam":
            return self._capture_pytelicam()
        if self._backend_name == "native":
            return self._capture_native()
        if self._backend_name == "harvester":
            return self._capture_harvester()

        raise RuntimeError(f"Unknown backend: {self._backend_name}")

    def save(self, filepath: str, img: Optional[np.ndarray] = None) -> bool:
        """
        Capture (if img not provided) and save to file.

        Parameters
        ----------
        filepath : str
            Output path (.tif, .png, .jpg)
        img : np.ndarray, optional
            Image to save. If None, captures a new image.

        Returns
        -------
        bool
            True if successful
        """
        try:
            if img is None:
                img = self.capture()

            ext = Path(filepath).suffix.lower()

            if ext in (".tif", ".tiff"):
                try:
                    import tifffile
                    tifffile.imwrite(filepath, img)
                    return True
                except ImportError:
                    pass

            try:
                from PIL import Image
                pil_img = Image.fromarray(img)
                pil_img.save(filepath)
                return True
            except ImportError:
                pass

            np.save(filepath.replace(ext, ".npy"), img)
            return True

        except Exception as e:
            logger.error(f"Save failed: {e}")
            return False

    def capture_and_save(self, filepath: str) -> bool:
        """Capture and save - compatible with DieMapper's capture_func signature."""
        return self.save(filepath)

    # ─── Settings ─────────────────────────────────────────────────

    def set_exposure(self, exposure_us: float):
        """Set exposure time in microseconds."""
        self.exposure_us = exposure_us
        if self._simulate:
            return

        if self._backend_name == "native" and self._native_api:
            # Use IIDC2 register-level API (works without GenICam)
            st = self._native_api.SetCamExposureTime(
                self._native_cam, ctypes.c_double(exposure_us),
            )
            if st != 0:
                logger.warning(f"Could not set exposure: {_status_str(st)}")
        elif self._backend_name == "pytelicam" and self._pytelicam_device:
            try:
                self._pytelicam_device.cam_control.set_exposure_time(exposure_us)
            except Exception as e:
                logger.warning(f"Could not set exposure: {e}")
        elif self._backend_name == "harvester" and self._harvester_ia:
            try:
                nm = self._harvester_ia.remote_device.node_map
                nm.ExposureTime.value = exposure_us
            except Exception as e:
                logger.warning(f"Could not set exposure: {e}")

    def get_exposure(self) -> float:
        """Get current exposure time in microseconds."""
        if self._simulate:
            return self.exposure_us
        if self._backend_name == "native" and self._native_api:
            val = ctypes.c_double(0)
            st = self._native_api.GetCamExposureTime(
                self._native_cam, ctypes.byref(val),
            )
            if st == 0:
                return val.value
            return self.exposure_us
        if self._backend_name == "pytelicam" and self._pytelicam_device:
            try:
                st, val = self._pytelicam_device.cam_control.get_exposure_time()
                return val
            except Exception:
                pass
        elif self._backend_name == "harvester" and self._harvester_ia:
            try:
                nm = self._harvester_ia.remote_device.node_map
                return nm.ExposureTime.value
            except Exception:
                pass
        return self.exposure_us

    def set_gain(self, gain_db: float):
        """Set gain in dB. Typical range for BU505MC: 0.0 to 24.0 dB."""
        if self._simulate:
            return

        if self._backend_name == "native" and self._native_api:
            # Use IIDC2 register-level API (works without GenICam)
            st = self._native_api.SetCamGain(
                self._native_cam, ctypes.c_double(gain_db),
            )
            if st != 0:
                logger.warning(f"Could not set gain: {_status_str(st)}")
        elif self._backend_name == "pytelicam" and self._pytelicam_device:
            try:
                self._pytelicam_device.cam_control.set_gain(gain_db)
            except Exception as e:
                logger.warning(f"Could not set gain: {e}")
        elif self._backend_name == "harvester" and self._harvester_ia:
            try:
                nm = self._harvester_ia.remote_device.node_map
                nm.Gain.value = gain_db
            except Exception as e:
                logger.warning(f"Could not set gain: {e}")

    def get_gain(self) -> float:
        """Get current gain in dB."""
        if self._simulate:
            return 0.0
        if self._backend_name == "native" and self._native_api:
            val = ctypes.c_double(0)
            st = self._native_api.GetCamGain(
                self._native_cam, ctypes.byref(val),
            )
            if st == 0:
                return val.value
            return 0.0
        if self._backend_name == "pytelicam" and self._pytelicam_device:
            try:
                st, val = self._pytelicam_device.cam_control.get_gain()
                return val
            except Exception:
                pass
        elif self._backend_name == "harvester" and self._harvester_ia:
            try:
                nm = self._harvester_ia.remote_device.node_map
                return nm.Gain.value
            except Exception:
                pass
        return 0.0

    def _get_bayer_code(self):
        """Get the OpenCV Bayer demosaicing code for the detected pattern.

        NOTE on OpenCV's Bayer naming quirk: cv2.COLOR_BAYER_<X>2RGB is an
        alias for COLOR_BAYER_<complement>2BGR, so debayering a PFNC "BayerX"
        sensor with COLOR_BAYER_X2RGB actually yields a BGR-ordered array
        (red and blue channels swapped). The name-matching COLOR_BAYER_X2BGR
        constant is the one that produces a true RGB array (channel0 = Red)
        for a PFNC "BayerX" sensor.
        """
        import cv2
        bayer_map = {
            "BayerBG": cv2.COLOR_BAYER_BG2BGR,
            "BayerGR": cv2.COLOR_BAYER_GR2BGR,
            "BayerRG": cv2.COLOR_BAYER_RG2BGR,
            "BayerGB": cv2.COLOR_BAYER_GB2BGR,
        }
        pattern = getattr(self, '_bayer_pattern', None) or "BayerBG"
        return bayer_map.get(pattern, cv2.COLOR_BAYER_BG2BGR)

    def get_bit_depth(self) -> int:
        """Get the active capture bit depth (8, 10, or 12)."""
        return self._bit_depth

    def set_bit_depth(self, target: int) -> int:
        """Switch camera between 8-bit and 12-bit capture.

        Stops the stream, changes PixelFormat, and restarts (~1-2 sec pause).
        Only works with pytelicam backend.

        Parameters
        ----------
        target : int
            Desired bit depth (8 or 12).

        Returns
        -------
        int
            Actual bit depth achieved.
        """
        if self._backend_name != "pytelicam":
            print(f"  [set_bit_depth] Not supported on backend '{self._backend_name}'")
            return self._bit_depth

        if target not in (8, 12):
            print(f"  [set_bit_depth] Invalid target {target}, must be 8 or 12")
            return self._bit_depth

        if target == self._bit_depth:
            print(f"  [set_bit_depth] Already at {target}-bit, no change needed")
            return self._bit_depth

        import pytelicam

        device = self._pytelicam_device
        if not device:
            print("  [set_bit_depth] No device connected")
            return self._bit_depth

        print(f"  [set_bit_depth] Switching from {self._bit_depth}-bit to {target}-bit...")

        # Stop stream
        if self._pytelicam_streaming:
            try:
                device.cam_stream.stop()
            except Exception as e:
                print(f"  [set_bit_depth] Stream stop error: {e}")
            try:
                device.cam_stream.close()
            except Exception as e:
                print(f"  [set_bit_depth] Stream close error: {e}")
            self._pytelicam_streaming = False

        # Set pixel format (reuse logic from _connect_pytelicam)
        actual_bit_depth = 8
        if target >= 12:
            for fmt_name in ("BayerBG12", "BayerGR12", "BayerRG12", "BayerGB12"):
                try:
                    device.genapi.set_enum_str_value("PixelFormat", fmt_name)
                    st, cur = device.genapi.get_enum_str_value("PixelFormat")
                    if fmt_name in str(cur):
                        actual_bit_depth = 12
                        self._bayer_pattern = fmt_name.replace("12", "")
                        print(f"  [set_bit_depth] Pixel format set: {fmt_name} [verified: {cur}]")
                        break
                    else:
                        print(f"  [set_bit_depth] {fmt_name}: set succeeded but format is {cur}")
                except Exception as e:
                    print(f"  [set_bit_depth] {fmt_name} not available: {e}")

        if actual_bit_depth == 8:
            for fmt_name in ("BayerBG8", "BayerGR8", "BayerRG8", "BayerGB8"):
                try:
                    device.genapi.set_enum_str_value("PixelFormat", fmt_name)
                    st, cur = device.genapi.get_enum_str_value("PixelFormat")
                    if fmt_name in str(cur):
                        self._bayer_pattern = fmt_name.replace("8", "")
                        print(f"  [set_bit_depth] Pixel format set: {fmt_name} (8-bit)")
                        break
                except Exception:
                    pass
            else:
                print("  [set_bit_depth] Pixel format: using camera default")

        self._bit_depth = actual_bit_depth

        # Restart stream
        try:
            device.cam_stream.open()
            device.cam_stream.start(pytelicam.CameraAcquisitionMode.Continuous)
            self._pytelicam_streaming = True
            print(f"  [set_bit_depth] Stream restarted at {actual_bit_depth}-bit")
        except Exception as e:
            print(f"  [set_bit_depth] Stream restart failed: {e}")

        # Let camera settle with new format
        time.sleep(0.3)

        return actual_bit_depth

    def get_resolution(self) -> Tuple[int, int]:
        """Get camera resolution (width, height)."""
        return (self.width, self.height)

    def __repr__(self):
        state = "connected" if self._connected else "disconnected"
        return f"TeliCamera({self._backend_name}, {self.model}, {state})"


# ─── Diagnostics ──────────────────────────────────────────────────────

def diagnose():
    """Print diagnostic info about Teli camera setup."""
    print("=" * 60)
    print("  TOSHIBA TELI CAMERA DIAGNOSTICS")
    print("=" * 60)

    status = check_sdk_installed()

    print(f"\n  pytelicam installed:  {'YES' if status['pytelicam'] else 'NO'}")
    print(f"  Native DLL found:    {'YES' if status['native_dll'] else 'NO'}")
    print(f"  Harvester installed: {'YES' if status['harvester'] else 'NO'}")
    print(f"  CTI file found:      {status['cti_file'] or 'NOT FOUND'}")
    print(f"  SDK directory:       {status['sdk_path'] or 'NOT FOUND'}")

    if not any([status["pytelicam"], status["native_dll"], status["cti_file"]]):
        print("\n  *** TeliCamSDK NOT INSTALLED ***")
        print("  Install from: https://www.toshiba-teli.co.jp/en/products/industrial-camera/software-telicamsdk.htm")
        print("=" * 60)
        return

    # Try to connect
    print("\n  Attempting camera connection...")
    try:
        cam = TeliCamera()
        cam.connect()
        print(f"  Backend:    {cam._backend_name}")
        print(f"  Camera:     {cam.model}")
        print(f"  Serial:     {cam.serial}")
        print(f"  Resolution: {cam.width} x {cam.height}")

        print("\n  Capturing test image...")
        img = cam.capture()
        print(f"  Image: {img.shape}, dtype: {img.dtype}")
        print(f"  Pixel range: {img.min()} - {img.max()}, mean: {img.mean():.1f}")

        cam.disconnect()
        print("\n  Camera test PASSED!")
    except Exception as e:
        print(f"  Connection failed: {e}")

    # Check USB devices
    print("\n  Checking USB devices...")
    try:
        import subprocess
        result = subprocess.run(
            ["powershell", "-Command",
             "Get-PnpDevice | Where-Object { $_.FriendlyName -match 'Teli|BU505|USB3 Vision' } "
             "| Select-Object FriendlyName,Status | Format-List"],
            capture_output=True, text=True, timeout=10,
        )
        if result.stdout.strip():
            print(result.stdout)
        else:
            print("  No Teli USB devices detected")
    except Exception:
        print("  Could not check USB devices")

    print("=" * 60)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        cam = TeliCamera(backend="simulate")
        cam.connect()
        img = cam.capture()
        print(f"Simulated capture: {img.shape}, dtype: {img.dtype}")
        cam.save("test_teli.tif", img)
        print("Saved test_teli.tif")
        cam.disconnect()
    else:
        diagnose()
