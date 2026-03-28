# PICO_REST_API_SPEC.md

Spec for the HTTP REST API served by the Pico 2W in AP (Access Point) mode.
This API is consumed exclusively by the Kivy app when connected directly to the
Pico's WiFi AP.

---

## Overview

The Pico runs a lightweight HTTP server (MicroPython `asyncio`-based) alongside
the `schedule.tick()` control loop. The server listens on port 80. All endpoints
return JSON. The API is the full control interface for the kiln when the app is
on-site.

---

## Authentication

All requests must include the header:

```
X-Kiln-Key: <api_key>
```

The API key is a string configured in `config.py` on the Pico. If the header is
missing or the key does not match, the server returns:

```
HTTP 401
{"error": "unauthorized"}
```

The key is compared as a plain string. No hashing required -- this is a LAN-only
interface on a private AP network.

---

## General Conventions

- All responses are `Content-Type: application/json`
- All timestamps are Unix timestamps (integer seconds) unless noted
- All temperatures are degrees C (float)
- All humidity values are percent RH (float)
- All moisture content values are percent MC (float)
- Boolean fields use JSON `true`/`false`
- `null` is used for unavailable sensor readings (sensor failed, not yet read)
- HTTP 200 for success; HTTP 4xx for client errors; HTTP 500 for server errors
- Error responses always include `{"error": "<message>"}`

---

## Endpoints

---

### GET /health

Pico system health check. Used by app for connection detection and Settings display.

**Response:**

```json
{
  "status": "ok",
  "uptime_s": 3612,
  "free_mem_bytes": 142336,
  "sdcard_mounted": true,
  "rtc_set": true,
  "run_active": true,
  "firmware_version": "1.0.0"
}
```

| Field | Type | Notes |
|-------|------|-------|
| `status` | string | Always "ok" if server is responding |
| `uptime_s` | int | Seconds since Pico boot |
| `free_mem_bytes` | int | MicroPython `gc.mem_free()` |
| `sdcard_mounted` | bool | SD card currently mounted |
| `rtc_set` | bool | True if RTC year >= 2024 |
| `run_active` | bool | True if a drying run is in progress |
| `firmware_version` | string | From `config.py` VERSION constant |

---

### POST /time

Set the Pico RTC clock. Called automatically by the app on connect when
`rtc_set` is false or auto-sync is enabled.

**Request body:**

```json
{"ts": 1711900000}
```

`ts` is a Unix timestamp (integer). The server sets the RTC and responds:

```json
{"ok": true, "ts": 1711900000}
```

---

### GET /status

Current kiln state. Primary data source for the Dashboard. Returns the latest
values read by the control loop.

**Response:**

```json
{
  "ts": 1711900000,
  "run_active": true,
  "cooldown": false,
  "schedule_name": "Hard Maple 1 inch",
  "stage_index": 2,
  "stage_name": "Stage 3 - Drying",
  "stage_type": "drying",
  "stage_elapsed_h": 14.5,
  "stage_min_h": 24,
  "total_elapsed_h": 38.2,
  "target_temp_c": 49.0,
  "target_rh_pct": 70.0,
  "target_mc_pct": 25.0,
  "temp_lumber": 48.7,
  "rh_lumber": 71.2,
  "temp_intake": 22.4,
  "rh_intake": 55.1,
  "mc_channel_1": 27.3,
  "mc_channel_2": 26.8,
  "mc_resistance_1": 125400,
  "mc_resistance_2": 131200,
  "heater_on": false,
  "vent_open": false,
  "vent_reason": null,
  "exhaust_fan_pct": 0,
  "exhaust_fan_rpm": 0,
  "circ_fan_on": true,
  "circ_fan_pct": 75,
  "current_12v_ma": 412,
  "current_5v_ma": 89,
  "active_alerts": ["rh_out_of_range"]
}
```

| Field | Type | Notes |
|-------|------|-------|
| `ts` | int | Unix timestamp of this reading |
| `run_active` | bool | |
| `cooldown` | bool | True if in post-run cooldown |
| `schedule_name` | string or null | null if no schedule loaded |
| `stage_index` | int or null | 0-based |
| `stage_name` | string or null | |
| `stage_type` | string or null | "drying" / "equalizing" / "conditioning" |
| `stage_elapsed_h` | float or null | |
| `stage_min_h` | float or null | Minimum hours before advance |
| `total_elapsed_h` | float or null | |
| `target_temp_c` | float or null | |
| `target_rh_pct` | float or null | |
| `target_mc_pct` | float or null | null for equalizing/conditioning |
| `temp_lumber` | float or null | |
| `rh_lumber` | float or null | |
| `temp_intake` | float or null | |
| `rh_intake` | float or null | |
| `mc_channel_1` | float or null | Temperature-corrected MC% |
| `mc_channel_2` | float or null | Temperature-corrected MC% |
| `mc_resistance_1` | int or null | Raw ohms |
| `mc_resistance_2` | int or null | Raw ohms |
| `heater_on` | bool | |
| `vent_open` | bool | |
| `vent_reason` | string or null | "rh_high" / "temp_high" / null |
| `exhaust_fan_pct` | int | 0-100 |
| `exhaust_fan_rpm` | int or null | null if not available |
| `circ_fan_on` | bool | |
| `circ_fan_pct` | int | 0-100 |
| `current_12v_ma` | float or null | INA219 at 0x40 |
| `current_5v_ma` | float or null | INA219 at 0x41 |
| `active_alerts` | array of string | Alert codes currently in suppression window |

