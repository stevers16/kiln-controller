# lib/schedule.py
#
# Drying schedule controller for the kiln firmware.
#
# Orchestrates all hardware modules (heater, exhaust fan, vents, circulation
# fans, sensors, moisture probes) to execute a multi-stage drying schedule
# loaded from a JSON file on the SD card.
#
# This module does NOT drive hardware directly -- it calls the existing
# lib/ module APIs. It does NOT replace main.py; main.py instantiates all
# modules and passes them in.

import time

try:
    import ujson as json
except ImportError:
    import json

# --- Constants ---
TEMP_DEADBAND_C        = 2.0    # +/- deg C around target
RH_DEADBAND_PCT        = 8.0    # +/- pct RH around target
LOOP_INTERVAL_S        = 120    # seconds between ticks (normal)
VENT_LOOP_INTERVAL_S   = 30     # seconds between ticks while venting
OUT_OF_RANGE_ALERT_MIN = 30     # minutes before out-of-range alert fires
HEATER_FAULT_RISE_C    = 2.0    # minimum temp rise to confirm heater working
HEATER_FAULT_MIN       = 20     # minutes on with no rise before fault alert
COOLDOWN_FAN_SPEED     = 50     # circulation fan speed pct during cooldown
CIRC_FAN_SPEED         = 75     # circulation fan speed pct during a run
EXHAUST_VENT_SPEED     = 80     # exhaust fan speed pct for RH venting
EXHAUST_OVERHEAT_SPEED = 60     # exhaust fan speed pct for overheat venting

VALID_STAGE_TYPES = ("drying", "equalizing", "conditioning")


