"""Camera color diagnostic — tries each camera index and backend."""
import time
import numpy as np
import cv2

# Try camera index 1 first (scan_gui default), then 0
for idx in [1, 0]:
    print(f"\n{'='*50}")
    print(f"Trying camera index {idx}...")
    print('='*50)
    try:
        from teli_camera import TeliCamera
        c = TeliCamera(camera_index=idx)
        c.connect()
        print(f"Backend:  {c._backend_name}")
        print(f"Pattern:  {c._bayer_pattern}")
        print(f"BitDepth: {c._bit_depth}")
        print(f"Model:    {c.model}")
        print(f"Size:     {c.width}x{c.height}")

        time.sleep(0.5)
        print("\nCapturing frame...")
        img = c.capture()
        print(f"Frame: shape={img.shape}, dtype={img.dtype}")

        if img.ndim == 3 and img.shape[2] == 3:
            r = img[:,:,0].astype(float).mean()
            g = img[:,:,1].astype(float).mean()
            b = img[:,:,2].astype(float).mean()
            print(f"\nChannel means:  R={r:.0f}  G={g:.0f}  B={b:.0f}")
            print(f"G/R={g/max(r,1):.2f}  G/B={g/max(b,1):.2f}")

            # Save the capture
            img8 = (img >> 8).astype(np.uint8) if img.dtype == np.uint16 else img
            fname = f"test_cam{idx}.png"
            cv2.imwrite(fname, cv2.cvtColor(img8, cv2.COLOR_RGB2BGR))
            print(f"Saved: {fname}")

        c.disconnect()
        print("OK — disconnected cleanly.")
    except Exception as e:
        print(f"FAILED: {e}")
        try:
            c.disconnect()
        except Exception:
            pass

input("\nPress Enter to close...")
