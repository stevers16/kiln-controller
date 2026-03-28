# Software Project Summary and Status

This document contains a summary of the software components built for this project,
and the key technical decisions made along the way.

It is maintained by Claude Code, and provided to Claude.ai to give context on the
state of the software side of this project.

---

## Overall status

Firmware at integration stage. Twelve modules exist in `lib/`. `main.py` and
`config.py` are implemented -- entry point wires all modules together, starts
WiFi AP, runs asyncio HTTP REST API server (24 endpoints), control loop,
display pages, LoRa heartbeat, and system test suite. The logging stack is
complete and fully tested on hardware. Vent servos are working. Current
monitoring (INA219) is implemented and basic hardware test confirmed passing.
SHT31 dual sensor module is implemented and tested on hardware (refactored to
accept shared I2C bus). Heater SSR driver is implemented and tested. UART
display driver is implemented and all tests pass. Moisture probe module is
implemented with per-channel calibration offsets. Mock LoRa transmitter driver
is complete (hardware on order). Drying schedule controller is complete with
FPL-based schedules for hard maple and beech, plus public advance() method for
manual stage advancement via REST API.

Cottage-side architecture decided: Ra-02 LoRa receiver wired directly to Pi4 SPI
bus. Pi4 runs a Python daemon (`kiln_server`) that receives LoRa packets, stores
telemetry in SQLite, serves a REST API for the Kivy phone app, and pushes alerts
via ntfy.sh. No ESP32 or MQTT broker in production system.

---

## Modules -- status summary

| File | Status | Tested on hardware |
|---|---|---|
| `lib/circulation.py` | Complete | Yes -- all tests pass |
| `lib/exhaust.py` | Complete | Yes -- all tests pass |
| `lib/sdcard.py` | Complete | Yes -- all tests pass |
| `lib/logger.py` | Complete | Yes -- all tests pass |
| `lib/vents.py` | Complete | Yes -- all tests pass |
| `lib/current.py` | Complete | Basic test passing on hardware |
| `lib/SHT31sensors.py` | Complete | Yes -- all tests pass |
| `lib/heater.py` | Complete | Yes -- all tests pass |
| `lib/display.py` | Complete | Yes -- all tests pass |
| `lib/moisture.py` | Complete | Yes -- all tests pass |
| `lib/lora.py` | Mock complete | Yes -- mock tests pass on hardware |
| `lib/schedule.py` | Complete | Yes -- mock-based tests pass on hardware |
| `main.py` | Complete | Pending hardware integration test |
| `config.py` | Complete | Template -- change passwords before deploy |

---

## lib/circulation.py

Controls 3x 120mm 4-pin PWM circulation fans wired as a group.

- PWM on GP17, MOSFET gate on GP19
- `on(speed_percent)` clamps to `MIN_START_PCT=20` to prevent stall
- `off()` zeros PWM then pulls gate low
- `tick()` call once per minute to accumulate `hours_on`
- `read_rpm()` returns `None` (no tach on these fans)
- Accepts optional `logger=None`; calls `logger.event()` on `on()` and `off()`
- Accepts optional `current_monitor=None` (INA219 on 12V rail)
- `on()` calls `verify_running(200, 500)` after 1s spin-up delay
- `verify_running(min_mA, max_mA)`: checks 12V rail current; logs WARN if out of range; returns None if no monitor
- All unit tests pass on hardware; current monitoring tests (9-11) added, require INA219 on I2C

---

## lib/exhaust.py

Controls the 80mm Foxconn PVA080G12Q exhaust fan.

- PWM on GP16, MOSFET gate on GP21 (separate pins -- gate is the hard on/off)
- Gate initialised LOW at boot before PWM is configured
- `on(speed_percent)`: drives gate HIGH, then sets PWM duty
- `off()`: zeros PWM duty first, then pulls gate LOW
- Tach on GP22 via falling-edge IRQ; `read_rpm(sample_ms=2000)` available
- `set_speed()` is a no-op if fan is not running
- Accepts optional `logger=None`; calls `logger.event()` on `on()` and `off()`
- All unit tests pass on hardware

---

## lib/sdcard.py

Wraps the MicroPython SPI sdcard driver and `uos.mount()`.

