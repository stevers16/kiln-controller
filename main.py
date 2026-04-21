# main.py -- Kiln controller entry point
#
# Initialises all hardware modules, starts the WiFi AP, runs the
# asyncio HTTP REST API server, and executes the drying control loop.
# Runs at boot on the Raspberry Pi Pico 2 W.

# --- Kill switch check (set by boot.py) ---
# If boot.py was interrupted with Ctrl-C, or /no_main exists, bail out
# immediately and drop to the REPL without touching any hardware.
import builtins

if getattr(builtins, "_kiln_skip_main", False):
    print("main.py: skip flag set by boot.py -- not starting controller.")
    import sys

    sys.exit(0)

import machine
import network
import time
import gc
import uos
import asyncio

try:
    import ujson as json
except ImportError:
    import json

import config
from lib.sdcard import SDCard
from lib.logger import Logger
from lib.SHT31sensors import SHT31Sensors
from lib.current import CurrentMonitor
from lib.circulation import CirculationFans
from lib.exhaust import ExhaustFan
from lib.vents import Vents
from lib.heater import Heater
from lib.moisture import MoistureProbe
from lib.display import Display, Color
from lib.lora import LoRa
from lib.schedule import KilnSchedule

# -----------------------------------------------------------------------
# Module-level state
# -----------------------------------------------------------------------

_boot_ticks = time.ticks_ms()
_status_cache = {}
_test_results = []
_test_running = False
_cached_rpm = None

# Module instances (populated by init_hardware)
sdcard = None
logger = None
i2c0 = None
sensors = None
monitor_12v = None
monitor_5v = None
circulation = None
exhaust = None
vents = None
heater = None
moisture = None
display = None
lora = None
schedule = None

# -----------------------------------------------------------------------
# Alert tier table (authoritative source for severity classification)
# -----------------------------------------------------------------------
# Copied from KivyApp/kilnapp/alerts.py FAULT_CODES / NOTICE_CODES.
# Everything not listed defaults to "info".
ALERT_CODE_TIERS = {
    # Hardware / firmware faults (tier = "fault")
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
    # Procedural / batch notices (tier = "notice")
    "STAGE_GOAL_NOT_MET": "notice",
    "STAGE_GOAL_NOT_REACHED": "notice",
    "WATER_PAN_REMINDER": "notice",
}

# Built-in schedule filenames (read-only)
BUILTIN_SCHEDULES = (
    "maple_05in.json",
    "maple_1in.json",
    "beech_05in.json",
    "beech_1in.json",
)

# HTTP status text
HTTP_STATUS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    413: "Payload Too Large",
    500: "Internal Server Error",
    503: "Service Unavailable",
}

# -----------------------------------------------------------------------
# Hardware initialisation
# -----------------------------------------------------------------------


def _try_init(label, fn):
    """Run a hardware init step, logging any failure but not raising.

    Returns the result of fn() on success, or None on failure.
    Critical for boot resilience -- a single failed peripheral must not
    crash the whole controller into a boot loop.

    Prints a "starting" line before and a "done" / "FAILED" line after,
    so a hang can be diagnosed by reading the last printed line over USB.
    """
    print(f"[init] {label} ...", end="")
    # Force the print to flush immediately so it is visible even if the
    # next call hangs and never returns.
    try:
        import sys

        sys.stdout.flush()
    except Exception:
        pass
    t0 = time.ticks_ms()
    try:
        result = fn()
        elapsed = time.ticks_diff(time.ticks_ms(), t0)
        print(f" OK ({elapsed}ms)")
        return result
    except Exception as e:
        elapsed = time.ticks_diff(time.ticks_ms(), t0)
        print(f" FAILED after {elapsed}ms: {e}")
        if logger is not None:
            try:
                logger.event("main", f"{label} init failed: {e}", level="ERROR")
            except Exception:
                pass
        return None


def init_hardware():
    global sdcard, logger, i2c0, sensors, monitor_12v, monitor_5v
    global circulation, exhaust, vents, heater, moisture
    global display, lora, schedule

    # 1. SD card first -- logger depends on it
    sdcard = _try_init("SDCard", lambda: SDCard())

    # 2. Logger (must succeed -- needed by everything else for log events)
    if sdcard is not None:
        _logger = Logger(sdcard)
        logger = _logger
        # Open the persistent system log so boot events get persisted
        _try_init("system log", lambda: _logger.open_system_log())

    # 3. Shared I2C bus
    i2c0 = _try_init(
        "I2C0",
        lambda: machine.I2C(0, sda=machine.Pin(0), scl=machine.Pin(1), freq=100_000),
    )

    # 4. Sensors
    if i2c0 is not None:
        sensors = _try_init(
            "SHT31Sensors", lambda: SHT31Sensors(i2c=i2c0, logger=logger)
        )

    # 5. Current monitors
    if i2c0 is not None:
        monitor_12v = _try_init(
            "CurrentMonitor 12V",
            lambda: CurrentMonitor(i2c0, 0x40, "12V", logger=logger),
        )
        monitor_5v = _try_init(
            "CurrentMonitor 5V",
            lambda: CurrentMonitor(i2c0, 0x41, "5V", logger=logger),
        )

    # 6. Circulation fans
    circulation = _try_init(
        "CirculationFans",
        lambda: CirculationFans(current_monitor=monitor_12v, logger=logger),
    )

    # 7. Exhaust fan
    exhaust = _try_init("ExhaustFan", lambda: ExhaustFan(logger=logger))

    # 8. Vents
    vents = _try_init("Vents", lambda: Vents(current_monitor=monitor_5v, logger=logger))

    # 9. Heater
    heater = _try_init("Heater", lambda: Heater(logger=logger))

    # 10. Moisture probes + calibration
    moisture = _try_init("MoistureProbe", lambda: MoistureProbe(logger=logger))
    if moisture is not None and sdcard is not None:
        _load_calibration(moisture, sdcard)

    # 11. Display
    display = _try_init("Display", lambda: Display(timeout_s=config.DISPLAY_TIMEOUT_S))

    # 12. LoRa
    lora = _try_init("LoRa", lambda: LoRa(logger=logger))

    # 13. Schedule controller (requires all critical hardware -- skip if missing)
    required = (sdcard, sensors, moisture, heater, exhaust, circulation, vents, lora)
    if all(x is not None for x in required):
        schedule = _try_init(
            "KilnSchedule",
            lambda: KilnSchedule(
                sdcard=sdcard,
                sensors=sensors,
                moisture=moisture,
                heater=heater,
                exhaust=exhaust,
                circulation=circulation,
                vents=vents,
                lora=lora,
                logger=logger,
            ),
        )
    else:
        missing = [
            n
            for n, x in zip(
                (
                    "sdcard",
                    "sensors",
                    "moisture",
                    "heater",
                    "exhaust",
                    "circulation",
                    "vents",
                    "lora",
                ),
                required,
            )
            if x is None
        ]
        print(f"[init] KilnSchedule SKIPPED -- missing: {missing}")
        if logger is not None:
            logger.event(
                "main", f"KilnSchedule skipped, missing: {missing}", level="ERROR"
            )


# -----------------------------------------------------------------------
# Calibration loader
# -----------------------------------------------------------------------


def _load_calibration(moisture_probe, sd):
    text = sd.read_text("calibration.json")
    if text is None:
        return
    try:
        cal = json.loads(text)
        moisture_probe.set_calibration(
            channel_1_offset=cal.get("channel_1_offset", 0.0),
            channel_2_offset=cal.get("channel_2_offset", 0.0),
        )
    except Exception as e:
        print(f"[main] WARNING: Calibration load failed: {e}")


# -----------------------------------------------------------------------
# WiFi
# -----------------------------------------------------------------------


def start_wifi():
    """
    Bring up the WiFi interface based on config.WIFI_MODE.

    "ap"  -- Pico hosts its own access point (production)
    "sta" -- Pico joins an existing WiFi network (development)
    """
    mode = getattr(config, "WIFI_MODE", "ap").lower()
    if mode == "sta":
        return _start_wifi_sta()
    return _start_wifi_ap()


def _start_wifi_ap():
    # cyw43 driver on Pico W / Pico 2 W:
    #   - WPA2 password must be 8-63 ASCII characters
    #   - config must be set BEFORE active(True)
    #   - the AP can only be started/stopped once per reboot
    pw = config.AP_PASSWORD
    if pw and len(pw) < 8:
        print(
            f"[main] WARNING: AP_PASSWORD is {len(pw)} chars; "
            f"WPA2 requires 8-63. AP will fall back to open."
        )

    ap = network.WLAN(network.AP_IF)
    if pw and len(pw) >= 8:
        ap.config(essid=config.AP_SSID, password=pw)
        sec = "WPA2"
    else:
        ap.config(essid=config.AP_SSID)
        sec = "open"
    ap.active(True)

    for _ in range(50):
        if ap.active():
            break
        time.sleep_ms(100)

    ip = ap.ifconfig()[0]
    logger.event("main", f"WiFi AP active -- SSID={config.AP_SSID} IP={ip} ({sec})")
    return ap


