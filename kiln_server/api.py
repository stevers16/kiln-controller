"""Flask REST API for kiln_server.

All endpoints are read-only. No authentication (LAN-only assumption).
Response schemas match Specs/Pi4_demon_spec.md.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from flask import Flask, jsonify, request

from . import __version__
from .database import HISTORY_FIELDS, Database
from .lora_receiver import LoraReceiver

log = logging.getLogger(__name__)


def create_app(
    db: Database,
    receiver: LoraReceiver,
    environment: str,
) -> Flask:
    app = Flask(__name__)
    # Keep JSON output compact; the /history endpoint can return thousands of rows.
    app.json.compact = True              # type: ignore[attr-defined]

    # ---------------------------------------------------------------- health
    @app.get("/health")
    def health():
        stats = receiver.health()
        return jsonify({
            "status": "ok",
            "environment": environment,
            "version": __version__,
            "db_size_bytes": db.db_size_bytes(),
            **stats,
        })

    # ---------------------------------------------------------------- status
    @app.get("/status")
    def status():
        latest = db.latest_telemetry()
        if latest is None:
            return jsonify({"status": "no_data"})
        return jsonify(_coerce_status(latest))

    # --------------------------------------------------------------- history
    @app.get("/history")
    def history():
        raw_fields = request.args.get("fields")
        if raw_fields:
            fields = [f.strip() for f in raw_fields.split(",") if f.strip()]
        else:
            fields = list(HISTORY_FIELDS)

        run_id = _parse_int(request.args.get("run_id"))
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
            "row_count": len(rows),
        })

    # ---------------------------------------------------------------- alerts
    @app.get("/alerts")
    def alerts():
        limit = _parse_int(request.args.get("limit")) or 50
        run_id = _parse_int(request.args.get("run_id"))
        code = request.args.get("code") or None

        rows = db.list_alerts(limit=limit, run_id=run_id, code=code)
        return jsonify({
            "alerts": rows,
            "count": len(rows),
        })

    # ------------------------------------------------------------------ runs
    @app.get("/runs")
    def runs():
        limit = _parse_int(request.args.get("limit")) or 20
        return jsonify({"runs": db.list_runs(limit=limit)})

    return app


# --- helpers ---------------------------------------------------------------

def _parse_int(raw: Optional[str]) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _coerce_status(row: Dict[str, Any]) -> Dict[str, Any]:
    """Expose 0/1 ints in telemetry as proper booleans for the Kivy app."""
    out = dict(row)
    for b in ("heater_on", "vent_open", "circ_fan_on"):
        if out.get(b) is not None:
            out[b] = bool(out[b])
    # Faults stored as comma-joined text; return as list.
    f = out.get("faults")
    if isinstance(f, str) and f:
        out["faults"] = f.split(",")
    elif f is None:
        out["faults"] = []
    return out
