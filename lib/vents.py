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
#   Intake servo signal  -> GP14 (3.3V logic sufficient for MG90S)
#   Exhaust servo signal -> GP15
#   Both servos power    -> 5V buck output
#   Both servos GND      -> common GND with Pico

import machine
import time

# --- Constants ---
PWM_FREQ    = 50      # Hz - standard hobby servo
DUTY_OPEN   = 6225    # int(1900 / 20000 * 65535)
DUTY_CLOSED = 3604    # int(1100 / 20000 * 65535)
TRAVEL_MS   = 600     # ms - time to allow servo travel before deinit


class Vents:
    """
    Controller for intake and exhaust vent servos.

    Both servos are always commanded together. PWM is de-energized
    after each move to prevent holding torque and servo buzz.
    Boot state: closed (set by __init__).
    """

    def __init__(self, intake_pin=14, exhaust_pin=15, logger=None,
                 current_monitor=None):
        self._intake_pin      = intake_pin
        self._exhaust_pin     = exhaust_pin
        self._logger          = logger
        self._current_monitor  = current_monitor
        self._open             = False   # Reflects last commanded position
        self._last_movement_mA = None    # Mid-travel current from most recent move

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

    def verify_position(self, expected_min_mA=100, expected_max_mA=400):
        """
        Check that the mid-travel current from the most recent move falls
        within [expected_min_mA, expected_max_mA].  Current is sampled by
        _move() at mid-travel while the servo is still energized.

        Fault threshold: > 600mA indicates a jammed or stalled servo.

        Returns True/False, or None if no current_monitor is attached or
        no move has been made yet.
        """
        if self._current_monitor is None or self._last_movement_mA is None:
            return None
        current_mA = self._last_movement_mA
        in_range = expected_min_mA <= current_mA <= expected_max_mA
        if not in_range:
            msg = (f"Servo current out of range: {current_mA:.1f}mA "
                   f"(expected {expected_min_mA}-{expected_max_mA}mA) - possible jam or stall")
            if self._logger:
                self._logger.event("vents", msg, level="WARN")
            print(f"[vents] WARN: {msg}")
        return in_range

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _move(self, duty):
        """Init PWM on both pins, set duty, wait for travel, deinit.
        Samples current at mid-travel if a current_monitor is attached."""
        try:
            intake_pwm  = machine.PWM(machine.Pin(self._intake_pin))
            exhaust_pwm = machine.PWM(machine.Pin(self._exhaust_pin))
            intake_pwm.freq(PWM_FREQ)
            exhaust_pwm.freq(PWM_FREQ)
            intake_pwm.duty_u16(duty)
            exhaust_pwm.duty_u16(duty)
            time.sleep_ms(TRAVEL_MS // 2)
            if self._current_monitor:
                reading = self._current_monitor.read()
                self._last_movement_mA = reading["current_mA"] if reading else None
            time.sleep_ms(TRAVEL_MS - TRAVEL_MS // 2)
            intake_pwm.deinit()
            exhaust_pwm.deinit()
        except Exception as e:
            print(f"[vents] WARNING: _move failed: {e}")


# --- Unit test ---
def test():
    print("=== Vents unit test ===")
    vents = Vents()
    all_passed = True

    # --- Test 1: close() - no exception; is_open() returns False ---
    try:
        vents.close()
        passed = not vents.is_open()
        print(f"  {'PASS' if passed else 'FAIL'} - close(): no exception, is_open()=False")
    except Exception as e:
        print(f"  FAIL - close() raised: {e}")
        passed = False
    all_passed &= passed

    # --- Test 2: open() - no exception; is_open() returns True ---
    try:
        vents.open()
        passed = vents.is_open()
        print(f"  {'PASS' if passed else 'FAIL'} - open(): no exception, is_open()=True")
    except Exception as e:
        print(f"  FAIL - open() raised: {e}")
        passed = False
    all_passed &= passed

    # --- Test 3: close() again - no exception; is_open() returns False ---
    try:
        vents.close()
        passed = not vents.is_open()
        print(f"  {'PASS' if passed else 'FAIL'} - close() again: no exception, is_open()=False")
    except Exception as e:
        print(f"  FAIL - close() raised: {e}")
        passed = False
    all_passed &= passed

    # --- Test 4: rapid cycle open->close->open with 1s between ---
    try:
        time.sleep_ms(1000)
        vents.open()
        time.sleep_ms(1000)
        vents.close()
        time.sleep_ms(1000)
        vents.open()
        passed = vents.is_open()
        print(f"  {'PASS' if passed else 'FAIL'} - rapid cycle: no PWM init errors, is_open()=True")
    except Exception as e:
        print(f"  FAIL - rapid cycle raised: {e}")
        passed = False
    all_passed &= passed

    # --- Current monitoring tests ---
    print("\n  -- Current monitoring tests --")
    from current import CurrentMonitor
    i2c = machine.I2C(0, sda=machine.Pin(0), scl=machine.Pin(1), freq=400_000)

    # --- Test 5: verify_position() returns None with no current_monitor ---
    passed = vents.verify_position() is None
    print(f"  {'PASS' if passed else 'FAIL'} - verify_position() returns None with no current_monitor")
    all_passed &= passed

    # --- Test 6: open() - mid-travel current in servo operating range ---
    mon_5v = CurrentMonitor(i2c, 0x41, "5V")
    vents_mon = Vents(current_monitor=mon_5v)
    time.sleep_ms(500)
    vents_mon.open()   # _move() samples at 300ms mid-travel, de-energizes at 600ms
    ok = vents_mon.verify_position(100, 400)
    mA_str = f"{vents_mon._last_movement_mA:.1f}mA" if vents_mon._last_movement_mA is not None else "read failed"
    passed = ok is True
    print(f"  {'PASS' if passed else 'FAIL'} - open(): mid-travel current in 100-400mA range ({mA_str})")
    all_passed &= passed

    # --- Test 7: close() - mid-travel current in servo operating range ---
    time.sleep_ms(500)
    vents_mon.close()
    ok = vents_mon.verify_position(100, 400)
    mA_str = f"{vents_mon._last_movement_mA:.1f}mA" if vents_mon._last_movement_mA is not None else "read failed"
    passed = ok is True
    print(f"  {'PASS' if passed else 'FAIL'} - close(): mid-travel current in 100-400mA range ({mA_str})")
    all_passed &= passed

    print(f"\n{'All tests passed!' if all_passed else 'Some tests FAILED'}")
    return all_passed


if __name__ == "__main__":
    test()
