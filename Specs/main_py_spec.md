# MAIN_PY_SPEC.md

Spec for `main.py` -- the entry point for the Pico 2W kiln controller firmware.
This is the top-level module that wires all `lib/` modules together, starts the
WiFi AP, runs the HTTP REST API server, and executes the control loop.

---

## Overview

`main.py` runs at boot. It:

1. Initialises all hardware modules in safe order
2. Starts the WiFi Access Point
3. Launches the asyncio HTTP server (REST API)
4. Runs the `KilnSchedule.tick()` control loop
5. Handles uncaught exceptions with a safe shutdown and reboot

The HTTP server and the control loop run concurrently as asyncio tasks. The
control loop must never be blocked by HTTP handler execution.

---

## Files

| File | Action |
|------|--------|
| `main.py` | Create at repo root |
| `config.py` | Create at repo root (see template below) |
| `calibration.json` | Created at runtime by `/calibration` POST endpoint |

---

## Module Instantiation Order

Order matters. Hardware is driven to a safe state at each constructor. Follow
this sequence exactly:

```python
# 1. SD card first -- logger depends on it
sdcard = SDCard(...)

# 2. Logger -- all modules may accept logger after this point
logger = Logger(sdcard)

# 3. Shared I2C bus (SHT31 sensors + INA219 current monitors)
i2c0 = machine.I2C(0, sda=machine.Pin(0), scl=machine.Pin(1), freq=100_000)

# 4. Sensors (I2C0, shared bus)
sensors = SHT31Sensors(i2c=i2c0, logger=logger)

# 5. Current monitors (I2C0, same bus)
monitor_12v = CurrentMonitor(i2c0, 0x40, "12V", logger=logger)
monitor_5v  = CurrentMonitor(i2c0, 0x41, "5V",  logger=logger)

# 6. Circulation fans
circulation = CirculationFans(pwm_pin=18, gate_pin=19,
                               current_monitor=monitor_12v, logger=logger)

# 7. Exhaust fan
exhaust = ExhaustFan(pwm_pin=17, gate_pin=21, tach_pin=22, logger=logger)

# 8. Vents
vents = Vents(intake_pin=14, exhaust_pin=15, logger=logger)

# 9. Heater
heater = Heater(pin=16, logger=logger)

# 10. Moisture probes -- load calibration offsets from SD if available
moisture = MoistureProbe(
    ch1_excite=6, ch1_adc=26,
    ch2_excite=7, ch2_adc=27,
    logger=logger
)
_load_calibration(moisture, sdcard)    # see Calibration Loading section

# 11. Display
display = KilnDisplay(uart_id=1, tx_pin=8, rx_pin=9,
                       button_pin=20, logger=logger)

# 12. LoRa (mock or real depending on config.USE_MOCK_LORA)
if config.USE_MOCK_LORA:
    lora = LoRaMock(logger=logger)
else:
    lora = LoRa(spi_id=1, sck=10, mosi=11, miso=12, cs=13,
                rst=28, logger=logger)

# 13. Schedule controller -- depends on all above
schedule = KilnSchedule(
    sdcard=sdcard, sensors=sensors, moisture=moisture,
    heater=heater, exhaust=exhaust, circulation=circulation,
    vents=vents, lora=lora, logger=logger
)
```

**I2C sharing:** `SHT31Sensors` accepts an optional `i2c` parameter. When
provided, it uses the shared bus directly instead of creating its own.
`CurrentMonitor` also accepts an external `i2c` instance. Both refactors
are complete -- see instantiation order above for the shared `i2c0` pattern.

---

## Boot Sequence

```
1. Import all modules
2. Print boot banner to REPL (firmware version, board)
3. Instantiate all hardware modules (order above)
4. Start WiFi AP (SSID and password from config.py)
5. Log boot event: logger.event("main", "Boot complete. AP started.")
6. Register display pages (see Display Pages section)
7. Start asyncio event loop:
   a. Task: HTTP server (port 80)
   b. Task: control loop (calls schedule.tick() + display.tick())
   c. Task: LoRa keepalive heartbeat (if no active run)
8. Run loop forever; handle KeyboardInterrupt gracefully
```

---

## WiFi AP

```python
import network

ap = network.WLAN(network.AP_IF)
ap.config(ssid=config.AP_SSID, password=config.AP_PASSWORD,
          security=network.WPA2 if config.AP_PASSWORD else 0)
ap.active(True)

# Wait for AP to become active (poll ap.active() with 100ms delay, max 5s)
# Log AP IP address once active
```

AP IP is always `192.168.4.1` by default on MicroPython.

---

## Control Loop

The control loop runs as an asyncio task. It:

1. Calls `schedule.tick()` at the interval returned by
   `schedule.tick_interval_s` (30s when venting, 120s otherwise)
2. Calls `display.tick()` every 100ms (button debounce, timeout, page refresh)
3. Updates the cached `/status` response (module-level dict) after each tick

