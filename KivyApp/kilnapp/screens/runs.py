"""Runs screen.

Phase 6: list of drying runs from the SD card + detail view. Data source
is GET /runs (file metadata from SD) plus GET /status (for the active run's
richer fields like schedule_name, stage_index, MC%).

The spec calls for schedule name, species/thickness, start/end, total
duration, stages completed, and final MC% in the detail view. The Pico's
/runs endpoint only returns file metadata (id, event_count, data_rows,
size_bytes), so the detail view for **historical** runs is limited to what
the files can tell us. The **active** run gets richer detail from /status.

"View History" and "View Alerts" buttons in the detail view navigate to
the History (placeholder until Phase 7) and Alerts tabs with the run pre-
selected.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from kivy.clock import Clock
from kivy.graphics import Color, Rectangle
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.screenmanager import Screen

from kilnapp import theme
from kilnapp.api.autodetect import DetectResult, MODE_OFFLINE
from kilnapp.api.client import call_async
from kilnapp.connection import ConnectionManager
from kilnapp.format import format_run_label, format_size
from kilnapp.widgets.cards import Panel, small_label, value_label
from kilnapp.widgets.dialog import confirm


REFRESH_INTERVAL_S = 30


# ---- Status badge ----------------------------------------------------------


class _StatusBadge(BoxLayout):
    """Coloured pill: ACTIVE (green) / COMPLETED (grey)."""

    def __init__(self, is_active: bool, **kwargs):
        super().__init__(orientation="vertical", **kwargs)
        self.size_hint = (None, None)
        self.size = (dp(80), dp(20))
        bg = theme.SEVERITY_OK if is_active else theme.TEXT_MUTED
        text = "ACTIVE" if is_active else "COMPLETED"
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


# ---- Run list row ----------------------------------------------------------


class _RunRow(Panel):
    """One row in the runs list. Tapping opens the detail view."""

    def __init__(
        self,
        run: Dict[str, Any],
        is_active: bool,
        on_tap=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.run = run
        self._on_tap = on_tap
        self.padding = (dp(10), dp(6), dp(10), dp(6))
        self.spacing = dp(2)

        # Header: primary label (ended or started date) + status badge
        header = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=dp(22), spacing=dp(6)
        )
        ts_lbl = value_label(format_run_label(run), size="14sp")
        ts_lbl.size_hint_x = 1
        header.add_widget(ts_lbl)
        header.add_widget(_StatusBadge(is_active))
        self.add_widget(header)

        # Secondary line: show the raw rid and (when meaningful) the
        # started date, so the user still has the canonical handle.
        rid = run.get("id") or "?"
        started = run.get("started_at_str")
        if run.get("ended_at_str") and started and "-" in started:
            self.add_widget(
                small_label(f"Started {started} - id {rid}", size="11sp")
            )
        else:
            self.add_widget(small_label(f"id {rid}", size="11sp"))

        # Detail line: data rows, event count, size
        data_rows = run.get("data_rows", 0)
        event_count = run.get("event_count", 0)
        size = run.get("size_bytes", 0)
        detail = f"{data_rows} data rows, {event_count} events, {format_size(size)}"
        self.add_widget(small_label(detail, size="11sp"))

        # Make entire panel tappable
        self.bind(on_touch_down=self._handle_touch)

    def _handle_touch(self, _widget, touch):
        if self.collide_point(*touch.pos) and self._on_tap:
            self._on_tap(self.run)
            return True
        return False


# ---- Run detail view -------------------------------------------------------


class _RunDetail(BoxLayout):
    """Detail view for a single run. Back button at top, info panels, action
    buttons at bottom."""

    def __init__(
        self,
        run: Dict[str, Any],
        is_active: bool,
        active_status: Optional[Dict[str, Any]],
        on_back=None,
        on_view_alerts=None,
        on_view_history=None,
        on_delete=None,
        **kwargs,
    ):
        super().__init__(orientation="vertical", **kwargs)
        self.padding = (dp(10), dp(8), dp(10), dp(8))
        self.spacing = dp(6)

        # Back button
        back_row = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=dp(36), spacing=dp(8)
        )
        back_btn = Button(
            text="< Back to Runs",
            size_hint_x=None,
            width=dp(140),
            font_size="13sp",
            background_color=(0.30, 0.32, 0.38, 1),
            color=(1, 1, 1, 1),
        )
        back_btn.bind(on_release=lambda _b: on_back() if on_back else None)
        back_row.add_widget(back_btn)
        back_row.add_widget(BoxLayout())  # spacer
        self.add_widget(back_row)

        # Scroll area for detail panels
        scroll = ScrollView(do_scroll_x=False, do_scroll_y=True)
        content = BoxLayout(
            orientation="vertical",
            spacing=dp(6),
            size_hint_y=None,
            padding=(0, 0, 0, dp(8)),
        )
        content.bind(minimum_height=content.setter("height"))

        # Run info panel
        info = Panel()
        info.add_widget(value_label(format_run_label(run), size="16sp"))

        rid = run.get("id") or "?"
        info.add_widget(small_label(f"id: {rid}"))

        started = run.get("started_at_str")
        if started and "-" in started:
            info.add_widget(small_label(f"Started: {started}"))

        ended = run.get("ended_at_str")
        if ended:
            # During an active run mtime reflects the last write, not an
            # actual end; call it 'Last update' to avoid confusion.
            label = "Last update" if is_active else "Ended"
            info.add_widget(small_label(f"{label}: {ended}"))

        status_text = "Active" if is_active else "Completed"
        info.add_widget(small_label(f"Status: {status_text}"))

        # Active run gets rich detail from /status
        if is_active and active_status:
            s = active_status
            sched = s.get("schedule_name") or "--"
            info.add_widget(small_label(f"Schedule: {sched}"))
            stage_idx = s.get("stage_index")
            stage_name = s.get("stage_name") or ""
            stage_type = (s.get("stage_type") or "").upper()
            if stage_idx is not None:
                info.add_widget(
                    small_label(
                        f"Stage {stage_idx}: {stage_name} [{stage_type}]"
                    )
                )
            total_h = s.get("total_elapsed_h")
            if total_h is not None:
                info.add_widget(small_label(f"Total elapsed: {total_h:.1f} h"))
            mc1 = s.get("mc_channel_1")
            mc2 = s.get("mc_channel_2")
            if mc1 is not None or mc2 is not None:
                mc1_s = f"{mc1:.1f}%" if mc1 is not None else "--"
                mc2_s = f"{mc2:.1f}%" if mc2 is not None else "--"
                info.add_widget(
                    small_label(f"MC: Ch1 {mc1_s}, Ch2 {mc2_s}")
                )

        content.add_widget(info)

        # File info panel
        files = Panel()
        files.add_widget(small_label("Log files", bold=True))
        event_log = run.get("event_log") or "--"
        data_csv = run.get("data_csv") or "--"
        files.add_widget(small_label(f"Event log: {event_log}"))
        files.add_widget(
            small_label(f"  {run.get('event_count', 0)} entries")
        )
        files.add_widget(small_label(f"Data CSV: {data_csv}"))
        files.add_widget(
            small_label(f"  {run.get('data_rows', 0)} rows")
        )
        files.add_widget(
            small_label(f"Total size: {format_size(run.get('size_bytes', 0))}")
        )
        content.add_widget(files)

        scroll.add_widget(content)
        self.add_widget(scroll)

        # Action buttons
        btn_row = BoxLayout(
            orientation="horizontal",
            size_hint_y=None,
            height=dp(40),
            spacing=dp(6),
        )
        if on_view_alerts:
            alerts_btn = Button(
                text="View Alerts",
                font_size="13sp",
                background_color=(0.30, 0.55, 0.85, 1),
                color=(1, 1, 1, 1),
            )
            alerts_btn.bind(
                on_release=lambda _b: on_view_alerts(run.get("id"))
            )
            btn_row.add_widget(alerts_btn)

        # History button - navigates to the History tab with this run
        # preselected in the run dropdown.
        history_btn = Button(
            text="View History",
            font_size="13sp",
            background_color=(0.30, 0.55, 0.85, 1),
            color=(1, 1, 1, 1),
        )
        if on_view_history:
            history_btn.bind(
                on_release=lambda _b: on_view_history(run.get("id"))
            )
        else:
            history_btn.disabled = True
            history_btn.opacity = 0.5
        btn_row.add_widget(history_btn)
        self.add_widget(btn_row)

        # Delete button - separate row so it's visually distinct. Disabled
        # for the active run (firmware returns 409 anyway but we gate the
        # UI too so the user doesn't get a scary-looking error).
        if on_delete:
            delete_row = BoxLayout(
                orientation="horizontal",
                size_hint_y=None,
                height=dp(40),
                spacing=dp(6),
            )
            delete_btn = Button(
                text="Delete Run",
                font_size="13sp",
                background_color=(0.85, 0.30, 0.30, 1),
                color=(1, 1, 1, 1),
            )
            if is_active:
                delete_btn.disabled = True
                delete_btn.opacity = 0.5
            delete_btn.bind(on_release=lambda _b: on_delete(run))
            delete_row.add_widget(delete_btn)
            self.add_widget(delete_row)


# ---- Runs screen -----------------------------------------------------------


class RunsScreen(Screen):
    def __init__(
        self,
        connection: ConnectionManager,
        on_navigate=None,
        **kwargs,
    ):
        super().__init__(name="runs", **kwargs)
        self.connection = connection
        self._on_navigate = on_navigate
        self._refresh_event = None
        self._in_flight = False
        self._current_mode = MODE_OFFLINE
        self._runs: List[Dict[str, Any]] = []
        self._active_status: Optional[Dict[str, Any]] = None

        # Background
        with self.canvas.before:
            self._bg_color = Color(*theme.BG_DARK)
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(
            pos=lambda w, v: setattr(self._bg_rect, "pos", v),
            size=lambda w, v: setattr(self._bg_rect, "size", v),
        )

        self._root = BoxLayout(
            orientation="vertical",
            padding=(dp(10), dp(8), dp(10), dp(8)),
            spacing=dp(6),
        )

        # Header row with title + refresh
        header = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=dp(36), spacing=dp(6)
        )
        title = value_label("Drying Runs", size="16sp")
        title.size_hint_x = 1
        header.add_widget(title)
        refresh_btn = Button(
            text="Refresh",
            size_hint_x=None,
            width=dp(84),
            font_size="12sp",
            background_color=(0.30, 0.55, 0.85, 1),
            color=(1, 1, 1, 1),
        )
        refresh_btn.bind(on_release=lambda _b: self.refresh_now())
        header.add_widget(refresh_btn)
        self._root.add_widget(header)

        # Status line - wraps and auto-sizes so long error messages
        # (e.g. network timeouts) stay legible instead of getting clipped.
        self.status_label = Label(
            text="",
            color=theme.TEXT_SECONDARY,
            font_size="11sp",
            size_hint_y=None,
            height=dp(18),
            halign="left",
            valign="top",
        )

        def _wrap_status(_w, w):
            self.status_label.text_size = (max(0, w - 4), None)
            self.status_label.texture_update()
            self.status_label.height = max(16, self.status_label.texture_size[1] + 2)

        self._root.bind(width=_wrap_status)
        self.status_label.bind(texture_size=lambda w, s: setattr(w, "height", max(16, s[1] + 2)))
        self._root.add_widget(self.status_label)

        # Scrollable list area
        self.scroll = ScrollView(do_scroll_x=False, do_scroll_y=True)
        self.list_box = BoxLayout(
            orientation="vertical",
            spacing=dp(6),
            size_hint_y=None,
            padding=(0, 0, 0, dp(8)),
        )
        self.list_box.bind(minimum_height=self.list_box.setter("height"))
        self.scroll.add_widget(self.list_box)
        self._root.add_widget(self.scroll)

        self.add_widget(self._root)

        # Detail view (replaces the list when a run is tapped)
        self._detail_widget = None

        # Subscribe to connection state
        self.connection.add_listener(self._on_connection_change)

    def on_enter(self, *args):
        self.refresh_now()

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
        self.refresh_now()

    # ---- data fetching -----------------------------------------------------

    def refresh_now(self) -> None:
        if self._in_flight:
            return
        if self._current_mode == MODE_OFFLINE:
            self.status_label.text = "Offline - waiting for connection."
            return
        if self.connection.client.config.base_url is None:
            return
        self._in_flight = True
        self.status_label.text = "Loading runs..."
        client = self.connection.client

        # Fetch /runs and /status independently so a slow call on one
        # doesn't time out the other.
        self._pending_calls = 2
        self._pending_err: Optional[Exception] = None

        def done_runs(result, err):
            if err is not None:
                self._pending_err = err
            else:
                self._runs = (result or {}).get("runs") or []
            self._pending_calls -= 1
            if self._pending_calls == 0:
                self._finish_refresh()

        def done_status(result, err):
            # /status failure is not fatal - we just won't have active-run
            # detail. Log it but don't blow up the whole refresh.
            if err is None and isinstance(result, dict):
                self._active_status = result
            self._pending_calls -= 1
            if self._pending_calls == 0:
                self._finish_refresh()

        call_async(lambda: client.runs(), done_runs)
        call_async(lambda: client.status(), done_status)

    def _finish_refresh(self) -> None:
        self._in_flight = False
        if self._pending_err is not None:
            # Strip long connection-pool / URL noise from requests errors
            msg = str(self._pending_err)
            if len(msg) > 120:
                msg = msg[:117] + "..."
            self.status_label.text = f"Load failed: {msg}"
            return
        count = len(self._runs)
        self.status_label.text = f"{count} run{'s' if count != 1 else ''}"
        self._render_list()

    # ---- list rendering ----------------------------------------------------

    def _render_list(self) -> None:
        self.list_box.clear_widgets()

        # Remove detail view if showing
        if self._detail_widget is not None:
            self._show_list_view()

        if not self._runs:
            empty_box = BoxLayout(
                orientation="vertical", size_hint_y=None, height=dp(96)
            )
            lbl = Label(
                text="No runs recorded",
                color=theme.TEXT_MUTED,
                font_size="14sp",
                halign="center",
                valign="middle",
            )
            lbl.bind(size=lambda w, s: setattr(w, "text_size", s))
            empty_box.add_widget(lbl)
            self.list_box.add_widget(empty_box)
            return

        # Determine which run (if any) is active. Prefer the explicit
        # `active_run_id` the firmware reports in /status; fall back to
        # "most recent by mtime" only on older firmware that doesn't
        # expose the field.
        active_run_id = None
        if self._active_status and self._active_status.get("run_active"):
            active_run_id = self._active_status.get("active_run_id")
            if not active_run_id and self._runs:
                active_run_id = self._runs[0].get("id")

        # Pull the active run to the top of the list regardless of
        # server sort order. Necessary because when the Pico RTC is
        # unset at run-start the file mtime is tiny (near epoch-2000),
        # pushing the live run to the bottom of a mtime-desc sort.
        runs = list(self._runs)
        if active_run_id:
            for i, r in enumerate(runs):
                if r.get("id") == active_run_id and i != 0:
                    runs.insert(0, runs.pop(i))
                    break

        for run in runs:
            is_active = run.get("id") == active_run_id
            row = _RunRow(
                run, is_active=is_active, on_tap=self._on_run_tapped
            )
            self.list_box.add_widget(row)

    # ---- detail view -------------------------------------------------------

    def _on_run_tapped(self, run: Dict[str, Any]) -> None:
        # Is this the active run? Trust the firmware's `active_run_id`
        # from /status when present; fall back to "first by mtime" on
        # older firmware.
        is_active = False
        if self._active_status and self._active_status.get("run_active"):
            active_id = self._active_status.get("active_run_id")
            if active_id:
                is_active = run.get("id") == active_id
            elif self._runs and self._runs[0].get("id") == run.get("id"):
                is_active = True

        self._show_detail_view(run, is_active)

    def _show_detail_view(
        self, run: Dict[str, Any], is_active: bool
    ) -> None:
        """Replace the list with a detail view for the tapped run."""
        # Hide the list content
        self._root.remove_widget(self.scroll)
        if self.status_label.parent:
            self._root.remove_widget(self.status_label)

        self._detail_widget = _RunDetail(
            run=run,
            is_active=is_active,
            active_status=self._active_status if is_active else None,
            on_back=self._show_list_view,
            on_view_alerts=self._on_view_alerts,
            on_view_history=self._on_view_history,
            on_delete=self._on_delete_pressed,
        )
        self._root.add_widget(self._detail_widget)

    def _show_list_view(self) -> None:
        """Switch back from detail to list view."""
        if self._detail_widget is not None:
            self._root.remove_widget(self._detail_widget)
            self._detail_widget = None
        # Re-add the list components if they were removed
        if self.status_label.parent is None:
            # Insert after header (index 0 in reversed children)
            self._root.add_widget(self.status_label, index=1)
        if self.scroll.parent is None:
            self._root.add_widget(self.scroll, index=0)

    def _on_view_alerts(self, run_id: Optional[str]) -> None:
        """Navigate to the Alerts tab with this run pre-selected."""
        if self._on_navigate:
            self._on_navigate("alerts", run_id=run_id)

    def _on_view_history(self, run_id: Optional[str]) -> None:
        """Navigate to the History tab with this run pre-selected."""
        if self._on_navigate:
            self._on_navigate("history", run_id=run_id)

    def _on_delete_pressed(self, run: Dict[str, Any]) -> None:
        run_id = run.get("id") or ""
        started = run.get("started_at_str") or run_id
        confirm(
            "Delete Run",
            f"Delete both the event log and data CSV for run '{started}'? "
            "This cannot be undone.",
            on_confirm=lambda: self._do_delete(run_id),
            confirm_text="Delete",
            danger=True,
        )

    def _do_delete(self, run_id: str) -> None:
        if not run_id:
            return
        if self._current_mode == MODE_OFFLINE:
            self.status_label.text = "Offline - cannot delete."
            return
        client = self.connection.client
        self.status_label.text = f"Deleting {run_id}..."

        def work():
            return client.run_delete(run_id)

        def done(result, err):
            if err is not None:
                self.status_label.text = f"Delete failed: {err}"
                return
            self.status_label.text = f"Deleted {run_id}."
            # Return to list view and refresh
            self._show_list_view()
            self.refresh_now()

        call_async(work, done)
