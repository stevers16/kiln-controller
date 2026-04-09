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

## Three-tier alert model

Not every interesting event is a "fault." A drying stage that ran out of
time without hitting its MC target is a real concern that the operator must
see, but no hardware is broken - the kiln is healthy and the *batch* may
not be. Treating that the same way as "the circulation fans aren't
spinning" trains the operator to ignore the red banner.

Three tiers, each with its own UI treatment:

| Tier | Examples | What it means | Dashboard treatment |
|---|---|---|---|
| **FAULT** | `CIRC_FAN_FAULT`, `SENSOR_LUMBER_FAIL`, `SD_WRITE_FAIL`, `HEATER_TIMEOUT`, `LORA_FAIL` | Hardware/firmware misbehaving. Operator may need to inspect or fix the kiln. | Red banner. Always elevated. |
| **NOTICE** | `STAGE_GOAL_NOT_MET`, `WATER_PAN_REMINDER` | Procedural / batch issue. The kiln is healthy; the schedule needs the operator's attention. | Amber banner. Elevated but distinct from a hardware fault. |
| **INFO** | `stage_advance`, `equalizing_start`, `conditioning_start`, `run_complete` | Lifecycle log entry. Useful in the audit trail; should never interrupt the operator. | No banner on the dashboard. Visible on the Alerts screen only. |

### Today's gap

The firmware does not currently distinguish these tiers. Every code that
flows through `schedule._last_alert_ts` is treated identically, and
`/status` returns one undifferentiated `active_alerts: list[str]` that
includes informational lifecycle events alongside genuine faults.

The Kivy app currently works around this with a hardcoded classification
table in `kilnapp/alerts.py` that maps each known code to a tier. This is
a stopgap. The Kivy table will drift out of sync with the firmware as new
alert codes are added.

### Firmware tagging requirement

Every alert / fault code emitted by the firmware MUST declare its tier at
the source. Two acceptable shapes:

**Shape A: severity field on each alert.** `_status_cache["fault_details"]`
becomes a list of `{code, source, message, tier}` dicts where `tier` is
one of `"fault"`, `"notice"`, `"info"`. The Kivy app classifies by reading
`tier` directly. `active_alerts` continues as a flat list of codes for
backwards compatibility but is augmented by `fault_details`.

**Shape B: per-tier arrays.** `/status` returns three explicit arrays:
`faults: list[str]`, `notices: list[str]`, `infos: list[str]`. Simple to
consume but breaks the existing `active_alerts` field. Pi4 daemon and
LoRa packet must be updated in lockstep.

**Recommendation: Shape A.** It is additive (no client breakage), it
matches the existing `fault_details` field this spec already proposes, and
it lets a single code change tier in the future without a flag day.

### Existing client-side classification table

The Kivy app's [`KivyApp/kilnapp/alerts.py`](../KivyApp/kilnapp/alerts.py)
already enumerates every alert code the system is known to emit, organised
into `FAULT_CODES` and `NOTICE_CODES` frozensets. **That file is the
authoritative starting list of codes for the firmware author** - rather
than re-deriving the codes from scratch, copy the two sets and assign each
code its tier in the firmware module that emits it. Anything not in either
set is INFO by default.

Codes the Kivy table currently knows about:
- **FAULT**: `CIRC_FAN_FAULT`, `EXHAUST_FAN_STALL`, `VENT_STALL`,
  `SENSOR_LUMBER_FAIL`, `SENSOR_INTAKE_FAIL`, `SENSOR_FAILURE`,
  `MOISTURE_PROBE_FAIL`, `HEATER_TIMEOUT`, `HEATER_FAULT`, `TEMP_OOR`,
  `RH_OOR`, `TEMP_OUT_OF_RANGE`, `RH_OUT_OF_RANGE`, `OVER_TEMP`,
  `SD_FAIL`, `SD_WRITE_FAIL`, `LORA_FAIL`, `LORA_TIMEOUT`
- **NOTICE**: `STAGE_GOAL_NOT_MET`, `STAGE_GOAL_NOT_REACHED`,
  `WATER_PAN_REMINDER`

### Per-module tier assignment