```python
async def control_loop():
    while True:
        schedule.tick()
        _update_status_cache()
        await asyncio.sleep(schedule.tick_interval_s)

async def display_loop():
    while True:
        display.tick()
        await asyncio.sleep(0.1)
```

The `_update_status_cache()` function reads `schedule.status()` and stores the
result in a module-level dict. All `/status` HTTP responses read from this cache
-- they do not call `schedule.status()` directly, avoiding any timing conflict.

---

## HTTP Server

Use MicroPython `asyncio` streams to implement a minimal HTTP/1.1 server.
Do not use any third-party HTTP framework.

```python
async def http_server():
    server = await asyncio.start_server(handle_request, "0.0.0.0", 80)
    async with server:
        await server.serve_forever()
```

**Request routing:**

Implement a simple router that maps `(method, path_prefix)` tuples to handler
coroutines. Path parameters (e.g. `/schedules/{filename}`) are extracted by
string splitting.

**Authentication:**

Every handler (except `/health`) checks for the `X-Kiln-Key` header before
processing. If missing or wrong, return 401 immediately.

**Response helpers:**

```python
def json_response(writer, data, status=200):
    body = json.dumps(data)
    writer.write(f"HTTP/1.1 {status} OK\r\n"
                 f"Content-Type: application/json\r\n"
                 f"Content-Length: {len(body)}\r\n"
                 f"\r\n{body}")
```

---

## RTC Sync

On `POST /time`, set the Pico RTC:

```python
import machine
rtc = machine.RTC()

def set_rtc(unix_ts):
    # Convert Unix timestamp to (year, month, day, weekday, hour, min, sec, subsec)
    import utime
    t = utime.localtime(unix_ts)
    rtc.datetime((t[0], t[1], t[2], t[6], t[3], t[4], t[5], 0))
```

Auto-sync: the first app connection triggers a `/time` POST automatically when
the app's "Auto-sync on connect" setting is enabled and the `/health` response
shows `rtc_set: false`.

---

## Calibration Loading

At boot, after `MoistureProbe` is instantiated, attempt to load
`calibration.json` from the SD card:

```python
def _load_calibration(moisture_probe, sdcard):
    text = sdcard.read_text("calibration.json")
    if text is None:
        return    # no file -- factory defaults (0.0 offset)
    try:
        cal = json.loads(text)
        moisture_probe.set_calibration(
            channel_1_offset=cal.get("channel_1_offset", 0.0),
            channel_2_offset=cal.get("channel_2_offset", 0.0)
        )
    except Exception as e:
        logger.event("main", f"Calibration load failed: {e}", level="WARNING")
```

`MoistureProbe.set_calibration(channel_1_offset, channel_2_offset)` is a new
method to be added to `lib/moisture.py` as part of this work.

The `calibration.json` format:

```json
{
  "channel_1_offset": -1.2,
  "channel_2_offset": -0.8,
  "calibrated_at": 1711900000
}
```

Corrected MC% = raw MC% + offset (offset may be negative or positive).

---

## Display Pages

Register the following pages on the `KilnDisplay` instance:

```python
display.register_page("status",   render_status_page)
display.register_page("sensors",  render_sensors_page)
display.register_page("moisture", render_moisture_page)
display.register_page("system",   render_system_page)
```

Page render functions read from the cached status dict and use `display` drawing
primitives to render the page. They are called by `display.tick()` when the page
changes or the display wakes from timeout.

**Page 1 -- Status:**
- Stage name + number
- Stage type
- Stage elapsed hours
- Heater / vent / fan state indicators

**Page 2 -- Sensors:**
- Lumber temp + RH (actual vs target)
- Intake temp + RH

**Page 3 -- Moisture:**
- Channel 1 MC% + resistance
- Channel 2 MC% + resistance
- Target MC% for current stage

**Page 4 -- System:**
- 12V rail current (mA)
- 5V rail current (mA)
- Uptime
- SD card mounted indicator
- LoRa TX count

---

## LoRa Heartbeat

When no run is active, transmit a heartbeat packet every 5 minutes so the Pi4
daemon can confirm the Pico is alive and the link is up.

```python
async def lora_heartbeat():
    while True:
        if not schedule._running:
            lora.send_telemetry({
                "type": "heartbeat",
                "ts": utime.time(),
                "uptime_s": utime.ticks_ms() // 1000,
                "run_active": False
            })
        await asyncio.sleep(300)
```

The Pi4 daemon records heartbeat packets in the `telemetry` table with
`run_id = NULL`.

---

## System Test Implementation

The system test suite runs as an asyncio task started by `POST /test/run`.
Results are stored in a module-level list that `/test/status` reads.

Test definitions are in a list of `(id, name, group, test_fn)` tuples where
`test_fn` is a coroutine that returns `(status, detail)`.

**Test list:**