- SPI0 at 400 kHz init speed (GP2=SCK, GP3=MOSI, GP4=MISO, GP5=CS) -- pin constants
  were briefly transposed (MISO/SCK swapped) causing a "bad SDK pin" error; corrected
- CS pin driven HIGH before SPI is initialised (prevents spurious transactions)
- Uses `uos.VfsFat(sd)` wrapper before `uos.mount()` (required in MicroPython 1.20+)
- Imports the raw driver as `sdcard_driver` (not `sdcard`) to avoid a naming
  conflict with this wrapper file -- see deployment note below
- Silent fail: prints REPL warning, returns `False` on any exception
- `is_mounted()`, `mount_point` property, safe `unmount()`
- `listdir(subdir="")`: returns sorted list of filenames at the SD root or a
  subdirectory; returns `[]` if not mounted or path missing
- `read_text(filename)`: returns full contents of a text file as a string;
  filename is relative to the mount point; returns `None` on any failure
- All unit tests pass on hardware

**Deployment note:** The MicroPython SPI sdcard Python driver must be deployed to
the Pico as `sdcard_driver.py` (NOT `sdcard.py`). A copy lives at the repo root
for convenience. Deploy with:
```
mpremote cp sdcard_driver.py :sdcard_driver.py
mpremote cp lib/sdcard.py :lib/sdcard.py
```

**Deployment script:** `update_lib.py` at repo root copies all files from `lib/`
to `/lib/` on the Pico in one step: `python update_lib.py`

---

## lib/logger.py

Single logging service for the entire kiln firmware.

- Owns one `SDCard` instance passed in at construction
- `begin_run()`: mounts SD, creates `event_YYYYMMDD_HHMM.txt` and
  `data_YYYYMMDD_HHMM.csv`, writes CSV header, logs `Run started`
- `end_run()`: logs `Run ended`, flushes and closes files, unmounts SD
- `event(source, message, level="INFO")`: timestamped line to event log and REPL.
  Format: `2026-03-17 14:30:05 [INFO ] [exhaust    ] Fan on at 75%`
- `data(record)`: appends CSV row from dict; missing keys written as empty;
  floats to 2 dp; bools as `1`/`0`; always flushes
- Timestamp falls back to `+NNNNNs` elapsed seconds if RTC not yet set (year < 2024)
- File suffix falls back to `run_NNNNN` if RTC not set
- Silent fail on all SD writes -- kiln keeps running if card fails mid-run

**Integration test:** `test_logging.py` at repo root exercises logger + circulation
fan events end-to-end. All tests pass on hardware.

---

## Logging spec -- implementation decisions

These decisions were made during the logging_spec.md work and differ from defaults
worth noting:

- **Driver naming:** Raw sdcard driver deployed as `sdcard_driver.py` to avoid
  Python import resolving to our own `lib/sdcard.py` wrapper when `import sdcard`
  is called inside `mount()`.
- **VfsFat required:** `uos.mount()` in MicroPython 1.20+ requires a `VfsFat`
  object, not a bare block device. The spec originally said `uos.mount(sd, path)`
  directly; the implementation uses `uos.VfsFat(sd)` first.
- **Logger does not import SDCard:** Logger accepts an `SDCard` instance injected
  at construction. SDCard is not imported inside logger.py.
- **All modules use dependency injection for logger:** Modules accept
  `logger=None` in `__init__` and call `logger.event()` only when provided.
  Logger is never imported directly by hardware modules.
- **ASCII only in all strings:** All print statements, logger calls, comments,
  and docstrings use ASCII characters only. No Unicode (em/en dashes, arrows,
  degree/ohm signs, etc.). Use `-`, `->`, `deg`, `ohm` etc. as substitutes.
  Enforced across all `.py` files.

---

## lib/vents.py

Controls 2x MG90S servos driving butterfly-style intake and exhaust dampers.

- PWM on GP14 (intake) and GP15 (exhaust); 50 Hz standard hobby servo frequency
- Both servos always commanded together (open or closed)
- PWM de-energized after each move (deinit after 600ms travel time) to prevent
  holding torque, buzz, and heat; PWM objects re-initialised on every move
