# boot.py -- runs before main.py on every Pico boot.
#
# Purpose:
#   1. Give USB CDC time to enumerate so the host can attach a REPL
#      before main.py starts touching hardware.
#   2. Give the user a 3-second window to abort main.py with Ctrl-C
#      without bricking the device.
#   3. Provide a "kill switch" -- if /no_main exists on the flash,
#      main.py will be skipped entirely and the Pico drops to REPL.
#      To create the kill switch from the REPL:
#          >>> open('/no_main', 'w').close()
#      To remove it (re-enable normal boot):
#          >>> import os; os.remove('/no_main')
#   4. Print the last boot error (if any) so you can see what failed
#      on the previous boot without needing to be attached at the time.

import time
import os
import sys
import select

# Configurable startup delay -- USB enumeration plus Ctrl-C window.
# The delay is polled in small increments so a Ctrl-C from a host
# (e.g. mpremote's raw-REPL handshake) is detected within ~50ms.
# Without polling, mpremote times out waiting for the REPL.
BOOT_DELAY_S = 3

print("=" * 50)
print("boot.py -- Kiln Controller")
print("=" * 50)

# Show last boot error if one was recorded
try:
    with open("/boot_error.log", "r") as f:
        last_err = f.read()
    if last_err:
        print("Previous boot recorded an error:")
        print("-" * 50)
        print(last_err)
        print("-" * 50)
except OSError:
    pass  # no previous error -- normal

# Check for kill switch
skip_main = False
try:
    os.stat("/no_main")
    skip_main = True
except OSError:
    pass

if skip_main:
    print("/no_main file present -- SKIPPING main.py")
    print("To re-enable main.py:  os.remove('/no_main')")
    # Tell main.py to bail out by setting a module-level flag.
    # main.py will check this immediately on import.
    import builtins

    builtins._kiln_skip_main = True
else:
    print(f"Booting in {BOOT_DELAY_S}s -- press Ctrl-C now to interrupt.")
    print("(Or create /no_main to permanently skip main.py)")
    # Poll stdin in 50ms increments instead of one long sleep, so:
    #   1. Ctrl-C from a host is caught quickly (raises KeyboardInterrupt)
    #   2. Any other host input (e.g. mpremote raw-REPL handshake bytes)
    #      aborts the delay so mpremote does not time out.
    poller = select.poll()
    poller.register(sys.stdin, select.POLLIN)
    end_ms = time.ticks_add(time.ticks_ms(), BOOT_DELAY_S * 1000)
    try:
        while time.ticks_diff(end_ms, time.ticks_ms()) > 0:
            events = poller.poll(50)
            if events:
                # Host is sending bytes (mpremote handshake or user input)
                print("Host input detected -- aborting boot delay.")
                import builtins

                builtins._kiln_skip_main = True
                break
    except KeyboardInterrupt:
        print("Boot interrupted by Ctrl-C -- dropping to REPL.")
        import builtins

        builtins._kiln_skip_main = True

print("boot.py complete.")
print("=" * 50)