---

### GET /history

Time-series data for graphing. Returns data from the SD card CSV log for the
current or specified run.

**Query parameters:**

| Parameter | Type | Default | Notes |
|-----------|------|---------|-------|
| `start` | int | run start | Unix timestamp |
| `end` | int | now | Unix timestamp |
| `fields` | string | all fields | Comma-separated field names |
| `resolution` | int | 1 | Return every Nth row (decimation) |
| `run` | string | current | Run filename prefix (e.g. "20260328_1430") |

**Response (columnar format):**

```json
{
  "fields": ["ts", "temp_lumber", "rh_lumber", "mc_channel_1", "mc_channel_2"],
  "rows": [
    [1711900000, 48.7, 71.2, 27.3, 26.8],
    [1711900120, 48.9, 70.8, 27.1, 26.5]
  ],
  "run": "20260328_1430",
  "row_count": 2
}
```

Available fields (subset of logger.data() record keys):

```
ts, stage, stage_type, temp_lumber, rh_lumber, temp_intake, rh_intake,
mc_channel_1, mc_channel_2, heater, vents, vent_reason, exhaust_pct,
target_temp, target_rh, target_mc, stage_h, current_12v_ma, current_5v_ma
```

Note: The Pico reads CSV data from SD card. For long runs at 2-minute intervals,
a 7-day run produces ~5040 rows. Use `resolution=5` for 30-minute granularity.

**Error responses:**
- `404` if run not found or no log data for the specified time range
- `503` if SD card not mounted

---

### GET /alerts

Recent alert events from the SD event log.

**Query parameters:**

| Parameter | Type | Default | Notes |
|-----------|------|---------|-------|
| `limit` | int | 50 | Maximum rows to return |
| `level` | string | all | "WARNING" or "ERROR" to filter |
| `run` | string | current | Run filename prefix |

**Response:**

```json
{
  "alerts": [
    {
      "ts": 1711900000,
      "level": "WARNING",
      "source": "schedule",
      "message": "rh_out_of_range: actual=82.1 target=70.0 limit=78.0",
      "code": "rh_out_of_range"
    }
  ],
  "count": 1
}
```

The `code` field is extracted from the message if the message begins with a
known alert code (see schedule.py alert types). Falls back to null if not
parseable.

---

### GET /runs

List of drying run log file sets on the SD card.

**Response:**

```json
{
  "runs": [
    {
      "id": "20260328_1430",
      "started_at_str": "2026-03-28 14:30",
      "event_log": "event_20260328_1430.txt",
      "data_csv": "data_20260328_1430.csv",
      "data_rows": 1247,
      "event_count": 83,
      "size_bytes": 94210
    }
  ]
}
```

Runs are listed in reverse chronological order (newest first). An active run
is included if log files exist. The `id` field is used to reference a run in
`/history`, `/alerts`, and `/logs` endpoints.

---

### GET /logs/{run_id}/events

Returns the full content of the event log for a run.

**Response:**

```json
{
  "run": "20260328_1430",
  "lines": [
    "2026-03-28 14:30:05 [INFO ] [schedule   ] Run started",
    "2026-03-28 14:30:06 [INFO ] [heater     ] Heater on"
  ],
  "line_count": 83
}
```

**Error:** `404` if run_id not found.

### DELETE /logs/{run_id}

Deletes both the event log and data CSV for the specified run.

**Response:**

```json
{"ok": true, "deleted": ["event_20260328_1430.txt", "data_20260328_1430.csv"]}
```

**Error:** `404` if run_id not found. `409` if run_id is the currently active run.

---

### GET /sdcard/info

SD card storage usage.

**Response:**

```json
{
  "mounted": true,
  "total_bytes": 2000000000,
  "used_bytes": 94210,
  "free_bytes": 1999905790,
  "file_count": 6
}
```

---

### GET /schedules

List of schedule JSON files on the SD card.