class KilnSchedule:
    """
    Multi-stage drying schedule controller.

    Coordinates heater, fans, vents, and sensors to execute a drying
    schedule loaded from JSON. Called via tick() from the main loop.
    """

    def __init__(self, sdcard, sensors, moisture, heater, exhaust,
                 circulation, vents, lora, logger=None):
        # Validate required arguments
        required = {
            "sdcard": sdcard, "sensors": sensors, "moisture": moisture,
            "heater": heater, "exhaust": exhaust, "circulation": circulation,
            "vents": vents, "lora": lora,
        }
        for name, obj in required.items():
            if obj is None:
                raise ValueError(f"KilnSchedule: {name} must not be None")

        self._sdcard = sdcard
        self._sensors = sensors
        self._moisture = moisture
        self._heater = heater
        self._exhaust = exhaust
        self._circulation = circulation
        self._vents = vents
        self._lora = lora
        self._logger = logger

        # Schedule state
        self._schedule = None
        self._stage_index = 0
        self._stage_start_ms = 0
        self._run_start_ms = 0
        self._running = False
        self._cooldown = False

        # Vent control
        self._vent_reason = None  # "rh_high", "temp_high", or None

        # Last readings cache
        self._last_sensor_read = None
        self._last_mc_read = None

        # Heater fault tracking
        self._heater_on_since = None  # ticks_ms when heater turned on
        self._heater_on_temp = None   # temp at heater-on time
        self._heater_fault_alerted = False

        # Out-of-range tracking
        self._temp_oor_since = None  # ticks_ms when temp went OOR
        self._rh_oor_since = None    # ticks_ms when RH went OOR

        # Alert rate limiting: {alert_type: ticks_ms}
        self._last_alert_ts = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, schedule_path):
        """
        Load a schedule JSON from the SD card.

        schedule_path is relative to SD mount point (e.g. "schedules/maple_1in.json").
        Returns True on success, False on any failure.
        """
        try:
            text = self._sdcard.read_text(schedule_path)
            if text is None:
                self._log_event(f"Failed to read schedule file: {schedule_path}")
                return False

            sched = json.loads(text)

            # Validate top-level fields
            if "stages" not in sched or not isinstance(sched["stages"], list):
                self._log_event("Schedule missing 'stages' array")
                return False

            if len(sched["stages"]) == 0:
                self._log_event("Schedule has no stages")
                return False

            # Validate each stage
            for i, stage in enumerate(sched["stages"]):
                ok = self._validate_stage(stage, i)
                if not ok:
                    return False

            self._schedule = sched
            name = sched.get("name", "unknown")
            n = len(sched["stages"])
            self._log_event(f"Schedule loaded: {name} ({n} stages)")
            return True

        except (ValueError, KeyError) as e:
            self._log_event(f"Schedule parse error: {e}")
            return False
        except Exception as e:
            self._log_event(f"Schedule load error: {e}")
            return False

    def start(self):
        """Begin executing the loaded schedule from stage 0."""
        if self._schedule is None:
            raise RuntimeError("No schedule loaded")

        now = time.ticks_ms()
        self._run_start_ms = now
        self._stage_index = 0
        self._stage_start_ms = now
        self._running = True
        self._cooldown = False

        # Reset all tracking state
        self._vent_reason = None
        self._last_sensor_read = None
        self._last_mc_read = None
        self._heater_on_since = None
        self._heater_on_temp = None
        self._heater_fault_alerted = False
        self._temp_oor_since = None
        self._rh_oor_since = None
        self._last_alert_ts = {}

        # Start hardware
        self._circulation.on(CIRC_FAN_SPEED)
        self._vents.close()

        if self._logger:
            self._logger.begin_run()

        name = self._schedule.get("name", "unknown")
        self._log_event(f"Run started: {name}")

        # Check if first stage needs entry alert
        self._on_stage_entry()

    def stop(self, reason="manual"):
        """Halt the run. Safe shutdown: heater off, vents open, fans to cooldown."""
        self._running = False
        self._cooldown = True

        # Safe state
        self._heater.off()
        self._vents.open()
        self._exhaust.off()
        self._circulation.on(COOLDOWN_FAN_SPEED)
        self._vent_reason = None

        self._log_event(f"Run stopped: {reason}")

        if self._logger:
            self._logger.end_run()

    def tick(self):
        """
        Execute one control cycle. Called from main.py main loop.
        Returns immediately if no run is active.
        """
        if not self._running:
            return

        stage = self._current_stage()

        # Step 1: Read sensors
        sensor_data = self._sensors.read()
        if sensor_data is None:
            self._send_alert("sensor_failure",
                             f"ALERT;sensor_failure;stage={self._stage_index}")
            self._log_event("Sensor read failed -- skipping tick")
            return

        self._last_sensor_read = sensor_data
        temp_c = sensor_data.get("temp_lumber")
        rh_pct = sensor_data.get("rh_lumber")

        if temp_c is None or rh_pct is None:
            self._send_alert("sensor_failure",
                             f"ALERT;sensor_failure;stage={self._stage_index}")
            self._log_event("Sensor returned None values -- skipping tick")
            return

        # Step 2: Read moisture
        mc_data = None
        try:
            mc_data = self._moisture.read_with_temp_correction(temp_c)
        except Exception as e:
            self._log_event(f"Moisture read error: {e}")
        self._last_mc_read = mc_data

        # Step 3: Temperature control
        self._control_temperature(temp_c, stage)

        # Step 4: RH and overheat vent control
        self._control_vents(temp_c, rh_pct, stage)

        # Step 5: Check stage advance
        self._check_stage_advance(temp_c, rh_pct, mc_data, stage)

        # Step 6: Check alert conditions
        self._check_alerts(temp_c, rh_pct, stage)

        # Step 7: Log data record
        self._log_data(temp_c, rh_pct, sensor_data, mc_data, stage)

    @property
    def tick_interval_s(self):
        """Interval for main loop to sleep between tick() calls."""
        if self._vent_reason is not None:
            return VENT_LOOP_INTERVAL_S
        return LOOP_INTERVAL_S

    def status(self):
        """Return a snapshot of current controller state."""
        stage = self._current_stage()
        stage_elapsed_h = 0.0
        if self._running and self._stage_start_ms:
            elapsed_ms = time.ticks_diff(time.ticks_ms(), self._stage_start_ms)
            stage_elapsed_h = elapsed_ms / 3_600_000

        mc_maple = None
        mc_beech = None
        if self._last_mc_read:
            mc_maple = self._last_mc_read.get("ch1_mc_pct")
            mc_beech = self._last_mc_read.get("ch2_mc_pct")

        actual_temp = None
        actual_rh = None
        if self._last_sensor_read:
            actual_temp = self._last_sensor_read.get("temp_lumber")
            actual_rh = self._last_sensor_read.get("rh_lumber")

        return {
            "running":         self._running,
            "schedule_name":   self._schedule.get("name") if self._schedule else None,
            "stage_index":     self._stage_index,
            "stage_name":      stage["name"] if stage else "",
            "stage_type":      stage["stage_type"] if stage else "",
            "stage_elapsed_h": round(stage_elapsed_h, 2),
            "target_temp_c":   stage["target_temp_c"] if stage else None,
            "target_rh_pct":   stage["target_rh_pct"] if stage else None,
            "target_mc_pct":   stage.get("target_mc_pct") if stage else None,
            "actual_temp_c":   actual_temp,
            "actual_rh_pct":   actual_rh,
            "actual_mc_maple": mc_maple,
            "actual_mc_beech": mc_beech,
            "heater_on":       self._heater.is_on(),
            "vents_open":      self._vents.is_open(),
            "vent_reason":     self._vent_reason,
            "cooldown":        self._cooldown,
        }

    # ------------------------------------------------------------------
    # Temperature control
    # ------------------------------------------------------------------

    def _control_temperature(self, temp_c, stage):
        """Deadband heater control with fault detection."""
        target = stage["target_temp_c"]

        # Overheat venting takes priority -- heater stays off
        if self._vent_reason == "temp_high":
            if self._heater.is_on():
                self._heater.off()
                self._log_event("Heater off (overheat vent active)")
                self._heater_on_since = None
            return

        if temp_c < target - TEMP_DEADBAND_C and not self._heater.is_on():
            self._heater.on()
            self._heater_on_since = time.ticks_ms()
            self._heater_on_temp = temp_c
            self._heater_fault_alerted = False
            self._log_event(f"Heater on (temp {temp_c:.1f} < "
                            f"target {target:.1f} - {TEMP_DEADBAND_C})")

        elif temp_c > target + TEMP_DEADBAND_C and self._heater.is_on():
            self._heater.off()
            self._log_event(f"Heater off (temp {temp_c:.1f} > "
                            f"target {target:.1f} + {TEMP_DEADBAND_C})")
            self._heater_on_since = None

        # Heater fault detection
        if (self._heater.is_on() and self._heater_on_since is not None
                and not self._heater_fault_alerted):
            on_ms = time.ticks_diff(time.ticks_ms(), self._heater_on_since)
            if on_ms >= HEATER_FAULT_MIN * 60_000:
                rise = temp_c - self._heater_on_temp
                if rise < HEATER_FAULT_RISE_C:
                    msg = (f"ALERT;heater_fault;stage={self._stage_index};"
                           f"temp={temp_c:.1f};rh=0.0")
                    self._send_alert("heater_fault", msg)
                    self._log_event(
                        f"Heater fault: on {HEATER_FAULT_MIN} min, "
                        f"rise {rise:.1f} deg C < {HEATER_FAULT_RISE_C}")
                    self._heater_fault_alerted = True

    # ------------------------------------------------------------------
    # RH and overheat vent control
    # ------------------------------------------------------------------

    def _control_vents(self, temp_c, rh_pct, stage):
        """Vent-only RH and overheat temperature control."""
        target_temp = stage["target_temp_c"]
        target_rh = stage["target_rh_pct"]

        # --- Check overheat condition (highest priority) ---
        overheat = temp_c > target_temp + TEMP_DEADBAND_C

        if overheat and self._vent_reason != "temp_high":
            # Activate or upgrade to overheat venting
            self._vents.open()
            self._exhaust.on(EXHAUST_OVERHEAT_SPEED)
            if self._heater.is_on():
                self._heater.off()
                self._log_event("Heater off (overheat)")
            prev = self._vent_reason
            self._vent_reason = "temp_high"
            if prev == "rh_high":
                self._log_event("Vent reason upgraded: rh_high -> temp_high")
            else:
                self._log_event(f"Vents open (overheat: {temp_c:.1f} deg C "
                                f"> {target_temp + TEMP_DEADBAND_C:.1f})")
            return

        # --- Check overheat close condition ---
        if self._vent_reason == "temp_high":
            if temp_c < target_temp + TEMP_DEADBAND_C / 2:
                self._vents.close()
                self._exhaust.off()
                self._vent_reason = None
                self._log_event(f"Vents closed (overheat resolved: "
                                f"{temp_c:.1f} deg C)")
            return  # Don't evaluate RH while in overheat recovery

        # --- Check RH high condition ---
        rh_high = rh_pct > target_rh + RH_DEADBAND_PCT

        if rh_high and self._vent_reason is None:
            # Cold suppression: don't vent for RH if too cold
            if temp_c < target_temp - TEMP_DEADBAND_C * 2:
                self._log_event(f"RH vent suppressed (temp {temp_c:.1f} "
                                f"too cold for venting)")
                return

            self._vents.open()
            self._exhaust.on(EXHAUST_VENT_SPEED)
            self._vent_reason = "rh_high"
            self._log_event(f"Vents open (RH high: {rh_pct:.1f} pct "
                            f"> {target_rh + RH_DEADBAND_PCT:.1f})")
            return

        # --- Check RH close condition ---
        if self._vent_reason == "rh_high":
            if rh_pct < target_rh - RH_DEADBAND_PCT / 2:
                self._vents.close()
                self._exhaust.off()
                self._vent_reason = None
                self._log_event(f"Vents closed (RH resolved: {rh_pct:.1f} pct)")

    # ------------------------------------------------------------------
    # Stage advance
    # ------------------------------------------------------------------

    def _check_stage_advance(self, temp_c, rh_pct, mc_data, stage):
        """Check if current stage should advance."""
        elapsed_ms = time.ticks_diff(time.ticks_ms(), self._stage_start_ms)
        elapsed_h = elapsed_ms / 3_600_000
        stage_type = stage["stage_type"]
        min_h = stage["min_duration_h"]
        max_h = stage.get("max_duration_h")

        if stage_type == "drying":
            self._check_drying_advance(elapsed_h, min_h, max_h, mc_data,
                                       stage, temp_c, rh_pct)
        else:
            # Equalizing and conditioning: time-only
            if elapsed_h >= min_h:
                self._advance_stage(temp_c, rh_pct)

    def _check_drying_advance(self, elapsed_h, min_h, max_h, mc_data,
                              stage, temp_c, rh_pct):
        """Check advance conditions for a drying stage."""
        target_mc = stage["target_mc_pct"]

        # Check max duration timeout first
        if max_h is not None and elapsed_h >= max_h:
            # Check if MC target was met
            mc_met = self._mc_target_met(mc_data, target_mc)
            if not mc_met:
                msg = (f"ALERT;stage_goal_not_met;stage={self._stage_index};"
                       f"temp={temp_c:.1f};rh={rh_pct:.1f}")
                self._send_alert("stage_goal_not_met", msg,
                                 rate_limit=False)
                self._log_event(
                    f"Stage {self._stage_index} max duration reached "
                    f"({max_h}h) -- MC target {target_mc} not met")
            self._advance_stage(temp_c, rh_pct)
            return

        # Normal advance: min_duration met AND MC target met
        if elapsed_h < min_h:
            return

        # Check MC
        mc1 = None
        mc2 = None
        if mc_data:
            mc1 = mc_data.get("ch1_mc_pct")
            mc2 = mc_data.get("ch2_mc_pct")

        if mc1 is None and mc2 is None:
            # Both probes failed -- fall back to time-only
            self._log_event(
                f"Stage {self._stage_index}: both MC probes None, "
                f"advancing on time only after {min_h}h")
            self._advance_stage(temp_c, rh_pct)
            return

        if self._mc_target_met(mc_data, target_mc):
            self._advance_stage(temp_c, rh_pct)

    def _mc_target_met(self, mc_data, target_mc):
        """Check if both moisture probes are at or below target."""
        if mc_data is None:
            return False

        mc1 = mc_data.get("ch1_mc_pct")
        mc2 = mc_data.get("ch2_mc_pct")

        if mc1 is None and mc2 is None:
            return False

        # Both must be at or below target (if available)
        if mc1 is not None and mc1 > target_mc:
            return False
        if mc2 is not None and mc2 > target_mc:
            return False

        return True

    def _advance_stage(self, temp_c, rh_pct):
        """Move to the next stage, or complete the run."""
        stages = self._schedule["stages"]
        old_index = self._stage_index
        old_name = stages[old_index]["name"]

        # Send stage advance alert
        msg = (f"ALERT;stage_advance;stage={old_index};"
               f"temp={temp_c:.1f};rh={rh_pct:.1f}")
        self._send_alert("stage_advance", msg, rate_limit=False)
        self._log_event(f"Stage {old_index} ({old_name}) complete")

        next_index = old_index + 1
        if next_index >= len(stages):
            # Run complete
            msg = (f"ALERT;run_complete;stage={old_index};"
                   f"temp={temp_c:.1f};rh={rh_pct:.1f}")
            self._send_alert("run_complete", msg, rate_limit=False)
            self._log_event("Schedule complete -- entering cooldown")
            self.stop(reason="complete")
            return

        # Advance to next stage
        self._stage_index = next_index
        self._stage_start_ms = time.ticks_ms()

        new_stage = stages[next_index]
        self._log_event(f"Entering stage {next_index}: {new_stage['name']} "
                        f"(target {new_stage['target_temp_c']} deg C, "
                        f"{new_stage['target_rh_pct']} pct RH)")

        # Reset heater fault tracking for new stage
        if self._heater.is_on():
            self._heater_on_since = time.ticks_ms()
            self._heater_on_temp = temp_c
        self._heater_fault_alerted = False

        # Reset OOR tracking for new stage
        self._temp_oor_since = None
        self._rh_oor_since = None

        # Stage entry alerts
        self._on_stage_entry()

    def _on_stage_entry(self):
        """Send entry alerts for equalizing/conditioning stages."""
        stage = self._current_stage()
        if stage is None:
            return

        stage_type = stage["stage_type"]
        if stage_type == "equalizing":
            msg = (f"ALERT;equalizing_start;stage={self._stage_index};"
                   f"temp=0.0;rh=0.0;add water pans if needed")
            self._send_alert("equalizing_start", msg, rate_limit=False)
            self._log_event("Equalizing stage -- add water pans if needed "
                            "for target RH")

        elif stage_type == "conditioning":
            msg = (f"ALERT;conditioning_start;stage={self._stage_index};"
                   f"temp=0.0;rh=0.0;add water pans if needed")
            self._send_alert("conditioning_start", msg, rate_limit=False)
            self._log_event("Conditioning stage -- add water pans if needed "
                            "for target RH")

    # ------------------------------------------------------------------
    # Alert conditions
    # ------------------------------------------------------------------

    def _check_alerts(self, temp_c, rh_pct, stage):
        """Check out-of-range conditions and fire rate-limited alerts."""
        target_temp = stage["target_temp_c"]
        target_rh = stage["target_rh_pct"]
        now = time.ticks_ms()

        # Temperature out of range
        temp_in_band = (target_temp - TEMP_DEADBAND_C
                        <= temp_c
                        <= target_temp + TEMP_DEADBAND_C)

        if not temp_in_band:
            if self._temp_oor_since is None:
                self._temp_oor_since = now
            else:
                oor_ms = time.ticks_diff(now, self._temp_oor_since)
                if oor_ms >= OUT_OF_RANGE_ALERT_MIN * 60_000:
                    msg = (f"ALERT;temp_out_of_range;stage={self._stage_index};"
                           f"temp={temp_c:.1f};rh={rh_pct:.1f}")
                    self._send_alert("temp_out_of_range", msg)
        else:
            self._temp_oor_since = None

        # RH out of range
        rh_in_band = (target_rh - RH_DEADBAND_PCT
                      <= rh_pct
                      <= target_rh + RH_DEADBAND_PCT)

        if not rh_in_band:
            if self._rh_oor_since is None:
                self._rh_oor_since = now
            else:
                oor_ms = time.ticks_diff(now, self._rh_oor_since)
                if oor_ms >= OUT_OF_RANGE_ALERT_MIN * 60_000:
                    msg = (f"ALERT;rh_out_of_range;stage={self._stage_index};"
                           f"temp={temp_c:.1f};rh={rh_pct:.1f}")
                    self._send_alert("rh_out_of_range", msg)
        else:
            self._rh_oor_since = None

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def _log_event(self, message):
        """Log event to logger and REPL."""
        print(f"schedule: {message}")
        if self._logger:
            self._logger.event("schedule", message)

    def _log_data(self, temp_c, rh_pct, sensor_data, mc_data, stage):
        """Log a data record for this tick."""
        if not self._logger:
            return

        elapsed_ms = time.ticks_diff(time.ticks_ms(), self._stage_start_ms)
        stage_h = elapsed_ms / 3_600_000

        mc_maple = None
        mc_beech = None
        if mc_data:
            mc_maple = mc_data.get("ch1_mc_pct")
            mc_beech = mc_data.get("ch2_mc_pct")

        record = {
            "stage":       self._stage_index,
            "stage_type":  stage["stage_type"],
            "temp_lumber": temp_c,
            "rh_lumber":   rh_pct,
            "temp_intake": sensor_data.get("temp_intake"),
            "rh_intake":   sensor_data.get("rh_intake"),
            "mc_maple":    mc_maple,
            "mc_beech":    mc_beech,
            "heater":      self._heater.is_on(),
            "vents":       self._vents.is_open(),
            "vent_reason": self._vent_reason,
            "exhaust_pct": self._exhaust.speed_pct,
            "target_temp": stage["target_temp_c"],
            "target_rh":   stage["target_rh_pct"],
            "target_mc":   stage.get("target_mc_pct"),
            "stage_h":     round(stage_h, 2),
        }

        self._logger.data(record)

    def _send_alert(self, alert_type, payload_str, rate_limit=True):
        """Send a LoRa alert and log it. Rate-limited by default."""
        if rate_limit:
            now = time.ticks_ms()
            last = self._last_alert_ts.get(alert_type)
            if last is not None:
                elapsed = time.ticks_diff(now, last)
                if elapsed < OUT_OF_RANGE_ALERT_MIN * 60_000:
                    return

        # Send via LoRa
        try:
            self._lora.send(payload_str.encode())
        except Exception as e:
            print(f"schedule: LoRa send error: {e}")

        # Update rate limit timestamp
        self._last_alert_ts[alert_type] = time.ticks_ms()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _current_stage(self):
        """Return the current stage dict, or None."""
        if self._schedule is None:
            return None
        stages = self._schedule["stages"]
        if self._stage_index < len(stages):
            return stages[self._stage_index]
        return None

    def _validate_stage(self, stage, index):
        """Validate a single stage dict. Returns True if valid."""
        required = ("name", "stage_type", "target_temp_c",
                    "target_rh_pct", "min_duration_h")

        for field in required:
            if field not in stage:
                self._log_event(
                    f"Stage {index} missing required field: {field}")
                return False

        stype = stage["stage_type"]
        if stype not in VALID_STAGE_TYPES:
            self._log_event(
                f"Stage {index} unknown stage_type: {stype}")
            return False

        if stype == "drying":
            mc = stage.get("target_mc_pct")
            if mc is None or not isinstance(mc, (int, float)):
                self._log_event(
                    f"Stage {index} (drying) requires numeric target_mc_pct")
                return False

        if stype in ("equalizing", "conditioning"):
            mc = stage.get("target_mc_pct")
            if mc is not None:
                self._log_event(
                    f"Stage {index} ({stype}) target_mc_pct must be null")
                return False

        return True


