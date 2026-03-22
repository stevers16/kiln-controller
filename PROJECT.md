# Software Project Summary and Status

This document contains a summary of the software components built for this project,
and the key technical decisions made along the way.

It is maintained by Claude Code, and provided to Claude.ai to give context on the
state of the software side of this project.

---

## Overall status

Early firmware stage. Seven modules exist in `lib/`. No `main.py` yet. The logging
stack is complete and fully tested on hardware. Vent servos are working. Current
monitoring (INA219) is implemented and basic hardware test confirmed passing.
SHT31 dual sensor module is implemented and tested on hardware.

---

## Modules — status summary

| File | Status | Tested on hardware |
|---|---|---|
| `lib/circulation.py` | Complete | Yes — all tests pass |
| `lib/exhaust.py` | Complete | Yes — all tests pass |
| `lib/sdcard.py` | Complete | Yes — all tests pass |
| `lib/logger.py` | Complete | Yes — all tests pass |
| `lib/vents.py` | Complete | Yes — all tests pass |
| `lib/current.py` | Complete | Basic test passing on hardware |
| `lib/SHT31sensors.py` | Complete | Yes -- all tests pass |
| `main.py` | Not written | — |

---

## lib/circulation.py

Controls 3× 120mm 4-pin PWM circulation fans wired as a group.

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

- PWM on GP16, MOSFET gate on GP21 (separate pins — gate is the hard on/off)
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

- SPI0 at 400 kHz init speed (GP2=SCK, GP3=MOSI, GP4=MISO, GP5=CS) — pin constants
  were briefly transposed (MISO/SCK swapped) causing a "bad SDK pin" error; corrected
- CS pin driven HIGH before SPI is initialised (prevents spurious transactions)
- Uses `uos.VfsFat(sd)` wrapper before `uos.mount()` (required in MicroPython 1.20+)
- Imports the raw driver as `sdcard_driver` (not `sdcard`) to avoid a naming
  conflict with this wrapper file — see deployment note below
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
- Silent fail on all SD writes — kiln keeps running if card fails mid-run

**Integration test:** `test_logging.py` at repo root exercises logger + circulation
fan events end-to-end. All tests pass on hardware.

---

## Logging spec — implementation decisions

These decisions were made during the LOGGING_SPEC.md work and differ from defaults
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

Controls 2× MG90S servos driving butterfly-style intake and exhaust dampers.

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
- I2C instance created internally (not shared) -- will need refactoring when main.py wires shared bus with INA219

---

## What still needs building

In rough priority order:

1. **Heater** (`lib/heater.py`) -- SSR on GP18, simple digital on/off with safety
   interlock logic
2. **Moisture probes** (`lib/moisture.py`) -- ADC on GP26/GP27, AC excitation on
   GP12/GP13
3. **Display** (`lib/display.py`) -- UART1 on GP8/GP9
4. **Wi-Fi / REST API** -- AP mode, HTTP server, time sync, mobile app interface
5. **Drying schedule controller** -- multi-stage logic consuming sensor readings
6. **`main.py`** -- entry point wiring all modules together
