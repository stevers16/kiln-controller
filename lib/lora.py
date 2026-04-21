# lib/lora.py
#
# LoRa transmitter driver for AI-Thinker Ra-02 (SX1278, 433 MHz).
# TX-only -- no receive path on the Pico side. DIO0 is not connected;
# TX completion is confirmed by polling RegIrqFlags (bit 3, TxDone).
#
# Wiring (from lora_telemetry_spec.md):
#   Ra-02 SCK  -> GP10 (SPI1 SCK)
#   Ra-02 MOSI -> GP11 (SPI1 TX)
#   Ra-02 MISO -> GP12 (SPI1 RX)
#   Ra-02 NSS  -> GP13 (SPI1 CS, active low)
#   Ra-02 RST  -> GP28 (active low)
#   Ra-02 DIO0 -> not connected
#   Ra-02 VCC  -> 3.3V (Pin 36)
#   Ra-02 GND  -> GND (Pin 38)

import machine
import time

try:
    import ujson as json
except ImportError:
    import json

# --- Pin defaults ---
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
TX_POLL_MS      = 5
ALERT_RETRIES   = 3
ALERT_RETRY_S   = 2

# --- SX1278 register addresses ---
_REG_FIFO           = 0x00
_REG_OP_MODE        = 0x01
_REG_FRF_MSB        = 0x06
_REG_FRF_MID        = 0x07
_REG_FRF_LSB        = 0x08
_REG_PA_CONFIG      = 0x09
_REG_OCP            = 0x0B
_REG_LNA            = 0x0C
_REG_FIFO_ADDR_PTR  = 0x0D
_REG_FIFO_TX_BASE   = 0x0E
_REG_IRQ_FLAGS      = 0x12
_REG_MODEM_CONFIG_1 = 0x1D
_REG_MODEM_CONFIG_2 = 0x1E
_REG_PREAMBLE_MSB   = 0x20
_REG_PREAMBLE_LSB   = 0x21
_REG_PAYLOAD_LENGTH = 0x22
_REG_MODEM_CONFIG_3 = 0x26
_REG_SYNC_WORD      = 0x39
_REG_DIO_MAPPING_1  = 0x40
_REG_VERSION        = 0x42
_REG_PA_DAC         = 0x4D

# --- SX1278 mode values ---
_MODE_SLEEP         = 0x80  # LoRa + sleep
_MODE_STDBY         = 0x81  # LoRa + standby
_MODE_TX            = 0x83  # LoRa + TX

# --- IRQ flag masks ---
_IRQ_TX_DONE        = 0x08  # bit 3

# --- SX1278 expected version ---
_EXPECTED_VERSION   = 0x12

# --- Crystal oscillator frequency ---
_FXOSC = 32_000_000
_FSTEP = _FXOSC / (1 << 19)  # 61.035 Hz


