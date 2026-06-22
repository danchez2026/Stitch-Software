"""
MAC 2000 Interactive CLI
=========================
Interactive command-line tool for testing and controlling the MAC 2000 stage.
Supports both real hardware and simulated mode.

Usage:
    python mac2000_cli.py              # Connect to COM3 (default)
    python mac2000_cli.py --port COM4  # Specify port
    python mac2000_cli.py --simulate   # Simulated mode (no hardware)

Commands:
    pos / where     - Show current position
    move X Y        - Absolute move (steps)
    movrel dX dY    - Relative move (steps)
    speed [value]   - Get/set speed
    accel [value]   - Get/set acceleration
    home            - Home X and Y axes
    zero            - Set current position as origin
    halt / stop     - Emergency stop
    status          - Check busy/idle
    ver             - Firmware version
    joy on/off      - Enable/disable joystick
    raw <command>   - Send raw command
    help            - Show commands
    quit / exit     - Disconnect and exit
"""

import argparse
import sys
import logging
from mac2000_driver import MAC2000, MAC2000Error, CommunicationError, CommandError


def print_help():
    print("""
┌─────────────────────────────────────────────────────────┐
│  MAC 2000 Stage Controller - Interactive CLI            │
├─────────────────────────────────────────────────────────┤
│  POSITION                                               │
│    pos, where          Show current X,Y position        │
│    posz                Show current Z position          │
│                                                         │
│  MOVEMENT                                               │
│    move <x> <y>        Absolute move (motor steps)      │
│    movrel <dx> <dy>    Relative move (motor steps)      │
│    movez <z>           Absolute Z move                  │
│    moverum <dx> <dy>   Relative move (microns)*         │
│    home                Home X and Y axes                │
│    center              Move to center of travel         │
│    halt, stop          Emergency stop all motors        │
│                                                         │
│  SETTINGS                                               │
│    speed [value]       Get or set speed (pulses/sec)    │
│    accel [value]       Get or set acceleration (1-255)  │
│    zero                Set current position as (0,0)    │
│    joy on/off          Joystick enable/disable          │
│                                                         │
│  INFO                                                   │
│    status              Check busy (B) or idle (N)       │
│    ver                 Firmware version                  │
│    config              Module configuration             │
│                                                         │
│  OTHER                                                  │
│    raw <command>       Send raw ASCII command            │
│    cal <steps_per_um>  Set calibration factor            │
│    grid <r> <c> <sx> <sy>  Move to grid pos (row,col)  │
│    help                Show this help                   │
│    quit, exit          Disconnect and exit               │
│                                                         │
│  * micron commands require calibration (cal <value>)    │
└─────────────────────────────────────────────────────────┘
""")


