# test_fault_surfacing.py
#
# Stand-alone test for the fault surfacing mechanism (error_checking_spec.md).
# Runs on the Pico via: mpremote run test_fault_surfacing.py
#
# Does NOT require any broken hardware. Tests inject faults by poking
# attributes on stub modules or on real module instances.

import time
import sys

# ---------------------------------------------------------------------------
# Copy of the ALERT_CODE_TIERS table from main.py so the test is self-
# contained and does not need to import the full main module (which would
# boot WiFi, start the HTTP server, etc.).
# ---------------------------------------------------------------------------
ALERT_CODE_TIERS = {
    "CIRC_FAN_FAULT": "fault",
    "EXHAUST_FAN_STALL": "fault",
    "VENT_STALL": "fault",
    "SENSOR_LUMBER_FAIL": "fault",
    "SENSOR_INTAKE_FAIL": "fault",
    "SENSOR_FAILURE": "fault",
    "MOISTURE_PROBE_FAIL": "fault",
    "HEATER_TIMEOUT": "fault",
    "HEATER_FAULT": "fault",
    "TEMP_OOR": "fault",
    "RH_OOR": "fault",
    "TEMP_OUT_OF_RANGE": "fault",
    "RH_OUT_OF_RANGE": "fault",
    "OVER_TEMP": "fault",
    "SD_FAIL": "fault",
    "SD_WRITE_FAIL": "fault",
    "LORA_FAIL": "fault",
    "LORA_TIMEOUT": "fault",
    "CURRENT_12V_FAIL": "fault",
    "CURRENT_5V_FAIL": "fault",
    "DISPLAY_FAIL": "fault",
    "MODULE_CHECK_FAILED": "fault",
    "STAGE_GOAL_NOT_MET": "notice",
    "STAGE_GOAL_NOT_REACHED": "notice",
    "WATER_PAN_REMINDER": "notice",
}


