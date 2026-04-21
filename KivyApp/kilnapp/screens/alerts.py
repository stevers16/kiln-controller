"""Alerts screen.

Phase 5: scrollable, chronological list of WARN/ERROR events from the
Pico's SD event log via GET /alerts. Filter bar across the top selects
All / Errors / Warnings, and a Run dropdown narrows to a specific run.

Empty state: "No alerts recorded".

Note on classification vs. severity:
- /alerts returns the SD log severity ("WARN" or "ERROR") - that's a
  *logging* level, not the operator-facing tier from kilnapp/alerts.py.
- This screen displays both: the WARN/ERROR badge from the log, plus
  (when a code is recognised) the FAULT/NOTICE/INFO tier from
  kilnapp.alerts.classify(). Future spec work in error_checking_spec.md
  will collapse these once the firmware tags severity at source.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from kivy.clock import Clock
from kivy.graphics import Color, Rectangle
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.screenmanager import Screen
from kivy.uix.togglebutton import ToggleButton

from kilnapp import theme
from kilnapp.alerts import (
    TIER_FAULT,
    TIER_NOTICE,
    classify,
    humanise,
)
from kilnapp.api.autodetect import DetectResult, MODE_OFFLINE
from kilnapp.api.client import call_async
from kilnapp.connection import ConnectionManager
from kilnapp.widgets.cards import Panel, small_label, value_label
from kilnapp.widgets.form import spinner


# Filter values
FILTER_ALL = "all"
FILTER_ERRORS = "errors"
FILTER_WARNINGS = "warnings"
_FILTER_LEVEL_PARAM = {
    FILTER_ALL: None,
    FILTER_ERRORS: "ERROR",
    FILTER_WARNINGS: "WARN",
}

REFRESH_INTERVAL_S = 30
ALERT_LIMIT = 200


def _fmt_ts(ts) -> str:
    """Format unix timestamp as 'YYYY-MM-DD HH:MM:SS'. Returns '--' on
    failure or when ts == 0 (RTC was not set when the event was logged)."""
    if not ts:
        return "--"
    try:
        import datetime

        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


class _SeverityBadge(BoxLayout):
    """Coloured pill showing the log level."""

    def __init__(self, level: str, **kwargs):
        super().__init__(orientation="vertical", **kwargs)
        self.size_hint = (None, None)
        # WARN -> amber, ERROR -> red, anything else -> grey
        l = (level or "").upper()
        if l.startswith("ERR"):
            bg = theme.SEVERITY_ERROR
            text = "ERROR"
        elif l.startswith("WARN"):
            bg = theme.SEVERITY_WARN
            text = "WARN"
        else:
            bg = theme.TEXT_MUTED
            text = l or "INFO"
        self.size = (62, 18)
        with self.canvas.before:
            self._bg = Color(*bg)
            self._rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(
            pos=lambda w, v: setattr(self._rect, "pos", v),
            size=lambda w, v: setattr(self._rect, "size", v),
        )
        lbl = Label(
            text=text,
            color=(1, 1, 1, 1),
            font_size="10sp",
            bold=True,
            halign="center",
            valign="middle",
        )
        lbl.bind(size=lambda w, s: setattr(w, "text_size", s))
        self.add_widget(lbl)


class _AlertRow(Panel):
    """One row in the alerts list."""

    def __init__(self, alert: Dict[str, Any], **kwargs):
        super().__init__(**kwargs)
        self.padding = (10, 6, 10, 6)
        self.spacing = 2

        # Header line: timestamp + severity badge + tier badge (if any)
        header = BoxLayout(orientation="horizontal", size_hint_y=None, height=20, spacing=6)
        ts_label = small_label(_fmt_ts(alert.get("ts")), size="11sp")
        ts_label.size_hint_x = 1
        header.add_widget(ts_label)
        header.add_widget(_SeverityBadge(alert.get("level", "")))
        # Tier badge: prefer server-provided tier, fall back to client-side
        code = alert.get("code") or ""
        server_tier = alert.get("tier")
        tier = classify(code, server_tier=server_tier) if code else None
        if tier in (TIER_FAULT, TIER_NOTICE):
            header.add_widget(_TierBadge(tier))
        self.add_widget(header)

        # Code line (only if a code was extracted)
        if code:
            code_label = value_label(humanise(code), size="13sp")
            code_label.color = theme.TEXT_PRIMARY
            self.add_widget(code_label)

        # Source + message
        source = alert.get("source") or ""
        message = alert.get("message") or ""
        # Strip the "CODE:" prefix from the message if it duplicates the code
        if code and message.startswith(code + ":"):
            message = message[len(code) + 1 :].strip()
        msg_text = f"[{source}] {message}" if source else message
        msg_label = small_label(msg_text, size="11sp")
        msg_label.color = theme.TEXT_SECONDARY
        # Allow multi-line wrap
        msg_label.text_size = (None, None)
        msg_label.size_hint_y = None
        msg_label.height = 32
        msg_label.halign = "left"
        msg_label.valign = "top"

        def _wrap(_w, w_size):
            msg_label.text_size = (w_size[0] - 4, None)
            msg_label.texture_update()
            msg_label.height = max(16, msg_label.texture_size[1] + 2)

        self.bind(width=lambda w, v: _wrap(w, (v, 0)))
        self.add_widget(msg_label)


class _TierBadge(BoxLayout):
    """Small badge showing FAULT/NOTICE classification next to the log level."""

    def __init__(self, tier: str, **kwargs):
        super().__init__(orientation="vertical", **kwargs)
        self.size_hint = (None, None)
        self.size = (60, 18)
        bg = theme.SEVERITY_ERROR if tier == TIER_FAULT else (0.78, 0.55, 0.10, 1)
        text = tier.upper()
        with self.canvas.before:
            self._bg = Color(*bg)
            self._rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(
            pos=lambda w, v: setattr(self._rect, "pos", v),
            size=lambda w, v: setattr(self._rect, "size", v),
        )
        lbl = Label(
            text=text,
            color=(1, 1, 1, 1),
            font_size="10sp",
            bold=True,
            halign="center",
            valign="middle",
        )
        lbl.bind(size=lambda w, s: setattr(w, "text_size", s))
        self.add_widget(lbl)


class AlertsScreen(Screen):
    def __init__(self, connection: ConnectionManager, **kwargs):
        super().__init__(name="alerts", **kwargs)
        self.connection = connection
        self._refresh_event = None
        self._in_flight = False
        self._current_filter = FILTER_ALL
        self._current_run: Optional[str] = None  # None = current run
        self._pending_preselect: Optional[str] = None
        self._known_runs: List[Dict[str, Any]] = []
        self._current_mode = MODE_OFFLINE

        # Background
        with self.canvas.before:
            self._bg_color = Color(*theme.BG_DARK)
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(
            pos=lambda w, v: setattr(self._bg_rect, "pos", v),
            size=lambda w, v: setattr(self._bg_rect, "size", v),
        )

        root = BoxLayout(
            orientation="vertical",
            padding=(10, 8, 10, 8),
            spacing=6,
        )

        # Filter bar: All / Errors / Warnings + Run dropdown
        filter_row = BoxLayout(
            orientation="horizontal",
            size_hint_y=None,
            height=36,
            spacing=4,
        )
        self._filter_buttons: Dict[str, ToggleButton] = {}
        for value, label in (
            (FILTER_ALL, "All"),
            (FILTER_ERRORS, "Errors"),
            (FILTER_WARNINGS, "Warnings"),
        ):
            btn = ToggleButton(
                text=label,
                group="alerts_filter",
                state="down" if value == FILTER_ALL else "normal",
                allow_no_selection=False,
                font_size="12sp",
                background_color=(0.30, 0.32, 0.38, 1),
                color=(1, 1, 1, 1),
            )
            btn.bind(on_release=self._make_filter_handler(value))
            self._filter_buttons[value] = btn
            filter_row.add_widget(btn)
        root.add_widget(filter_row)

        # Run dropdown row
        run_row = BoxLayout(
            orientation="horizontal",
            size_hint_y=None,
            height=36,
            spacing=4,
        )
        run_label = small_label("Run:", size="12sp")
        run_label.size_hint_x = None
        run_label.width = 40
        run_label.height = 36
        run_row.add_widget(run_label)
        self.run_spinner = spinner(values=["Current run"], initial="Current run")
        self.run_spinner.bind(text=self._on_run_changed)
        run_row.add_widget(self.run_spinner)
        refresh_btn = Button(
            text="Refresh",
            size_hint_x=None,
            width=80,
            font_size="12sp",
            background_color=(0.30, 0.55, 0.85, 1),
            color=(1, 1, 1, 1),
        )
        refresh_btn.bind(on_release=lambda _b: self.refresh_now())
        run_row.add_widget(refresh_btn)
        root.add_widget(run_row)

        # Status / count line
        self.status_label = small_label("", size="11sp")
        self.status_label.height = 16
        root.add_widget(self.status_label)

        # Scrollable alerts list
        self.scroll = ScrollView(do_scroll_x=False, do_scroll_y=True)
        self.list_box = BoxLayout(
            orientation="vertical",
            spacing=6,
            size_hint_y=None,
            padding=(0, 0, 0, 8),
        )
        self.list_box.bind(minimum_height=self.list_box.setter("height"))
        self.scroll.add_widget(self.list_box)
        root.add_widget(self.scroll)

        self.add_widget(root)

        # Subscribe to connection state and tick periodically
        self.connection.add_listener(self._on_connection_change)

    def preselect_run(self, run_id: Optional[str]) -> None:
        """Called by the app router when navigating here from the Runs
        detail view. Applied on the next `on_enter`. Pass None to mean
        'current run'."""
        self._pending_preselect = run_id or ""  # empty string distinguishes
                                                 # 'asked for current run' vs
                                                 # 'no preselect requested'.

    def on_enter(self, *args):
        """Refresh immediately when the screen becomes visible."""
        if self._pending_preselect is not None:
            requested = self._pending_preselect
            self._pending_preselect = None
            self._apply_preselect(requested)
        else:
            self.refresh_now()

    def _apply_preselect(self, run_id: str) -> None:
        """Apply a requested run preselection. If the runs list has been
        populated we can sync the spinner to the matching label; otherwise
        we store the id, refresh the runs list, and sync after it lands."""
        self._current_run = run_id or None

        def sync_spinner():
            if not run_id:
                self.run_spinner.text = "Current run"
                return
            for r in self._known_runs:
                if r.get("id") == run_id:
                    label = self._format_run_label(r)
                    if label in self.run_spinner.values:
                        self.run_spinner.text = label
                        return
            # Unknown id - fall back to showing "Current run" in the
            # spinner but still filter the alerts fetch to the requested
            # id (server will return an empty list if it doesn't exist).
            self.run_spinner.text = "Current run"

        # If we already have a runs list, sync immediately.
        if self._known_runs:
            sync_spinner()
            self.refresh_now()
            return

        # Otherwise fetch runs first, then sync + refresh.
        if self._current_mode == MODE_OFFLINE:
            self.refresh_now()
            return
        client = self.connection.client
        if client.config.base_url is None:
            self.refresh_now()
            return

        def work():
            return client.runs()

        def done(result, err):
            if err is None and isinstance(result, dict):
                self._known_runs = result.get("runs") or []
                values = ["Current run"] + [
                    self._format_run_label(r) for r in self._known_runs
                ]
                self.run_spinner.values = values
            sync_spinner()
            self.refresh_now()

        call_async(work, done)

    # ---- connection wiring -------------------------------------------------

    def _on_connection_change(self, result: DetectResult) -> None:
        self._current_mode = result.mode
        if self._refresh_event is not None:
            self._refresh_event.cancel()
            self._refresh_event = None
        if result.mode == MODE_OFFLINE:
            self.status_label.text = "Offline - waiting for connection."
            return
        self._refresh_event = Clock.schedule_interval(
            lambda _dt: self.refresh_now(), REFRESH_INTERVAL_S
        )
        # Pull runs list (so the dropdown is populated) and an immediate refresh
        self._refresh_runs()
        self.refresh_now()

    # ---- filter handling ---------------------------------------------------

    def _make_filter_handler(self, value: str):
        def handler(_btn):
            self._current_filter = value
            self.refresh_now()

        return handler

    def _on_run_changed(self, _spinner, text: str) -> None:
        if text == "Current run":
            self._current_run = None
        else:
            # Find the run id matching this label
            for run in self._known_runs:
                if self._format_run_label(run) == text:
                    self._current_run = run.get("id")
                    break
        self.refresh_now()

    @staticmethod
    def _format_run_label(run: Dict[str, Any]) -> str:
        """Friendly label for a run. Prefers the formatted ended
        timestamp (from file mtime), falls back to the start date parsed
        from the rid, then to the raw rid.

        Every branch forces str(): Pi4 /runs returns integer primary
        keys, Pico returns strings - both need to survive Spinner.values.
        """
        ended = run.get("ended_at_str")
        if ended:
            return str(ended)
        started = run.get("started_at_str")
        if started and "-" in str(started):
            return str(started)
        rid = run.get("id")
        return str(rid) if rid is not None else "?"

    # ---- data fetching -----------------------------------------------------

    def _refresh_runs(self) -> None:
        if self._current_mode == MODE_OFFLINE:
            return
        if self.connection.client.config.base_url is None:
            return
        client = self.connection.client

        def work():
            return client.runs()

        def done(result, err):
            if err is not None or not isinstance(result, dict):
                return
            runs = result.get("runs") or []
            self._known_runs = runs
            values = ["Current run"] + [self._format_run_label(r) for r in runs]
            current_text = self.run_spinner.text
            self.run_spinner.values = values
            if current_text not in values:
                self.run_spinner.text = "Current run"

        call_async(work, done)

    def refresh_now(self) -> None:
        if self._in_flight:
            return
        if self._current_mode == MODE_OFFLINE:
            self.status_label.text = "Offline - waiting for connection."
            return
        if self.connection.client.config.base_url is None:
            return
        self._in_flight = True
        self.status_label.text = "Loading..."
        client = self.connection.client
        level = _FILTER_LEVEL_PARAM[self._current_filter]
        run = self._current_run

        def work():
            return client.alerts(limit=ALERT_LIMIT, level=level, run=run)

        def done(result, err):
            self._in_flight = False
            if err is not None:
                self.status_label.text = f"Load failed: {err}"
                self._render([])
                return
            alerts = (result or {}).get("alerts") or []
            count = (result or {}).get("count", len(alerts))
            run_label = "current run" if run is None else run
            filter_label = {
                FILTER_ALL: "All",
                FILTER_ERRORS: "Errors only",
                FILTER_WARNINGS: "Warnings only",
            }[self._current_filter]
            self.status_label.text = f"{count} alerts | {filter_label} | {run_label}"
            self._render(alerts)

        call_async(work, done)

    # ---- rendering ---------------------------------------------------------

    def _render(self, alerts: List[Dict[str, Any]]) -> None:
        self.list_box.clear_widgets()
        if not alerts:
            # Build a fresh empty-state widget every render. Reusing a
            # cached Label across re-renders causes 'already has a parent'
            # exceptions because clear_widgets() removes it from list_box
            # but the previous BoxLayout wrapper still owns it.
            empty_box = BoxLayout(
                orientation="vertical",
                size_hint_y=None,
                height=80,
            )
            empty_label = Label(
                text="No alerts recorded",
                color=theme.TEXT_MUTED,
                font_size="14sp",
                halign="center",
                valign="middle",
            )
            empty_label.bind(size=lambda w, s: setattr(w, "text_size", s))
            empty_box.add_widget(empty_label)
            self.list_box.add_widget(empty_box)
            return
        for alert in alerts:
            try:
                self.list_box.add_widget(_AlertRow(alert))
            except Exception as e:
                print(f"[alerts] failed to render alert {alert!r}: {e}")
