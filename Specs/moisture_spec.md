# Moisture Probe Spec -- lib/moisture.py

Spec for Claude Code to implement `lib/moisture.py`.

---

## Purpose

Reads wood moisture content (MC%) from two resistive probe channels.
Each channel consists of a voltage divider with a 100kohm reference resistor,
an AC excitation GPIO, and an ADC input. AC excitation alternates current
direction between readings to prevent electrolysis on the probe pins.

---

## Hardware

### Circuit (per channel, ch2 identical)

```
GP12 (excitation) ---[R6 100kohm]---+--- GP26 (ADC0)
                                    |
                                R_wood  (probe pins in lumber)
                                    |
                                   GND
```

- GP12 driven HIGH during a measurement, LOW otherwise
- GP26 reads voltage at the junction between R6 and R_wood
- When GP12 is HIGH: V_adc = 3.3V * R_wood / (100000 + R_wood)
- When GP12 is LOW: both ends of the divider are at 0V -- ADC will read ~0
  -- this phase is NOT used for measurement; it exists only to alternate
  current direction and prevent electrolysis

### Pin assignments

| Signal         | GPIO | Notes                              |
|----------------|------|------------------------------------|
| Ch1 excitation | GP12 | Digital OUT, driven HIGH to measure|
| Ch1 ADC        | GP26 | ADC0, reads divider midpoint       |
| Ch2 excitation | GP13 | Digital OUT, driven HIGH to measure|
| Ch2 ADC        | GP27 | ADC1, reads divider midpoint       |

### Probe construction

- Two stainless steel pins per probe, ~25-30mm apart, driven into face grain
- One pin connects to the GP12/R6/GP26 junction node
- Other pin connects to GND
- Polarity does not matter for the resistance measurement

---

## Class interface

```python
class MoistureProbe:
    def __init__(self,
                 excite_pin_1=12, adc_pin_1=26,
                 excite_pin_2=13, adc_pin_2=27,
                 species_1="maple", species_2="beech",
                 logger=None):
```

Constructor args are pin numbers following the exhaust.py pattern.
Both channels are always instantiated together in a single object.

---

## Constructor behaviour

- Initialise both excitation pins as digital outputs, driven LOW
- Initialise both ADC pins using `machine.ADC`
- Store species strings for MC% correction (see Calibration section)
- Store logger reference (may be None)
- Log init event if logger provided:
  `logger.event("moisture", "Moisture probe init -- ch1=maple ch2=beech")`
- Do NOT take any readings at construction time

---

## Read sequence

Each channel is read independently and sequentially (never simultaneously).

### Single channel read -- internal method `_read_channel(excite_pin, adc, samples=5)`

1. Drive excite_pin HIGH
2. Wait 15ms for signal to settle (RC settling time with wood resistance)
3. Take `samples` ADC readings with 2ms between each
4. Drive excite_pin LOW
5. Wait 10ms (allow any charge to dissipate before next operation)
6. Average the samples (discard none -- noise is low at these frequencies)
7. Convert ADC counts to voltage: `v = adc_val / 65535 * 3.3`
8. Calculate R_wood from voltage divider:
   - `R_ref = 100000`  (100kohm)
   - If v >= 3.3 or v <= 0: return None (open circuit or short -- probe disconnected)
   - `R_wood = R_ref * v / (3.3 - v)`
9. Return R_wood in ohms (float), or None on invalid reading

### Public read methods

```python
def read_resistance(self) -> dict:
    """
    Returns raw resistance readings for both channels.
    dict keys: "ch1_ohms", "ch2_ohms"
    Values are float (ohms) or None if probe disconnected / invalid.
    """

def read(self) -> dict:
    """
    Returns MC% for both channels plus raw resistance.
    dict keys: "ch1_mc_pct", "ch2_mc_pct", "ch1_ohms", "ch2_ohms"
    MC% values are float or None.
    Logs WARNING via logger if either channel returns None.
    """
```

`read()` is the primary method called by the schedule controller.

---

## Resistance to MC% conversion

Wood MC% is derived from R_wood via a lookup table. Resistance varies
enormously with MC (roughly 1kohm at 30% MC down to 10Mohm+ below 7% MC),
so the lookup table is log-spaced.

