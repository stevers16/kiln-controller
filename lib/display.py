# lib/display.py
import machine
import time

# --- Constants ---
UART_ID = 1
UART_TX = 4  # GP4 -> display RX
UART_RX = 5  # GP5 <- display TX
BAUD_RATE = 115200
WAIT_MS = 500  # default response timeout


# Color constants (0-63; see datasheet colour table)
class Color:
    BLACK = 0
    RED = 1
    GREEN = 2
    BLUE = 3
    YELLOW = 4
    CYAN = 5
    MAGENTA = 6
    GRAY = 7
    DARK_GRAY = 8
    DARK_RED = 9
    DARK_GREEN = 10
    DARK_BLUE = 11
    DARK_YELLOW = 12
    DARK_CYAN = 13
    DARK_MAGENTA = 14
    WHITE = 15
    PURPLE = 49
    ORANGE = 56
    OLIVE = 58


# Orientation constants for DIR() command
class Orientation:
    PORTRAIT = 0  # default
    LANDSCAPE = 1  # 90 deg CCW from portrait
    PORTRAIT_UPSIDE = 2
    LANDSCAPE_UPSIDE = 3


# Font sizes supported by DC*/DCV* commands
VALID_SIZES = (16, 24, 32, 48, 72)

# Physical display dimensions in landscape mode
DISPLAY_W = 480
DISPLAY_H = 320


