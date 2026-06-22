"""
MAC 2000 Serial Communication Test Script
==========================================
Tests RS-232 communication with the Ludl MAC 2000 stage controller.

The MAC 2000 typically uses:
  - 9600 baud (default)
  - 8 data bits, No parity, 2 stop bits (8N2)
  - Commands terminated with \r (carriage return)
  - Responses terminated with \r\n

Usage: python test_mac2000_serial.py
"""

import serial
import time
import sys


COM_PORT = "COM3"

# MAC 2000 serial settings to try (in order of likelihood)
SERIAL_CONFIGS = [
    {"baudrate": 9600,  "bytesize": 8, "parity": "N", "stopbits": 2, "label": "9600/8N2"},
    {"baudrate": 9600,  "bytesize": 8, "parity": "N", "stopbits": 1, "label": "9600/8N1"},
    {"baudrate": 19200, "bytesize": 8, "parity": "N", "stopbits": 2, "label": "19200/8N2"},
    {"baudrate": 19200, "bytesize": 8, "parity": "N", "stopbits": 1, "label": "19200/8N1"},
]

# MAC 2000 test commands
TEST_COMMANDS = [
    ("STATUS",    "Get controller status"),
    ("WHERE X Y", "Get current X,Y position"),
    ("Ver",       "Get firmware version"),
    ("RDSTAT",    "Read status register"),
]


def try_command(ser, command, timeout=2.0):
    """Send a command and read the response."""
    # Flush buffers
    ser.reset_input_buffer()
    ser.reset_output_buffer()

    # Send command with CR terminator
    cmd_bytes = (command + "\r").encode("ascii")
    ser.write(cmd_bytes)
    print(f"  TX: {command!r}")

    # Wait and read response
    time.sleep(0.5)
    response = b""
    end_time = time.time() + timeout
    while time.time() < end_time:
        if ser.in_waiting > 0:
            chunk = ser.read(ser.in_waiting)
            response += chunk
            time.sleep(0.1)
        else:
            if response:
                break
            time.sleep(0.1)

    if response:
        # Try to decode, show hex if not printable
        try:
            decoded = response.decode("ascii", errors="replace").strip()
            print(f"  RX: {decoded!r}")
            print(f"  RX (hex): {response.hex(' ')}")
        except Exception:
            print(f"  RX (hex): {response.hex(' ')}")
        return response
    else:
        print("  RX: <no response>")
        return None


def test_serial_config(config):
    """Test a specific serial configuration."""
    label = config.pop("label", "unknown")
    print(f"\n{'='*60}")
    print(f"Testing config: {label} on {COM_PORT}")
    print(f"{'='*60}")

    try:
        ser = serial.Serial(
            port=COM_PORT,
            timeout=2,
            write_timeout=2,
            **config
        )
    except serial.SerialException as e:
        print(f"  ERROR: Could not open {COM_PORT}: {e}")
        return False

    print(f"  Port opened successfully: {ser.name}")
    time.sleep(0.5)  # Give the controller time after port open

    # Drain any startup data
    if ser.in_waiting:
        startup = ser.read(ser.in_waiting)
        print(f"  Startup data: {startup!r}")

    got_response = False
    for cmd, desc in TEST_COMMANDS:
        print(f"\n  --- {desc} ---")
        resp = try_command(ser, cmd)
        if resp:
            got_response = True

    ser.close()
    return got_response


def main():
    print("=" * 60)
    print("Ludl MAC 2000 Serial Communication Test")
    print("=" * 60)
    print(f"Target port: {COM_PORT}")

    # First check if port exists
    import serial.tools.list_ports
    ports = [p.device for p in serial.tools.list_ports.comports()]
    if COM_PORT not in ports:
        print(f"\nERROR: {COM_PORT} not found!")
        print(f"Available ports: {ports}")
        sys.exit(1)

    print(f"Port {COM_PORT} found. Starting tests...\n")

    working_config = None
    for config in SERIAL_CONFIGS:
        label = config["label"]
        config_copy = dict(config)
        if test_serial_config(config_copy):
            working_config = label
            print(f"\n>>> GOT RESPONSE with config: {label} <<<")
            break
        else:
            print(f"\n  No response with {label}, trying next...")

    print("\n" + "=" * 60)
    if working_config:
        print(f"SUCCESS: MAC 2000 responded with config: {working_config}")
        print("Serial communication is working!")
    else:
        print("WARNING: No responses received on any configuration.")
        print("\nTroubleshooting:")
        print("  1. Is the MAC 2000 powered on? (Check front panel LEDs)")
        print("  2. Is the RS-232 cable connected to the 'RS 232 TERMINAL' port?")
        print("  3. Try swapping TX/RX pins (pins 2 and 3) - may need null modem")
        print("  4. Check DIP switches on the controller for baud rate settings")
        print("  5. Try a different USB-to-Serial adapter")
    print("=" * 60)


if __name__ == "__main__":
    main()
