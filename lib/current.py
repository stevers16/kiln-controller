# lib/current.py
#
# Reads DC current, bus voltage, and power from an INA219 module via I2C0.
# Two instances are used: one for the 12V rail (0x40) and one for the 5V rail (0x41).
#
# The INA219 is driven via raw I2C register reads/writes - no external library.
#
# Calibration (0.1ohm shunt, 3.2A max):
#   Cal = trunc(0.04096 / (0.1 * 0.0001)) = 4096 = 0x1000
#   Current LSB = 0.1mA/bit
#   Power LSB   = 2mW/bit
#
# Wiring summary:
#   INA219 #1 (12V rail): address 0x40, A0/A1 unsoldered
#   INA219 #2 (5V rail):  address 0x41, A0 bridged, A1 unsoldered
#   Both powered from Pico 3.3V rail; I2C0 SDA=GP0, SCL=GP1

import machine
import time

# --- INA219 registers ---
_REG_CONFIG  = 0x00
_REG_SHUNT   = 0x01
_REG_BUS     = 0x02
_REG_POWER   = 0x03
_REG_CURRENT = 0x04
_REG_CAL     = 0x05

# Calibration value for 0.1ohm shunt, Current LSB = 0.1mA
_CAL_VALUE   = 0x1000   # 4096

# Configuration: reset defaults (32V range, PGA /8, BADC 12-bit, SADC 12-bit, continuous)
_CONFIG_DEFAULT = 0x399F


class CurrentMonitor:
    """
    Driver for a single INA219 current/power monitor on a shared I2C bus.

    Two instances are expected in the kiln:
        mon_12v = CurrentMonitor(i2c, 0x40, "12V", logger=logger)
        mon_5v  = CurrentMonitor(i2c, 0x41, "5V",  logger=logger)

    The I2C instance is created externally (shared with SHT31 sensors)
    and passed in - not created here.
    """

    def __init__(self, i2c, address, label, logger=None):
        self._i2c    = i2c
        self._addr   = address
        self._label  = label
        self._logger = logger
        self._source = f"current_{label.replace('V', 'v')}"  # "current_12v" / "current_5v"
        self._ready  = False

        # Fault contract
        self.fault = False
        self.fault_code = None
        self.fault_message = None
        self.fault_tier = "fault"
        self.fault_last_checked_ms = None

        self._init_device()

        # Latch fault immediately if init failed
        if not self._ready:
            code = f"CURRENT_{label.upper()}_FAIL"
            self.fault = True
            self.fault_code = code
            self.fault_message = f"INA219 init failed at 0x{address:02X}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read(self):
        """
        Read bus voltage, current, and power from the INA219.

        Returns:
            dict with keys: bus_voltage_V, current_mA, power_mW, label
            None on I2C failure.
        """
        try:
            bus_raw     = self._read_reg(_REG_BUS)
            current_raw = self._read_reg(_REG_CURRENT)
            power_raw   = self._read_reg(_REG_POWER)
        except Exception as e:
            if self._logger:
                self._logger.event(self._source, f"I2C read error: {e}", level="ERROR")
            print(f"[{self._source}] WARNING: read failed: {e}")
            return None

        # Bus voltage: bits 15:3, LSB = 4mV
        bus_voltage_V = ((bus_raw >> 3) & 0x1FFF) * 0.004

        # Current: signed 16-bit, LSB = 0.1mA
        if current_raw > 32767:
            current_raw -= 65536
        current_mA = current_raw * 0.1

        # Power: unsigned, LSB = 2mW
        power_mW = power_raw * 2.0

        return {
            "bus_voltage_V": bus_voltage_V,
            "current_mA":    current_mA,
            "power_mW":      power_mW,
            "label":         self._label,
        }

    def check_range(self, min_mA, max_mA):
        """
        Read current and check it falls within [min_mA, max_mA].

        Returns:
            True  if in range
            False if out of range (also logs WARN)
            None  on read failure
        """
        result = self.read()
        if result is None:
            return None

        current_mA = result["current_mA"]
        in_range = min_mA <= current_mA <= max_mA

        if not in_range:
            msg = (f"Current out of range: {current_mA:.1f}mA "
                   f"(expected {min_mA}-{max_mA}mA)")
            if self._logger:
                self._logger.event(self._source, msg, level="WARN")
            print(f"[{self._source}] WARN: {msg}")

        return in_range

    def check_health(self):
        """Periodic self-check. Returns True if faulted."""
        self.fault_last_checked_ms = time.ticks_ms()
        if not self._ready:
            # Init failure is already latched
            return self.fault
        # Try a read to confirm I2C is still working
        result = self.read()
        if result is None:
            self.fault = True
            code = f"CURRENT_{self._label.upper()}_FAIL"
            self.fault_code = code
            self.fault_message = f"INA219 read failed at 0x{self._addr:02X}"
        else:
            self.fault = False
            self.fault_code = None
            self.fault_message = None
        return self.fault

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _init_device(self):
        """Write calibration and configuration registers. Silent fail."""
        try:
            self._write_reg(_REG_CAL, _CAL_VALUE)
            self._write_reg(_REG_CONFIG, _CONFIG_DEFAULT)
            self._ready = True
            if self._logger:
                self._logger.event(self._source,
                                   f"INA219 at 0x{self._addr:02X} initialised ({self._label} rail)")
        except Exception as e:
            print(f"[{self._source}] WARNING: init failed: {e}")
            if self._logger:
                self._logger.event(self._source,
                                   f"INA219 init failed at 0x{self._addr:02X}: {e}",
                                   level="ERROR")

    def _write_reg(self, reg, value):
        """Write a 16-bit big-endian value to an INA219 register."""
        data = bytes([reg, (value >> 8) & 0xFF, value & 0xFF])
        self._i2c.writeto(self._addr, data)

    def _read_reg(self, reg):
        """Read a 16-bit big-endian value from an INA219 register."""
        self._i2c.writeto(self._addr, bytes([reg]))
        data = self._i2c.readfrom(self._addr, 2)
        return (data[0] << 8) | data[1]


