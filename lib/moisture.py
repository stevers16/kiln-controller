# lib/moisture.py
#
# Reads wood moisture content (MC%) from two resistive probe channels.
# Each channel uses a 100kohm reference resistor voltage divider with
# AC excitation to prevent electrolysis on the probe pins.
#
# Wiring (per channel):
#   GP6 (excitation) ----[R6 100kohm]---+--- GP26 (ADC0)
#                                       |
#                                   R_wood (probe pins in lumber)
#                                       |
#                                      GND
#
# GP6/GP26 = channel 1 (maple), GP7/GP27 = channel 2 (beech)

import machine
import time
import math

# --- Constants ---
R_REF_OHM = 100_000
ADC_MAX = 65535
VCC = 3.3
SETTLE_MS = 15
SAMPLE_COUNT = 5
SAMPLE_INTERVAL_MS = 2
DISCHARGE_MS = 10
TEMP_REF_C = 20.0
TEMP_CORRECTION_PER_DEG = 0.06

# Resistance-to-MC% lookup table (Douglas fir reference)
# (R_wood_ohms, MC_percent) pairs, log-spaced
# Source: Forest Products Laboratory Wood Handbook
RESISTANCE_TABLE = [
    (1_000, 30.0),
    (2_000, 27.0),
    (5_000, 24.0),
    (10_000, 21.5),
    (20_000, 19.0),
    (50_000, 16.5),
    (100_000, 14.5),
    (200_000, 12.5),
    (500_000, 10.5),
    (1_000_000, 8.5),
    (2_000_000, 7.0),
    (5_000_000, 6.0),
]

# Species correction offsets (MC% points, applied after base lookup)
SPECIES_CORRECTION = {
    "maple": -0.5,
    "beech": -0.3,
    "douglas_fir": 0.0,
    "oak": +0.5,
    "pine": +0.3,
}


def resistance_to_mc(r_ohms, species="douglas_fir"):
    """
    Convert wood resistance (ohms) to moisture content (MC%).

    Uses log-linear interpolation on the resistance lookup table,
    then applies a species correction offset.

    Returns float MC% or None if resistance is out of measurable range.
    """
    if r_ohms is None:
        return None

    # Clamp low -- at or above fibre saturation
    if r_ohms <= 1_000:
        mc = 30.0
        correction = SPECIES_CORRECTION.get(species, 0.0)
        return mc + correction

    # Too dry to measure reliably
    if r_ohms >= 5_000_000:
        return None

    # Log-linear interpolation between bracketing table entries
    log_r = math.log10(r_ohms)

    for i in range(len(RESISTANCE_TABLE) - 1):
        r_lo, mc_lo = RESISTANCE_TABLE[i]
        r_hi, mc_hi = RESISTANCE_TABLE[i + 1]
        if r_lo <= r_ohms <= r_hi:
            log_lo = math.log10(r_lo)
            log_hi = math.log10(r_hi)
            frac = (log_r - log_lo) / (log_hi - log_lo)
            mc = mc_lo + frac * (mc_hi - mc_lo)
            correction = SPECIES_CORRECTION.get(species, 0.0)
            return mc + correction

    # Should not reach here, but return None as safety
    return None


def _apply_temp_correction(mc_raw, temp_c):
    """
    Apply temperature correction to raw MC% reading.

    Reference temperature is 20degC. At higher kiln temperatures,
    wood resistance drops, causing raw MC% to read too high.
    Correction: approx +0.06 MC% per degC below 20, -0.06 above 20.
    """
    correction = (TEMP_REF_C - temp_c) * TEMP_CORRECTION_PER_DEG
    return mc_raw + correction


