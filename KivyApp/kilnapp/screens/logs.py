"""Logs screen (AP/STA only).

Phase 11: browse log file sets stored on the Pico SD card, open the event
log in an in-app viewer (level filter + substring search), and download
the event log or the data CSV to the user's Downloads folder.

Delete was handled in Phase 6 on the Runs screen, so this screen does
not duplicate it. Entry point is the "Tools (Direct only)" section on
Settings, matching the Schedules and System Test pattern.

Data sources
------------
- GET /sdcard/info   -> storage indicator
- GET /runs          -> one row per run on the SD card (event + data files)
- GET /logs/{rid}/events   -> event log text (in-app viewer + .txt download)
- GET /history?run={rid}   -> columnar data; reconstructed to CSV client-side

Download behaviour on desktop writes to `~/Downloads` (with a fallback to
the app's `user_data_dir` when Downloads is not writable). Android will
hook this into the SAF in Phase 15 (noted as deferred in the plan).
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from kivy.clock import Clock
from kivy.graphics import Color, Rectangle
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.progressbar import ProgressBar
from kivy.uix.scrollview import ScrollView
from kivy.uix.screenmanager import Screen

from kilnapp import theme
from kilnapp.api.autodetect import DetectResult, MODE_OFFLINE, is_direct_mode
from kilnapp.api.client import call_async
from kilnapp.connection import ConnectionManager
from kilnapp.platform_helpers import download_dir
from kilnapp.widgets.cards import Panel, small_label, value_label
from kilnapp.format import format_size
from kilnapp.widgets.form import spinner, text_input


_LEVEL_ALL = "ALL"
_LEVEL_OPTIONS = [_LEVEL_ALL, "INFO", "WARN", "ERROR"]

# Any SD >= 80% full shows a warning tint on the storage bar.
_STORAGE_WARN_FRAC = 0.80


def _line_level(line: str) -> str:
    """Extract a level label from an event log line.

    Logger format: ``2026-03-17 14:30:05 [INFO ] [exhaust    ] Fan on at 75%``
    The level token is the first bracketed group. Anything unrecognised
    is reported as 'OTHER' so the ALL filter still includes it.
    """
    lb = line.find("[")
    if lb == -1:
        return "OTHER"
    rb = line.find("]", lb + 1)
    if rb == -1:
        return "OTHER"
    token = line[lb + 1:rb].strip().upper()
    if token in ("INFO", "WARN", "ERROR"):
        return token
    # Older firmware emitted 'WARNING'; treat it as WARN so the filter
    # still catches it.
    if token == "WARNING":
        return "WARN"
    return "OTHER"


def _rows_to_csv(fields: List[str], rows: List[List[Any]]) -> str:
    """Reconstruct a CSV from /history's columnar response.

    Matches the Pico's on-SD format: simple comma-join with newline row
    separators and empty strings for None. Rows are already a list of
    parsed values from the Pico's handler.
    """
    lines = [",".join(fields)]
    for r in rows:
        cells = []
        for v in r:
            if v is None:
                cells.append("")
            elif isinstance(v, float):
                # 2 dp matches logger.data() on the firmware side so the
                # exported file reads the same as the original on-SD CSV.
                cells.append(f"{v:.2f}")
            else:
                cells.append(str(v))
        lines.append(",".join(cells))
    return "\n".join(lines) + "\n"


# ---- Storage indicator ----------------------------------------------------


class _StorageBar(Panel):
    """SD card storage indicator at the top of the logs list."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.padding = (10, 6, 10, 6)
        self.title = value_label("SD storage", size="13sp")
        self.add_widget(self.title)
        self.summary = small_label("-", size="11sp")
        self.add_widget(self.summary)
        self.bar = ProgressBar(max=1.0, value=0.0, size_hint_y=None, height=8)
        self.add_widget(self.bar)
        self.warn = small_label("", size="11sp")
        self.warn.color = theme.SEVERITY_WARN
        self.warn.height = 0
        self.add_widget(self.warn)

    def set_unknown(self, note: str) -> None:
        self.summary.text = note
        self.bar.value = 0.0
        self.warn.text = ""
        self.warn.height = 0

    def set_info(self, info: Dict[str, Any]) -> None:
        if not info.get("mounted"):
            self.set_unknown("SD card not mounted")
            return
        total = int(info.get("total_bytes", 0) or 0)
        used = int(info.get("used_bytes", 0) or 0)
        free = int(info.get("free_bytes", 0) or 0)
        files = int(info.get("file_count", 0) or 0)
        if total <= 0:
            self.set_unknown("SD card mounted (size unknown)")
            return
        frac = min(1.0, used / total)
        self.bar.value = frac
        self.summary.text = (
            f"{format_size(used)} used / {format_size(total)} total  "
            f"({format_size(free)} free, {files} files)"
        )
        if frac >= _STORAGE_WARN_FRAC:
            self.warn.text = f"Warning: SD card over {int(_STORAGE_WARN_FRAC * 100)}% full"
            self.warn.height = 16
        else:
            self.warn.text = ""
            self.warn.height = 0


