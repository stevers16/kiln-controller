# Spec: Logging service — lib/sdcard.py + lib/logger.py

## Overview

Two files to write:

1. **`lib/sdcard.py`** — low-level SD card mount/unmount wrapper
2. **`lib/logger.py`** — logging service used by all other modules

Logger provides two logs per drying run:
- **Event log** (text) — human-readable timestamped entries from all components
- **Data log** (CSV) — periodic environment snapshots for the full run duration

Both files live on the SD card. Silent fail with REPL warning if SD is unavailable.

---

## lib/sdcard.py

### Purpose
Wraps the MicroPython `sdcard` driver and `uos.mount()`. Provides a clean
mount/unmount interface that `logger.py` depends on.

### Dependencies
MicroPython's built-in `sdcard` module (available as `import sdcard` on Pico).
SPI pins per GPIO map: SCK=GP2, MOSI=GP3, MISO=GP4, CS=GP5.

### Class: `SDCard`

```python
class SDCard:
    def __init__(self, sck=2, mosi=3, miso=4, cs=5, mount_point="/sd")
    def mount(self) -> bool          # Returns True on success, False on failure
    def unmount(self) -> None        # Safe unmount; no-op if not mounted
    def is_mounted(self) -> bool
    @property
    def mount_point(self) -> str     # e.g. "/sd"
```

**mount() behaviour:**
- Initialise SPI0 at 1MHz (conservative — can increase after testing)
- Instantiate `sdcard.SDCard(spi, cs_pin)`
- Call `uos.mount(sd, self._mount_point)`
- On any exception: print warning to REPL, return False
- On success: return True

**unmount() behaviour:**
- Call `uos.umount(self._mount_point)` inside try/except
- Silently ignore if not mounted

### Unit test
```
=== SDCard unit test ===
  PASS — mount() returns True
  PASS — is_mounted() True after mount
  PASS — /sd appears in uos.listdir('/')
  PASS — can write and read back a test file
  PASS — unmount() succeeds
  PASS — is_mounted() False after unmount
```

---

## lib/logger.py

### Purpose
Single logging service for the entire kiln firmware. Owns one `SDCard` instance.
Provides event logging (text) and data logging (CSV) for a drying run.

### Log file naming
Both files are created in the SD root when `begin_run()` is called:
- Event log: `event_YYYYMMDD_HHMM.txt`
- Data log:  `data_YYYYMMDD_HHMM.csv`

Timestamp comes from `time.localtime()`. If time has not been set (year < 2024),
fall back to `run_NNNNN` where NNNNN is `time.ticks_ms() // 1000`.

### Class: `Logger`

```python
class Logger:
    def __init__(self, sd: SDCard)
    def begin_run(self) -> bool      # Creates both files with headers; returns False if SD unavailable
    def end_run(self) -> None        # Flush and close both files
    def event(self, source: str, message: str, level: str = "INFO") -> None
    def data(self, record: dict) -> None
    @property
    def run_active(self) -> bool
```

### event() format

Appends one line to the event log:

```
2026-03-17 14:30:05 [INFO ] [exhaust    ] Fan on at 75%
2026-03-17 14:30:06 [WARN ] [sdcard     ] Write failed — retrying
2026-03-17 14:30:07 [ERROR] [heater     ] SSR did not respond
```

- Timestamp: `YYYY-MM-DD HH:MM:SS` from `time.localtime()`; fall back to
  `+NNNNNs` (elapsed seconds) if time not set
- Level: `INFO`, `WARN`, `ERROR` — padded to 5 chars
- Source: caller-supplied string, padded/truncated to 10 chars
- Always flushes after write (no buffering — kiln may lose power mid-run)
- Also prints to REPL (so it appears in Thonny console during development)
- Silent fail if SD unavailable: print to REPL only

### data() format

`record` is a dict with these keys (all optional — missing keys written as empty):

