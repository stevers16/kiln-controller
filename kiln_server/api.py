"""Flask REST API for kiln_server.

All endpoints are read-only. No authentication (LAN-only assumption).
Response schemas match Specs/Pi4_demon_spec.md.

Field naming
------------
The Pi4 daemon stores telemetry under the column names the Pico sends in
its compact LoRa JSON (mc_channel_1, exhaust_fan_pct, circ_fan_pct,
faults). The Kivy app's Dashboard / Alerts / Runs screens originally
target the Pico's HTTP /status surface, which exposes a richer set of
derived fields (run_active, active_run_id, schedule_name, stage_index,
fault_details, target_*, mc_resistance_*). To keep both modes presenting
the same shape on the Kivy side, /status, /alerts, and /runs synthesize
the missing aliases from what the daemon does know. Anything that
genuinely cannot be derived from telemetry alone (per-stage targets,
mc_resistance_*, stage_elapsed_h vs stage_min_h) is returned as null
so the Kivy code's None-tolerant rendering kicks in.
"""

from __future__ import annotations

import datetime
import logging
import time
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request

from . import __version__
from .database import HISTORY_FIELDS, Database
from .lora_receiver import LoraReceiver
from .notifier import Notifier

log = logging.getLogger(__name__)


# -----------------------------------------------------------------------
# Alert tier table (mirror of main.py ALERT_CODE_TIERS on the Pico)
# Used to synthesize `tier` and `level` on /alerts rows and to build
# fault_details on /status. Codes outside this dict default to "info".
# Comparisons are case-insensitive.
# -----------------------------------------------------------------------
ALERT_CODE_TIERS = {
    # Hardware / firmware faults
    "CIRC_FAN_FAULT": "fault",
    "EXHAUST_FAN_STALL": "fault",
    "VENT_STALL": "fault",
    "SENSOR_LUMBER_FAIL": "fault",
    "SENSOR_INTAKE_FAIL": "fault",
    "SENSOR_FAILURE": "fault",
    "MOISTURE_PROBE_FAIL": "fault",
    "HEATER_TIMEOUT": "fault",
    "HEATER_FAULT": "fault",
    "TEMP_OOR": "fault",
    "RH_OOR": "fault",
    "TEMP_OUT_OF_RANGE": "fault",
    "RH_OUT_OF_RANGE": "fault",
    "OVER_TEMP": "fault",
    "SD_FAIL": "fault",
    "SD_WRITE_FAIL": "fault",
    "LORA_FAIL": "fault",
    "LORA_TIMEOUT": "fault",
    "CURRENT_12V_FAIL": "fault",
    "CURRENT_5V_FAIL": "fault",
    "DISPLAY_FAIL": "fault",
    "MODULE_CHECK_FAILED": "fault",
    # Procedural notices
    "STAGE_GOAL_NOT_MET": "notice",
    "STAGE_GOAL_NOT_REACHED": "notice",
    "WATER_PAN_REMINDER": "notice",
}


def _classify_tier(code: Optional[str]) -> str:
    if not code:
        return "info"
    return ALERT_CODE_TIERS.get(code.upper(), "info")


def _level_for_tier(tier: str) -> str:
    if tier == "fault":
        return "ERROR"
    if tier == "notice":
        return "WARN"
    return "INFO"


# Telemetry fields older than this are treated as stale (run not active).
# 30s heartbeat + ~2 missed beats = 90s.
STALE_TELEMETRY_S = 90


