# LoRa Telemetry Module -- Hardware & Firmware Spec
**Kiln Controller Project**
**Status:** Ra-02 modules on order (arriving ~2 weeks); Pi4 daemon not yet implemented
**Scope:** Covers Pico-side LoRa transmitter and Pi4-side LoRa receiver + data server

---

## Overview

The kiln Pico 2W transmits periodic telemetry and fault alerts via a LoRa radio link to a
Raspberry Pi 4 at the cottage (~200m, wooded path following cleared driveway). The Pi4 has
a second Ra-02 module wired directly to its SPI bus. A Python daemon receives LoRa packets,
writes them to a SQLite database, serves a REST API for the Kivy phone app, and pushes
fault alerts via ntfy.sh.

```
Pico 2W --> SPI1 --> Ra-02 (433 MHz) ~~LoRa~~ Ra-02 --> SPI0 --> Pi4 daemon --> SQLite
                                                                              --> REST API --> Kivy app
                                                                              --> ntfy.sh --> phone
```

**Pico-side is transmit-only.** The Pico never listens for incoming LoRa packets. All
parameter changes and commands to the kiln are handled via the Wi-Fi AP / REST API interface.
DIO0 is not connected on the Pico side -- TX completion is confirmed by polling the SX1278
IRQ flags register over SPI (see Firmware section). This frees GP20 for use as the display
button input.

No ESP32 or MQTT broker is required in the deployed system. The ESP32-WROOM-32 devboard
(owned) is useful for bench testing the Pico LoRa transmitter before the Pi4 daemon is
ready, but is not part of the production architecture.

---

## Hardware

### LoRa Modules

**Part:** AI-Thinker Ra-02 (SX1278, 433 MHz) with IPEX antenna
**Quantity:** 2 (ordered from AliExpress)
**Critical:** 433 MHz variant only -- NOT the 915 MHz Ra-01 or Ra-01S.

**Ra-02 electrical characteristics:**
- Supply voltage: 3.3V (NOT 5V tolerant -- will be damaged by 5V)
- Logic levels: 3.3V
- Interface: SPI (mode 0)
- Max SPI clock: 10 MHz (use 1-4 MHz in practice)
- Current draw: ~120 mA TX, ~12 mA RX, ~1.5 mA idle

**Antenna:** IPEX (u.FL) connector with antenna included in order.

---

### Pico-Side Wiring (Kiln Enclosure)

The Ra-02 connects via SPI1 on the Pico using the default SPI1 pin block (GP10-GP13).
Moisture probe AC excitation channels were moved from GP12/GP13 to GP6/GP7 to free the
full SPI1 block for LoRa -- this is already reflected in `lib/moisture.py` (GP6/GP7).

DIO0 is **not connected** on the Pico side. TX completion is confirmed by polling the
SX1278 IRQ flags register over SPI (see Firmware section). GP20 is therefore free for
use as the display button input, which is its current assignment in `lib/display.py`.

**Power:**

| Ra-02 Pin | Pico Pin      | Notes               |
|-----------|---------------|---------------------|
| VCC       | 3.3V (Pin 36) | Ra-02 is 3.3V only  |
| GND       | GND (Pin 38)  | Common ground       |

**SPI and control:**

| Ra-02 Pin | Pico GPIO | Physical Pin | Notes                           |
|-----------|-----------|--------------|---------------------------------|
| SCK       | GP10      | Pin 14       | SPI1 SCK (default)              |
| MOSI      | GP11      | Pin 15       | SPI1 TX (default)               |
| MISO      | GP12      | Pin 16       | SPI1 RX (default)               |
| NSS (CS)  | GP13      | Pin 17       | SPI1 CS (default) -- active low |
| RST       | GP28      | Pin 34       | Reset -- active low             |
| DIO0      | --        | not connected| Not needed (TX-only, polling)   |

**GPIO map (Pico GPIO Map tab in BOM):**

