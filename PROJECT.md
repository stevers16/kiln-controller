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
display pages, LoRa heartbeat, and system test suite. Error checking and
fault surfacing (`Specs/error_checking_spec.md`) is implemented: every module
exposes a uniform fault contract, `main.py` aggregates faults from all modules
via `_collect_module_faults()`, and `/status` returns `active_alerts` + 
`fault_details` (with three-tier severity: fault/notice/info). The control
loop runs every 10s for responsive fault detection and clearing. The logging stack is
complete and fully tested on hardware. Vent servos are working. Current
monitoring (INA219) is implemented and basic hardware test confirmed passing.
SHT31 dual sensor module is implemented and tested on hardware (refactored to
accept shared I2C bus). Heater SSR driver is implemented and tested. UART
display driver is implemented and all tests pass. Moisture probe module is
implemented with per-channel calibration offsets. Real LoRa transmitter driver
is complete and verified (Pico->Pi4 link tested). Drying schedule controller is complete with
FPL-based schedules for hard maple and beech, plus public advance() method for
manual stage advancement via REST API.

Cottage-side architecture decided: Ra-02 LoRa receiver wired directly to Pi4 SPI
bus. Pi4 runs a Python daemon (`kiln_server`) that receives LoRa packets, stores
telemetry in SQLite, serves a REST API for the Kivy phone app, and pushes alerts
via ntfy.sh. Daemon is now implemented (`kiln_server/`, per
`Specs/Pi4_demon_spec.md`) and running on a local Pi. The Phase 14 Kivy work
augmented the daemon's REST surface so the Cottage-mode Kivy app sees the same
shape it sees from the Pico: `/status` synthesises run_active /
schedule_name / fault_details / stage_index / last_packet_age_s,
`/alerts` rows carry tier+level+source, and `/runs` rows carry the
Pico-style formatted timestamps and data_rows/event_count aliases.
No ESP32 or MQTT broker in production system.

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
| `lib/lora.py` | Complete | Yes -- real SX1278 driver, TX verified Pico->Pi4 |
| `lib/schedule.py` | Complete | Yes -- mock-based tests pass on hardware |
| `main.py` | Complete | Pending hardware integration test |
| `config.py` | Complete | Template -- change passwords before deploy |

---

## lib/circulation.py

Controls 3x 120mm 4-pin PWM circulation fans wired as a group.

- PWM on GP18, MOSFET gate on GP19
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

- PWM on GP17, MOSFET gate on GP21 (separate pins -- gate is the hard on/off)
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

**Deployment script:** `update_lib.py` at repo root copies `main.py`, `config.py`,
and all files from `lib/` to the Pico in one step: `python update_lib.py`

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
- Accepts optional `logger=None`; calls `logger.event("sensors", ..., level="WARN")` on failures
- No third-party libraries -- SHT31 protocol implemented directly
- Accepts optional `i2c` parameter for shared bus; creates its own I2C0 instance if not provided
- When `i2c` is passed in, `sda_pin`/`scl_pin`/`freq` are ignored (bus already configured)

---

## lib/heater.py

Controls the 500W backup ceramic PTC heater via a Fotek SSR-25DA solid-state relay.

- SSR control pin on GP16 (digital output through 1k ohm current-limiting resistor)
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

Real LoRa transmitter driver for AI-Thinker Ra-02 (SX1278, 433 MHz).

- Real SX1278 SPI driver -- replaced mock implementation after Ra-02 hardware arrived
- SPI1 on GP10 (SCK), GP11 (MOSI), GP12 (MISO), GP13 (CS), RST on GP28
- DIO0 not connected on Pico side -- TX completion uses register polling (RegIrqFlags 0x12 bit 3, 5ms poll interval, 2s timeout)
- TX-only -- no receive path on the Pico side
- Init sequence: hardware reset, verify version register (0x12), configure frequency/BW/SF/CR/power, set FIFO base, clear IRQ flags
- RF params: 433 MHz, BW 125 kHz, SF9, CR 4/5, 17 dBm PA_BOOST, 8-symbol preamble, public sync word (0x12)
- `send(payload: bytes) -> bool` -- writes FIFO, triggers TX, polls TxDone, returns to sleep
- `send_telemetry(data: dict) -> bool` -- JSON serialise and send; field names match Pi4 SQLite schema (ts, stage, temp_lumber, temp_intake, humidity_lumber, humidity_intake, mc_channel_1, mc_channel_2, exhaust_fan_rpm, exhaust_fan_pct, circ_fan_on, heater_on, vent_open)
- `send_alert(code: str, message: str) -> bool` -- with 3x retry, 2s spacing
- `reset()` -- pulses RST low for 10ms
- `tx_count` and `last_payload` properties for inspection
- Accepts optional `logger=None`; source string "lora"; logs on init, send success, send timeout, and RST events
- Silent fail on SPI errors: returns to sleep mode, logs error, returns False
- 13 unit tests included (hardware-in-the-loop); test 13 verifies radio works after reset + reinit
- End-to-end TX verified: Pico -> Pi4 LoRa link confirmed working

**Alert codes (from lora_telemetry_spec):** OVER_TEMP, SENSOR_FAIL, FAN_STALL,
HEATER_TIMEOUT, SD_FAIL, LORA_TIMEOUT, STAGE_COMPLETE, SCHEDULE_DONE

