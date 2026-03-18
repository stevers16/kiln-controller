# lib/logger.py
#
# Logging service for the kiln firmware.
# Owns one SDCard instance. Provides event logging (text) and data
# logging (CSV) for a drying run.
#
# Both log files live on the SD card. Silent fail with REPL warning
# if SD is unavailable — the kiln must keep running regardless.

import time

# --- CSV column order ---
DATA_COLUMNS = [
    "ts", "temp_lumber", "rh_lumber", "temp_intake", "rh_intake",
    "mc_ch1", "mc_ch2", "exhaust_pct", "circ_pct",
    "vent_intake", "vent_exhaust", "heater_on", "stage",
]


class Logger:
    """
    Single logging service for the entire kiln firmware.
    Provides event logging (text) and data logging (CSV) per drying run.
    """

    def __init__(self, sd):
        self._sd = sd
        self._event_file = None
        self._data_file = None
        self._run_active = False

    # ------------------------------------------------------------------
    # Timestamp helpers
    # ------------------------------------------------------------------

    def _time_is_set(self):
        """True if RTC has been set (year >= 2024)."""
        return time.localtime()[0] >= 2024

    def _timestamp(self):
        """Return a formatted timestamp string."""
        if self._time_is_set():
            t = time.localtime()
            return f"{t[0]:04d}-{t[1]:02d}-{t[2]:02d} {t[3]:02d}:{t[4]:02d}:{t[5]:02d}"
        else:
            return f"+{time.ticks_ms() // 1000}s"

    def _file_suffix(self):
        """Return suffix for log file names."""
        if self._time_is_set():
            t = time.localtime()
            return f"{t[0]:04d}{t[1]:02d}{t[2]:02d}_{t[3]:02d}{t[4]:02d}"
        else:
            return f"run_{time.ticks_ms() // 1000:05d}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def begin_run(self):
        """
        Create event and data log files on the SD card.
        Returns True on success, False if SD is unavailable.
        """
        if not self._sd.mount():
            print("[logger] WARNING: SD mount failed — logging disabled")
            return False

        suffix = self._file_suffix()
        base = self._sd.mount_point
        event_path = f"{base}/event_{suffix}.txt"
        data_path = f"{base}/data_{suffix}.csv"

        try:
            self._event_file = open(event_path, "w")
            self._data_file = open(data_path, "w")
            # Write CSV header
            self._data_file.write(",".join(DATA_COLUMNS) + "\n")
            self._data_file.flush()
            self._run_active = True
            self.event("logger", "Run started")
            return True
        except Exception as e:
            print(f"[logger] WARNING: Failed to create log files — {e}")
            self._close_files()
            return False

    def end_run(self):
        """Flush and close both log files, unmount SD."""
        if self._run_active:
            self.event("logger", "Run ended")
        self._close_files()
        self._sd.unmount()
        self._run_active = False

    def event(self, source, message, level="INFO"):
        """
        Append a timestamped event line to the event log and REPL.

        Format: 2026-03-17 14:30:05 [INFO ] [exhaust    ] Fan on at 75%
        """
        ts = self._timestamp()
        lvl = f"{level:<5s}"
        src = f"{source:<10s}"
        line = f"{ts} [{lvl}] [{src}] {message}"

        # Always print to REPL
        print(line)

        # Write to SD if available
        if self._event_file:
            try:
                self._event_file.write(line + "\n")
                self._event_file.flush()
            except Exception as e:
                print(f"[logger] WARNING: SD write failed — {e}")

    def data(self, record):
        """
        Append a CSV data row. record is a dict with keys from DATA_COLUMNS.
        Missing keys are written as empty. Floats to 2 decimal places. Bools as 1/0.
        """
        if not self._data_file:
            return

        values = []
        for col in DATA_COLUMNS:
            val = record.get(col, "")
            if val == "" or val is None:
                values.append("")
            elif isinstance(val, bool):
                values.append("1" if val else "0")
            elif isinstance(val, float):
                values.append(f"{val:.2f}")
            else:
                values.append(str(val))

        line = ",".join(values)
        try:
            self._data_file.write(line + "\n")
            self._data_file.flush()
        except Exception as e:
            print(f"[logger] WARNING: SD write failed — {e}")

    @property
    def run_active(self):
        """True if a drying run is currently being logged."""
        return self._run_active

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _close_files(self):
        """Safely close both file handles."""
        for f in (self._event_file, self._data_file):
            if f:
                try:
                    f.flush()
                    f.close()
                except Exception:
                    pass
        self._event_file = None
        self._data_file = None