class LoRa:
    """
    LoRa transmitter driver for Ra-02 / SX1278, 433 MHz.

    TX-only. DIO0 is not connected -- TX completion is confirmed by
    polling RegIrqFlags for TxDone. Uses SPI1 on GP10-GP13.
    """

    def __init__(self, spi_id=SPI_ID, sck=SCK_PIN, mosi=MOSI_PIN,
                 miso=MISO_PIN, cs=CS_PIN, rst=RST_PIN,
                 frequency=FREQUENCY, logger=None):
        self._frequency = frequency
        self._logger = logger
        self._tx_count = 0
        self._last_payload = None
        self._initialised = False

        # Fault contract
        self.fault = False
        self.fault_code = None
        self.fault_message = None
        self.fault_tier = "fault"
        self.fault_last_checked_ms = None

        # Hardware pin objects
        self._cs = machine.Pin(cs, machine.Pin.OUT)
        self._cs.high()  # deselect
        self._rst = machine.Pin(rst, machine.Pin.OUT)
        self._rst.high()

        # SPI bus
        self._spi = machine.SPI(
            spi_id,
            baudrate=1_000_000,
            polarity=0,
            phase=0,
            sck=machine.Pin(sck),
            mosi=machine.Pin(mosi),
            miso=machine.Pin(miso),
        )

        # Init the radio
        try:
            self._init_radio()
            self._initialised = True
        except Exception as e:
            print(f"LoRa: init failed - {e}")
            if self._logger:
                self._logger.event("lora", f"Init failed: {e}", level="ERROR")
            self.fault = True
            self.fault_code = "LORA_FAIL"
            self.fault_message = f"Init failed: {e}"

    # ------------------------------------------------------------------
    # SPI register access
    # ------------------------------------------------------------------

    def _read_reg(self, addr):
        """Read a single register."""
        self._cs.low()
        self._spi.write(bytes([addr & 0x7F]))
        result = self._spi.read(1)
        self._cs.high()
        return result[0]

    def _write_reg(self, addr, value):
        """Write a single register."""
        self._cs.low()
        self._spi.write(bytes([addr | 0x80, value]))
        self._cs.high()

    def _write_fifo(self, data):
        """Write a block of bytes to the FIFO register."""
        self._cs.low()
        self._spi.write(bytes([_REG_FIFO | 0x80]))
        self._spi.write(data)
        self._cs.high()

    # ------------------------------------------------------------------
    # Radio initialisation
    # ------------------------------------------------------------------

    def _init_radio(self):
        """Configure the SX1278 for LoRa TX."""
        # Hardware reset
        self._rst.low()
        time.sleep_ms(10)
        self._rst.high()
        time.sleep_ms(10)

        # Verify chip version
        version = self._read_reg(_REG_VERSION)
        if version != _EXPECTED_VERSION:
            raise RuntimeError(
                f"SX1278 version 0x{version:02X}, expected 0x{_EXPECTED_VERSION:02X}"
            )

        # Set sleep mode (LoRa mode bit must be set in sleep first)
        self._write_reg(_REG_OP_MODE, _MODE_SLEEP)
        time.sleep_ms(10)

        # Set frequency
        frf = int(self._frequency / _FSTEP)
        self._write_reg(_REG_FRF_MSB, (frf >> 16) & 0xFF)
        self._write_reg(_REG_FRF_MID, (frf >> 8) & 0xFF)
        self._write_reg(_REG_FRF_LSB, frf & 0xFF)

        # Set TX FIFO base address to 0x00
        self._write_reg(_REG_FIFO_TX_BASE, 0x00)

        # PA config: PA_BOOST, max power, 17 dBm
        # PaSelect=1, OutputPower=15 -> Pout = 2 + 15 = 17 dBm
        self._write_reg(_REG_PA_CONFIG, 0x8F)

        # OCP: enable, 120 mA (sufficient for 17 dBm PA_BOOST)
        self._write_reg(_REG_OCP, 0x2B)

        # PA DAC: default (not using +20 dBm mode)
        self._write_reg(_REG_PA_DAC, 0x84)

        # Modem config 1: BW 125kHz (0x7), CR 4/5 (0x1), explicit header (0x0)
        self._write_reg(_REG_MODEM_CONFIG_1, 0x72)

        # Modem config 2: SF9 (0x9), CRC on (bit 2)
        self._write_reg(_REG_MODEM_CONFIG_2, 0x94)

        # Modem config 3: AGC auto on, LowDataRateOptimize off (not needed for SF9/125k)
        self._write_reg(_REG_MODEM_CONFIG_3, 0x04)

        # Preamble length
        self._write_reg(_REG_PREAMBLE_MSB, 0x00)
        self._write_reg(_REG_PREAMBLE_LSB, PREAMBLE_LEN)

        # Sync word (0x12 = public LoRaWAN, 0x34 = private)
        self._write_reg(_REG_SYNC_WORD, 0x12)

        # DIO mapping: DIO0 = TxDone (not connected but set anyway)
        self._write_reg(_REG_DIO_MAPPING_1, 0x40)

        # Clear IRQ flags
        self._write_reg(_REG_IRQ_FLAGS, 0xFF)

        # Go to standby
        self._write_reg(_REG_OP_MODE, _MODE_STDBY)

        freq_mhz = self._frequency / 1_000_000
        print(f"LoRa: init OK (SPI{SPI_ID}, "
              f"{freq_mhz:.1f} MHz, SF{SPREADING_FACTOR}, "
              f"{TX_POWER_DBM} dBm)")
        if self._logger:
            self._logger.event(
                "lora",
                f"Init OK - {freq_mhz:.1f} MHz "
                f"SF{SPREADING_FACTOR} {TX_POWER_DBM} dBm"
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send(self, payload):
        """
        Transmit raw bytes. Returns True on success, False on timeout.

        Writes payload to FIFO, triggers TX, polls RegIrqFlags for
        TxDone (bit 3) at 5ms intervals with 2s timeout.
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

        try:
            # Go to standby for FIFO access
            self._write_reg(_REG_OP_MODE, _MODE_STDBY)

            # Set FIFO pointer to TX base
            self._write_reg(_REG_FIFO_ADDR_PTR, 0x00)

            # Write payload to FIFO
            self._write_fifo(payload)

            # Set payload length
            self._write_reg(_REG_PAYLOAD_LENGTH, payload_len)

            # Clear IRQ flags
            self._write_reg(_REG_IRQ_FLAGS, 0xFF)

            # Set mode to TX
            self._write_reg(_REG_OP_MODE, _MODE_TX)

            # Poll for TxDone
            start = time.ticks_ms()
            while True:
                flags = self._read_reg(_REG_IRQ_FLAGS)
                if flags & _IRQ_TX_DONE:
                    break
                elapsed = time.ticks_diff(time.ticks_ms(), start)
                if elapsed >= TX_TIMEOUT_MS:
                    # Timeout -- return to sleep
                    self._write_reg(_REG_OP_MODE, _MODE_SLEEP)
                    print(f"LoRa: TX timeout after {elapsed}ms")
                    if self._logger:
                        self._logger.event(
                            "lora",
                            f"TX timeout after {elapsed}ms",
                            level="WARN"
                        )
                    self.fault = True
                    self.fault_code = "LORA_TIMEOUT"
                    self.fault_message = f"TX timeout after {elapsed}ms"
                    return False
                time.sleep_ms(TX_POLL_MS)

            # Clear IRQ flags
            self._write_reg(_REG_IRQ_FLAGS, 0xFF)

            # Return to sleep mode
            self._write_reg(_REG_OP_MODE, _MODE_SLEEP)

            self._tx_count += 1
            self._last_payload = payload

            # Successful TX clears any prior timeout fault
            if self.fault and self.fault_code == "LORA_TIMEOUT":
                self.fault = False
                self.fault_code = None
                self.fault_message = None

            if self._logger:
                self._logger.event(
                    "lora",
                    f"TX #{self._tx_count} ({payload_len} bytes)"
                )

            return True

        except Exception as e:
            # Ensure safe state on any SPI error
            try:
                self._write_reg(_REG_OP_MODE, _MODE_SLEEP)
            except Exception:
                pass
            print(f"LoRa: send error - {e}")
            if self._logger:
                self._logger.event(
                    "lora", f"Send error: {e}", level="ERROR"
                )
            return False

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
                                   level="WARN")
            return False

        return self.send(payload)

    def send_alert(self, code, message):
        """
        Transmit a fault alert as 'ALERT;<code>;<message>' over LoRa with
        up to ALERT_RETRIES retries spaced ALERT_RETRY_S apart.

        Wire format matches the Pi4 daemon's alert parser:
            ALERT;<code>;stage=<n>;temp=<t>;rh=<r>[;extra]

        If `message` already starts with 'ALERT;' it is sent verbatim so
        callers can pre-build the full wire string. Otherwise the
        ALERT;<code>;<message> envelope is added here. No JSON -- avoids
        the MicroPython json.dumps float-precision issue that bit the
        telemetry path.

        Alert codes: OVER_TEMP, SENSOR_FAIL, FAN_STALL, HEATER_TIMEOUT,
                     SD_FAIL, LORA_TIMEOUT, STAGE_COMPLETE, SCHEDULE_DONE
        """
        if isinstance(message, str) and message.startswith("ALERT;"):
            payload = message.encode()
        else:
            payload = f"ALERT;{code};{message}".encode()

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
                               level="WARN")
        return False

    def reset(self):
        """Pulse the RST pin low for 10ms to reset the SX1278."""
        self._rst.low()
        time.sleep_ms(10)
        self._rst.high()
        time.sleep_ms(10)
        if self._logger:
            self._logger.event("lora", "Radio reset")

    def check_health(self):
        """Periodic self-check. Returns True if faulted."""
        self.fault_last_checked_ms = time.ticks_ms()
        # Init failure is permanent; TX timeout clears on next success
        return self.fault

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

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


# --- Unit test (hardware-in-the-loop) ---
def test():
    print("=== LoRa unit test ===")
    all_passed = True

    # --- Test 1: init and version check ---
    lora = LoRa()
    passed = lora.is_initialised
    print(f"  {'PASS' if passed else 'FAIL'} - Init: initialised={lora.is_initialised}")
    all_passed &= passed
    if not lora.is_initialised:
        print("  ABORT - Cannot continue without initialised radio")
        return False

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

    # --- Test 10: alert payload is 'ALERT;<code>;<message>' string ---
    try:
        decoded = lora.last_payload.decode()
        passed = (
            decoded.startswith("ALERT;OVER_TEMP;")
            and "Temp 85 deg C exceeds limit" in decoded
        )
    except Exception:
        passed = False
    print(f"  {'PASS' if passed else 'FAIL'} - Alert payload is ALERT;<code>;<message>")
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

    # --- Test 12: reset completes ---
    try:
        lora.reset()
        passed = True
    except Exception:
        passed = False
    print(f"  {'PASS' if passed else 'FAIL'} - reset() completes without error")
    all_passed &= passed

    # --- Test 13: radio works after reset + reinit ---
    try:
        lora3 = LoRa()
        result = lora3.send(b"post-reset test")
        passed = result is True and lora3.is_initialised
    except Exception:
        passed = False
    print(f"  {'PASS' if passed else 'FAIL'} - New instance works after reset")
    all_passed &= passed

    print(f"\n{'All tests passed!' if all_passed else 'Some tests FAILED'}")
    return all_passed


if __name__ == "__main__":
    test()
