# SCHEDULE_CONTROLLER_SPEC.md

Spec for Claude Code implementation of `lib/schedule.py` -- the drying schedule
controller for the kiln firmware.

---

## Overview

`lib/schedule.py` is the top-level control logic for a wood-drying kiln. It
coordinates all existing hardware modules (heater, exhaust fan, vents, circulation
fans, sensors, moisture probes) to execute a multi-stage drying schedule loaded
from a JSON file on the SD card.

This module does NOT drive hardware directly -- it orchestrates the existing
`lib/` modules. It does NOT replace `main.py`; `main.py` will instantiate all
modules and pass them in.

---

## Files to create or modify

| File | Action | Notes |
|---|---|---|
| `lib/schedule.py` | Create | Full implementation per this spec |
| `schedules/maple_05in.json` | Create | FPL-based schedule for 0.5" hard maple |
| `schedules/maple_1in.json` | Create | FPL-based schedule for 1" hard maple |
| `schedules/beech_05in.json` | Create | FPL-based schedule for 0.5" beech |
| `schedules/beech_1in.json` | Create | FPL-based schedule for 1" beech |
| `PROJECT.md` | Update | Add schedule.py to modules table and notes |

---

## Schedule JSON format

Schedules are stored as JSON files in a `schedules/` directory at the SD card
root. The controller loads the file at run start; editing the file on the SD card
changes behaviour without reflashing.

```json
{
  "name": "Hard Maple 1 inch",
  "species": "maple",
  "thickness_in": 1.0,
  "stages": [
    {
      "name": "Stage 1 - Initial warm-up",
      "stage_type": "drying",
      "target_temp_c": 40,
      "target_rh_pct": 85,
      "target_mc_pct": 35.0,
      "min_duration_h": 12,
      "max_duration_h": 48
    },
    {
      "name": "Equalizing",
      "stage_type": "equalizing",
      "target_temp_c": 60,
      "target_rh_pct": 55,
      "target_mc_pct": null,
      "min_duration_h": 24,
      "max_duration_h": null
    },
    {
      "name": "Conditioning",
      "stage_type": "conditioning",
      "target_temp_c": 60,
      "target_rh_pct": 70,
      "target_mc_pct": null,
      "min_duration_h": 8,
      "max_duration_h": null
    }
  ]
}
```

### JSON field definitions

- `stage_type`: One of `"drying"`, `"equalizing"`, or `"conditioning"`. Controls
  advance logic and alert behaviour. See Stage Types section below.
- `target_temp_c`: Desired lumber-zone temperature in degrees C.
- `target_rh_pct`: Desired lumber-zone relative humidity in percent.
- `target_mc_pct`: Wood MC% threshold for stage advance. Required for `"drying"`
  stages. Must be `null` for `"equalizing"` and `"conditioning"` stages (advance
  is time-only).
- `min_duration_h`: Minimum hours in stage before advance conditions are checked.
- `max_duration_h`: Maximum hours allowed in stage. If elapsed and advance
  conditions not yet met, send a `"stage_goal_not_met"` LoRa alert and advance
  anyway. Use `null` to disable (recommended for equalizing and conditioning).

### Stage types

**`"drying"`** -- normal MC-based advance.
- Advance requires BOTH: `stage_elapsed_h >= min_duration_h` AND both moisture
  probe readings at or below `target_mc_pct`.
- If either probe reads `None`, fall back to time-only advance after
  `min_duration_h` is met (log a warning that MC check was skipped).
- `max_duration_h` enforces a timeout: advance with `"stage_goal_not_met"` alert
  if MC threshold not reached in time.

**`"equalizing"`** -- time-only advance; no MC check.
- Purpose: bring wettest boards down to target MC without over-drying dry ones.
- RH is set slightly higher than the final drying stage (see schedule tables).
- Advance after `min_duration_h` elapsed.
- `max_duration_h` should be `null` -- operator decides when to advance via the
  REST API if needed.
