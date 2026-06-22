"""Explore pytelicam API to find correct method names."""
import pytelicam

system = pytelicam.get_camera_system()
n = system.get_num_of_cameras()
print(f"Cameras: {n}")

dev = system.create_device_object(0)
dev.open()

# 1. Explore GenApiWrapper methods
print("\n=== GenApiWrapper methods ===")
genapi = dev.genapi
for attr in sorted(dir(genapi)):
    if not attr.startswith('_'):
        print(f"  {attr}")

# 2. Explore CameraControl methods
print("\n=== CameraControl methods ===")
ctrl = dev.cam_control
for attr in sorted(dir(ctrl)):
    if not attr.startswith('_'):
        print(f"  {attr}")

# 3. Try to read pixel format via different approaches
print("\n=== Pixel Format ===")
try:
    val = dev.node_map.PixelFormat.value
    print(f"  node_map.PixelFormat.value = {val}")
except Exception as e:
    print(f"  node_map.PixelFormat: {e}")

try:
    val = genapi.get_enumeration_value("PixelFormat") if hasattr(genapi, 'get_enumeration_value') else "N/A"
    print(f"  genapi.get_enumeration_value = {val}")
except Exception as e:
    print(f"  genapi.get_enumeration_value: {e}")

try:
    val = genapi.get_integer_value("PixelFormat") if hasattr(genapi, 'get_integer_value') else "N/A"
    print(f"  genapi.get_integer_value = {val}")
except Exception as e:
    print(f"  genapi.get_integer_value: {e}")

# 4. Try node_map for model/serial
print("\n=== Camera Info ===")
try:
    print(f"  DeviceModelName: '{dev.node_map.DeviceModelName.value}'")
except Exception as e:
    print(f"  DeviceModelName: {e}")
try:
    print(f"  DeviceSerialNumber: '{dev.node_map.DeviceSerialNumber.value}'")
except Exception as e:
    print(f"  DeviceSerialNumber: {e}")

# 5. Check node_map attributes for PixelFormat
print("\n=== node_map.PixelFormat attributes ===")
try:
    pf = dev.node_map.PixelFormat
    for attr in sorted(dir(pf)):
        if not attr.startswith('_'):
            try:
                val = getattr(pf, attr)
                if not callable(val):
                    print(f"  {attr} = {val}")
                else:
                    print(f"  {attr}()")
            except:
                print(f"  {attr} (error reading)")
except Exception as e:
    print(f"  PixelFormat access failed: {e}")

# 6. Check pytelicam module for OutputImageType
print("\n=== OutputImageType ===")
try:
    oit = pytelicam.OutputImageType
    for attr in sorted(dir(oit)):
        if not attr.startswith('_'):
            print(f"  {attr} = {getattr(oit, attr)}")
except Exception as e:
    print(f"  OutputImageType: {e}")

# 7. Check CameraDevice properties
print("\n=== CameraDevice properties ===")
for attr in sorted(dir(dev)):
    if not attr.startswith('_'):
        print(f"  {attr}")

dev.close()
system.terminate()
print("\nDone!")