Unit Tests (per module):
- `exhaust_init` -- ExhaustFan: fan starts at 50%, RPM > 0, stops cleanly
- `exhaust_tach` -- ExhaustFan: tach reads non-zero RPM at 75%
- `circulation_init` -- CirculationFans: starts at 20% minimum, no current fault
- `vents_open_close` -- Vents: open, verify servo moved, close
- `heater_on_off` -- Heater: on, verify GPIO high, off, verify GPIO low
- `sdcard_mount` -- SDCard: mounts successfully, listdir returns a list
- `sensors_read` -- SHT31Sensors: both sensors return plausible temp/RH values
- `moisture_read` -- MoistureProbe: both channels return resistance values
- `current_12v` -- CurrentMonitor 0x40: returns non-zero mA reading
- `current_5v` -- CurrentMonitor 0x41: returns non-zero mA reading
- `display_init` -- KilnDisplay: clear and draw text without exception
- `lora_mock_tx` -- LoRa: send_telemetry returns True; if mock, note in detail

Integration Tests:
- `schedule_load` -- KilnSchedule: loads maple_1in.json successfully
- `logger_event` -- Logger: writes an event to SD card, reads it back
- `logger_data` -- Logger: writes a data row, reads it back

Commissioning Tests:
- `heater_temp_rise` -- Heater on for 90s; temp_lumber rises >= HEATER_FAULT_RISE_C;
  heater off. SKIP if temp_lumber already > 50C (too hot to test safely).
- `lora_tx_real` -- Send a real LoRa packet; SKIP if config.USE_MOCK_LORA is True
- `rtc_set` -- RTC year >= 2024; FAIL with "Sync clock via app" if not set

All tests must leave hardware in a safe state on completion (heater off, fans off
or at idle, vents closed).

---

## Exception Handling and Watchdog

Wrap the main asyncio entry point in a try/except:

```python
try:
    asyncio.run(main())
except Exception as e:
    # Safe shutdown
    heater.off()
    vents.open()      # fail-open for ventilation
    circulation.off()
    exhaust.off()
    logger.event("main", f"Fatal exception: {e}", level="ERROR")
    import machine
    machine.reset()   # reboot after 5 seconds
```

A MicroPython software watchdog timer (WDT) may be added in a later iteration.
For now, the asyncio loop itself is the liveness mechanism.

---

## config.py Template

```python
# config.py -- deploy to Pico root

VERSION          = "1.0.0"

# WiFi AP
AP_SSID          = "KilnController"
AP_PASSWORD      = "changeme"      # WPA2; empty string for open AP

# REST API
API_KEY          = "changeme"      # must match Kivy app Settings

# LoRa
USE_MOCK_LORA    = True            # set False when Ra-02 hardware is installed
LORA_SF          = 9
LORA_FREQ_MHZ    = 433.0

# Schedule
DEFAULT_SCHEDULE = "maple_1in.json"

# Display
DISPLAY_TIMEOUT_S = 30             # seconds before backlight off; 0 to disable

# Logging
LOG_FLUSH_INTERVAL_S = 120         # seconds between forced SD flushes
```

---

## Constraints and Patterns

- **ASCII only.** No Unicode in any string, comment, or docstring.
- **asyncio throughout.** No `time.sleep()` in any task -- use
  `await asyncio.sleep()`. Blocking the event loop blocks the HTTP server.
- **No global mutable state except status cache.** The status cache dict is the
  only module-level mutable state intentionally shared between tasks.
- **Safe boot state.** All hardware modules drive outputs to safe state in their
  constructors. `main.py` must not assume any GPIO state from a previous run.
- **Silent SD failures.** SD card errors must never crash the control loop.
  All SD reads/writes are wrapped in try/except.
- **Follow exhaust.py patterns.** Class-based modules, logger dependency injection,
  no direct hardware imports in main.py beyond `machine` and `network`.
- **No third-party libraries.** Standard MicroPython + existing `lib/` modules only.

---

## Open Items and Prerequisites

Before `main.py` can be fully implemented, the following prerequisite changes
are required in existing modules:

1. ~~**SHT31Sensors I2C refactor:**~~ DONE. Accepts optional `i2c` parameter;
   falls back to creating its own if not provided.

2. ~~**CurrentMonitor I2C refactor:**~~ DONE. Already accepted external `i2c`
   instance at initial implementation.

3. ~~**MoistureProbe.set_calibration():**~~ DONE. Applies per-channel MC% offsets;
   corrected MC% = raw MC% + offset. Offsets default to 0.0.

4. **Real lib/lora.py:** Replace mock with SX1278 register implementation once
   Ra-02 hardware arrives. Same interface as mock; `is_mock` returns False.

5. **lib/schedule.py status() field alignment:** Ensure `status()` return dict
   keys match the field names in the Pico REST API `/status` response exactly
   (including `mc_channel_1`, `mc_channel_2`, `current_12v_ma`, `current_5v_ma`).

Prerequisites 1-3 are complete. `main.py` implementation can proceed.