| GPIO | Function                      | Notes                                  |
|------|-------------------------------|----------------------------------------|
| GP6  | Digital OUT -- AC excite Ch1  | Moisture probe (moved from GP12)       |
| GP7  | Digital OUT -- AC excite Ch2  | Moisture probe (moved from GP13)       |
| GP10 | SPI1 SCK -- LoRa Ra-02        |                                        |
| GP11 | SPI1 TX (MOSI) -- LoRa Ra-02  |                                        |
| GP12 | SPI1 RX (MISO) -- LoRa Ra-02  |                                        |
| GP13 | SPI1 CS -- LoRa Ra-02         | Active low                             |
| GP20 | Digital IN -- Display button  | Freed from LoRa DIO0; see display spec |
| GP28 | Digital OUT -- LoRa RST       |                                        |

**Decoupling:** Place a 100 nF ceramic capacitor between Ra-02 VCC and GND as close to
the module as possible. The Ra-02 RF switching causes supply transients.

---

### Pi4-Side Wiring (Cottage)

The Ra-02 wires directly to the Pi4 GPIO header via SPI0. The Pi4 GPIO is 3.3V -- no
level shifting required. SPI must be enabled on the Pi4 via:
`sudo raspi-config` -> Interface Options -> SPI -> Enable

DIO0 IS connected on the Pi4 side because the daemon uses interrupt-driven receive.

**Power:**

| Ra-02 Pin | Pi4 Pin       | Notes              |
|-----------|---------------|--------------------|
| VCC       | Pin 17 (3.3V) | Ra-02 is 3.3V only |
| GND       | Pin 20 (GND)  | Common ground      |

**SPI and control (SPI0, CE0):**

| Ra-02 Pin | Pi4 GPIO | Pi4 Physical Pin | Notes                     |
|-----------|----------|------------------|---------------------------|
| SCK       | GPIO11   | Pin 23           | SPI0 SCLK                 |
| MOSI      | GPIO10   | Pin 19           | SPI0 MOSI                 |
| MISO      | GPIO9    | Pin 21           | SPI0 MISO                 |
| NSS (CS)  | GPIO8    | Pin 24           | SPI0 CE0 -- active low    |
| DIO0      | GPIO25   | Pin 22           | RX done interrupt (input) |
| RST       | GPIO17   | Pin 11           | Reset line (output)       |

**Decoupling:** 100 nF ceramic capacitor between Ra-02 VCC and GND.

---

## Firmware Architecture

### Pico Side -- `lib/lora.py`

A mock implementation already exists and passes hardware tests. This section describes
the real driver to replace it once Ra-02 hardware arrives. Follow the existing module
pattern (class-based, pin numbers as constructor args, matching `exhaust.py` template).

```python
# Instantiation example (from main.py)
lora = LoRa(
    spi_id=1,
    sck=10, mosi=11, miso=12,
    cs=13, rst=28,
    frequency=433_000_000
)
```

Note: no `irq` parameter -- DIO0 is not connected on the Pico side.

**Class responsibilities:**
- Initialise SPI1 using default pin block:
  `SPI(1, baudrate=1_000_000, sck=Pin(10), mosi=Pin(11), miso=Pin(12))`
- Configure SX1278 registers via SPI (frequency, bandwidth, spreading factor, coding rate)
- `send(payload: bytes) -> bool` -- blocking TX with polling-based completion (see below)
- `send_telemetry(data: dict) -> bool` -- serialises dict to JSON, calls `send()`
- `send_alert(code: str, message: str) -> bool` -- 3x retry with 2s spacing
- `reset()` -- pulses RST pin low for 10ms
- `is_mock` property returns False (distinguishes from mock implementation)
- TX-only -- no receive path on Pico side
- Accepts optional `logger=None`; source string `"lora"`; logs on init, send success,
  send timeout, and RST events

**TX completion -- register polling (no DIO0 needed):**

Because DIO0 is not connected, TX completion is determined by polling the SX1278 IRQ flags
register (RegIrqFlags, address 0x12) over SPI. The TxDone flag is bit 3 (mask 0x08).

Procedure in `send()`:
1. Write payload to FIFO, set mode to TX (RegOpMode = 0x83)
2. Poll RegIrqFlags in a loop at 5 ms intervals until bit 3 (TxDone) is set, or 2000 ms
   timeout is reached
3. Clear all IRQ flags by writing 0xFF to RegIrqFlags
4. Return device to sleep mode (RegOpMode = 0x80)
5. Return True on TxDone, False on timeout (caller logs `LORA_TIMEOUT` alert)