- On entry: send a `"equalizing_start"` LoRa alert. This is the operator's
  reminder to add water pans to the kiln enclosure if additional humidity is
  needed to reach the target RH (vent-only RH control cannot raise RH above
  ambient; water pans are the manual supplement).

**`"conditioning"`** -- time-only advance; no MC check.
- Purpose: relieve residual drying stresses (casehardening). Essential for
  furniture and precision applications.
- RH is set significantly higher than the final drying stage -- typically 10-15%
  above. This stage is very likely to require water pans to reach target RH in a
  vent-only system.
- Advance after `min_duration_h` elapsed.
- `max_duration_h` should be `null`.
- On entry: send a `"conditioning_start"` LoRa alert with water pan reminder.

**Note on RH control for equalizing and conditioning:** The kiln uses vent-only
RH control. Venting lowers RH; restricting vents allows RH to rise passively
from wood moisture. For equalizing and conditioning stages, the higher target RH
may require the operator to manually add water pans. The LoRa alerts at stage
entry serve as the reminder to do so. The controller attempts to hit the target
RH with vents regardless; if it cannot reach the target, the normal
`"rh_out_of_range"` alert mechanism applies after the standard timeout.

---

## Class: `KilnSchedule`

```python
class KilnSchedule:
    def __init__(self, sdcard, sensors, moisture, heater, exhaust,
                 circulation, vents, lora, logger=None):
```

### Constructor arguments

| Arg | Type | Notes |
|---|---|---|
| `sdcard` | `SDCard` instance | For loading JSON |
| `sensors` | `SHT31Sensors` instance | Temperature and RH source |
| `moisture` | `MoistureProbe` instance | MC% source |
| `heater` | `Heater` instance | SSR control |
| `exhaust` | `ExhaustFan` instance | Venting fan |
| `circulation` | `CirculationFans` instance | Always-on internal airflow |
| `vents` | `Vents` instance | Servo-driven dampers |
| `lora` | LoRa instance | Alert transmitter (mock class available for testing) |
| `logger` | `Logger` instance or `None` | Event and data logging |

All arguments are required except `logger`. Raise `ValueError` if any required
argument (including `lora`) is `None`.

### Constants (module-level, not class attributes)

```python
TEMP_DEADBAND_C        = 2.0   # +/- degrees C
RH_DEADBAND_PCT        = 8.0   # +/- percent RH
LOOP_INTERVAL_S        = 120   # seconds between ticks (normal)
VENT_LOOP_INTERVAL_S   = 30    # seconds between ticks while venting
OUT_OF_RANGE_ALERT_MIN = 30    # minutes before out-of-range alert fires
HEATER_FAULT_RISE_C    = 2.0   # minimum temp rise to confirm heater working
HEATER_FAULT_MIN       = 20    # minutes on with no rise before fault alert
COOLDOWN_FAN_SPEED     = 50    # circulation fan speed pct during cooldown
CIRC_FAN_SPEED         = 75    # circulation fan speed pct during a run
EXHAUST_VENT_SPEED     = 80    # exhaust fan speed pct for RH venting
EXHAUST_OVERHEAT_SPEED = 60    # exhaust fan speed pct for overheat venting
```

---

## Public API

### `load(schedule_path)`

Load a schedule JSON from the SD card. `schedule_path` is relative to the SD
mount point (e.g. `"schedules/maple_1in.json"`). Returns `True` on success,
`False` on any failure (file missing, JSON parse error, missing required fields).
Logs the error. Does not raise.

Validation: every stage must have `name`, `stage_type`, `target_temp_c`,
`target_rh_pct`, and `min_duration_h`. `"drying"` stages must have a numeric
`target_mc_pct`. `"equalizing"` and `"conditioning"` stages must have
`target_mc_pct` as `null`. Unknown `stage_type` values cause `load()` to
return `False`.

### `start()`

Begin executing the loaded schedule from stage 0. Raises `RuntimeError` if no
schedule is loaded. Records the run start time. Starts circulation fans at
`CIRC_FAN_SPEED`. Closes vents. Logs a run-start event. Calls
`logger.begin_run()` if logger provided.

