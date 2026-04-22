"""HTTP client for the Pico AP and Pi4 daemon REST APIs.

Phase 2: only the methods autodetect + Settings need are implemented (`health`,
`status`). Later phases extend this with `history`, `alerts`, `runs`, `start`,
`stop`, `advance`, etc.

Threading model
---------------
`requests` is blocking, but Kivy's main thread must never block. The
`call_async` helper runs a function on a worker thread and delivers the
result to the Kivy main thread via `Clock.schedule_once`. All callers in the
app should use `call_async`; never call client methods on the main thread.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

import requests
from kivy.clock import Clock


DEFAULT_TIMEOUT_S = 5.0
PROBE_TIMEOUT_S = 3.0


class ApiError(Exception):
    """Anything that prevented us from getting a usable response."""


class AuthError(ApiError):
    """HTTP 401 - bad or missing API key."""


class TimeoutError_(ApiError):
    """Network timeout."""


@dataclass
class ClientConfig:
    base_url: Optional[str] = None
    api_key: str = ""
    requires_auth: bool = True   # Pico requires it; Pi4 does not
    timeout: float = DEFAULT_TIMEOUT_S


class KilnApiClient:
    """Thin wrapper over `requests`.

    A single instance is shared by the whole app. The `ConnectionManager`
    rewrites `config.base_url`, `config.api_key`, and `config.requires_auth`
    every time autodetect picks a new endpoint.
    """

    def __init__(self) -> None:
        self.config = ClientConfig()

    # ---- low-level helpers -------------------------------------------------

    def _headers(self, auth_required: bool) -> dict:
        h = {"Accept": "application/json"}
        if auth_required and self.config.requires_auth and self.config.api_key:
            h["X-Kiln-Key"] = self.config.api_key
        return h

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        auth: bool = True,
    ) -> Any:
        url = (base_url or self.config.base_url or "").rstrip("/") + path
        if not url.startswith("http"):
            raise ApiError("no base url configured")
        try:
            resp = requests.request(
                method,
                url,
                headers=self._headers(auth),
                json=json,
                timeout=timeout or self.config.timeout,
            )
        except requests.Timeout as e:
            raise TimeoutError_(str(e)) from e
        except requests.RequestException as e:
            raise ApiError(str(e)) from e

        if resp.status_code == 401:
            raise AuthError("unauthorized (HTTP 401)")
        if resp.status_code >= 400:
            # Try to surface the server's error message if it's JSON
            body = resp.text or ""
            try:
                data = resp.json()
                if isinstance(data, dict):
                    body = data.get("error") or data.get("message") or str(data)
            except ValueError:
                pass
            raise ApiError(f"HTTP {resp.status_code}: {body[:200]}")
        if not resp.text:
            return None
        try:
            return resp.json()
        except ValueError as e:
            raise ApiError(f"non-JSON response: {e}") from e

    def _get(
        self,
        path: str,
        *,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        auth: bool = True,
    ) -> Any:
        return self._request("GET", path, base_url=base_url, timeout=timeout, auth=auth)

    def _post(self, path: str, *, json: Any = None) -> Any:
        return self._request("POST", path, json=json)

    # ---- public endpoints (Phase 2 subset) ---------------------------------

    def health(
        self,
        *,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """GET /health - no auth on Pico, no auth on Pi4."""
        return self._get("/health", base_url=base_url, timeout=timeout, auth=False)

    def status(self) -> Any:
        """GET /status - requires auth on Pico."""
        return self._get("/status")

    def health_current(self) -> Any:
        """GET /health on the currently configured base URL."""
        return self._get("/health", auth=False)

    def alerts(
        self,
        *,
        limit: int = 50,
        level: Optional[str] = None,
        run: Optional[str] = None,
    ) -> Any:
        """GET /alerts. Returns {alerts: [...], count: int}.

        - level: "WARN" or "ERROR". Filter and response level fields
          both use "WARN" (standardised 2026-04-21).
        - run: run id (e.g. '20260408_1730') or None for the current run.
        """
        qs_parts = [f"limit={int(limit)}"]
        if level:
            qs_parts.append(f"level={level}")
        if run:
            qs_parts.append(f"run={run}")
        return self._get("/alerts?" + "&".join(qs_parts))

    def runs(self) -> Any:
        """GET /runs. Returns {runs: [...]} - list of run records derived
        from SD card log files (no SQLite on the Pico).

        Uses a longer timeout than the default because the Pico handler
        scans every data_*.csv + event_*.txt on the SD card and counts
        lines line-by-line over SPI. With many historical runs this can
        comfortably exceed the 5s default. See PROJECT.md 'Known firmware
        bugs' for the planned server-side fix.
        """
        return self._get("/runs", timeout=30.0)

    def run_delete(self, run_id: str) -> Any:
        """DELETE /logs/{run_id}. Removes both the event log and data CSV
        for the run from the SD card. 409 if attempting to delete the
        currently active run.
        """
        return self._request("DELETE", f"/logs/{run_id}")

    def history(
        self,
        *,
        run: Optional[str] = None,
        fields: Optional[list] = None,
        resolution: int = 1,
    ) -> Any:
        """GET /history. Returns columnar {fields: [...], rows: [[...], ...],
        run: str, row_count: int}. Row values are already parsed to int /
        float / None by the Pico handler.

        - run: run id to load; omit for current/most-recent run.
        - fields: subset of columns to return. Default = all columns.
        - resolution: return every Nth row; Pico does line-level decimation.

        Timeout is generous because the Pico streams every row over SPI
        from SD. A multi-day run has thousands of rows; 60s handles the
        worst case comfortably.
        """
        qs = []
        if run:
            qs.append(f"run={run}")
        if fields:
            qs.append("fields=" + ",".join(fields))
        if resolution and resolution > 1:
            qs.append(f"resolution={int(resolution)}")
        path = "/history" + (("?" + "&".join(qs)) if qs else "")
        return self._get(path, timeout=60.0)

    # ---- schedules (Pico AP only - GET list/detail require auth) ----------

    def schedules(self) -> Any:
        """GET /schedules. Returns {schedules: [{filename, name, species,
        thickness_in, stage_count, builtin, size_bytes}, ...]}.
        """
        return self._get("/schedules")

    def schedule_get(self, filename: str) -> Any:
        """GET /schedules/{filename}. Returns the full schedule JSON
        ({name, species, thickness_in, stages: [...]}).
        """
        return self._get(f"/schedules/{filename}")

    def schedule_put(self, filename: str, schedule: dict) -> Any:
        """PUT /schedules/{filename}. Body is the full schedule dict
        ({name, species, thickness_in, stages: [...]}). Pico rejects
        built-in filenames (403) and validates the stage list.
        """
        return self._request("PUT", f"/schedules/{filename}", json=schedule)

    def schedule_delete(self, filename: str) -> Any:
        """DELETE /schedules/{filename}. Pico rejects built-in
        filenames (403) and returns 404 if the file is missing."""
        return self._request("DELETE", f"/schedules/{filename}")

    # ---- run control (Pico AP only - all require auth) --------------------

    def run_start(
        self,
        schedule_filename: Optional[str] = None,
        label: Optional[str] = None,
    ) -> Any:
        """POST /run/start. Body: {schedule: filename, label?: str}.

        The current Pico firmware only consumes `schedule`; `label` is sent
        through for forward compatibility with a future Pi4 daemon / firmware
        that records a user-supplied run label alongside the run record.
        """
        body: dict = {}
        if schedule_filename:
            body["schedule"] = schedule_filename
        if label:
            body["label"] = label
        return self._post("/run/start", json=body)

    def run_stop(self, reason: str = "manual") -> Any:
        """POST /run/stop. Body: {reason: ...}."""
        return self._post("/run/stop", json={"reason": reason})

    def run_advance(self) -> Any:
        """POST /run/advance. Bypasses MC% / time checks server-side; the
        client is expected to gate the button on stage_elapsed_h >= stage_min_h.
        """
        return self._post("/run/advance")

    def run_shutdown(self) -> Any:
        """POST /run/shutdown. Ends cooldown: heater off, fans off, vents
        closed. 409 if a run is currently active (call run_stop first).
        """
        return self._post("/run/shutdown")

    # ---- system test (Pico AP only - all require auth) -------------------

    def test_run(self) -> Any:
        """POST /test/run. Pico returns {ok, test_count, estimated_duration_s}.
        409 if a test is already running; also 409 if a drying run is active.
        """
        return self._post("/test/run")

    def test_status(self) -> Any:
        """GET /test/status. Returns {complete, elapsed_s, tests: [...],
        passed, failed, skipped, pending, overall?}. `overall` is only
        present once complete=True.

        Each test row is {id, name, group, status, detail, duration_ms}
        where status is one of pending / running / pass / fail / skip.
        """
        return self._get("/test/status")

    def set_time(self, unix_ts: int) -> Any:
        """POST /time with a unix timestamp so the Pico can set its RTC.

        The Pico has no battery-backed RTC, so without this call its
        clock defaults to an arbitrary point and all event / data log
        timestamps and ntfy.sh push timestamps are wrong. Call on
        connect and any time the user hits 'Sync Pico clock now' in
        Settings.
        """
        return self._post("/time", json={"ts": int(unix_ts)})

    # ---- threading helper --------------------------------------------------


def call_async(
    func: Callable[[], Any],
    on_result: Callable[[Any, Optional[Exception]], None],
) -> None:
    """Run `func` on a worker thread; deliver `(result, error)` to the Kivy
    main thread via Clock.schedule_once.

    Exactly one of result/error will be non-None.
    """

    def worker() -> None:
        try:
            result = func()
            err: Optional[Exception] = None
        except Exception as e:  # noqa: BLE001 - we deliberately catch all
            result = None
            err = e

        def deliver(_dt: float) -> None:
            on_result(result, err)

        Clock.schedule_once(deliver, 0)

    threading.Thread(target=worker, daemon=True).start()
