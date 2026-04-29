"""LoRa receiver thread for kiln_server.

Owns the Ra-02 SX1278 on SPI0 of the Pi4, configures it for continuous RX,
and decodes incoming packets. Telemetry packets (JSON) are inserted into the
`telemetry` table. Alert strings (ALERT;code;k=v;...) are inserted into the
`alerts` table and forwarded to ntfy.sh.

This module imports spidev and RPi.GPIO lazily so the rest of the package
can be imported (and parse-only unit tested) on a non-Pi development machine.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .database import BOOLEAN_TELEMETRY_COLS, TELEMETRY_COLUMNS, Database
from .notifier import Notifier

log = logging.getLogger(__name__)

# --- SX1278 register map (subset used here) ---------------------------------
REG_FIFO            = 0x00
REG_OP_MODE         = 0x01
REG_FRF_MSB         = 0x06
REG_FRF_MID         = 0x07
REG_FRF_LSB         = 0x08
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
REG_MODEM_CONFIG_3  = 0x26
REG_SYNC_WORD       = 0x39
REG_DIO_MAPPING_1   = 0x40
REG_VERSION         = 0x42

MODE_SLEEP         = 0x80
MODE_RX_CONTINUOUS = 0x85

IRQ_RX_DONE         = 0x40
IRQ_PAYLOAD_CRC_ERR = 0x20

FXOSC = 32_000_000
FSTEP = FXOSC / (1 << 19)
EXPECTED_VERSION = 0x12

# --- Telemetry field mapping ------------------------------------------------
# Fields the Pico may send in its compact telemetry JSON. Unknown fields are
# ignored so firmware can add fields without breaking the daemon.
TELEMETRY_FIELDS = {
    "ts", "stage", "stage_idx", "stage_type",
    "temp_lumber", "temp_intake", "rh_lumber", "rh_intake",
    "mc_channel_1", "mc_channel_2",
    "heater_on", "vent_open", "vent_reason",
    "exhaust_fan_pct", "exhaust_fan_rpm",
    "circ_fan_on", "circ_fan_pct",
    "current_12v_ma", "current_5v_ma",
    "faults",
}

# Lifecycle alert codes: receiver reacts to these in addition to storing them.
LIFECYCLE_RUN_START = "run_started"
LIFECYCLE_RUN_END = "run_complete"

# --- SX1278 driver -----------------------------------------------------------

class SX1278:
    """Minimal SPI driver for the SX1278 in LoRa mode."""

    def __init__(
        self,
        spi_bus: int,
        spi_ce: int,
        rst_pin: int,
        dio0_pin: int,
        freq_hz: int = 433_000_000,
    ):
        import spidev            # lazy import for non-Pi dev machines
        import RPi.GPIO as GPIO

        self._GPIO = GPIO
        self.rst_pin = rst_pin
        self.dio0_pin = dio0_pin
        self.freq_hz = freq_hz

        self.spi = spidev.SpiDev()
        self.spi.open(spi_bus, spi_ce)
        self.spi.max_speed_hz = 1_000_000
        self.spi.mode = 0

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(rst_pin, GPIO.OUT, initial=GPIO.HIGH)
        GPIO.setup(dio0_pin, GPIO.IN)

    def reset(self) -> None:
        self._GPIO.output(self.rst_pin, self._GPIO.LOW)
        time.sleep(0.01)
        self._GPIO.output(self.rst_pin, self._GPIO.HIGH)
        time.sleep(0.01)

    def read_reg(self, addr: int) -> int:
        resp = self.spi.xfer2([addr & 0x7F, 0x00])
        return resp[1]

    def write_reg(self, addr: int, value: int) -> None:
        self.spi.xfer2([addr | 0x80, value & 0xFF])

    def read_fifo(self, length: int) -> bytes:
        data = self.spi.xfer2([REG_FIFO & 0x7F] + [0x00] * length)
        return bytes(data[1:])

    def dio0(self) -> int:
        return int(self._GPIO.input(self.dio0_pin))

    def configure_rx(self, sf: int = 9, cr: int = 5) -> None:
        """Put the radio in continuous RX mode with the given RF params.

        Must be called after reset(). Matches the Pico TX configuration:
        BW 125 kHz, explicit header, CRC on, sync word 0x12, preamble 8.
        """
        self.write_reg(REG_OP_MODE, MODE_SLEEP)
        time.sleep(0.01)

        frf = int(self.freq_hz / FSTEP)
        self.write_reg(REG_FRF_MSB, (frf >> 16) & 0xFF)
        self.write_reg(REG_FRF_MID, (frf >> 8) & 0xFF)
        self.write_reg(REG_FRF_LSB, frf & 0xFF)

        self.write_reg(REG_FIFO_RX_BASE, 0x00)
        self.write_reg(REG_LNA, 0x23)

        # MODEM_CONFIG_1: BW 125 kHz (0x7), CR 4/5 (0x1), explicit header (0)
        mc1 = 0x72 if cr == 5 else (0x70 | ((cr - 4) << 1))
        self.write_reg(REG_MODEM_CONFIG_1, mc1)

        # MODEM_CONFIG_2: SF<<4 | CRC on (0x04) | TxContinuous off
        self.write_reg(REG_MODEM_CONFIG_2, (sf << 4) | 0x04)

        # MODEM_CONFIG_3: AGC auto on (0x04); LowDataRateOptimize off
        self.write_reg(REG_MODEM_CONFIG_3, 0x04)

        self.write_reg(REG_PREAMBLE_MSB, 0x00)
        self.write_reg(REG_PREAMBLE_LSB, 0x08)

        self.write_reg(REG_SYNC_WORD, 0x12)
        self.write_reg(REG_DIO_MAPPING_1, 0x00)   # DIO0 = RxDone

        self.write_reg(REG_IRQ_FLAGS, 0xFF)
        self.write_reg(REG_OP_MODE, MODE_RX_CONTINUOUS)

    def read_packet(self) -> Optional[Tuple[bytes, int, float]]:
        """Read one RX packet from the FIFO.

        Returns (payload, rssi_dbm, snr_db) or None on CRC error.
        Must be called after DIO0 goes high (RxDone). Clears IRQ flags.
        """
        flags = self.read_reg(REG_IRQ_FLAGS)
        if flags & IRQ_PAYLOAD_CRC_ERR:
            self.write_reg(REG_IRQ_FLAGS, 0xFF)
            return None

        nb_bytes = self.read_reg(REG_RX_NB_BYTES)
        current_addr = self.read_reg(REG_FIFO_RX_CURRENT)
        self.write_reg(REG_FIFO_ADDR_PTR, current_addr)
        payload = self.read_fifo(nb_bytes)

        snr_raw = self.read_reg(REG_PKT_SNR)
        if snr_raw > 127:
            snr_raw -= 256
        snr_db = snr_raw / 4.0

        rssi_raw = self.read_reg(REG_PKT_RSSI)
        if snr_db >= 0:
            rssi_dbm = -157 + rssi_raw
        else:
            rssi_dbm = -157 + rssi_raw + snr_db

        self.write_reg(REG_IRQ_FLAGS, 0xFF)
        return (payload, int(rssi_dbm), float(snr_db))

    def close(self) -> None:
        try:
            self.write_reg(REG_OP_MODE, MODE_SLEEP)
        except Exception:
            pass
        try:
            self.spi.close()
        except Exception:
            pass
        try:
            self._GPIO.cleanup((self.rst_pin, self.dio0_pin))
        except Exception:
            pass


# --- Packet parsing ---------------------------------------------------------

def parse_packet(payload: bytes) -> Tuple[str, Dict[str, Any]]:
    """Classify a raw LoRa payload.

    Returns (kind, parsed) where kind is one of:
        "telemetry"  -- parsed dict ready for telemetry insert
        "heartbeat"  -- parsed dict with type/uptime/ts
        "alert"      -- parsed dict with code/message/value/limit_val/stage
        "unknown"    -- parsed["raw"] holds the original bytes (hex)
    """
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return ("unknown", {"raw": payload.hex()})

    # JSON first: telemetry and heartbeat are always JSON.
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except (ValueError, json.JSONDecodeError):
            return ("unknown", {"raw": text})
        if isinstance(obj, dict):
            if obj.get("type") == "heartbeat":
                return ("heartbeat", obj)
            return ("telemetry", _normalise_telemetry(obj))
        return ("unknown", {"raw": text})

    # Fallback: semicolon-delimited alert string.
    if text.startswith("ALERT;"):
        return ("alert", _parse_alert(text))

    return ("unknown", {"raw": text})


def _normalise_telemetry(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Map a Pico telemetry dict to the schema columns used by the daemon."""
    out: Dict[str, Any] = {}
    for k, v in obj.items():
        if k == "type":
            continue
        if k in TELEMETRY_FIELDS:
            out[k] = v
    # Pico sends stage_idx; schema column is stage.
    if "stage_idx" in out and "stage" not in out:
        try:
            out["stage"] = int(out["stage_idx"])
        except (TypeError, ValueError):
            out["stage"] = None
    out.pop("stage_idx", None)
    # faults is a list on the wire; flatten to comma-separated string for SQLite.
    if isinstance(out.get("faults"), list):
        out["faults"] = ",".join(str(x) for x in out["faults"])
    return out


