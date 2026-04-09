# Error Checking and Fault Surfacing Spec

Author: Steve, drafted via Claude Code session 2026-04-08
Status: Draft - intended as input for a future Claude Code session that will
implement the changes outlined here.

---

## Why this spec exists

The kiln firmware modules in `lib/` already detect a number of faults at
runtime - I2C errors, current out of range, sensor CRC failures, fan stalls,
LoRa init failures, SD write failures, etc. Each module logs these to the SD
event log and (in some cases) to the REPL. **But there is no consistent path
from "module detected a fault" to "operator sees the fault on the Kivy app."**

The motivating example: during a recent test the circulation fans were
loose, drew only 35 mA instead of the expected 200-500 mA range, and
`circulation.verify_running()` correctly logged an ERROR. The Kivy dashboard
showed nothing - no banner, no badge, nothing. The operator only noticed
because the fans weren't spinning. That is exactly the failure mode the fault
banner is supposed to catch.

This spec defines:

1. A consistent **fault model** every `lib/` module exposes (the "fault
   contract").
2. A central **fault aggregator** that polls every module each control-loop
   tick and produces a single list of active faults.
3. The **transport** that surfaces those faults to the Kivy app via the
   `/status` REST endpoint.
4. A **mapping rule** ("anything that causes a unit test to fail must also
   be checked at runtime") and the asymmetries we need to fix to satisfy it.
5. A migration order so the work can be done module-by-module without
   breaking the existing firmware.

---

## Background: today's behaviour

| Module | Fault detection at runtime | Surfaced via | Polled by main.py? |
|---|---|---|---|
| `circulation.py` | `verify_running()` checks 12V current after `on()` | Logs ERROR; returns True/False/None | No - result discarded |
| `exhaust.py` | None at runtime (test() checks RPM, production does not) | n/a | No |
| `vents.py` | `verify_position()` checks 5V current at mid-travel | Logs WARN; returns True/False/None | No - schedule.py never calls it |
| `heater.py` | None | n/a | n/a |
| `SHT31sensors.py` | I2C / CRC failures, returns None | Logs WARNING per failure | Detected indirectly via None reads in schedule cache |
| `moisture.py` | Probe disconnect / out of range, returns None | Logs WARNING per failure | Same |
| `current.py` | I2C read failure, init failure, `check_range()` | Logs ERROR / WARN; `_ready` flag (private) | No (only init-time check) |
| `lora.py` | Init failure, TX timeout, payload errors | Logs ERROR / WARNING; `is_initialised` property | At init only |
| `sdcard.py` | Mount failure, listdir/read failure | Logs WARNING; `is_mounted()` property | Init only |
| `logger.py` | SD mount failure, file open / write failure | Logs WARNING; silently drops writes after first failure | No |
| `schedule.py` | Heater timeout, temp/RH out-of-range, sensor read failure | Logs + sends LoRa alert; populates `_last_alert_ts` | **Yes - this is the only path that ends up in `active_alerts`** |
| `display.py` | UART timeout (no "OK") | Returns empty string; no logging | No |

`main.py`'s `_update_status_cache()` builds the `/status` payload's
`active_alerts` field **only** from `schedule._last_alert_ts`. Faults in
every other module are completely invisible to the Kivy app, even though they
are written to the SD event log.

The Kivy dashboard's fault banner is wired to `active_alerts`, so the gap is:

> **Detected but not propagated** - the firmware sees the fault, writes it
> to the SD card, and then forgets about it.

---

## The rule we want to satisfy

> **Anything that causes a unit test to fail should also be checked at
> runtime, and any fault detected at runtime must be visible in the
> `/status` `active_alerts` array within one control-loop tick.**

This is a two-part rule and both halves matter:

- **Test/production parity** - if `test()` asserts that
  `circulation.verify_running()` returns True, then production must call
  `verify_running()` and react when it returns False. Today the production
  call discards the return value.
- **Operator visibility** - faults must end up in `active_alerts` so the
  Kivy fault banner lights up. The operator should never have to SSH into
  the Pico and grep the SD log to find out something is wrong.

---

## The fault contract

Every hardware module in `lib/` (circulation, exhaust, vents, heater,
sensors, moisture, current, lora, sdcard, logger, display) gains the same
small set of fault-related members. Modules are still free to implement
their own internal checks; this contract just provides a consistent way for
`main.py` to ask "are you broken right now?"

### Required public surface

```python
class SomeModule:
    # Boolean: is there an unresolved fault on this module right now?
    # Set by the module when a check fails; cleared when a successful
    # check brings it back into spec.
    fault: bool

    # Short stable code suitable for use in active_alerts (UPPER_SNAKE).
    # Examples: "CIRC_FAN_FAULT", "SENSOR_LUMBER_FAIL", "SD_WRITE_FAIL".
    # None when fault is False.
    fault_code: Optional[str]

    # Short human-readable message; surfaces in the Kivy fault banner
    # tooltip and logs. None when fault is False.
    fault_message: Optional[str]

    # Optional: the last time the module performed its self-check, so
    # the aggregator can detect a stuck module that hasn't been polled.
    # time.ticks_ms() value, or None if never checked.
    fault_last_checked_ms: Optional[int]

    def check_health(self) -> bool:
        """Run any cheap self-checks the module supports and update
        fault / fault_code / fault_message / fault_last_checked_ms.
        Returns the new value of `fault` (True == something is wrong).

        Modules may also update fault state from inside their normal
        operations - e.g. circulation.on() should set fault = True
        immediately if verify_running() fails. check_health() is for
        the periodic 'is everything still OK' poll between commands.
        """
```

### Latching vs. clearing

- A fault is **latched** as soon as it is observed.
- It is **cleared** when:
  - A successful subsequent check brings the value back into spec, AND
  - The module decides the underlying cause is plausibly resolved (e.g.
    `circulation.off()` clears `CIRC_FAN_FAULT` because we're no longer
    expecting current draw).
- Modules MUST NOT auto-clear faults on a single successful read if the
  underlying mechanism is flaky - require N consecutive good reads before
  clearing. (Recommendation: N = 3.)

### Multiple faults per module

Most modules will only ever have one active fault at a time. For modules
that legitimately have multiple independent failure modes (e.g. SHT31 has
two sensors at different addresses), use one of:

- Two boolean fields: `fault_lumber`, `fault_intake` and a derived
  `fault = fault_lumber or fault_intake`. `fault_code` returns whichever
  is active (or a combined code if both).
- A single fault but with `fault_code` distinguishing them
  (`SENSOR_LUMBER_FAIL` vs `SENSOR_INTAKE_FAIL`).

The choice is per-module. Be consistent within a module.

---

## The aggregator

`main.py` (or a new `lib/health.py` module) implements the central
aggregator. Sketch:

```python
# In _update_status_cache(), after the existing schedule alert collection:

faults = []  # list of (code, source, message)

for source, mod in [
    ("circulation", circulation),
    ("exhaust", exhaust),
    ("vents", vents),
    ("heater", heater),
    ("sensors", sensors),
    ("moisture", moisture),
    ("current_12v", monitor_12v),
    ("current_5v", monitor_5v),
    ("lora", lora),
    ("sdcard", sdcard),
    ("logger", logger),
    ("display", display),
]:
    if mod is None:
        continue
    try:
        # Cheap re-check. Do NOT do anything expensive here - this runs
        # every status cache update.
        mod.check_health()
    except Exception as e:
        faults.append(("MODULE_CHECK_FAILED", source, str(e)))
        continue
    if getattr(mod, "fault", False):
        faults.append((
            mod.fault_code or "UNKNOWN_FAULT",
            source,
            mod.fault_message or "",
        ))

# Existing schedule alerts continue to flow in via _last_alert_ts.
for atype in active_alerts:
    faults.append((atype.upper(), "schedule", ""))

# Deduplicate by code (highest source priority wins) and serialise.
_status_cache["active_alerts"] = [code for code, _src, _msg in faults]
_status_cache["fault_details"] = [
    {"code": code, "source": src, "message": msg}
    for code, src, msg in faults
]
```

### Two new `/status` fields

- `active_alerts: list[str]` - already exists, change semantics so it
  contains every fault from every source, not just schedule alerts.
  Backwards-compatible with the Kivy dashboard's existing fault banner.
- `fault_details: list[{code, source, message}]` - new. Powers the future
  Alerts screen (Phase 5) with rich per-fault info.

Update `Specs/lora_telemetry_spec.md` and the Pi4 daemon to mirror these
field changes.

---

## Test/production parity gaps to close

These are the places where the unit test asserts something the production
code does not. The aggregator above only works once these are fixed.

| Module | Test asserts | Production does | Fix |
|---|---|---|---|
| `circulation.py` | `verify_running()` returns True after `on()` | Calls it but discards result | Latch `fault = True` inside `verify_running()` when current is out of range |
| `exhaust.py` | RPM > 0 within tach sample window | Reads RPM only on demand from main.py for telemetry | Add `verify_running()` similar to circulation, latch `fault` if RPM == 0 after spin-up |
| `vents.py` | `verify_position()` returns True after each `_move()` | `_move()` samples current but never calls verify_position from production | Have `_move()` call verify_position() itself and latch `fault` + `fault_code = "VENT_STALL"` |
| `SHT31sensors.py` | Both sensors return non-None | Returns None silently | Track per-sensor fail count; latch `fault` after 3 consecutive failures |
| `moisture.py` | Both channels return numeric MC | Returns None silently | Same: 3-consecutive-failure latch |
| `current.py` | `_ready` is True | Init failure logged but not exposed | Promote `_ready` to public `fault` property |
| `lora.py` | `is_initialised` is True after init | Already exposed but not aggregated | Add `fault` alias + aggregator entry |
| `sdcard.py` | `is_mounted()` is True | Already exposed but not aggregated | Add `fault` alias + aggregator entry |
| `logger.py` | Each `event()` and `data()` call writes | After first failure, writes silently drop | Track `_write_failures`; latch `fault` after 3 consecutive failures |
| `display.py` | Each command returns "OK" | UART timeouts return empty string, never escalate | Track consecutive timeouts; latch `fault` after N |
| `schedule.py` | n/a (no unit test) | Already populates `_last_alert_ts` | Continue to flow into the aggregator unchanged |

`heater.py` has no failure modes the firmware can detect; safety lives in
the RY85 thermal fuse. It still implements the contract (always
`fault = False`) so the aggregator's loop is uniform.

---

## Migration plan

Done in this order so each step is independently testable on the bench:

1. **Define the contract.** Add a tiny `lib/fault.py` (or just put the
   four properties + `check_health()` stub directly in each module) so
   the contract is real Python and not just docs.

2. **Migrate one module end-to-end as a reference.** Recommended:
   `circulation.py`. It has the clearest existing check (`verify_running`),
   the example failure that prompted this spec, and a small surface area.
   Update its tests too. After this step:
   - `circulation.fault` becomes True when current is out of range.
   - `_update_status_cache()` polls it.
   - `active_alerts` contains `"CIRC_FAN_FAULT"`.
   - The Kivy fault banner lights up. Verify on the bench.

3. **Migrate the easy modules.** `current.py`, `sdcard.py`, `lora.py`,
   `heater.py` - each is small and most already have the underlying
   detection in place.

4. **Migrate the sensor / moisture modules** with the consecutive-failure
   counter. Be careful not to flap on transient I2C glitches.

5. **Migrate `vents.py` and `exhaust.py`** which need new runtime checks
   that did not exist before (only test() did them).

6. **Migrate `logger.py` and `display.py`** which need write/UART failure
   counters.

7. **Update the aggregator in `main.py`** to walk all modules.

8. **Update `Specs/lora_telemetry_spec.md`** to add `fault_details` to
   the LoRa telemetry packet so cottage-mode users see the same info.

9. **Update the Kivy app**:
   - Phase 5 Alerts screen will consume `fault_details`.
   - Dashboard's existing `FaultBanner` already consumes `active_alerts`
     so no change needed there.

Each step can be merged independently. After steps 1-2 the system already
catches the original circulation-fan-fault scenario.

---

## Out of scope for this spec

- **Fault history.** Faults that have come and gone. The SD event log
  already records these; the Alerts screen will fetch them from the
  log on demand.
- **Recovery / retry.** Some faults benefit from a retry policy
  (SD mount in particular). That belongs in a separate "self-healing"
  spec, not this one.
- **Push notifications.** The Pi4 daemon pushes to ntfy.sh. Once
  `fault_details` is in the LoRa packet the daemon already has what it
  needs.
- **Severity grading.** This spec treats all faults as equally important.
  A future revision should add `severity = INFO | WARNING | ERROR` so the
  Kivy dashboard can choose between an amber pill and a red banner.

---

## Acceptance criteria

A future Claude Code session has finished this work when:

1. Every module in `lib/` exposes the four required properties and a
   `check_health()` method.
2. `main.py`'s `_update_status_cache()` calls `check_health()` on every
   module each tick.
3. Disconnecting the circulation fan power harness mid-run causes
   `active_alerts` to contain `"CIRC_FAN_FAULT"` within one tick, and the
   Kivy dashboard shows a red fault banner.
4. Disconnecting one of the SHT31 sensors causes
   `active_alerts` to contain the appropriate sensor code within 3 ticks.
5. Pulling the SD card mid-run causes `"SD_WRITE_FAIL"` to surface within
   3 ticks (does not require remount).
6. Forcing one of the unit tests to fail causes the equivalent runtime
   path to also fail and produce a fault. (Test the contract by patching
   the test to inject a known-bad reading.)
7. `test_modules.py` continues to pass for every module.
8. `main.py` integration test (run with no faults) shows
   `active_alerts == []`.

When all eight pass, the gap between "fault detected" and "operator sees
the fault" is closed.