For SF9 / 125 kHz / 4:5 coding rate, airtime for a 100-byte payload is approximately
330 ms. The 2000 ms timeout gives comfortable headroom across all expected payload sizes.
This approach is functionally equivalent to interrupt-driven TX confirmation for a
transmit-only module on a 30-second heartbeat schedule.

**Suggested LoRa RF parameters:**

| Parameter        | Value     | Notes                                              |
|------------------|-----------|----------------------------------------------------|
| Frequency        | 433.0 MHz | Within 433.05-434.79 MHz ISM band (Canada RSS-210) |
| Bandwidth        | 125 kHz   | Standard                                           |
| Spreading Factor | SF9       | Good for wooded 200m; increase if link marginal    |
| Coding Rate      | 4/5       |                                                    |
| TX Power         | 17 dBm    | Ra-02 maximum; reduce if interference observed     |
| Preamble         | 8 symbols | Default                                            |

**Transmission schedule:**
- Telemetry heartbeat: every 30 seconds (driven by `schedule.py` tick)
- Fault alerts: immediate on detection, retried up to 3x with 2s spacing
- SPI1 (LoRa) and SPI0 (SD card) are separate buses -- no contention

**Fault codes in telemetry packet (added by error_checking_spec.md):**

The LoRa telemetry packet now includes an optional `faults` field: a flat
JSON array of fault code strings (e.g. `"faults":["SD_FAIL","LORA_TIMEOUT"]`).
Only tier="fault" codes are included (not notices or info). The list is
trimmed from the back until the total packet fits within the SX1278 255-byte
FIFO limit. With the existing ~231-byte base payload, typically 0-1 short
fault codes fit. If no faults are active, the field is omitted entirely.

The rich `fault_details` list (with code, source, message, tier per fault)
is served exclusively over the Pico HTTP `/status` endpoint. The Pi4 daemon
receives only the flat code list over LoRa and can look up descriptions
from a static table or simply display the codes.

**Duty cycle note:** At 30s intervals TX duty cycle is well under 1% -- within the 10%
limit for 433 MHz ISM use in Canada.

---

### Pi4 Side -- `kiln_server/` Python package

The Pi4 runs a single Python process that owns the Ra-02 via SPI, writes all received
data to SQLite, serves a REST API to the Kivy app, and pushes alerts via ntfy.sh.

**Python dependencies:**
- `spidev` -- SPI bus access on Pi4
- `RPi.GPIO` -- GPIO interrupt handling for DIO0
- `pysx127x` or equivalent SX1276/78 driver (or port register sequences from the Pico
  driver directly -- keeps both sides symmetric and eliminates a third-party dependency)
- `flask` or `fastapi` -- REST API
- `requests` -- ntfy.sh HTTP POST
- `sqlite3` -- stdlib, no install required

**Package structure:**

```
kiln_server/
  __main__.py       -- entry point; starts receiver loop and REST API server
  config.py         -- Pi4 GPIO pins, DB path, ntfy topic, API port, LoRa params
  lora_receiver.py  -- SX1278 init, receive loop, DIO0 interrupt handler
  database.py       -- SQLite open/init, insert_telemetry(), insert_alert(), query functions
  api.py            -- REST route definitions
  notifier.py       -- ntfy.sh POST helper
  schema.sql        -- SQLite CREATE TABLE statements (also executed by database.py on init)
```

**`config.py` (the only file that changes between bench and cottage deployment):**

```python
# Bench Pi4 (home)
DB_PATH       = "/home/pi/kiln_data.db"
NTFY_TOPIC    = "your-unique-kiln-topic"
API_PORT      = 8080

# Pi4 GPIO pin numbers (BCM numbering -- same on bench and cottage)
LORA_RST_PIN  = 17
LORA_DIO0_PIN = 25
LORA_SPI_BUS  = 0
LORA_SPI_DEV  = 0    # CE0

# LoRa RF params -- must match Pico side
LORA_FREQ_HZ  = 433_000_000
LORA_SF       = 9
LORA_BW_HZ    = 125_000
```