**Response:**

```json
{
  "schedules": [
    {
      "filename": "maple_1in.json",
      "name": "Hard Maple 1 inch",
      "species": "maple",
      "thickness_in": 1.0,
      "stage_count": 9,
      "builtin": true,
      "size_bytes": 1204
    }
  ]
}
```

`builtin` is true for the four factory schedules: `maple_05in.json`,
`maple_1in.json`, `beech_05in.json`, `beech_1in.json`.

---

### GET /schedules/{filename}

Returns the full JSON content of a schedule file.

**Response:** Raw schedule JSON (see schedule_controller_spec.md for format).

**Error:** `404` if filename not found.

---

### PUT /schedules/{filename}

Create or overwrite a schedule file. Used by the Schedule Editor.

**Request body:** Raw schedule JSON (Content-Type: application/json).

**Validation performed by Pico:**
- Valid JSON
- Required top-level fields present: `name`, `species`, `stages`
- Each stage has required fields
- `target_mc_pct` rules: numeric for drying, null for equalizing/conditioning
- At least one stage

**Response on success:**

```json
{"ok": true, "filename": "my_schedule.json", "stage_count": 9}
```

**Error responses:**
- `400` with validation message if schedule is invalid
- `403` if filename matches a built-in schedule (built-ins are read-only)
- `503` if SD card not mounted

---

### DELETE /schedules/{filename}

Delete a user-created schedule file.

**Response:**

```json
{"ok": true, "deleted": "my_schedule.json"}
```

**Error responses:**
- `404` if not found
- `403` if filename matches a built-in schedule

---

### GET /calibration

Returns current moisture probe calibration data.

**Response:**

```json
{
  "channel_1_offset": -1.2,
  "channel_2_offset": -0.8,
  "calibrated_at": 1711900000,
  "source": "calibration.json"
}
```

If `calibration.json` does not exist on SD card:

```json
{
  "channel_1_offset": 0.0,
  "channel_2_offset": 0.0,
  "calibrated_at": null,
  "source": "defaults"
}
```

---

### POST /calibration

Save updated moisture probe calibration offsets to SD card as `calibration.json`.

**Request body:**

```json
{
  "channel_1_offset": -1.2,
  "channel_2_offset": -0.8
}
```

Offsets are in MC% units. The corrected MC% is computed as:
`mc_corrected = mc_raw + offset`

Pico writes `calibration.json` to SD card root and reloads it into the running
moisture module instance.

**Response:**

```json
{"ok": true, "calibrated_at": 1711900000}
```

**Error:** `503` if SD card not mounted.

---

### GET /moisture/live

Trigger a fresh moisture probe reading and return raw results. Used by the
Calibration screen "Take reading" button.

**Response:**

```json
{
  "channel_1": {
    "mc_pct": 27.3,
    "resistance_ohms": 125400,
    "temp_corrected": true,
    "temp_c": 48.7
  },
  "channel_2": {
    "mc_pct": 26.8,
    "resistance_ohms": 131200,
    "temp_corrected": true,
    "temp_c": 48.7
  }
}
```

`mc_pct` includes species correction (from schedule) and calibration offset.
`temp_corrected` is true if a valid lumber temperature reading was available.
Values are `null` if the probe read failed.

---

### POST /run/start

Start a drying run.

**Request body:**

```json
{
  "schedule": "maple_1in.json",
  "label": "Workshop maple boards batch 1"
}
```

`label` is optional.

**Response on success:**

```json
{"ok": true, "schedule": "Hard Maple 1 inch", "started_at": 1711900000}
```

**Error responses:**
- `409` if a run is already active
- `404` if schedule file not found
- `400` if schedule fails validation on load
- `503` if SD card not mounted (required for logging)

---

### POST /run/stop

Stop the active drying run immediately.

**Request body (optional):**

```json
{"reason": "manual"}
```

**Response:**

```json
{"ok": true, "stopped_at": 1711900000, "reason": "manual"}
```

**Error:** `409` if no run is currently active.

---

### POST /run/advance

Manually advance to the next stage, bypassing time and MC% checks.

**Request body (optional):**

```json
{"reason": "manual override"}
```

**Response:**

```json
{
  "ok": true,
  "previous_stage": 2,
  "new_stage": 3,
  "new_stage_name": "Stage 4 - Drying"
}
```

**Error responses:**
- `409` if no run is active
- `409` if already on the last stage (use `/run/stop` instead)

---

### POST /test/run

Start the system test suite. Returns immediately; tests run asynchronously.

**Request body:**

```json
{}
```

**Response:**

```json
{
  "ok": true,
  "test_count": 24,
  "estimated_duration_s": 300
}
```