def _start_wifi_sta():
    """
    Connect to an existing WiFi network as a station.
    Used in development so other machines on the LAN can hit the REST API.
    """
    ssid = config.STA_SSID
    pw = config.STA_PASSWORD

    sta = network.WLAN(network.STA_IF)
    sta.active(True)

    def _log(msg, level="INFO"):
        if logger is not None:
            logger.event("main", msg, level=level)
        else:
            print(f"[main] {msg}")

    if sta.isconnected():
        # Already connected from a previous boot? Reuse it.
        ip = sta.ifconfig()[0]
        _log(f"WiFi STA already connected -- SSID={ssid} IP={ip}")
        return sta

    print(f"[main] Connecting to WiFi SSID={ssid} ...")
    sta.connect(ssid, pw)

    # Wait up to 20 seconds for association + DHCP
    for _ in range(200):
        if sta.isconnected():
            break
        time.sleep_ms(100)

    if not sta.isconnected():
        status = sta.status()
        _log(f"WiFi STA failed to connect to {ssid} (status={status})", level="ERROR")
        return sta

    cfg = sta.ifconfig()
    ip = cfg[0]
    gateway = cfg[2]
    _log(f"WiFi STA connected -- SSID={ssid} IP={ip} GW={gateway}")
    return sta


# -----------------------------------------------------------------------
# RTC sync
# -----------------------------------------------------------------------


def set_rtc(unix_ts):
    t = time.localtime(unix_ts)
    machine.RTC().datetime((t[0], t[1], t[2], t[6], t[3], t[4], t[5], 0))


def _rtc_is_set():
    return time.localtime()[0] >= 2024


# -----------------------------------------------------------------------
# Uptime helper
# -----------------------------------------------------------------------


def _uptime_s():
    return time.ticks_diff(time.ticks_ms(), _boot_ticks) // 1000


# -----------------------------------------------------------------------
# Status cache
# -----------------------------------------------------------------------


def _collect_module_faults():
    """Poll all modules for fault state and return aggregated results.

    Returns (codes, details) where:
    - codes: list of fault code strings (deduped, faults first)
    - details: list of {code, source, message, tier} dicts
    """
    faults = []  # list of (code, source, message, tier)
    for source, mod in (
        ("circulation", circulation),
        ("exhaust", exhaust),
        ("vents", vents),
        ("heater", heater),
        ("sensors", sensors),
        ("moisture", moisture),
        ("current_12v", monitor_12v),
        ("current_5v", monitor_5v),
        ("lora", lora),
        ("sdcard", sdcard),
        ("logger", logger),
        ("display", display),
    ):
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

    # Build deduped code list and detail list
    seen = set()
    codes = []
    details = []
    # Sort: faults first, then notices, then info
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


def _update_status_cache():
    """Build the status cache, tolerating any missing modules."""
    global _status_cache

    # Schedule may be None if init failed -- use an empty dict so .get() works
    s = {}
    sensor_data = {}
    mc_data = {}
    total_h = None
    stage_min_h = None
    active_alerts = []
    if schedule is not None:
        try:
            s = schedule.status() or {}
        except Exception as e:
            print(f"[main] schedule.status() failed: {e}")
        try:
            sensor_data = schedule._last_sensor_read or {}
        except Exception:
            pass
        try:
            mc_data = schedule._last_mc_read or {}
        except Exception:
            pass
        try:
            if schedule.is_running and schedule._run_start_ms:
                elapsed = time.ticks_diff(time.ticks_ms(), schedule._run_start_ms)
                total_h = round(elapsed / 3_600_000, 2)
        except Exception:
            pass
        try:
            if schedule._schedule and schedule.is_running:
                stages = schedule._schedule.get("stages", [])
                idx = schedule._stage_index
                if 0 <= idx < len(stages):
                    stage_min_h = stages[idx].get("min_duration_h")
        except Exception:
            pass
        try:
            now = time.ticks_ms()
            for atype, ts in schedule._last_alert_ts.items():
                if time.ticks_diff(now, ts) < 30 * 60_000:
                    active_alerts.append(atype)
        except Exception:
            pass

    # Idle direct reads: when no run is active the schedule.tick() loop is
    # not refreshing the sensor / moisture caches, so /status would otherwise
    # return None for every temperature, humidity, and MC field. Read the
    # sensors directly here so the Kivy dashboard (and any other client) can
    # see live values at idle. This block is skipped while a run is active so
    # the schedule's cached reads remain authoritative during a drying run.
    run_active = bool(s.get("running", False))
    if not run_active:
        if sensors is not None:
            try:
                idle_sensor = sensors.read()
                if idle_sensor:
                    sensor_data = idle_sensor
                    # Lumber temp/RH are read from `s` further down via
                    # actual_temp_c / actual_rh_pct - inject them so the
                    # idle path uses the same field names.
                    s["actual_temp_c"] = idle_sensor.get("temp_lumber")
                    s["actual_rh_pct"] = idle_sensor.get("rh_lumber")
            except Exception as e:
                print(f"[main] idle sensors.read() failed: {e}")
        if moisture is not None:
            try:
                idle_mc = moisture.read()
                if idle_mc:
                    mc_data = idle_mc
                    s["actual_mc_maple"] = idle_mc.get("ch1_mc_pct")
                    s["actual_mc_beech"] = idle_mc.get("ch2_mc_pct")
            except Exception as e:
                print(f"[main] idle moisture.read() failed: {e}")

    cur_12v = None
    cur_5v = None
    if monitor_12v is not None:
        try:
            r12 = monitor_12v.read()
            if r12:
                cur_12v = r12.get("current_mA")
        except Exception:
            pass
    if monitor_5v is not None:
        try:
            r5 = monitor_5v.read()
            if r5:
                cur_5v = r5.get("current_mA")
        except Exception:
            pass

    # --- Fault aggregator ---
    # Poll all modules for fault state and merge with schedule alerts.
    # Runs in both idle and run-active paths (spec normative req #3).
    mod_codes, mod_details = _collect_module_faults()

    # Merge schedule-emitted alerts (from _last_alert_ts) into fault_details.
    # Schedule alerts carry their own codes; look up tier from the static table.
    for acode in active_alerts:
        code_upper = acode.upper()
        tier = ALERT_CODE_TIERS.get(code_upper, ALERT_CODE_TIERS.get(acode, "info"))
        if code_upper not in (d["code"] for d in mod_details):
            mod_details.append({
                "code": code_upper,
                "source": "schedule",
                "message": "",
                "tier": tier,
            })
        if code_upper not in mod_codes:
            mod_codes.append(code_upper)

    # Rebuild active_alerts: combine module faults + schedule alerts, deduped.
    all_alert_codes = mod_codes

    _status_cache = {
        "ts": time.time() if _rtc_is_set() else 0,
        "run_active": s.get("running", False),
        "active_run_id": logger.run_id if logger is not None else None,
        "cooldown": s.get("cooldown", False),
        "schedule_name": s.get("schedule_name"),
        "stage_index": s.get("stage_index"),
        "stage_name": s.get("stage_name"),
        "stage_type": s.get("stage_type"),
        "stage_elapsed_h": s.get("stage_elapsed_h"),
        "stage_min_h": stage_min_h,
        "total_elapsed_h": total_h,
        "target_temp_c": s.get("target_temp_c"),
        "target_rh_pct": s.get("target_rh_pct"),
        "target_mc_pct": s.get("target_mc_pct"),
        "temp_lumber": s.get("actual_temp_c"),
        "rh_lumber": s.get("actual_rh_pct"),
        "temp_intake": sensor_data.get("temp_intake"),
        "rh_intake": sensor_data.get("rh_intake"),
        "mc_channel_1": s.get("actual_mc_maple"),
        "mc_channel_2": s.get("actual_mc_beech"),
        "mc_resistance_1": _safe_int(mc_data.get("ch1_ohms")),
        "mc_resistance_2": _safe_int(mc_data.get("ch2_ohms")),
        "heater_on": s.get("heater_on", False),
        "vent_open": s.get("vents_open", False),
        "vent_reason": s.get("vent_reason"),
        "exhaust_fan_pct": exhaust.speed_pct if exhaust else 0,
        "exhaust_fan_rpm": _cached_rpm,
        "circ_fan_on": circulation.is_running if circulation else False,
        "circ_fan_pct": circulation.speed_pct if circulation else 0,
        "current_12v_ma": cur_12v,
        "current_5v_ma": cur_5v,
        "active_alerts": all_alert_codes,
        "fault_details": mod_details,
        "degraded_modules": _missing_modules(),
    }


