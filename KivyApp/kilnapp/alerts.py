"""Alert classification.

Until the firmware tags each alert with a severity (see error_checking_spec.md
section "Three-tier alert model"), the Kivy app classifies alert codes itself
using these tables. The wire format is lowercase and the firmware does not
guarantee a particular case, so all comparisons are case-insensitive.

Tiers
-----
- FAULT  - hardware/firmware problem the operator must address. Red banner.
- NOTICE - procedural / batch issue. The kiln is healthy but the operator
           should know something. Amber banner.
- INFO   - lifecycle log entry. Goes in the event log only; never on the
           dashboard banner. Includes things like "stage advanced" which
           the dashboard already shows in the StageBanner.
"""

from __future__ import annotations

from typing import Iterable, List, Tuple


# Hardware / firmware problems. Loud, red banner.
FAULT_CODES = frozenset(
    code.lower()
    for code in (
        "CIRC_FAN_FAULT",
        "EXHAUST_FAN_STALL",
        "VENT_STALL",
        "SENSOR_LUMBER_FAIL",
        "SENSOR_INTAKE_FAIL",
        "SENSOR_FAILURE",
        "MOISTURE_PROBE_FAIL",
        "HEATER_TIMEOUT",
        "HEATER_FAULT",
        "TEMP_OOR",
        "RH_OOR",
        "TEMP_OUT_OF_RANGE",
        "RH_OUT_OF_RANGE",
        "OVER_TEMP",
        "SD_FAIL",
        "SD_WRITE_FAIL",
        "LORA_FAIL",
        "LORA_TIMEOUT",
        "DISPLAY_FAIL",
    )
)

# Procedural / batch issues. The kiln is fine; the schedule needs the
# operator's attention. Amber banner.
NOTICE_CODES = frozenset(
    code.lower()
    for code in (
        "STAGE_GOAL_NOT_MET",
        "STAGE_GOAL_NOT_REACHED",
        "WATER_PAN_REMINDER",
    )
)

# Everything else is INFO and silently dropped from the dashboard banner.
# Examples seen in the firmware today:
#   stage_advance, equalizing_start, conditioning_start, run_complete


# Tier constants
TIER_FAULT = "fault"
TIER_NOTICE = "notice"
TIER_INFO = "info"


def classify(code: str, server_tier: str | None = None) -> str:
    """Classify an alert code into a tier.

    If the server provides a tier (from fault_details or /alerts rows),
    use it directly. Otherwise fall back to the local FAULT_CODES /
    NOTICE_CODES tables for backwards compatibility with older firmware.
    """
    if server_tier and server_tier in (TIER_FAULT, TIER_NOTICE, TIER_INFO):
        return server_tier
    if not code:
        return TIER_INFO
    c = code.lower()
    if c in FAULT_CODES:
        return TIER_FAULT
    if c in NOTICE_CODES:
        return TIER_NOTICE
    return TIER_INFO


def split_alerts(
    codes: Iterable[str],
    fault_details: Iterable[dict] | None = None,
) -> Tuple[List[str], List[str]]:
    """Return (faults, notices) lists, preserving order, dropping INFO codes.

    If fault_details is provided (from /status), use the server-provided
    tier for each code. Otherwise fall back to the local classification.
    """
    # Build a code -> tier lookup from fault_details if available
    tier_map: dict[str, str] = {}
    for fd in fault_details or []:
        c = fd.get("code")
        t = fd.get("tier")
        if c and t:
            tier_map[c] = t

    faults: List[str] = []
    notices: List[str] = []
    for code in codes or []:
        tier = classify(code, server_tier=tier_map.get(code))
        if tier == TIER_FAULT:
            faults.append(code)
        elif tier == TIER_NOTICE:
            notices.append(code)
    return faults, notices


def humanise(code: str) -> str:
    """Make an alert code more readable for display.

    `stage_goal_not_met` -> `Stage goal not met`
    """
    if not code:
        return ""
    return code.replace("_", " ").strip().capitalize()
