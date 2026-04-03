# test_modules.py -- Run all lib/ module unit tests on the Pico
#
# Usage:  mpremote run test_modules.py
#
# Imports each lib module, calls its test() function, and reports
# a summary of pass/fail results at the end.

import sys

MODULE_TESTS = [
    ("lib.sdcard",       "SDCard"),
    ("lib.SHT31sensors", "SHT31Sensors"),
    ("lib.current",      "CurrentMonitor"),
    ("lib.circulation",  "CirculationFans"),
    ("lib.exhaust",      "ExhaustFan"),
    ("lib.vents",        "Vents"),
    ("lib.heater",       "Heater"),
    ("lib.moisture",     "MoistureProbe"),
    ("lib.display",      "Display"),
    ("lib.lora",         "LoRa"),
    ("lib.logger",       "Logger"),
    ("lib.schedule",     "KilnSchedule"),
]

print("=" * 50)
print("  Kiln Controller -- Module Unit Tests")
print("=" * 50)

results = {}
for mod_name, label in MODULE_TESTS:
    print(f"\n--- {label} ({mod_name}) ---")
    try:
        mod = __import__(mod_name, None, None, ["test"])
        passed = mod.test()
        results[label] = passed
    except Exception as e:
        print(f"  ERROR: {e}")
        results[label] = False

# Summary
failed = [name for name, ok in results.items() if not ok]
passed_count = sum(1 for ok in results.values() if ok)
total = len(results)

print("\n" + "=" * 50)
print(f"  Results: {passed_count}/{total} modules passed")
print("=" * 50)

if failed:
    print("\n  FAILED modules:")
    for name in failed:
        print(f"    - {name}")
else:
    print("\n  All modules passed!")

print()
sys.exit(0 if not failed else 1)