The migration plan section lower in this spec must be updated so each
module's `fault_code` is accompanied by an explicit tier. Modules
implementing the fault contract should expose:

```python
class SomeModule:
    fault: bool
    fault_code: Optional[str]
    fault_message: Optional[str]
    fault_tier: str  # "fault" | "notice" | "info" - default "fault"
    fault_last_checked_ms: Optional[int]
```

Hardware modules (circulation, exhaust, vents, sensors, moisture, current,
lora, sdcard, logger, display) all default to `fault_tier = "fault"` -
their entire purpose is to detect hardware problems, so anything they
flag is hardware. The schedule module is the only place that emits NOTICE
codes (`STAGE_GOAL_NOT_MET`, `WATER_PAN_REMINDER`) and the only place
that emits INFO codes (`stage_advance`, etc.). Schedule alerts should
carry the tier explicitly when they are recorded in `_last_alert_ts`.

### Tier must propagate to /alerts as well

The Kivy Alerts screen ([`KivyApp/kilnapp/screens/alerts.py`](../KivyApp/kilnapp/screens/alerts.py))
currently calls `kilnapp.alerts.classify(code)` on every row returned by
`GET /alerts` because the response rows have no tier field. Once the
firmware tags severity at source, **`/alerts` rows must include a `tier`
field** (one of `"fault"` / `"notice"` / `"info"`) so the screen can
drop its client-side classification call.

Concretely the new `/alerts` row shape becomes:

```json
{
  "ts": 1712685000,
  "level": "WARN",
  "tier": "fault",
  "source": "circulation",
  "message": "Current out of range after on() - possible fan fault",
  "code": "CIRC_FAN_FAULT"
}
```

The Pico stores raw event log lines today, so the implementer has a
choice: either embed the tier in the log line format itself
(`2026-04-09 14:32:01 [ERROR] [circulation] [FAULT] CIRC_FAN_FAULT: ...`)
and update `_parse_event_line` in `main.py` to extract it, or keep the
log format and look up the tier from a static table when serving
`/alerts`. The former survives a Pico reboot; the latter is simpler.

### Acceptance criterion (additional)

Once the firmware tags severity, the Kivy app should drop the
`FAULT_CODES` / `NOTICE_CODES` tables in `kilnapp/alerts.py` and the
`classify()` calls in `kilnapp/screens/alerts.py` and read `tier` from
`fault_details` (on `/status`) and from each `/alerts` row directly. The
hardcoded tables remain only as a backwards-compatibility fallback for
older firmware.

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
aggregator. It runs inside `_update_status_cache()` so the cache rebuilt
on every control-loop tick (10s while idle, 30-120s during a run) carries
fresh fault state.

### Normative requirements

1. **Skip absent modules.** Some `lib/` modules legitimately fail to
   construct at boot (e.g. SD card not present, INA219 missing, display
   not wired). The aggregator MUST check `if mod is None: continue` for
   every module before calling `check_health()`. The current
   `_status_cache` builder already follows this pattern (`if monitor_12v
   is not None`, `if circulation else 0`, etc.); the aggregator must
   match.

2. **Catch exceptions from `check_health()`.** A buggy module must not
   crash the status cache update. Wrap each `check_health()` call in a
   try/except and surface the failure as a synthetic
   `MODULE_CHECK_FAILED` fault tagged with the module name.

3. **Run during idle, not just during runs.** `_update_status_cache()`
   has an "idle direct reads" block (added recently to keep
   sensor / moisture values fresh on the dashboard when no run is
   active). The aggregator MUST run inside that block too, in both the
   run-active and idle code paths. Otherwise faults won't appear on the
   Kivy dashboard until the next run starts - which defeats the whole
   point for the "is the kiln OK before I start a run" check.

4. **Cheap calls only.** `check_health()` runs every cache update, so
   it must not perform any expensive operation: no I2C reads on every
   call, no SD writes, no SPI transactions just to ask "are you OK".
   Modules that need active probing should perform it in their own
   `tick()` method or alongside their normal operations and only
   re-read cached state from `check_health()`.

### Sketch

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
- `fault_details: list[{code, source, message, tier}]` - new. Powers the
  Alerts screen (Phase 5, already shipped) and the dashboard's fault
  banner with rich per-fault info.