# ---- Log set row ----------------------------------------------------------


def _row_button(text: str, *, width: int = 68) -> Button:
    return Button(
        text=text,
        size_hint_x=None,
        width=width,
        size_hint_y=None,
        height=30,
        font_size="11sp",
        background_color=(0.30, 0.55, 0.85, 1),
        color=(1, 1, 1, 1),
    )


class _LogSetRow(Panel):
    """One row per run: event log + data CSV badges + action buttons."""

    def __init__(
        self,
        run: Dict[str, Any],
        *,
        is_active: bool,
        on_view,
        on_download_events,
        on_download_csv,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.run = run
        self.padding = (10, 6, 10, 6)
        self.spacing = 2

        header = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=20, spacing=6
        )
        primary = run.get("ended_at_str") or run.get("started_at_str") or run.get("id") or "?"
        header.add_widget(value_label(str(primary), size="14sp"))
        self.add_widget(header)

        rid = run.get("id") or "?"
        sub = f"id {rid}"
        if is_active:
            sub += "  (ACTIVE)"
        self.add_widget(small_label(sub, size="11sp"))

        event_count = run.get("event_count", 0)
        data_rows = run.get("data_rows", 0)
        size = run.get("size_bytes", 0)
        self.add_widget(
            small_label(
                f"Events {event_count}  |  Data rows {data_rows}  |  {format_size(size)}",
                size="11sp",
            )
        )

        actions = BoxLayout(
            orientation="horizontal",
            size_hint_y=None,
            height=34,
            spacing=6,
            padding=(0, 4, 0, 0),
        )
        view_btn = _row_button("View", width=60)
        view_btn.bind(on_release=lambda _b: on_view(run))
        actions.add_widget(view_btn)

        dl_ev = _row_button("Download events", width=140)
        dl_ev.bind(on_release=lambda _b: on_download_events(run))
        actions.add_widget(dl_ev)

        dl_csv = _row_button("Download CSV", width=120)
        dl_csv.bind(on_release=lambda _b: on_download_csv(run))
        actions.add_widget(dl_csv)

        actions.add_widget(BoxLayout())  # spacer pushes buttons left
        self.add_widget(actions)


# ---- Event log viewer -----------------------------------------------------


