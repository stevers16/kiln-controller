"""SQLite storage for kiln_server.

One long-lived connection per thread is the safest pattern with the Python
sqlite3 module. `Database` stores the path and hands out connections via a
thread-local cache. All writes are serialised through a module-level lock so
the receiver thread and the Flask request threads cannot interleave inserts.

Schema lives in schema.sql next to this file and is applied on first open.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_SCHEMA_FILE = Path(__file__).with_name("schema.sql")

# Telemetry columns stored as 0/1 ints in SQLite but exposed as bool via the
# /status API and accepted as bool from the LoRa wire format.
BOOLEAN_TELEMETRY_COLS: Tuple[str, ...] = ("heater_on", "vent_open", "circ_fan_on")

# Telemetry columns the daemon writes. Order matters for insert_telemetry().
TELEMETRY_COLUMNS: Tuple[str, ...] = (
    "ts",
    "received_at",
    "run_id",
    "stage",
    "stage_type",
    "temp_lumber",
    "temp_intake",
    "rh_lumber",
    "rh_intake",
    "mc_channel_1",
    "mc_channel_2",
    "heater_on",
    "vent_open",
    "vent_reason",
    "exhaust_fan_pct",
    "exhaust_fan_rpm",
    "circ_fan_on",
    "circ_fan_pct",
    "current_12v_ma",
    "current_5v_ma",
    "faults",
    "lora_rssi",
    "lora_snr",
)

# Fields the /history endpoint may return. Whitelist -- any client-supplied
# field not in here is rejected to prevent SQL injection via column names.
HISTORY_FIELDS: Tuple[str, ...] = (
    "ts",
    "received_at",
    "stage",
    "stage_type",
    "temp_lumber",
    "temp_intake",
    "rh_lumber",
    "rh_intake",
    "mc_channel_1",
    "mc_channel_2",
    "heater_on",
    "vent_open",
    "vent_reason",
    "exhaust_fan_pct",
    "exhaust_fan_rpm",
    "circ_fan_on",
    "circ_fan_pct",
    "current_12v_ma",
    "current_5v_ma",
    "lora_rssi",
    "lora_snr",
)


class Database:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # --- connection management ---------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    def _init_schema(self) -> None:
        sql = _SCHEMA_FILE.read_text()
        c = self.conn()
        with self._write_lock:
            c.executescript(sql)

    # --- runs ---------------------------------------------------------------

    def open_run(self, started_at: int, schedule_name: Optional[str] = None,
                 label: Optional[str] = None) -> int:
        c = self.conn()
        with self._write_lock:
            cur = c.execute(
                "INSERT INTO runs (started_at, schedule_name, label, completed) "
                "VALUES (?, ?, ?, 0)",
                (int(started_at), schedule_name, label),
            )
            return int(cur.lastrowid)

    def close_run(self, run_id: int, ended_at: int) -> None:
        c = self.conn()
        with self._write_lock:
            c.execute(
                "UPDATE runs SET ended_at = ?, completed = 1 WHERE id = ?",
                (int(ended_at), int(run_id)),
            )

    def active_run_id(self) -> Optional[int]:
        c = self.conn()
        row = c.execute(
            "SELECT id FROM runs WHERE ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return int(row["id"]) if row else None

    def list_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        c = self.conn()
        # Single GROUP BY pass per child table; avoids the N+1 correlated
        # subquery pattern that scaled with telemetry row count.
        rows = c.execute(
            """
            SELECT r.id, r.started_at, r.ended_at, r.schedule_name, r.label,
                   r.completed,
                   COALESCE(t.cnt, 0) AS telemetry_count,
                   COALESCE(a.cnt, 0) AS alert_count
            FROM runs r
            LEFT JOIN (SELECT run_id, COUNT(*) AS cnt FROM telemetry
                       GROUP BY run_id) t ON t.run_id = r.id
            LEFT JOIN (SELECT run_id, COUNT(*) AS cnt FROM alerts
                       GROUP BY run_id) a ON a.run_id = r.id
            ORDER BY r.started_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        out = []
        for r in rows:
            started = r["started_at"]
            ended = r["ended_at"]
            duration_h = None
            if ended is not None and started is not None:
                duration_h = round((ended - started) / 3600.0, 2)
            out.append({
                "id": r["id"],
                "started_at": started,
                "ended_at": ended,
                "duration_h": duration_h,
                "schedule_name": r["schedule_name"],
                "label": r["label"],
                "completed": bool(r["completed"]),
                "telemetry_count": r["telemetry_count"],
                "alert_count": r["alert_count"],
            })
        return out

    def run_bounds(self, run_id: int) -> Optional[Tuple[int, Optional[int]]]:
        c = self.conn()
        row = c.execute(
            "SELECT started_at, ended_at FROM runs WHERE id = ?",
            (int(run_id),),
        ).fetchone()
        if not row:
            return None
        return (int(row["started_at"]), row["ended_at"])

    def get_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        c = self.conn()
        row = c.execute(
            "SELECT id, started_at, ended_at, schedule_name, label, completed "
            "FROM runs WHERE id = ?",
            (int(run_id),),
        ).fetchone()
        return dict(row) if row else None

    # --- telemetry ----------------------------------------------------------

    def insert_telemetry(self, record: Dict[str, Any]) -> int:
        values = tuple(record.get(col) for col in TELEMETRY_COLUMNS)
        placeholders = ",".join("?" for _ in TELEMETRY_COLUMNS)
        cols = ",".join(TELEMETRY_COLUMNS)
        c = self.conn()
        with self._write_lock:
            cur = c.execute(
                f"INSERT INTO telemetry ({cols}) VALUES ({placeholders})",
                values,
            )
            return int(cur.lastrowid)

    def latest_telemetry(self) -> Optional[Dict[str, Any]]:
        c = self.conn()
        row = c.execute(
            "SELECT * FROM telemetry ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d.pop("id", None)
        return d

    def telemetry_count(self) -> int:
        c = self.conn()
        row = c.execute("SELECT COUNT(*) AS n FROM telemetry").fetchone()
        return int(row["n"]) if row else 0

    def query_history(
        self,
        fields: Iterable[str],
        start: int,
        end: int,
        resolution: int = 1,
        run_id: Optional[int] = None,
    ) -> List[List[Any]]:
        # Validate field names against whitelist.
        safe_fields = [f for f in fields if f in HISTORY_FIELDS]
        if not safe_fields:
            safe_fields = ["ts"]
        cols = ",".join(safe_fields)

        params: List[Any] = [int(start), int(end)]
        sql = f"SELECT {cols} FROM telemetry WHERE ts BETWEEN ? AND ?"
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(int(run_id))
        # Decimate at SQL time so a 100k-row run with resolution=10 doesn't
        # materialise 100k Row objects in memory just to discard 90% of them.
        # `id` is an INTEGER PRIMARY KEY AUTOINCREMENT (insert-order monotonic),
        # so id-stride approximates the previous Python `rows[::resolution]`.
        if resolution > 1:
            sql += " AND (id % ?) = 0"
            params.append(int(resolution))
        sql += " ORDER BY ts ASC"

        c = self.conn()
        rows = c.execute(sql, params).fetchall()
        return [[row[f] for f in safe_fields] for row in rows]

    # --- alerts -------------------------------------------------------------

    def insert_alert(self, record: Dict[str, Any]) -> int:
        c = self.conn()
        with self._write_lock:
            cur = c.execute(
                """
                INSERT INTO alerts
                    (ts, received_at, run_id, code, message,
                     value, limit_val, lora_rssi, lora_snr)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(record.get("ts") or int(time.time())),
                    int(record.get("received_at") or int(time.time())),
                    record.get("run_id"),
                    record["code"],
                    record.get("message"),
                    record.get("value"),
                    record.get("limit_val"),
                    record.get("lora_rssi"),
                    record.get("lora_snr"),
                ),
            )
            return int(cur.lastrowid)

    def list_alerts(
        self,
        limit: int = 50,
        run_id: Optional[int] = None,
        code: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        params: List[Any] = []
        sql = "SELECT * FROM alerts WHERE 1=1"
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(int(run_id))
        if code:
            sql += " AND code = ?"
            params.append(code)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        c = self.conn()
        rows = c.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # --- misc --------------------------------------------------------------

    def db_size_bytes(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0
