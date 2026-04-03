# lib/lora.py
#
# Mock LoRa transmitter driver for AI-Thinker Ra-02 (SX1278, 433 MHz).
# This is a mock implementation for development while hardware is on order.
# Replace with real SPI/SX1278 register access when Ra-02 modules arrive.
#
# The real driver will use SPI1 on GP10-GP13 with RST on GP28.
# DIO0 is not connected on the Pico side -- TX completion uses register
# polling (RegIrqFlags bit 3, TxDone).
#
# Wiring summary (from LORA_TELEMETRY_SPEC.md):
#   Ra-02 SCK  -> GP10 (SPI1 SCK)
#   Ra-02 MOSI -> GP11 (SPI1 TX)
#   Ra-02 MISO -> GP12 (SPI1 RX)
#   Ra-02 NSS  -> GP13 (SPI1 CS, active low)
#   Ra-02 RST  -> GP28 (active low)
#   Ra-02 VCC  -> 3.3V (Pin 36)
#   Ra-02 GND  -> GND (Pin 38)

import time

try:
    import ujson as json
except ImportError:
    import json

# --- Constants ---
SPI_ID          = 1
SCK_PIN         = 10
MOSI_PIN        = 11
MISO_PIN        = 12
CS_PIN          = 13
RST_PIN         = 28
FREQUENCY       = 433_000_000
BANDWIDTH       = 125_000
SPREADING_FACTOR = 9
CODING_RATE     = 5        # 4/5
TX_POWER_DBM    = 17
PREAMBLE_LEN    = 8
TX_TIMEOUT_MS   = 2000
HEARTBEAT_S     = 30
ALERT_RETRIES   = 3
ALERT_RETRY_S   = 2

# Mock simulated TX airtime (ms per byte at SF9/125kHz, approximate)
_MOCK_AIRTIME_MS_PER_BYTE = 3


class LoRa:
    """
    Mock LoRa transmitter (Ra-02 / SX1278, 433 MHz).

    Simulates the send interface without real SPI hardware. All sends
    succeed and are logged. Replace with real SX1278 register access
    when hardware arrives.
    """

    def __init__(self, spi_id=SPI_ID, sck=SCK_PIN, mosi=MOSI_PIN,
                 miso=MISO_PIN, cs=CS_PIN, rst=RST_PIN,
                 frequency=FREQUENCY, logger=None):
        self._spi_id = spi_id
        self._sck = sck
        self._mosi = mosi
        self._miso = miso
        self._cs = cs
        self._rst = rst
        self._frequency = frequency
        self._logger = logger

        self._mock = True
        self._tx_count = 0
        self._last_payload = None
        self._initialised = False

        # Simulate hardware init
        try:
            self._init_radio()
            self._initialised = True
        except Exception as e:
            print(f"LoRa: init failed - {e}")

    def _init_radio(self):
        """Mock radio initialisation (replaces SPI/register setup)."""
        # Real implementation will:
        #   1. Init SPI(spi_id, baudrate=1_000_000, sck=Pin(sck), ...)
        #   2. Pulse RST low for 10ms then wait 10ms
        #   3. Verify SX1278 version register (0x42, expect 0x12)
        #   4. Set sleep mode, configure frequency, BW, SF, CR, TX power
        #   5. Set preamble length
        print(f"LoRa: mock init (SPI{self._spi_id}, "
              f"{self._frequency / 1_000_000:.1f} MHz, "
              f"SF{SPREADING_FACTOR}, "
              f"{TX_POWER_DBM} dBm)")
        if self._logger:
            self._logger.event("lora",
                               f"Mock init OK - {self._frequency / 1_000_000:.1f} MHz "
                               f"SF{SPREADING_FACTOR} {TX_POWER_DBM} dBm")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send(self, payload):
        """
        Transmit raw bytes. Returns True on success, False on timeout.

        Real implementation will:
          1. Write payload to SX1278 FIFO
          2. Set mode to TX (RegOpMode = 0x83)
          3. Poll RegIrqFlags (0x12) for TxDone (bit 3) at 5ms intervals
          4. Clear IRQ flags (write 0xFF to 0x12)
          5. Return to sleep mode (RegOpMode = 0x80)
        """
        if not self._initialised:
            print("LoRa: send failed - not initialised")
            return False

        if not isinstance(payload, (bytes, bytearray)):
            print("LoRa: send failed - payload must be bytes")
            return False

        payload_len = len(payload)
        if payload_len == 0 or payload_len > 255:
            print(f"LoRa: send failed - invalid length {payload_len}")
            return False

        # Simulate TX airtime
        airtime_ms = payload_len * _MOCK_AIRTIME_MS_PER_BYTE
        time.sleep_ms(min(airtime_ms, 50))  # cap mock delay at 50ms

        self._tx_count += 1
        self._last_payload = payload

        if self._logger:
            self._logger.event("lora",
                               f"TX #{self._tx_count} ({payload_len} bytes)")

        return True

    def send_telemetry(self, data):
        """
        Serialise dict to JSON and transmit. Returns True/False.

        Expected keys (matching Pi4 SQLite telemetry schema):
            ts, stage, temp_lumber, temp_intake, humidity_lumber,
            humidity_intake, mc_channel_1, mc_channel_2, exhaust_fan_rpm,
            exhaust_fan_pct, circ_fan_on, heater_on, vent_open
        """
        try:
            payload = json.dumps(data).encode()
        except Exception as e:
            print(f"LoRa: telemetry serialise failed - {e}")
            if self._logger:
                self._logger.event("lora",
                                   f"Telemetry serialise failed - {e}",
                                   level="WARNING")
            return False

        return self.send(payload)

    def send_alert(self, code, message):
        """
        Transmit a fault alert. Retries up to ALERT_RETRIES times
        with ALERT_RETRY_S spacing on failure.

        Alert codes: OVER_TEMP, SENSOR_FAIL, FAN_STALL, HEATER_TIMEOUT,
                     SD_FAIL, LORA_TIMEOUT, STAGE_COMPLETE, SCHEDULE_DONE
        """
        alert = {
            "ts": time.time(),
            "code": code,
            "message": message,
        }

        try:
            payload = json.dumps(alert).encode()
        except Exception as e:
            print(f"LoRa: alert serialise failed - {e}")
            if self._logger:
                self._logger.event("lora",
                                   f"Alert serialise failed - {e}",
                                   level="WARNING")
            return False

        for attempt in range(1, ALERT_RETRIES + 1):
            if self.send(payload):
                if self._logger:
                    self._logger.event("lora",
                                       f"Alert {code} sent (attempt {attempt})")
                return True
            if attempt < ALERT_RETRIES:
                time.sleep(ALERT_RETRY_S)

        if self._logger:
            self._logger.event("lora",
                               f"Alert {code} failed after {ALERT_RETRIES} attempts",
                               level="WARNING")
        return False

    def reset(self):
        """
        Pulse the RST pin to reset the SX1278.

        Real implementation will drive RST low for 10ms, then wait 10ms.
        """
        print("LoRa: mock reset")
        if self._logger:
            self._logger.event("lora", "Radio reset (mock)")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_mock(self):
        """True if running mock (no real hardware)."""
        return self._mock

    @property
    def is_initialised(self):
        """True if radio initialisation succeeded."""
        return self._initialised

    @property
    def tx_count(self):
        """Total number of successful transmissions."""
        return self._tx_count

    @property
    def last_payload(self):
        """Last transmitted payload (bytes), or None."""
        return self._last_payload


