"""Test all connected Teli cameras to find which one streams."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from teli_camera import TeliCamera

for idx in range(3):
    print(f"\n===== Camera {idx} =====")
    try:
        cam = TeliCamera(camera_index=idx)
        cam.connect()
        print(f"  Backend: {cam._backend_name}")
        print(f"  Model: {cam.model}")
        print(f"  Serial: {cam.serial}")
        print(f"  Simulated: {cam._simulate}")
        if cam._connected and not cam._simulate:
            img = cam.capture()
            print(f"  Capture: {img.shape}, range {img.min()}-{img.max()}")
        cam.disconnect()
    except Exception as e:
        print(f"  FAILED: {e}")

print("\nDone! Use the camera index that says 'Stream opened' in the GUI Cam# dropdown.")