def _parse_alert(text: str) -> Dict[str, Any]:
    """Parse ALERT;{code};k=v;k=v;...;[extra_text] into a dict.

    `stage`, `temp`, `rh` and any other k=v pairs are pulled out; anything
    without `=` becomes part of `message_extra`. The full original string is
    always preserved in `message`.
    """
    parts = [p for p in text.split(";") if p != ""]
    # parts[0] == "ALERT"
    code = parts[1] if len(parts) >= 2 else "unknown"
    kv: Dict[str, str] = {}
    extras: List[str] = []
    for p in parts[2:]:
        if "=" in p:
            k, _, v = p.partition("=")
            kv[k.strip()] = v.strip()
        else:
            extras.append(p.strip())

    def _float_or_none(key: str) -> Optional[float]:
        if key not in kv:
            return None
        try:
            return float(kv[key])
        except ValueError:
            return None

    def _int_or_none(key: str) -> Optional[int]:
        if key not in kv:
            return None
        try:
            return int(kv[key])
        except ValueError:
            return None

    # `or`-chain would collapse a legitimate 0.0 to the next fallback,
    # so prefer the first non-None.
    value: Optional[float] = next(
        (v for v in (_float_or_none(k) for k in ("value", "temp", "rh"))
         if v is not None),
        None,
    )

    return {
        "code": code,
        "message": text,
        "message_extra": "; ".join(extras) if extras else None,
        "stage": _int_or_none("stage"),
        "value": value,
        "limit_val": _float_or_none("limit"),
    }


