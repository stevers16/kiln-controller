# PI4_DAEMON_SPEC.md

Spec for the `kiln_server` Python daemon running on the Raspberry Pi 4 at the
cottage. This document supersedes the informal daemon sketch in
`lora_telemetry_spec.md` and adds all endpoints and database details required
by the Kivy app.

---

## Overview

The Pi4 daemon (`kiln_server`) is a long-running Python process that:

1. Receives LoRa telemetry packets from the kiln Pico via a Ra-02 SX1278 module
   wired to the Pi4 SPI0 bus
2. Writes all telemetry and alerts to a SQLite database
3. Serves a read-only REST API (port 8080) for the Kivy app
4. Pushes fault alerts to a phone via ntfy.sh

No control commands are relayed to the Pico from the Pi4. The Pi4 API is
strictly read-only. Control requires direct AP connection from the app to the
Pico.

---

## Package Structure

```
kiln_server/
    __main__.py         -- entry point; starts all threads/tasks
    config.py           -- environment-specific settings (only file that differs
                           between bench and cottage deployments)
    lora_receiver.py    -- SX1278 SPI init, DIO0 interrupt, receive loop
    database.py         -- SQLite schema, insert and query functions
    api.py              -- Flask REST endpoints
    notifier.py         -- ntfy.sh HTTP POST
    schema.sql          -- SQL schema (applied on first run)
```

---

## Hardware

Ra-02 SX1278 wired to Pi4 GPIO header via SPI0. See `lora_telemetry_spec.md`
for full wiring table. SPI must be enabled via `raspi-config`.

---

## config.py

The only file that differs between bench and cottage deployments.

```python
# config.py

ENVIRONMENT   = "cottage"          # "bench" or "cottage" -- shown in /health

# LoRa SPI
SPI_BUS       = 0
SPI_CE        = 0                  # CE0 = GPIO8
DIO0_PIN      = 25                 # GPIO25
RST_PIN       = 17                 # GPIO17
LORA_SF       = 9
LORA_FREQ_MHZ = 433.0

# Database
DB_PATH       = "/home/srelias/CottageKiln/kiln_data.db"

# API
API_HOST      = "0.0.0.0"
API_PORT      = 8080

# Notifications
NTFY_TOPIC    = "kiln-cottage-abc123"   # choose a unique topic name
NTFY_URL      = "https://ntfy.sh"

# Alert suppression (do not re-notify same code within this window)
ALERT_SUPPRESS_S = 1800            # 30 minutes
```

---

## SQLite Schema

Applied from `schema.sql` on first run if tables do not exist.

```sql
CREATE TABLE IF NOT EXISTS telemetry (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,       -- Unix timestamp from Pico RTC
    received_at     INTEGER NOT NULL,       -- Unix timestamp on Pi4 (wall clock)
    run_id          INTEGER,                -- FK to runs.id (nullable)
    stage           INTEGER,
    stage_type      TEXT,
    temp_lumber     REAL,
    temp_intake     REAL,
    rh_lumber       REAL,
    rh_intake       REAL,
    mc_channel_1    REAL,
    mc_channel_2    REAL,
    heater_on       INTEGER,                -- 0/1
    vent_open       INTEGER,                -- 0/1
    vent_reason     TEXT,
    exhaust_fan_pct INTEGER,
    exhaust_fan_rpm INTEGER,
    circ_fan_on     INTEGER,                -- 0/1
    circ_fan_pct    INTEGER,
    current_12v_ma  REAL,
    current_5v_ma   REAL,
    lora_rssi       INTEGER,                -- dBm measured at Pi4
    lora_snr        REAL
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    received_at INTEGER NOT NULL,
    run_id      INTEGER,
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
    label         TEXT,
    completed     INTEGER DEFAULT 0         -- 0/1
);

CREATE INDEX IF NOT EXISTS idx_telemetry_ts    ON telemetry(ts);
CREATE INDEX IF NOT EXISTS idx_telemetry_run   ON telemetry(run_id);
CREATE INDEX IF NOT EXISTS idx_alerts_ts       ON alerts(ts);
CREATE INDEX IF NOT EXISTS idx_alerts_run      ON alerts(run_id);
```

---

## LoRa Packet Parsing

The Pico transmits two packet types:

**Telemetry packet** (JSON, transmitted every 30s during a run):

