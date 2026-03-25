# Display Button & Timeout Feature Spec

**Target file:** `lib/display.py`
**New file:** none
**Hardware:** Momentary pushbutton, GP10, wired to GND, internal pull-up enabled

---

## Overview

Add two related features to the existing `Display` class:

1. **Auto-timeout:** The display blanks (backlight off) after a configurable idle
   period with no button activity.
2. **Button wake + page cycling:** A single momentary pushbutton wakes the display
   from timeout and, when the display is already awake, advances to the next page.

The display class becomes page-aware: callers register named pages as render
callbacks, and the button cycles through them in order.

---

## Hardware

```
GP10 ---- [button] ---- GND
Internal pull-up enabled (Pin.PULL_UP)
Button is active-low (reads 0 when pressed)
```

No external resistor required. A 100nF cap from GP10 to GND is recommended on
the physical build but not required for firmware.

---

## New constructor parameters

```python
Display(
    uart_id=UART_ID,
    tx_pin=UART_TX,
    rx_pin=UART_RX,
    baudrate=BAUD_RATE,
    button_pin=10,          # NEW: GP10, pass None to disable button entirely
    timeout_s=30,           # NEW: seconds of inactivity before blanking; 0 = never
)
```

Both new parameters are optional with sensible defaults. Passing `button_pin=None`
or `timeout_s=0` disables those features independently.

---

## Button behaviour

Implement button reading via **polling**, not IRQ. The `tick()` method (see below)
is called from the main loop and handles all button logic. Do not use `Pin.irq()`
— consistent with the pattern used in other modules and avoids IRQ/UART conflicts.

Debounce: require the pin to read stable for **50 ms** before acting on a press.
Implement with a simple state machine tracking `_btn_last_state` and
`_btn_stable_since` (ticks_ms timestamp).

### Press actions

| Display state | Button press action |
|---|---|
| Blanked (timeout) | Wake display, redraw current page, reset idle timer. Do NOT advance page. |
| Awake, 0 or 1 pages registered | Reset idle timer only. |
| Awake, 2+ pages registered | Advance to next page (wrapping), redraw, reset idle timer. |

A "press" is a falling edge (HIGH -> LOW after debounce). Ignore releases.

---

## Timeout behaviour

- Track `_last_activity` as a `ticks_ms` timestamp. Reset it on every button press
  and on every call to `_reset_idle()` (see below).
- `tick()` compares `ticks_diff(now, _last_activity)` against `timeout_s * 1000`.
- When timeout expires: call `set_backlight(255)` (off per datasheet) and set
  `_display_on = False`.
- When waking: call `set_backlight(0)` (full brightness) and set `_display_on = True`,
  then redraw current page.
- If `timeout_s == 0`, never blank regardless of inactivity.

---

## Page system

```python
display.register_page(name: str, render_fn: callable)
```

- `name`: string identifier (e.g. `"status"`, `"sensors"`, `"schedule"`)
- `render_fn`: a zero-argument callable. When a page becomes active, the display
  calls `self.clear()` then `render_fn()`. The render function is responsible for
  all draw calls needed to populate that page.
- Pages are stored in an ordered list in registration order.
- `_current_page_idx` tracks the active page (integer index).
- `show_page(name: str)`: jump directly to a named page (for programmatic
  navigation from main.py). Clears and redraws. Resets idle timer. Wakes display
  if blanked.
- `current_page_name` property: returns name of active page, or `None` if no
  pages registered.

### Page cycling

On button press (when awake, 2+ pages registered):
```python
_current_page_idx = (_current_page_idx + 1) % len(_pages)
self.clear()
_pages[_current_page_idx].render_fn()
```

---

## New public method: tick()

```python
def tick(self) -> bool:
```

Must be called regularly from the main loop (once per second is sufficient;
more frequent is fine). Handles:
1. Button debounce and press detection
2. Timeout check and blanking
3. Page redraw on wake

Returns `True` if a button press was detected this call, `False` otherwise.
This allows `main.py` to react to button presses if needed (e.g. logging).

---

## New public method: _reset_idle()

```python
def _reset_idle(self):
```

Resets `_last_activity` to now. Call this from any method that represents
user-visible activity (e.g. `show_page()`, any draw call initiated by the
application rather than by the button). This prevents the display blanking
mid-update when the kiln controller is actively writing sensor data.

Make this public (no leading underscore despite the name convention) so
`main.py` can call it when it refreshes sensor data on the display.

Rename to `reset_idle()` (no underscore).

---

## Internal state added to __init__

```python
# Button
self._btn_pin        = machine.Pin(button_pin, machine.Pin.IN,
                                   machine.Pin.PULL_UP) if button_pin is not None else None
self._btn_last_state = 1          # pulled high = not pressed
self._btn_stable_ms  = 0          # ticks_ms when state last changed
self._debounce_ms    = 50

# Timeout
self._timeout_ms     = timeout_s * 1000
self._last_activity  = time.ticks_ms()
self._display_on     = True

# Pages
self._pages          = []         # list of SimpleNamespace(name, render_fn)
self._current_page_idx = 0
```

---

## Changes to existing methods

**`__init__`:** Add new parameters, initialise new state. No other changes to
existing init logic.

**No changes** to any existing drawing primitives, text methods, or scrolling
console. Those are all unaffected.

---

## Unit tests to add

Add these tests to the existing `test()` function, after the existing tests.
All new tests follow the same PASS/FAIL pattern already in place.

```
Test: register_page adds page correctly
  - Register one page named "status"
  - Assert current_page_name == "status"
  - PASS/FAIL

Test: show_page navigates correctly
  - Register pages "status" and "sensors"
  - Call show_page("sensors")
  - Assert current_page_name == "sensors"
  - PASS/FAIL

Test: show_page unknown name raises ValueError
  - Call show_page("nonexistent") inside try/except
  - PASS if ValueError raised, FAIL otherwise

Test: timeout blanks display
  - Construct Display with timeout_s=1, button_pin=None
  - Call reset_idle() to sync timer
  - Sleep 1.1 seconds
  - Call tick()
  - Assert _display_on == False
  - PASS/FAIL
  (Note: this test requires hardware; skip gracefully if UART not connected)

Test: tick() returns bool
  - Call tick() once
  - Assert isinstance(result, bool)
  - PASS/FAIL
```

Button press tests require hardware interaction and cannot be automated; add a
comment noting this.

---

## What NOT to change

- Do not change any existing method signatures
- Do not change UART pin assignments, baud rate, or init sequence
- Do not change the `_sanitise()` logic
- Do not change the scrolling console behaviour
- Do not add logging/logger parameter at this stage (noted for future main.py work)
- Do not remove or modify any existing unit tests
- ASCII only in all strings, comments, docstrings (project-wide rule)

---

## Summary of new public API

```python
# Constructor (new params)
Display(..., button_pin=10, timeout_s=30)

# Page management
display.register_page(name, render_fn)
display.show_page(name)
display.current_page_name          # property

# Main loop integration
display.tick()                     # call once per loop iteration
display.reset_idle()               # call when app writes to display
```