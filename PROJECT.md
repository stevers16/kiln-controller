# Software Project Summary and Status

This document contains a summary of the software components built for this project,
and the key technical decisions made along the way.

It is maintained by Claude Code, and provided to Claude.ai to give context on the
state of the software side of this project.

---

## Overall status

Early firmware stage. Four modules exist in `lib/`. No `main.py` yet. The logging
stack is complete and fully tested on hardware.

---

## Modules — status summary

| File | Status | Tested on hardware |
|---|---|---|
| `lib/circulation.py` | Complete | Yes — all tests pass |
| `lib/exhaust.py` | Complete | Yes — all tests pass |
| `lib/sdcard.py` | Complete | Yes — all tests pass |
| `lib/logger.py` | Complete | Yes — all tests pass |
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
- All unit tests pass on hardware

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

- SPI0 at 400 kHz init speed (GP2=SCK, GP3=MOSI, GP4=MISO, GP5=CS)
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

---

## lib/logger.py

Single logging service for the entire kiln firmware. Written to spec; not yet
tested on hardware (blocked by SD card issue above).

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

---

## What still needs building

In rough priority order:

1. **SHT31 sensors** (`lib/sensors.py`) — I²C on GP0/GP1, two addresses,
   temp + RH readings
2. **Vent servos** (`lib/vents.py`) — MG90S on GP14 (intake) and GP15 (exhaust),
   PWM position control
3. **Heater** (`lib/heater.py`) — SSR on GP18, simple digital on/off with safety
   interlock logic
4. **Moisture probes** (`lib/moisture.py`) — ADC on GP26/GP27, AC excitation on
   GP12/GP13
5. **Display** (`lib/display.py`) — UART1 on GP8/GP9
6. **Wi-Fi / REST API** — AP mode, HTTP server, time sync, mobile app interface
7. **Drying schedule controller** — multi-stage logic consuming sensor readings
8. **`main.py`** — entry point wiring all modules together