### `stop(reason="manual")`

Halt the run immediately. Heater off. Vents open. Circulation fans remain on at
`COOLDOWN_FAN_SPEED`. Exhaust fan off. Logs the stop reason. Calls
`logger.end_run()` if logger provided. Sets internal state to stopped.

### `tick()`

Called from `main.py` main loop. Returns immediately if no run is active.

Performs one control cycle:

1. Read sensors (`sensors.read()`). If `None`, send `"sensor_failure"` LoRa
   alert and return without actuating anything. Log the failure. Do not count
   this tick against stage timers.

2. Read moisture (`moisture.read_with_temp_correction(temp_c)`), using lumber-
   zone temperature from step 1. `None` moisture is non-fatal -- log a warning
   but continue.

3. Evaluate temperature control (see Temperature Control section).

4. Evaluate RH and overheat vent control (see RH and Temperature Control
   section).

5. Check stage advance conditions (see Stage Advance section).

6. Check alert conditions (see Alert section).

7. Log a data record (see Logging section).

8. Update the next-tick timestamp.

### `tick_interval_s` property

Returns `VENT_LOOP_INTERVAL_S` if vents are currently open, else
`LOOP_INTERVAL_S`. `main.py` uses this to set the sleep interval between
`tick()` calls.

### `status()` -> dict

Returns a snapshot of current controller state for the REST API and display:

```python
{
    "running":          bool,
    "schedule_name":    str or None,
    "stage_index":      int,
    "stage_name":       str,
    "stage_type":       str,
    "stage_elapsed_h":  float,
    "target_temp_c":    float,
    "target_rh_pct":    float,
    "target_mc_pct":    float or None,
    "actual_temp_c":    float or None,
    "actual_rh_pct":    float or None,
    "actual_mc_maple":  float or None,
    "actual_mc_beech":  float or None,
    "heater_on":        bool,
    "vents_open":       bool,
    "vent_reason":      str or None,
    "cooldown":         bool
}
```

---

## Temperature control

Within `tick()`, after reading sensors:

- If `actual_temp_c < target_temp_c - TEMP_DEADBAND_C` and heater is off:
  turn heater on.
- If `actual_temp_c > target_temp_c + TEMP_DEADBAND_C` and heater is on:
  turn heater off.

Heater fault detection: track `_heater_on_since` and `_heater_on_temp` when
heater turns on. If heater has been on for `HEATER_FAULT_MIN` minutes and temp
has risen less than `HEATER_FAULT_RISE_C` from `_heater_on_temp`, send a
`"heater_fault"` alert. Alert once per heater-on cycle (`_heater_fault_alerted`
flag, reset when heater turns on).

---

## RH and temperature control (vent-only)

Within `tick()`, after reading sensors. Use lumber-zone RH
(`sensors.read()["rh_lumber"]`).

Venting is triggered by either of two independent conditions, tracked via
`_vent_reason` (`"rh_high"`, `"temp_high"`, or `None`).

### RH too high

- Open condition: `actual_rh_pct > target_rh_pct + RH_DEADBAND_PCT` and
  `_vent_reason is None`. Open vents, exhaust fan on at `EXHAUST_VENT_SPEED`.
  Set `_vent_reason = "rh_high"`.
- Close condition: `actual_rh_pct < target_rh_pct - RH_DEADBAND_PCT / 2` and
  `_vent_reason == "rh_high"`. Close vents, exhaust fan off. Clear
  `_vent_reason`.
- Cold suppression: do NOT open vents for RH if
  `actual_temp_c < target_temp_c - TEMP_DEADBAND_C * 2`. Log suppression.
  This suppression does NOT apply to overheat venting.

### Temperature too high (passive solar overheat)

- Open condition: `actual_temp_c > target_temp_c + TEMP_DEADBAND_C`. Open
  vents, exhaust fan on at `EXHAUST_OVERHEAT_SPEED`. Turn heater off. Set
  `_vent_reason = "temp_high"`.