```python
{
    "ts":           str,    # timestamp string
    "temp_lumber":  float,  # °C
    "rh_lumber":    float,  # %
    "temp_intake":  float,  # °C
    "rh_intake":    float,  # %
    "mc_ch1":       float,  # wood moisture % channel 1
    "mc_ch2":       float,  # wood moisture % channel 2
    "exhaust_pct":  int,    # exhaust fan speed 0-100
    "circ_pct":     int,    # circulation fan speed 0-100
    "vent_intake":  int,    # intake vent position 0-100
    "vent_exhaust": int,    # exhaust vent position 0-100
    "heater_on":    bool,
    "stage":        str,    # current drying stage name
}
```

CSV header row written by `begin_run()`:
```
ts,temp_lumber,rh_lumber,temp_intake,rh_intake,mc_ch1,mc_ch2,exhaust_pct,circ_pct,vent_intake,vent_exhaust,heater_on,stage
```

Floats written to 2 decimal places. Bools as `1`/`0`. Always flushes after write.

### begin_run() behaviour
- Calls `sd.mount()` — if fails, sets internal flag, all subsequent calls are no-ops
- Creates event and data files
- Writes CSV header row to data log
- Writes first event: `[logger] Run started`
- Returns True on success, False on failure

### end_run() behaviour
- Writes final event: `[logger] Run ended`
- Flushes and closes both file handles
- Calls `sd.unmount()`
- Resets internal state so `begin_run()` can be called again

### Silent fail pattern
Every file write is wrapped in try/except. On exception:
```python
print(f"[logger] WARNING: SD write failed — {e}")
```
Then continue. Never raise. The kiln must keep running even if the SD card fails.

### Unit test
```
=== Logger unit test ===
  PASS — begin_run() returns True
  PASS — run_active is True
  PASS — event log file exists on SD
  PASS — data log file exists on SD
  PASS — event() writes correctly formatted line
  PASS — event() with WARN level
  PASS — data() writes CSV row with correct columns
  PASS — data() with partial record (missing keys write empty)
  PASS — end_run() closes cleanly
  PASS — run_active is False after end_run
  PASS — begin_run() can be called again (second run)
```

---

## Integration with existing modules

Once logger.py is complete, update `exhaust.py` and `circulation.py` to accept
`logger=None` in `__init__` and call `logger.event()` on significant state changes:

```python
# exhaust.py
def __init__(self, pwm_pin=PWM_PIN, tach_pin=TACH_PIN, gate_pin=GATE_PIN, logger=None):
    self._logger = logger
    ...

def on(self, speed_percent):
    ...
    if self._logger:
        self._logger.event("exhaust", f"Fan on at {speed_percent}%")
```

Suggested event messages per module:

| Module | Event |
|---|---|
| exhaust | `Fan on at {pct}%`, `Fan off` |
| circulation | `Fans on at {pct}%`, `Fans off` |
| heater (future) | `Heater on`, `Heater off` |
| servo/vent (future) | `Intake vent {pct}%`, `Exhaust vent {pct}%` |
| sensors (future) | `Sensor read failed` (errors only) |

---

## Also required: fix exhaust.py

The repo version of `exhaust.py` is outdated. Replace it with the correct version:

- `PWM_PIN = 16`, `GATE_PIN = 21`, `TACH_PIN = 22` as module-level constants
- `__init__(self, pwm_pin=PWM_PIN, tach_pin=TACH_PIN, gate_pin=GATE_PIN, logger=None)`
- `self._gate = machine.Pin(gate_pin, machine.Pin.OUT)` initialised low at boot
- `self._pwm` initialised with `duty_u16(0)`
- `on()`: drives gate high, then sets PWM duty
- `off()`: zeros PWM duty, then drives gate low
- `is_running` property
- `set_speed()` method (no-op if not running)
- Logger calls as described above
- Unit test updated to match

Do not modify `circulation.py` — it is correct and already follows the right pattern.