# --- Receiver thread --------------------------------------------------------

class LoraReceiver(threading.Thread):
    """Background thread: waits on DIO0, parses packets, writes to DB/ntfy.

    Uses a simple polling loop over GPIO rather than an edge interrupt so the
    thread can be stopped cleanly via `stop()` without blocking inside a C
    callback.
    """

    def __init__(
        self,
        radio: SX1278,
        db: Database,
        notifier: Notifier,
        poll_interval_s: float = 0.02,
    ):
        super().__init__(name="lora-rx", daemon=True)
        self.radio = radio
        self.db = db
        self.notifier = notifier
        self.poll_interval_s = poll_interval_s
        self._stop = threading.Event()

        # Shared stats exposed via /health. Read from other threads.
        self._stats_lock = threading.Lock()
        self.start_time = time.time()
        self.total_packets = 0
        self.last_packet_ts: Optional[int] = None
        self.last_rssi: Optional[int] = None
        self.last_snr: Optional[float] = None

    # --- public API --------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def health(self) -> Dict[str, Any]:
        with self._stats_lock:
            last_ts = self.last_packet_ts
            return {
                "uptime_s": int(time.time() - self.start_time),
                "last_packet_ts": last_ts,
                "last_packet_age_s": (int(time.time() - last_ts)
                                     if last_ts else None),
                "total_packets": self.total_packets,
                "lora_rssi_last": self.last_rssi,
                "lora_snr_last": self.last_snr,
            }

    # --- thread body -------------------------------------------------------

    def run(self) -> None:
        log.info("lora-rx: receiver thread started")
        while not self._stop.is_set():
            try:
                if self.radio.dio0():
                    self._handle_packet()
                else:
                    time.sleep(self.poll_interval_s)
            except Exception as e:
                log.exception("lora-rx: unexpected error: %s", e)
                time.sleep(0.5)
        log.info("lora-rx: receiver thread stopping")

    def _handle_packet(self) -> None:
        result = self.radio.read_packet()
        if result is None:
            log.warning("lora-rx: CRC error, packet discarded")
            return

        payload, rssi, snr = result
        received_at = int(time.time())
        kind, parsed = parse_packet(payload)

        with self._stats_lock:
            self.total_packets += 1
            self.last_rssi = rssi
            self.last_snr = snr
            self.last_packet_ts = received_at

        log.info("lora-rx: %s packet (%d bytes, RSSI=%d dBm, SNR=%.1f dB)",
                 kind, len(payload), rssi, snr)

        if kind == "telemetry":
            self._store_telemetry(parsed, received_at, rssi, snr)
        elif kind == "alert":
            self._store_alert(parsed, received_at, rssi, snr)
        elif kind == "heartbeat":
            log.debug("lora-rx: heartbeat uptime=%s", parsed.get("uptime_s"))
        else:
            log.warning("lora-rx: unknown packet, discarded: %s",
                        parsed.get("raw", "?")[:80])

    # --- handlers ----------------------------------------------------------

    def _store_telemetry(
        self,
        data: Dict[str, Any],
        received_at: int,
        rssi: int,
        snr: float,
    ) -> None:
        # Open a run implicitly if none is active. The Pico does not emit an
        # explicit "run_started" alert, so the first telemetry after idle is
        # what anchors the run.
        run_id = self.db.active_run_id()
        if run_id is None and data.get("stage") is not None:
            ts = int(data.get("ts") or received_at)
            run_id = self.db.open_run(started_at=ts)
            log.info("lora-rx: opened run %d at ts=%d", run_id, ts)

        record: Dict[str, Any] = {col: None for col in TELEMETRY_COLUMNS}
        record.update(data)
        record["ts"] = int(data.get("ts") or received_at)
        record["received_at"] = received_at
        record["run_id"] = run_id
        record["lora_rssi"] = rssi
        record["lora_snr"] = snr
        for b in BOOLEAN_TELEMETRY_COLS:
            v = record.get(b)
            if v is not None:
                record[b] = 1 if v else 0
        self.db.insert_telemetry(record)

    def _store_alert(
        self,
        data: Dict[str, Any],
        received_at: int,
        rssi: int,
        snr: float,
    ) -> None:
        code = data["code"]

        # Lifecycle: run_complete closes the active run.
        run_id = self.db.active_run_id()
        if code == LIFECYCLE_RUN_END and run_id is not None:
            self.db.close_run(run_id, received_at)
            log.info("lora-rx: closed run %d at ts=%d", run_id, received_at)
        elif code == LIFECYCLE_RUN_START and run_id is None:
            run_id = self.db.open_run(started_at=received_at)
            log.info("lora-rx: opened run %d via alert", run_id)

        self.db.insert_alert({
            "ts": received_at,
            "received_at": received_at,
            "run_id": run_id,
            "code": code,
            "message": data.get("message"),
            "value": data.get("value"),
            "limit_val": data.get("limit_val"),
            "lora_rssi": rssi,
            "lora_snr": snr,
        })

        # Push to phone.
        self.notifier.send(code, data.get("message"))