def _missing_modules():
    """Return a list of module names that failed to initialize."""
    return [
        n for n, x in (
            ("sdcard", sdcard), ("sensors", sensors),
            ("monitor_12v", monitor_12v), ("monitor_5v", monitor_5v),
            ("circulation", circulation), ("exhaust", exhaust),
            ("vents", vents), ("heater", heater),
            ("moisture", moisture), ("display", display),
            ("lora", lora), ("schedule", schedule),
        )
        if x is None
    ]


def _safe_int(val):
    if val is None:
        return None
    try:
        return int(val)
    except Exception:
        return None


# -----------------------------------------------------------------------
# Display pages
# -----------------------------------------------------------------------


def _fmt(val, unit="", decimals=1):
    if val is None:
        return "--"
    if isinstance(val, float):
        return f"{val:.{decimals}f}{unit}"
    return f"{val}{unit}"


def render_status_page():
    display.clear(Color.BLACK)
    s = _status_cache
    if s.get("run_active"):
        display.draw_text(10, 10, s.get("stage_name") or "Running", Color.WHITE, 24)
        display.draw_text(10, 40, f"Type: {s.get('stage_type', '--')}", Color.WHITE, 16)
        display.draw_text(
            10, 65, f"Elapsed: {_fmt(s.get('stage_elapsed_h'), 'h')}", Color.WHITE, 16
        )
    else:
        display.draw_text(10, 10, "Idle", Color.WHITE, 32)

    y = 100
    indicators = []
    if s.get("heater_on"):
        indicators.append("HTR:ON")
    else:
        indicators.append("HTR:OFF")
    if s.get("vent_open"):
        indicators.append("VENT:OPEN")
    else:
        indicators.append("VENT:SHUT")
    if s.get("circ_fan_on"):
        indicators.append(f"FAN:{s.get('circ_fan_pct', 0)}%")
    else:
        indicators.append("FAN:OFF")
    display.draw_text(10, y, "  ".join(indicators), Color.YELLOW, 16)


def render_sensors_page():
    display.clear(Color.BLACK)
    s = _status_cache
    display.draw_text(10, 10, "Sensors", Color.WHITE, 24)

    display.draw_text(10, 45, "Lumber:", Color.GREEN, 16)
    display.draw_text(
        10,
        65,
        f"  T: {_fmt(s.get('temp_lumber'), ' C')} / {_fmt(s.get('target_temp_c'), ' C')}",
        Color.WHITE,
        16,
    )
    display.draw_text(
        10,
        85,
        f"  RH: {_fmt(s.get('rh_lumber'), '%')} / {_fmt(s.get('target_rh_pct'), '%')}",
        Color.WHITE,
        16,
    )

    display.draw_text(10, 115, "Intake:", Color.GREEN, 16)
    display.draw_text(
        10, 135, f"  T: {_fmt(s.get('temp_intake'), ' C')}", Color.WHITE, 16
    )
    display.draw_text(
        10, 155, f"  RH: {_fmt(s.get('rh_intake'), '%')}", Color.WHITE, 16
    )


def render_moisture_page():
    display.clear(Color.BLACK)
    s = _status_cache
    display.draw_text(10, 10, "Moisture", Color.WHITE, 24)

    display.draw_text(
        10, 45, f"Ch1: {_fmt(s.get('mc_channel_1'), '% MC')}", Color.WHITE, 16
    )
    r1 = s.get("mc_resistance_1")
    if r1 is not None:
        display.draw_text(10, 65, f"  R: {r1} ohm", Color.WHITE, 16)

    display.draw_text(
        10, 95, f"Ch2: {_fmt(s.get('mc_channel_2'), '% MC')}", Color.WHITE, 16
    )
    r2 = s.get("mc_resistance_2")
    if r2 is not None:
        display.draw_text(10, 115, f"  R: {r2} ohm", Color.WHITE, 16)

    target = s.get("target_mc_pct")
    if target is not None:
        display.draw_text(10, 150, f"Target: {_fmt(target, '% MC')}", Color.YELLOW, 16)


def render_system_page():
    display.clear(Color.BLACK)
    s = _status_cache
    display.draw_text(10, 10, "System", Color.WHITE, 24)

    display.draw_text(
        10, 45, f"12V: {_fmt(s.get('current_12v_ma'), ' mA', 0)}", Color.WHITE, 16
    )
    display.draw_text(
        10, 65, f" 5V: {_fmt(s.get('current_5v_ma'), ' mA', 0)}", Color.WHITE, 16
    )

    up = _uptime_s()
    h = up // 3600
    m = (up % 3600) // 60
    display.draw_text(10, 95, f"Uptime: {h}h {m}m", Color.WHITE, 16)

    sd_ok = sdcard.is_mounted() if sdcard else False
    display.draw_text(
        10,
        120,
        f"SD: {'OK' if sd_ok else 'NONE'}",
        Color.GREEN if sd_ok else Color.RED,
        16,
    )

    tx = lora.tx_count if lora else 0
    display.draw_text(10, 145, f"LoRa TX: {tx}", Color.WHITE, 16)


# -----------------------------------------------------------------------
# HTTP server
# -----------------------------------------------------------------------


