# SPEC: `lib/sensors.py` — SHT31 Dual Sensor Module

## Purpose

Read temperature and relative humidity from two SHT31-D sensors over I²C. One sensor
is mounted in the lumber zone; the other is near the intake. The module follows the
established class-based pattern from `exhaust.py`.

---

## Hardware

| Parameter | Value |
|---|---|
| Sensor | SHT31-D (×2) |
| Interface | I²C (via `machine.I2C`) |
| SDA | GP0 |
| SCL | GP1 |
| Sensor A address | `0x44` (ADDR pin low — lumber zone) |
| Sensor B address | `0x45` (ADDR pin pulled high — intake) |
| Supply voltage | 3.3V |

---

## Class: `SHT31Sensors`

### Constructor

```python
SHT31Sensors(sda_pin=0, scl_pin=1, freq=100_000, logger=None)
```

- Creates a single `machine.I2C` bus shared by both sensors
- Scans the bus on init; raises `RuntimeError` if either `0x44` or `0x45` is not found
- Stores `logger` for dependency injection (never imports logger directly)

---

### Public Methods

#### `read() -> dict`

Issues a single-shot high-repeatability measurement command (`0x2C06`) to each sensor
in sequence, waits the required 15 ms, reads 6 bytes, verifies both CRC bytes, and returns:

```python
{
    "temp_lumber":  float,  # °C, lumber zone (0x44)
    "rh_lumber":    float,  # %RH, lumber zone (0x44)
    "temp_intake":  float,  # °C, intake (0x45)
    "rh_intake":    float,  # %RH, intake (0x45)
}
```

Returns `None` for any sensor that fails (CRC error, I²C NACK, timeout) rather than
raising — the kiln keeps running with partial data. Logs a WARNING via `logger.event()`
if a logger is provided.

#### `read_lumber() -> tuple[float, float] | None`

Convenience wrapper returning `(temp_c, rh_pct)` for the lumber zone sensor, or `None`
on failure.

#### `read_intake() -> tuple[float, float] | None`

Convenience wrapper returning `(temp_c, rh_pct)` for the intake sensor, or `None` on
failure.

#### `soft_reset()`

Sends the soft-reset command (`0x30A2`) to both sensors. Waits 2 ms after reset per
datasheet. Safe to call on init before the first read.

---

## CRC

Use the SHT31 CRC-8 polynomial: `x^8 + x^5 + x^4 + 1` (init `0xFF`, poly `0x31`).
Verify both the temperature word CRC and the humidity word CRC from the 6-byte response.
On CRC failure, return `None` for that sensor and log a warning.

---

## Conversion Formulas

From the SHT31 datasheet:

```
temp_c = -45 + 175 * raw_temp / 65535
rh_pct = 100 * raw_rh / 65535
```

---

## Unit Tests

Follow the pattern in `exhaust.py`. All tests are hardware-in-the-loop and run from
the REPL.

| Test | What it checks |
|---|---|
| `test_init()` | Both addresses found on bus scan; no exception on construction |
| `test_read_both()` | `read()` returns a dict with all four keys; values are physically plausible (temp −10–80 °C, RH 0–100%) |
| `test_crc()` | Manually corrupt a raw byte and confirm `None` is returned rather than a bad value |
| `test_lumber_intake_convenience()` | `read_lumber()` and `read_intake()` each return a 2-tuple of floats |
| `test_soft_reset()` | `soft_reset()` completes without exception; subsequent `read()` succeeds |
| `test_logger_called()` | Construct with a mock logger; induce a failure (disconnect one sensor if possible, or patch `i2c.readfrom`); confirm `logger.event()` is called with level `"WARNING"` |

---

## Key Constraints for Claude Code

- **Follow `exhaust.py` as the structural template** — class-based, pin numbers as
  constructor args, hardware-in-the-loop unit tests in the same file
- **Single shared I²C bus** — one `machine.I2C` instance for both sensors; do not
  create two buses
- **No third-party libraries** — implement the SHT31 protocol directly using
  MicroPython's `machine.I2C` `writeto` / `readfrom` calls only
- **Logger dependency injection** — accept `logger=None`; call
  `logger.event(source, message, level)` only when provided; never import logger
- **Silent failure on partial reads** — a single bad sensor must not crash the module;
  return `None` for that sensor's values
- **GP0/GP1 are I²C0** — PROJECT.md shows these pins unallocated; no conflicts expected

---

## Naming Conventions

- The `source` string passed to `logger.event()` must be `"sensors"`
- The dict keys from `read()` — `temp_lumber`, `rh_lumber`, `temp_intake`,
  `rh_intake` — are canonical. The drying schedule controller and logger will use
  these exact keys.
- The `data()` call in `lib/logger.py` accepts a dict directly, so `read()` output
  can be passed straight through to it with no transformation.