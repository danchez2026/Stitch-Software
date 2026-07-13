"""
Verify the joystick-override fix against real hardware.

Background: a mis-centered/dirty joystick pot continuously drives an
axis (observed: X creeping ~-3 to -4 steps/s while idle) and makes the
MAC2000 silently IGNORE serial MOVE/MOVREL commands on that axis — the
controller still acknowledges them with :A. This broke Corner Check and
scan tile positioning on X while Y worked perfectly.

Fix: MAC2000.set_joystick(False) ("JOYSTICK X- Y-") before automated
move sequences, set_joystick(True) after.

Tests:
  1. Connect + version
  2. Idle drift measurement with joystick ENABLED (informational —
     nonzero drift means the joystick pot is off-center right now)
  3. With joystick DISABLED: idle drift must be ~0 and MOVREL/MOVE on
     both axes must land within tolerance
  4. Corner-check-scale absolute X move (+20000 steps ~ 8 mm) and back
  5. Joystick re-enabled at the end (always, via finally)
"""
import time

from mac2000_driver import MAC2000

PORT = "COM3"
TOL = 50  # steps (~20 um settling deadband)

failures = []

stage = MAC2000(PORT)

print("=== TEST 1: connect ===")
ver = stage.connect()
print(f"  version: {ver!r}")
if "3.900" not in ver:
    failures.append("T1: version read failed")


def drift_rate(seconds=6.0):
    p0 = stage.get_position()
    t0 = time.time()
    time.sleep(seconds)
    p1 = stage.get_position()
    dt = time.time() - t0
    return (p1.x - p0.x) / dt, (p1.y - p0.y) / dt


def wait_idle(max_s=30):
    for _ in range(int(max_s / 0.2)):
        time.sleep(0.2)
        if not stage.is_busy():
            return True
    return False


print("=== TEST 2: idle drift, joystick enabled (informational) ===")
rx, ry = drift_rate()
print(f"  drift: X {rx:+.2f} steps/s, Y {ry:+.2f} steps/s")
if abs(rx) > 0.5 or abs(ry) > 0.5:
    print("  NOTE: joystick pot is off-center — hardware needs "
          "cleaning/recentering (fix makes software immune to this)")

print("=== TEST 3+4: moves with joystick disabled ===")
stage.set_joystick(False)
try:
    rx, ry = drift_rate()
    print(f"  drift with joystick disabled: X {rx:+.2f}, Y {ry:+.2f} steps/s")
    if abs(rx) > 0.5 or abs(ry) > 0.5:
        failures.append(f"T3: drift persists with joystick disabled "
                        f"({rx:+.2f}, {ry:+.2f})")

    ref = stage.get_position()
    print(f"  reference: ({ref.x},{ref.y})")

    checks = [
        ("MOVREL X +500", lambda: stage.move_relative(500, 0),
         ref.x + 500, None),
        ("MOVE X back", lambda: stage.move_absolute(ref.x, ref.y),
         ref.x, ref.y),
        ("MOVREL Y +500", lambda: stage.move_relative(0, 500),
         None, ref.y + 500),
        ("MOVE Y back", lambda: stage.move_absolute(ref.x, ref.y),
         ref.x, ref.y),
        ("MOVE X +20000 (corner-check scale)",
         lambda: stage.move_absolute(ref.x + 20000, ref.y),
         ref.x + 20000, ref.y),
        ("MOVE back to ref", lambda: stage.move_absolute(ref.x, ref.y),
         ref.x, ref.y),
    ]
    for label, cmd, tx, ty in checks:
        cmd()
        wait_idle()
        time.sleep(0.3)
        p = stage.get_position()
        ok = ((tx is None or abs(p.x - tx) <= TOL)
              and (ty is None or abs(p.y - ty) <= TOL))
        print(f"  {label}: at ({p.x},{p.y}) {'OK' if ok else 'FAILED'}")
        if not ok:
            failures.append(f"{label}: target ({tx},{ty}) got ({p.x},{p.y})")
finally:
    stage.set_joystick(True)
    print("  joystick re-enabled")

stage.disconnect()

print()
if failures:
    print("FAILURES:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL JOYSTICK-OVERRIDE TESTS PASSED")