# --- Unit test ---
def test():
    import uos
    from sdcard import SDCard

    print("=== Logger unit test ===")
    sd = SDCard()
    logger = Logger(sd)
    all_passed = True

    # --- Test 1: begin_run() ---
    result = logger.begin_run()
    passed = result is True
    print(f"  {'PASS' if passed else 'FAIL'} — begin_run() returns True")
    all_passed &= passed

    # --- Test 2: run_active ---
    passed = logger.run_active is True
    print(f"  {'PASS' if passed else 'FAIL'} — run_active is True")
    all_passed &= passed

    # Find the log files on SD
    sd_files = uos.listdir(sd.mount_point)
    event_files = [f for f in sd_files if f.startswith("event_")]
    data_files = [f for f in sd_files if f.startswith("data_")]

    # --- Test 3: event log file exists ---
    passed = len(event_files) > 0
    print(f"  {'PASS' if passed else 'FAIL'} — event log file exists on SD")
    all_passed &= passed

    # --- Test 4: data log file exists ---
    passed = len(data_files) > 0
    print(f"  {'PASS' if passed else 'FAIL'} — data log file exists on SD")
    all_passed &= passed

    # --- Test 5: event() writes correctly formatted line ---
    logger.event("exhaust", "Fan on at 75%")
    # Read last line of event file
    event_path = sd.mount_point + "/" + event_files[-1]
    with open(event_path, "r") as f:
        lines = f.readlines()
    last_line = lines[-1].strip()
    passed = "[INFO ]" in last_line and "[exhaust   ]" in last_line and "Fan on at 75%" in last_line
    print(f"  {'PASS' if passed else 'FAIL'} — event() writes correctly formatted line")
    all_passed &= passed

    # --- Test 6: event() with WARN level ---
    logger.event("sdcard", "Write retry", level="WARN")
    with open(event_path, "r") as f:
        lines = f.readlines()
    last_line = lines[-1].strip()
    passed = "[WARN ]" in last_line and "[sdcard    ]" in last_line
    print(f"  {'PASS' if passed else 'FAIL'} — event() with WARN level")
    all_passed &= passed

    # --- Test 7: data() writes CSV row with correct columns ---
    full_record = {
        "ts": "2026-03-17 14:30:05",
        "temp_lumber": 45.12,
        "rh_lumber": 62.50,
        "temp_intake": 32.00,
        "rh_intake": 55.30,
        "mc_ch1": 18.40,
        "mc_ch2": 19.10,
        "exhaust_pct": 75,
        "circ_pct": 100,
        "vent_intake": 50,
        "vent_exhaust": 80,
        "heater_on": True,
        "stage": "conditioning",
    }
    logger.data(full_record)
    data_path = sd.mount_point + "/" + data_files[-1]
    with open(data_path, "r") as f:
        lines = f.readlines()
    # First line is header, second is our row
    passed = len(lines) == 2 and "45.12" in lines[1] and ",1," in lines[1]
    print(f"  {'PASS' if passed else 'FAIL'} — data() writes CSV row with correct columns")
    all_passed &= passed

    # --- Test 8: data() with partial record ---
    logger.data({"ts": "2026-03-17 14:31:00", "temp_lumber": 45.50})
    with open(data_path, "r") as f:
        lines = f.readlines()
    last_row = lines[-1].strip()
    fields = last_row.split(",")
    passed = len(fields) == len(DATA_COLUMNS) and fields[0] == "2026-03-17 14:31:00" and fields[2] == ""
    print(f"  {'PASS' if passed else 'FAIL'} — data() with partial record (missing keys write empty)")
    all_passed &= passed

    # --- Test 9: end_run() ---
    logger.end_run()
    passed = True  # No exception
    print(f"  {'PASS' if passed else 'FAIL'} — end_run() closes cleanly")
    all_passed &= passed

    # --- Test 10: run_active after end_run ---
    passed = logger.run_active is False
    print(f"  {'PASS' if passed else 'FAIL'} — run_active is False after end_run")
    all_passed &= passed

    # --- Test 11: begin_run() can be called again ---
    sd2 = SDCard()
    logger2 = Logger(sd2)
    result = logger2.begin_run()
    passed = result is True and logger2.run_active
    logger2.end_run()
    print(f"  {'PASS' if passed else 'FAIL'} — begin_run() can be called again (second run)")
    all_passed &= passed

    print(f"\n{'All tests passed!' if all_passed else 'Some tests FAILED'}")
    return all_passed


if __name__ == "__main__":
    test()
