"""
Ludl MAC 2000 Stage Controller - Python Driver
================================================
Complete serial driver for the Ludl MAC 2000 XY stage controller.

Protocol: ASCII high-level commands over RS-232
Serial: 9600 baud, 8N2, null modem cable required
Commands terminated with CR (\\r), responses terminated with LF (\\n)
Positive reply: ":A [data]\\n"  |  Negative reply: ":N <error_code>\\n"

Usage:
    from mac2000_driver import MAC2000

    stage = MAC2000("COM3")
    stage.connect()
    print(stage.get_version())
    print(stage.get_position())
    stage.move_absolute(10000, 5000)
    stage.wait_until_idle()
    stage.disconnect()

Simulated mode (no hardware needed):
    stage = MAC2000("COM3", simulate=True)
    stage.connect()  # Works without actual serial port
"""

import serial
import time
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class MAC2000Error(Exception):
    """Base exception for MAC 2000 errors."""
    pass


class CommunicationError(MAC2000Error):
    """Serial communication failed."""
    pass


class CommandError(MAC2000Error):
    """Controller returned a negative reply (:N)."""

    ERROR_CODES = {
        -1: "Unknown/unrecognized command",
        -2: "Illegal point type, axis, or module not installed",
        -3: "Not enough parameters",
        -4: "Parameter out of range",
        -21: "Process aborted by HALT command",
    }

    def __init__(self, code: int, raw: str = ""):
        self.code = code
        self.raw = raw
        msg = self.ERROR_CODES.get(code, f"Unknown error code {code}")
        super().__init__(f"MAC 2000 error {code}: {msg} (raw: {raw!r})")


@dataclass
class StagePosition:
    """Current stage position in motor steps."""
    x: int
    y: int

    def to_microns(self, steps_per_um: float = 1.0) -> Tuple[float, float]:
        """Convert steps to microns using calibration factor."""
        return (self.x / steps_per_um, self.y / steps_per_um)

    def __repr__(self):
        return f"StagePosition(x={self.x}, y={self.y})"