- Close condition: `actual_temp_c < target_temp_c + TEMP_DEADBAND_C / 2` and
  `_vent_reason == "temp_high"`. Close vents, exhaust fan off. Clear
  `_vent_reason`. Resume normal heater control.

### Priority

If both conditions are true simultaneously, `"temp_high"` takes priority.
If `_vent_reason == "rh_high"` and overheat condition activates: upgrade to
`"temp_high"` (increase fan speed to `EXHAUST_OVERHEAT_SPEED`, turn heater
off, update `_vent_reason`).

---

## Stage advance

### Drying stages

Advance requires BOTH:
1. `stage_elapsed_h >= stage.min_duration_h`
2. Both moisture probe readings <= `stage.target_mc_pct` (or both are `None`,
   in which case fall back to time-only after `min_duration_h`; log warning).

If `stage.max_duration_h` is not `null` and `stage_elapsed_h >=
stage.max_duration_h` and MC threshold not met: log warning, send
`"stage_goal_not_met"` alert, advance anyway.

### Equalizing and conditioning stages

Advance after `min_duration_h` elapsed. No MC check. No `max_duration_h`
timeout (field is `null` in JSON for these stages).

On entry to an equalizing stage: send `"equalizing_start"` LoRa alert. Payload
includes reminder text: `"add water pans if needed for target RH"`.

On entry to a conditioning stage: send `"conditioning_start"` LoRa alert.
Payload includes same reminder text.

### On any stage advance

Log the advance. Send `"stage_advance"` LoRa alert (in addition to any
stage-type-specific alerts above). Update `_stage_index` and `_stage_start_s`.

### Run complete

When the final stage completes: send `"run_complete"` LoRa alert. Call
`stop(reason="complete")`. Cooldown: heater off, vents open, circulation fans
at `COOLDOWN_FAN_SPEED`, exhaust fan off.

---

## Alert conditions and LoRa payloads

All alerts are sent via both `lora.send(payload_str)` AND
`logger.event("schedule", ...)` -- both always happen. The mock LoRa class
is available for testing without hardware.

Do not re-send the same alert type more than once per `OUT_OF_RANGE_ALERT_MIN`
minutes. Use `_last_alert_ts` dict keyed by alert type to enforce this. One-shot
alerts (stage advance, run complete, sensor failure on a given tick) bypass rate
limiting.

| Condition | Alert type string | Triggered when |
|---|---|---|
| Stage advance | `"stage_advance"` | Any stage advances |
| Stage goal not met | `"stage_goal_not_met"` | max_duration_h exceeded before MC% met |
| Equalizing entry | `"equalizing_start"` | Entering an equalizing stage |
| Conditioning entry | `"conditioning_start"` | Entering a conditioning stage |
| Run complete | `"run_complete"` | Final stage done, cooldown entered |
| Temp out of range | `"temp_out_of_range"` | Temp outside deadband for >30 min |
| RH out of range | `"rh_out_of_range"` | RH outside deadband for >30 min |
| Sensor failure | `"sensor_failure"` | sensors.read() returns None |
| Heater fault | `"heater_fault"` | Heater on >20 min, no temp rise |

LoRa payload format -- plain ASCII string, semicolon-delimited:

```
ALERT;{alert_type};stage={stage_index};temp={actual_temp_c:.1f};rh={actual_rh_pct:.1f}
```

For equalizing_start and conditioning_start, append the water pan reminder:

```
ALERT;equalizing_start;stage={stage_index};temp={t:.1f};rh={r:.1f};add water pans if needed
```

For sensor_failure, temp and rh fields are omitted:

```
ALERT;sensor_failure;stage={stage_index}
```

---

## Logging

On each successful `tick()`, call `logger.data(record)` with:

```python
{
    "stage":        stage_index,
    "stage_type":   str,
    "temp_lumber":  float or None,
    "rh_lumber":    float or None,
    "temp_intake":  float or None,
    "rh_intake":    float or None,
    "mc_maple":     float or None,
    "mc_beech":     float or None,
    "heater":       bool,
    "vents":        bool,
    "vent_reason":  str or None,
    "exhaust_pct":  int,
    "target_temp":  float,
    "target_rh":    float,
    "target_mc":    float or None,
    "stage_h":      float
}
```