- `open()` -> duty 6225 (1.9 ms pulse); `close()` -> duty 3604 (1.1 ms pulse)
- Pulse range inset from 1.0-2.0 ms spec to protect homemade linkage
- `is_open()` reflects last commanded position (no position sensing hardware)
- `__init__` calls `close()` so physical position matches software state at boot
- Accepts optional `logger=None`; calls `logger.event("vents", ...)` on open/close
- Accepts optional `current_monitor=None` (INA219 on 5V rail)
- `_move()` samples 5V rail current at mid-travel (300ms in) and caches in `_last_movement_mA`
- `verify_position(min_mA, max_mA)`: checks cached mid-travel current; fault threshold >600mA (stall/jam); returns None if no monitor or no move made yet
- All unit tests pass on hardware; current monitoring tests (5-7) added, require INA219 on I2C

---

## lib/current.py

Reads DC current, bus voltage, and power from INA219 modules via I2C0.

- Two instances: 0x40 for 12V rail, 0x41 for 5V rail
- Raw I2C register access - no external library
- Calibrated for 0.1ohm shunt: Cal=0x1000, Current LSB=0.1mA, Power LSB=2mW
- `read()` returns dict with `bus_voltage_V`, `current_mA`, `power_mW`, `label`; returns `None` on I2C failure
- `check_range(min_mA, max_mA)` returns True/False/None; logs WARN if out of range
- Accepts `logger=None`; uses source `"current_12v"` / `"current_5v"`
- I2C instance passed in - not created internally (shared with SHT31 when built)
- Silent fail: init errors printed to REPL; `read()` returns None on exception
- Basic hardware test confirmed passing

---

## Measured hardware baselines

Measured 2026-03-22 with INA219 modules.

| Rail | Condition | Current | Notes |
|------|-----------|---------|-------|
| 12V (0x40) | Idle | 39 mA | Fans off, heater off |
| 12V (0x40) | 3x circ fans at 75% | 200-500 mA | Expected operating range |
| 5V (0x41) | Idle | 8 mA | Servos de-energized |

---

## lib/SHT31sensors.py

Reads temperature and relative humidity from two SHT31-D sensors over I2C.

- Single shared I2C bus on GP0 (SDA) / GP1 (SCL), freq 100kHz
- Sensor A at 0x44 (ADDR pin low) -- lumber zone
- Sensor B at 0x45 (ADDR pin high) -- intake
- Constructor scans bus and raises RuntimeError if either address is missing
- `read()` returns dict with `temp_lumber`, `rh_lumber`, `temp_intake`, `rh_intake`
- `read_lumber()` / `read_intake()` convenience methods return (temp_c, rh_pct) tuple
- `soft_reset()` sends 0x30A2 to both sensors with 2ms delay
- Single-shot high-repeatability measurement (0x2C06), 15ms wait, 6-byte read
- CRC-8 verification (poly 0x31, init 0xFF) on both temp and RH words
- Silent fail: returns None for any sensor that fails (CRC, I2C, timeout)
- Accepts optional `logger=None`; calls `logger.event("sensors", ..., level="WARNING")` on failures
- No third-party libraries -- SHT31 protocol implemented directly
- Accepts optional `i2c` parameter for shared bus; creates its own I2C0 instance if not provided
- When `i2c` is passed in, `sda_pin`/`scl_pin`/`freq` are ignored (bus already configured)

---

## lib/heater.py

Controls the 500W backup ceramic PTC heater via a Fotek SSR-25DA solid-state relay.

- SSR control pin on GP18 (digital output through 1k ohm current-limiting resistor)
- Pin driven LOW at construction before anything else -- safe boot state
- `on()` drives pin HIGH; `off()` drives pin LOW
- `is_on()` returns software-tracked state (does not read GPIO)
- No PWM or duty cycling -- simple on/off; temperature regulation is the controller's job
- No current monitoring -- SSR switches 120V AC; INA219 monitors DC rails only
- Hardware safety: RY85 85degC one-time thermal fuse on AC output (firmware not involved)
- Accepts optional `logger=None`; calls `logger.event("heater", ...)` on init/on/off
- Unit tests included in module; all 8 tests cover init state, on/off, double-call safety, logger events, and logger=None

---

## lib/display.py

Driver for JC035-HVGA-ST-02-V02 3.5" UART serial display.