### Base lookup table (Douglas fir reference, before species correction)

This is the published industry-standard resistance-to-MC curve used in
commercial pin-type meters. Values are approximate and calibration via
handheld meter is expected (see Calibration section).

```python
# (R_wood_ohms, MC_percent) pairs, log-spaced
# Source: Forest Products Laboratory Wood Handbook
RESISTANCE_TABLE = [
    (1_000,     30.0),
    (2_000,     27.0),
    (5_000,     24.0),
    (10_000,    21.5),
    (20_000,    19.0),
    (50_000,    16.5),
    (100_000,   14.5),
    (200_000,   12.5),
    (500_000,   10.5),
    (1_000_000,  8.5),
    (2_000_000,  7.0),
    (5_000_000,  6.0),
]
```

Interpolation is log-linear: convert resistance to log10, interpolate
linearly between the two bracketing table entries, return MC%.

If R_wood < 1kohm: return 30.0 (clamp -- wood is at or above fibre saturation)
If R_wood > 5Mohm: return None (wood is too dry to measure reliably; below
fibre drying range for kiln purposes)

### Species correction factors

Apply a species correction offset (in MC% points) after the base lookup:

```python
SPECIES_CORRECTION = {
    "maple":  -0.5,   # Hard maple reads slightly low vs Douglas fir reference
    "beech":  -0.3,   # European beech, similar to maple
    "douglas_fir": 0.0,  # Reference species
    "oak":    +0.5,
    "pine":   +0.3,
}
```

These are small corrections. If the species string is not found in the dict,
apply 0.0 correction and log a WARNING once at construction time.

### Helper function

```python
def resistance_to_mc(r_ohms, species="douglas_fir") -> float | None:
```

Module-level function (not a method), usable independently for calibration
and testing. Applies table lookup then species correction. Returns None if
resistance is out of range.

---

## Calibration

The lookup table provides a starting point. Actual probe accuracy depends on:
- Exact resistor values (1% tolerance recommended for R6/R7)
- Contact resistance at probe pins
- Wood temperature (resistance drops ~3% per degC -- significant in a kiln)

### Temperature correction

At kiln operating temperatures (40-80degC), uncorrected resistance readings
will report MC% too high. Apply a temperature correction:

```python
def read_with_temp_correction(self, temp_c: float) -> dict:
    """
    Same as read() but applies temperature correction factor.
    temp_c: wood temperature from SHT31 lumber zone sensor.
    Returns same dict as read(), with corrected MC% values.
    """
```

Temperature correction factor (multiplicative on MC%):

```python
# Reference temperature is 20degC (standard meter calibration temp)
# Correction: approx +0.06 MC% per degC below 20, -0.06 above 20
# i.e. at 60degC kiln temp, raw MC% is ~2.4 points too high
TEMP_REF_C = 20.0
TEMP_CORRECTION_PER_DEG = 0.06  # MC% per degC deviation

def _apply_temp_correction(mc_raw, temp_c):
    correction = (TEMP_REF_C - temp_c) * TEMP_CORRECTION_PER_DEG
    return mc_raw + correction
```

`read_with_temp_correction()` is the method the schedule controller should
use during kiln operation. `read()` is available for bench testing without
a temperature input.

### Handheld meter calibration workflow

The expected field calibration procedure (not implemented in firmware,
documented here for context):

1. Insert probes into a board sample
2. Read both the Pico ADC value and the handheld meter simultaneously
3. Record pairs: (R_wood computed from ADC, MC% from handheld)
4. Adjust RESISTANCE_TABLE or species correction in firmware if systematic
   offset is found

---

## Logger integration

Follow the exact pattern used in exhaust.py and circulation.py.

- Accept `logger=None` in `__init__`
- Never import logger module directly
- Call `logger.event(source, message, level)` only when logger is not None
- Source string: `"moisture"`
- Log on: init, any None reading (WARNING), any out-of-range result (WARNING)
- Do NOT log every successful read (called frequently; would flood event log)