class _EventLogViewer(BoxLayout):
    """Full-screen (within the Logs Screen) event log viewer.

    Toolbar: back button, level filter spinner, search field, line counter.
    Body: scrollable monospace label showing filtered lines. Kivy's
    SelectableLabel would be richer but a plain Label is enough for the
    phase's read-only requirement and avoids pulling in RecycleView.
    """

    def __init__(
        self,
        run_id: str,
        on_back: Callable[[], None],
        **kwargs,
    ):
        super().__init__(orientation="vertical", **kwargs)
        self.padding = (10, 8, 10, 8)
        self.spacing = 6
        self._run_id = run_id
        self._all_lines: List[str] = []

        # Toolbar
        toolbar = BoxLayout(
            orientation="horizontal",
            size_hint_y=None,
            height=36,
            spacing=6,
        )
        back_btn = Button(
            text="< Back",
            size_hint_x=None,
            width=80,
            font_size="12sp",
            background_color=(0.40, 0.42, 0.48, 1),
            color=(1, 1, 1, 1),
        )
        back_btn.bind(on_release=lambda _b: on_back())
        toolbar.add_widget(back_btn)

        self.level_spinner = spinner(values=_LEVEL_OPTIONS, initial=_LEVEL_ALL)
        self.level_spinner.size_hint_x = None
        self.level_spinner.width = 90
        self.level_spinner.bind(text=lambda _s, _v: self._render())
        toolbar.add_widget(self.level_spinner)

        self.search = text_input("", hint="Search")
        self.search.size_hint_x = 1
        self.search.multiline = False
        self.search.bind(text=lambda _i, _v: self._render())
        toolbar.add_widget(self.search)

        self.add_widget(toolbar)

        # Line counter
        self.counter = small_label("-", size="11sp")
        self.add_widget(self.counter)

        # Body: scrollable monospace text
        scroll = ScrollView(do_scroll_x=True, do_scroll_y=True)
        # Bare Label inside a 0-hint box so long lines can extend right
        # without being text-wrapped. Monospace font distinguishes the
        # columnar log format.
        self._body_box = BoxLayout(
            orientation="vertical",
            size_hint_x=None,
            size_hint_y=None,
            padding=(4, 4, 4, 4),
        )
        self._body_box.bind(
            minimum_width=self._body_box.setter("width"),
            minimum_height=self._body_box.setter("height"),
        )
        self._body_label = Label(
            text="",
            color=theme.TEXT_PRIMARY,
            font_size="11sp",
            font_name="RobotoMono-Regular",
            halign="left",
            valign="top",
            size_hint_x=None,
            size_hint_y=None,
            markup=False,
        )

        def _update_label_size(_w, texture_size):
            self._body_label.width = max(320, texture_size[0] + 8)
            self._body_label.height = max(20, texture_size[1] + 4)

        self._body_label.bind(texture_size=_update_label_size)
        self._body_box.add_widget(self._body_label)
        scroll.add_widget(self._body_box)
        self.add_widget(scroll)

        self.status = small_label("", size="11sp")
        self.status.color = theme.TEXT_SECONDARY
        self.add_widget(self.status)

    def set_loading(self) -> None:
        self.status.text = f"Loading event log for run {self._run_id} ..."
        self._body_label.text = ""
        self.counter.text = "-"

    def set_lines(self, lines: List[str]) -> None:
        self._all_lines = [ln for ln in lines if ln is not None]
        self.status.text = ""
        self._render()

    def set_error(self, err: str) -> None:
        self._all_lines = []
        self._body_label.text = ""
        self.counter.text = "-"
        self.status.text = err

    def _render(self) -> None:
        level = (self.level_spinner.text or _LEVEL_ALL).upper()
        query = (self.search.text or "").strip().lower()
        if level == _LEVEL_ALL and not query:
            filtered = self._all_lines
        else:
            filtered = []
            for line in self._all_lines:
                if level != _LEVEL_ALL and _line_level(line) != level:
                    continue
                if query and query not in line.lower():
                    continue
                filtered.append(line)
        total = len(self._all_lines)
        shown = len(filtered)
        self.counter.text = f"{shown} / {total} lines"
        # Label text size is driven by texture_size via the bind above,
        # so no explicit text_size is needed.
        self._body_label.text = "\n".join(filtered) if filtered else "(no matching lines)"


# ---- Main screen ----------------------------------------------------------