**Operation:**
1. On start: initialise Ra-02 via SPI, configure for continuous receive mode
2. DIO0 interrupt fires on packet received -> read payload, parse JSON
3. Write telemetry record to SQLite `telemetry` table with Pi4 wall-clock timestamp
4. If packet is an alert, write to `alerts` table and POST to ntfy.sh
5. REST API runs concurrently (thread or async) serving Kivy app queries
6. Daemon runs continuously; started at boot via systemd unit file

---

## SQLite Schema

```sql
-- schema.sql

CREATE TABLE IF NOT EXISTS telemetry (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,   -- Unix timestamp from Pico RTC
    received_at     INTEGER NOT NULL,   -- Unix timestamp on Pi4 (wall clock)
    stage           TEXT,
    temp_lumber     REAL,
    temp_intake     REAL,
    humidity_lumber REAL,
    humidity_intake REAL,
    mc_channel_1    REAL,
    mc_channel_2    REAL,
    exhaust_fan_rpm INTEGER,
    exhaust_fan_pct INTEGER,
    circ_fan_on     INTEGER,            -- 0/1
    heater_on       INTEGER,            -- 0/1
    vent_open       INTEGER,            -- 0/1
    lora_rssi       INTEGER,            -- dBm, measured at Pi4 receiver
    lora_snr        REAL
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    received_at INTEGER NOT NULL,
    code        TEXT NOT NULL,
    message     TEXT,
    value       REAL,
    limit_val   REAL,
    lora_rssi   INTEGER,
    lora_snr    REAL
);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    INTEGER NOT NULL,
    ended_at      INTEGER,
    schedule_name TEXT,
    completed     INTEGER DEFAULT 0     -- 0/1
);

CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry(ts);
CREATE INDEX IF NOT EXISTS idx_alerts_ts    ON alerts(ts);
```

---

## REST API Endpoints

Base URL: `http://<pi4-ip>:8080`

All responses are JSON.

| Method | Path       | Description                                                             |
|--------|------------|-------------------------------------------------------------------------|
| GET    | `/status`  | Latest telemetry record                                                 |
| GET    | `/history` | Time-series data; params: `start`, `end` (Unix ts), `fields` (CSV list)|
| GET    | `/alerts`  | Recent alerts; param: `limit` (default 50)                             |
| GET    | `/runs`    | List of drying runs with start/end times                                |
| GET    | `/health`  | Daemon uptime, last packet received timestamp, total packet count       |

**Example `/history` response (columnar format for Kivy plotting):**

```json
{
  "fields": ["ts", "temp_lumber", "humidity_lumber", "mc_channel_1"],
  "rows": [
    [1700000000, 52.3, 61.2, 18.4],
    [1700000030, 52.5, 60.9, 18.3]
  ]
}
```

The columnar format minimises payload size for long runs -- more efficient than an array
of objects when plotting hundreds or thousands of data points.

---

## Alert Codes

Alerts are generated on the Pico and transmitted as LoRa packets. The Pi4 daemon writes
them to the `alerts` table and triggers ntfy.sh notifications.

| Code             | Source | Condition                                              |
|------------------|--------|--------------------------------------------------------|
| `OVER_TEMP`      | Pico   | Either SHT31 zone exceeds stage limit                  |
| `SENSOR_FAIL`    | Pico   | SHT31 read error or timeout                            |
| `FAN_STALL`      | Pico   | Exhaust fan tach reads 0 RPM when commanded on         |
| `HEATER_TIMEOUT` | Pico   | SSR on continuously beyond safety threshold            |
| `SD_FAIL`        | Pico   | SD card write error                                    |
| `LORA_TIMEOUT`   | Pico   | TxDone flag not seen within 2000 ms polling window     |
| `STAGE_COMPLETE` | Pico   | Drying schedule stage transition (informational)       |
| `SCHEDULE_DONE`  | Pico   | Full drying schedule completed                         |

---

## Phone Notifications -- ntfy.sh

The Pi4 daemon POSTs directly to ntfy.sh on alert receipt. No MQTT broker or separate
notification script required.

```python
# notifier.py
import requests

def send_alert(topic, code, message):
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=f"{code}: {message}",
            headers={"Priority": "high", "Tags": "warning"},
            timeout=5
        )
    except Exception:
        pass  # never let notification failure affect daemon operation
```

