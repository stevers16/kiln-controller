# Solar Wood Drying Kiln - Claude Code Project Context

## Project overview

A solar-powered wood drying kiln (~30 cubic feet, 5ft x 2ft x 3ft) controlled by a
Raspberry Pi Pico 2 W running MicroPython. The kiln dries hardwood (maple, beech,
0.5-1 inch boards) using multi-stage schedules with progressive temperature and
humidity targets.

This repo contains three software workstreams:

1. **Pico firmware** (MicroPython) at the repo root and in `lib/` - controls the
   kiln hardware, runs a WiFi AP and REST API, and transmits LoRa telemetry.
2. **Pi4 cottage daemon** `kiln_server/` (planned) - receives LoRa packets,
   stores telemetry in SQLite, serves a REST API, pushes ntfy.sh alerts.
3. **Kivy mobile/desktop app** `KivyApp/` - the primary human interface to the
   kiln. Talks to either the Pico AP REST API (full control) or the Pi4 daemon
   REST API (read-only monitoring).

Hardware design, component selection, and wiring decisions are handled separately
in Claude.ai (project: Solar Wood Kiln). Claude Code is responsible for software
only. When hardware questions arise, flag them rather than making assumptions.

---

## Hardware summary

| Component | Detail |
|---|---|
| Controller | Raspberry Pi Pico 2 W (MicroPython) |
| Temp/RH sensors | 2x SHT31 on I2C (GP0/GP1) |
| Exhaust fan | Foxconn PVA080G12Q 80mm, PWM GP16, gate GP21, tach GP22 |
| Circulation fans | 3x Thermalright TL-C12C 120mm, PWM GP17, gate GP19 |
| Vent servos | 2x MG90S, GP14 (intake), GP15 (exhaust) |
| Backup heater | 500W PTC via Fotek SSR-25DA, GP18 |
| Moisture probes | 2x voltage divider ADC GP26/GP27, AC excitation GP6/GP7 |
| Current sensors | 2x INA219 (12V rail 0x40, 5V rail 0x41) on shared I2C |
| Display | JC035-HVGA-ST-02-V02 3.5" UART, UART1 GP8/GP9 |
| SD card | SPI micro SD module, GP2-GP5 |
| LoRa radio | AI-Thinker Ra-02 (SX1278, 433 MHz), SPI1 GP10-GP13, RST GP28 |
| Wi-Fi | AP mode, HTTP/REST API for Kivy app |

### Pico 2 W GPIO map