class MAC2000:
    """
    Driver for the Ludl MAC 2000 motorized stage controller.

    Parameters
    ----------
    port : str
        COM port (e.g., "COM3")
    baudrate : int
        Baud rate, default 9600
    char_delay : float
        Delay between characters in seconds. MAC 2000 requires >= 0.01 (10ms).
        MAC 5000 can use 0.
    timeout : float
        Serial read timeout in seconds
    simulate : bool
        If True, simulate all commands without hardware
    steps_per_um : float
        Motor steps per micron for position conversion.
        Common values: 20 (BioPoint 2), 40 (BioPrecision).
        Set to 0 to disable conversion (use raw steps).
    """

    # Default serial settings for MAC 2000
    DEFAULT_BAUDRATE = 9600
    DEFAULT_BYTESIZE = serial.EIGHTBITS
    DEFAULT_PARITY = serial.PARITY_NONE
    DEFAULT_STOPBITS = serial.STOPBITS_TWO

    # Timing
    DEFAULT_CHAR_DELAY = 0.01   # 10ms between chars (MAC 2000 requirement)
    DEFAULT_TIMEOUT = 2.0       # 2 second read timeout
    POST_OPEN_DELAY = 0.5       # Wait after opening port
    STATUS_POLL_INTERVAL = 0.1  # 100ms between status polls

    def __init__(
        self,
        port: str,
        baudrate: int = DEFAULT_BAUDRATE,
        char_delay: float = DEFAULT_CHAR_DELAY,
        timeout: float = DEFAULT_TIMEOUT,
        simulate: bool = False,
        steps_per_um: float = 0,
    ):
        self.port = port
        self.baudrate = baudrate
        self.char_delay = char_delay
        self.timeout = timeout
        self.simulate = simulate
        self.steps_per_um = steps_per_um

        self._serial: Optional[serial.Serial] = None
        self._connected = False

        # Simulation state
        self._sim_pos = StagePosition(0, 0)
        self._sim_speed = (10000, 10000)
        self._sim_accel = (50, 50)
        self._sim_busy = False

    # ─── Connection Management ────────────────────────────────────────

    def connect(self) -> str:
        """
        Open serial connection to the MAC 2000.
        Returns the firmware version string if successful.
        """
        if self.simulate:
            self._connected = True
            logger.info("MAC 2000 connected (SIMULATED)")
            return "MAC2000 SIMULATED"

        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=self.DEFAULT_BYTESIZE,
                parity=self.DEFAULT_PARITY,
                stopbits=self.DEFAULT_STOPBITS,
                timeout=self.timeout,
                write_timeout=self.timeout,
            )
            time.sleep(self.POST_OPEN_DELAY)

            # Drain any startup data
            if self._serial.in_waiting:
                self._serial.read(self._serial.in_waiting)

            self._connected = True
            logger.info(f"MAC 2000 connected on {self.port}")

            # Try to get version to confirm communication
            try:
                version = self.get_version()
                logger.info(f"Firmware: {version}")
                return version
            except Exception:
                logger.warning("Connected but could not read version")
                return "Connected (version unknown)"

        except serial.SerialException as e:
            raise CommunicationError(f"Failed to open {self.port}: {e}")

    def disconnect(self):
        """Close serial connection."""
        if self._serial and self._serial.is_open:
            self._serial.close()
        self._connected = False
        logger.info("MAC 2000 disconnected")

    @property
    def connected(self) -> bool:
        return self._connected

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()

    # ─── Low-Level Communication ──────────────────────────────────────

    def _send_raw(self, data: str):
        """Send raw string with inter-character delay."""
        if not self._serial or not self._serial.is_open:
            raise CommunicationError("Port not open")

        # Drain any leftover data from previous commands
        time.sleep(0.05)
        if self._serial.in_waiting:
            self._serial.read(self._serial.in_waiting)

        if self.char_delay > 0:
            # MAC 2000 requires delay between characters
            for char in data:
                self._serial.write(char.encode("ascii"))
                time.sleep(self.char_delay)
        else:
            self._serial.write(data.encode("ascii"))

    def _read_response(self, timeout: Optional[float] = None) -> str:
        """
        Read response from controller.

        The MAC 2000 v3.900 firmware responds with:
          - STATUS: just 'B' or 'N' (no prefix)
          - Most commands: ':A [data]\\n' or ':N [error]\\n'
          - VER: multi-line text followed by ':A \\n' on last line

        We read until we see ':A' or ':N' followed by '\\n',
        collecting everything as the full response.
        """
        if not self._serial or not self._serial.is_open:
            raise CommunicationError("Port not open")

        actual_timeout = timeout or self.timeout
        end_time = time.time() + actual_timeout
        response = b""

        while time.time() < end_time:
            if self._serial.in_waiting > 0:
                chunk = self._serial.read(self._serial.in_waiting)
                response += chunk

                decoded = response.decode("ascii", errors="replace")

                # STATUS returns just B or N
                if decoded.strip() in ("B", "N"):
                    break

                # Look for ':A' or ':N' followed by newline (the final response)
                # This handles multi-line responses like VER where ':A' is on the last line
                if ":A" in decoded or ":N" in decoded:
                    # Make sure we have the full line after :A or :N
                    last_colon = max(decoded.rfind(":A"), decoded.rfind(":N"))
                    after_colon = decoded[last_colon:]
                    if "\n" in after_colon or "\r" in after_colon:
                        break
                    # Keep reading for the rest of the line
                    time.sleep(0.05)
                else:
                    time.sleep(0.05)
            else:
                if response:
                    # We have some data but no terminator yet - wait a bit more
                    time.sleep(0.05)
                else:
                    time.sleep(0.1)

        if not response:
            raise CommunicationError(
                f"No response from MAC 2000 (timeout={actual_timeout}s)"
            )

        decoded = response.decode("ascii", errors="replace").strip()
        logger.debug(f"RX raw: {decoded!r}")
        return decoded

    def send_command(self, command: str, timeout: Optional[float] = None) -> str:
        """
        Send a command and return the parsed response.

        Parameters
        ----------
        command : str
            Command string (without \\r terminator)
        timeout : float, optional
            Override default timeout for this command

        Returns
        -------
        str
            Response data (everything after ":A " if present)

        Raises
        ------
        CommandError
            If controller returns :N (negative reply)
        CommunicationError
            If no response or communication failure
        """
        if self.simulate:
            return self._simulate_command(command)

        if not self._connected:
            raise CommunicationError("Not connected")

        logger.debug(f"TX: {command!r}")
        self._send_raw(command + "\r")

        # Small delay to let the controller process
        time.sleep(0.05)

        response = self._read_response(timeout)
        return self._parse_response(response)

    def _parse_response(self, response: str) -> str:
        """Parse controller response, raise on errors."""
        # STATUS command returns just 'B' or 'N'
        if response.strip() in ("B", "N"):
            return response.strip()

        # The response may be multi-line (e.g., VER returns text then ':A')
        # Find the ':A' or ':N' line which is the actual result
        lines = response.replace("\r", "\n").split("\n")
        lines = [l.strip() for l in lines if l.strip()]

        # Collect any data before ':A'/':N' (e.g., VER text)
        preamble_parts = []
        result_line = None

        for line in lines:
            if line.startswith(":A") or line.startswith(":N"):
                result_line = line
            else:
                preamble_parts.append(line)

        # If we found a ':A' or ':N' line, parse it
        if result_line:
            if result_line.startswith(":A"):
                data = result_line[2:].strip()
                # If there's preamble (like VER text), return that instead
                if preamble_parts and not data:
                    return " ".join(preamble_parts)
                elif preamble_parts and data:
                    return " ".join(preamble_parts) + " " + data
                return data

            if result_line.startswith(":N"):
                error_part = result_line[2:].strip()
                if error_part == "BUSY":
                    raise CommandError(-99, response)
                try:
                    code = int(error_part)
                except ValueError:
                    code = -999
                raise CommandError(code, response)

        # No ':A'/':N' found - might be raw STATUS response or unknown format
        # Check if any line is just B or N
        for line in lines:
            if line in ("B", "N"):
                return line

        # Unknown format - return as-is
        logger.warning(f"Unexpected response format: {response!r}")
        return response

    # ─── Status Commands ──────────────────────────────────────────────

    def get_version(self) -> str:
        """Get firmware version string."""
        return self.send_command("VER")

    def get_config(self) -> str:
        """Get module configuration/inventory."""
        return self.send_command("Rconfig", timeout=5.0)

    def get_status(self, axis: str = "") -> str:
        """
        Check if motors are busy.

        Parameters
        ----------
        axis : str
            Optional axis to check ("X", "Y", "Z", "S").
            If empty, checks all motors.

        Returns
        -------
        str
            "B" if busy (moving), "N" if not busy (idle)
        """
        cmd = f"STATUS {axis}".strip()
        return self.send_command(cmd)

    def is_busy(self, axis: str = "") -> bool:
        """Check if stage is currently moving."""
        return self.get_status(axis) == "B"

    def wait_until_idle(self, timeout: float = 60.0, poll_interval: float = None):
        """
        Block until all motors have stopped moving.

        Parameters
        ----------
        timeout : float
            Maximum time to wait in seconds
        poll_interval : float
            Time between status polls (default: 0.1s)
        """
        if self.simulate:
            return

        interval = poll_interval or self.STATUS_POLL_INTERVAL
        start = time.time()
        while time.time() - start < timeout:
            if not self.is_busy():
                return
            time.sleep(interval)

        raise MAC2000Error(f"Timeout waiting for idle after {timeout}s")

    # ─── Position Commands ────────────────────────────────────────────

    def get_position(self) -> StagePosition:
        """
        Get current X,Y position in motor steps.

        Returns
        -------
        StagePosition
            Current position with x, y attributes
        """
        response = self.send_command("WHERE X Y")
        parts = response.split()
        if len(parts) >= 2:
            return StagePosition(x=int(parts[0]), y=int(parts[1]))
        elif len(parts) == 1:
            return StagePosition(x=int(parts[0]), y=0)
        else:
            raise CommunicationError(f"Invalid WHERE response: {response!r}")

    def get_position_um(self) -> Tuple[float, float]:
        """Get current position in microns (requires steps_per_um calibration)."""
        if self.steps_per_um <= 0:
            raise MAC2000Error("steps_per_um not calibrated. Set it first.")
        pos = self.get_position()
        return pos.to_microns(self.steps_per_um)

    def set_origin(self, x: int = 0, y: int = 0):
        """
        Redefine current position as (x, y) without moving.
        Typically used as set_origin(0, 0) to zero the stage.
        """
        self.send_command(f"HERE X={x} Y={y}")

    def get_z_position(self) -> int:
        """Get current Z (focus) position in motor steps."""
        response = self.send_command("WHERE Z")
        return int(response.strip())

    # ─── Movement Commands ────────────────────────────────────────────

    def move_absolute(self, x: int, y: int, wait: bool = False):
        """
        Move to absolute position in motor steps.

        Parameters
        ----------
        x, y : int
            Target position in motor steps
        wait : bool
            If True, block until motion completes
        """
        self.send_command(f"MOVE X={x} Y={y}")
        if wait:
            self.wait_until_idle()

    def move_relative(self, dx: int, dy: int, wait: bool = False):
        """
        Move relative to current position.

        Parameters
        ----------
        dx, dy : int
            Distance to move in motor steps (positive or negative)
        wait : bool
            If True, block until motion completes
        """
        self.send_command(f"MOVREL X={dx} Y={dy}")
        if wait:
            self.wait_until_idle()

    def move_absolute_um(self, x_um: float, y_um: float, wait: bool = False):
        """Move to absolute position in microns (requires calibration)."""
        if self.steps_per_um <= 0:
            raise MAC2000Error("steps_per_um not calibrated")
        x_steps = int(round(x_um * self.steps_per_um))
        y_steps = int(round(y_um * self.steps_per_um))
        self.move_absolute(x_steps, y_steps, wait=wait)

    def move_relative_um(self, dx_um: float, dy_um: float, wait: bool = False):
        """Move relative in microns (requires calibration)."""
        if self.steps_per_um <= 0:
            raise MAC2000Error("steps_per_um not calibrated")
        dx_steps = int(round(dx_um * self.steps_per_um))
        dy_steps = int(round(dy_um * self.steps_per_um))
        self.move_relative(dx_steps, dy_steps, wait=wait)

    def move_z(self, z: int, wait: bool = False):
        """Move Z axis (focus) to absolute position."""
        self.send_command(f"MOVE Z={z}")
        if wait:
            self.wait_until_idle()

    def move_z_relative(self, dz: int, wait: bool = False):
        """Move Z axis relative to current position."""
        self.send_command(f"MOVREL Z={dz}")
        if wait:
            self.wait_until_idle()

    def halt(self):
        """Emergency stop - immediately halt all motor movement."""
        self.send_command("HALT")

    # ─── Speed & Acceleration ─────────────────────────────────────────

    def set_speed(self, x_speed: int, y_speed: Optional[int] = None):
        """
        Set motor speed in pulses per second.
        Range: 85 to 2,764,800 pulses/sec.
        """
        if y_speed is None:
            y_speed = x_speed
        self.send_command(f"SPEED X={x_speed} Y={y_speed}")

    def get_speed(self) -> Tuple[int, int]:
        """Get current speed settings (x, y) in pulses/sec."""
        response = self.send_command("SPEED X Y")
        parts = response.split()
        return (int(parts[0]), int(parts[1]))

    def set_acceleration(self, x_accel: int, y_accel: Optional[int] = None):
        """
        Set acceleration profile (1-255).
        Higher values = faster acceleration.
        """
        if y_accel is None:
            y_accel = x_accel
        self.send_command(f"ACCEL X={x_accel} Y={y_accel}")

    def get_acceleration(self) -> Tuple[int, int]:
        """Get current acceleration settings (x, y)."""
        response = self.send_command("ACCEL X Y")
        parts = response.split()
        return (int(parts[0]), int(parts[1]))

    def set_start_speed(self, x_speed: int, y_speed: Optional[int] = None):
        """Set start speed (initial speed before acceleration ramp)."""
        if y_speed is None:
            y_speed = x_speed
        self.send_command(f"STSPEED X={x_speed} Y={y_speed}")

    # ─── Homing & Calibration ─────────────────────────────────────────

    def home(self, axes: str = "X Y", wait: bool = True, timeout: float = 120.0):
        """
        Home specified axes. Drives to limit switches/index pulses.

        Parameters
        ----------
        axes : str
            Axes to home, e.g. "X Y" or "X Y Z"
        wait : bool
            If True, block until homing completes
        timeout : float
            Maximum time to wait for homing
        """
        self.send_command(f"HOME {axes}", timeout=5.0)
        if wait:
            self.wait_until_idle(timeout=timeout)

    def center(self, axes: str = "X Y", wait: bool = True):
        """Move to center of axis range."""
        self.send_command(f"CENTER {axes}")
        if wait:
            self.wait_until_idle()

    # ─── Joystick Control ─────────────────────────────────────────────

    def enable_joystick(self, x: bool = True, y: bool = True):
        """Enable or disable joystick control."""
        x_flag = "+" if x else "-"
        y_flag = "+" if y else "-"
        self.send_command(f"JOYSTICK X{x_flag} Y{y_flag}")

    def disable_joystick(self):
        """Disable joystick on all axes."""
        self.enable_joystick(False, False)

    # ─── Utility ──────────────────────────────────────────────────────

    def reset(self):
        """
        Hard reset the controller (like power cycle).
        WARNING: Controller will be unresponsive during reset.
        """
        self._send_raw("Remres\r")
        time.sleep(5.0)  # Allow time for controller reboot

    def set_transmission_delay(self, value: int):
        """Set inter-character transmission delay on controller (1-255)."""
        self.send_command(f"TRXDEL {value}")

    def get_transmission_delay(self) -> int:
        """Get current transmission delay setting."""
        response = self.send_command("TRXDEL")
        return int(response.strip())

    # ─── Convenience Methods ──────────────────────────────────────────

    def move_to_grid_position(
        self,
        row: int,
        col: int,
        step_x: int,
        step_y: int,
        origin_x: int = 0,
        origin_y: int = 0,
        wait: bool = True,
    ):
        """
        Move to a position on a grid pattern.

        Parameters
        ----------
        row, col : int
            Grid row and column (0-indexed)
        step_x, step_y : int
            Step size between grid positions in motor steps
        origin_x, origin_y : int
            Origin offset in motor steps
        wait : bool
            If True, block until motion completes
        """
        target_x = origin_x + col * step_x
        target_y = origin_y + row * step_y
        self.move_absolute(target_x, target_y, wait=wait)

    def scan_positions(self, positions: list, settle_time: float = 0.2):
        """
        Generator that moves to each (x, y) position and yields the position.
        Use this for iterating over scan patterns.

        Parameters
        ----------
        positions : list of (int, int)
            List of (x, y) positions in motor steps
        settle_time : float
            Time to wait after reaching each position (seconds)

        Yields
        ------
        tuple
            (index, x, y) for each position after arrival
        """
        for i, (x, y) in enumerate(positions):
            self.move_absolute(x, y, wait=True)
            time.sleep(settle_time)
            yield i, x, y

    # ─── Simulation ───────────────────────────────────────────────────

    def _simulate_command(self, command: str) -> str:
        """Handle commands in simulation mode."""
        cmd_upper = command.upper().strip()

        if cmd_upper.startswith("VER"):
            return "MAC2000 SIMULATED v1.0"

        if cmd_upper.startswith("STATUS"):
            return "N"

        if cmd_upper.startswith("WHERE"):
            if "Z" in cmd_upper:
                return "0"
            return f"{self._sim_pos.x} {self._sim_pos.y}"

        if cmd_upper.startswith("MOVE ") and not cmd_upper.startswith("MOVREL"):
            # Parse absolute move
            x, y = self._sim_pos.x, self._sim_pos.y
            for part in command.split():
                if part.upper().startswith("X="):
                    x = int(part.split("=")[1])
                elif part.upper().startswith("Y="):
                    y = int(part.split("=")[1])
            self._sim_pos = StagePosition(x, y)
            logger.debug(f"SIM: Moved to {self._sim_pos}")
            return ""

        if cmd_upper.startswith("MOVREL"):
            dx, dy = 0, 0
            for part in command.split():
                if part.upper().startswith("X="):
                    dx = int(part.split("=")[1])
                elif part.upper().startswith("Y="):
                    dy = int(part.split("=")[1])
            self._sim_pos = StagePosition(
                self._sim_pos.x + dx, self._sim_pos.y + dy
            )
            logger.debug(f"SIM: Moved to {self._sim_pos}")
            return ""

        if cmd_upper.startswith("HERE"):
            x, y = 0, 0
            for part in command.split():
                if part.upper().startswith("X="):
                    x = int(part.split("=")[1])
                elif part.upper().startswith("Y="):
                    y = int(part.split("=")[1])
            self._sim_pos = StagePosition(x, y)
            return ""

        if cmd_upper.startswith("SPEED"):
            if "=" in command:
                return ""
            return f"{self._sim_speed[0]} {self._sim_speed[1]}"

        if cmd_upper.startswith("ACCEL"):
            if "=" in command:
                return ""
            return f"{self._sim_accel[0]} {self._sim_accel[1]}"

        if cmd_upper.startswith("HOME"):
            self._sim_pos = StagePosition(0, 0)
            return ""

        if cmd_upper.startswith("HALT"):
            return ""

        if cmd_upper.startswith("JOYSTICK"):
            return ""

        if cmd_upper.startswith("RCONFIG"):
            return "SIM: No modules"

        # Default: acknowledge
        return ""

    def __repr__(self):
        mode = "SIM" if self.simulate else self.port
        state = "connected" if self._connected else "disconnected"
        return f"MAC2000({mode}, {state})"
