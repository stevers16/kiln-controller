#!/usr/bin/env python3
# lora_test_rx.py -- Pi4-side LoRa RX test
#
# Validates the Ra-02 module is connected, then listens for packets
# from the Pico and prints them as they arrive.
#
# Usage:  python3 lora_test_rx.py
#
# Prerequisites:
#   sudo raspi-config -> Interface Options -> SPI -> Enable (reboot)
#   pip install spidev RPi.GPIO
#
# Pi4 wiring (from lora_telemetry_spec.md):
#   Ra-02 SCK  -> GPIO11 (Pin 23)  SPI0 SCLK
#   Ra-02 MOSI -> GPIO10 (Pin 19)  SPI0 MOSI
#   Ra-02 MISO -> GPIO9  (Pin 21)  SPI0 MISO
#   Ra-02 NSS  -> GPIO8  (Pin 24)  SPI0 CE0
#   Ra-02 DIO0 -> GPIO25 (Pin 22)  RX done interrupt
#   Ra-02 RST  -> GPIO17 (Pin 11)  Reset (active low)
#   Ra-02 VCC  -> Pin 17 (3.3V)
#   Ra-02 GND  -> Pin 20 (GND)

import sys
import time
import json
import signal

try:
    import spidev
    import RPi.GPIO as GPIO
except ImportError:
    print("ERROR: This script must run on a Raspberry Pi with spidev and RPi.GPIO installed.")
    print("Install with:  pip install spidev RPi.GPIO")
    sys.exit(1)

# --- Pi4 pin assignments (BCM numbering) ---
RST_PIN  = 17
DIO0_PIN = 25
SPI_BUS  = 0
SPI_DEV  = 0   # CE0

# --- SX1278 registers ---
REG_FIFO            = 0x00
REG_OP_MODE         = 0x01
REG_FRF_MSB         = 0x06
REG_FRF_MID         = 0x07
REG_FRF_LSB         = 0x08
REG_PA_CONFIG       = 0x09
REG_OCP             = 0x0B
REG_LNA             = 0x0C
REG_FIFO_ADDR_PTR   = 0x0D
REG_FIFO_RX_BASE    = 0x0F
REG_FIFO_RX_CURRENT = 0x10
REG_IRQ_FLAGS       = 0x12
REG_RX_NB_BYTES     = 0x13
REG_PKT_SNR         = 0x19
REG_PKT_RSSI        = 0x1A
REG_MODEM_CONFIG_1  = 0x1D
REG_MODEM_CONFIG_2  = 0x1E
REG_PREAMBLE_MSB    = 0x20
REG_PREAMBLE_LSB    = 0x21
REG_PAYLOAD_LENGTH  = 0x22
REG_MODEM_CONFIG_3  = 0x26
REG_SYNC_WORD       = 0x39
REG_DIO_MAPPING_1   = 0x40
REG_VERSION         = 0x42
REG_PA_DAC          = 0x4D

# --- SX1278 modes ---
MODE_SLEEP          = 0x80
MODE_STDBY          = 0x81
MODE_RX_CONTINUOUS  = 0x85

# --- IRQ masks ---
IRQ_RX_DONE         = 0x40  # bit 6
IRQ_PAYLOAD_CRC_ERR = 0x20  # bit 5

# --- RF parameters (must match Pico TX side) ---
FREQUENCY       = 433_000_000
FXOSC           = 32_000_000
FSTEP           = FXOSC / (1 << 19)
EXPECTED_VERSION = 0x12


class SX1278:
    """Minimal SX1278 driver for Pi4 using spidev."""

    def __init__(self):
        self.spi = spidev.SpiDev()
        self.spi.open(SPI_BUS, SPI_DEV)
        self.spi.max_speed_hz = 1_000_000
        self.spi.mode = 0

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(RST_PIN, GPIO.OUT)
        GPIO.setup(DIO0_PIN, GPIO.IN)

    def reset(self):
        GPIO.output(RST_PIN, GPIO.LOW)
        time.sleep(0.01)
        GPIO.output(RST_PIN, GPIO.HIGH)
        time.sleep(0.01)

    def read_reg(self, addr):
        resp = self.spi.xfer2([addr & 0x7F, 0x00])
        return resp[1]

    def write_reg(self, addr, value):
        self.spi.xfer2([addr | 0x80, value])

    def read_fifo(self, length):
        data = self.spi.xfer2([REG_FIFO & 0x7F] + [0x00] * length)
        return bytes(data[1:])

    def cleanup(self):
        self.spi.close()
        GPIO.cleanup()


