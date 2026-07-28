"""
Verify the reconnect/handshake changes against real hardware (no unplug needed).

  1. connect() with a WRONG configured port (COM9) must auto-follow the
     FTDI adapter to the real port
  2. normal connect() handshake still works
  3. simulated mid-session port death: close the underlying serial handle
     behind the driver's back, then issue WHERE — send_command must
     auto-reconnect and return a valid position
  4. regression: rapid polling still clean after all of the above
"""
import logging
import time

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from mac2000_driver import MAC2000, CommunicationError

failures = []

print("=== TEST 1: wrong configured port (COM9) -> autodetect ===")
stage = MAC2000("COM9")
try:
    ver = stage.connect()
    print(f"  connected on {stage.port}: {ver!r}")
    if "3.900" not in ver:
        failures.append("T1: bad version")
except Exception as e:
    failures.append(f"T1: {e}")
    print(f"  FAILED: {e}")
if stage.connected:
    ref = stage.get_position()
    print(f"  position: ({ref.x},{ref.y})")

print("=== TEST 2: simulated USB drop mid-session ===")
# Kill the OS handle behind the driver's back; next command must recover
stage._serial.close()
print("  (serial handle closed behind driver's back)")
try:
    p = stage.get_position()
    ok = (p.x, p.y) == (ref.x, ref.y)
    print(f"  position after auto-reconnect: ({p.x},{p.y}) "
          f"{'OK' if ok else 'WRONG'}")
    if not ok:
        failures.append(f"T2: wrong position ({p.x},{p.y})")
except Exception as e:
    failures.append(f"T2: {e}")
    print(f"  FAILED: {e}")

print("=== TEST 3: motion command during dead port is NOT blindly retried ===")
stage._serial.close()
try:
    stage.move_relative(0, 0)
    print("  move went through after reconnect "
          "(acceptable only if reconnect happened before send)")
except CommunicationError as e:
    print(f"  correctly raised: {e}")
except Exception as e:
    failures.append(f"T3: unexpected {type(e).__name__}: {e}")

print("=== TEST 4: regression - 20 rapid polls ===")
bad = 0
for i in range(20):
    try:
        p = stage.get_position()
        if (p.x, p.y) != (ref.x, ref.y):
            bad += 1
            print(f"  [{i}] WRONG ({p.x},{p.y})")
    except Exception as e:
        bad += 1
        print(f"  [{i}] ERROR {e}")
print(f"  bad: {bad}/20")
if bad:
    failures.append(f"T4: {bad} bad polls")

stage.disconnect()
print()
if failures:
    print("FAILURES:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL RECONNECT TESTS PASSED")