**Test scripts:**
- `lora_test_tx.py` -- Pico-side: sends numbered JSON messages every 5s via lib/lora.py
- `lora_test_rx.py` -- Pi4-side: validates Ra-02 hardware (6 tests), then listens for packets with RSSI/SNR readout

---

## lib/schedule.py

Drying schedule controller -- top-level control logic for multi-stage kiln drying.

- Orchestrates heater, exhaust, vents, circulation, sensors, moisture, LoRa
- Loads schedule JSON from SD card via `load(schedule_path)` -- validates all stages
- `start()` begins from stage 0; `stop(reason)` halts with safe shutdown
- `tick()` called from main loop -- reads sensors, controls heater/vents, checks advance
- `advance()` public method for manual stage advancement via REST API `/run/advance`; reads last sensor data and delegates to `_advance_stage()`; raises RuntimeError if no run active or on last stage
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

**Pi4 `kiln_server` package** (implemented, running on local Pi):
- `lora_receiver.py` -- SX1278 SPI driver + receive thread; polls DIO0 at 20ms, parses telemetry JSON / heartbeat / `ALERT;...` strings
- `database.py` -- SQLite schema (telemetry/alerts/runs); WAL mode, per-thread connections, write-lock serialised inserts; columnar `/history` query with field whitelist
- `api.py` -- Flask app: `/health`, `/status`, `/history`, `/alerts`, `/runs`
- `notifier.py` -- ntfy.sh POST with 30-min per-code suppression; lifecycle codes (`run_started`, `run_complete`, `equalizing_start`, `conditioning_start`) bypass suppression
- `config.py` -- only file that differs between bench Pi4 and cottage Pi4
- `schema.sql` -- applied on first run (CREATE IF NOT EXISTS)
- `__main__.py` -- wires everything, handles SIGTERM
- `kiln-server.service` -- systemd unit
- `requirements.txt` -- flask, requests, spidev, RPi.GPIO

Run lifecycle is inferred: first telemetry with a `stage` opens a run; `run_complete` alert closes it. Pico's wire format (`stage_idx`, no `type` field on telemetry, optional `faults` list) is normalised to schema columns on insert.

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
- Control loop: runs every 10s; calls `schedule.tick()` + `_update_status_cache()` + fault aggregator each iteration. LoRa telemetry rate-limited to 30s minimum
- Fault aggregator: `_collect_module_faults()` polls `check_health()` on all 12 modules every tick; `ALERT_CODE_TIERS` dict maps each code to fault/notice/info; `/status` returns `active_alerts` (flat codes) + `fault_details` (list of `{code, source, message, tier}` dicts); `/alerts` injects active faults from status cache alongside SD event log entries
- Display loop: `display.tick()` every 100ms with 4 registered pages (status, sensors, moisture, system)
- LoRa heartbeat: sends keepalive telemetry every 5 min when no run is active
- RPM reader: caches exhaust fan RPM every 10s; feeds `exhaust.update_rpm()` for mid-run stall detection
- System test suite: 18 tests (unit, integration, commissioning) run as async task via POST /test/run
- Calibration loading from SD card `calibration.json` at boot
- Fatal exception handler: safe shutdown (heater off, vents open, fans off) then reboot after 5s
- Authentication via `X-Kiln-Key` header on all endpoints except /health and /version
- WiFi AP security uses integer constant 4 (WPA2-PSK); `network.WPA2` not available on Pico 2 W

## config.py

Template configuration file with defaults. Must be edited before first deployment.

- VERSION, AP_SSID, AP_PASSWORD, API_KEY
- LORA_SF, LORA_FREQ_MHZ
- DEFAULT_SCHEDULE, DISPLAY_TIMEOUT_S, LOG_FLUSH_INTERVAL_S

---

## test_modules.py

Standalone module test runner. Imports each `lib/` module and calls its `test()`
function in sequence, then reports a summary of pass/fail results.

- Usage: `mpremote run test_modules.py`
- Does not start WiFi, HTTP server, or control loop
- Tests all 12 modules: sdcard, SHT31sensors, current, circulation, exhaust, vents, heater, moisture, display, lora, logger, schedule
- Exits with code 0 if all pass, 1 if any fail

---

## KivyApp/

Kivy mobile/desktop app -- primary human interface to the kiln. Lives in
`KivyApp/`. Spec: `Specs/kivy_app_spec.md`. Built incrementally with explicit
user testing and approval after every phase. Plan file:
`C:\Users\Steve\.claude\plans\flickering-swinging-balloon.md`.

### Phase status

| Phase | Description | Status |
|---|---|---|
| 0 | Environment + hello world (venv, requirements.txt, minimal Kivy App) | Approved |
| 1 | App skeleton with bottom navigation (5 placeholder tabs) | Approved |
| 2 | Settings + persistent storage + API client + auto-detect connection | Approved |
| 3 | Dashboard MVP (read-only from Pico /status) | Approved |
| 4 | Dashboard banners + AP-mode action buttons (start/stop/advance) | Approved |
| 5 | Alerts screen | Approved |
| 6 | Runs screen + run detail view + delete | Approved |
| 7 | History graphs (5 plot tabs) | Approved |
| 8 | Start Run flow (AP only) | Approved |
| 9 | Schedules viewer + editor (AP only) | Approved |
| 10 | System Test screen (AP only) | Approved |
| 11 | Logs screen - view/download remaining (delete moved to Phase 6) | Approved |
| 12 | Moisture Calibration (AP only) | Approved |
| 13 | Module Upload (AP only) | Approved |
| 14 | Pi4 Cottage mode end-to-end | Awaiting approval |
| 15 | Android packaging via buildozer | Not started |

