"""
Verify the stage-loss fix against real hardware.

Tests:
  1. Connect + version (with new retry/resync logic)
  2. Rapid WHERE polling x40 - all reads valid and identical
  3. Torn-read storm: 10 short-timeout WHEREs, then confirm position reads
     are correct (old code could return x=103 from a torn ':A 103')
  4. GUI-style concurrency: background position poll thread + foreground
     status/speed commands sharing a lock, 15 seconds
  5. Small physical move (+1000,+1000 steps = ~400 um) and back, verifying
     position integrity after motion  [only if RUN_MOVE = True]
"""
import threading
import time

from mac2000_driver import MAC2000, CommunicationError

RUN_MOVE = True
PORT = "COM3"

failures = []

stage = MAC2000(PORT)

print("=== TEST 1: connect ===")
ver = stage.connect()
print(f"  version: {ver!r}")
if "3.900" not in ver:
    failures.append("T1: version read failed")

ref = stage.get_position()
print(f"  reference position: ({ref.x},{ref.y})")

print("=== TEST 2: rapid WHERE x40 ===")
bad = 0
for i in range(40):
    try:
        p = stage.get_position()
        if (p.x, p.y) != (ref.x, ref.y):
            bad += 1
            print(f"  [{i}] WRONG: ({p.x},{p.y})")
    except Exception as e:
        bad += 1
        print(f"  [{i}] ERROR: {e}")
print(f"  bad reads: {bad}/40")
if bad:
    failures.append(f"T2: {bad} bad reads")

print("=== TEST 3: torn-read storm ===")
for i in range(10):
    try:
        stage.send_command("WHERE X Y", timeout=0.001)
    except CommunicationError:
        pass  # expected timeout; resync should recover
bad = 0
for i in range(10):
    try:
        p = stage.get_position()
        ok = (p.x, p.y) == (ref.x, ref.y)
        if not ok:
            bad += 1
        print(f"  read {i}: ({p.x},{p.y}) {'OK' if ok else 'WRONG'}")
    except Exception as e:
        bad += 1
        print(f"  read {i}: ERROR {e}")
if bad:
    failures.append(f"T3: {bad} bad reads after torn storm")

print("=== TEST 4: GUI-style concurrent polling (15s) ===")
lock = threading.Lock()
stop = threading.Event()
poll_errors = []
poll_count = [0]

def _poll():
    while not stop.is_set():
        if lock.acquire(blocking=False):
            try:
                p = stage.get_position()
                poll_count[0] += 1
                if (p.x, p.y) != (ref.x, ref.y):
                    poll_errors.append(f"wrong pos ({p.x},{p.y})")
            except Exception as e:
                poll_errors.append(str(e))
            finally:
                lock.release()
        time.sleep(0.5)

t = threading.Thread(target=_poll, daemon=True)
t.start()
fg_errors = []
end = time.time() + 15
while time.time() < end:
    with lock:
        try:
            stage.get_status()
            stage.get_speed()
        except Exception as e:
            fg_errors.append(str(e))
    time.sleep(0.3)
stop.set()
t.join(timeout=3)
print(f"  polls: {poll_count[0]}, poll errors: {poll_errors}, "
      f"fg errors: {fg_errors}")
if poll_errors or fg_errors:
    failures.append(f"T4: poll={poll_errors} fg={fg_errors}")

if RUN_MOVE:
    # Tolerance: 50 steps = ~20 um at 2.5 steps/um (stepper settling
    # deadband is a few um; garbled reads would be off by orders of
    # magnitude, which is what we're actually testing for)
    TOL = 50
    print("=== TEST 5: move/return drift check (5 cycles, "
          "+1000,+1000 steps ~ 400um) ===")
    for cycle in range(5):
        stage.move_relative(1000, 1000)
        stage.wait_until_idle(timeout=30)
        time.sleep(0.5)
        p1 = stage.get_position()
        stage.move_absolute(ref.x, ref.y)
        stage.wait_until_idle(timeout=30)
        time.sleep(0.5)
        p2 = stage.get_position()
        d1 = (p1.x - (ref.x + 1000), p1.y - (ref.y + 1000))
        d2 = (p2.x - ref.x, p2.y - ref.y)
        print(f"  cycle {cycle}: out-err={d1} home-err={d2}")
        if abs(d1[0]) > TOL or abs(d1[1]) > TOL:
            failures.append(f"T5 cycle {cycle}: move target off by {d1}")
        if abs(d2[0]) > TOL or abs(d2[1]) > TOL:
            failures.append(f"T5 cycle {cycle}: return off by {d2}")

stage.disconnect()

print()
if failures:
    print("FAILURES:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL TESTS PASSED")