class Display:
    """
    Driver for JC035-HVGA-ST-02-V02 3.5" UART serial display.
    Connected to Pico via UART1: GP4 (TX->RX), GP5 (RX<-TX).
    Display VCC must be powered from VBUS (5V), not 3.3V.

    All commands are ASCII strings terminated with \\r\\n.
    The display replies OK\\r\\n after each command executes.
    A mandatory 1-second wait after power-on is required before
    sending any commands (enforced in __init__).
    """

    def __init__(
        self, uart_id=UART_ID, tx_pin=UART_TX, rx_pin=UART_RX, baudrate=BAUD_RATE
    ):
        self._uart = machine.UART(
            uart_id,
            baudrate=baudrate,
            tx=machine.Pin(tx_pin),
            rx=machine.Pin(rx_pin),
        )
        # Datasheet: wait 1 s after power-on before sending commands
        time.sleep(1)

        # Display geometry (landscape)
        self._w = DISPLAY_W
        self._h = DISPLAY_H

        # Text-scrolling state
        self._char_h = 24
        self._line_space = 4
        self._char_w = self._char_h // 2
        self._n_lines = self._h // (self._char_h + self._line_space)
        self._n_chars = self._w // self._char_w
        self._line_buffer = []
        self._current_line = ""
        self._cursor = {"x": 0, "y": 0}

        self.set_orientation(Orientation.LANDSCAPE)
        self.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _send(self, cmd: str):
        """Send a command string; appends \\r\\n if absent."""
        if not cmd.endswith("\r\n"):
            cmd += "\r\n"
        self._uart.write(cmd.encode("ascii"))

    def _wait_ok(self, timeout_ms=WAIT_MS) -> str:
        """
        Poll UART until 'OK' is received or timeout expires.
        Returns the raw response string (stripped).
        Silent timeout matches original driver behaviour.
        """
        deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
        buf = b""
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            if self._uart.any():
                buf += self._uart.read(self._uart.any())
                if b"OK" in buf:
                    return buf.decode("ascii").strip()
            time.sleep_ms(5)
        return ""  # timeout

    def _cmd(self, cmd: str, timeout_ms=WAIT_MS) -> str:
        """Send command and wait for OK response."""
        self._send(cmd)
        return self._wait_ok(timeout_ms)

    @staticmethod
    def _sanitise(text: str) -> str:
        """
        Remove characters the display parser treats as delimiters.
        Hardware treats ',' and ';' as argument/command separators
        even inside quoted strings.
        """
        return text.replace(",", " ").replace(";", " ")

    # ------------------------------------------------------------------
    # Display control
    # ------------------------------------------------------------------

    def clear(self, color=Color.BLACK):
        """Clear screen to color (0-63)."""
        return self._cmd(f"CLR({color});", timeout_ms=120)

    def set_orientation(self, orientation: int):
        """Set display orientation (use Orientation constants)."""
        return self._cmd(f"DIR({orientation});", timeout_ms=50)

    def set_background_color(self, color: int):
        """Set background colour used by DCV* and DC48/72 with-bg commands."""
        return self._cmd(f"SBC({color});", timeout_ms=30)

    def set_backlight(self, brightness: int):
        """
        Set backlight brightness 0-255.
        0 = full brightness, 255 = off (counter-intuitive but per datasheet).
        """
        if not 0 <= brightness <= 255:
            raise ValueError("brightness must be 0-255")
        return self._cmd(f"BL({brightness});", timeout_ms=30)

    def get_version(self) -> str:
        """Query firmware version string from display."""
        return self._cmd("VER;", timeout_ms=100)

    # ------------------------------------------------------------------
    # Drawing primitives
    # ------------------------------------------------------------------

    def draw_pixel(self, x: int, y: int, color: int):
        return self._cmd(f"PS({x},{y},{color});", timeout_ms=30)

    def draw_line(self, x0: int, y0: int, x1: int, y1: int, color: int):
        return self._cmd(f"PL({x0},{y0},{x1},{y1},{color});", timeout_ms=40)

    def draw_rectangle(self, x0: int, y0: int, x1: int, y1: int, color: int, fill=True):
        cmd = "BOXF" if fill else "BOX"
        return self._cmd(f"{cmd}({x0},{y0},{x1},{y1},{color});", timeout_ms=50)

    def draw_circle(self, x: int, y: int, r: int, color: int, fill=True):
        cmd = "CIRF" if fill else "CIR"
        return self._cmd(f"{cmd}({x},{y},{r},{color});", timeout_ms=40)

    # ------------------------------------------------------------------
    # Text
    # ------------------------------------------------------------------

    def draw_text(
        self,
        x: int,
        y: int,
        text: str,
        color: int = Color.WHITE,
        size: int = 24,
        background_fill: bool = False,
    ):
        """
        Draw text at (x, y).
        size must be one of VALID_SIZES (16, 24, 32, 48, 72).
        background_fill uses the colour set by set_background_color().
        Note: size 72 supports ASCII only (no Chinese).
        """
        if size not in VALID_SIZES:
            raise ValueError(f"size must be one of {VALID_SIZES}")
        text = self._sanitise(text)
        if size in (48, 72):
            # DC48/DC72 use a mode flag: 0=transparent, 1=with background
            m = 1 if background_fill else 0
            cmd_name = f"DC{size}"
            return self._cmd(
                f"{cmd_name}({x},{y},'{text}',{color},{m});", timeout_ms=60
            )
        else:
            cmd_name = f"DCV{size}" if background_fill else f"DC{size}"
            return self._cmd(f"{cmd_name}({x},{y},'{text}',{color});", timeout_ms=50)

    def draw_button(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        text: str,
        style: int = 2,
        frame_color: int = Color.WHITE,
        text_color: int = Color.WHITE,
        bg_color: int = Color.BLUE,
    ):
        """
        Draw a button widget.
        style: 0=plain text, 1=pressed, 2=raised, 4=colour frame, 8=no bg
               (styles are combinable via bitwise OR, e.g. 2|8 = raised, no bg)
        """
        text = self._sanitise(text)
        return self._cmd(
            f"BTN({x},{y},{w},{h},'{text}',{style},{frame_color},"
            f"{text_color},{bg_color});",
            timeout_ms=60,
        )

    def draw_qr(
        self, x: int, y: int, text: str, size: int = 200, color: int = Color.BLACK
    ):
        """Render a QR code. Allow extra time - takes ~960 ms per datasheet."""
        text = self._sanitise(text)
        return self._cmd(f"QRCODE({x},{y},{text},{size},{color});", timeout_ms=1200)

    # ------------------------------------------------------------------
    # Scrolling text console  (retained from original driver)
    # ------------------------------------------------------------------

    def write_characters(self, text: str):
        """
        Write text to a virtual scrolling console, wrapping and
        scrolling lines automatically.
        Use '\\n' to force a line feed.
        """
        for ch in text:
            if ch == "\n":
                self._cursor = self._feed_line(self._current_line)
                self._current_line = ""
            else:
                self.draw_text(
                    self._cursor["x"], self._cursor["y"], ch, size=self._char_h
                )
                self._current_line += ch
                self._cursor["x"] += self._char_w
                if self._cursor["x"] > self._w - self._char_w:
                    self._cursor = self._feed_line(self._current_line)
                    self._current_line = ""

    def _feed_line(self, line: str) -> dict:
        self._line_buffer.append(line)
        if len(self._line_buffer) > self._n_lines - 1:
            self._line_buffer.pop(0)
            self.clear()
            for i, ln in enumerate(self._line_buffer):
                self.draw_text(0, i * (self._char_h + self._line_space), ln)
        return {
            "x": 0,
            "y": len(self._line_buffer) * (self._char_h + self._line_space),
        }