### Conventions
- Standard CPython 3 (NOT MicroPython). Free use of `requests`, `pathlib`, etc.
- Venv at `KivyApp/.venv/` (gitignored). Dependencies pinned in
  `KivyApp/requirements.txt`.
- All HTTP work runs off the Kivy main thread.
- AP-only screens hide or visibly disable in Cottage mode.
- See "Kivy app development practices" in `CLAUDE.md` for the full ruleset.

### Phase 7 implementation notes
- Chart library: `matplotlib==3.10.8` embedded via
  `kivy_garden.matplotlib==0.1.1.dev0` (`FigureCanvasKivyAgg`). User
  confirmed this choice over `kivy_garden.graph` before install.
- `/history` returns columnar JSON; `screens/history.py:_unpack_columnar`
  converts to `{field: [values]}` once at load time; time-range changes
  filter the cached arrays without refetching.
- Plot tab content reflects the actual `DATA_COLUMNS` in
  `lib/logger.py`. Target lines (target_temp/target_rh/target_mc) are
  not logged today; they remain TODOs for the Pi4 daemon which can
  derive them from the schedule snapshot.
- Stage column is stored as an int by current firmware (stage index);
  `_encode_stages` still accepts string labels from older CSVs and
  assigns stable indices in first-seen order.
- Timestamps in CSVs have two forms: `YYYY-MM-DD HH:MM:SS` (RTC set)
  and `+NNNNs` (elapsed seconds from boot, RTC not set). Both are
  parsed to datetime; x-axis is rendered as elapsed-from-first-sample
  so the two forms are indistinguishable on the chart. The
  `_xs_numeric` fallback uses row indices when timestamps are all
  None (defence against pre-fix CSVs with empty ts columns).
- Auto-refresh: 30s on the History screen when the selected run is
  the one /status reports as active. Cancelled on_leave to avoid
  background polling.
- Android buildozer recipe patch for matplotlib + kivy_garden.matplotlib
  is deferred to Phase 15.

### Phase 8 implementation notes
- `screens/start_run.py` is a single Screen containing a scrollable
  three-section layout (schedule picker / run label / checklist) with
  a Start Run footer button. Not a wizard with Next/Back buttons -
  scrolling through the three sections is simpler on a phone and keeps
  the user's prior step answers visible while they pick later ones.
- Registered in `app.py` alongside the five nav tabs but deliberately
  not added to `BottomNav`. `BottomNav.select("start_run")` is a no-op
  (lookup miss), so the Dashboard tab stays highlighted while the
  wizard is up - which also signals "you came from here" to the user.
- Dashboard's `_on_start_pressed` now calls `on_navigate("start_run")`
  instead of showing the Phase 8 placeholder dialog.
- Species + thickness shortcut buttons map to built-in filenames via
  `SHORTCUT_FILENAMES`. Non-matching combos (Other / Custom) leave the
  selection blank and ask the user to pick manually from the spinner.
- The spinner shows every schedule on the Pico (`GET /schedules`) as
  `"<name> (<filename>)"` so user-created schedules are reachable.
  Filename is recovered by splitting on the trailing `" ("`.
- Duration range on the preview panel is derived from summed
  `min_duration_h` / `max_duration_h` across stages; open-ended stages
  (null `max_duration_h`) render as `"N+ h"`.
- `POST /run/start` now carries an optional `label` field. Current
  Pico firmware ignores it (`data.get("schedule", default)` is the
  only field consumed), but the wire format is future-proofed for a
  Pi4 daemon / firmware that will record operator-supplied run
  labels alongside the run record.
- Connection-mode listener on the wizard returns the user to the
  Dashboard if the app drops out of AP/STA mode while the screen is
  up (Cottage mode cannot start runs).
- Checklist state resets on `on_pre_enter`; a user returning to the
  wizard after a cancel starts fresh.

### Phase 9 implementation notes
- Two new screens under `KivyApp/kilnapp/screens/`:
  `schedules.py` (list) and `schedule_editor.py` (viewer / editor).
  Both AP/STA only; no Cottage-mode entry points.
- Entry point: a new "Tools (Direct only)" section on the Settings
  screen with a Schedules button. The spec allows "accessed from the
  Dashboard or Settings" for AP-only screens; Settings keeps the
  Dashboard uncluttered. The button is greyed out in Cottage/Offline
  via a connection listener.
- `app.py` registers both screens alongside the existing five nav tabs.
  Neither is in `BottomNav`. `_navigate_to()` was updated to only call
  `bottom_nav.select()` for the five real tabs, so the user's original
  tab stays highlighted while the editor is up (same pattern as
  Start Run in Phase 8).
