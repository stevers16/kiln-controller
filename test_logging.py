# test_logging.py
#
# Top-level integration test: exercises the logging service with real
# CirculationFans events and mock data() records.
#
# Run on the Pico via:
#   mpremote run test_logging.py

import sys
if "/lib" not in sys.path:
    sys.path.append("/lib")

import time
import uos
from sdcard import SDCard
from logger import Logger, DATA_COLUMNS
from circulation import CirculationFans


def test():
    print("=== Logging integration test ===\n")
    all_passed = True

    # --- Setup ---
    sd = SDCard()
    logger = Logger(sd)

    result = logger.begin_run()
    passed = result is True and logger.run_active
    print(f"  {'PASS' if passed else 'FAIL'} - begin_run() succeeded, run_active=True")
    all_passed &= passed
    if not result:
        print("  ABORT - cannot continue without SD card")
        return False

    # Find log files so we can verify contents later
    sd_files = uos.listdir(sd.mount_point)
    event_file = [f for f in sd_files if f.startswith("event_")][-1]
    data_file = [f for f in sd_files if f.startswith("data_")][-1]
    event_path = sd.mount_point + "/" + event_file
    data_path = sd.mount_point + "/" + data_file

    # ------------------------------------------------------------------
    # Part 1 - CirculationFans event logging
    # ------------------------------------------------------------------
    print("\n--- Circulation fan events ---")

    fans = CirculationFans(logger=logger)

    # Turn on at 60%
    fans.on(60)
    time.sleep_ms(100)

    # Change speed to 85%
    fans.set_speed(85)
    time.sleep_ms(100)

    # Turn off
    fans.off()
    time.sleep_ms(100)

    # Turn on at full speed
    fans.on(100)
    time.sleep_ms(100)

    # Turn off again
    fans.off()

    # Read back event log and verify
    with open(event_path, "r") as f:
        event_lines = f.readlines()

    # Expected circulation events (in order after "Run started"):
    #   Fans on at 60%
    #   Fans on at 85%   (set_speed calls on() internally)
    #   Fans off
    #   Fans on at 100%
    #   Fans off
    circ_lines = [l for l in event_lines if "[circulation]" in l]

    passed = len(circ_lines) == 5
    print(f"  {'PASS' if passed else 'FAIL'} - 5 circulation events logged (got {len(circ_lines)})")
    all_passed &= passed

    passed = "Fans on at 60%" in circ_lines[0]
    print(f"  {'PASS' if passed else 'FAIL'} - First event: Fans on at 60%")
    all_passed &= passed

    passed = "Fans on at 85%" in circ_lines[1]
    print(f"  {'PASS' if passed else 'FAIL'} - Second event: Fans on at 85%")
    all_passed &= passed

    passed = "Fans off" in circ_lines[2]
    print(f"  {'PASS' if passed else 'FAIL'} - Third event: Fans off")
    all_passed &= passed

    passed = "[INFO ]" in circ_lines[0]
    print(f"  {'PASS' if passed else 'FAIL'} - Events use INFO level")
    all_passed &= passed

    # Verify Run started is the first event
    passed = "[logger    ]" in event_lines[0] and "Run started" in event_lines[0]
    print(f"  {'PASS' if passed else 'FAIL'} - First line is 'Run started'")
    all_passed &= passed

    # ------------------------------------------------------------------
    # Part 2 - Mock data records
    # ------------------------------------------------------------------
    print("\n--- Mock data records ---")

    # Simulate a few minutes of kiln operation
    mock_data = [
        {
            "ts": "2026-03-17 10:00:00",
            "temp_lumber": 28.50,
            "rh_lumber": 78.20,
            "temp_intake": 22.10,
            "rh_intake": 45.00,
            "mc_ch1": 32.50,
            "mc_ch2": 31.80,
            "exhaust_pct": 0,
            "circ_pct": 60,
            "vent_intake": 100,
            "vent_exhaust": 50,
            "heater_on": False,
            "stage": "warmup",
        },
        {
            "ts": "2026-03-17 10:05:00",
            "temp_lumber": 35.70,
            "rh_lumber": 72.10,
            "temp_intake": 30.40,
            "rh_intake": 40.30,
            "mc_ch1": 30.10,
            "mc_ch2": 29.50,
            "exhaust_pct": 40,
            "circ_pct": 85,
            "vent_intake": 80,
            "vent_exhaust": 60,
            "heater_on": True,
            "stage": "drying_1",
        },
        {
            "ts": "2026-03-17 10:10:00",
            "temp_lumber": 42.30,
            "rh_lumber": 65.00,
            "temp_intake": 38.20,
            "rh_intake": 35.50,
            "mc_ch1": 27.80,
            "mc_ch2": 27.20,
            "exhaust_pct": 75,
            "circ_pct": 100,
            "vent_intake": 60,
            "vent_exhaust": 80,
            "heater_on": True,
            "stage": "drying_1",
        },
        # Partial record - only a few fields
        {
            "ts": "2026-03-17 10:15:00",
            "temp_lumber": 43.10,
            "rh_lumber": 63.50,
        },
    ]

    for rec in mock_data:
        logger.data(rec)

    # Read back CSV and verify
    with open(data_path, "r") as f:
        csv_lines = f.readlines()

    # Line 0 = header, lines 1-4 = data rows
    passed = csv_lines[0].strip() == ",".join(DATA_COLUMNS)
    print(f"  {'PASS' if passed else 'FAIL'} - CSV header matches DATA_COLUMNS")
    all_passed &= passed

    passed = len(csv_lines) == 5  # 1 header + 4 data rows
    print(f"  {'PASS' if passed else 'FAIL'} - 4 data rows written (got {len(csv_lines) - 1})")
    all_passed &= passed

    # Verify first full row
    row1 = csv_lines[1].strip().split(",")
    passed = row1[0] == "2026-03-17 10:00:00"
    print(f"  {'PASS' if passed else 'FAIL'} - Row 1 timestamp correct")
    all_passed &= passed

    passed = row1[1] == "28.50" and row1[2] == "78.20"
    print(f"  {'PASS' if passed else 'FAIL'} - Row 1 temp/rh floats formatted to 2dp")
    all_passed &= passed

    passed = row1[11] == "0"  # heater_on=False -> "0"
    print(f"  {'PASS' if passed else 'FAIL'} - Row 1 heater_on=False written as '0'")
    all_passed &= passed

    passed = row1[12] == "warmup"
    print(f"  {'PASS' if passed else 'FAIL'} - Row 1 stage='warmup'")
    all_passed &= passed

    # Verify second row has heater_on=True -> "1"
    row2 = csv_lines[2].strip().split(",")
    passed = row2[11] == "1"
    print(f"  {'PASS' if passed else 'FAIL'} - Row 2 heater_on=True written as '1'")
    all_passed &= passed

    # Verify partial record (row 4) has empty fields
    row4 = csv_lines[4].strip().split(",")
    passed = len(row4) == len(DATA_COLUMNS)
    print(f"  {'PASS' if passed else 'FAIL'} - Partial row has correct column count")
    all_passed &= passed

    passed = row4[0] == "2026-03-17 10:15:00" and row4[1] == "43.10" and row4[3] == ""
    print(f"  {'PASS' if passed else 'FAIL'} - Partial row: present fields filled, missing fields empty")
    all_passed &= passed

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    print("\n--- Cleanup ---")

    logger.end_run()
    passed = not logger.run_active
    print(f"  {'PASS' if passed else 'FAIL'} - end_run() succeeded, run_active=False")
    all_passed &= passed

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print(f"\n{'All tests passed!' if all_passed else 'Some tests FAILED'}")
    return all_passed


if __name__ == "__main__":
    test()