def main():
    parser = argparse.ArgumentParser(description="MAC 2000 Interactive CLI")
    parser.add_argument("--port", default="COM3", help="COM port (default: COM3)")
    parser.add_argument("--simulate", action="store_true", help="Simulation mode")
    parser.add_argument("--baud", type=int, default=9600, help="Baud rate")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug output")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    stage = MAC2000(
        port=args.port,
        baudrate=args.baud,
        simulate=args.simulate,
    )

    mode_str = "SIMULATED" if args.simulate else args.port
    print(f"MAC 2000 CLI - Connecting to {mode_str}...")

    try:
        version = stage.connect()
        print(f"Connected! Firmware: {version}")
    except CommunicationError as e:
        print(f"Connection failed: {e}")
        if not args.simulate:
            print("Tip: Use --simulate to test without hardware")
        sys.exit(1)

    print('Type "help" for commands, "quit" to exit.\n')

    while True:
        try:
            line = input("MAC2000> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue

        parts = line.split()
        cmd = parts[0].lower()

        try:
            # ── Position ──
            if cmd in ("pos", "where", "w"):
                pos = stage.get_position()
                print(f"  Position: X={pos.x}  Y={pos.y} (steps)")
                if stage.steps_per_um > 0:
                    um = pos.to_microns(stage.steps_per_um)
                    print(f"           X={um[0]:.1f}  Y={um[1]:.1f} (um)")

            elif cmd == "posz":
                z = stage.get_z_position()
                print(f"  Z Position: {z} (steps)")

            # ── Movement ──
            elif cmd == "move":
                if len(parts) < 3:
                    print("  Usage: move <x> <y>")
                    continue
                x, y = int(parts[1]), int(parts[2])
                print(f"  Moving to X={x} Y={y}...")
                stage.move_absolute(x, y, wait=True)
                pos = stage.get_position()
                print(f"  Arrived: X={pos.x}  Y={pos.y}")

            elif cmd in ("movrel", "mr"):
                if len(parts) < 3:
                    print("  Usage: movrel <dx> <dy>")
                    continue
                dx, dy = int(parts[1]), int(parts[2])
                print(f"  Moving relative dX={dx} dY={dy}...")
                stage.move_relative(dx, dy, wait=True)
                pos = stage.get_position()
                print(f"  Position: X={pos.x}  Y={pos.y}")

            elif cmd == "moverum":
                if stage.steps_per_um <= 0:
                    print("  Set calibration first: cal <steps_per_um>")
                    continue
                if len(parts) < 3:
                    print("  Usage: moverum <dx_um> <dy_um>")
                    continue
                dx, dy = float(parts[1]), float(parts[2])
                print(f"  Moving relative {dx} x {dy} um...")
                stage.move_relative_um(dx, dy, wait=True)
                pos = stage.get_position()
                um = pos.to_microns(stage.steps_per_um)
                print(f"  Position: {um[0]:.1f} x {um[1]:.1f} um")

            elif cmd == "movez":
                if len(parts) < 2:
                    print("  Usage: movez <z>")
                    continue
                z = int(parts[1])
                print(f"  Moving Z to {z}...")
                stage.move_z(z, wait=True)
                print("  Done.")

            elif cmd == "home":
                print("  Homing X and Y axes... (this may take a while)")
                stage.home("X Y", wait=True, timeout=120)
                print("  Homing complete.")
                pos = stage.get_position()
                print(f"  Position: X={pos.x}  Y={pos.y}")

            elif cmd == "center":
                print("  Centering stage...")
                stage.center("X Y", wait=True)
                pos = stage.get_position()
                print(f"  Position: X={pos.x}  Y={pos.y}")

            elif cmd in ("halt", "stop"):
                stage.halt()
                print("  HALTED - all motors stopped.")

            # ── Settings ──
            elif cmd == "speed":
                if len(parts) >= 2:
                    val = int(parts[1])
                    stage.set_speed(val)
                    print(f"  Speed set to {val} pulses/sec")
                else:
                    sx, sy = stage.get_speed()
                    print(f"  Speed: X={sx}  Y={sy} pulses/sec")

            elif cmd == "accel":
                if len(parts) >= 2:
                    val = int(parts[1])
                    stage.set_acceleration(val)
                    print(f"  Acceleration set to {val}")
                else:
                    ax, ay = stage.get_acceleration()
                    print(f"  Acceleration: X={ax}  Y={ay}")

            elif cmd == "zero":
                stage.set_origin(0, 0)
                print("  Origin set to (0, 0)")

            elif cmd == "joy":
                if len(parts) < 2:
                    print("  Usage: joy on/off")
                    continue
                if parts[1].lower() == "on":
                    stage.enable_joystick()
                    print("  Joystick enabled")
                else:
                    stage.disable_joystick()
                    print("  Joystick disabled")

            # ── Info ──
            elif cmd == "status":
                s = stage.get_status()
                state = "BUSY (moving)" if s == "B" else "IDLE"
                print(f"  Status: {s} - {state}")

            elif cmd == "ver":
                v = stage.get_version()
                print(f"  Firmware: {v}")

            elif cmd == "config":
                c = stage.get_config()
                print(f"  Config: {c}")

            # ── Calibration ──
            elif cmd == "cal":
                if len(parts) < 2:
                    if stage.steps_per_um > 0:
                        print(f"  Calibration: {stage.steps_per_um} steps/um")
                    else:
                        print("  Not calibrated. Usage: cal <steps_per_um>")
                    continue
                stage.steps_per_um = float(parts[1])
                print(f"  Calibration set: {stage.steps_per_um} steps/um")

            # ── Grid ──
            elif cmd == "grid":
                if len(parts) < 5:
                    print("  Usage: grid <row> <col> <step_x> <step_y>")
                    continue
                r, c, sx, sy = int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4])
                print(f"  Moving to grid ({r},{c}) step=({sx},{sy})...")
                stage.move_to_grid_position(r, c, sx, sy, wait=True)
                pos = stage.get_position()
                print(f"  Position: X={pos.x}  Y={pos.y}")

            # ── Raw ──
            elif cmd == "raw":
                if len(parts) < 2:
                    print("  Usage: raw <command>")
                    continue
                raw_cmd = " ".join(parts[1:])
                result = stage.send_command(raw_cmd)
                print(f"  Response: {result!r}")

            # ── Help/Quit ──
            elif cmd == "help":
                print_help()

            elif cmd in ("quit", "exit", "q"):
                break

            else:
                print(f"  Unknown command: {cmd}. Type 'help' for commands.")

        except CommandError as e:
            print(f"  Controller error: {e}")
        except CommunicationError as e:
            print(f"  Communication error: {e}")
        except MAC2000Error as e:
            print(f"  Error: {e}")
        except ValueError as e:
            print(f"  Invalid value: {e}")

    stage.disconnect()
    print("Disconnected. Goodbye!")


if __name__ == "__main__":
    main()