```json
{
  "type": "telemetry",
  "ts": 1711900000,
  "stage": 2,
  "stage_type": "drying",
  "temp_lumber": 48.7,
  "rh_lumber": 71.2,
  "temp_intake": 22.4,
  "rh_intake": 55.1,
  "mc_channel_1": 27.3,
  "mc_channel_2": 26.8,
  "heater_on": false,
  "vent_open": false,
  "vent_reason": null,
  "exhaust_fan_pct": 0,
  "exhaust_fan_rpm": 0,
  "circ_fan_on": true,
  "circ_fan_pct": 75,
  "current_12v_ma": 412.0,
  "current_5v_ma": 89.0
}
```

**Alert packet** (semicolon-delimited ASCII string):

```
ALERT;{code};stage={n};temp={t:.1f};rh={r:.1f}[;{extra}]
```

Examples:
```
ALERT;rh_out_of_range;stage=2;temp=48.7;rh=82.1
ALERT;heater_fault;stage=3;temp=45.2;rh=70.1
ALERT;equalizing_start;stage=7;temp=63.0;rh=55.0;add water pans if needed
ALERT;sensor_failure;stage=2
ALERT;run_complete;stage=8;temp=63.0;rh=48.0
```

The daemon detects packet type by attempting JSON parse first. If JSON parse
fails, treat as an alert string. Unknown formats are logged and discarded.

**Run lifecycle detection:**
- `run_started` alert: open a new `runs` row, record `started_at`
- `run_complete` alert: close the active `runs` row, set `ended_at` and
  `completed=1`
- If daemon restarts with an open run in the DB, leave it open -- the Pico will
  send a `run_complete` alert when the run ends

**run_id assignment:**
- All telemetry and alert rows received while a run is open get the current
  `runs.id` as `run_id`
- Rows received outside a run have `run_id = NULL`

---

## REST API Endpoints

Base URL: `http://<pi4-ip>:8080`

All responses are JSON. No authentication required (LAN-only, read-only).

---

### GET /health

Daemon status and environment info.

**Response:**

```json
{
  "status": "ok",
  "environment": "cottage",
  "uptime_s": 86412,
  "last_packet_ts": 1711900000,
  "last_packet_age_s": 28,
  "total_packets": 8642,
  "db_size_bytes": 2048000,
  "lora_rssi_last": -94,
  "lora_snr_last": 7.2
}
```

---

### GET /status

Latest telemetry record from the database.

**Response:**

```json
{
  "ts": 1711900000,
  "received_at": 1711900002,
  "run_id": 3,
  "stage": 2,
  "stage_type": "drying",
  "temp_lumber": 48.7,
  "rh_lumber": 71.2,
  "temp_intake": 22.4,
  "rh_intake": 55.1,
  "mc_channel_1": 27.3,
  "mc_channel_2": 26.8,
  "heater_on": false,
  "vent_open": false,
  "vent_reason": null,
  "exhaust_fan_pct": 0,
  "exhaust_fan_rpm": 0,
  "circ_fan_on": true,
  "circ_fan_pct": 75,
  "current_12v_ma": 412.0,
  "current_5v_ma": 89.0,
  "lora_rssi": -94,
  "lora_snr": 7.2
}
```

Returns `{"status": "no_data"}` if no telemetry rows exist.

---

### GET /history

Time-series telemetry data for graphing.

**Query parameters:**

| Parameter | Type | Default | Notes |
|-----------|------|---------|-------|
| `start` | int | run start | Unix timestamp |
| `end` | int | now | Unix timestamp |
| `fields` | string | all | Comma-separated column names |
| `resolution` | int | 1 | Return every Nth row (server-side decimation) |
| `run_id` | int | latest run | Filter by run |

Available fields:
```
ts, received_at, stage, stage_type, temp_lumber, temp_intake, rh_lumber,
rh_intake, mc_channel_1, mc_channel_2, heater_on, vent_open, vent_reason,
exhaust_fan_pct, exhaust_fan_rpm, circ_fan_on, current_12v_ma, current_5v_ma,
lora_rssi, lora_snr
```

**Response (columnar format):**

```json
{
  "fields": ["ts", "temp_lumber", "rh_lumber", "mc_channel_1"],
  "rows": [
    [1711900000, 48.7, 71.2, 27.3],
    [1711900030, 48.9, 70.8, 27.1]
  ],
  "run_id": 3,
  "row_count": 2
}
```

**Implementation note:** Decimation is applied in SQL using `WHERE rowid % N = 0`
approximation or by row-number filtering in Python after query. For `resolution=1`
(no decimation), return all rows in range.

---

### GET /alerts

Recent alerts from the database.

**Query parameters:**

| Parameter | Type | Default | Notes |
|-----------|------|---------|-------|
| `limit` | int | 50 | Max rows |
| `run_id` | int | all | Filter by run |
| `code` | string | all | Filter by alert code |

