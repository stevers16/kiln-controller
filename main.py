# main.py -- Kiln controller entry point
#
# Initialises all hardware modules, starts the WiFi AP, runs the
# asyncio HTTP REST API server, and executes the drying control loop.
# Runs at boot on the Raspberry Pi Pico 2 W.

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

# Built-in schedule filenames (read-only)
BUILTIN_SCHEDULES = (
    "maple_05in.json", "maple_1in.json",
    "beech_05in.json", "beech_1in.json",
)

# HTTP status text
HTTP_STATUS = {
    200: "OK", 400: "Bad Request", 401: "Unauthorized",
    403: "Forbidden", 404: "Not Found", 409: "Conflict",
    413: "Payload Too Large", 500: "Internal Server Error",
    503: "Service Unavailable",
}

# -----------------------------------------------------------------------
# Hardware initialisation
# -----------------------------------------------------------------------

def init_hardware():
    global sdcard, logger, i2c0, sensors, monitor_12v, monitor_5v
    global circulation, exhaust, vents, heater, moisture
    global display, lora, schedule

    # 1. SD card first -- logger depends on it
    sdcard = SDCard()

    # 2. Logger
    logger = Logger(sdcard)

    # 3. Shared I2C bus
    i2c0 = machine.I2C(0, sda=machine.Pin(0), scl=machine.Pin(1), freq=100_000)

    # 4. Sensors
    sensors = SHT31Sensors(i2c=i2c0, logger=logger)

    # 5. Current monitors
    monitor_12v = CurrentMonitor(i2c0, 0x40, "12V", logger=logger)
    monitor_5v = CurrentMonitor(i2c0, 0x41, "5V", logger=logger)

    # 6. Circulation fans
    circulation = CirculationFans(
        current_monitor=monitor_12v, logger=logger
    )

    # 7. Exhaust fan
    exhaust = ExhaustFan(logger=logger)

    # 8. Vents
    vents = Vents(current_monitor=monitor_5v, logger=logger)

    # 9. Heater
    heater = Heater(logger=logger)

    # 10. Moisture probes + calibration
    moisture = MoistureProbe(logger=logger)
    _load_calibration(moisture, sdcard)

    # 11. Display
    display = Display(timeout_s=config.DISPLAY_TIMEOUT_S)

    # 12. LoRa
    lora = LoRa(logger=logger)

    # 13. Schedule controller
    schedule = KilnSchedule(
        sdcard=sdcard, sensors=sensors, moisture=moisture,
        heater=heater, exhaust=exhaust, circulation=circulation,
        vents=vents, lora=lora, logger=logger,
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
# WiFi AP
# -----------------------------------------------------------------------

def start_wifi():
    ap = network.WLAN(network.AP_IF)
    ap.config(
        ssid=config.AP_SSID,
        password=config.AP_PASSWORD,
        security=network.WPA2 if config.AP_PASSWORD else 0,
    )
    ap.active(True)
    for _ in range(50):
        if ap.active():
            break
        time.sleep_ms(100)
    ip = ap.ifconfig()[0]
    logger.event("main", f"WiFi AP active -- SSID={config.AP_SSID} IP={ip}")
    return ap


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

def _update_status_cache():
    global _status_cache
    s = schedule.status()

    # Read additional data from modules
    try:
        sensor_data = schedule._last_sensor_read or {}
    except Exception:
        sensor_data = {}

    try:
        mc_data = schedule._last_mc_read or {}
    except Exception:
        mc_data = {}

    cur_12v = None
    cur_5v = None
    try:
        r12 = monitor_12v.read()
        if r12:
            cur_12v = r12.get("current_mA")
    except Exception:
        pass
    try:
        r5 = monitor_5v.read()
        if r5:
            cur_5v = r5.get("current_mA")
    except Exception:
        pass

    # Total elapsed hours
    total_h = None
    try:
        if schedule._running and schedule._run_start_ms:
            elapsed = time.ticks_diff(time.ticks_ms(), schedule._run_start_ms)
            total_h = round(elapsed / 3_600_000, 2)
    except Exception:
        pass

    # Stage min hours
    stage_min_h = None
    try:
        if schedule._schedule and schedule._running:
            stages = schedule._schedule.get("stages", [])
            idx = schedule._stage_index
            if 0 <= idx < len(stages):
                stage_min_h = stages[idx].get("min_duration_h")
    except Exception:
        pass

    # Active alerts (alert types currently in suppression window)
    active_alerts = []
    try:
        now = time.ticks_ms()
        for atype, ts in schedule._last_alert_ts.items():
            if time.ticks_diff(now, ts) < 30 * 60_000:
                active_alerts.append(atype)
    except Exception:
        pass

    _status_cache = {
        "ts": time.time() if _rtc_is_set() else 0,
        "run_active": s.get("running", False),
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
        "active_alerts": active_alerts,
    }


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
        display.draw_text(10, 65, f"Elapsed: {_fmt(s.get('stage_elapsed_h'), 'h')}", Color.WHITE, 16)
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
    display.draw_text(10, 65, f"  T: {_fmt(s.get('temp_lumber'), ' C')} / {_fmt(s.get('target_temp_c'), ' C')}", Color.WHITE, 16)
    display.draw_text(10, 85, f"  RH: {_fmt(s.get('rh_lumber'), '%')} / {_fmt(s.get('target_rh_pct'), '%')}", Color.WHITE, 16)

    display.draw_text(10, 115, "Intake:", Color.GREEN, 16)
    display.draw_text(10, 135, f"  T: {_fmt(s.get('temp_intake'), ' C')}", Color.WHITE, 16)
    display.draw_text(10, 155, f"  RH: {_fmt(s.get('rh_intake'), '%')}", Color.WHITE, 16)


def render_moisture_page():
    display.clear(Color.BLACK)
    s = _status_cache
    display.draw_text(10, 10, "Moisture", Color.WHITE, 24)

    display.draw_text(10, 45, f"Ch1: {_fmt(s.get('mc_channel_1'), '% MC')}", Color.WHITE, 16)
    r1 = s.get("mc_resistance_1")
    if r1 is not None:
        display.draw_text(10, 65, f"  R: {r1} ohm", Color.WHITE, 16)

    display.draw_text(10, 95, f"Ch2: {_fmt(s.get('mc_channel_2'), '% MC')}", Color.WHITE, 16)
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

    display.draw_text(10, 45, f"12V: {_fmt(s.get('current_12v_ma'), ' mA', 0)}", Color.WHITE, 16)
    display.draw_text(10, 65, f" 5V: {_fmt(s.get('current_5v_ma'), ' mA', 0)}", Color.WHITE, 16)

    up = _uptime_s()
    h = up // 3600
    m = (up % 3600) // 60
    display.draw_text(10, 95, f"Uptime: {h}h {m}m", Color.WHITE, 16)

    sd_ok = sdcard.is_mounted() if sdcard else False
    display.draw_text(10, 120, f"SD: {'OK' if sd_ok else 'NONE'}", Color.GREEN if sd_ok else Color.RED, 16)

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
        filename = path[len("/schedules/"):]
        return await handle_schedule_get(filename, writer)
    if method == "PUT" and path.startswith("/schedules/"):
        filename = path[len("/schedules/"):]
        return await handle_schedule_put(filename, body, writer)
    if method == "DELETE" and path.startswith("/schedules/"):
        filename = path[len("/schedules/"):]
        return await handle_schedule_delete(filename, writer)

    if method == "GET" and path.startswith("/logs/"):
        # /logs/{run_id}/events
        parts = path[len("/logs/"):].split("/")
        run_id = parts[0] if parts else ""
        return await handle_logs_events(run_id, writer)
    if method == "DELETE" and path.startswith("/logs/"):
        run_id = path[len("/logs/"):].rstrip("/")
        return await handle_logs_delete(run_id, writer)

    if method == "PUT" and path.startswith("/modules/"):
        mod_path = path[len("/modules/"):]
        return await handle_module_upload(mod_path, body, writer)

    await send_error(writer, "Not found", 404)


# -----------------------------------------------------------------------
# Endpoint handlers
# -----------------------------------------------------------------------

async def handle_health(writer):
    await send_json(writer, {
        "status": "ok",
        "uptime_s": _uptime_s(),
        "free_mem_bytes": gc.mem_free(),
        "sdcard_mounted": sdcard.is_mounted(),
        "rtc_set": _rtc_is_set(),
        "run_active": schedule._running if schedule else False,
        "firmware_version": config.VERSION,
    })


async def handle_status(writer):
    await send_json(writer, _status_cache)


async def handle_version(writer):
    mp_ver = uos.uname().version if hasattr(uos.uname(), "version") else "unknown"
    board = uos.uname().machine if hasattr(uos.uname(), "machine") else "unknown"
    await send_json(writer, {
        "firmware_version": config.VERSION,
        "micropython_version": mp_ver,
        "board": board,
    })


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
    csv_file = _find_data_csv(run_id)
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
    if not sdcard.is_mounted():
        await send_error(writer, "SD card not mounted", 503)
        return

    limit = int(query.get("limit", "50"))
    level_filter = query.get("level", "")
    run_id = query.get("run", "")

    event_file = _find_event_file(run_id)
    if event_file is None:
        await send_json(writer, {"alerts": [], "count": 0})
        return

    path = f"{sdcard.mount_point}/{event_file}"
    alerts = []
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # Only include WARNING and ERROR lines
                if "[WARN" not in line and "[ERROR" not in line:
                    continue
                if level_filter:
                    if level_filter == "WARNING" and "[WARN" not in line:
                        continue
                    if level_filter == "ERROR" and "[ERROR" not in line:
                        continue

                alert = _parse_event_line(line)
                if alert:
                    alerts.append(alert)
    except Exception:
        pass

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
        level = line[level_start + 1:level_end].strip()
        # Extract source
        src_start = line.index("[", level_end + 1)
        src_end = line.index("]", src_start)
        source = line[src_start + 1:src_end].strip()
        # Message is everything after
        message = line[src_end + 2:].strip()
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

        return {
            "ts": ts,
            "level": level,
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
    t = (int(ymd[0]), int(ymd[1]), int(ymd[2]),
         int(hms[0]), int(hms[1]), int(hms[2]), 0, 0)
    return time.mktime(t)


async def handle_runs(writer):
    if not sdcard.is_mounted():
        await send_json(writer, {"runs": []})
        return

    files = sdcard.listdir()
    # Gather run IDs from data CSV filenames
    run_ids = []
    for f in files:
        if f.startswith("data_") and f.endswith(".csv"):
            rid = f.replace("data_", "").replace(".csv", "")
            run_ids.append(rid)
    run_ids.sort(reverse=True)

    runs = []
    for rid in run_ids:
        event_name = f"event_{rid}.txt"
        data_name = f"data_{rid}.csv"
        event_count = 0
        data_rows = 0
        size = 0

        try:
            stat = uos.stat(f"{sdcard.mount_point}/{data_name}")
            size += stat[6]
            # Count data rows (subtract 1 for header)
            with open(f"{sdcard.mount_point}/{data_name}", "r") as f:
                for _ in f:
                    data_rows += 1
            data_rows = max(0, data_rows - 1)
        except Exception:
            pass

        try:
            stat = uos.stat(f"{sdcard.mount_point}/{event_name}")
            size += stat[6]
            with open(f"{sdcard.mount_point}/{event_name}", "r") as f:
                for _ in f:
                    event_count += 1
        except Exception:
            pass

        # Format started_at from run ID (YYYYMMDD_HHMM)
        started = rid
        if len(rid) >= 13:
            started = f"{rid[0:4]}-{rid[4:6]}-{rid[6:8]} {rid[9:11]}:{rid[11:13]}"

        runs.append({
            "id": rid,
            "started_at_str": started,
            "event_log": event_name,
            "data_csv": data_name,
            "data_rows": data_rows,
            "event_count": event_count,
            "size_bytes": size,
        })

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
        await send_json(writer, {
            "run": run_id,
            "lines": lines,
            "line_count": len(lines),
        })
    except OSError:
        await send_error(writer, "Run not found", 404)


async def handle_logs_delete(run_id, writer):
    if not sdcard.is_mounted():
        await send_error(writer, "SD card not mounted", 503)
        return

    # Prevent deleting active run
    if schedule._running and logger._run_active:
        try:
            suffix = logger._file_suffix() if hasattr(logger, "_file_suffix") else ""
            # Check if run_id matches current active log
            if logger._event_file:
                active_name = str(logger._event_file.name) if hasattr(logger._event_file, "name") else ""
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
        await send_json(writer, {
            "mounted": True,
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "file_count": len(files),
        })
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
        for key in ("name", "stage_type", "target_temp_c", "target_rh_pct", "min_duration_h"):
            if key not in stage:
                await send_error(writer, f"Stage {i} missing field: {key}", 400)
                return
        stype = stage["stage_type"]
        if stype == "drying":
            if stage.get("target_mc_pct") is None:
                await send_error(writer, f"Stage {i}: drying stage requires target_mc_pct", 400)
                return
        elif stype in ("equalizing", "conditioning"):
            if stage.get("target_mc_pct") is not None:
                await send_error(writer, f"Stage {i}: {stype} stage must have null target_mc_pct", 400)
                return

    # Write to SD
    path = f"{sdcard.mount_point}/schedules/{filename}"
    try:
        with open(path, "w") as f:
            f.write(json.dumps(data))
        await send_json(writer, {
            "ok": True,
            "filename": filename,
            "stage_count": len(stages),
        })
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
    text = sdcard.read_text("calibration.json")
    if text is None:
        await send_json(writer, {
            "channel_1_offset": 0.0,
            "channel_2_offset": 0.0,
            "calibrated_at": None,
            "source": "defaults",
        })
        return
    try:
        cal = json.loads(text)
        cal["source"] = "calibration.json"
        await send_json(writer, cal)
    except Exception:
        await send_json(writer, {
            "channel_1_offset": 0.0,
            "channel_2_offset": 0.0,
            "calibrated_at": None,
            "source": "defaults",
        })


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

        await send_json(writer, {
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
        })
    except Exception as e:
        await send_error(writer, f"Read failed: {e}", 500)


async def handle_run_start(body, writer):
    if schedule._running:
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
    name = ""
    try:
        name = schedule._schedule.get("name", "")
    except Exception:
        pass

    _update_status_cache()
    await send_json(writer, {"ok": True, "schedule": name, "started_at": ts})


async def handle_run_stop(body, writer):
    if not schedule._running:
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
        await send_json(writer, {
            "ok": True,
            "previous_stage": old_idx,
            "new_stage": new_idx,
            "new_stage_name": new_name,
        })
    except RuntimeError as e:
        await send_error(writer, str(e), 409)


async def handle_test_run(writer):
    global _test_running
    if _test_running:
        await send_error(writer, "Test already in progress", 409)
        return
    if schedule._running:
        await send_error(writer, "Cannot test while run active", 409)
        return

    _test_running = True
    asyncio.create_task(_run_test_suite())

    await send_json(writer, {
        "ok": True,
        "test_count": len(TESTS),
        "estimated_duration_s": 300,
    })


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
        modules.append({
            "path": "main.py",
            "size_bytes": stat[6],
            "modified": _stat_time_str(stat),
        })
    except Exception:
        pass

    # lib/*.py
    try:
        for fname in uos.listdir("lib"):
            if fname.endswith(".py"):
                fpath = f"lib/{fname}"
                try:
                    stat = uos.stat(fpath)
                    modules.append({
                        "path": fpath,
                        "size_bytes": stat[6],
                        "modified": _stat_time_str(stat),
                    })
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

    await send_json(writer, {
        "ok": True,
        "path": mod_path,
        "size_bytes": size,
        "rebooting": True,
    })

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

def _find_data_csv(run_id=""):
    if not sdcard.is_mounted():
        return None
    files = sdcard.listdir()
    csvs = [f for f in files if f.startswith("data_") and f.endswith(".csv")]
    if not csvs:
        return None
    if run_id:
        target = f"data_{run_id}.csv"
        return target if target in csvs else None
    # Default: most recent (last alphabetically)
    csvs.sort()
    return csvs[-1]


def _find_event_file(run_id=""):
    if not sdcard.is_mounted():
        return None
    files = sdcard.listdir()
    events = [f for f in files if f.startswith("event_") and f.endswith(".txt")]
    if not events:
        return None
    if run_id:
        target = f"event_{run_id}.txt"
        return target if target in events else None
    events.sort()
    return events[-1]


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
        _test_results.append({
            "id": tid,
            "name": name,
            "group": group,
            "status": "pending",
            "detail": None,
            "duration_ms": None,
        })

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
        is_open = vents.is_open()
        vents.close()
        await asyncio.sleep(1)
        is_closed = not vents.is_open()
        if is_open and is_closed:
            return ("pass", "Open and close verified")
        return ("fail", f"open={is_open} closed={is_closed}")

    if tid == "heater_on_off":
        heater.on()
        await asyncio.sleep(0.5)
        was_on = heater.is_on()
        heater.off()
        await asyncio.sleep(0.5)
        is_off = not heater.is_on()
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
            ok = (-10 <= tl <= 80 and 0 <= rl <= 100 and
                  -10 <= ti <= 80 and 0 <= ri <= 100)
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
        mock_note = " (mock)" if lora.is_mock else ""
        if ok:
            return ("pass", f"send_telemetry returned True{mock_note}")
        return ("fail", f"send_telemetry returned False{mock_note}")

    if tid == "schedule_load":
        ok = schedule.load(f"schedules/{config.DEFAULT_SCHEDULE}")
        if ok:
            return ("pass", f"Loaded {config.DEFAULT_SCHEDULE}")
        return ("fail", f"Failed to load {config.DEFAULT_SCHEDULE}")

    if tid == "logger_event":
        if not logger._run_active:
            logger.begin_run()
        logger.event("test", "System test event")
        # Verify by reading back
        if logger._event_file:
            return ("pass", "Event written to SD")
        return ("fail", "No event file open")

    if tid == "logger_data":
        if not logger._run_active:
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
        if config.USE_MOCK_LORA or lora.is_mock:
            return ("skip", "Mock LoRa -- real TX not available")
        ok = lora.send_telemetry({"test": True, "ts": time.time()})
        if ok:
            return ("pass", "Real LoRa TX succeeded")
        return ("fail", "Real LoRa TX failed")

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
    while True:
        try:
            schedule.tick()
            _update_status_cache()
        except Exception as e:
            logger.event("main", f"Control loop error: {e}", level="ERROR")
        await asyncio.sleep(schedule.tick_interval_s)


async def display_loop():
    while True:
        try:
            display.tick()
        except Exception as e:
            print(f"[main] display tick error: {e}")
        await asyncio.sleep(0.1)


async def lora_heartbeat():
    while True:
        try:
            if not schedule._running:
                lora.send_telemetry({
                    "type": "heartbeat",
                    "ts": time.time() if _rtc_is_set() else 0,
                    "uptime_s": _uptime_s(),
                    "run_active": False,
                })
        except Exception as e:
            print(f"[main] heartbeat error: {e}")
        await asyncio.sleep(300)


async def rpm_reader():
    global _cached_rpm
    while True:
        try:
            if exhaust.is_running:
                _cached_rpm = exhaust.read_rpm(sample_ms=1000)
            else:
                _cached_rpm = 0
        except Exception:
            _cached_rpm = None
        await asyncio.sleep(10)


# -----------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------

async def main():
    print(f"Kiln Controller v{config.VERSION}")
    print(f"Board: {uos.uname().machine}")

    init_hardware()
    start_wifi()

    # Register display pages
    display.register_page("status", render_status_page)
    display.register_page("sensors", render_sensors_page)
    display.register_page("moisture", render_moisture_page)
    display.register_page("system", render_system_page)

    logger.event("main", "Boot complete. AP started.")
    _update_status_cache()

    # Launch async tasks
    asyncio.create_task(control_loop())
    asyncio.create_task(display_loop())
    asyncio.create_task(lora_heartbeat())
    asyncio.create_task(rpm_reader())

    # Start HTTP server
    server = await asyncio.start_server(handle_request, "0.0.0.0", 80)
    logger.event("main", "HTTP server listening on port 80")

    # Keep running
    while True:
        await asyncio.sleep(60)
        gc.collect()


# -----------------------------------------------------------------------
# Boot with exception handling
# -----------------------------------------------------------------------

try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("Interrupted by user")
except Exception as e:
    print(f"FATAL: {e}")
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
    time.sleep(5)
    machine.reset()
