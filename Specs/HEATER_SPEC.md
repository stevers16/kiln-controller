# Spec: lib/heater.py

## Purpose

Controls the 120V backup ceramic heater via a Fotek SSR-25DA solid-state relay.
The SSR is driven by a digital GPIO output through a current-limiting resistor.
Safety logic (temperature limits, interlocks) is the responsibility of the main
controller -- heater.py is a simple on/off driver with logging.

---

## Hardware

- **SSR:** Fotek SSR-25DA, DC control input 3-32V, direct Pico GPIO drive
- **Control pin:** GP18 (digital output)
- **Current-limiting resistor:** 1k ohm in series between GP18 and SSR control input
- **Safety fuse:** RY85 85degC one-time thermal fuse in series on AC output side
  (hardware-only safety, no firmware involvement)
- **Load:** 500W ceramic PTC heater

---

## Class Interface

```python
class Heater:
    def __init__(self, pin: int, logger=None):
        ...
```

### Constructor

- `pin` -- GPIO pin number (caller passes GP18)
- `logger` -- optional Logger instance; if None, no logging occurs
- Initialise the pin as a digital output, driven LOW at construction
- Log an event on construction: `"Heater initialised, SSR off"`

### Methods

```python
def on(self) -> None:
    """Drive SSR control input HIGH, turning heater on."""

def off(self) -> None:
    """Drive SSR control input LOW, turning heater off."""

def is_on(self) -> bool:
    """Return True if heater is currently commanded on."""
```

---

## Behaviour

### on()
- Drive GP18 HIGH
- Update internal state to reflect heater is on
- Log event: `"Heater on"` at level INFO
- No return value

### off()
- Drive GP18 LOW
- Update internal state to reflect heater is off
- Log event: `"Heater off"` at level INFO
- No return value

### is_on()
- Return internal state bool -- reflects last commanded state
- Does not read GPIO; tracks state in software

---

## Logging

Follows the same pattern as all other modules:

```python
if self._logger:
    self._logger.event("heater", "Heater on")
```

Source string: `"heater"`

Events to log:
- Construction: `"Heater initialised, SSR off"` at INFO
- `on()`: `"Heater on"` at INFO
- `off()`: `"Heater off"` at INFO

---

## Boot Safety

The SSR control pin must be driven LOW at construction before any other
initialisation. This ensures the heater cannot fire due to a floating GPIO
during boot. This mirrors the pattern used in exhaust.py for the MOSFET gate.

---

## Unit Tests

Provide a standalone test file `test_heater.py` at the repo root following
the hardware-in-the-loop pattern used by other modules.

Tests should cover:

1. **Heater initialises off** -- after construction, `is_on()` returns False
   and SSR pin is LOW
2. **on() turns heater on** -- `is_on()` returns True after `on()`
3. **off() turns heater off** -- `is_on()` returns False after `on()` then `off()`
4. **Double on() is safe** -- calling `on()` twice does not error
5. **Double off() is safe** -- calling `off()` twice does not error
6. **Logger receives on event** -- verify logger.event() called with correct
   source and message on `on()`
7. **Logger receives off event** -- verify logger.event() called with correct
   source and message on `off()`
8. **Logger=None is safe** -- all methods work without logger

---

## Notes

- No current monitoring -- the SSR switches 120V AC; the INA219 monitors DC rails
  only and will not see heater load current
- No duty cycling or PWM -- on/off only; temperature control is the
  responsibility of the drying schedule controller in main.py
- No safety interlock logic in this module -- that is handled at the controller
  level and by the physical RY85 thermal fuse
- ASCII only in all strings -- no Unicode characters in comments, docstrings,
  log messages, or print statements