Example log calls:
```python
logger.event("moisture", "Moisture probe init -- ch1=maple ch2=beech")
logger.event("moisture", "Ch1 probe disconnected or open circuit", level="WARNING")
logger.event("moisture", "Ch2 resistance out of range: 8500000 ohm", level="WARNING")
```

---

## Data record keys

When the schedule controller calls `logger.data(record)`, it will include
moisture probe values. The keys this module produces must match:

| Key          | Type        | Description                          |
|--------------|-------------|--------------------------------------|
| `mc_ch1_pct` | float/None  | MC% channel 1 (temp-corrected)       |
| `mc_ch2_pct` | float/None  | MC% channel 2 (temp-corrected)       |
| `r_ch1_ohm`  | float/None  | Raw resistance channel 1             |
| `r_ch2_ohm`  | float/None  | Raw resistance channel 2             |

These will be added to the CSV header in logger.py when main.py is written.

---

## Unit tests

Include a `_run_tests()` function at the bottom of the module following the
exhaust.py pattern. Tests run on hardware with real probes connected.

### Test list

**Test 1 -- Init state**
- After construction, both excitation pins should be LOW
- Verify with `excite_pin.value() == 0` on both channels
- PASS/FAIL

**Test 2 -- Read resistance (probes connected)**
- Call `read_resistance()`
- Both ch1_ohms and ch2_ohms should be non-None floats > 0
- Values should be in the range 1kohm to 10Mohm (plausible for wood)
- PASS/FAIL

**Test 3 -- Read MC% (probes connected)**
- Call `read()`
- Both ch1_mc_pct and ch2_mc_pct should be non-None floats
- Values should be in range 6.0 to 30.0
- PASS/FAIL

**Test 4 -- Excitation pin returns LOW after read**
- After `read()` completes, both excitation pins should be LOW
- Verify with `excite_pin.value() == 0`
- PASS/FAIL

**Test 5 -- Open circuit (probes disconnected)**
- Unplug both probe headers
- Call `read()`
- Both mc values should be None (open circuit detected)
- PASS/FAIL

**Test 6 -- resistance_to_mc() module function**
- `resistance_to_mc(100_000, "maple")` should return approx 14.0 (14.5 - 0.5 correction)
- `resistance_to_mc(100_000, "beech")` should return approx 14.2 (14.5 - 0.3 correction)
- `resistance_to_mc(1_000, "maple")` should return 30.0 (clamped)
- `resistance_to_mc(9_000_000, "maple")` should return None (too dry)
- PASS/FAIL

**Test 7 -- Temperature correction**
- At 20degC: `read_with_temp_correction(20.0)` should match `read()` (no correction)
- At 60degC: corrected MC% should be lower than uncorrected by approx 2.4 points
- PASS/FAIL

**Test 8 -- Logger integration**
- Construct with a mock logger object that records calls
- Call `read()` with probes disconnected
- Verify logger.event() was called with level="WARNING"
- PASS/FAIL

---

## Module-level constants (summary)

```python
R_REF_OHM = 100_000          # Reference resistor value
ADC_MAX = 65535               # 16-bit ADC full scale
VCC = 3.3                     # Supply voltage
SETTLE_MS = 15                # Excitation settle time before ADC sample
SAMPLE_COUNT = 5              # ADC samples to average per reading
SAMPLE_INTERVAL_MS = 2        # Delay between samples
DISCHARGE_MS = 10             # Wait after excitation goes LOW
TEMP_REF_C = 20.0             # Reference temperature for correction
TEMP_CORRECTION_PER_DEG = 0.06  # MC% correction per degC
```

---

## What Claude Code should NOT change

- Pin assignments (GP12/GP13/GP26/GP27) -- hardware is fixed
- The AC excitation sequence (HIGH -> settle -> sample -> LOW -> discharge)
- The logger dependency injection pattern
- The ASCII-only string rule (no Unicode in any string, comment, or docstring)
- The module-level `resistance_to_mc()` function signature

---

## Files to create or modify

| File                  | Action   | Notes                                      |
|-----------------------|----------|--------------------------------------------|
| `lib/moisture.py`     | Create   | Full implementation per this spec          |
| `PROJECT.md`          | Update   | Add moisture.py to modules table and notes |

No other files need to change. Logger CSV header columns will be added when
`main.py` is written.