class LogsScreen(Screen):
    """AP/STA-only logs browser."""

    def __init__(
        self,
        connection: ConnectionManager,
        on_finish: Optional[Callable[[], None]] = None,
        **kwargs,
    ):
        super().__init__(name="logs", **kwargs)
        self.connection = connection
        self._on_finish = on_finish
        self._current_mode: str = MODE_OFFLINE
        self._runs: List[Dict[str, Any]] = []
        self._active_run_id: Optional[str] = None
        self._in_flight = False
        self._viewer: Optional[_EventLogViewer] = None

        with self.canvas.before:
            self._bg_color = Color(*theme.BG_DARK)
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(
            pos=lambda w, v: setattr(self._bg_rect, "pos", v),
            size=lambda w, v: setattr(self._bg_rect, "size", v),
        )

        self._root = BoxLayout(
            orientation="vertical",
            padding=(10, 8, 10, 8),
            spacing=6,
        )

        # Header
        header = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=32, spacing=6
        )
        title = value_label("Logs", size="16sp")
        title.size_hint_x = 1
        header.add_widget(title)
        refresh_btn = Button(
            text="Refresh",
            size_hint_x=None,
            width=80,
            font_size="12sp",
            background_color=(0.30, 0.55, 0.85, 1),
            color=(1, 1, 1, 1),
        )
        refresh_btn.bind(on_release=lambda _b: self.refresh_now())
        header.add_widget(refresh_btn)
        back_btn = Button(
            text="< Back",
            size_hint_x=None,
            width=80,
            font_size="12sp",
            background_color=(0.40, 0.42, 0.48, 1),
            color=(1, 1, 1, 1),
        )
        back_btn.bind(on_release=lambda _b: self._back())
        header.add_widget(back_btn)
        self._root.add_widget(header)

        # Storage indicator
        self.storage = _StorageBar()
        self._root.add_widget(self.storage)

        # Status line
        self.status_label = Label(
            text="",
            color=theme.TEXT_SECONDARY,
            font_size="11sp",
            size_hint_y=None,
            height=16,
            halign="left",
            valign="top",
        )
        self.status_label.bind(
            size=lambda w, s: setattr(w, "text_size", s),
        )
        self._root.add_widget(self.status_label)

        # Scrollable list of runs
        self._scroll = ScrollView(do_scroll_x=False, do_scroll_y=True)
        self._list_box = BoxLayout(
            orientation="vertical",
            spacing=6,
            size_hint_y=None,
            padding=(0, 0, 0, 8),
        )
        self._list_box.bind(minimum_height=self._list_box.setter("height"))
        self._scroll.add_widget(self._list_box)
        self._root.add_widget(self._scroll)

        self.add_widget(self._root)

        self.connection.add_listener(self._on_connection_change)

    # ---- lifecycle ---------------------------------------------------------

    def on_pre_enter(self, *args):
        # If we left mid-viewer, come back to the list. Fresh refresh so
        # storage info + run sizes reflect any changes made elsewhere.
        if self._viewer is not None:
            self._close_viewer()
        self.refresh_now()

    def _back(self) -> None:
        if self._viewer is not None:
            self._close_viewer()
            return
        if self._on_finish is not None:
            self._on_finish()

    def _on_connection_change(self, result: DetectResult) -> None:
        self._current_mode = result.mode
        if is_direct_mode(result.mode):
            # Only auto-refresh when we actually have a Pico to ask.
            if self._viewer is None:
                self.refresh_now()

    # ---- refresh -----------------------------------------------------------

    def refresh_now(self) -> None:
        if self._in_flight:
            return
        if not is_direct_mode(self._current_mode):
            self.status_label.text = "Pico not reachable - Logs require direct mode."
            self.storage.set_unknown("-")
            self._list_box.clear_widgets()
            return
        client = self.connection.client
        if client.config.base_url is None:
            return
        self._in_flight = True
        self.status_label.text = "Loading logs..."

        self._pending = 3  # runs + sdcard/info + /status
        self._pending_err: Optional[Exception] = None
        self._active_run_id = None

        def done_runs(result, err):
            if err is not None:
                self._pending_err = err
            else:
                self._runs = (result or {}).get("runs") or []
            self._tick()

        def done_sd(result, err):
            if err is None and isinstance(result, dict):
                self.storage.set_info(result)
            elif err is not None:
                self.storage.set_unknown(f"SD info failed: {err}")
            self._tick()

        def done_status(result, err):
            # /status failure is non-fatal; the worst that happens is we
            # don't flag the active run in the list.
            if err is None and isinstance(result, dict):
                if result.get("run_active"):
                    self._active_run_id = result.get("active_run_id")
            self._tick()

        call_async(lambda: client.runs(), done_runs)
        call_async(lambda: client.sdcard_info(), done_sd)
        call_async(lambda: client.status(), done_status)

    def _tick(self) -> None:
        self._pending -= 1
        if self._pending > 0:
            return
        self._in_flight = False
        if self._pending_err is not None:
            msg = str(self._pending_err)
            if len(msg) > 140:
                msg = msg[:137] + "..."
            self.status_label.text = f"Load failed: {msg}"
            return
        count = len(self._runs)
        self.status_label.text = f"{count} log set{'s' if count != 1 else ''}"
        self._render_list()

    def _render_list(self) -> None:
        self._list_box.clear_widgets()
        if not self._runs:
            empty = Label(
                text="No logs on SD card",
                color=theme.TEXT_MUTED,
                font_size="13sp",
                size_hint_y=None,
                height=60,
                halign="center",
                valign="middle",
            )
            empty.bind(size=lambda w, s: setattr(w, "text_size", s))
            self._list_box.add_widget(empty)
            return

        # Active run first, then server's mtime-desc order.
        runs = list(self._runs)
        if self._active_run_id:
            for i, r in enumerate(runs):
                if r.get("id") == self._active_run_id and i != 0:
                    runs.insert(0, runs.pop(i))
                    break

        for r in runs:
            row = _LogSetRow(
                r,
                is_active=(r.get("id") == self._active_run_id),
                on_view=self._open_viewer,
                on_download_events=self._download_events,
                on_download_csv=self._download_csv,
            )
            self._list_box.add_widget(row)

    # ---- viewer ------------------------------------------------------------

    def _open_viewer(self, run: Dict[str, Any]) -> None:
        run_id = run.get("id") or ""
        if not run_id:
            return
        # Swap the list area for the viewer. Keeping the top header is
        # redundant here - the viewer has its own toolbar with Back.
        self._root.remove_widget(self.storage)
        self._root.remove_widget(self.status_label)
        self._root.remove_widget(self._scroll)
        self._viewer = _EventLogViewer(run_id=run_id, on_back=self._close_viewer)
        self._root.add_widget(self._viewer)
        self._viewer.set_loading()

        client = self.connection.client

        def work():
            return client.logs_events(run_id)

        def done(result, err):
            if self._viewer is None:
                return  # user left while load was in-flight
            if err is not None:
                self._viewer.set_error(f"Load failed: {err}")
                return
            lines = []
            if isinstance(result, dict):
                lines = list(result.get("lines") or [])
            self._viewer.set_lines(lines)

        call_async(work, done)

    def _close_viewer(self) -> None:
        if self._viewer is None:
            return
        self._root.remove_widget(self._viewer)
        self._viewer = None
        # Restore list view widgets in original order
        self._root.add_widget(self.storage)
        self._root.add_widget(self.status_label)
        self._root.add_widget(self._scroll)

    # ---- downloads ---------------------------------------------------------

    def _download_events(self, run: Dict[str, Any]) -> None:
        run_id = run.get("id") or ""
        if not run_id:
            return
        self.status_label.text = f"Downloading event log for {run_id}..."
        client = self.connection.client

        def work():
            return client.logs_events(run_id)

        def done(result, err):
            if err is not None:
                self.status_label.text = f"Download failed: {err}"
                return
            lines = []
            if isinstance(result, dict):
                lines = list(result.get("lines") or [])
            text = "\n".join(str(ln) for ln in lines) + "\n"
            fname = f"event_{run_id}.txt"
            self._write_download(fname, text)

        call_async(work, done)

    def _download_csv(self, run: Dict[str, Any]) -> None:
        run_id = run.get("id") or ""
        if not run_id:
            return
        self.status_label.text = f"Downloading data CSV for {run_id}..."
        client = self.connection.client

        def work():
            return client.history(run=run_id)

        def done(result, err):
            if err is not None:
                self.status_label.text = f"Download failed: {err}"
                return
            if not isinstance(result, dict):
                self.status_label.text = "Download failed: unexpected response"
                return
            fields = list(result.get("fields") or [])
            rows = list(result.get("rows") or [])
            if not fields:
                self.status_label.text = "Download failed: no data fields in response"
                return
            text = _rows_to_csv(fields, rows)
            fname = f"data_{run_id}.csv"
            self._write_download(fname, text)

        call_async(work, done)

    def _write_download(self, fname: str, text: str) -> None:
        target_dir = download_dir()
        path = target_dir / fname
        try:
            path.write_text(text, encoding="utf-8")
        except Exception as e:
            self.status_label.text = f"Write failed: {e}"
            return
        self.status_label.text = f"Saved to {path}"