**Error:** `409` if a test run is already in progress or a kiln run is active.

---

### GET /test/status

Poll system test progress. Returns full result array on every call (idempotent).

**Response during test:**

```json
{
  "complete": false,
  "elapsed_s": 45,
  "tests": [
    {
      "id": "circulation_init",
      "name": "Circulation fan init",
      "group": "Unit Tests",
      "status": "pass",
      "detail": "Fan started at 20% minimum speed",
      "duration_ms": 1230
    },
    {
      "id": "heater_temp_rise",
      "name": "Heater temperature rise",
      "group": "Commissioning",
      "status": "running",
      "detail": null,
      "duration_ms": null
    },
    {
      "id": "lora_tx",
      "name": "LoRa transmit",
      "group": "Commissioning",
      "status": "pending",
      "detail": null,
      "duration_ms": null
    }
  ],
  "passed": 8,
  "failed": 0,
  "skipped": 0,
  "pending": 16
}
```

**Response when complete:**

```json
{
  "complete": true,
  "elapsed_s": 287,
  "tests": [...],
  "passed": 22,
  "failed": 1,
  "skipped": 1,
  "overall": "fail"
}
```

Status values: `"pending"` | `"running"` | `"pass"` | `"fail"` | `"skip"`

Test groups: `"Unit Tests"` | `"Integration Tests"` | `"Commissioning"`

---

### GET /modules

List Python module files currently installed on the Pico filesystem.

**Response:**

```json
{
  "modules": [
    {
      "path": "lib/exhaust.py",
      "size_bytes": 4210,
      "modified": "2026-03-28 14:30"
    },
    {
      "path": "main.py",
      "size_bytes": 2841,
      "modified": "2026-03-28 09:15"
    }
  ]
}
```

Lists files in `/lib/*.py` and `main.py` at the root. Does not list schedule
JSON files (those are returned by `/schedules`).

---

### PUT /modules/{path}

Upload a Python module file. `path` is URL-encoded relative path
(e.g. `lib%2Fexhaust.py` for `lib/exhaust.py`, or `main.py`).

**Request:**
- Content-Type: `application/octet-stream`
- Body: raw file bytes

**Behaviour:**
- Writes the file to the Pico filesystem at the specified path
- After successful write, schedules a reboot (1 second delay) to reload modules
- Response is returned before reboot

**Response on success:**

```json
{
  "ok": true,
  "path": "lib/exhaust.py",
  "size_bytes": 4210,
  "rebooting": true
}
```

**Error responses:**
- `400` if path is outside allowed directories (only `lib/*.py` and `main.py`
  are permitted)
- `400` if file is larger than 512KB
- `413` if body exceeds limit

---

### GET /version

Returns firmware version information.

**Response:**

```json
{
  "firmware_version": "1.0.0",
  "micropython_version": "1.23.0",
  "board": "Pico2W"
}
```

---

## HTTP Server Implementation Notes

- Use MicroPython `asyncio` with a simple request router -- no third-party
  HTTP framework
- Run the HTTP server as an asyncio task alongside `schedule.tick()` in the
  main event loop
- Server must not block the control loop -- all handlers are async and must
  not use `time.sleep()`
- SD card reads for `/history` and `/logs` may take up to several seconds for
  large files; stream response in chunks if possible
- System test runs in a separate asyncio task; `/test/status` reads shared
  state (use a module-level result list, not a queue)
- Maximum concurrent connections: 1 (MicroPython limitation on Pico; queue
  additional requests)
- Request body size limit: 512KB (sufficient for all module files)
- CORS headers not required (native app, not browser)

---

## config.py

The following constants must be defined in `config.py` on the Pico:

```python
# config.py

VERSION       = "1.0.0"

# WiFi AP settings
AP_SSID       = "KilnController"
AP_PASSWORD   = "changeme"         # WPA2; set to empty string for open AP
AP_IP         = "192.168.4.1"

# REST API
API_KEY       = "changeme"         # Must match app Settings

# LoRa
LORA_SF       = 9
LORA_FREQ_MHZ = 433.0

# Schedule defaults
DEFAULT_SCHEDULE = "maple_1in.json"

# Logging
LOG_FLUSH_INTERVAL_S = 120
```

---

## Error Code Reference

| HTTP Code | Meaning |
|-----------|---------|
| 200 | Success |
| 400 | Bad request (validation error, malformed JSON) |
| 401 | Missing or incorrect API key |
| 403 | Forbidden (e.g. attempt to modify built-in schedule) |
| 404 | Resource not found |
| 409 | Conflict (run already active, test in progress, etc.) |
| 413 | Request body too large |
| 500 | Unexpected server error |
| 503 | Service unavailable (SD card not mounted, hardware fault) |