- Schedule list rows (`_ScheduleRow`) show name + species/thickness +
  stage count + size + a BUILT-IN badge for factory schedules. Four
  per-row actions: View, Duplicate, Edit, Delete. Edit/Delete are
  disabled on built-ins; the Pico rejects both server-side (403), so
  disabling client-side is belt-and-braces. The user-facing
  disambiguation matches the spec: "Duplicate to edit".
- Four editor modes: `view`, `edit`, `duplicate`, `new`. `load()` is
  called by the Schedules screen via a direct method call on the
  editor instance (fetched from the ScreenManager) before switching
  screens, because `ScreenManager.current = ...` doesn't carry
  parameters the way `_navigate_to` does for the bottom-nav tabs.
- Filename handling: auto-derived from schedule name via `_slugify()`
  in new/duplicate mode (reactive to name edits until the user
  manually overrides the field). Locked in edit mode to prevent
  accidental renames creating orphan files on the SD card. `.json`
  suffix is appended automatically on save if missing.
- Thickness is a spinner with `0.5 / 1 / custom`. A custom numeric
  field appears only when `custom` is selected; it collapses to
  `height=0` otherwise so it doesn't occupy space.
- **Deviation from spec and plan**: the stage table uses a plain
  `BoxLayout` inside a `ScrollView`, not a `RecycleView`. Rationale:
  Kivy's RecycleView recycles row widgets by design, which mangles
  per-row `TextInput` focus and loses in-progress edits on scroll
  (well-documented Kivy pitfall). Typical schedules have <12 stages,
  so performance is not a concern. Each `_StageRow` holds direct
  references to its own TextInputs/Spinners and implements
  `collect()` for save-time validation.
- Client-side validation in `_StageRow.collect()` + `_collect()`
  mirrors the server-side checks in `main.py handle_schedule_put` so
  the user sees inline errors rather than a 400 from the server:
  drying stages require numeric target_mc_pct, equalizing/
  conditioning forbid it, temps clamp to 30-80 C, RH to 20-95 %,
  min/max duration numeric with max >= min (or blank for
  unlimited). Save still handles 400 gracefully if the Pico later
  adds stricter rules.
- `_StageRow._on_type_change()` enforces the MC% rule live: switching
  a row from drying to equalizing/conditioning clears + disables the
  MC field; switching back re-enables it. Also applied on initial
  load so an incoming equalizing stage has MC correctly greyed out.
- `_duplicate_filename()` handles the "Duplicate twice in a row" case
  by appending a digit (`maple_05in_copy` -> `maple_05in_copy2`).
  Schedule name gets a "(copy)" suffix too so the user sees which
  one they're editing.