async def send_json(writer, data, status=200):
    body = json.dumps(data)
    status_text = HTTP_STATUS.get(status, "OK")
    header = (
        f"HTTP/1.1 {status} {status_text}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    )
    writer.write(header.encode())
    writer.write(body.encode())
    await writer.drain()


async def send_error(writer, message, status=400):
    await send_json(writer, {"error": message}, status)


def _parse_qs(qs):
    params = {}
    if not qs:
        return params
    for pair in qs.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            params[k] = v
    return params


async def handle_request(reader, writer):
    try:
        # Read request line
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        if not line:
            writer.close()
            await writer.wait_closed()
            return
        request_line = line.decode().strip()
        parts = request_line.split(" ")
        if len(parts) < 2:
            writer.close()
            await writer.wait_closed()
            return
        method = parts[0]
        raw_path = parts[1]

        # Split path and query string
        if "?" in raw_path:
            path, qs = raw_path.split("?", 1)
        else:
            path, qs = raw_path, ""
        query = _parse_qs(qs)

        # Read headers
        headers = {}
        while True:
            hline = await asyncio.wait_for(reader.readline(), timeout=5)
            if not hline or hline == b"\r\n" or hline == b"\n":
                break
            decoded = hline.decode().strip()
            if ":" in decoded:
                hk, hv = decoded.split(":", 1)
                headers[hk.strip().lower()] = hv.strip()

        # Read body if Content-Length present
        body = b""
        cl = headers.get("content-length")
        if cl:
            cl = int(cl)
            if cl > 524288:
                await send_error(writer, "Request body too large", 413)
                writer.close()
                await writer.wait_closed()
                return
            body = await asyncio.wait_for(reader.readexactly(cl), timeout=30)

        # Auth check (skip for /health and /version)
        if path not in ("/health", "/version"):
            api_key = headers.get("x-kiln-key", "")
            if api_key != config.API_KEY:
                await send_error(writer, "unauthorized", 401)
                writer.close()
                await writer.wait_closed()
                return

        # Route to handler
        await _route(method, path, query, body, writer)

    except asyncio.TimeoutError:
        pass
    except Exception as e:
        try:
            await send_error(writer, f"Internal error: {e}", 500)
        except Exception:
            pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        gc.collect()


async def _route(method, path, query, body, writer):
    # Exact-match routes
    if method == "GET" and path == "/health":
        return await handle_health(writer)
    if method == "GET" and path == "/status":
        return await handle_status(writer)
    if method == "GET" and path == "/version":
        return await handle_version(writer)
    if method == "POST" and path == "/time":
        return await handle_time(body, writer)
    if method == "GET" and path == "/history":
        return await handle_history(query, writer)
    if method == "GET" and path == "/alerts":
        return await handle_alerts(query, writer)
    if method == "GET" and path == "/runs":
        return await handle_runs(writer)
    if method == "GET" and path == "/sdcard/info":
        return await handle_sdcard_info(writer)
    if method == "GET" and path == "/calibration":
        return await handle_calibration_get(writer)
    if method == "POST" and path == "/calibration":
        return await handle_calibration_post(body, writer)
    if method == "GET" and path == "/moisture/live":
        return await handle_moisture_live(writer)
    if method == "POST" and path == "/run/start":
        return await handle_run_start(body, writer)
    if method == "POST" and path == "/run/stop":
        return await handle_run_stop(body, writer)
    if method == "POST" and path == "/run/advance":
        return await handle_run_advance(writer)
    if method == "POST" and path == "/run/shutdown":
        return await handle_run_shutdown(writer)
    if method == "POST" and path == "/test/run":
        return await handle_test_run(writer)
    if method == "GET" and path == "/test/status":
        return await handle_test_status(writer)
    if method == "GET" and path == "/modules":
        return await handle_modules_list(writer)

    # Prefix-match routes with path parameters
    if method == "GET" and path == "/schedules":
        return await handle_schedules_list(writer)
    if method == "GET" and path.startswith("/schedules/"):
        filename = path[len("/schedules/") :]
        return await handle_schedule_get(filename, writer)
    if method == "PUT" and path.startswith("/schedules/"):
        filename = path[len("/schedules/") :]
        return await handle_schedule_put(filename, body, writer)
    if method == "DELETE" and path.startswith("/schedules/"):
        filename = path[len("/schedules/") :]
        return await handle_schedule_delete(filename, writer)

    if method == "GET" and path.startswith("/logs/"):
        # /logs/{run_id}/events
        parts = path[len("/logs/") :].split("/")
        run_id = parts[0] if parts else ""
        return await handle_logs_events(run_id, writer)
    if method == "DELETE" and path.startswith("/logs/"):
        run_id = path[len("/logs/") :].rstrip("/")
        return await handle_logs_delete(run_id, writer)

    if method == "PUT" and path.startswith("/modules/"):
        mod_path = path[len("/modules/") :]
        return await handle_module_upload(mod_path, body, writer)

    await send_error(writer, "Not found", 404)


# -----------------------------------------------------------------------
# Endpoint handlers
# -----------------------------------------------------------------------


async def handle_health(writer):
    await send_json(
        writer,
        {
            "status": "ok",
            "uptime_s": _uptime_s(),
            "free_mem_bytes": gc.mem_free(),
            "sdcard_mounted": sdcard.is_mounted(),
            "rtc_set": _rtc_is_set(),
            "run_active": schedule.is_running if schedule else False,
            "firmware_version": config.VERSION,
        },
    )


async def handle_status(writer):
    await send_json(writer, _status_cache)


async def handle_version(writer):
    info = uos.uname()
    mp_ver = info.version if hasattr(info, "version") else "unknown"
    board = info.machine if hasattr(info, "machine") else "unknown"
    await send_json(
        writer,
        {
            "firmware_version": config.VERSION,
            "micropython_version": mp_ver,
            "board": board,
        },
    )


async def handle_time(body, writer):
    try:
        data = json.loads(body)
        ts = data["ts"]
        set_rtc(ts)
        logger.event("main", f"RTC set to {ts}")
        await send_json(writer, {"ok": True, "ts": ts})
    except Exception as e:
        await send_error(writer, f"Invalid request: {e}", 400)


async def handle_history(query, writer):
    if not sdcard.is_mounted():
        await send_error(writer, "SD card not mounted", 503)
        return

    resolution = int(query.get("resolution", "1"))
    if resolution < 1:
        resolution = 1
    req_fields = query.get("fields", "")

    # Find the data CSV for the requested run (or current)
    run_id = query.get("run", "")
    csv_file = _find_run_file("data_", ".csv", run_id)
    if csv_file is None:
        await send_error(writer, "No log data found", 404)
        return

    csv_path = f"{sdcard.mount_point}/{csv_file}"
    try:
        f = open(csv_path, "r")
    except Exception:
        await send_error(writer, "Cannot read log file", 404)
        return

    try:
        # Read CSV header
        header_line = f.readline().strip()
        if not header_line:
            f.close()
            await send_error(writer, "Empty log file", 404)
            return
        all_fields = header_line.split(",")

        # Determine which fields to return
        if req_fields:
            out_fields = [fld for fld in req_fields.split(",") if fld in all_fields]
            if not out_fields:
                out_fields = all_fields
            field_indices = [all_fields.index(fld) for fld in out_fields]
        else:
            out_fields = all_fields
            field_indices = list(range(len(all_fields)))

        # Stream response -- write header then rows
        fields_json = json.dumps(out_fields)
        status_text = HTTP_STATUS[200]
        resp_header = (
            f"HTTP/1.1 200 {status_text}\r\n"
            f"Content-Type: application/json\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        writer.write(resp_header.encode())
        writer.write(f'{{"fields":{fields_json},"rows":['.encode())
        await writer.drain()

        row_count = 0
        line_num = 0
        first = True
        for line in f:
            line = line.strip()
            if not line:
                continue
            line_num += 1
            if line_num % resolution != 0:
                continue

            cols = line.split(",")
            row = []
            for idx in field_indices:
                if idx < len(cols):
                    val = cols[idx]
                    # Try to convert numeric values
                    if val == "":
                        row.append(None)
                    else:
                        try:
                            if "." in val:
                                row.append(float(val))
                            else:
                                row.append(int(val))
                        except ValueError:
                            row.append(val)
                else:
                    row.append(None)

            prefix = "" if first else ","
            writer.write((prefix + json.dumps(row)).encode())
            first = False
            row_count += 1

            # Yield periodically to avoid blocking
            if row_count % 50 == 0:
                await writer.drain()
                gc.collect()

        run_name = csv_file.replace("data_", "").replace(".csv", "")
        writer.write(f'],"run":"{run_name}","row_count":{row_count}}}'.encode())
        await writer.drain()

    finally:
        f.close()


async def handle_alerts(query, writer):
    limit = int(query.get("limit", "50"))
    level_filter = query.get("level", "")
    run_id = query.get("run", "")

    event_file = None
    if sdcard is not None and sdcard.is_mounted():
        event_file = _find_run_file("event_", ".txt", run_id)

    alerts = []
    if event_file:
        path = f"{sdcard.mount_point}/{event_file}"
        try:
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    # Only include WARN and ERROR lines
                    if "[WARN" not in line and "[ERROR" not in line:
                        continue
                    if level_filter:
                        if level_filter == "WARN" and "[WARN" not in line:
                            continue
                        if level_filter == "ERROR" and "[ERROR" not in line:
                            continue

                    alert = _parse_event_line(line)
                    if alert:
                        alerts.append(alert)
        except Exception:
            pass

    # Inject any currently active faults from the status cache that are
    # not already in the event log. This covers faults detected by
    # check_health() after the event file was closed (or between ticks).
    existing_codes = {a.get("code") for a in alerts if a.get("code")}
    for fd in _status_cache.get("fault_details", []):
        code = fd.get("code")
        if code and code not in existing_codes:
            tier = fd.get("tier", "info")
            # Only inject faults and notices (not info lifecycle events)
            if tier in ("fault", "notice"):
                alerts.append({
                    "ts": _status_cache.get("ts", 0),
                    "level": "ERROR" if tier == "fault" else "WARN",
                    "tier": tier,
                    "source": fd.get("source", ""),
                    "message": fd.get("message", ""),
                    "code": code,
                })
                existing_codes.add(code)

    # Return newest first, up to limit
    alerts.reverse()
    alerts = alerts[:limit]
    await send_json(writer, {"alerts": alerts, "count": len(alerts)})


def _parse_event_line(line):
    try:
        # Format: 2026-03-28 14:30:05 [WARN ] [schedule   ] message
        ts_str = line[:19]
        # Extract level
        level_start = line.index("[", 20)
        level_end = line.index("]", level_start)
        level = line[level_start + 1 : level_end].strip()
        # Extract source
        src_start = line.index("[", level_end + 1)
        src_end = line.index("]", src_start)
        source = line[src_start + 1 : src_end].strip()
        # Message is everything after
        message = line[src_end + 2 :].strip()
        # Try to extract alert code (messages starting with known patterns)
        code = None
        if "ALERT;" in message:
            parts = message.split(";")
            if len(parts) >= 2:
                code = parts[1]
        elif "_" in message.split(":")[0] and " " not in message.split(":")[0]:
            code = message.split(":")[0]

        # Build timestamp (best effort)
        ts = 0
        try:
            ts = _parse_ts_str(ts_str)
        except Exception:
            pass

        # Look up tier from code; case-insensitive match against ALERT_CODE_TIERS
        tier = "info"
        if code:
            tier = ALERT_CODE_TIERS.get(code.upper(), ALERT_CODE_TIERS.get(code, "info"))

        return {
            "ts": ts,
            "level": level,
            "tier": tier,
            "source": source,
            "message": message,
            "code": code,
        }
    except Exception:
        return None


def _parse_ts_str(ts_str):
    # "2026-03-28 14:30:05" -> unix timestamp
    # Minimal parser for MicroPython
    d = ts_str.split(" ")
    ymd = d[0].split("-")
    hms = d[1].split(":")
    t = (
        int(ymd[0]),
        int(ymd[1]),
        int(ymd[2]),
        int(hms[0]),
        int(hms[1]),
        int(hms[2]),
        0,
        0,
    )
    return time.mktime(t)


async def handle_runs(writer):
    if not sdcard.is_mounted():
        await send_json(writer, {"runs": []})
        return

    files = sdcard.listdir()
    # Gather run IDs from data CSV filenames; track which runs have a
    # cached stats file so we can skip line-counting.
    run_ids = []
    stats_set = set()
    for f in files:
        if f.startswith("data_") and f.endswith(".csv"):
            rid = f.replace("data_", "").replace(".csv", "")
            run_ids.append(rid)
        elif f.startswith("stats_") and f.endswith(".json"):
            stats_set.add(f[len("stats_"):-len(".json")])
    run_ids.sort(reverse=True)

    active_rid = logger.run_id if logger is not None else None

    runs = []
    for rid in run_ids:
        event_name = f"event_{rid}.txt"
        data_name = f"data_{rid}.csv"
        event_count = 0
        data_rows = 0
        size = 0
        data_mtime = 0  # epoch-2000 seconds; 0 if stat fails
        event_mtime = 0

        try:
            stat = uos.stat(f"{sdcard.mount_point}/{data_name}")
            size += stat[6]
            data_mtime = stat[8]
        except Exception:
            pass

        try:
            stat = uos.stat(f"{sdcard.mount_point}/{event_name}")
            size += stat[6]
            event_mtime = stat[8]
        except Exception:
            pass

        # Row/event counts: prefer live counters for the active run,
        # then the cached stats file, otherwise leave at 0 (legacy run
        # without a stats file - acceptable; the Pi4 daemon will have
        # exact counts via SQLite).
        if rid == active_rid and logger is not None:
            data_rows = logger.data_rows
            event_count = logger.event_count
        elif rid in stats_set:
            try:
                with open(f"{sdcard.mount_point}/stats_{rid}.json", "r") as f:
                    stats = json.loads(f.read())
                data_rows = int(stats.get("data_rows", 0))
                event_count = int(stats.get("event_count", 0))
            except Exception:
                pass

        # Format started_at from run ID (YYYYMMDD_HHMM)
        started = rid
        if len(rid) >= 13:
            started = f"{rid[0:4]}-{rid[4:6]}-{rid[6:8]} {rid[9:11]}:{rid[11:13]}"

        # mtime is the last write to whichever file is newer. Use it as
        # the run's "ended at". Only format it as wall-clock when the
        # Pico RTC was set (year >= 2024); otherwise leave blank so the
        # client knows not to trust it.
        mtime = max(data_mtime, event_mtime)
        ended_at_str = ""
        if mtime:
            try:
                lt = time.localtime(mtime)
                if lt[0] >= 2024:
                    ended_at_str = (
                        f"{lt[0]:04d}-{lt[1]:02d}-{lt[2]:02d} "
                        f"{lt[3]:02d}:{lt[4]:02d}"
                    )
            except Exception:
                pass

        runs.append(
            {
                "id": rid,
                "started_at_str": started,
                "ended_at_str": ended_at_str,
                "mtime": mtime,
                "event_log": event_name,
                "data_csv": data_name,
                "data_rows": data_rows,
                "event_count": event_count,
                "size_bytes": size,
            }
        )

    # Sort by mtime descending so the most recently written run is first.
    # Zero mtimes sort last (unknown/failed stat). Ties fall back to rid
    # descending, which is chronological for dated rids.
    runs.sort(key=lambda r: (r.get("mtime", 0), r.get("id", "")), reverse=True)

    await send_json(writer, {"runs": runs})


async def handle_logs_events(run_id, writer):
    if not sdcard.is_mounted():
        await send_error(writer, "SD card not mounted", 503)
        return

    event_file = f"event_{run_id}.txt"
    path = f"{sdcard.mount_point}/{event_file}"
    try:
        lines = []
        with open(path, "r") as f:
            for line in f:
                lines.append(line.strip())
        await send_json(
            writer,
            {
                "run": run_id,
                "lines": lines,
                "line_count": len(lines),
            },
        )
    except OSError:
        await send_error(writer, "Run not found", 404)


async def handle_logs_delete(run_id, writer):
    if not sdcard.is_mounted():
        await send_error(writer, "SD card not mounted", 503)
        return

    # Prevent deleting active run
    if schedule.is_running and logger.run_active:
        try:
            suffix = logger._file_suffix() if hasattr(logger, "_file_suffix") else ""
            # Check if run_id matches current active log
            if logger._event_file:
                active_name = (
                    str(logger._event_file.name)
                    if hasattr(logger._event_file, "name")
                    else ""
                )
                if run_id in active_name:
                    await send_error(writer, "Cannot delete active run", 409)
                    return
        except Exception:
            pass

    deleted = []
    event_file = f"event_{run_id}.txt"
    data_file = f"data_{run_id}.csv"
    found = False
    for fname in (event_file, data_file):
        try:
            uos.remove(f"{sdcard.mount_point}/{fname}")
            deleted.append(fname)
            found = True
        except OSError:
            pass

    if not found:
        await send_error(writer, "Run not found", 404)
        return

    await send_json(writer, {"ok": True, "deleted": deleted})


async def handle_sdcard_info(writer):
    if not sdcard.is_mounted():
        await send_json(writer, {"mounted": False})
        return

    try:
        stat = uos.statvfs(sdcard.mount_point)
        block_size = stat[0]
        total_blocks = stat[2]
        free_blocks = stat[3]
        total = block_size * total_blocks
        free = block_size * free_blocks
        used = total - free

        files = sdcard.listdir()
        await send_json(
            writer,
            {
                "mounted": True,
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
                "file_count": len(files),
            },
        )
    except Exception as e:
        await send_json(writer, {"mounted": True, "error": str(e)})


async def handle_schedules_list(writer):
    if not sdcard.is_mounted():
        await send_json(writer, {"schedules": []})
        return

    files = sdcard.listdir("schedules")
    schedules = []
    for fname in files:
        if not fname.endswith(".json"):
            continue
        text = sdcard.read_text(f"schedules/{fname}")
        if text is None:
            continue
        try:
            data = json.loads(text)
            info = {
                "filename": fname,
                "name": data.get("name", fname),
                "species": data.get("species", ""),
                "thickness_in": data.get("thickness_in"),
                "stage_count": len(data.get("stages", [])),
                "builtin": fname in BUILTIN_SCHEDULES,
            }
            try:
                stat = uos.stat(f"{sdcard.mount_point}/schedules/{fname}")
                info["size_bytes"] = stat[6]
            except Exception:
                info["size_bytes"] = len(text)
            schedules.append(info)
        except Exception:
            pass

    await send_json(writer, {"schedules": schedules})


async def handle_schedule_get(filename, writer):
    text = sdcard.read_text(f"schedules/{filename}")
    if text is None:
        await send_error(writer, "Schedule not found", 404)
        return
    try:
        data = json.loads(text)
        await send_json(writer, data)
    except Exception:
        await send_error(writer, "Invalid schedule file", 500)


async def handle_schedule_put(filename, body, writer):
    if not sdcard.is_mounted():
        await send_error(writer, "SD card not mounted", 503)
        return
    if filename in BUILTIN_SCHEDULES:
        await send_error(writer, "Cannot modify built-in schedule", 403)
        return

    try:
        data = json.loads(body)
    except Exception:
        await send_error(writer, "Invalid JSON", 400)
        return

    # Validate required fields
    if "name" not in data or "species" not in data or "stages" not in data:
        await send_error(writer, "Missing required fields: name, species, stages", 400)
        return
    stages = data["stages"]
    if not stages or not isinstance(stages, list):
        await send_error(writer, "At least one stage required", 400)
        return
    for i, stage in enumerate(stages):
        for key in (
            "name",
            "stage_type",
            "target_temp_c",
            "target_rh_pct",
            "min_duration_h",
        ):
            if key not in stage:
                await send_error(writer, f"Stage {i} missing field: {key}", 400)
                return
        stype = stage["stage_type"]
        if stype == "drying":
            if stage.get("target_mc_pct") is None:
                await send_error(
                    writer, f"Stage {i}: drying stage requires target_mc_pct", 400
                )
                return
        elif stype in ("equalizing", "conditioning"):
            if stage.get("target_mc_pct") is not None:
                await send_error(
                    writer,
                    f"Stage {i}: {stype} stage must have null target_mc_pct",
                    400,
                )
                return

    # Write to SD
    path = f"{sdcard.mount_point}/schedules/{filename}"
    try:
        with open(path, "w") as f:
            f.write(json.dumps(data))
        await send_json(
            writer,
            {
                "ok": True,
                "filename": filename,
                "stage_count": len(stages),
            },
        )
    except Exception as e:
        await send_error(writer, f"Write failed: {e}", 500)


async def handle_schedule_delete(filename, writer):
    if filename in BUILTIN_SCHEDULES:
        await send_error(writer, "Cannot delete built-in schedule", 403)
        return
    try:
        uos.remove(f"{sdcard.mount_point}/schedules/{filename}")
        await send_json(writer, {"ok": True, "deleted": filename})
    except OSError:
        await send_error(writer, "Schedule not found", 404)


async def handle_calibration_get(writer):
    defaults = {
        "channel_1_offset": 0.0,
        "channel_2_offset": 0.0,
        "calibrated_at": None,
        "source": "defaults",
    }
    text = sdcard.read_text("calibration.json")
    if text is None:
        await send_json(writer, defaults)
        return
    try:
        cal = json.loads(text)
        cal["source"] = "calibration.json"
        await send_json(writer, cal)
    except Exception:
        await send_json(writer, defaults)


async def handle_calibration_post(body, writer):
    if not sdcard.is_mounted():
        await send_error(writer, "SD card not mounted", 503)
        return
    try:
        data = json.loads(body)
        ch1 = float(data.get("channel_1_offset", 0.0))
        ch2 = float(data.get("channel_2_offset", 0.0))
    except Exception as e:
        await send_error(writer, f"Invalid request: {e}", 400)
        return

    ts = time.time() if _rtc_is_set() else 0
    cal = {
        "channel_1_offset": ch1,
        "channel_2_offset": ch2,
        "calibrated_at": ts,
    }

    path = f"{sdcard.mount_point}/calibration.json"
    try:
        with open(path, "w") as f:
            f.write(json.dumps(cal))
    except Exception as e:
        await send_error(writer, f"Write failed: {e}", 500)
        return

    # Apply to running moisture probe
    moisture.set_calibration(channel_1_offset=ch1, channel_2_offset=ch2)
    logger.event("main", f"Calibration saved: ch1={ch1} ch2={ch2}")

    await send_json(writer, {"ok": True, "calibrated_at": ts})


async def handle_moisture_live(writer):
    try:
        # Get current lumber temperature for correction
        sensor_data = sensors.read()
        temp_c = None
        if sensor_data:
            temp_c = sensor_data.get("temp_lumber")

        if temp_c is not None:
            reading = moisture.read_with_temp_correction(temp_c)
            temp_corrected = True
        else:
            reading = moisture.read()
            temp_corrected = False

        await send_json(
            writer,
            {
                "channel_1": {
                    "mc_pct": reading.get("ch1_mc_pct"),
                    "resistance_ohms": _safe_int(reading.get("ch1_ohms")),
                    "temp_corrected": temp_corrected,
                    "temp_c": temp_c,
                },
                "channel_2": {
                    "mc_pct": reading.get("ch2_mc_pct"),
                    "resistance_ohms": _safe_int(reading.get("ch2_ohms")),
                    "temp_corrected": temp_corrected,
                    "temp_c": temp_c,
                },
            },
        )
    except Exception as e:
        await send_error(writer, f"Read failed: {e}", 500)


async def handle_run_start(body, writer):
    if schedule.is_running:
        await send_error(writer, "Run already active", 409)
        return
    if not sdcard.is_mounted():
        await send_error(writer, "SD card not mounted", 503)
        return

    try:
        data = json.loads(body)
        sched_file = data.get("schedule", config.DEFAULT_SCHEDULE)
    except Exception:
        await send_error(writer, "Invalid request body", 400)
        return

    sched_path = f"schedules/{sched_file}"
    if not schedule.load(sched_path):
        await send_error(writer, "Schedule not found or invalid", 404)
        return

    try:
        schedule.start()
    except Exception as e:
        await send_error(writer, f"Start failed: {e}", 400)
        return

    ts = time.time() if _rtc_is_set() else 0
    name = schedule.schedule_name or ""

    _update_status_cache()
    await send_json(writer, {"ok": True, "schedule": name, "started_at": ts})


async def handle_run_stop(body, writer):
    if not schedule.is_running:
        await send_error(writer, "No run active", 409)
        return

    reason = "manual"
    if body:
        try:
            data = json.loads(body)
            reason = data.get("reason", "manual")
        except Exception:
            pass

    schedule.stop(reason=reason)
    ts = time.time() if _rtc_is_set() else 0
    _update_status_cache()
    await send_json(writer, {"ok": True, "stopped_at": ts, "reason": reason})


async def handle_run_advance(writer):
    try:
        old_idx, new_idx, new_name = schedule.advance()
        _update_status_cache()
        await send_json(
            writer,
            {
                "ok": True,
                "previous_stage": old_idx,
                "new_stage": new_idx,
                "new_stage_name": new_name,
            },
        )
    except RuntimeError as e:
        await send_error(writer, str(e), 409)


async def handle_run_shutdown(writer):
    """End cooldown and put the kiln fully idle: heater off, fans off,
    vents closed, cooldown flag cleared. 409 if a run is currently active
    (caller should /run/stop first).
    """
    if schedule is None:
        await send_error(writer, "Schedule controller unavailable", 503)
        return
    try:
        schedule.shutdown()
    except RuntimeError as e:
        await send_error(writer, str(e), 409)
        return
    ts = time.time() if _rtc_is_set() else 0
    _update_status_cache()
    await send_json(writer, {"ok": True, "shutdown_at": ts})


async def handle_test_run(writer):
    global _test_running
    if _test_running:
        await send_error(writer, "Test already in progress", 409)
        return
    if schedule.is_running:
        await send_error(writer, "Cannot test while run active", 409)
        return

    _test_running = True
    asyncio.create_task(_run_test_suite())

    await send_json(
        writer,
        {
            "ok": True,
            "test_count": len(TESTS),
            "estimated_duration_s": 300,
        },
    )


async def handle_test_status(writer):
    elapsed = 0
    passed = sum(1 for t in _test_results if t["status"] == "pass")
    failed = sum(1 for t in _test_results if t["status"] == "fail")
    skipped = sum(1 for t in _test_results if t["status"] == "skip")
    pending = sum(1 for t in _test_results if t["status"] == "pending")
    running = sum(1 for t in _test_results if t["status"] == "running")

    result = {
        "complete": not _test_running,
        "elapsed_s": elapsed,
        "tests": _test_results,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "pending": pending + running,
    }
    if not _test_running and _test_results:
        result["overall"] = "pass" if failed == 0 else "fail"

    await send_json(writer, result)


async def handle_modules_list(writer):
    modules = []

    # main.py at root
    try:
        stat = uos.stat("main.py")
        modules.append(
            {
                "path": "main.py",
                "size_bytes": stat[6],
                "modified": _stat_time_str(stat),
            }
        )
    except Exception:
        pass

    # lib/*.py
    try:
        for fname in uos.listdir("lib"):
            if fname.endswith(".py"):
                fpath = f"lib/{fname}"
                try:
                    stat = uos.stat(fpath)
                    modules.append(
                        {
                            "path": fpath,
                            "size_bytes": stat[6],
                            "modified": _stat_time_str(stat),
                        }
                    )
                except Exception:
                    pass
    except Exception:
        pass

    await send_json(writer, {"modules": modules})


async def handle_module_upload(mod_path, body, writer):
    # URL-decode %2F -> /
    mod_path = mod_path.replace("%2F", "/").replace("%2f", "/")

    # Validate path
    allowed = mod_path == "main.py" or (
        mod_path.startswith("lib/") and mod_path.endswith(".py")
    )
    if not allowed:
        await send_error(writer, "Path not allowed (lib/*.py or main.py only)", 400)
        return
    if len(body) > 524288:
        await send_error(writer, "File too large (max 512KB)", 413)
        return

    try:
        with open(mod_path, "wb") as f:
            f.write(body)
        stat = uos.stat(mod_path)
        size = stat[6]
    except Exception as e:
        await send_error(writer, f"Write failed: {e}", 500)
        return

    await send_json(
        writer,
        {
            "ok": True,
            "path": mod_path,
            "size_bytes": size,
            "rebooting": True,
        },
    )

    # Schedule reboot after response is sent
    asyncio.create_task(_delayed_reboot())


async def _delayed_reboot():
    await asyncio.sleep(1)
    machine.reset()


def _stat_time_str(stat):
    try:
        t = time.localtime(stat[8])
        return f"{t[0]:04d}-{t[1]:02d}-{t[2]:02d} {t[3]:02d}:{t[4]:02d}"
    except Exception:
        return "unknown"


# -----------------------------------------------------------------------
# File lookup helpers
# -----------------------------------------------------------------------


def _find_run_file(prefix, ext, run_id=""):
    """Find a run log file on the SD card by prefix and extension.

    Returns filename (e.g. "data_20260317_1430.csv") or None.
    Without run_id, returns the most recent (last alphabetically).
    """
    if not sdcard.is_mounted():
        return None
    files = sdcard.listdir()
    matches = [f for f in files if f.startswith(prefix) and f.endswith(ext)]
    if not matches:
        return None
    if run_id:
        target = f"{prefix}{run_id}{ext}"
        return target if target in matches else None
    matches.sort()
    return matches[-1]


# -----------------------------------------------------------------------
# System test suite
# -----------------------------------------------------------------------

TESTS = [
    # (id, name, group)
    ("exhaust_init", "Exhaust fan init", "Unit Tests"),
    ("exhaust_tach", "Exhaust tach", "Unit Tests"),
    ("circulation_init", "Circulation fan init", "Unit Tests"),
    ("vents_open_close", "Vents open/close", "Unit Tests"),
    ("heater_on_off", "Heater on/off", "Unit Tests"),
    ("sdcard_mount", "SD card mount", "Unit Tests"),
    ("sensors_read", "SHT31 sensors read", "Unit Tests"),
    ("moisture_read", "Moisture probe read", "Unit Tests"),
    ("current_12v", "Current monitor 12V", "Unit Tests"),
    ("current_5v", "Current monitor 5V", "Unit Tests"),
    ("display_init", "Display init", "Unit Tests"),
    ("lora_mock_tx", "LoRa transmit", "Unit Tests"),
    ("schedule_load", "Schedule load", "Integration Tests"),
    ("logger_event", "Logger event write", "Integration Tests"),
    ("logger_data", "Logger data write", "Integration Tests"),
    ("heater_temp_rise", "Heater temp rise", "Commissioning"),
    ("lora_tx_real", "LoRa real TX", "Commissioning"),
    ("rtc_set", "RTC clock set", "Commissioning"),
]


async def _run_test_suite():
    global _test_running, _test_results

    # Initialise results
    _test_results = []
    for tid, name, group in TESTS:
        _test_results.append(
            {
                "id": tid,
                "name": name,
                "group": group,
                "status": "pending",
                "detail": None,
                "duration_ms": None,
            }
        )

    start_ms = time.ticks_ms()

    for i, (tid, name, group) in enumerate(TESTS):
        _test_results[i]["status"] = "running"
        t0 = time.ticks_ms()
        try:
            status, detail = await _run_single_test(tid)
        except Exception as e:
            status = "fail"
            detail = str(e)
        elapsed = time.ticks_diff(time.ticks_ms(), t0)
        _test_results[i]["status"] = status
        _test_results[i]["detail"] = detail
        _test_results[i]["duration_ms"] = elapsed

        # Ensure safe state between tests
        try:
            heater.off()
        except Exception:
            pass
        await asyncio.sleep(0.1)

    _test_running = False


async def _run_single_test(tid):
    if tid == "exhaust_init":
        exhaust.on(50)
        await asyncio.sleep(2)
        rpm = exhaust.read_rpm(sample_ms=1000)
        running = exhaust.is_running
        exhaust.off()
        if not running:
            return ("fail", "Fan did not start")
        if rpm is not None and rpm > 0:
            return ("pass", f"RPM={rpm} at 50%")
        return ("pass", "Fan started at 50% (no tach reading)")

    if tid == "exhaust_tach":
        exhaust.on(75)
        await asyncio.sleep(2)
        rpm = exhaust.read_rpm(sample_ms=1500)
        exhaust.off()
        if rpm is not None and rpm > 0:
            return ("pass", f"RPM={rpm} at 75%")
        return ("fail", "Tach read zero or None at 75%")

    if tid == "circulation_init":
        circulation.on(30)
        await asyncio.sleep(1)
        running = circulation.is_running
        circulation.off()
        if running:
            return ("pass", "Fans started at 30% (clamped to min)")
        return ("fail", "Fans did not start")

    if tid == "vents_open_close":
        vents.open()
        await asyncio.sleep(1)
        is_open = vents.is_open
        vents.close()
        await asyncio.sleep(1)
        is_closed = not vents.is_open
        if is_open and is_closed:
            return ("pass", "Open and close verified")
        return ("fail", f"open={is_open} closed={is_closed}")

    if tid == "heater_on_off":
        heater.on()
        await asyncio.sleep(0.5)
        was_on = heater.is_on
        heater.off()
        await asyncio.sleep(0.5)
        is_off = not heater.is_on
        if was_on and is_off:
            return ("pass", "On/off verified via is_on()")
        return ("fail", f"on={was_on} off_after={is_off}")

    if tid == "sdcard_mount":
        mounted = sdcard.is_mounted()
        if mounted:
            files = sdcard.listdir()
            return ("pass", f"Mounted, {len(files)} files")
        # Try mounting
        if sdcard.mount():
            return ("pass", "Mounted successfully")
        return ("fail", "Mount failed")

    if tid == "sensors_read":
        data = sensors.read()
        if data is None:
            return ("fail", "read() returned None")
        tl = data.get("temp_lumber")
        rl = data.get("rh_lumber")
        ti = data.get("temp_intake")
        ri = data.get("rh_intake")
        if tl is not None and rl is not None and ti is not None and ri is not None:
            ok = (
                -10 <= tl <= 80
                and 0 <= rl <= 100
                and -10 <= ti <= 80
                and 0 <= ri <= 100
            )
            if ok:
                return ("pass", f"L:{tl:.1f}C/{rl:.0f}% I:{ti:.1f}C/{ri:.0f}%")
            return ("fail", f"Values out of range: {data}")
        return ("fail", f"Some sensors returned None: {data}")

    if tid == "moisture_read":
        res = moisture.read_resistance()
        ch1 = res.get("ch1_ohms")
        ch2 = res.get("ch2_ohms")
        if ch1 is not None and ch2 is not None:
            return ("pass", f"ch1={ch1:.0f} ch2={ch2:.0f} ohm")
        if ch1 is not None or ch2 is not None:
            return ("pass", f"ch1={ch1} ch2={ch2} (partial)")
        return ("fail", "Both channels returned None")

    if tid == "current_12v":
        r = monitor_12v.read()
        if r and r.get("current_mA") is not None:
            return ("pass", f"{r['current_mA']:.1f}mA, {r['bus_voltage_V']:.2f}V")
        return ("fail", "Read returned None")

    if tid == "current_5v":
        r = monitor_5v.read()
        if r and r.get("current_mA") is not None:
            return ("pass", f"{r['current_mA']:.1f}mA, {r['bus_voltage_V']:.2f}V")
        return ("fail", "Read returned None")

    if tid == "display_init":
        try:
            display.clear(Color.BLACK)
            display.draw_text(10, 10, "Test OK", Color.GREEN, 24)
            return ("pass", "Clear and draw_text OK")
        except Exception as e:
            return ("fail", str(e))

    if tid == "lora_mock_tx":
        ok = lora.send_telemetry({"test": True})
        if ok:
            return ("pass", "send_telemetry returned True")
        return ("fail", "send_telemetry returned False")

    if tid == "schedule_load":
        ok = schedule.load(f"schedules/{config.DEFAULT_SCHEDULE}")
        if ok:
            return ("pass", f"Loaded {config.DEFAULT_SCHEDULE}")
        return ("fail", f"Failed to load {config.DEFAULT_SCHEDULE}")

    if tid == "logger_event":
        if not logger.run_active:
            logger.begin_run()
        logger.event("test", "System test event")
        # Verify by reading back
        if logger._event_file:
            return ("pass", "Event written to SD")
        return ("fail", "No event file open")

    if tid == "logger_data":
        if not logger.run_active:
            logger.begin_run()
        logger.data({"ts": time.time(), "temp_lumber": 25.0, "stage": "test"})
        if logger._data_file:
            logger.end_run()
            return ("pass", "Data row written to SD")
        logger.end_run()
        return ("fail", "No data file open")

    if tid == "heater_temp_rise":
        data = sensors.read()
        if data is None or data.get("temp_lumber") is None:
            return ("skip", "No sensor reading available")
        if data["temp_lumber"] > 50:
            return ("skip", "Temp already > 50C, unsafe to test")
        start_temp = data["temp_lumber"]
        heater.on()
        await asyncio.sleep(90)
        data2 = sensors.read()
        heater.off()
        if data2 is None or data2.get("temp_lumber") is None:
            return ("fail", "Sensor read failed after heating")
        rise = data2["temp_lumber"] - start_temp
        if rise >= 1.0:
            return ("pass", f"Temp rose {rise:.1f}C in 90s")
        return ("fail", f"Temp only rose {rise:.1f}C in 90s")

    if tid == "lora_tx_real":
        ok = lora.send_telemetry({"test": True, "ts": time.time()})
        if ok:
            return ("pass", "LoRa TX succeeded")
        return ("fail", "LoRa TX failed")

    if tid == "rtc_set":
        if _rtc_is_set():
            t = time.localtime()
            return ("pass", f"RTC year={t[0]}")
        return ("fail", "Sync clock via app")

    return ("skip", "Unknown test")


# -----------------------------------------------------------------------
# Async task loops
# -----------------------------------------------------------------------


async def control_loop():
    """Main control loop. Runs every 10s for responsive fault detection.

    The original loop slept 30-120s (matching the schedule's tick cadence),
    which meant faults took minutes to detect and clear. Running at 10s is
    safe -- schedule.tick() control logic (heater deadband, vent decisions,
    stage advance) is idempotent and benefits from faster responsiveness.
    LoRa telemetry is rate-limited to avoid flooding the link.
    """
    _last_lora_ms = time.ticks_ms()
    while True:
        try:
            if schedule is not None:
                schedule.tick()
                # Rate-limit LoRa telemetry to every 30s minimum
                if (lora is not None and schedule.is_running
                        and time.ticks_diff(time.ticks_ms(), _last_lora_ms) >= 30_000):
                    _send_lora_telemetry()
                    _last_lora_ms = time.ticks_ms()
            _update_status_cache()
        except Exception as e:
            print(f"[main] control loop error: {e}")
            if logger is not None:
                try:
                    logger.event("main", f"Control loop error: {e}", level="ERROR")
                except Exception:
                    pass
        await asyncio.sleep(10)


def _compact_json_value(v):
    """Render a single Python scalar as compact JSON for the LoRa wire."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{round(v, 1)}"
    return str(v)  # int / other numerics


def _build_compact_json(d):
    """Build a JSON object with no whitespace.

    MicroPython's json.dumps(obj) inserts a space after every ':' and ','
    AND serialises floats at full IEEE754 precision. Both behaviours
    inflate LoRa telemetry past the SX1278 255-byte FIFO. We hand-build
    a compact, rounded representation here so we are not at the mercy
    of the MicroPython port's json implementation.
    """
    parts = []
    for k, v in d.items():
        if isinstance(v, str):
            # Strings need JSON escaping; we only emit short, safe strings.
            parts.append(f'"{k}":"{v}"')
        elif isinstance(v, (list, tuple)):
            # List of strings (used for faults field)
            items = ",".join(f'"{s}"' for s in v)
            parts.append(f'"{k}":[{items}]')
        else:
            parts.append(f'"{k}":{_compact_json_value(v)}')
    return "{" + ",".join(parts) + "}"


def _send_lora_telemetry():
    """Build and send a LoRa telemetry packet matching the Pi4 SQLite schema.

    Wire format is optimised for the SX1278 255-byte FIFO limit:
    - Compact JSON (no whitespace)
    - Floats rounded to 1 dp
    - Stage sent as integer index, NOT the human-readable name (Pi4 looks
      up the name from the schedule on disk)
    - Field names match /status (rh_*, not humidity_*) so the daemon and
      the dashboard share one vocabulary
    Typical packet: ~225 bytes.
    """
    if lora is None:
        return
    s = _status_cache
    payload = {
        "ts": int(s.get("ts", 0) or 0),
        "stage_idx": s.get("stage_index"),
        "temp_lumber": s.get("temp_lumber"),
        "temp_intake": s.get("temp_intake"),
        "rh_lumber": s.get("rh_lumber"),
        "rh_intake": s.get("rh_intake"),
        "mc_channel_1": s.get("mc_channel_1"),
        "mc_channel_2": s.get("mc_channel_2"),
        "exhaust_fan_rpm": s.get("exhaust_fan_rpm"),
        "exhaust_fan_pct": s.get("exhaust_fan_pct", 0),
        "circ_fan_on": 1 if s.get("circ_fan_on") else 0,
        "heater_on": 1 if s.get("heater_on") else 0,
        "vent_open": 1 if s.get("vent_open") else 0,
    }
    # Build base packet first, then fit as many fault codes as space allows.
    # The base telemetry is ~225 bytes; the SX1278 FIFO limit is 255.
    try:
        base_wire = _build_compact_json(payload)
        fault_codes = [
            d["code"] for d in s.get("fault_details", [])
            if d.get("tier") == "fault"
        ][:5]
        if fault_codes:
            # Try adding faults, trimming until it fits
            while fault_codes:
                payload["faults"] = fault_codes
                wire = _build_compact_json(payload).encode()
                if len(wire) <= 255:
                    break
                fault_codes.pop()
            else:
                # Even one code didn't fit -- send without faults
                del payload["faults"]
                wire = base_wire.encode()
        else:
            wire = base_wire.encode()
        if len(wire) > 255:
            print(f"[main] LoRa telemetry too long ({len(wire)} bytes) - dropped")
            return
        lora.send(wire)
    except Exception as e:
        print(f"[main] LoRa telemetry error: {e}")


async def display_loop():
    if display is None:
        return  # No display -- nothing to tick
    while True:
        try:
            display.tick()
        except Exception as e:
            print(f"[main] display tick error: {e}")
        await asyncio.sleep(0.1)


async def lora_heartbeat():
    if lora is None:
        return
    while True:
        try:
            run_active = schedule is not None and schedule.is_running
            if not run_active:
                lora.send_telemetry(
                    {
                        "type": "heartbeat",
                        "ts": time.time() if _rtc_is_set() else 0,
                        "uptime_s": _uptime_s(),
                        "run_active": False,
                    }
                )
        except Exception as e:
            print(f"[main] heartbeat error: {e}")
        await asyncio.sleep(300)


async def rpm_reader():
    global _cached_rpm
    if exhaust is None:
        return
    while True:
        try:
            if exhaust.is_running:
                _cached_rpm = exhaust.read_rpm(sample_ms=1000)
            else:
                _cached_rpm = 0
        except Exception:
            _cached_rpm = None
        # Feed the cached RPM into the exhaust module's fault state
        # so mid-run stalls are detected without a blocking re-read
        # in check_health().
        exhaust.update_rpm(_cached_rpm)
        await asyncio.sleep(10)


# -----------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------


async def main():
    print(f"Kiln Controller v{config.VERSION}")
    print(f"Board: {uos.uname().machine}")

    init_hardware()
    start_wifi()

    # Register display pages (only if display is available)
    if display is not None:
        try:
            display.register_page("status", render_status_page)
            display.register_page("sensors", render_sensors_page)
            display.register_page("moisture", render_moisture_page)
            display.register_page("system", render_system_page)
        except Exception as e:
            print(f"[main] display page registration failed: {e}")

    missing = _missing_modules()
    boot_msg = "Boot complete."
    if missing:
        boot_msg += f" DEGRADED -- missing: {missing}"
    if logger is not None:
        logger.event("main", boot_msg, level="WARN" if missing else "INFO")
    else:
        print(f"[main] {boot_msg}")

    _update_status_cache()

    # Launch async tasks (each is a no-op if its hardware is missing)
    asyncio.create_task(control_loop())
    asyncio.create_task(display_loop())
    asyncio.create_task(lora_heartbeat())
    asyncio.create_task(rpm_reader())

    # Start HTTP server -- always runs, even with no hardware,
    # so the REST API stays available for remote debugging.
    server = await asyncio.start_server(handle_request, "0.0.0.0", 80)
    if logger is not None:
        logger.event("main", "HTTP server listening on port 80")
    else:
        print("[main] HTTP server listening on port 80")

    # Keep running
    while True:
        await asyncio.sleep(60)
        gc.collect()


# -----------------------------------------------------------------------
# Boot with exception handling
# -----------------------------------------------------------------------


def _record_boot_error(exc):
    """Persist a traceback to /boot_error.log for post-mortem reading."""
    import sys

    try:
        with open("/boot_error.log", "w") as f:
            f.write(f"FATAL at uptime {_uptime_s()}s: {exc}\n")
            _print_exc = getattr(sys, "print_exception", None)
            if _print_exc:
                _print_exc(exc, f)
    except Exception:
        pass


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("Interrupted by user")
except Exception as e:
    import sys

    print("=" * 50)
    print(f"FATAL: {e}")
    print("-" * 50)
    # MicroPython-specific traceback printer
    _print_exc = getattr(sys, "print_exception", None)
    if _print_exc:
        _print_exc(e)
    print("=" * 50)
    _record_boot_error(e)
    # Safe shutdown
    try:
        heater.off()
    except Exception:
        pass
    try:
        vents.open()
    except Exception:
        pass
    try:
        circulation.off()
    except Exception:
        pass
    try:
        exhaust.off()
    except Exception:
        pass
    try:
        logger.event("main", f"Fatal exception: {e}", level="ERROR")
    except Exception:
        pass
    # Do NOT auto-reboot -- the boot loop hides the error.
    # Drop to REPL so the user can read the traceback and diagnose.
    print("Dropping to REPL. Power-cycle the Pico to retry.")
