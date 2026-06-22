"""
MAC 2000 Serial Diagnostics
============================
Tests the USB-Serial adapter itself, then attempts MAC 2000 communication
with all possible settings including hardware flow control variants.

Also includes a loopback test to verify the USB-Serial adapter is working.
"""

import serial
import serial.tools.list_ports
import time
import sys


COM_PORT = "COM3"


def port_info():
    """Print detailed info about the COM port."""
    print("=" * 60)
    print("PORT INFORMATION")
    print("=" * 60)
    for port in serial.tools.list_ports.comports():
        if port.device == COM_PORT:
            print(f"  Device:       {port.device}")
            print(f"  Description:  {port.description}")
            print(f"  HWID:         {port.hwid}")
            print(f"  VID:PID:      {port.vid}:{port.pid}")
            print(f"  Serial #:     {port.serial_number}")
            print(f"  Manufacturer: {port.manufacturer}")
            print(f"  Product:      {port.product}")
            print(f"  Interface:    {port.interface}")
            return True
    print(f"  ERROR: {COM_PORT} not found!")
    return False


def loopback_test():
    """
    Loopback test: short TX to RX on the DB-9 connector (pins 2 and 3).
    If you jumper these pins, whatever we send should come back.
    This verifies the USB-Serial adapter hardware works.
    """
    print("\n" + "=" * 60)
    print("LOOPBACK TEST")
    print("=" * 60)
    print("  To run this test: jumper pins 2 and 3 on the DB-9 connector")
    print("  (or short TX to RX with a paperclip on the adapter end)")
    print("  Press Enter when ready, or 's' to skip: ", end="", flush=True)

    choice = input().strip().lower()
    if choice == 's':
        print("  Skipped.")
        return None

    try:
        ser = serial.Serial(COM_PORT, 9600, timeout=1)
        test_msg = b"HELLO_MAC2000\r"
        ser.write(test_msg)
        time.sleep(0.3)
        response = ser.read(ser.in_waiting or 100)
        ser.close()

        if response == test_msg:
            print(f"  PASS: Loopback received: {response!r}")
            return True
        elif response:
            print(f"  PARTIAL: Got {response!r} (expected {test_msg!r})")
            return True
        else:
            print("  FAIL: No loopback data received.")
            print("  -> Check that pins 2 and 3 are jumpered correctly.")
            return False
    except Exception as e:
        print(f"  ERROR: {e}")
        return False


def check_modem_signals():
    """Check the RS-232 modem control signals."""
    print("\n" + "=" * 60)
    print("MODEM SIGNAL CHECK")
    print("=" * 60)
    try:
        ser = serial.Serial(COM_PORT, 9600, timeout=1)
        time.sleep(0.3)

        print(f"  CTS (Clear to Send):   {ser.cts}")
        print(f"  DSR (Data Set Ready):  {ser.dsr}")
        print(f"  RI  (Ring Indicator):  {ser.ri}")
        print(f"  CD  (Carrier Detect):  {ser.cd}")

        # Try toggling DTR and RTS
        ser.dtr = True
        ser.rts = True
        time.sleep(0.2)
        print(f"\n  After asserting DTR+RTS:")
        print(f"  CTS: {ser.cts}  DSR: {ser.dsr}  CD: {ser.cd}")

        ser.close()

        if ser.cts or ser.dsr:
            print("\n  -> Signals detected! Something is connected.")
        else:
            print("\n  -> No signals detected from remote device.")
            print("     This could mean:")
            print("     a) Cable is not connected properly")
            print("     b) Need null modem adapter (TX/RX swap)")
            print("     c) MAC 2000 is not powered on")
            print("     d) Wrong port on the MAC 2000 back panel")

    except Exception as e:
        print(f"  ERROR: {e}")


def exhaustive_test():
    """Try every reasonable serial config with a simple command."""
    print("\n" + "=" * 60)
    print("EXHAUSTIVE COMMUNICATION TEST")
    print("=" * 60)

    bauds = [9600, 19200, 38400, 4800, 2400, 1200]
    parities = [serial.PARITY_NONE, serial.PARITY_EVEN, serial.PARITY_ODD]
    stopbits_list = [serial.STOPBITS_TWO, serial.STOPBITS_ONE]
    flow_controls = [
        {"xonxoff": False, "rtscts": False, "dsrdtr": False, "label": "no-flow"},
        {"xonxoff": True,  "rtscts": False, "dsrdtr": False, "label": "xon/xoff"},
        {"xonxoff": False, "rtscts": True,  "dsrdtr": False, "label": "rts/cts"},
    ]

    # Simple command that should always work
    cmd = b"STATUS\r"
    total = 0
    found = False

    for baud in bauds:
        for parity in parities:
            for stop in stopbits_list:
                for flow in flow_controls:
                    total += 1
                    parity_name = {serial.PARITY_NONE: "N", serial.PARITY_EVEN: "E", serial.PARITY_ODD: "O"}[parity]
                    config_label = f"{baud}/8{parity_name}{int(stop)}/{flow['label']}"

                    try:
                        ser = serial.Serial(
                            port=COM_PORT,
                            baudrate=baud,
                            bytesize=8,
                            parity=parity,
                            stopbits=stop,
                            xonxoff=flow["xonxoff"],
                            rtscts=flow["rtscts"],
                            dsrdtr=flow["dsrdtr"],
                            timeout=1,
                            write_timeout=1,
                        )
                        ser.reset_input_buffer()
                        ser.write(cmd)
                        time.sleep(0.5)

                        resp = b""
                        if ser.in_waiting:
                            resp = ser.read(ser.in_waiting)

                        ser.close()

                        if resp:
                            print(f"  [{total:3d}] {config_label:30s} -> RESPONSE: {resp!r}")
                            print(f"       HEX: {resp.hex(' ')}")
                            found = True
                        # Only print non-responses for common configs
                        elif baud in [9600, 19200] and parity == serial.PARITY_NONE:
                            print(f"  [{total:3d}] {config_label:30s} -> <no response>")

                    except Exception as e:
                        pass  # Skip errors silently for exhaustive test

    print(f"\n  Tested {total} configurations.")
    if not found:
        print("  NO RESPONSES on any configuration.")
        print("\n  MOST LIKELY CAUSE: Null modem cable needed!")
        print("  The MAC 2000 RS-232 port is DCE/DTE and needs TX/RX crossover.")


def main():
    print("Ludl MAC 2000 - Serial Diagnostics")
    print("=" * 60)

    if not port_info():
        sys.exit(1)

    check_modem_signals()
    loopback_test()
    exhaustive_test()

    print("\n" + "=" * 60)
    print("NEXT STEPS")
    print("=" * 60)
    print("""
  If no responses were received:

  1. GET A NULL MODEM ADAPTER (most likely fix)
     - The Micro-Manager docs explicitly state:
       "Use a null modem type serial cable"
     - A null modem adapter swaps pins 2 (TX) and 3 (RX)
     - Available at any electronics store or Amazon
     - Search: "DB-9 null modem adapter" or "DB-25 null modem"

  2. CHECK THE CABLE PATH
     - Your USB-Serial adapter (FTDI) -> DB-9 end
     - DB-9 to DB-25 adapter (if needed for MAC 2000)
     - Connected to "RS 232 TERMINAL" port on MAC 2000 back

  3. CHECK MAC 2000 DISPLAY
     - "HELP-00" may indicate a startup state or error
     - Consult manual Section 3 for display codes

  4. AFTER null modem adapter is installed, re-run:
     python test_mac2000_serial.py
""")


if __name__ == "__main__":
    main()