# --- Unit test (hardware-in-the-loop) ---
def test():
    print("=== CurrentMonitor unit test ===")
    i2c = machine.I2C(0, sda=machine.Pin(0), scl=machine.Pin(1), freq=400_000)
    all_passed = True

    for address, label, volt_min, volt_max in (
        (0x40, "12V", 11.0, 13.0),
        (0x41, "5V",  4.5,  5.5),
    ):
        print(f"\n  -- {label} rail (0x{address:02X}) --")

        # --- Test 1: device detected on I2C bus ---
        devices = i2c.scan()
        passed = address in devices
        print(f"  {'PASS' if passed else 'FAIL'} - Device 0x{address:02X} detected on I2C bus")
        all_passed &= passed
        if not passed:
            print(f"  (skipping remaining {label} tests - device not found)")
            continue

        mon = CurrentMonitor(i2c, address, label)

        # --- Test 2: read() returns correct dict keys ---
        result = mon.read()
        passed = (result is not None and
                  "bus_voltage_V" in result and
                  "current_mA"    in result and
                  "power_mW"      in result and
                  "label"         in result)
        print(f"  {'PASS' if passed else 'FAIL'} - read() returns dict with expected keys")
        all_passed &= passed

        if result is None:
            print(f"  (skipping voltage/current tests - read() returned None)")
            continue

        # --- Test 3: bus voltage in plausible range ---
        v = result["bus_voltage_V"]
        passed = volt_min <= v <= volt_max
        print(f"  {'PASS' if passed else 'FAIL'} - Bus voltage {v:.3f}V within {volt_min}-{volt_max}V")
        all_passed &= passed

        # --- Test 4: idle current - log value, no assertion ---
        mA = result["current_mA"]
        print(f"  INFO  - Idle current: {mA:.1f}mA  (no assertion - load-dependent)")
        print(f"  INFO  - Power: {result['power_mW']:.1f}mW")

        # --- Test 5: label matches ---
        passed = result["label"] == label
        print(f"  {'PASS' if passed else 'FAIL'} - label == '{label}'")
        all_passed &= passed

    print(f"\n{'All tests passed!' if all_passed else 'Some tests FAILED'}")
    return all_passed


if __name__ == "__main__":
    test()