# --- Unit test ---
def test():
    """
    Exercise all drawing primitives and verify display responds with OK.
    Visual inspection required - no automated pass/fail for rendering.
    UART response pass/fail is checked automatically.
    """
    print("=== Display unit test ===")
    display = Display()
    all_passed = True

    def check(label, result):
        nonlocal all_passed
        passed = "OK" in result if result else False
        status = "PASS" if passed else "FAIL"
        print(f"  {status} - {label}")
        if not passed:
            all_passed = False

    # Firmware version query
    ver = display.get_version()
    check(f"get_version -> '{ver}'", ver if ver else "")

    # Screen clear
    check("clear(BLACK)", display.clear(Color.BLACK))

    # Primitives
    check("draw_pixel", display.draw_pixel(10, 10, Color.WHITE))
    check("draw_line", display.draw_line(0, 50, 200, 50, Color.GREEN))
    check(
        "draw_rectangle unfilled",
        display.draw_rectangle(10, 60, 110, 110, Color.YELLOW, fill=False),
    )
    check(
        "draw_rectangle filled",
        display.draw_rectangle(120, 60, 220, 110, Color.BLUE, fill=True),
    )
    check(
        "draw_circle unfilled", display.draw_circle(260, 85, 30, Color.CYAN, fill=False)
    )
    check("draw_circle filled", display.draw_circle(330, 85, 30, Color.RED, fill=True))

    # Text at each valid size
    y = 130
    for sz in (16, 24, 32, 48):
        check(
            f"draw_text size={sz}",
            display.draw_text(0, y, f"Size {sz}", Color.WHITE, size=sz),
        )
        y += sz + 4

    # Text with background fill
    display.set_background_color(Color.DARK_BLUE)
    check(
        "draw_text bg_fill",
        display.draw_text(0, y, "BG fill", Color.WHITE, size=24, background_fill=True),
    )

    # Button widget
    check(
        "draw_button",
        display.draw_button(
            10,
            290,
            120,
            28,
            "START",
            style=2,
            frame_color=Color.GREEN,
            text_color=Color.WHITE,
            bg_color=Color.DARK_GREEN,
        ),
    )

    # Backlight cycle
    for level in (100, 50, 0):
        check(f"set_backlight({level})", display.set_backlight(level))

    # Scrolling console
    display.clear()
    print("  INFO - scrolling console (visual check only)")
    for i in range(12):
        display.write_characters(f"Line {i}: kiln temp OK\n")

    print(f"\n{'All tests passed!' if all_passed else 'Some tests FAILED'}")
    return all_passed


if __name__ == "__main__":
    test()