- `schedule_put` + `schedule_delete` added to
  `KivyApp/kilnapp/api/client.py`. Both use `_request()` directly
  (PUT / DELETE aren't wrapped by the `_post` helper).

### Phase 10 implementation notes
- New screen `KivyApp/kilnapp/screens/system_test.py`. AP/STA only;
  the run button is greyed out in Cottage/Offline via a connection
  listener (same pattern as Schedules).
- Entry point: a "System Test" button in the existing
  "Tools (Direct only)" section of the Settings screen. The spec
  allows access from Dashboard or Settings; Settings keeps the
  Dashboard uncluttered.
- `app.py` registers the screen; `_navigate_to()` treats it like
  Start Run and Schedules (not a bottom-nav tab, no tab highlight
  change), so the user's original tab stays highlighted while the
  test screen is up.
- Threading: `call_async` for both `POST /test/run` and the 1 s
  `GET /test/status` polls. Poll errors are shown in the status
  label but don't kill the poll loop - Pico is frequently slow to
  respond mid-test (heater commissioning step is 2+ min of CPU
  attention) and a single timeout should not collapse the UI.
- Rows are keyed by test id and upserted each tick rather than
  rebuilt, so Kivy doesn't churn widgets and the user's scroll
  position is preserved while RUN/PASS/FAIL badges update in place.
  Tests are grouped by their `group` field (Unit Tests / Integration
  Tests / Commissioning) - section headers appear in discovery order
  as new groups are seen.
- Progress bar is a real `ProgressBar` filled by
  (done / total) where done = pass+fail+skip. Elapsed time is
  client-side (server's `elapsed_s` field is currently always 0).
- On completion: a summary Panel is inserted above the test list
  showing overall PASS/FAIL plus counts, with "Save results" and
  "Copy to clipboard" buttons. Save writes a timestamped .txt to
  the user's `~/Downloads` folder (falls back to `user_data_dir` if
  Downloads is not writable); Android will hook into the SAF in
  Phase 15. Copy uses `kivy.core.clipboard.Clipboard`.
- Leaving the screen stops local polling but does NOT abort the
  test on the Pico - there is no `/test/cancel` endpoint. Coming
  back to the screen with a test still running shows a stale view
  until the user re-triggers Run, since the Pico's idempotent
  `/test/status` is only polled while we have a local start
  timestamp. Good-enough for now; future work could auto-resume
  polling if `complete=False` on entry.
- Pre-run confirmation dialog mentions heater + fans activation
  and the 3-5 minute duration. No client-side check prevents
  running the test while a drying run is active; the Pico returns
  409 and the error is surfaced via the status label.

### Phase 11 implementation notes
- New screen `KivyApp/kilnapp/screens/logs.py`. AP/STA only;
  the Settings entry-point button (`Logs`) is greyed out in
  Cottage/Offline via the existing `_apply_tools_gate()` pattern.
- `app.py` registers the screen as `"logs"`. Not a bottom-nav tab,
  so the original tab stays highlighted while the screen is up
  (same pattern as Schedules, System Test, Start Run).
- Storage indicator (`_StorageBar`) reads `GET /sdcard/info` and
  shows used / total + free + file count plus a `ProgressBar`. A
  warning line is rendered above 80% full (per spec).
- Log set list is populated from `GET /runs` (the same endpoint
  Runs uses). Each row shows the same primary label, the rid,
  event-count / data-row / total-size summary, and three buttons:
  View, Download events, Download CSV. Delete intentionally
  omitted - Phase 6's Runs detail view already has it.
- `GET /status` is fetched alongside `/runs` purely so the active
  run can be flagged with `(ACTIVE)` and pulled to the top of the
  list (mirrors Runs' handling of the RTC-unset / mtime-near-zero
  case for the live run).
- In-app event log viewer (`_EventLogViewer`) is a sub-widget that
  swaps out the list area when the user taps View. Toolbar:
  Back / level spinner (ALL / INFO / WARN / ERROR) / search field
  / line counter. Body is a monospace `Label` inside a horizontally
  + vertically scrollable `ScrollView`; the label width tracks
  `texture_size[0]` so long lines aren't wrapped. Filtering and
  search re-render in-memory without re-fetching.
- `_line_level()` parses the level token from the standard logger
  format (`"YYYY-MM-DD HH:MM:SS [LEVEL] [source] msg"`). Unknown
  tokens map to `OTHER` so an `ALL` view still shows them; legacy
  `WARNING` lines are normalised to `WARN` so the filter catches
  both spellings (matches the firmware's WARN-vs-WARNING fix
  already shipped).
- Event log download uses the same `GET /logs/{rid}/events` payload
  that the viewer consumes, joined with newlines and saved as
  `event_<rid>.txt`. Data CSV download fetches `GET /history?run=
  <rid>` (already columnar) and reconstructs the on-SD CSV format
  via `_rows_to_csv()` (`,`-joined, `\n` rows, empty string for
  None, 2-decimal floats matching `logger.data()`). Saved as
  `data_<rid>.csv`.
- Desktop save target is `~/Downloads`, falling back to
  `App.user_data_dir` if Downloads is not writable - mirrors the
  Phase 10 System Test report-save behaviour. Android SAF hookup
  is deferred to Phase 15 per the plan.
- Two new client methods in `api/client.py`: `sdcard_info()` and
  `logs_events(run_id)` (with a 60s timeout - long runs produce
  thousands of event lines and the Pico reads them off SPI before
  serialising).

### Phase 12 implementation notes
- New screen `KivyApp/kilnapp/screens/calibration.py`. AP/STA only;
  the Settings "Moisture Calibration" tool button is greyed out in
  Cottage/Offline via `_apply_tools_gate()` (same pattern as the
  Schedules / System Test / Logs buttons).
- `app.py` registers the screen as `"calibration"`. Reached from
  Settings; not a bottom-nav tab.
- Three new client methods in `api/client.py`: `moisture_live()`,
  `calibration_get()`, `calibration_post(ch1, ch2)`.
- Offset math: firmware stores `corrected_mc = raw_mc + offset` and
  `/moisture/live` returns the *corrected* value (current offset
  already baked in). Client recovers raw via
  `raw_mc = corrected_mc - current_offset`, then computes
  `new_offset = reference_mc - raw_mc` on Apply. Doc-comment at the
  top of `calibration.py` spells this out so future edits don't drop
  the `current_offset` term by accident.
- Single "Take reading" button triggers one `GET /moisture/live` call
  and populates both channels - there's no reason to split it because
  the endpoint returns both channels in the same payload.
- Per-channel `_ChannelPanel` shows resistance (Ohm / kOhm / MOhm),
  corrected MC%, raw MC% (computed client-side), current offset,
  and a temp-correction indicator. A reference MC% input + Apply
  button computes the proposed offset; Apply only mutates local
  state - nothing hits the Pico until Save.
- Proposed offsets are clamped to +/-50 MC% on Apply so a typo
  doesn't save a wild value. (Typical offsets are well under 5 MC%.)
- Save shows a confirmation dialog summarising both channels and
  then POSTs `{channel_1_offset, channel_2_offset}`. It sends BOTH
  channels every time (not just the one the user changed): the
  firmware write is atomic anyway and this avoids drift if one
  channel was edited months ago and loaded from SD this session.
- Reset to defaults uses the same POST path with `{0.0, 0.0}` after
  a danger-styled confirmation dialog. The firmware writes a fresh
  calibration.json with `calibrated_at` set to now, so the
  "Current calibration" timestamp updates on the next refresh.
- "Current calibration" panel shows both offsets + last-calibrated
  timestamp + a source note. Falls back to "RTC was not set" when
  the Pico returned `calibrated_at=0` (happens if the Pico clock
  was never synced before the calibration write).
- After a successful Save or Reset, the screen re-fetches
  `/calibration` + `/moisture/live` so the panel values reflect the
  Pico's actual state rather than whatever the client proposed.

### Phase 14 implementation notes
- Pi4 `kiln_server/api.py` now synthesises the field shape the Kivy
  Dashboard (Pico-targeted) expects from a telemetry row plus the
  runs table. New synthesised fields on `/status`:
  `run_active`, `active_run_id`, `cooldown` (always False),
  `schedule_name` (from runs.schedule_name), `stage_index`,
  `stage_name` ("Stage N" fallback - the Pi4 has no schedule data
  to look up real names), `total_elapsed_h`, `active_alerts`
  (alias of `faults`), `fault_details` (synthesised
  `{code, source: "lora", message, tier}` per fault using the same
  ALERT_CODE_TIERS table the firmware ships with),
  `last_packet_age_s`, `last_packet_ts`, plus explicit `null` for
  `stage_elapsed_h` / `stage_min_h` / `target_*` / `mc_resistance_*`
  so the Dashboard's `data.get(...)` paths take the same branches
  in both modes. `run_active` requires both an open run AND a
  fresh telemetry row (within 90s) - this prevents stale telemetry
  from a long-disconnected Pico from looking "active".
- `/alerts` rows now carry `tier` (fault/notice/info), `level`
  (ERROR/WARN/INFO derived from tier), and `source` ("lora").
  `/alerts?level=...` filters server-side post-decoration.
- `/runs` rows now expose `started_at_str` / `ended_at_str`
  (formatted via `datetime`), `data_rows` (alias for
  telemetry_count), `event_count` (alias for alert_count), and
  `size_bytes: 0` (SQLite per-run size isn't a useful number).
  An `active` flag is also added so the Kivy run list can show the
  ACTIVE badge without re-querying /status.
- `/health` now includes `ntfy_topic` and `ntfy_url` so the Kivy
  Settings Daemon-info section can display them. Added `notifier`
  parameter to `create_app()` and wired it through `__main__.py`.
- Both `/alerts` and `/history` accept `run` as a synonym for
  `run_id`; the Kivy app uses the Pico-style `run` query string.
- Kivy `screens/dashboard.py` gained a `LoraLinkPanel`
  (Cottage-mode only) with a 5-bar RSSI indicator, raw RSSI dBm,
  SNR dB, and last-packet age. Bars step from green -> amber ->
  red as signal degrades (cutoffs: -60/-75/-90/-105 dBm). The
  panel attaches/detaches with the connection mode rather than
  staying empty in AP/STA mode.
- Kivy `screens/settings.py` gained a "Daemon info (Cottage)"
  section populated from `GET /health` (environment, uptime,
  packets received, last-packet age, ntfy.sh topic). Out-of-mode
  the labels show a hint to connect via Cottage; the section
  stays attached because Kivy's BoxLayout height-binding fights
  with manual `height=0` writes (any child layout pass restores
  it from minimum_height). `on_pre_enter` re-fetches /health on
  every visit while in Cottage mode.
- Kivy `screens/history.py` `_unpack_columnar()` now aliases
  short-vs-long column names (`mc_ch1` <-> `mc_channel_1`,
  `exhaust_pct` <-> `exhaust_fan_pct`, `circ_pct` <->
  `circ_fan_pct`). It also projects the Pi4's single
  `vent_open` boolean into per-servo `vent_intake` /
  `vent_exhaust` 0/100 columns so the existing Humidity vent
  shading and Diagnostics vent traces still render in Cottage
  mode.
- AP-only gating audit: every AP-only screen
  (`start_run`, `schedules`, `schedule_editor`, `system_test`,
  `module_upload`, `logs`, `calibration`) already gates the
  destructive operation on `MODE_DIRECT or MODE_STA` and the
  Settings Tools-section buttons disable in Cottage via
  `_apply_tools_gate()`. The Dashboard's Start/Stop/Advance
  /Shutdown buttons hide via `_is_direct_mode()`. No additional
  gating work was required.

### Phase 13 implementation notes
- New screen `KivyApp/kilnapp/screens/module_upload.py`. AP/STA only;
  the Settings "Module Upload" tool button is greyed out in
  Cottage/Offline via `_apply_tools_gate()` (same pattern as the
  other AP-only tools).
- `app.py` registers the screen as `"module_upload"`. Reached from
  Settings; not a bottom-nav tab.
- Two new client methods in `api/client.py`: `modules_list()` and
  `module_upload(mod_path, body)`. The latter sends raw bytes via a
  new `data=` / `content_type=` pass-through on `_request()` so the
  Pico's `handle_module_upload` (which writes `body` directly to
  flash) gets exactly what was on disk - no JSON wrapping.
- Two upload destinations are supported, routed by file extension
  on the target path:
    - .py    -> `PUT /modules/{path}` (firmware accepts main.py
                or lib/*.py only; reboots on success)
    - .json  -> `PUT /schedules/{filename}` (Pico runs the same
                full schedule-schema validation as the editor; no
                reboot)
  The screen rejects target paths the firmware would 400 on so the
  user sees the error inline rather than as a server response.
- File picker is the built-in `kivy.uix.filechooser.FileChooserListView`
  inside a Popup, filtered to `*.py` and `*.json`. On phone the
  Phase 15 buildozer pass will swap this for plyer/filechooser; on
  desktop the Kivy widget is reliable.
- Warning banner is always visible (red title + 3-line body) per the
  spec. A second `main.py replaces the entry point` warning shows
  inline only when the target path is exactly `main.py` and is
  driven by a live bind on the target text input - it appears as
  soon as the user picks main.py or types the path manually.
- 512KB cap is enforced client-side too (`_MAX_BYTES`) so a large
  file is rejected before any bytes hit the wire.
- Confirm dialog summarises the destructive action: main.py shows
  the danger-styled variant; lib/*.py uploads call out the reboot;
  schedule uploads note "no reboot".
- Reconnect watch (`_start_reconnect_watch`) polls `/health` every
  3s for up to 30s after a .py upload and reports when the Pico is
  back. On success it nudges `connection.detect()` so the indicator
  and other screens see the freshly rebooted endpoint without
  waiting for the next autodetect cycle. JSON uploads skip the
  watch.
- "Installed modules" panel calls `GET /modules` on entry, on a
  Refresh button, and after every successful schedule save (module
  uploads refresh later via the reconnect watch).
- Progress bar implementation is intentionally minimal: `requests`
  doesn't expose per-byte upload progress without a streaming
  generator, and modules are tens of KB so a busy spinner / status
  line is more honest than a fake bar. The bar jumps to 0.1 at
  start and 1.0 on completion. Documented in the file's docstring.

### Phase 7-adjacent fixes shipped with the phase
Several cross-cutting issues surfaced during Phase 7 testing and got
fixed in the same batch (rather than waiting for their nominal phase):

- **CSV data logging schema mismatch (FIRMWARE).** `lib/schedule.py`
  `_log_data()` record keys didn't match `lib/logger.py` `DATA_COLUMNS`
  (wrote `mc_maple`/`mc_beech`/`heater`/`vents`, not `mc_ch1`/`mc_ch2`/
  `heater_on`/`vent_intake`/`vent_exhaust`). Every pre-fix CSV has
  empty MC/heater/vent/circ/ts columns. Fixed by aligning keys; also
  `logger.data()` now auto-populates `ts` via `_timestamp()` when the
  caller doesn't. Vents (single is_open bool) map to 0/100 for both
  vent_intake/vent_exhaust since hardware moves them as a pair.
- **Active run id tracking (FIRMWARE + KIVY).** `lib/logger.py` now
  stores `_run_id` on `begin_run()` and exposes it via a `run_id`
  property. `main.py` `/status` returns `active_run_id`. The Kivy
  Runs and History screens trust this field to mark the ACTIVE badge
  and auto-select the active run in History. Both screens also pull
  the active run to the top of their list/dropdown regardless of
  server sort order - required because a run started before RTC sync
  lands gets a tiny mtime (near epoch-2000) and otherwise drops to
  the bottom of the mtime-desc sort.
- **/runs endpoint enriched (FIRMWARE).** Now returns `mtime` (int,
  epoch-2000 seconds) and `ended_at_str` (formatted `YYYY-MM-DD HH:MM`,
  empty when RTC wasn't set), and sorts by mtime desc. Kivy displays
  `ended_at_str` as the primary run label with rid/started as
  secondary context.
- **RTC auto-sync (KIVY).** `KivyApp/kilnapp/connection.py`
  `_maybe_sync_rtc()` POSTs unix time to `/time` on every successful
  Pico detect (AP or STA), rate-limited to once per 6 h. The
  `auto_sync_rtc=True` setting was present in storage but no code
  actually pushed time before.
- **Connection override: Force Pico STA (KIVY).** Added
  `OVERRIDE_STA` option. Auto mode's spec-ordered probe list is Pico
  AP -> Pi4 -> Pico STA, so as soon as a Pi4 daemon is up on the same
  LAN, Auto always lands on Pi4 and the user can't reach the Pico
  STA. Force STA bypasses that. Also renamed the existing "Force
  Direct" to "Force Pico AP" for clarity.
- **Bottom nav multi-highlight (KIVY).** `BottomNav.select()` now
  manually clears other tabs to `"normal"` before programmatic
  switches. Kivy's ToggleButtonBehavior group de-selection only runs
  on touch press, not on direct `state` writes, so nav jumps from
  screen-to-screen (e.g. `_navigate_to`) were stacking highlights.
- **View Alerts/History from Runs detail (KIVY).** `run_id` now
  plumbs through the nav callback into both Alerts and History
  screens' `preselect_run(run_id)` APIs so jumping from a run card
  pre-filters/pre-selects correctly instead of showing "current run".
- **Pi4 integer run ids (KIVY).** All run-label formatters now
  `str()`-coerce the run id so Kivy Label / Spinner widgets don't
  throw on Pi4's SQLite integer primary keys.

---

## What still needs building

In rough priority order:

1. **`kiln_server/` Pi4 daemon** -- IMPLEMENTED and running on a local
   Pi. Phase 14 added Pico-shape augmentation of `/status`, `/alerts`,
   `/runs`, and `/health` so the Kivy Cottage mode reads the same
   field set it does in Direct mode. Outstanding:
   - End-to-end exercise from the Kivy app against the running Pi
     (Phase 14 user test)
   - ntfy.sh push smoke test from a real fault (any LORA_TIMEOUT or
     equivalent code can confirm the path)
2. **Kivy app** -- in progress, see "KivyApp/" section above

## Known firmware bugs

- **LoRa telemetry packet length (FIXED in main.py).** `_send_lora_telemetry`
  was building JSON via MicroPython's `json.dumps`, which serialises floats
  at full IEEE754 precision (21.1 -> 21.100000381469727) and adds whitespace
  after every separator. With 6 float fields the packet hit 302 bytes vs.
  the SX1278 255-byte FIFO. Fix: hand-built compact JSON with floats
  rounded to 1 dp, stage sent as integer index instead of name, field
  names aligned to /status (rh_*, not humidity_*). Typical packet now
  ~225 bytes. Wire format change: when the Pi4 daemon spec is written it
  must consume `stage_idx` (int) instead of `stage` (string) and
  `rh_lumber` / `rh_intake` instead of `humidity_lumber` /
  `humidity_intake`.
- **CSV data logging: record keys didn't match DATA_COLUMNS (FIXED).**
  `lib/schedule.py` `_log_data()` was writing a record with keys
  `{mc_maple, mc_beech, heater, vents, ...}` that didn't match
  `lib/logger.py` `DATA_COLUMNS` (`{mc_ch1, mc_ch2, heater_on,
  vent_intake, vent_exhaust, ...}`). Also `ts` was never written.
  Consequence: every pre-fix CSV has empty `ts`, `mc_ch1`, `mc_ch2`,
  `circ_pct`, `vent_intake`, `vent_exhaust`, `heater_on` columns. Fixed:
  (a) schedule.py record keys now match DATA_COLUMNS exactly,
  (b) logger.data() auto-populates `ts` via `_timestamp()` when the
  caller didn't set it, (c) vents (single is_open bool) map to 0/100
  for both vent_intake/vent_exhaust since the hardware moves them as a
  pair. Existing CSVs cannot be retroactively fixed; runs started after
  this firmware deploy will plot correctly in the Kivy History screen.
  The Kivy `_xs_numeric` row-index fallback remains as defence for
  any remaining pre-fix CSVs.
- **`lora.send_alert()` float-precision risk (FIXED).** `send_alert()`
  was wrapping `{ts, code, message}` through `json.dumps` which has the
  same float-precision / whitespace issue that bit telemetry, AND the
  JSON envelope was routed through the Pi4's telemetry parser (not
  alert parser), so `notifier.send()` never fired even when called.
  Fixed by emitting the `ALERT;<code>;<message>` wire format directly
  (no JSON), matching what `schedule._send_alert()` already sends via
  `lora.send()`. If `message` already starts with `ALERT;` the method
  passes it through verbatim so pre-formatted wire strings still work.
- **`/runs` is slow (>5s with many historical runs) (FIXED).**
  `handle_runs` used to open every `data_*.csv` and `event_*.txt` on
  the SD card and read each one line-by-line to compute `data_rows`
  and `event_count`, which over SPI with ~10+ runs exceeded the
  default 5s HTTP timeout. Fixed by caching counts per run: `Logger`
  now tracks `event_count` / `data_rows` live during a run and writes
  a tiny `stats_<rid>.json` on `end_run()`. `handle_runs` reads the
  stats file (cheap) instead of scanning lines; the active run is
  served from the logger's live counters; legacy runs without a
  stats file show 0 (acceptable - Pi4 daemon will have exact counts
  via SQLite).
- **`/alerts` WARN vs WARNING inconsistency (FIXED).** Standardised on
  `WARN` everywhere: filter value, all `logger.event(..., level=...)`
  callers in `lib/` and `main.py`, and the Kivy filter map. Log-line
  inclusion check (`"[WARN" not in line`) is a prefix match, so legacy
  `[WARNING]` lines already on the SD card still appear in /alerts
  responses; only new lines will consistently read `[WARN ]`.
- **Schedule alert/fault mixing (FIXED).** `_last_alert_ts` used to mix
  lifecycle codes with real fault codes; the Kivy app classified them
  client-side. Fixed by `error_checking_spec.md` implementation: every
  module now exposes a fault contract, `main.py` aggregates all faults
  via `_collect_module_faults()`, and `/status` returns both
  `active_alerts` (flat code list) and `fault_details` (list of
  `{code, source, message, tier}` dicts). The three-tier model
  (`fault`/`notice`/`info`) is tagged at source. The Kivy app reads
  `tier` from the server when present and falls back to its local
  `FAULT_CODES`/`NOTICE_CODES` tables for older firmware.
- **Faults do not trigger ntfy.sh pushes (FIXED).** Fixed on the
  firmware side via `_emit_new_fault_alerts()` in `main.py`: each
  control-loop tick (after `_collect_module_faults`) diffs the
  current fault/notice code set against the previous tick's set and
  emits `ALERT;<code>;stage=<n>;temp=<t>;rh=<r>;source=<src>` via
  `lora.send_alert()` for any newly-active code. Per-code 30-min
  suppression (`FAULT_ALERT_SUPPRESS_MS`) mirrors
  `schedule._send_alert()` so a flapping fault does not spam the
  phone. The telemetry `faults` array remains unchanged so the Kivy
  Dashboard still sees the live fault set regardless of alert
  delivery. Daemon-side diffing was rejected: it would lose state on
  daemon restart (causing spurious "new" bursts) and delay
  notifications by up to one telemetry interval (~30s).