# Kiln Controller

A MicroPython-based lumber drying kiln controller for a passive solar kiln in the
Georgian Bay area of Ontario. Designed for drying hardwood lumber (maple and beech,
0.5--1 inch thick) using multi-stage schedules derived from USDA Forest Products
Laboratory references (FPL-GTR-57 and FPL-GTR-118).

The kiln is approximately 5 ft x 2 ft x 3 ft, heated passively by solar gain with a
500W ceramic PTC backup heater for shoulder-season use. A Raspberry Pi Pico 2W
controls all kiln hardware. Telemetry is transmitted over LoRa (~200m) to a Raspberry
Pi 4 at the cottage, which stores data in SQLite and serves a REST API for a Kivy
mobile app.

---

## Hardware

| Subsystem | Component |
|---|---|
| MCU | Raspberry Pi Pico 2W |
| Temperature / RH | 2x SHT31-D (I2C, 0x44 / 0x45) |
| Moisture probes | Resistive, voltage divider, AC excitation |
| Exhaust fan | Foxconn PVA080G12Q 80mm PWM, tach feedback |
| Circulation fans | 3x Thermalright TL-C12C 120mm PWM |
| Vent dampers | 2x MG90S servo (intake + exhaust) |
| Backup heater | 500W PTC + Fotek SSR-25DA + RY85 thermal fuse |
| Current monitoring | 2x INA219 (12V rail + 5V rail) |
| Display | JC035-HVGA-ST-02 3.5" UART serial, 320x480 |
| SD card | SPI micro SD (logging) |
| LoRa radio | AI-Thinker Ra-02 433 MHz (SX1278), TX-only |
| Remote server | Raspberry Pi 4 with Ra-02 receiver |

---

## Schematic and custom PCB

KiCad 9.0 project with schematic and custom PCB layout is in the PCB folder.

---

## Firmware (Pico 2W -- MicroPython)

All hardware drivers live in `lib/`. Every module is class-based, accepts pin numbers
as constructor arguments, and follows the pattern established in `lib/exhaust.py`.

| Module | Function |
|---|---|
| `lib/exhaust.py` | 80mm exhaust fan -- PWM + tach |
| `lib/circulation.py` | 3x 120mm circulation fans -- PWM group |
| `lib/vents.py` | Intake + exhaust damper servos |
| `lib/heater.py` | SSR on/off control |
| `lib/SHT31sensors.py` | Dual SHT31 temperature + RH |
| `lib/moisture.py` | Resistive moisture probes, species correction |
| `lib/current.py` | INA219 DC current monitoring |
| `lib/lora.py` | SX1278 LoRa transmitter (TX-only, register polling) |
| `lib/display.py` | UART serial display driver + button + page system |
| `lib/sdcard.py` | SPI SD card mount/unmount wrapper |
| `lib/logger.py` | Event log + CSV data log to SD card |
| `lib/schedule.py` | Multi-stage drying schedule controller |
| `main.py` | Entry point -- asyncio control loop + WiFi AP + REST API |
| `config.py` | Deployment configuration (SSID, key, LoRa params) |

Drying schedules are stored as JSON in `schedules/` and loaded from the SD card at
runtime. Schedules are included for hard maple and beech at 0.5 in and 1 in thickness.

### Deploying to the Pico

```
python update_lib.py
```

This copies `main.py`, `config.py`, and all `lib/` files to the Pico in one step.
The MicroPython SPI SD card driver must be deployed as `sdcard_driver.py`:

```
mpremote cp sdcard_driver.py :sdcard_driver.py
```

### Running tests

```
mpremote run test_modules.py
```

Tests all 12 modules in sequence and reports pass/fail. Does not start WiFi or the
control loop.

---

## Cottage-side architecture (Pi4)

```
Pico --> SPI1 --> Ra-02 ~~LoRa~~ Ra-02 --> SPI0 --> Pi4 kiln_server
                                                 --> SQLite
                                                 --> REST API :8080 --> Kivy app
                                                 --> ntfy.sh --> phone
```

The `kiln_server/` Python package (in progress) runs as a systemd service on the Pi4.
It receives LoRa packets from the Pico, writes telemetry and alerts to SQLite, serves
a REST API for the mobile app, and pushes fault alerts via ntfy.sh.

The Pico also runs a WiFi AP and exposes its own REST API (port 80) for direct
local control when within range.

---

## Drying schedules

Schedules follow USDA FPL kiln-drying recommendations:

- Hard maple (T3-C2): `schedules/maple_05in.json`, `schedules/maple_1in.json`
- Beech (T4-C3): `schedules/beech_05in.json`, `schedules/beech_1in.json`

Each schedule has 9 stages: 7 drying stages + equalizing + conditioning. Stage advance
is automatic (MC% + minimum time) for drying stages, and time-only for
equalizing/conditioning. Manual advance is available via the REST API.

---

## Status

Pico firmware is at the integration stage. All 12 `lib/` modules are complete and
hardware-tested. `main.py` is implemented and pending full hardware integration test.
The Pi4 `kiln_server` daemon and Kivy mobile app are next.