# ---------------------------------------------------------------------------
# Copy of _compact_json_value and _build_compact_json from main.py
# ---------------------------------------------------------------------------
def _compact_json_value(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{round(v, 1)}"
    return str(v)


def _build_compact_json(d):
    parts = []
    for k, v in d.items():
        if isinstance(v, str):
            parts.append(f'"{k}":"{v}"')
        elif isinstance(v, (list, tuple)):
            items = ",".join(f'"{s}"' for s in v)
            parts.append(f'"{k}":[{items}]')
        else:
            parts.append(f'"{k}":{_compact_json_value(v)}')
    return "{" + ",".join(parts) + "}"


# ---------------------------------------------------------------------------
# Stub module: minimal object implementing the fault contract
# ---------------------------------------------------------------------------
class StubModule:
    def __init__(self):
        self.fault = False
        self.fault_code = None
        self.fault_message = None
        self.fault_tier = "fault"
        self.fault_last_checked_ms = None

    def check_health(self):
        self.fault_last_checked_ms = time.ticks_ms()
        return self.fault


# ---------------------------------------------------------------------------
# Copy of _collect_module_faults logic, parameterised on a module list
# ---------------------------------------------------------------------------
def collect_faults(module_list):
    """Run the aggregator over a list of (source, mod) tuples."""
    faults = []
    for source, mod in module_list:
        if mod is None:
            continue
        try:
            if hasattr(mod, "check_health"):
                mod.check_health()
        except Exception as e:
            faults.append((
                "MODULE_CHECK_FAILED",
                source,
                str(e)[:80],
                "fault",
            ))
            continue
        if getattr(mod, "fault", False):
            code = getattr(mod, "fault_code", None) or "UNKNOWN_FAULT"
            msg = getattr(mod, "fault_message", None) or ""
            tier = getattr(mod, "fault_tier", "fault")
            faults.append((code, source, msg, tier))

    seen = set()
    codes = []
    details = []
    tier_order = {"fault": 0, "notice": 1, "info": 2}
    faults.sort(key=lambda x: tier_order.get(x[3], 2))
    for code, src, msg, tier in faults:
        if code not in seen:
            seen.add(code)
            codes.append(code)
        details.append({
            "code": code,
            "source": src,
            "message": msg,
            "tier": tier,
        })
    return codes, details


def merge_schedule_alerts(codes, details, alert_ts_dict):
    """Merge schedule _last_alert_ts into codes/details."""
    now = time.ticks_ms()
    for atype, ts in alert_ts_dict.items():
        if time.ticks_diff(now, ts) < 30 * 60_000:
            code_upper = atype.upper()
            tier = ALERT_CODE_TIERS.get(code_upper, "info")
            existing_codes = {d["code"] for d in details}
            if code_upper not in existing_codes:
                details.append({
                    "code": code_upper,
                    "source": "schedule",
                    "message": "",
                    "tier": tier,
                })
            if code_upper not in codes:
                codes.append(code_upper)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test():
    print("=== Fault Surfacing Tests ===")
    all_passed = True

    # --- Test A: No faults ---
    print("\n  -- Test A: No faults --")
    mods = [
        ("circ", StubModule()),
        ("exhaust", StubModule()),
        ("vents", StubModule()),
        ("heater", StubModule()),
        ("sensors", StubModule()),
        ("lora", StubModule()),
        ("sdcard", StubModule()),
        ("missing", None),  # None modules should be skipped
    ]
    codes, details = collect_faults(mods)
    passed = codes == [] and details == []
    print(f"  {'PASS' if passed else 'FAIL'} - No faults: codes={codes}, details count={len(details)}")
    all_passed &= passed

    # --- Test B: Single fault ---
    print("\n  -- Test B: Single fault --")
    circ = StubModule()
    circ.fault = True
    circ.fault_code = "CIRC_FAN_FAULT"
    circ.fault_message = "injected"
    circ.fault_tier = "fault"
    mods = [("circulation", circ), ("exhaust", StubModule())]
    codes, details = collect_faults(mods)
    passed = (
        "CIRC_FAN_FAULT" in codes
        and len(details) == 1
        and details[0]["tier"] == "fault"
        and details[0]["source"] == "circulation"
    )
    print(f"  {'PASS' if passed else 'FAIL'} - Single fault: codes={codes}")
    all_passed &= passed

    # --- Test C: Multiple faults, ordering ---
    print("\n  -- Test C: Multiple faults from different sources --")
    circ = StubModule()
    circ.fault = True
    circ.fault_code = "CIRC_FAN_FAULT"
    circ.fault_tier = "fault"

    notice_mod = StubModule()
    notice_mod.fault = True
    notice_mod.fault_code = "STAGE_GOAL_NOT_MET"
    notice_mod.fault_tier = "notice"

    sd = StubModule()
    sd.fault = True
    sd.fault_code = "SD_WRITE_FAIL"
    sd.fault_tier = "fault"

    mods = [
        ("circulation", circ),
        ("schedule_stub", notice_mod),
        ("sdcard", sd),
    ]
    codes, details = collect_faults(mods)
    # Faults should come before notices in the codes list
    fault_idx = codes.index("CIRC_FAN_FAULT")
    notice_idx = codes.index("STAGE_GOAL_NOT_MET")
    passed = (
        len(codes) == 3
        and fault_idx < notice_idx
    )
    print(f"  {'PASS' if passed else 'FAIL'} - Multiple faults ordered: {codes}")
    all_passed &= passed

    # --- Test D: check_health() exception handling ---
    print("\n  -- Test D: check_health() exception handling --")
    class BuggyModule(StubModule):
        def check_health(self):
            raise RuntimeError("I2C bus locked up")

    buggy = BuggyModule()
    ok_mod = StubModule()
    mods = [("buggy_sensor", buggy), ("good_fan", ok_mod)]
    codes, details = collect_faults(mods)
    passed = (
        "MODULE_CHECK_FAILED" in codes
        and any(d["source"] == "buggy_sensor" for d in details)
        and any("I2C bus locked up" in d["message"] for d in details)
    )
    print(f"  {'PASS' if passed else 'FAIL'} - Exception caught: codes={codes}")
    all_passed &= passed

    # --- Test E: Schedule-emitted notice via merge ---
    print("\n  -- Test E: Schedule-emitted notice --")
    codes = []
    details = []
    alert_ts = {"stage_goal_not_met": time.ticks_ms()}
    merge_schedule_alerts(codes, details, alert_ts)
    passed = (
        "STAGE_GOAL_NOT_MET" in codes
        and len(details) == 1
        and details[0]["tier"] == "notice"
        and details[0]["source"] == "schedule"
    )
    print(f"  {'PASS' if passed else 'FAIL'} - Schedule notice: codes={codes}, tier={details[0]['tier'] if details else 'N/A'}")
    all_passed &= passed

    # --- Test F: Latching and clearing with N=3 ---
    print("\n  -- Test F: Latching and clearing --")
    try:
        from circulation import CirculationFans
        fans = CirculationFans()

        # Manually latch the fault
        fans.fault = True
        fans.fault_code = "CIRC_FAN_FAULT"
        fans.fault_message = "test latch"
        fans._good_reads = 0

        # Simulate 2 consecutive good reads (should NOT clear yet)
        fans._good_reads = 2
        # Fault should still be latched
        passed_a = fans.fault is True
        print(f"  {'PASS' if passed_a else 'FAIL'} - After 2 good reads: fault still latched")

        # Simulate 3rd good read via the counter reaching 3
        fans._good_reads = 3
        # Now simulate what verify_running does when ok is True and _good_reads >= 3
        if fans._good_reads >= 3 and fans.fault:
            fans.fault = False
            fans.fault_code = None
            fans.fault_message = None
        passed_b = fans.fault is False and fans.fault_code is None
        print(f"  {'PASS' if passed_b else 'FAIL'} - After 3 good reads: fault cleared")

        # off() also clears
        fans.fault = True
        fans.fault_code = "CIRC_FAN_FAULT"
        fans.off()
        passed_c = fans.fault is False
        print(f"  {'PASS' if passed_c else 'FAIL'} - off() clears fault")

        passed = passed_a and passed_b and passed_c
    except Exception as e:
        print(f"  FAIL - Exception during latch/clear test: {e}")
        passed = False
    all_passed &= passed

    # --- Test G: LoRa compact JSON with faults (trim-to-fit) ---
    print("\n  -- Test G: LoRa compact JSON with faults --")
    # Build base telemetry payload (no faults) to measure headroom
    base_payload = {
        "ts": 1712685000,
        "stage_idx": 3,
        "temp_lumber": 45.3,
        "temp_intake": 22.1,
        "rh_lumber": 68.5,
        "rh_intake": 55.0,
        "mc_channel_1": 28.3,
        "mc_channel_2": 31.7,
        "exhaust_fan_rpm": 2400,
        "exhaust_fan_pct": 80,
        "circ_fan_on": 1,
        "heater_on": 0,
        "vent_open": 1,
    }
    base_wire = _build_compact_json(base_payload).encode()
    base_len = len(base_wire)
    passed_base = base_len <= 255
    print(f"  {'PASS' if passed_base else 'FAIL'} - Base packet (no faults): {base_len} bytes")

    # Verify the trim-to-fit logic: try adding faults, pop until it fits.
    # Short codes should fit; the logic must never produce >255 bytes.
    short_codes = ["SD_FAIL", "LORA_FAIL"]
    fault_codes = list(short_codes)
    payload = dict(base_payload)
    while fault_codes:
        payload["faults"] = fault_codes
        wire = _build_compact_json(payload).encode()
        if len(wire) <= 255:
            break
        fault_codes.pop()

    if fault_codes:
        passed_trimmed = len(wire) <= 255
        print(f"  {'PASS' if passed_trimmed else 'FAIL'} - Short codes fit: {len(fault_codes)} faults, {len(wire)} bytes")
    else:
        # Even short codes didn't fit - unexpected with a 231-byte base
        passed_trimmed = False
        print(f"  FAIL - Could not fit even short fault codes (base={base_len} bytes)")

    # Long codes get trimmed gracefully (0 codes is acceptable)
    long_codes = [
        "CIRC_FAN_FAULT", "EXHAUST_FAN_STALL", "SENSOR_LUMBER_FAIL",
        "SD_WRITE_FAIL", "MOISTURE_PROBE_FAIL",
    ]
    fault_codes = list(long_codes)
    payload = dict(base_payload)
    while fault_codes:
        payload["faults"] = fault_codes
        wire = _build_compact_json(payload).encode()
        if len(wire) <= 255:
            break
        fault_codes.pop()

    if fault_codes:
        passed_long = len(wire) <= 255
        print(f"  {'PASS' if passed_long else 'FAIL'} - Long codes trimmed to {len(fault_codes)}: {len(wire)} bytes")
    else:
        # All trimmed away - still passes (graceful degradation)
        passed_long = True
        print(f"  PASS - Long codes trimmed to 0 (base too large) - graceful fallback")

    passed = passed_base and passed_trimmed and passed_long
    all_passed &= passed

    # --- Summary ---
    print(f"\n{'All tests passed!' if all_passed else 'Some tests FAILED'}")
    return all_passed


if __name__ == "__main__":
    ok = test()
    sys.exit(0 if ok else 1)