# --- Unit tests ---
def test():
    print("=== KilnSchedule unit test ===")
    all_passed = True

    # --- Mock classes for testing ---
    class MockSDCard:
        def __init__(self):
            self._files = {}
        def set_file(self, path, content):
            self._files[path] = content
        def read_text(self, path):
            return self._files.get(path)

    class MockSensors:
        def __init__(self):
            self.temp_lumber = 40.0
            self.rh_lumber = 60.0
        def read(self):
            return {
                "temp_lumber": self.temp_lumber,
                "rh_lumber": self.rh_lumber,
                "temp_intake": 25.0,
                "rh_intake": 50.0,
            }

    class MockMoisture:
        def __init__(self):
            self.mc1 = 20.0
            self.mc2 = 20.0
        def read_with_temp_correction(self, temp_c):
            return {
                "ch1_mc_pct": self.mc1,
                "ch2_mc_pct": self.mc2,
                "ch1_ohms": 50000,
                "ch2_ohms": 50000,
            }

    class MockHeater:
        def __init__(self):
            self._on = False
        def on(self):
            self._on = True
        def off(self):
            self._on = False
        def is_on(self):
            return self._on

    class MockExhaust:
        def __init__(self):
            self._running = False
            self.speed_pct = 0
        def on(self, speed):
            self._running = True
            self.speed_pct = speed
        def off(self):
            self._running = False
            self.speed_pct = 0
        @property
        def is_running(self):
            return self._running

    class MockCirculation:
        def __init__(self):
            self._running = False
            self._speed = 0
        def on(self, speed=100):
            self._running = True
            self._speed = speed
        def off(self):
            self._running = False
            self._speed = 0
        def set_speed(self, speed):
            self._speed = speed
        @property
        def is_running(self):
            return self._running
        @property
        def speed(self):
            return self._speed

    class MockVents:
        def __init__(self):
            self._open = False
        def open(self):
            self._open = True
        def close(self):
            self._open = False
        def is_open(self):
            return self._open

    class MockLoRa:
        def __init__(self):
            self.sends = []
        def send(self, payload):
            self.sends.append(payload)
            return True

    class MockLogger:
        def __init__(self):
            self.events = []
            self.data_records = []
            self._run_active = False
        def event(self, source, message, level="INFO"):
            self.events.append((source, message, level))
        def data(self, record):
            self.data_records.append(record)
        def begin_run(self):
            self._run_active = True
        def end_run(self):
            self._run_active = False

    def make_controller(sd=None, sensors=None, moisture=None, heater=None,
                        exhaust=None, circ=None, vents=None, lora=None,
                        logger=None):
        return KilnSchedule(
            sdcard=sd or MockSDCard(),
            sensors=sensors or MockSensors(),
            moisture=moisture or MockMoisture(),
            heater=heater or MockHeater(),
            exhaust=exhaust or MockExhaust(),
            circulation=circ or MockCirculation(),
            vents=vents or MockVents(),
            lora=lora or MockLoRa(),
            logger=logger,
        )

    VALID_SCHEDULE = json.dumps({
        "name": "Test Schedule",
        "species": "maple",
        "thickness_in": 1.0,
        "stages": [
            {
                "name": "Drying",
                "stage_type": "drying",
                "target_temp_c": 50,
                "target_rh_pct": 60,
                "target_mc_pct": 15.0,
                "min_duration_h": 1,
                "max_duration_h": 10
            },
            {
                "name": "Equalizing",
                "stage_type": "equalizing",
                "target_temp_c": 60,
                "target_rh_pct": 50,
                "target_mc_pct": None,
                "min_duration_h": 2,
                "max_duration_h": None
            },
            {
                "name": "Conditioning",
                "stage_type": "conditioning",
                "target_temp_c": 60,
                "target_rh_pct": 70,
                "target_mc_pct": None,
                "min_duration_h": 1,
                "max_duration_h": None
            }
        ]
    })

    # --- Test 1: load() with valid JSON ---
    sd = MockSDCard()
    sd.set_file("test.json", VALID_SCHEDULE)
    ctrl = make_controller(sd=sd)
    result = ctrl.load("test.json")
    passed = result is True and ctrl._schedule is not None
    passed = passed and len(ctrl._schedule["stages"]) == 3
    print(f"  {'PASS' if passed else 'FAIL'} - load() valid JSON returns True, 3 stages")
    all_passed &= passed

    # --- Test 2: load() missing file ---
    ctrl2 = make_controller()
    result = ctrl2.load("nonexistent.json")
    passed = result is False
    print(f"  {'PASS' if passed else 'FAIL'} - load() missing file returns False")
    all_passed &= passed

    # --- Test 3: load() malformed JSON ---
    sd3 = MockSDCard()
    sd3.set_file("bad.json", "{not valid json")
    ctrl3 = make_controller(sd=sd3)
    result = ctrl3.load("bad.json")
    passed = result is False
    print(f"  {'PASS' if passed else 'FAIL'} - load() malformed JSON returns False")
    all_passed &= passed

    # --- Test 4: load() drying stage missing target_mc_pct ---
    bad_sched = json.dumps({
        "name": "Bad",
        "stages": [{
            "name": "S1", "stage_type": "drying",
            "target_temp_c": 50, "target_rh_pct": 60,
            "target_mc_pct": None,
            "min_duration_h": 1, "max_duration_h": 10
        }]
    })
    sd4 = MockSDCard()
    sd4.set_file("bad_mc.json", bad_sched)
    ctrl4 = make_controller(sd=sd4)
    result = ctrl4.load("bad_mc.json")
    passed = result is False
    print(f"  {'PASS' if passed else 'FAIL'} - load() drying with null MC returns False")
    all_passed &= passed

    # --- Test 5: load() equalizing with non-null target_mc_pct ---
    bad_eq = json.dumps({
        "name": "Bad EQ",
        "stages": [{
            "name": "EQ", "stage_type": "equalizing",
            "target_temp_c": 60, "target_rh_pct": 50,
            "target_mc_pct": 10.0,
            "min_duration_h": 2, "max_duration_h": None
        }]
    })
    sd5 = MockSDCard()
    sd5.set_file("bad_eq.json", bad_eq)
    ctrl5 = make_controller(sd=sd5)
    result = ctrl5.load("bad_eq.json")
    passed = result is False
    print(f"  {'PASS' if passed else 'FAIL'} - load() equalizing with non-null MC returns False")
    all_passed &= passed

    # --- Test 6: start() without loaded schedule ---
    ctrl6 = make_controller()
    try:
        ctrl6.start()
        passed = False
    except RuntimeError:
        passed = True
    print(f"  {'PASS' if passed else 'FAIL'} - start() without schedule raises RuntimeError")
    all_passed &= passed

    # --- Test 7: status() returns expected keys ---
    sd7 = MockSDCard()
    sd7.set_file("test.json", VALID_SCHEDULE)
    ctrl7 = make_controller(sd=sd7)
    ctrl7.load("test.json")
    st = ctrl7.status()
    expected_keys = {
        "running", "schedule_name", "stage_index", "stage_name",
        "stage_type", "stage_elapsed_h", "target_temp_c", "target_rh_pct",
        "target_mc_pct", "actual_temp_c", "actual_rh_pct",
        "actual_mc_maple", "actual_mc_beech", "heater_on", "vents_open",
        "vent_reason", "cooldown"
    }
    passed = set(st.keys()) == expected_keys
    passed = passed and st["running"] is False
    passed = passed and st["stage_type"] == "drying"
    print(f"  {'PASS' if passed else 'FAIL'} - status() has correct keys and values")
    all_passed &= passed

    # After start
    ctrl7.start()
    st2 = ctrl7.status()
    passed = st2["running"] is True and st2["schedule_name"] == "Test Schedule"
    print(f"  {'PASS' if passed else 'FAIL'} - status() after start: running=True")
    all_passed &= passed
    ctrl7.stop()

    # --- Test 8: stop() leaves safe state ---
    sd8 = MockSDCard()
    sd8.set_file("test.json", VALID_SCHEDULE)
    heater8 = MockHeater()
    vents8 = MockVents()
    circ8 = MockCirculation()
    exhaust8 = MockExhaust()
    ctrl8 = make_controller(sd=sd8, heater=heater8, vents=vents8,
                            circ=circ8, exhaust=exhaust8)
    ctrl8.load("test.json")
    ctrl8.start()
    heater8.on()  # simulate heater was on
    ctrl8.stop()
    passed = (not heater8.is_on() and vents8.is_open()
              and circ8.is_running and not exhaust8.is_running)
    print(f"  {'PASS' if passed else 'FAIL'} - stop(): heater off, vents open, "
          f"circ on, exhaust off")
    all_passed &= passed

    # --- Test 9: Temperature control deadband ---
    sd9 = MockSDCard()
    sd9.set_file("test.json", VALID_SCHEDULE)
    sensors9 = MockSensors()
    heater9 = MockHeater()
    ctrl9 = make_controller(sd=sd9, sensors=sensors9, heater=heater9)
    ctrl9.load("test.json")
    ctrl9.start()

    # Target is 50C, deadband 2C. Temp 47 -> heater should turn on
    sensors9.temp_lumber = 47.0
    sensors9.rh_lumber = 60.0
    ctrl9.tick()
    passed = heater9.is_on()
    print(f"  {'PASS' if passed else 'FAIL'} - Temp {sensors9.temp_lumber} < target-deadband: "
          f"heater on")
    all_passed &= passed

    # Temp 53 -> heater should turn off
    sensors9.temp_lumber = 53.0
    ctrl9.tick()
    passed = not heater9.is_on()
    print(f"  {'PASS' if passed else 'FAIL'} - Temp {sensors9.temp_lumber} > target+deadband: "
          f"heater off")
    all_passed &= passed
    ctrl9.stop()

    # --- Test 10: Overheat vent control ---
    sd10 = MockSDCard()
    sd10.set_file("test.json", VALID_SCHEDULE)
    sensors10 = MockSensors()
    heater10 = MockHeater()
    vents10 = MockVents()
    exhaust10 = MockExhaust()
    ctrl10 = make_controller(sd=sd10, sensors=sensors10, heater=heater10,
                             vents=vents10, exhaust=exhaust10)
    ctrl10.load("test.json")
    ctrl10.start()

    # Simulate overheat: temp above target + deadband
    sensors10.temp_lumber = 55.0  # target 50 + deadband 2 = 52
    sensors10.rh_lumber = 60.0
    heater10.on()
    ctrl10.tick()
    passed = (vents10.is_open() and not heater10.is_on()
              and ctrl10._vent_reason == "temp_high")
    print(f"  {'PASS' if passed else 'FAIL'} - Overheat: vents open, heater off, "
          f"reason=temp_high")
    all_passed &= passed

    # Cool down below target + deadband/2 = 51
    sensors10.temp_lumber = 50.5
    ctrl10.tick()
    passed = not vents10.is_open() and ctrl10._vent_reason is None
    print(f"  {'PASS' if passed else 'FAIL'} - Overheat resolved: vents closed")
    all_passed &= passed
    ctrl10.stop()

    # --- Test 11: Equalizing entry alert ---
    eq_sched = json.dumps({
        "name": "EQ Test",
        "stages": [{
            "name": "EQ",
            "stage_type": "equalizing",
            "target_temp_c": 60,
            "target_rh_pct": 50,
            "target_mc_pct": None,
            "min_duration_h": 0,
            "max_duration_h": None
        }]
    })
    sd11 = MockSDCard()
    sd11.set_file("eq.json", eq_sched)
    lora11 = MockLoRa()
    ctrl11 = make_controller(sd=sd11, lora=lora11)
    ctrl11.load("eq.json")
    ctrl11.start()

    # Check that equalizing_start alert was sent
    alert_found = False
    for payload in lora11.sends:
        text = payload.decode() if isinstance(payload, bytes) else payload
        if "equalizing_start" in text and "water pans" in text:
            alert_found = True
            break
    passed = alert_found
    print(f"  {'PASS' if passed else 'FAIL'} - Equalizing entry: LoRa alert with "
          f"water pan reminder")
    all_passed &= passed
    ctrl11.stop()

    # --- Test 12: Constructor rejects None arguments ---
    try:
        KilnSchedule(None, MockSensors(), MockMoisture(), MockHeater(),
                     MockExhaust(), MockCirculation(), MockVents(), MockLoRa())
        passed = False
    except ValueError:
        passed = True
    print(f"  {'PASS' if passed else 'FAIL'} - Constructor rejects None sdcard")
    all_passed &= passed

    print(f"\n{'All tests passed!' if all_passed else 'Some tests FAILED'}")
    return all_passed


if __name__ == "__main__":
    test()