def create_app(
    db: Database,
    receiver: LoraReceiver,
    environment: str,
    notifier: Optional[Notifier] = None,
) -> Flask:
    app = Flask(__name__)
    # Keep JSON output compact; the /history endpoint can return thousands of rows.
    app.json.compact = True              # type: ignore[attr-defined]

    # ---------------------------------------------------------------- health
    @app.get("/health")
    def health():
        stats = receiver.health()
        body = {
            "status": "ok",
            "environment": environment,
            "version": __version__,
            "db_size_bytes": db.db_size_bytes(),
            **stats,
        }
        if notifier is not None:
            body["ntfy_topic"] = notifier.topic
            body["ntfy_url"] = notifier.url
        return jsonify(body)

    # ---------------------------------------------------------------- status
    @app.get("/status")
    def status():
        latest = db.latest_telemetry()
        if latest is None:
            return jsonify({"status": "no_data", "run_active": False})
        receiver_stats = receiver.health()
        return jsonify(_coerce_status(latest, db, receiver_stats))

    # --------------------------------------------------------------- history
    @app.get("/history")
    def history():
        raw_fields = request.args.get("fields")
        if raw_fields:
            fields = [f.strip() for f in raw_fields.split(",") if f.strip()]
        else:
            fields = list(HISTORY_FIELDS)

        run_id = _parse_int(request.args.get("run_id"))
        # The Kivy History screen (designed against the Pico /history) sends
        # `run=<id>` rather than `run_id=<id>`. Accept both so the same Kivy
        # code path works against either daemon.
        if run_id is None:
            run_id = _parse_int(request.args.get("run"))
        start = _parse_int(request.args.get("start"))
        end = _parse_int(request.args.get("end"))
        resolution = _parse_int(request.args.get("resolution")) or 1
        resolution = max(1, resolution)

        # Default range: the latest run, or "everything".
        if start is None or end is None:
            if run_id is None:
                run_id = db.active_run_id()
                if run_id is None:
                    # Fall back to the most recent closed run.
                    runs = db.list_runs(limit=1)
                    if runs:
                        run_id = runs[0]["id"]
            if run_id is not None:
                bounds = db.run_bounds(run_id)
                if bounds:
                    if start is None:
                        start = bounds[0]
                    if end is None:
                        end = bounds[1] or int(time.time())

        if start is None:
            start = 0
        if end is None:
            end = int(time.time())

        # Ensure ts and stage are always returned so clients can anchor rows.
        if "ts" not in fields:
            fields = ["ts"] + fields

        rows = db.query_history(
            fields=fields,
            start=start,
            end=end,
            resolution=resolution,
            run_id=run_id,
        )
        # Echo only the fields that passed the whitelist.
        safe_fields = [f for f in fields if f in HISTORY_FIELDS]
        return jsonify({
            "fields": safe_fields,
            "rows": rows,
            "run_id": run_id,
            # Kivy History reads the `run` key from the Pico response;
            # echo both names so either works.
            "run": run_id,
            "row_count": len(rows),
        })

    # ---------------------------------------------------------------- alerts
    @app.get("/alerts")
    def alerts():
        limit = _parse_int(request.args.get("limit")) or 50
        run_id = _parse_int(request.args.get("run_id"))
        # Accept the Kivy app's `run=<id>` parameter as a synonym.
        if run_id is None:
            run_id = _parse_int(request.args.get("run"))
        # Kivy sends `level=ERROR|WARN`; translate to a tier filter.
        level = (request.args.get("level") or "").upper() or None
        code = request.args.get("code") or None

        rows = db.list_alerts(limit=limit, run_id=run_id, code=code)
        decorated = [_decorate_alert(r) for r in rows]
        if level:
            decorated = [a for a in decorated if a.get("level") == level]
        return jsonify({
            "alerts": decorated,
            "count": len(decorated),
        })

    # ------------------------------------------------------------------ runs
    @app.get("/runs")
    def runs():
        limit = _parse_int(request.args.get("limit")) or 20
        rows = db.list_runs(limit=limit)
        active_run_id = db.active_run_id()
        return jsonify({
            "runs": [_decorate_run(r, active_run_id) for r in rows],
        })

    return app


# --- helpers ---------------------------------------------------------------