def run_hardware_tests(radio):
    """Validate the Ra-02 module is connected and responding."""
    print("=" * 50)
    print("  Ra-02 Hardware Validation (Pi4)")
    print("=" * 50)
    all_passed = True

    # Test 1: Reset
    print("\n  --- Test 1: Hardware reset ---")
    try:
        radio.reset()
        passed = True
    except Exception as e:
        passed = False
        print(f"  ERROR: {e}")
    print(f"  {'PASS' if passed else 'FAIL'} - RST pin toggle (GPIO{RST_PIN})")
    all_passed &= passed

    # Test 2: SPI read -- version register
    print("\n  --- Test 2: SPI communication ---")
    version = radio.read_reg(REG_VERSION)
    passed = version == EXPECTED_VERSION
    print(f"  {'PASS' if passed else 'FAIL'} - Version register: 0x{version:02X} (expected 0x{EXPECTED_VERSION:02X})")
    all_passed &= passed
    if not passed:
        print("  ABORT: SX1278 not detected. Check SPI wiring and that SPI is enabled.")
        return False

    # Test 3: Register write/read
    print("\n  --- Test 3: Register write/read ---")
    radio.write_reg(REG_OP_MODE, MODE_SLEEP)
    mode = radio.read_reg(REG_OP_MODE)
    passed = mode == MODE_SLEEP
    print(f"  {'PASS' if passed else 'FAIL'} - Write MODE_SLEEP, read back 0x{mode:02X} (expected 0x{MODE_SLEEP:02X})")
    all_passed &= passed

    # Test 4: Frequency register write/read
    print("\n  --- Test 4: Frequency configuration ---")
    frf = int(FREQUENCY / FSTEP)
    radio.write_reg(REG_FRF_MSB, (frf >> 16) & 0xFF)
    radio.write_reg(REG_FRF_MID, (frf >> 8) & 0xFF)
    radio.write_reg(REG_FRF_LSB, frf & 0xFF)
    msb = radio.read_reg(REG_FRF_MSB)
    mid = radio.read_reg(REG_FRF_MID)
    lsb = radio.read_reg(REG_FRF_LSB)
    frf_readback = (msb << 16) | (mid << 8) | lsb
    passed = frf_readback == frf
    freq_mhz = frf_readback * FSTEP / 1_000_000
    print(f"  {'PASS' if passed else 'FAIL'} - Frequency: {freq_mhz:.3f} MHz (target {FREQUENCY / 1_000_000:.1f} MHz)")
    all_passed &= passed

    # Test 5: DIO0 pin readable
    print("\n  --- Test 5: DIO0 pin ---")
    try:
        val = GPIO.input(DIO0_PIN)
        passed = True
        print(f"  {'PASS' if passed else 'FAIL'} - DIO0 (GPIO{DIO0_PIN}) readable, current={val}")
    except Exception as e:
        passed = False
        print(f"  FAIL - DIO0 read error: {e}")
    all_passed &= passed

    # Test 6: Modem config write/read
    print("\n  --- Test 6: Modem configuration ---")
    # BW 125kHz, CR 4/5, explicit header
    radio.write_reg(REG_MODEM_CONFIG_1, 0x72)
    mc1 = radio.read_reg(REG_MODEM_CONFIG_1)
    # SF9, CRC on
    radio.write_reg(REG_MODEM_CONFIG_2, 0x94)
    mc2 = radio.read_reg(REG_MODEM_CONFIG_2)
    passed = mc1 == 0x72 and mc2 == 0x94
    print(f"  {'PASS' if passed else 'FAIL'} - ModemConfig1=0x{mc1:02X} (0x72), ModemConfig2=0x{mc2:02X} (0x94)")
    all_passed &= passed

    print("\n" + "=" * 50)
    if all_passed:
        print("  All hardware tests PASSED")
    else:
        print("  Some tests FAILED")
    print("=" * 50)

    return all_passed


