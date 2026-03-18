# Solar Wood Drying Kiln — Claude Code Project Context

## Project overview

A solar-powered wood drying kiln (~30 cubic feet, 5ft × 2ft × 3ft) controlled by a
Raspberry Pi Pico 2 W running MicroPython. The kiln dries hardwood (maple, beech,
0.5–1 inch boards) using multi-stage schedules with progressive temperature and
humidity targets.

This repo contains the MicroPython firmware. Hardware design, component selection,
and wiring decisions are handled separately in Claude.ai (project: Solar Wood Kiln).
Claude Code is responsible for software only. When hardware questions arise, flag
them rather than making assumptions.

---

## Hardware summary

| Component | Detail |
|---|---|
| Controller | Raspberry Pi Pico 2 W (MicroPython) |
| Temp/RH sensors | 2× SHT31 on I²C (GP0/GP1) |
| Exhaust fan | Foxconn PVA080G12Q 80mm, PWM GP16, gate GP21, tach GP22 |
| Circulation fans | 3× Thermalright TL-C12C 120mm, PWM GP17, gate GP19 |
| Vent servos | 2× MG90S, GP14 (intake), GP15 (exhaust) |
| Backup heater | 500W PTC via Fotek SSR-25DA, GP18 |
| Moisture probes | 2× voltage divider ADC GP26/GP27, AC excitation GP12/GP13 |
| Display | JC035-HVGA-ST-02-V02 3.5" UART, UART1 GP8/GP9 |
| SD card | SPI micro SD module, GP2–GP5 |
| Wi-Fi | AP mode, HTTP/REST API for mobile app |

### GPIO map

| GPIO | Function |
|---|---|
| GP0 | I²C0 SDA — SHT31 ×2 |
| GP1 | I²C0 SCL — SHT31 ×2 |
| GP2 | SPI0 SCK — SD card |
| GP3 | SPI0 TX (MOSI) — SD card |
| GP4 | SPI0 RX (MISO) — SD card |
| GP5 | SPI0 CS — SD card |
| GP8 | UART1 TX — display |
| GP9 | UART1 RX — display |
| GP12 | Digital OUT — moisture probe AC excitation ch1 |
| GP13 | Digital OUT — moisture probe AC excitation ch2 |
| GP14 | PWM — intake vent servo |
| GP15 | PWM — exhaust vent servo |
| GP16 | PWM — exhaust fan |
| GP17 | PWM — circulation fans (×3 shared) |
| GP18 | Digital OUT — SSR heater control |
| GP19 | Digital OUT — circulation fan MOSFET gate |
| GP21 | Digital OUT — exhaust fan MOSFET gate |
| GP22 | Digital IN — exhaust fan tach |
| GP26 | ADC0 — moisture probe ch1 |
| GP27 | ADC1 — moisture probe ch2 |
| GP28 | ADC2 — spare |

---

## Repo structure

```
/
├── main.py                  # Entry point (to be written)
├── lib/
│   ├── exhaust.py           # Exhaust fan module ⚠️ see note below
│   ├── circulation.py       # Circulation fans module
│   ├── sdcard.py            # SD card mount/unmount (to be written)
│   └── logger.py            # Logging service (to be written)
├── CLAUDE.md                # This file
└── PROJECT.md               # Software state summary (maintained by Claude Code)
```

> **⚠️ exhaust.py note:** The version in the repo is outdated. The correct updated
> version has gate_pin=21 (separate from pwm_pin=16), persistent `self._gate` Pin
> object initialised low at boot, `on()` drives gate high then sets PWM duty,
> `off()` zeros PWM duty then drives gate low. Claude Code should replace the repo
> version with the correct implementation described in the logging spec before
> proceeding.

---

## Module conventions

All modules in `lib/` follow the pattern established in `circulation.py`:

- Class-based, one class per module
- Constants at top of file (pin numbers, frequencies, thresholds)
- `__init__` initialises all hardware to a safe off/idle state
- Public API: `on()`, `off()`, `set_speed()` / `read_rpm()` etc. as appropriate
- `is_running` property where relevant
- `tick()` method where the module needs periodic updates from the main loop
- Bottom of file: `test()` function and `if __name__ == "__main__": test()` block
- Unit tests are self-contained — print PASS/FAIL per assertion, return bool

**Dependency injection for logger:** Once `lib/logger.py` exists, modules accept
an optional `logger=None` parameter in `__init__`. When provided, they call
`logger.event(source, message)` for significant state changes. They never import
logger directly — it is always passed in.

---

## Coding standards

- MicroPython target — no CPython-only stdlib (no `pathlib`, `datetime`, etc.)
- Use `machine`, `time`, `uos`, `uio` — MicroPython builtins only
- Timestamps from `time.localtime()` — Pico has no RTC battery so time will be
  set at run-start from the mobile app via the REST API; before that, use elapsed
  seconds from `time.ticks_ms()`
- No third-party packages
- f-strings are fine (MicroPython supports them)
- Silent fail pattern: wrap hardware calls in try/except, print warning to REPL,
  continue — never crash the kiln over a peripheral failure

---

## Current software state

See `PROJECT.md` for the up-to-date summary of what is implemented and tested.

---

## How to deploy

Copy files to the Pico using `mpremote` or Thonny. The Pico runs `main.py` on boot.
During development, modules can be run directly via the REPL:

```python
from lib.circulation import test
test()
```

Or with mpremote:
```bash
mpremote run lib/circulation.py
```