| GPIO | Physical Pin | Function | Connected To |
|------|-------------|----------|--------------|
| GP0 | Pin 1 | I2C0 SDA | SHT31 #1 + SHT31 #2 + INA219 12V (0x40) + INA219 5V (0x41) - shared I2C bus |
| GP1 | Pin 2 | I2C0 SCL | SHT31 #1 + SHT31 #2 + INA219 12V (0x40) + INA219 5V (0x41) - shared I2C bus |
| GP2 | Pin 4 | SPI0 MISO | Micro SD card module |
| GP3 | Pin 5 | SPI0 MOSI | Micro SD card module |
| GP4 | Pin 6 | SPI0 SCK | Micro SD card module |
| GP5 | Pin 7 | SPI0 CS | Micro SD card module chip select |
| GP6 | Pin 9 | Digital OUT | Moisture probe AC excitation Ch1 - polarity flip to prevent corrosion |
| GP7 | Pin 10 | Digital OUT | Moisture probe AC excitation Ch2 - polarity flip to prevent corrosion |
| GP8 | Pin 11 | UART1 TX | JC035 display RX (3.5" UART serial display) |
| GP9 | Pin 12 | UART1 RX | JC035 display TX (3.5" UART serial display) |
| GP10 | Pin 14 | SPI1 SCK | LoRa Ra-02 (SX1278, 433 MHz) |
| GP11 | Pin 15 | SPI1 TX | LoRa Ra-02 MOSI |
| GP12 | Pin 16 | SPI1 RX | LoRa Ra-02 MISO |
| GP13 | Pin 17 | SPI1 CS | LoRa Ra-02 NSS (active low) |
| GP14 | Pin 19 | PWM | MG90S servo - intake vent flap |
| GP15 | Pin 20 | PWM | MG90S servo - exhaust vent flap |
| GP16 | Pin 21 | Digital OUT / PWM | Exhaust fan PWM - Foxconn PVA080G12Q 80mm |
| GP17 | Pin 22 | Digital OUT / PWM | Circulation fan PWM - 3x TL-C12C 120mm |
| GP18 | Pin 24 | Digital OUT | SSR control - Fotek SSR-25DA -> 120V backup heater |
| GP19 | Pin 25 | Digital OUT | FQP30N06L MOSFET gate - circulation fan on/off |
| GP20 | Pin 26 | Digital IN | Display button (active-low, internal pull-up) |
| GP21 | Pin 27 | Digital OUT | FQP30N06L MOSFET gate - exhaust fan on/off |
| GP22 | Pin 29 | Digital IN | Exhaust fan tach - falling-edge IRQ, 10k ohm pull-up + 104 cap to GND |
| GP26 | Pin 31 | ADC0 | Moisture probe Ch1 (maple) - 100k ohm voltage divider |
| GP27 | Pin 32 | ADC1 | Moisture probe Ch2 (beech) - 100k ohm voltage divider |
| GP28 | Pin 34 | Digital OUT | LoRa Ra-02 RST (active low) |
| VBUS | Pin 40 | 5V input | LM2596 buck converter output (fed by 12V wall brick) |
| GND | Pin 38 | Ground | Common ground - 12V wall brick GND rail |


---

## Repo structure

```
/
├── main.py                  # Pico entry point - asyncio control loop + REST API
├── boot.py                  # Pico boot hook
├── config.py                # Pico deployment config (SSID, key, LoRa params)
├── update_lib.py            # Helper - copies firmware files to Pico via mpremote
├── test_modules.py          # Pico-side standalone test runner for all lib modules
├── sdcard_driver.py         # MicroPython SPI SD driver (deployed as sdcard_driver.py)
├── lora_test_tx.py          # Pico-side LoRa TX test
├── lora_test_rx.py          # Pi4-side LoRa RX test
├── lib/                     # Pico firmware modules (MicroPython)
│   ├── circulation.py
│   ├── exhaust.py
│   ├── sdcard.py
│   ├── logger.py
│   ├── vents.py
│   ├── current.py
│   ├── SHT31sensors.py
│   ├── heater.py
│   ├── display.py
│   ├── moisture.py
│   ├── lora.py
│   └── schedule.py
├── schedules/               # FPL drying schedule JSON (maple/beech, 0.5"/1")
├── KivyApp/                 # Kivy mobile/desktop app (Python 3 / CPython)
├── kiln_server/             # Pi4 daemon (planned, not yet implemented)
├── Specs/                   # Design specs for each subsystem
├── PCB/                     # KiCad 9 schematic + custom PCB layout
├── CLAUDE.md                # This file
└── PROJECT.md               # Software state summary (maintained by Claude Code)
```

---

## Module conventions (Pico firmware)

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

## Coding standards (Pico firmware only)

These standards apply to `main.py`, `boot.py`, `config.py`, and everything in
`lib/`. They do NOT apply to the Kivy app (see "Kivy app development practices"
below) or to the Pi4 `kiln_server` daemon, which both run on standard CPython 3.

- MicroPython target - no CPython-only stdlib (no `pathlib`, `datetime`, etc.)
- Use `machine`, `time`, `uos`, `uio` - MicroPython builtins only
- Timestamps from `time.localtime()` - Pico has no RTC battery so time will be
  set at run-start from the mobile app via the REST API; before that, use elapsed
  seconds from `time.ticks_ms()`
- No third-party packages
- f-strings are fine (MicroPython supports them)
- **ASCII only in all strings:** use only ASCII characters in print statements,
  logging calls, comments, and docstrings. No em dashes, en dashes, arrows,
  degree signs, ohm signs, or any other non-ASCII. Use `-`, `->`, `<-`, `deg`,
  `ohm` etc. as substitutes.
- Silent fail pattern: wrap hardware calls in try/except, print warning to REPL,
  continue - never crash the kiln over a peripheral failure

---

## Kivy app development practices

These rules govern everything under `KivyApp/`. They are intentionally different
from the firmware coding standards above.

1. **Location.** All Kivy app code lives under `KivyApp/`. Nothing Kivy-related
   goes in the repo root or in `lib/` (which is MicroPython-only).
2. **Targets.** Primary target is an Android phone via buildozer. Desktop
   (Windows/macOS) is a fully supported testing target - every phase of
   development must run on the desktop. Android packaging is the final phase.
3. **Python environment.** Use a dedicated virtual environment under
   `KivyApp/.venv/` (gitignored). Do not install Kivy globally. Pin versions in
   `KivyApp/requirements.txt`.
4. **Incremental delivery with approval gates.** Build the app one phase at a
   time. After each phase, stop, ask the user to test the desktop build, and
   wait for explicit approval before starting the next phase. Do not bundle
   multiple phases together. Update PROJECT.md at the end of every phase before
   asking for approval.
5. **Status tracking.** PROJECT.md is the single source of truth for Kivy app
   status. The "KivyApp" section there tracks each phase.
6. **No hardware blocking.** Kivy code must never block the main thread on
   hardware or network IO. All HTTP calls run async or on a worker thread, with
   results delivered back to the Kivy main thread via `Clock.schedule_once`.
7. **Mode-aware UI.** AP-only screens and actions must be hidden or visibly
   disabled in Cottage mode - never silently dropped. The app never silently
   attempts a control action when not connected to the Pico AP.
8. **Standard CPython 3 conventions apply.** Kivy is desktop Python, not
   MicroPython. The ASCII-only and "no stdlib" rules from the firmware do NOT
   apply here. Use `requests`, `asyncio`, `pathlib`, `dataclasses`, etc. freely.
9. **Spec source of truth.** [Specs/kivy_app_spec.md](Specs/kivy_app_spec.md) is
   the canonical UI spec. Pico REST API surface is documented in `main.py`. The
   Pi4 daemon REST API and columnar `/history` shape are in
   `Specs/lora_telemetry_spec.md`.

---

## Current software state

See `PROJECT.md` for the up-to-date summary of what is implemented and tested
across all three workstreams (Pico firmware, Pi4 daemon, Kivy app).

---

## How to deploy

### Pico firmware
Copy files to the Pico using the helper script (uses `mpremote` under the hood):

```bash
python update_lib.py
```

This copies `main.py`, `config.py`, and all `lib/` files in one step. The
MicroPython SPI SD card driver must be deployed once as `sdcard_driver.py`:

```bash
mpremote cp sdcard_driver.py :sdcard_driver.py
```

During development, individual modules can be run directly:

```bash
mpremote run lib/circulation.py
```

### Kivy app
See `KivyApp/README.md`. Desktop development uses a venv under `KivyApp/.venv/`.
Android packaging via buildozer is deferred to the final development phase.