# --- Unit test ---
def test():
    print("=== LoRa mock unit test ===")
    all_passed = True

    # --- Test 1: init defaults ---
    lora = LoRa()
    passed = lora.is_initialised and lora.is_mock
    print(f"  {'PASS' if passed else 'FAIL'} - Init: initialised=True, mock=True")
    all_passed &= passed

    # --- Test 2: tx_count starts at 0 ---
    passed = lora.tx_count == 0 and lora.last_payload is None
    print(f"  {'PASS' if passed else 'FAIL'} - Init: tx_count=0, last_payload=None")
    all_passed &= passed

    # --- Test 3: send raw bytes ---
    result = lora.send(b"hello lora")
    passed = result is True and lora.tx_count == 1
    print(f"  {'PASS' if passed else 'FAIL'} - send(bytes) returns True, tx_count=1")
    all_passed &= passed

    # --- Test 4: last_payload tracks ---
    passed = lora.last_payload == b"hello lora"
    print(f"  {'PASS' if passed else 'FAIL'} - last_payload matches sent data")
    all_passed &= passed

    # --- Test 5: send rejects non-bytes ---
    result = lora.send("not bytes")
    passed = result is False and lora.tx_count == 1
    print(f"  {'PASS' if passed else 'FAIL'} - send(str) returns False, tx_count unchanged")
    all_passed &= passed

    # --- Test 6: send rejects empty payload ---
    result = lora.send(b"")
    passed = result is False
    print(f"  {'PASS' if passed else 'FAIL'} - send(empty) returns False")
    all_passed &= passed

    # --- Test 7: send_telemetry dict ---
    data = {"ts": 1700000000, "temp_lumber": 52.3, "stage": "drying"}
    result = lora.send_telemetry(data)
    passed = result is True and lora.tx_count == 2
    print(f"  {'PASS' if passed else 'FAIL'} - send_telemetry(dict) returns True")
    all_passed &= passed

    # --- Test 8: telemetry payload is valid JSON ---
    try:
        decoded = json.loads(lora.last_payload)
        passed = decoded["temp_lumber"] == 52.3
    except Exception:
        passed = False
    print(f"  {'PASS' if passed else 'FAIL'} - Telemetry payload is valid JSON")
    all_passed &= passed

    # --- Test 9: send_alert with retries ---
    result = lora.send_alert("OVER_TEMP", "Temp 85 deg C exceeds limit")
    passed = result is True
    print(f"  {'PASS' if passed else 'FAIL'} - send_alert returns True")
    all_passed &= passed

    # --- Test 10: alert payload contains code ---
    try:
        decoded = json.loads(lora.last_payload)
        passed = decoded["code"] == "OVER_TEMP" and "message" in decoded
    except Exception:
        passed = False
    print(f"  {'PASS' if passed else 'FAIL'} - Alert payload has code and message")
    all_passed &= passed

    # --- Test 11: logger integration ---
    class MockLogger:
        def __init__(self):
            self.events = []
        def event(self, source, message, level="INFO"):
            self.events.append((source, message, level))

    mock_log = MockLogger()
    lora2 = LoRa(logger=mock_log)
    lora2.send(b"test")
    lora2.send_telemetry({"ts": 0})
    passed = len(mock_log.events) >= 3  # init + send + telemetry send
    passed = passed and all(e[0] == "lora" for e in mock_log.events)
    print(f"  {'PASS' if passed else 'FAIL'} - Logger receives events with source='lora'")
    all_passed &= passed

    # --- Test 12: reset does not crash ---
    try:
        lora.reset()
        passed = True
    except Exception:
        passed = False
    print(f"  {'PASS' if passed else 'FAIL'} - reset() completes without error")
    all_passed &= passed

    print(f"\n{'All tests passed!' if all_passed else 'Some tests FAILED'}")
    return all_passed


if __name__ == "__main__":
    test()
