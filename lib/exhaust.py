# lib/exhaust.py
#
# Controls the 80mm exhaust fan (Foxconn PVA080G12Q) via PWM speed control
# and a dedicated MOSFET gate pin for hard on/off switching.
#
# PWM and gate are separate pins - GP16 drives the PWM signal, GP21 drives
# the FQP30N06L MOSFET gate. Gate is initialised low (fan off) at boot.
#
# Wiring summary:
#   Fan pin 1 (GND/black)  -> MOSFET drain
#   Fan pin 2 (12V/yellow) -> 12V rail (direct)
#   Fan pin 3 (tach/green) -> GP22 via 10kohm pull-up to 3.3V + 104 cap to GND
#   Fan pin 4 (PWM/blue)   -> GP16 via 100ohm series resistor
#   MOSFET gate (GP21)     -> 100ohm gate resistor -> GP21; 10kohm pull-down to GND
#   MOSFET source          -> 12V rail GND
#   Flyback diode          -> 1N4007 across fan (cathode to 12V, anode to drain)

import machine
import time

# --- Constants ---
PWM_PIN             = 16
GATE_PIN            = 21
TACH_PIN            = 22
PWM_FREQ            = 25000
TACH_PULSES_PER_REV = 2


class ExhaustFan:
    """
    Controller for the 80mm PWM exhaust fan.

    Gate and PWM are separate pins. Gate is driven low at init so the
    fan is always off at boot regardless of PWM state.
    """

    def __init__(self, pwm_pin=PWM_PIN, tach_pin=TACH_PIN, gate_pin=GATE_PIN,
                 logger=None):
        self._logger = logger

        # Gate pin - initialise low (fan off) before PWM is configured
        self._gate = machine.Pin(gate_pin, machine.Pin.OUT)
        self._gate.low()

        # PWM - init with 0% duty so gate and PWM agree at startup
        self._pwm = machine.PWM(machine.Pin(pwm_pin))
        self._pwm.freq(PWM_FREQ)
        self._pwm.duty_u16(0)

        # Tach - falling-edge IRQ counts pulses
        self._pulse_count = 0
        self._tach = machine.Pin(tach_pin, machine.Pin.IN, machine.Pin.PULL_UP)
        self._tach.irq(trigger=machine.Pin.IRQ_FALLING, handler=self._tach_irq)

        self._running = False
        self._speed_pct = 0

    def _tach_irq(self, pin):
        self._pulse_count += 1

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on(self, speed_percent):
        """Start fan at given speed (0-100%)."""
        speed_percent = max(0, min(100, speed_percent))
        duty = int(speed_percent / 100 * 65535)
        self._gate.high()
        self._pwm.duty_u16(duty)
        self._running = True
        self._speed_pct = speed_percent
        if self._logger:
            self._logger.event("exhaust", f"Fan on at {speed_percent}%")

    def off(self):
        """Stop fan - zero PWM duty and pull gate low."""
        self._pwm.duty_u16(0)
        self._gate.low()
        self._running = False
        self._speed_pct = 0
        if self._logger:
            self._logger.event("exhaust", "Fan off")

    def set_speed(self, speed_percent):
        """Adjust speed while running. Ignored if fan is off."""
        if self._running:
            self.on(speed_percent)

    def read_rpm(self, sample_ms=2000):
        """Return RPM averaged over sample_ms milliseconds."""
        self._pulse_count = 0
        time.sleep_ms(sample_ms)
        rpm = (self._pulse_count / TACH_PULSES_PER_REV) * (60000 / sample_ms)
        return rpm

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self):
        """True if fan is currently commanded on."""
        return self._running

    @property
    def speed_pct(self):
        """Last commanded speed (0-100). 0 when off."""
        return self._speed_pct


# --- Unit test ---
def test():
    print("=== ExhaustFan unit test ===")
    fan = ExhaustFan()
    all_passed = True

    # --- Test 1: off at init ---
    passed = not fan.is_running and fan.speed_pct == 0
    print(f"  {'PASS' if passed else 'FAIL'} - Init: not running, speed=0")
    all_passed &= passed

    # --- Test 2: speed and RPM at various levels ---
    tests = [(25, 800, 1800), (50, 1800, 2800), (100, 3800, 4800)]
    for speed, rpm_min, rpm_max in tests:
        fan.on(speed)
        time.sleep(2)
        rpm = fan.read_rpm()
        passed = rpm_min < rpm < rpm_max
        print(f"  {'PASS' if passed else 'FAIL'} - {speed}% -> {rpm:.0f} RPM (expected {rpm_min}-{rpm_max})")
        all_passed &= passed

    # --- Test 3: speed_pct tracks ---
    fan.on(75)
    passed = fan.speed_pct == 75
    print(f"  {'PASS' if passed else 'FAIL'} - speed_pct=75 after on(75)")
    all_passed &= passed

    # --- Test 4: off() stops fan ---
    fan.off()
    time.sleep(2)
    rpm = fan.read_rpm(1000)
    passed = rpm == 0
    print(f"  {'PASS' if passed else 'FAIL'} - off() -> {rpm:.0f} RPM (expected 0)")
    all_passed &= passed

    # --- Test 5: is_running tracks state ---
    fan.on(50)
    passed = fan.is_running
    print(f"  {'PASS' if passed else 'FAIL'} - is_running=True after on()")
    all_passed &= passed

    fan.off()
    passed = not fan.is_running and fan.speed_pct == 0
    print(f"  {'PASS' if passed else 'FAIL'} - is_running=False, speed=0 after off()")
    all_passed &= passed

    # --- Test 6: set_speed() ignored when off ---
    fan.set_speed(80)
    passed = not fan.is_running
    print(f"  {'PASS' if passed else 'FAIL'} - set_speed() while off: no effect")
    all_passed &= passed

    print(f"\n{'All tests passed!' if all_passed else 'Some tests FAILED'}")
    return all_passed


if __name__ == "__main__":
    test()