def configure_rx(radio):
    """Configure the SX1278 for LoRa RX continuous mode."""
    # Sleep mode first (required to set LoRa bit)
    radio.write_reg(REG_OP_MODE, MODE_SLEEP)
    time.sleep(0.01)

    # Frequency
    frf = int(FREQUENCY / FSTEP)
    radio.write_reg(REG_FRF_MSB, (frf >> 16) & 0xFF)
    radio.write_reg(REG_FRF_MID, (frf >> 8) & 0xFF)
    radio.write_reg(REG_FRF_LSB, frf & 0xFF)

    # RX FIFO base address
    radio.write_reg(REG_FIFO_RX_BASE, 0x00)

    # LNA: max gain, boost on (for receive sensitivity)
    radio.write_reg(REG_LNA, 0x23)

    # Modem config: BW 125kHz, CR 4/5, explicit header
    radio.write_reg(REG_MODEM_CONFIG_1, 0x72)

    # SF9, CRC on
    radio.write_reg(REG_MODEM_CONFIG_2, 0x94)

    # AGC auto on
    radio.write_reg(REG_MODEM_CONFIG_3, 0x04)

    # Preamble
    radio.write_reg(REG_PREAMBLE_MSB, 0x00)
    radio.write_reg(REG_PREAMBLE_LSB, 0x08)

    # Sync word (must match TX side)
    radio.write_reg(REG_SYNC_WORD, 0x12)

    # DIO0 mapping: RxDone
    radio.write_reg(REG_DIO_MAPPING_1, 0x00)

    # Clear IRQ flags
    radio.write_reg(REG_IRQ_FLAGS, 0xFF)

    # Set RX continuous mode
    radio.write_reg(REG_OP_MODE, MODE_RX_CONTINUOUS)

    print("Radio configured for RX continuous mode")
    print(f"  Frequency: {FREQUENCY / 1_000_000:.1f} MHz")
    print(f"  SF9, BW 125 kHz, CR 4/5, CRC on")


def read_packet(radio):
    """
    Read a received packet from the FIFO.
    Returns (payload_bytes, rssi_dbm, snr_db) or None if CRC error.
    """
    # Check for CRC error
    flags = radio.read_reg(REG_IRQ_FLAGS)
    if flags & IRQ_PAYLOAD_CRC_ERR:
        # Clear flags and discard
        radio.write_reg(REG_IRQ_FLAGS, 0xFF)
        return None

    # Number of bytes received
    nb_bytes = radio.read_reg(REG_RX_NB_BYTES)

    # Set FIFO pointer to start of last packet
    current_addr = radio.read_reg(REG_FIFO_RX_CURRENT)
    radio.write_reg(REG_FIFO_ADDR_PTR, current_addr)

    # Read payload
    payload = radio.read_fifo(nb_bytes)

    # Read RSSI and SNR
    pkt_snr = radio.read_reg(REG_PKT_SNR)
    if pkt_snr > 127:
        pkt_snr -= 256
    snr_db = pkt_snr / 4.0

    pkt_rssi_raw = radio.read_reg(REG_PKT_RSSI)
    if snr_db >= 0:
        rssi_dbm = -157 + pkt_rssi_raw  # for 433 MHz
    else:
        rssi_dbm = -157 + pkt_rssi_raw + snr_db

    # Clear IRQ flags
    radio.write_reg(REG_IRQ_FLAGS, 0xFF)

    return (payload, rssi_dbm, snr_db)


def listen(radio):
    """Listen for LoRa packets and print them."""
    print("\nListening for packets... (Ctrl-C to stop)\n")

    pkt_count = 0
    while True:
        # Wait for DIO0 to go high (RxDone)
        if GPIO.input(DIO0_PIN):
            result = read_packet(radio)
            if result is None:
                print(f"  [CRC ERROR] -- packet discarded")
                continue

            payload, rssi, snr = result
            pkt_count += 1
            ts = time.strftime("%H:%M:%S")

            print(f"  [{ts}] Packet #{pkt_count} "
                  f"({len(payload)} bytes, RSSI={rssi:.0f} dBm, SNR={snr:.1f} dB)")

            # Try to decode as JSON
            try:
                decoded = json.loads(payload.decode("utf-8"))
                print(f"           {json.dumps(decoded, indent=None)}")
            except Exception:
                # Print raw hex if not JSON
                hex_str = " ".join(f"{b:02X}" for b in payload[:64])
                print(f"           [raw] {hex_str}")
                if len(payload) > 64:
                    print(f"           ... ({len(payload) - 64} more bytes)")

            print()

        time.sleep(0.01)  # 10ms poll interval


def main():
    radio = SX1278()

    # Register cleanup on exit
    def cleanup_handler(sig, frame):
        print("\nShutting down...")
        try:
            radio.write_reg(REG_OP_MODE, MODE_SLEEP)
        except Exception:
            pass
        radio.cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup_handler)
    signal.signal(signal.SIGTERM, cleanup_handler)

    # Step 1: Hardware validation
    radio.reset()
    if not run_hardware_tests(radio):
        print("\nHardware validation failed. Fix wiring before continuing.")
        radio.cleanup()
        sys.exit(1)

    # Step 2: Configure for RX and listen
    print()
    configure_rx(radio)
    listen(radio)


if __name__ == "__main__":
    main()
