# lib/circulation.py
#
# Controls 3x 120mm 4-pin PWM circulation fans wired as a group.
# All PWM wires tied to GP17; FQP30N06L MOSFET gate on GP19 switches
# the shared 12V GND side for hard on/off.
#
# No tach wire on these fans - RPM is not measurable.
# Health monitoring via hours_on counter; call tick() once per minute
# from the main loop (or scheduler) to keep the counter accurate.
#
# Wiring summary:
#   Fan pin 1 (GND/black)  -> MOSFET drain (shared); MOSFET source -> GND rail
#   Fan pin 2 (12V/yellow) -> 12V rail (direct)
#   Fan pin 3 (tach/green) -> not connected (no tach on these fans)
#   Fan pin 4 (PWM/blue)   -> GP17 via 100ohm series resistor (x3 tied together)
#   MOSFET gate (GP19)     -> 100ohm gate resistor -> GP19; 10kohm pull-down to GND
#   Flyback diodes         -> 1N4007 across each fan (cathode to 12V, anode to drain)

import machine
import time

# --- Constants ---
PWM_PIN = 17
GATE_PIN = 19
PWM_FREQ = 25000  # Hz  - standard 4-pin PC fan spec
MIN_START_PCT = 20  # Below this most fans stall; clamp on() to this floor


class CirculationFans:
    """
    Group controller for 3x 120mm 4-pin PWM circulation fans.

    All fans share a single PWM signal and a single MOSFET gate.
    No tach is available; use hours_on for health/maintenance logging.
    """

    def __init__(
        self, pwm_pin=PWM_PIN, gate_pin=GATE_PIN, logger=None, current_monitor=None
    ):
        self._logger = logger
        self._current_monitor = current_monitor
        self._gate = machine.Pin(gate_pin, machine.Pin.OUT)
        self._gate.low()  # Fans off at init

        self._pwm = machine.PWM(machine.Pin(pwm_pin))
        self._pwm.freq(PWM_FREQ)
        self._pwm.duty_u16(0)

        self._running = False
        self._speed_pct = 0
        self._minutes_on = 0  # Incremented by tick()

        # Fault contract
        self.fault = False
        self.fault_code = None
        self.fault_message = None
        self.fault_tier = "fault"
        self.fault_last_checked_ms = None
        self._good_reads = 0  # consecutive good reads for N=3 clear

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on(self, speed_percent=100):
        """
        Enable fans at speed_percent (0-100).
        Values below MIN_START_PCT are clamped up to prevent stall.
        """
        speed_percent = max(MIN_START_PCT, min(100, speed_percent))
        duty = int(speed_percent / 100 * 65535)
        self._gate.high()
        self._pwm.duty_u16(duty)
        self._running = True
        self._speed_pct = speed_percent
        if self._logger:
            self._logger.event("circulation", f"Fans on at {speed_percent}%")
        # Allow fans to spin up before verifying current draw
        time.sleep_ms(1000)
        self.verify_running(200, 500)

    def off(self):
        """Cut power to all fans via MOSFET gate; zero PWM signal."""
        self._pwm.duty_u16(0)
        self._gate.low()
        self._running = False
        self._speed_pct = 0
        # Clear fault -- no longer expecting current draw
        self.fault = False
        self.fault_code = None
        self.fault_message = None
        self._good_reads = 0
        if self._logger:
            self._logger.event("circulation", "Fans off")

    def set_speed(self, speed_percent):
        """Adjust speed while running. Ignored if fans are off."""
        if self._running:
            self.on(speed_percent)

    def verify_running(self, expected_min_mA=200, expected_max_mA=500):
        """
        Check that the 12V rail current is within the expected range for
        fans-on operation.  Logs ERROR and latches fault if out of range.

        Returns True/False, or None if no current_monitor is attached.
        """
        if self._current_monitor is None:
            return None
        ok = self._current_monitor.check_range(expected_min_mA, expected_max_mA)
        self.fault_last_checked_ms = time.ticks_ms()
        if ok is False:
            self.fault = True
            self.fault_code = "CIRC_FAN_FAULT"
            self.fault_message = "Current out of range after on() - possible fan fault"
            self._good_reads = 0
            if self._logger:
                self._logger.event(
                    "circulation",
                    "Current out of range after on() - possible fan fault",
                    level="ERROR",
                )
        elif ok is True:
            self._good_reads += 1
            if self._good_reads >= 3 and self.fault:
                self.fault = False
                self.fault_code = None
                self.fault_message = None
        return ok

    def check_health(self):
        """Periodic self-check for the fault aggregator.

        If running, re-reads the 12V rail current via verify_running().
        If stopped, returns current fault state unchanged.
        Must stay cheap -- one I2C read at most.
        """
        self.fault_last_checked_ms = time.ticks_ms()
        if self._running:
            self.verify_running(200, 500)
        return self.fault

    def read_rpm(self):
        """
        Not available - no tach wire on these fans.
        Returns None. Provided for API consistency with ExhaustFan.
        """
        return None

    def tick(self):
        """
        Call once per minute from the main loop / scheduler.
        Increments the hours_on counter while fans are running.
        """
        if self._running:
            self._minutes_on += 1

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self):
        """True if fans are currently commanded on."""
        return self._running

    @property
    def speed_pct(self):
        """Last commanded speed (0-100). 0 when off."""
        return self._speed_pct

    @property
    def hours_on(self):
        """Accumulated run time in hours (float), driven by tick() calls."""
        return self._minutes_on / 60.0


