# Current Monitor Spec — lib/current.py

## Purpose
Read DC current, bus voltage, and power from two INA219 modules via I2C0.
Use readings to verify expected operation of circulation fans and vent servos,
and to trigger fault conditions when current is out of expected range.

## Hardware
- Two CJMCU-219 (INA219) modules on I2C0 (GP0=SDA, GP1=SCL)
- INA219 #1: 12V rail monitor — address 0x40 (A0, A1 unsoldered)
- INA219 #2: 5V rail monitor — address 0x41 (A0 bridged, A1 unsoldered)
- Shunt resistor: 0.1Ω onboard (R100) — max 3.2A
- Both modules powered from Pico 3.3V rail

## Module: lib/current.py

### Class: CurrentMonitor

Constructor:
  CurrentMonitor(i2c, address, label, logger=None)
  - i2c: machine.I2C instance (shared, passed in — not created internally)
  - address: int — I2C address (0x40 or 0x41)
  - label: str — "12V" or "5V" for logging context
  - logger: optional Logger instance

### INA219 register interface (implement directly — no external library)
Use raw I2C register reads/writes. Registers:
  - 0x00: Configuration
  - 0x01: Shunt voltage (raw, LSB = 10µV)
  - 0x02: Bus voltage (raw, bits 15:3, LSB = 4mV, shift right 3)
  - 0x03: Power (raw, LSB = 2mW with default calibration)
  - 0x04: Current (raw, LSB depends on calibration register)
  - 0x05: Calibration

Calibration for 0.1Ω shunt, 3.2A max:
  - Cal = trunc(0.04096 / (0.1 * 0.0001)) = 4096
  - Write 0x1000 to calibration register (0x05)
  - Current LSB = 0.1mA per bit
  - Power LSB = 2mW per bit

### Methods

read() -> dict:
  Returns {
    "bus_voltage_V": float,   # Bus voltage in volts
    "current_mA": float,      # Current in milliamps (signed)
    "power_mW": float,        # Power in milliwatts
    "label": str              # "12V" or "5V"
  }
  - Returns None on I2C read failure (do not raise)
  - Log read failures via logger.event() if logger provided

check_range(min_mA, max_mA) -> bool:
  - Calls read(), returns True if current_mA within [min_mA, max_mA]
  - Returns None on read failure
  - Log out-of-range events via logger.event(level="WARN")

### Unit tests (hardware-in-the-loop, same pattern as exhaust.py)
- Confirm device responds on I2C bus (detect address)
- Read bus voltage — assert within plausible range (e.g. 11–13V for 12V rail)
- Read current at idle — log value, no assertion (load-dependent)
- Confirm read() returns correct dict keys

## Integration: lib/circulation.py

Add to CirculationFans class:
  - Accept optional current_monitor=None in __init__
  - Add verify_running(expected_min_mA, expected_max_mA) -> bool method:
      Calls current_monitor.check_range() and returns result.
      Logs WARN via logger if out of range.
      Returns None if no current_monitor provided.
  - Call verify_running() in on() after a short delay (50ms) to confirm
    fans drew expected current on startup.
  - Typical 3× TL-C12C at 12V: ~300–900mA range (measure and tune).

## Integration: lib/vents.py 

Apply same pattern to Vents class:
  - Accept optional current_monitor=None for 5V rail monitor
  - Add verify_position(expected_min_mA, expected_max_mA) -> bool
  - MG90S servo at 5V under light load: ~100–400mA during movement,
    ~5–20mA holding. Fault if current spikes > 600mA (jammed/stalled).

## Shared I2C instance
The I2C0 instance must be created once in main.py and passed to all
modules that use it (CurrentMonitor ×2, SHT31 ×2 when built).
Do NOT create I2C inside CurrentMonitor.__init__.

Example main.py wiring (for reference only — main.py not yet written):
  from machine import I2C, Pin
  i2c = I2C(0, sda=Pin(0), scl=Pin(1), freq=400_000)
  mon_12v = CurrentMonitor(i2c, 0x40, "12V", logger=logger)
  mon_5v  = CurrentMonitor(i2c, 0x41, "5V",  logger=logger)

## Fault thresholds (initial values — tune after hardware measurement)
  12V rail idle (fans off):   < 50mA
  12V rail fans on (3×):      300–900mA
  5V rail idle:               < 200mA  
  5V rail servos active:      200–600mA
  FAULT — any rail > 2500mA: log ERROR, do not shut down autonomously
                              (let schedule controller decide action)

## Logger integration
  - log event on: startup read success/failure, out-of-range current,
    I2C errors
  - do NOT log every periodic read (too verbose for SD card)
  - logger calls use source="current_12v" or "current_5v"