- UART1 on GP8 (TX) / GP9 (RX)
- Default baud rate 115200; 1-second post-power-on delay enforced in constructor
- All commands are ASCII strings terminated with `\r\n`; display replies `OK\r\n`
- `clear(color)`, `set_orientation()`, `set_background_color()`, `set_backlight()`
- Drawing primitives: `draw_pixel()`, `draw_line()`, `draw_rectangle()`, `draw_circle()`
- Text: `draw_text()` supports sizes 16/24/32/48/72 with optional background fill
- Widgets: `draw_button()`, `draw_qr()`
- Scrolling text console: `write_characters()` with auto line-wrap and scroll
- `get_version()` displays firmware version on screen (no UART response)
- `_sanitise()` strips commas and semicolons from user text (display treats them as delimiters)
- Button on GP20 (active-low, internal pull-up, polling with 50ms debounce)
- Auto-timeout: backlight off after configurable idle period (default 30s); `timeout_s=0` disables
- Page system: `register_page(name, render_fn)` adds named pages; button cycles through them
- `show_page(name)` for programmatic navigation; `current_page_name` property
- `tick()` called from main loop -- handles button debounce, page cycling, and timeout blanking
- `reset_idle()` resets inactivity timer (call when app refreshes display content)
- Button wake redraws current page without advancing; page advance only when awake with 2+ pages
- Accepts no logger yet -- can be added when main.py integration begins
- All unit tests pass on hardware (button/timeout/page tests included)

---

## lib/moisture.py

Reads wood moisture content (MC%) from two resistive probe channels.

- Ch1: excitation GP6, ADC GP26 (maple); Ch2: excitation GP7, ADC GP27 (beech)
- AC excitation: drive HIGH -> 15ms settle -> 5 ADC samples -> drive LOW -> 10ms discharge
- 100kohm reference resistor voltage divider; R_wood calculated from ADC voltage
- Log-linear interpolation on 12-point resistance-to-MC% lookup table (FPL Wood Handbook)
- Species correction offsets: maple -0.5, beech -0.3, oak +0.5, pine +0.3
- `read_resistance()` returns raw ohms; `read()` returns MC% + ohms
- `read_with_temp_correction(temp_c)` applies -0.06 MC%/degC above 20degC reference
- `set_calibration(channel_1_offset, channel_2_offset)` applies per-channel MC% offsets loaded from SD card calibration.json at boot; corrected MC% = raw MC% + offset
- Module-level `resistance_to_mc(r_ohms, species)` function for standalone use
- Accepts optional `logger=None`; logs WARNING on None readings or out-of-range resistance
- Silent fail: excitation pin forced LOW on any exception
- 9 unit tests included; test 5 requires manual probe disconnect; test 9 covers calibration offsets
- GP6/GP7 used for AC excitation (moved from GP12/GP13 to free SPI1 block for LoRa)

---

## lib/lora.py

Mock LoRa transmitter driver for AI-Thinker Ra-02 (SX1278, 433 MHz).

- Mock implementation for development while Ra-02 modules are on order
- SPI1 on GP10 (SCK), GP11 (MOSI), GP12 (MISO), GP13 (CS), RST on GP28
- DIO0 interrupt on GP20 (wired but not used in TX-only mock)
- TX-only -- no receive path on the Pico side
- `send(payload: bytes) -> bool` -- raw byte transmission (mock always returns True)
- `send_telemetry(data: dict) -> bool` -- JSON serialise and send
- `send_alert(code: str, message: str) -> bool` -- with 3x retry, 2s spacing
- `reset()` -- radio reset stub
- `is_mock` property returns True; `tx_count` and `last_payload` for inspection
- Accepts optional `logger=None`; source string "lora"
- 12 unit tests included, all mock-based

**Real driver (pending Ra-02 hardware):** Will replace mock. Same interface, same pin
assignments. `is_mock` returns False. Implements SX1278 register configuration and
TxDone polling (reg 0x12 bit 3, 5ms poll interval, 2s timeout).

---

## lib/schedule.py

Drying schedule controller -- top-level control logic for multi-stage kiln drying.

