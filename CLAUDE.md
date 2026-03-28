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
| Moisture probes | 2x voltage divider ADC GP26/GP27, AC excitation GP6/GP7 |
| Display | JC035-HVGA-ST-02-V02 3.5" UART, UART1 GP8/GP9 |
| SD card | SPI micro SD module, GP2–GP5 |
| Wi-Fi | AP mode, HTTP/REST API for mobile app |

### GPIO map

Here's the GPIO map section ready to drop into CLAUDE.md:
markdown## Pico 2 W GPIO Map

| GPIO | Physical Pin | Function | Connected To |
|------|-------------|----------|--------------|
| GP0 | Pin 1 | I²C0 SDA | SHT31 #1 + SHT31 #2 + INA219 12V (0x40) + INA219 5V (0x41) — shared I²C bus. SHT31 addresses TBD. |
| GP1 | Pin 2 | I²C0 SCL | SHT31 #1 + SHT31 #2 + INA219 12V (0x40) + INA219 5V (0x41) — shared I²C bus. SHT31 addresses TBD. |
| GP2 | Pin 4 | SPI0 MISO | Micro SD card module |
| GP3 | Pin 5 | SPI0 MOSI | Micro SD card module |
| GP4 | Pin 6 | SPI0 SCK | Micro SD card module |
| GP5 | Pin 7 | SPI0 CS | Micro SD card module chip select |
| GP6 | Pin 9 | Digital OUT | Moisture probe AC excitation Ch1 -- polarity flip to prevent corrosion |
| GP7 | Pin 10 | Digital OUT | Moisture probe AC excitation Ch2 -- polarity flip to prevent corrosion |
| GP8 | Pin 11 | UART1 TX | JC035 display RX (3.5" UART serial display) |
| GP9 | Pin 12 | UART1 RX | JC035 display TX (3.5" UART serial display) |
| GP10 | Pin 14 | SPI1 SCK | LoRa Ra-02 (SX1278, 433 MHz) |
| GP11 | Pin 15 | SPI1 TX | LoRa Ra-02 MOSI |
| GP12 | Pin 16 | SPI1 RX | LoRa Ra-02 MISO |
| GP13 | Pin 17 | SPI1 CS | LoRa Ra-02 NSS (active low) |
| GP14 | Pin 19 | PWM | MG90S servo — intake vent flap |
| GP15 | Pin 20 | PWM | MG90S servo — exhaust vent flap |
| GP16 | Pin 21 | Digital OUT / PWM | Exhaust fan PWM — Foxconn PVA080G12Q 80mm |
| GP17 | Pin 22 | Digital OUT / PWM | Circulation fan PWM — 3× TL-C12C 120mm |
| GP18 | Pin 24 | Digital OUT | SSR control — Fotek SSR-25DA → 120V backup heater |
| GP19 | Pin 25 | Digital OUT | FQP30N06L MOSFET gate — circulation fan on/off |
| GP20 | Pin 26 | Digital IN | Display button (active-low, internal pull-up) |
| GP21 | Pin 27 | Digital OUT | FQP30N06L MOSFET gate -- exhaust fan on/off |
| GP22 | Pin 29 | Digital IN | Exhaust fan tach — falling-edge IRQ, 10kΩ pull-up + 104 cap to GND |
| GP26 | Pin 31 | ADC0 | Moisture probe Ch1 (maple) — 100kΩ voltage divider |
| GP27 | Pin 32 | ADC1 | Moisture probe Ch2 (beech) — 100kΩ voltage divider |
| GP28 | Pin 34 | Digital OUT | LoRa Ra-02 RST (active low) |
| VBUS | Pin 40 | 5V input | LM2596 buck converter output (fed by 12V wall brick) |
| GND | Pin 38 | Ground | Common ground — 12V wall brick GND rail |


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
- **ASCII only in all strings:** use only ASCII characters in print statements,
  logging calls, comments, and docstrings. No em dashes (`—`), en dashes (`–`),
  arrows (`→`, `←`), degree signs (`°`), ohm signs (`Ω`), or any other non-ASCII.
  Use `-`, `->`, `<-`, `deg`, `ohm` etc. as substitutes.
- Silent fail pattern: wrap hardware calls in try/except, print warning to REPL,
  continue - never crash the kiln over a peripheral failure

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