Use `logger.event("schedule", message)` for state-change events (stage advance,
heater on/off, vent open/close, run start, run stop, alerts).

---

## Schedule JSON files

The four JSON files must use schedules based on FPL kiln-drying data for hard
maple and beech. Primary references:

- FPL-GTR-118 "Drying Hardwood Lumber", Chapter 7, p.72 (basic hardwood
  schedules). Beech is classified as hard-to-dry; hard maple as moderately
  hard-to-dry.
- FPL-GTR-57 "Dry Kiln Schedules for Commercial Woods", Section VII tables:
  hard maple uses schedule T3-C2 (4/4) in the index; beech (American) uses
  schedule T4-C3 (4/4).

GTR-57 Appendix A governs equalizing and conditioning parameters. The equalizing
target RH is set to the EMC corresponding to the desired final MC plus ~2-3%.
The conditioning target RH is set ~10-15% higher than the final drying stage.

### Conversion note

FPL schedules are expressed in Fahrenheit dry-bulb and wet-bulb pairs. Convert
to Celsius (TC = (TF - 32) / 1.8) and derive RH from the wet-bulb depression
using standard psychrometric tables.

### Guidance values

These are approximate values consistent with FPL T3-C2 (hard maple) and T4-C3
(beech) at conventional temperatures. Claude Code should cross-check against the
actual GTR-57 tables and adjust if they differ.

#### Hard maple 0.5 inch (`schedules/maple_05in.json`)

0.5" stock dries faster and can step more aggressively than 1" stock.

| Stage | Type | Temp (C) | RH% | Target MC% | Min hrs | Max hrs |
|-------|------|----------|-----|------------|---------|---------|
| 1 - Initial | drying | 38 | 85 | 35 | 8 | 36 |
| 2 | drying | 43 | 78 | 30 | 8 | 36 |
| 3 | drying | 49 | 70 | 25 | 12 | 48 |
| 4 | drying | 54 | 62 | 20 | 12 | 48 |
| 5 | drying | 57 | 54 | 15 | 12 | 48 |
| 6 | drying | 60 | 45 | 10 | 12 | 48 |
| 7 - Final dry | drying | 63 | 38 | 7 | 8 | 36 |
| 8 - Equalizing | equalizing | 63 | 48 | null | 12 | null |
| 9 - Conditioning | conditioning | 63 | 68 | null | 6 | null |

#### Hard maple 1 inch (`schedules/maple_1in.json`)

| Stage | Type | Temp (C) | RH% | Target MC% | Min hrs | Max hrs |
|-------|------|----------|-----|------------|---------|---------|
| 1 - Initial | drying | 38 | 85 | 35 | 12 | 72 |
| 2 | drying | 43 | 78 | 30 | 12 | 72 |
| 3 | drying | 49 | 70 | 25 | 24 | 96 |
| 4 | drying | 54 | 62 | 20 | 24 | 96 |
| 5 | drying | 57 | 54 | 15 | 24 | 96 |
| 6 | drying | 60 | 45 | 10 | 24 | 96 |
| 7 - Final dry | drying | 63 | 38 | 7 | 16 | 72 |
| 8 - Equalizing | equalizing | 63 | 48 | null | 24 | null |
| 9 - Conditioning | conditioning | 63 | 68 | null | 8 | null |

#### Beech 0.5 inch (`schedules/beech_05in.json`)

Beech is harder to dry than maple -- more checking risk above 30% MC.
Initial stages use higher RH than maple equivalent.