Install the ntfy app on phone and subscribe to the configured topic name. Alert delivery
is best-effort -- the kiln runs safely whether or not the notification goes through.

---

## Systemd Service

```ini
# /etc/systemd/system/kiln-server.service
[Unit]
Description=Kiln LoRa Telemetry Server
After=network.target

[Service]
ExecStart=/usr/bin/python3 -m kiln_server
WorkingDirectory=/home/pi/kiln_server
Restart=always
RestartSec=5
User=pi

[Install]
WantedBy=multi-user.target
```

Enable: `sudo systemctl enable kiln-server && sudo systemctl start kiln-server`

---

## Bench Test Plan

Both Pi4 units are identical -- the bench test exactly duplicates the cottage deployment.
Only `config.py` changes between environments.

**End-to-end bench test:**
1. Wire bench Pi4 to Ra-02 per Pi4 wiring table above
2. Enable SPI: `sudo raspi-config` -> Interface Options -> SPI -> Enable; reboot
3. Install dependencies: `pip install spidev RPi.GPIO flask requests`
4. Start daemon: `python -m kiln_server`
5. Wire Pico to Ra-02 per Pico wiring table (DIO0 unconnected); flash real `lib/lora.py`
6. Run `lora_test.py` on Pico -- transmit a packet every 5s
7. Verify TxDone flag is seen via register polling within expected airtime (~330 ms for 100 bytes at SF9)
8. Confirm rows in SQLite: `sqlite3 kiln_data.db "SELECT * FROM telemetry LIMIT 5;"`
9. Confirm REST API: `curl http://localhost:8080/status`
10. Trigger a test alert from Pico; confirm ntfy.sh notification on phone
11. Force a `LORA_TIMEOUT` by asserting CS high during TX; verify alert code fires correctly
12. Short-range RSSI will be very strong (-40 to -60 dBm) -- that is expected

**Pre-deployment check at cottage:**
1. Deploy cottage Pi4 with identical setup; update `config.py` for cottage network
2. Start `kiln_server` as systemd service
3. Power Pico at kiln location
4. Query `/health` -- watch `last_packet_ts` and packet count increment
5. Target RSSI: better than -115 dBm at SF9. Expect -90 to -105 dBm for 200m wooded path.
6. If RSSI marginal, increase SF to SF10 or SF11 in `config.py` and Pico constructor
   (3-6 dB link budget gain per SF step, at cost of longer air time)

---

## BOM

| Item                                    | Qty | Est. Cost (CAD) | Notes                          |
|-----------------------------------------|-----|-----------------|--------------------------------|
| AI-Thinker Ra-02 433 MHz + IPEX antenna | 2   | ~$8             | Ordered from AliExpress        |
| 100 nF ceramic capacitor                | 2   | ~$0.10          | Decoupling; likely in stock    |
| Female-female jumper wires              | 6   | ~$1             | Pi4 GPIO header connection     |

Both Pi4 units are already owned. ESP32-WROOM-32 devboard retained as an optional tool
for bench-testing Pico LoRa TX before the Pi4 daemon is ready; not part of production
system.

---

## Open Items

- [x] Ra-02 modules ordered (arriving ~2 weeks)
- [x] GPIO pin assignments finalised: GP10-13 SPI1, GP28 RST; DIO0 not connected on Pico
- [x] GP20 confirmed as display button (freed from LoRa DIO0)
- [x] Moisture probe AC excitation confirmed on GP6/GP7 in `lib/moisture.py`
- [x] Architecture decision: Ra-02 wired directly to Pi4; no ESP32 in production
- [ ] Implement real `lib/lora.py` on Pico to replace mock (after Ra-02 arrives)
- [ ] Implement `kiln_server/` Python package (Claude Code)
- [ ] Enable SPI on bench Pi4 and wire Ra-02
- [ ] Bench test: Pico TX -> Pi4 RX -> SQLite -> REST API -> ntfy.sh
- [ ] Choose ntfy.sh topic name
- [ ] Write and enable systemd unit file on cottage Pi4
- [ ] Kivy app REST API integration (separate spec, to be written)