Update `Specs/lora_telemetry_spec.md` and the Pi4 daemon to mirror these
field changes.

### LoRa wire format constraints

The LoRa telemetry packet has a hard 255-byte cap (SX1278 FIFO) and the
existing telemetry payload is already ~225 bytes after the recent
compaction work in `main.py:_build_compact_json` (see `PROJECT.md` "Known
firmware bugs" for the history). There is **no room** to embed
`fault_details` as a list of `{code, source, message, tier}` dicts in
every telemetry packet.

Recommendation: send only a flat list of fault codes over LoRa, and
serve the rich `fault_details` exclusively over the HTTP `/status`
endpoint. The Pi4 daemon receives the LoRa codes, looks up the message
text from a static table (or just displays the code), and exposes the
fuller `fault_details` shape via its own `/status` endpoint.

```python
# In _build_compact_json for the LoRa packet, add a single short field:
#   "faults": ["CIRC_FAN_FAULT", "SD_WRITE_FAIL"]
# Sized check: each code is ~15-20 chars, list overhead ~5 chars per
# entry. Three faults adds ~70 bytes. The aggregator MUST cap the LoRa
# fault list to (e.g.) 5 codes to keep us safely under 255.
```

Anything new added to the LoRa packet must use the same conventions as
the existing fields:
- Hand-built compact JSON via `_compact_json_value` / `_build_compact_json`,
  not `json.dumps`. MicroPython serialises floats at full IEEE754
  precision and inserts whitespace after every separator.
- Short stable field names (`faults`, not `active_fault_codes`).
- Floats rounded to 1dp.
- No nested objects per fault. Lists of strings are fine.

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
   the LoRa telemetry packet so cottage-mode users see the same info.  Add a ToDo in PROJECT.md to implement the updated telemetry spec and for the Pi4 daemon spec to mirror the new fields.

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
   module each tick, in both the run-active and idle code paths.
3. **Canonical motivating example - circulation fan disconnect.** With
   the circulation fan power harness physically disconnected (or one of
   the fan blades blocked so it can't spin), start the `test_quick.json`
   schedule from the Kivy dashboard. Wait one control-loop tick (~10
   seconds while idle, the first tick of the run otherwise). The
   following must all hold:

   `GET /status` returns:
   ```json
   {
     "...": "...",
     "run_active": true,
     "active_alerts": ["CIRC_FAN_FAULT"],
     "fault_details": [
       {
         "code": "CIRC_FAN_FAULT",
         "source": "circulation",
         "tier": "fault",
         "message": "Current out of range after on() - possible fan fault"
       }
     ]
   }
   ```

   `GET /alerts` returns at least one row with
   `{"level": "ERROR", "tier": "fault", "source": "circulation",
   "code": "CIRC_FAN_FAULT", ...}`.

   The Kivy dashboard shows a red **FAULT** banner with the text
   `Circ fan fault` (or `FAULT: Circ fan fault`) within ~10 seconds of
   the run starting. Tapping the banner navigates to the Alerts tab
   where the same row is visible with both the **ERROR** log-level
   badge and the **FAULT** tier badge.

4. Disconnecting one of the SHT31 sensors causes
   `active_alerts` to contain the appropriate sensor code within 3 ticks.
5. Pulling the SD card mid-run causes `"SD_WRITE_FAIL"` to surface within
   3 ticks (does not require remount).
6. Forcing one of the unit tests to fail causes the equivalent runtime
   path to also fail and produce a fault. (Test the contract by patching
   the test to inject a known-bad reading.)
7. `test_modules.py` continues to pass for every module.
8. `main.py` integration test (run with no faults) shows
   `active_alerts == []` and `fault_details == []`.
9. With the firmware emitting `tier` on every alert, the Kivy app's
   `kilnapp/alerts.py` `FAULT_CODES` / `NOTICE_CODES` tables can be
   commented out and the dashboard / Alerts screen still classify
   correctly. (This validates that the firmware-side tagging is
   complete and the client fallback is genuinely a fallback.)

When all nine pass, the gap between "fault detected" and "operator sees
the fault" is closed.