| Stage | Type | Temp (C) | RH% | Target MC% | Min hrs | Max hrs |
|-------|------|----------|-----|------------|---------|---------|
| 1 - Initial | drying | 35 | 88 | 35 | 12 | 48 |
| 2 | drying | 40 | 82 | 30 | 12 | 48 |
| 3 | drying | 46 | 74 | 25 | 12 | 48 |
| 4 | drying | 52 | 65 | 20 | 12 | 48 |
| 5 | drying | 57 | 56 | 15 | 12 | 48 |
| 6 | drying | 60 | 46 | 10 | 12 | 48 |
| 7 - Final dry | drying | 63 | 38 | 7 | 8 | 36 |
| 8 - Equalizing | equalizing | 63 | 50 | null | 12 | null |
| 9 - Conditioning | conditioning | 63 | 70 | null | 8 | null |

#### Beech 1 inch (`schedules/beech_1in.json`)

| Stage | Type | Temp (C) | RH% | Target MC% | Min hrs | Max hrs |
|-------|------|----------|-----|------------|---------|---------|
| 1 - Initial | drying | 35 | 88 | 35 | 24 | 96 |
| 2 | drying | 40 | 82 | 30 | 24 | 96 |
| 3 | drying | 46 | 74 | 25 | 24 | 96 |
| 4 | drying | 52 | 65 | 20 | 24 | 96 |
| 5 | drying | 57 | 56 | 15 | 24 | 96 |
| 6 | drying | 60 | 46 | 10 | 24 | 96 |
| 7 - Final dry | drying | 63 | 38 | 7 | 16 | 72 |
| 8 - Equalizing | equalizing | 63 | 50 | null | 24 | null |
| 9 - Conditioning | conditioning | 63 | 70 | null | 8 | null |

---

## Internal state (private attributes)

| Attribute | Purpose |
|---|---|
| `_schedule` | Parsed JSON dict or None |
| `_stage_index` | Current stage (int) |
| `_stage_start_s` | utime.ticks_ms() when stage began |
| `_run_start_s` | utime.ticks_ms() when run began |
| `_running` | bool |
| `_cooldown` | bool |
| `_vent_reason` | str or None: "rh_high", "temp_high", or None |
| `_last_sensor_read` | last successful sensor dict |
| `_heater_on_since` | timestamp when heater last turned on, or None |
| `_heater_on_temp` | temp reading at heater-on time |
| `_heater_fault_alerted` | bool, reset on heater turn-on |
| `_temp_oor_since` | timestamp when temp first went out of range, or None |
| `_rh_oor_since` | timestamp when RH first went out of range, or None |
| `_last_alert_ts` | dict keyed by alert type, for rate-limiting |

---

## Constraints and patterns

- **ASCII only.** No Unicode in any string, comment, or docstring. Use `deg`,
  `pct`, `-`, `->` as substitutes.
- **Follow `lib/exhaust.py` module pattern.** Class-based, `logger=None`
  dependency injection.
- **No blocking delays inside `tick()`.** All timing is timestamp-based using
  `utime.ticks_ms()` and `utime.ticks_diff()`.
- **Silent fail on SD.** If `sdcard.read_text()` returns None, log and return
  False from `load()`. Never raise from `load()`.
- **utime for elapsed time.** Do not use wall-clock time for elapsed
  calculations (RTC may not be set at boot).
- **No third-party libraries.** Standard MicroPython only plus existing `lib/`
  modules.

---

## Unit tests

Include hardware-in-the-loop unit tests at the bottom of the file under
`if __name__ == "__main__"`:

1. `load()` with a valid JSON file -- returns True, stage count and fields correct
2. `load()` with a missing file -- returns False, no exception
3. `load()` with malformed JSON -- returns False, no exception
4. `load()` with a drying stage missing `target_mc_pct` -- returns False
5. `load()` with an equalizing stage that has a non-null `target_mc_pct` --
   returns False
6. `start()` without a loaded schedule -- raises RuntimeError
7. `status()` returns expected keys and `stage_type` field before and after start
8. `stop()` leaves heater off, vents open, circulation running at cooldown speed
9. Overheat vent: manually confirm vents open and heater off when temp exceeds
   target + deadband (requires heater and sensor hardware)
10. Stage advance: confirm advance triggers correctly for a drying stage (requires
    probe hardware or manual probe disconnect for MC fallback test)
11. Equalizing entry: confirm LoRa alert fires with water pan reminder text