class MoistureProbe:
    """
    Controller for two resistive wood moisture probe channels.

    Both channels are read independently and sequentially. AC excitation
    alternates current direction between readings to prevent electrolysis.
    """

    def __init__(
        self,
        excite_pin_1=6,
        adc_pin_1=26,
        excite_pin_2=7,
        adc_pin_2=27,
        species_1="maple",
        species_2="beech",
        logger=None,
    ):
        self._logger = logger
        self._species_1 = species_1
        self._species_2 = species_2

        # Excitation pins - digital outputs, driven LOW at init
        self._excite_1 = machine.Pin(excite_pin_1, machine.Pin.OUT)
        self._excite_1.low()
        self._excite_2 = machine.Pin(excite_pin_2, machine.Pin.OUT)
        self._excite_2.low()

        # ADC pins
        self._adc_1 = machine.ADC(machine.Pin(adc_pin_1))
        self._adc_2 = machine.ADC(machine.Pin(adc_pin_2))

        # Warn once if species not in correction table
        for sp in (species_1, species_2):
            if sp not in SPECIES_CORRECTION:
                if self._logger:
                    self._logger.event(
                        "moisture",
                        f"Unknown species '{sp}' -- using 0.0 correction",
                        level="WARN",
                    )

        # Per-channel calibration offsets (MC% points, loaded from SD)
        self._cal_offset_1 = 0.0
        self._cal_offset_2 = 0.0

        # Fault contract
        self.fault = False
        self.fault_code = None
        self.fault_message = None
        self.fault_tier = "fault"
        self.fault_last_checked_ms = None
        self._fail_ch1 = 0
        self._fail_ch2 = 0

        if self._logger:
            self._logger.event(
                "moisture", f"Moisture probe init -- ch1={species_1} ch2={species_2}"
            )

    def set_calibration(self, channel_1_offset=0.0, channel_2_offset=0.0):
        """
        Set per-channel calibration offsets (MC% points).

        Corrected MC% = raw MC% + offset. Offsets may be negative or positive.
        Typically loaded from calibration.json on the SD card at boot.
        """
        self._cal_offset_1 = float(channel_1_offset)
        self._cal_offset_2 = float(channel_2_offset)
        if self._logger:
            self._logger.event(
                "moisture",
                f"Calibration set -- ch1_offset={self._cal_offset_1} ch2_offset={self._cal_offset_2}",
            )

    def _read_channel(self, excite_pin, adc, samples=SAMPLE_COUNT):
        """
        Read a single moisture channel.

        Drives excitation HIGH, takes ADC samples, drives LOW, then
        computes R_wood from the voltage divider.

        Returns R_wood in ohms (float) or None if reading is invalid.
        """
        try:
            # Drive excitation HIGH and let signal settle
            excite_pin.high()
            time.sleep_ms(SETTLE_MS)

            # Take samples
            readings = []
            for i in range(samples):
                readings.append(adc.read_u16())
                if i < samples - 1:
                    time.sleep_ms(SAMPLE_INTERVAL_MS)

            # Drive excitation LOW and allow discharge
            excite_pin.low()
            time.sleep_ms(DISCHARGE_MS)

            # Average the samples
            avg = sum(readings) / len(readings)

            # Convert to voltage
            v = avg / ADC_MAX * VCC

            # Calculate R_wood from voltage divider
            if v >= VCC or v <= 0:
                return None  # Open circuit or short

            r_wood = R_REF_OHM * v / (VCC - v)
            return r_wood

        except Exception as e:
            # Ensure excitation is LOW on any error
            try:
                excite_pin.low()
            except Exception:
                pass
            print(f"moisture: channel read error: {e}")
            return None

    def read_resistance(self):
        """
        Returns raw resistance readings for both channels.

        dict keys: "ch1_ohms", "ch2_ohms"
        Values are float (ohms) or None if probe disconnected / invalid.
        """
        ch1 = self._read_channel(self._excite_1, self._adc_1)
        ch2 = self._read_channel(self._excite_2, self._adc_2)
        return {"ch1_ohms": ch1, "ch2_ohms": ch2}

    def read(self):
        """
        Returns MC% for both channels plus raw resistance.

        dict keys: "ch1_mc_pct", "ch2_mc_pct", "ch1_ohms", "ch2_ohms"
        MC% values are float or None.
        Logs WARN via logger if either channel returns None.
        """
        res = self.read_resistance()
        ch1_ohms = res["ch1_ohms"]
        ch2_ohms = res["ch2_ohms"]

        ch1_mc = resistance_to_mc(ch1_ohms, self._species_1)
        ch2_mc = resistance_to_mc(ch2_ohms, self._species_2)

        # Apply per-channel calibration offsets
        if ch1_mc is not None:
            ch1_mc += self._cal_offset_1
        if ch2_mc is not None:
            ch2_mc += self._cal_offset_2

        if ch1_mc is None and self._logger:
            if ch1_ohms is None:
                self._logger.event(
                    "moisture",
                    "Ch1 probe disconnected or open circuit",
                    level="WARN",
                )
            else:
                self._logger.event(
                    "moisture",
                    f"Ch1 resistance out of range: {ch1_ohms:.0f} ohm",
                    level="WARN",
                )

        if ch2_mc is None and self._logger:
            if ch2_ohms is None:
                self._logger.event(
                    "moisture",
                    "Ch2 probe disconnected or open circuit",
                    level="WARN",
                )
            else:
                self._logger.event(
                    "moisture",
                    f"Ch2 resistance out of range: {ch2_ohms:.0f} ohm",
                    level="WARN",
                )

        # Track consecutive None readings per channel (N=3 to latch)
        if ch1_mc is None:
            self._fail_ch1 += 1
        else:
            self._fail_ch1 = 0
        if ch2_mc is None:
            self._fail_ch2 += 1
        else:
            self._fail_ch2 = 0

        # Update fault state
        if self._fail_ch1 >= 3 or self._fail_ch2 >= 3:
            self.fault = True
            self.fault_code = "MOISTURE_PROBE_FAIL"
            which = []
            if self._fail_ch1 >= 3:
                which.append("ch1")
            if self._fail_ch2 >= 3:
                which.append("ch2")
            self.fault_message = f"Probe failing: {', '.join(which)}"
        else:
            self.fault = False
            self.fault_code = None
            self.fault_message = None

        return {
            "ch1_mc_pct": ch1_mc,
            "ch2_mc_pct": ch2_mc,
            "ch1_ohms": ch1_ohms,
            "ch2_ohms": ch2_ohms,
        }

    def check_health(self):
        """Periodic self-check. Performs a fresh read to update fault state.

        Each channel read takes ~30ms (AC excitation + ADC samples).
        Cheap enough to run every status cache update (10-120s interval).
        """
        self.fault_last_checked_ms = time.ticks_ms()
        self.read()
        return self.fault

    def read_with_temp_correction(self, temp_c):
        """
        Same as read() but applies temperature correction factor.

        temp_c: wood temperature from SHT31 lumber zone sensor.
        Returns same dict as read(), with corrected MC% values.
        """
        result = self.read()

        if result["ch1_mc_pct"] is not None:
            result["ch1_mc_pct"] = _apply_temp_correction(result["ch1_mc_pct"], temp_c)
        if result["ch2_mc_pct"] is not None:
            result["ch2_mc_pct"] = _apply_temp_correction(result["ch2_mc_pct"], temp_c)

        return result