- Orchestrates heater, exhaust, vents, circulation, sensors, moisture, LoRa
- Loads schedule JSON from SD card via `load(schedule_path)` -- validates all stages
- `start()` begins from stage 0; `stop(reason)` halts with safe shutdown
- `tick()` called from main loop -- reads sensors, controls heater/vents, checks advance
- `status()` returns full state dict for REST API and display
- `tick_interval_s` property: 30s while venting, 120s otherwise
- Temperature control: deadband heater with fault detection (20 min no-rise alert)
- RH control: vent-only with cold suppression; overheat venting takes priority
- Stage advance: drying stages use MC% + time; equalizing/conditioning are time-only
- LoRa alerts: stage_advance, stage_goal_not_met, equalizing_start, conditioning_start, run_complete, temp/rh out of range, sensor_failure, heater_fault
- Alert rate limiting: same alert type suppressed for 30 min (one-shot alerts bypass)
- Equalizing/conditioning entry alerts include water pan reminder
- Schedule JSON files in `schedules/` directory: maple_05in, maple_1in, beech_05in, beech_1in
- All schedules based on FPL-GTR-57/118 kiln-drying data
- 12 unit tests using mock objects

---

## Cottage-side architecture

**Decision:** Ra-02 LoRa receiver wired directly to Pi4 GPIO/SPI0. No ESP32 or MQTT
broker in production system.

**Data flow:**
```
Pico --> SPI1 --> Ra-02 ~~LoRa~~ Ra-02 --> SPI0 --> Pi4 kiln_server daemon
                                                 --> SQLite (telemetry + alerts + runs)
                                                 --> REST API (port 8080) --> Kivy app
                                                 --> ntfy.sh --> phone notifications
```

**Pi4 `kiln_server` package** (not yet implemented):
- `lora_receiver.py` -- SX1278 init, DIO0 interrupt-driven receive loop
- `database.py` -- SQLite insert and query (telemetry, alerts, runs tables)
- `api.py` -- Flask/FastAPI REST endpoints: /status, /history, /alerts, /runs, /health
- `notifier.py` -- ntfy.sh HTTP POST on alert receipt
- `config.py` -- only file that differs between bench Pi4 and cottage Pi4

**SQLite schema:** telemetry table stores one row per LoRa packet (30s interval);
alerts table stores fault events; runs table tracks drying run start/end.

**REST API `/history` response** uses columnar format (fields array + rows array of
arrays) to minimise payload size for Kivy plot queries over long runs.

**Bench testing:** Spare Pi4 available at home. Bench setup is identical to cottage
deployment -- only `config.py` network addresses differ. ESP32-WROOM-32 (owned) can
be used to test Pico TX before Pi4 daemon is written, but is not part of production.

**Spec:** See `lora_telemetry_spec.md` for full wiring tables, SQLite schema, REST API
endpoint definitions, and test plan.

---

## main.py

Entry point wiring all `lib/` modules together. Runs at boot.

- Instantiates all 12 hardware modules in safe order with shared I2C bus
- Starts WiFi AP (SSID/password from config.py)
- Runs asyncio HTTP server on port 80 with 24 REST API endpoints
- Control loop: `schedule.tick()` + status cache update at `tick_interval_s`
- Display loop: `display.tick()` every 100ms with 4 registered pages (status, sensors, moisture, system)
- LoRa heartbeat: sends telemetry every 5 min when no run is active
- RPM reader: caches exhaust fan RPM every 10s (avoids blocking 2s tach read in status path)
- System test suite: 18 tests (unit, integration, commissioning) run as async task via POST /test/run
- Calibration loading from SD card `calibration.json` at boot
- Fatal exception handler: safe shutdown (heater off, vents open, fans off) then reboot after 5s
- Authentication via `X-Kiln-Key` header on all endpoints except /health and /version

## config.py

Template configuration file with defaults. Must be edited before first deployment.

- VERSION, AP_SSID, AP_PASSWORD, API_KEY
- USE_MOCK_LORA, LORA_SF, LORA_FREQ_MHZ
- DEFAULT_SCHEDULE, DISPLAY_TIMEOUT_S, LOG_FLUSH_INTERVAL_S

---

## What still needs building

In rough priority order:

1. **Real `lib/lora.py`** -- replace mock when Ra-02 hardware arrives
2. **`kiln_server/` Pi4 daemon** -- LoRa RX, SQLite storage, REST API, ntfy.sh alerts
3. **Kivy Android app** -- mobile interface; queries Pi4 REST API for history/plots,
   Pico AP REST API for live control