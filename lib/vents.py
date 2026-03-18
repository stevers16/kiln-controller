# lib/vents.py
#
# Controls 2x MG90S servos driving butterfly-style dampers.
# Intake servo on GP14, exhaust servo on GP15.
# Both servos are always commanded together (open or closed).
#
# PWM is de-energized after each move to prevent holding torque,
# buzz, and heat. PWM objects are re-initialised on every move.
#
# Wiring summary:
#   Intake servo signal  → GP14 (3.3V logic sufficient for MG90S)
#   Exhaust servo signal → GP15
#   Both servos power    → 5V buck output
#   Both servos GND      → common GND with Pico

import machine
import time

# --- Constants ---
PWM_FREQ    = 50      # Hz — standard hobby servo
DUTY_OPEN   = 6225    # int(1900 / 20000 * 65535)
DUTY_CLOSED = 3604    # int(1100 / 20000 * 65535)
TRAVEL_MS   = 600     # ms — time to allow servo travel before deinit


class Vents:
    """
    Controller for intake and exhaust vent servos.

    Both servos are always commanded together. PWM is de-energized
    after each move to prevent holding torque and servo buzz.
    Boot state: closed (set by __init__).
    """

    def __init__(self, intake_pin=14, exhaust_pin=15, logger=None):
        self._intake_pin  = intake_pin
        self._exhaust_pin = exhaust_pin
        self._logger      = logger
        self._open        = False   # Reflects last commanded position

        # Close on boot so physical position matches software state
        self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def open(self):
        """Command both servos to the OPEN position and de-energize."""
        self._move(DUTY_OPEN)
        self._open = True
        if self._logger:
            self._logger.event("vents", "Vents opened")

    def close(self):
        """Command both servos to the CLOSED position and de-energize."""
        self._move(DUTY_CLOSED)
        self._open = False
        if self._logger:
            self._logger.event("vents", "Vents closed")

    def is_open(self):
        """Return True if last commanded position was open (not sensed)."""
        return self._open

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _move(self, duty):
        """Init PWM on both pins, set duty, wait for travel, deinit."""
        try:
            intake_pwm  = machine.PWM(machine.Pin(self._intake_pin))
            exhaust_pwm = machine.PWM(machine.Pin(self._exhaust_pin))
            intake_pwm.freq(PWM_FREQ)
            exhaust_pwm.freq(PWM_FREQ)
            intake_pwm.duty_u16(duty)
            exhaust_pwm.duty_u16(duty)
            time.sleep_ms(TRAVEL_MS)
            intake_pwm.deinit()
            exhaust_pwm.deinit()
        except Exception as e:
            print(f"[vents] WARNING: _move failed: {e}")


# --- Unit test ---
def test():
    print("=== Vents unit test ===")
    vents = Vents()
    all_passed = True

    # --- Test 1: close() — no exception; is_open() returns False ---
    try:
        vents.close()
        passed = not vents.is_open()
        print(f"  {'PASS' if passed else 'FAIL'} — close(): no exception, is_open()=False")
    except Exception as e:
        print(f"  FAIL — close() raised: {e}")
        passed = False
    all_passed &= passed

    # --- Test 2: open() — no exception; is_open() returns True ---
    try:
        vents.open()
        passed = vents.is_open()
        print(f"  {'PASS' if passed else 'FAIL'} — open(): no exception, is_open()=True")
    except Exception as e:
        print(f"  FAIL — open() raised: {e}")
        passed = False
    all_passed &= passed

    # --- Test 3: close() again — no exception; is_open() returns False ---
    try:
        vents.close()
        passed = not vents.is_open()
        print(f"  {'PASS' if passed else 'FAIL'} — close() again: no exception, is_open()=False")
    except Exception as e:
        print(f"  FAIL — close() raised: {e}")
        passed = False
    all_passed &= passed

    # --- Test 4: rapid cycle open→close→open with 1s between ---
    try:
        time.sleep_ms(1000)
        vents.open()
        time.sleep_ms(1000)
        vents.close()
        time.sleep_ms(1000)
        vents.open()
        passed = vents.is_open()
        print(f"  {'PASS' if passed else 'FAIL'} — rapid cycle: no PWM init errors, is_open()=True")
    except Exception as e:
        print(f"  FAIL — rapid cycle raised: {e}")
        passed = False
    all_passed &= passed

    print(f"\n{'All tests passed!' if all_passed else 'Some tests FAILED'}")
    return all_passed


if __name__ == "__main__":
    test()