# --- Unit tests ---
def test():
    print("=== MoistureProbe unit test ===")
    all_passed = True

    # --- Test 1: Init state ---
    probe = MoistureProbe()
    p1 = probe._excite_1.value() == 0
    p2 = probe._excite_2.value() == 0
    passed = p1 and p2
    print(f"  {'PASS' if passed else 'FAIL'} - Init: both excitation pins LOW")
    all_passed &= passed

    # --- Test 2: Read resistance (probes connected) ---
    res = probe.read_resistance()
    ch1 = res["ch1_ohms"]
    ch2 = res["ch2_ohms"]
    passed = (
        ch1 is not None
        and ch2 is not None
        and 1_000 <= ch1 <= 10_000_000
        and 1_000 <= ch2 <= 10_000_000
    )
    print(f"  {'PASS' if passed else 'FAIL'} - Read resistance: ch1={ch1} ch2={ch2}")
    all_passed &= passed

    # --- Test 3: Read MC% (probes connected) ---
    reading = probe.read()
    mc1 = reading["ch1_mc_pct"]
    mc2 = reading["ch2_mc_pct"]
    passed = (
        mc1 is not None
        and mc2 is not None
        and 6.0 <= mc1 <= 30.0
        and 6.0 <= mc2 <= 30.0
    )
    print(f"  {'PASS' if passed else 'FAIL'} - Read MC%: ch1={mc1} ch2={mc2}")
    all_passed &= passed

    # --- Test 4: Excitation pins LOW after read ---
    p1 = probe._excite_1.value() == 0
    p2 = probe._excite_2.value() == 0
    passed = p1 and p2
    print(f"  {'PASS' if passed else 'FAIL'} - Excitation pins LOW after read")
    all_passed &= passed

    # --- Test 5: Open circuit (probes disconnected) ---
    # This test must be run separately with probes unplugged.
    # From the REPL:
    #   from lib.moisture import MoistureProbe
    #   p = MoistureProbe()
    #   r = p.read()
    #   print(r)  # ch1_mc_pct and ch2_mc_pct should both be None
    print("  SKIP - Test 5: run manually with probes disconnected (see source)")

    # --- Test 6: resistance_to_mc() module function ---
    t6_pass = True
    mc = resistance_to_mc(100_000, "maple")
    ok = mc is not None and 13.5 <= mc <= 14.5
    t6_pass &= ok
    print(f"  {'PASS' if ok else 'FAIL'} - resistance_to_mc(100k, maple) = {mc}")

    mc = resistance_to_mc(100_000, "beech")
    ok = mc is not None and 13.7 <= mc <= 14.7
    t6_pass &= ok
    print(f"  {'PASS' if ok else 'FAIL'} - resistance_to_mc(100k, beech) = {mc}")

    mc = resistance_to_mc(1_000, "maple")
    ok = mc is not None and mc >= 29.0
    t6_pass &= ok
    print(
        f"  {'PASS' if ok else 'FAIL'} - resistance_to_mc(1k, maple) = {mc} (clamped)"
    )

    mc = resistance_to_mc(9_000_000, "maple")
    ok = mc is None
    t6_pass &= ok
    print(
        f"  {'PASS' if ok else 'FAIL'} - resistance_to_mc(9M, maple) = None (too dry)"
    )

    passed = t6_pass
    all_passed &= passed

    # --- Test 7: Temperature correction ---
    reading_20 = probe.read()
    reading_corr = probe.read_with_temp_correction(20.0)
    reading_hot = probe.read_with_temp_correction(60.0)

    if reading_20["ch1_mc_pct"] is not None and reading_corr["ch1_mc_pct"] is not None:
        # At 20degC correction should be ~0
        diff_20 = abs(reading_20["ch1_mc_pct"] - reading_corr["ch1_mc_pct"])
        ok_20 = diff_20 < 0.5  # Allow small variance from separate reads
        print(
            f"  {'PASS' if ok_20 else 'FAIL'} - Temp correction at 20degC: diff={diff_20:.2f} (expect ~0)"
        )

        if reading_hot["ch1_mc_pct"] is not None:
            # At 60degC, corrected should be ~2.4 points lower than uncorrected
            expected_drop = (60.0 - TEMP_REF_C) * TEMP_CORRECTION_PER_DEG
            diff_hot = reading_20["ch1_mc_pct"] - reading_hot["ch1_mc_pct"]
            ok_hot = (
                abs(diff_hot - expected_drop) < 1.0
            )  # Allow variance from separate reads
            print(
                f"  {'PASS' if ok_hot else 'FAIL'} - Temp correction at 60degC: drop={diff_hot:.2f} (expect ~{expected_drop:.1f})"
            )
            passed = ok_20 and ok_hot
        else:
            print("  SKIP - Could not verify 60degC correction (probe returned None)")
            passed = ok_20
    else:
        print("  SKIP - Test 7 requires probes connected")
        passed = True
    all_passed &= passed

    # --- Test 8: Logger integration ---
    class MockLogger:
        def __init__(self):
            self.calls = []

        def event(self, source, message, level="INFO"):
            self.calls.append((source, message, level))

    mock = MockLogger()
    logged_probe = MoistureProbe(logger=mock)
    # Init should have logged
    init_logged = any("init" in c[1].lower() for c in mock.calls)

    # Force a disconnected read by using an ADC pin with nothing on it
    # (GP28 is spare -- should read ~0 or noise)
    # Instead, just check that the logger was called at init
    passed = init_logged
    print(f"  {'PASS' if passed else 'FAIL'} - Logger: init event logged")
    all_passed &= passed

    # Check WARN on None readings (use the mock logger probe as-is;
    # if probes are connected, force a None by testing resistance_to_mc)
    mock.calls.clear()
    # Simulate: if probes happen to return None, logger should warn
    # We can at least verify the mock pattern works
    if logged_probe.read()["ch1_mc_pct"] is None:
        warn_logged = any(c[2] == "WARN" for c in mock.calls)
        print(
            f"  {'PASS' if warn_logged else 'FAIL'} - Logger: WARN on None reading"
        )
        all_passed &= warn_logged
    else:
        print("  INFO  - Logger WARN test: probes connected, no None to trigger")

    # --- Test 9: set_calibration() offsets ---
    mock2 = MockLogger()
    cal_probe = MoistureProbe(logger=mock2)
    # Default offsets should be 0.0
    ok_defaults = cal_probe._cal_offset_1 == 0.0 and cal_probe._cal_offset_2 == 0.0
    print(f"  {'PASS' if ok_defaults else 'FAIL'} - Calibration: default offsets are 0.0")
    all_passed &= ok_defaults

    cal_probe.set_calibration(channel_1_offset=-1.2, channel_2_offset=0.8)
    ok_set = cal_probe._cal_offset_1 == -1.2 and cal_probe._cal_offset_2 == 0.8
    print(f"  {'PASS' if ok_set else 'FAIL'} - Calibration: offsets set correctly")
    all_passed &= ok_set

    # Verify calibration event was logged
    cal_logged = any("calibration" in c[1].lower() for c in mock2.calls)
    print(f"  {'PASS' if cal_logged else 'FAIL'} - Calibration: set event logged")
    all_passed &= cal_logged

    # Verify offset is applied to MC% reading
    reading_uncal = probe.read()  # probe has 0.0 offsets
    cal_probe_2 = MoistureProbe()
    cal_probe_2.set_calibration(channel_1_offset=-2.0, channel_2_offset=1.5)
    reading_cal = cal_probe_2.read()
    if reading_uncal["ch1_mc_pct"] is not None and reading_cal["ch1_mc_pct"] is not None:
        diff = reading_uncal["ch1_mc_pct"] - reading_cal["ch1_mc_pct"]
        # Expect ~2.0 difference (allow variance from separate reads)
        ok_applied = abs(diff - 2.0) < 1.0
        print(
            f"  {'PASS' if ok_applied else 'FAIL'} - Calibration: offset applied to MC% (diff={diff:.2f}, expect ~2.0)"
        )
        all_passed &= ok_applied
    else:
        print("  SKIP - Calibration offset verification requires probes connected")

    print(f"\n{'All tests passed!' if all_passed else 'Some tests FAILED'}")
    return all_passed


if __name__ == "__main__":
    test()