# --- Unit test ---
def test():
    print("=== CirculationFans unit test ===")
    fans = CirculationFans()
    all_passed = True

    # --- Test 1: off at init ---
    passed = not fans.is_running and fans.speed_pct == 0
    print(f"  {'PASS' if passed else 'FAIL'} - Init: not running, speed=0")
    all_passed &= passed

    # --- Test 2: on() clamps below MIN_START_PCT ---
    fans.on(10)
    passed = fans.speed_pct == MIN_START_PCT and fans.is_running
    print(
        f"  {'PASS' if passed else 'FAIL'} - on(10) clamped to {MIN_START_PCT}%, is_running=True"
    )
    all_passed &= passed

    # --- Test 3: on() at valid speeds ---
    for speed in (25, 50, 75, 100):
        fans.on(speed)
        time.sleep_ms(3000)
        passed = fans.speed_pct == speed and fans.is_running
        print(
            f"  {'PASS' if passed else 'FAIL'} - on({speed}%) -> speed_pct={fans.speed_pct}"
        )
        all_passed &= passed

    # --- Test 4: read_rpm() returns None ---
    passed = fans.read_rpm() is None
    print(f"  {'PASS' if passed else 'FAIL'} - read_rpm() returns None (no tach)")
    all_passed &= passed

    # --- Test 5: tick() increments hours_on ---
    fans.on(100)
    before = fans.hours_on
    for _ in range(60):
        fans.tick()
    passed = abs(fans.hours_on - (before + 1.0)) < 0.01
    print(
        f"  {'PASS' if passed else 'FAIL'} - 60 ticks -> +1.0h (got {fans.hours_on - before:.2f}h)"
    )
    all_passed &= passed

    # --- Test 6: tick() does NOT increment when off ---
    fans.off()
    before = fans.hours_on
    for _ in range(10):
        fans.tick()
    passed = fans.hours_on == before
    print(f"  {'PASS' if passed else 'FAIL'} - tick() while off: hours unchanged")
    all_passed &= passed

    # --- Test 7: set_speed() ignored when off ---
    fans.set_speed(80)
    passed = not fans.is_running and fans.speed_pct == 0
    print(f"  {'PASS' if passed else 'FAIL'} - set_speed() while off: no effect")
    all_passed &= passed

    # --- Test 8: off() state ---
    fans.on(75)
    fans.off()
    passed = not fans.is_running and fans.speed_pct == 0
    print(f"  {'PASS' if passed else 'FAIL'} - off(): not running, speed=0")
    all_passed &= passed

    # --- Current monitoring tests ---
    print("\n  -- Current monitoring tests --")
    from current import CurrentMonitor

    i2c = machine.I2C(0, sda=machine.Pin(0), scl=machine.Pin(1), freq=400_000)

    # --- Test 9: verify_running() returns None with no current_monitor ---
    passed = fans.verify_running() is None
    print(
        f"  {'PASS' if passed else 'FAIL'} - verify_running() returns None with no current_monitor"
    )
    all_passed &= passed

    # --- Test 10: fans on with monitor - current in expected range ---
    mon_12v = CurrentMonitor(i2c, 0x40, "12V")
    fans_mon = CirculationFans(current_monitor=mon_12v)
    fans_mon.on(75)
    time.sleep_ms(3000)  # Allow fans to reach running speed before asserting
    ok = fans_mon.verify_running(200, 500)
    reading = mon_12v.read()
    mA_str = f"{reading['current_mA']:.1f}mA" if reading else "read failed"
    passed = ok is True
    print(
        f"  {'PASS' if passed else 'FAIL'} - fans on 75%: current in 200-500mA range ({mA_str})"
    )
    all_passed &= passed

    # --- Test 11: fans off - log idle current (no assertion; load-dependent) ---
    fans_mon.off()
    time.sleep_ms(500)
    reading = mon_12v.read()
    if reading:
        print(f"  INFO  - 12V idle current after off(): {reading['current_mA']:.1f}mA")
    else:
        print(f"  INFO  - 12V idle current after off(): read failed")

    # --- Fault contract tests ---
    print("\n  -- Fault contract tests --")

    # --- Test 12: fault attributes exist and default to safe ---
    fans2 = CirculationFans()
    passed = (
        fans2.fault is False
        and fans2.fault_code is None
        and fans2.fault_message is None
        and fans2.fault_tier == "fault"
        and fans2.fault_last_checked_ms is None
    )
    print(f"  {'PASS' if passed else 'FAIL'} - Fault attributes default to safe state")
    all_passed &= passed

    # --- Test 13: off() clears a latched fault ---
    fans2.fault = True
    fans2.fault_code = "CIRC_FAN_FAULT"
    fans2.fault_message = "test"
    fans2.off()
    passed = fans2.fault is False and fans2.fault_code is None
    print(f"  {'PASS' if passed else 'FAIL'} - off() clears latched fault")
    all_passed &= passed

    # --- Test 14: check_health() returns False when stopped and no fault ---
    fans2.off()
    result = fans2.check_health()
    passed = result is False and fans2.fault_last_checked_ms is not None
    print(f"  {'PASS' if passed else 'FAIL'} - check_health() stopped, no fault -> False")
    all_passed &= passed

    # --- Test 15: verify_running() latches fault on out-of-range (with monitor) ---
    fans_mon2 = CirculationFans(current_monitor=mon_12v)
    # Fans off, so current should be near idle -- way below 200mA
    fans_mon2._running = True  # pretend running so check_health works
    ok = fans_mon2.verify_running(200, 500)
    passed = fans_mon2.fault is True and fans_mon2.fault_code == "CIRC_FAN_FAULT"
    print(f"  {'PASS' if passed else 'FAIL'} - verify_running() latches fault on low current")
    all_passed &= passed
    fans_mon2._running = False  # clean up

    print(f"\n{'All tests passed!' if all_passed else 'Some tests FAILED'}")
    return all_passed


if __name__ == "__main__":
    test()
