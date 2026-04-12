# lib/heater.py
#
# Controls the 120V backup ceramic PTC heater via a Fotek SSR-25DA
# solid-state relay. The SSR is driven by GP18 through a 1k ohm
# current-limiting resistor. Simple on/off driver -- all safety
# logic (temperature limits, interlocks) lives in the main controller.
#
# Hardware safety: RY85 85degC one-time thermal fuse on AC output side
# (firmware has no involvement with this -- it is a last-resort cutout).
#
# Wiring summary:
#   GP18 -> 1k ohm resistor -> SSR DC input (+)
#   SSR DC input (-) -> GND
#   SSR AC output -> thermal fuse -> 120V heater -> neutral

import machine
import time

# --- Constants ---
SSR_PIN = 18


class Heater:
    """
    On/off driver for the backup ceramic heater via SSR.

    No PWM or duty cycling -- the drying schedule controller in main.py
    is responsible for temperature regulation by calling on()/off().
    """

    def __init__(self, pin=SSR_PIN, logger=None):
        self._logger = logger
        self._pin = machine.Pin(pin, machine.Pin.OUT)
        self._pin.low()  # SSR off at boot -- safe state
        self._on = False

        # Fault contract (no detectable failure modes -- safety is in the
        # RY85 thermal fuse). Always False; present for aggregator uniformity.
        self.fault = False
        self.fault_code = None
        self.fault_message = None
        self.fault_tier = "fault"
        self.fault_last_checked_ms = None

        if self._logger:
            self._logger.event("heater", "Heater initialised, SSR off")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on(self):
        """Drive SSR control input HIGH, turning heater on."""
        self._pin.high()
        self._on = True
        if self._logger:
            self._logger.event("heater", "Heater on")

    def off(self):
        """Drive SSR control input LOW, turning heater off."""
        self._pin.low()
        self._on = False
        if self._logger:
            self._logger.event("heater", "Heater off")

    def check_health(self):
        """No-op self-check. Heater has no detectable failure modes."""
        self.fault_last_checked_ms = time.ticks_ms()
        return self.fault

    @property
    def is_on(self):
        """True if heater is currently commanded on."""
        return self._on


# --- Unit test ---
def test():
    print("=== Heater unit test ===")
    all_passed = True

    # --- Test 1: Heater initialises off ---
    h = Heater()
    passed = not h.is_on and h._pin.value() == 0
    print(f"  {'PASS' if passed else 'FAIL'} - Init: is_on()=False, pin LOW")
    all_passed &= passed

    # --- Test 2: on() turns heater on ---
    h.on()
    passed = h.is_on and h._pin.value() == 1
    print(f"  {'PASS' if passed else 'FAIL'} - on(): is_on()=True, pin HIGH")
    all_passed &= passed

    # --- Test 3: off() turns heater off ---
    h.off()
    passed = not h.is_on and h._pin.value() == 0
    print(f"  {'PASS' if passed else 'FAIL'} - off(): is_on()=False, pin LOW")
    all_passed &= passed

    # --- Test 4: Double on() is safe ---
    h.on()
    h.on()
    passed = h.is_on and h._pin.value() == 1
    print(f"  {'PASS' if passed else 'FAIL'} - Double on(): no error, still on")
    all_passed &= passed
    h.off()

    # --- Test 5: Double off() is safe ---
    h.off()
    h.off()
    passed = not h.is_on and h._pin.value() == 0
    print(f"  {'PASS' if passed else 'FAIL'} - Double off(): no error, still off")
    all_passed &= passed

    # --- Test 6: Logger receives on event ---
    class FakeLogger:
        def __init__(self):
            self.events = []
        def event(self, source, message, level="INFO"):
            self.events.append((source, message, level))

    log = FakeLogger()
    h2 = Heater(logger=log)
    log.events.clear()  # Discard init event for this test
    h2.on()
    passed = len(log.events) == 1 and log.events[0] == ("heater", "Heater on", "INFO")
    print(f"  {'PASS' if passed else 'FAIL'} - Logger receives on event")
    all_passed &= passed

    # --- Test 7: Logger receives off event ---
    log.events.clear()
    h2.off()
    passed = len(log.events) == 1 and log.events[0] == ("heater", "Heater off", "INFO")
    print(f"  {'PASS' if passed else 'FAIL'} - Logger receives off event")
    all_passed &= passed

    # --- Test 8: Logger=None is safe ---
    h3 = Heater(logger=None)
    try:
        h3.on()
        h3.off()
        h3.is_on
        passed = True
    except Exception as e:
        passed = False
        print(f"    Exception: {e}")
    print(f"  {'PASS' if passed else 'FAIL'} - Logger=None: all methods work")
    all_passed &= passed

    print(f"\n{'All tests passed!' if all_passed else 'Some tests FAILED'}")
    return all_passed


if __name__ == "__main__":
    test()
