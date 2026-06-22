"""Quick test to isolate camera stream issue."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print("Step 1: Import teli_camera only...")
from teli_camera import TeliCamera

print("Step 2: Connect camera (standalone, no GUI)...")
cam = TeliCamera()
result = cam.connect()
print(f"  Result: {result}")
print(f"  Backend: {cam._backend_name}")
print(f"  Simulated: {cam._simulate}")

if cam._connected and not cam._simulate:
    print("Step 3: Capture test frame...")
    img = cam.capture()
    print(f"  Shape: {img.shape}, range: {img.min()}-{img.max()}")

cam.disconnect()
print("Step 4: Disconnected OK")

print("\nStep 5: Now import tkinter + PIL (like GUI does)...")
import tkinter as tk
from PIL import Image, ImageTk
import numpy as np

print("Step 6: Reconnect camera after heavy imports...")
cam2 = TeliCamera()
result2 = cam2.connect()
print(f"  Result: {result2}")
print(f"  Backend: {cam2._backend_name}")
print(f"  Simulated: {cam2._simulate}")

if cam2._connected and not cam2._simulate:
    print("Step 7: Capture test frame...")
    img2 = cam2.capture()
    print(f"  Shape: {img2.shape}, range: {img2.min()}-{img2.max()}")

cam2.disconnect()
print("\nDone!")