**Response:**

```json
{
  "alerts": [
    {
      "id": 42,
      "ts": 1711900000,
      "received_at": 1711900002,
      "run_id": 3,
      "code": "rh_out_of_range",
      "message": "ALERT;rh_out_of_range;stage=2;temp=48.7;rh=82.1",
      "value": 82.1,
      "limit_val": 78.0,
      "lora_rssi": -94,
      "lora_snr": 7.2
    }
  ],
  "count": 1
}
```

Alerts are returned in reverse chronological order (newest first).

---

### GET /runs

List of all drying runs.

**Query parameters:**

| Parameter | Type | Default | Notes |
|-----------|------|---------|-------|
| `limit` | int | 20 | Max rows |

**Response:**

```json
{
  "runs": [
    {
      "id": 3,
      "started_at": 1711800000,
      "ended_at": 1711900000,
      "duration_h": 27.8,
      "schedule_name": "Hard Maple 1 inch",
      "label": "Workshop maple batch 1",
      "completed": true,
      "telemetry_count": 3336,
      "alert_count": 4
    }
  ]
}
```

An active run has `ended_at: null` and `completed: false`.
`duration_h` is null for active runs.

---

## Notification -- ntfy.sh

```python
# notifier.py

import requests

def send_alert(topic, code, message):
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=f"{code}: {message}",
            headers={
                "Priority": "high",
                "Tags": "warning",
                "Title": "Kiln Alert"
            },
            timeout=5
        )
    except Exception:
        pass    # never let notification failure affect daemon operation
```

**Alert suppression:** The daemon tracks the last notification time per alert
code (in memory). The same code is not re-notified within `ALERT_SUPPRESS_S`
seconds (default 30 minutes). One-shot alerts (`run_complete`, `equalizing_start`,
`conditioning_start`) bypass suppression.

**Phone setup:** Install the ntfy app and subscribe to the configured topic name.
Topic name is displayed in the Kivy app Settings screen (from `/health` response).

---

## Systemd Service

```ini
# /etc/systemd/system/kiln-server.service

[Unit]
Description=Kiln LoRa Telemetry Server
After=network.target

[Service]
ExecStart=/usr/bin/python3 -m kiln_server
WorkingDirectory=/home/srelias/CottageKiln/kiln_server
Restart=always
RestartSec=5
User=pi
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Enable and start:
```
sudo systemctl enable kiln-server
sudo systemctl start kiln-server
```

View logs:
```
journalctl -u kiln-server -f
```

---

## Dependencies

```
flask
spidev
RPi.GPIO
requests
```

Install:
```
pip3 install flask spidev RPi.GPIO requests
```

---

## Bench vs. Cottage Deployment

The bench Pi4 and cottage Pi4 are identical in software. Only `config.py` differs:

| Setting | Bench | Cottage |
|---------|-------|---------|
| `ENVIRONMENT` | "bench" | "cottage" |
| `DB_PATH` | `/home/srelias/CottageKiln/kiln_bench.db` | `/home/srelias/CottageKiln/kiln_data.db` |
| `NTFY_TOPIC` | (optional bench topic) | (production topic) |

The Kivy app Settings screen shows the `ENVIRONMENT` value from `/health` so the
operator can confirm which Pi4 they are connected to.

---

## Bench Test Plan

1. Enable SPI on bench Pi4: `sudo raspi-config` -> Interface Options -> SPI -> Enable; reboot
2. Wire Ra-02 to Pi4 per `lora_telemetry_spec.md` wiring table
3. Install dependencies
4. Set `config.py` to `ENVIRONMENT = "bench"`
5. Start daemon: `python3 -m kiln_server`
6. Wire Pico to Ra-02 per Pico wiring; flash real `lib/lora.py`
7. Run `lora_test.py` on Pico -- transmit packets every 5s
8. Confirm SQLite rows: `sqlite3 kiln_bench.db "SELECT * FROM telemetry LIMIT 5;"`
9. Confirm REST API: `curl http://localhost:8080/status`
10. Trigger a test alert from Pico; confirm ntfy.sh notification on phone
11. Query `/health` -- confirm `last_packet_age_s` increments then resets

---

## Open Items

- [ ] Implement `kiln_server/` Python package per this spec
- [ ] Enable SPI on bench Pi4 and wire Ra-02
- [ ] Bench end-to-end test (steps above)
- [ ] Choose ntfy.sh topic name for cottage deployment
- [ ] Write and enable systemd unit file on cottage Pi4
- [ ] Pre-deployment RSSI check at cottage (target better than -115 dBm at SF9)
