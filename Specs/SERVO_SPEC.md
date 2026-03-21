# lib/vents.py — Vent Servo Module Spec

## Purpose

Controls two MG90S metal gear servos driving homemade butterfly-style dampers —
one on the intake opening, one on the exhaust opening. Servos are always commanded
together (both open or both closed). No hard mechanical stops on the dampers.

---

## Hardware

| Servo   | GPIO | Power         |
|---------|------|---------------|
| Intake  | GP14 | 5V buck output |
| Exhaust | GP15 | 5V buck output |

- PWM signal direct from Pico — 3.3V logic is sufficient for MG90S signal input
- No level shifting required
- Both servos share a common 5V rail and GND with the Pico

---

## PWM Parameters

| Parameter       | Value                                      |
|-----------------|--------------------------------------------|
| Frequency       | 50 Hz (standard hobby servo)               |
| Period          | 20,000 µs                                  |
| OPEN pulse      | 1.9 ms → duty_u16 = 6225                   |
| CLOSED pulse    | 1.1 ms → duty_u16 = 3604                   |
| Travel time     | 600 ms (wait after command before deinit)  |

Pulse range is intentionally inset from the 1.0–2.0 ms spec to protect homemade
linkage with no hard mechanical stops.

**Duty calculations:**
```
OPEN:   int(1900 / 20000 * 65535) = 6225
CLOSED: int(1100 / 20000 * 65535) = 3604
```

---

## De-energize Behaviour

After commanding a move:
1. Set PWM duty on both pins
2. `time.sleep_ms(600)` — allow travel to complete
3. Call `pwm.deinit()` on both PWM objects

This prevents holding torque against the linkage and eliminates servo buzz and heat.
PWM must be **re-initialized on every move** since `deinit()` destroys the PWM object.

---

## API

```python
class Vents:
    def __init__(self, intake_pin=14, exhaust_pin=15, logger=None)
    def open()      # command both servos OPEN, wait 600ms, de-energize
    def close()     # command both servos CLOSED, wait 600ms, de-energize
    def is_open()   # returns bool — last commanded position (not sensed)
```

### Notes

- `is_open()` reflects the last commanded position. No position feedback hardware
  exists yet (INA219 current sensing is planned for a future iteration).
- Boot behaviour: `__init__` calls `close()` so physical position matches software
  state from power-on.
- Logger injection: accept `logger=None`; call `logger.event("vents", ...)` on
  `open()` and `close()` if logger is provided. Never import logger directly.

---

## Internal Structure

Use a private helper to avoid repetition:

```python
def _move(self, duty):
    """Init PWM on both pins, set duty, wait for travel, deinit."""
    intake_pwm = machine.PWM(machine.Pin(self._intake_pin))
    exhaust_pwm = machine.PWM(machine.Pin(self._exhaust_pin))
    intake_pwm.freq(PWM_FREQ)
    exhaust_pwm.freq(PWM_FREQ)
    intake_pwm.duty_u16(duty)
    exhaust_pwm.duty_u16(duty)
    time.sleep_ms(TRAVEL_MS)
    intake_pwm.deinit()
    exhaust_pwm.deinit()
```

---

## Constants Block

```python
PWM_FREQ     = 50
DUTY_OPEN    = 6225
DUTY_CLOSED  = 3604
TRAVEL_MS    = 600
```

---

## Unit Test

Follow the `exhaust.py` pattern: `test()` function, runnable as `__main__`.

No position sensing is available, so tests confirm correct execution and state
tracking only — not physical position.

| Test | Action | Assert |
|------|--------|--------|
| 1 | `close()` | No exceptions; `is_open()` returns `False` |
| 2 | `open()` | No exceptions; `is_open()` returns `True` |
| 3 | `close()` again | No exceptions; `is_open()` returns `False` |
| 4 | Rapid cycle: open→close→open, 1s between each | No PWM init errors across repeated deinit/reinit |

Print PASS/FAIL per test. Return `True` if all pass.

---

## Future

INA219 current sensing on the 5V rail is planned to provide indirect position
confirmation (stall current spike when damper reaches end of travel). This will
be added as a separate module and injected into `Vents` when implemented.

---

## File Location

`lib/vents.py` — follows the structure of `lib/exhaust.py` as the canonical pattern.