def _parse_int(raw: Optional[str]) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _coerce_status(
    row: Dict[str, Any],
    db: Database,
    receiver_stats: Dict[str, Any],
) -> Dict[str, Any]:
    """Project a telemetry row into the /status shape the Kivy app expects.

    The Pico's HTTP /status surface is the canonical schema for the Kivy
    Dashboard. The daemon synthesises the additional fields the Kivy
    code looks for (run_active, active_run_id, schedule_name,
    stage_index, fault_details, etc.) from the latest telemetry row plus
    the runs table.
    """
    out = dict(row)

    # 0/1 -> bool
    for b in ("heater_on", "vent_open", "circ_fan_on"):
        if out.get(b) is not None:
            out[b] = bool(out[b])

    # Faults: stored as comma-joined text. Expose both as a list (`faults`,
    # backwards-compatible) and as the Pico-shaped pair (`active_alerts`
    # + `fault_details`).
    f = out.get("faults")
    if isinstance(f, str) and f:
        codes = [c for c in f.split(",") if c]
    elif isinstance(f, list):
        codes = list(f)
    else:
        codes = []
    out["faults"] = codes
    out["active_alerts"] = list(codes)
    out["fault_details"] = [
        {
            "code": c,
            "source": "lora",
            "message": "",
            "tier": _classify_tier(c),
        }
        for c in codes
    ]

    # Stage aliases. Telemetry stores `stage` as an int; Kivy reads
    # `stage_index` and `stage_name`. Stage names are not transmitted
    # over LoRa (the Pico would have to send the schedule snapshot too)
    # so fall back to "Stage N" when we have an index.
    stage_idx = row.get("stage")
    if stage_idx is not None:
        try:
            out["stage_index"] = int(stage_idx)
        except (TypeError, ValueError):
            out["stage_index"] = None
    else:
        out["stage_index"] = None
    if out.get("stage_index") is not None and not out.get("stage_name"):
        out["stage_name"] = f"Stage {out['stage_index']}"

    # Run-context fields. The active run lives in the runs table; we
    # report `run_active=True` only when the latest telemetry is fresh
    # *and* the row's run_id matches the active run.
    active_run_id = db.active_run_id()
    out["active_run_id"] = active_run_id
    out["cooldown"] = False  # daemon cannot infer cooldown from telemetry

    last_packet_age_s = receiver_stats.get("last_packet_age_s")
    out["last_packet_age_s"] = last_packet_age_s
    out["last_packet_ts"] = receiver_stats.get("last_packet_ts")

    fresh = (
        last_packet_age_s is not None
        and last_packet_age_s <= STALE_TELEMETRY_S
    )
    out["run_active"] = bool(
        active_run_id is not None
        and out.get("run_id") == active_run_id
        and fresh
    )

    # Pull the schedule name in from the runs table for the active run.
    if active_run_id is not None:
        info = _run_info(db, active_run_id)
        if info:
            out["schedule_name"] = info.get("schedule_name")
            started = info.get("started_at")
            if started and out.get("ts"):
                try:
                    out["total_elapsed_h"] = round(
                        (int(out["ts"]) - int(started)) / 3600.0, 2
                    )
                except (TypeError, ValueError):
                    out["total_elapsed_h"] = None

    # Fields the daemon cannot derive from telemetry alone. Emit them
    # explicitly as null so the Kivy code reads `data.get(...) is None`
    # consistently across both modes.
    for missing in (
        "stage_elapsed_h", "stage_min_h",
        "target_temp_c", "target_rh_pct", "target_mc_pct",
        "mc_resistance_1", "mc_resistance_2",
    ):
        out.setdefault(missing, None)

    return out


def _run_info(db: Database, run_id: int) -> Optional[Dict[str, Any]]:
    """Lightweight lookup of a single run row."""
    c = db.conn()
    row = c.execute(
        "SELECT id, started_at, ended_at, schedule_name, label, completed "
        "FROM runs WHERE id = ?",
        (int(run_id),),
    ).fetchone()
    if not row:
        return None
    return dict(row)


def _decorate_alert(row: Dict[str, Any]) -> Dict[str, Any]:
    """Add the `tier` / `level` / `source` fields the Kivy Alerts screen
    expects on top of the database row. The daemon doesn't track a
    per-alert source (everything arrives over LoRa), so it's hard-coded
    to "lora" - mirrors how the Pico tags injected fault rows.
    """
    out = dict(row)
    code = out.get("code")
    tier = _classify_tier(code)
    out["tier"] = tier
    out["level"] = _level_for_tier(tier)
    out.setdefault("source", "lora")
    return out


def _format_ts(ts: Any) -> Optional[str]:
    """Format a unix timestamp into the YYYY-MM-DD HH:MM string the Pico
    /runs handler emits. Returns None on falsy / unparseable input."""
    if not ts:
        return None
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime(
            "%Y-%m-%d %H:%M"
        )
    except (OSError, ValueError, TypeError):
        return None


def _decorate_run(row: Dict[str, Any], active_run_id: Optional[int]) -> Dict[str, Any]:
    """Adapt a runs row to the shape the Kivy Runs screen expects.

    The Pico /runs handler emits `started_at_str`, `ended_at_str`,
    `data_rows`, `event_count`, and `size_bytes`. The daemon stores
    `started_at` / `ended_at` as unix ints and counts telemetry / alert
    rows separately; alias them so the same Kivy renderer works against
    both. `size_bytes` is left as 0 because the daemon's per-run "size"
    would be SQLite page accounting and not meaningful to the user.
    """
    out = dict(row)
    started = row.get("started_at")
    ended = row.get("ended_at")

    out["started_at_str"] = _format_ts(started)
    out["ended_at_str"] = _format_ts(ended)

    out["data_rows"] = row.get("telemetry_count", 0) or 0
    out["event_count"] = row.get("alert_count", 0) or 0
    out["size_bytes"] = 0

    # Kivy's _RunRow expects the run id under "id" (already present) and
    # tolerates either string or int, so no work needed there. Emit the
    # `active` flag explicitly so dropdown / list code can show ACTIVE
    # without re-querying /status.
    out["active"] = (active_run_id is not None and row.get("id") == active_run_id)
    return out
