-- kiln_server SQLite schema.
-- Applied by database.py on first run (all CREATE statements are IF NOT EXISTS).

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
    heater_on       INTEGER,
    vent_open       INTEGER,
    vent_reason     TEXT,
    exhaust_fan_pct INTEGER,
    exhaust_fan_rpm INTEGER,
    circ_fan_on     INTEGER,
    circ_fan_pct    INTEGER,
    current_12v_ma  REAL,
    current_5v_ma   REAL,
    faults          TEXT,                   -- comma-joined fault codes (nullable)
    lora_rssi       INTEGER,
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
    completed     INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_telemetry_ts  ON telemetry(ts);
CREATE INDEX IF NOT EXISTS idx_telemetry_run ON telemetry(run_id);
CREATE INDEX IF NOT EXISTS idx_alerts_ts     ON alerts(ts);
CREATE INDEX IF NOT EXISTS idx_alerts_run    ON alerts(run_id);
