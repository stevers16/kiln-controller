"""System Test screen (AP/STA only).

Phase 10: run the Pico's built-in hardware test suite and stream results
into the app.

Flow:
  1. User taps "Run System Test" -> confirmation dialog.
  2. Client POSTs /test/run, starts a 1 s poll of /test/status.
  3. Results are rendered as rows grouped by category; a progress bar
     and elapsed-time counter update each tick.
  4. When /test/status returns complete=True, a final summary appears
     with overall pass/fail, per-status counts, and Save / Copy buttons.

The screen is hidden from Cottage mode (same gating pattern as Schedules).
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from kivy.clock import Clock
from kivy.core.clipboard import Clipboard
from kivy.graphics import Color, Rectangle
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.progressbar import ProgressBar
from kivy.uix.scrollview import ScrollView
from kivy.uix.screenmanager import Screen

from kilnapp import theme
from kilnapp.api.autodetect import DetectResult, MODE_DIRECT, MODE_OFFLINE, MODE_STA
from kilnapp.api.client import call_async
from kilnapp.connection import ConnectionManager
from kilnapp.widgets.cards import Panel, small_label, value_label
from kilnapp.widgets.dialog import confirm


POLL_INTERVAL_S = 1.0

# Tests that actually energise a load. Surfaced in the pre-run warning so
# the operator can decide whether the kiln is safe to exercise right now.
_SAFETY_NOTES = (
    "Ensure the kiln is safe to operate during test.\n"
    "The heater, exhaust fan, circulation fans, and vent servos\n"
    "will activate briefly. Keep the door clear."
)

_STATUS_COLORS = {
    "pass": theme.SEVERITY_OK,
    "fail": theme.SEVERITY_ERROR,
    "skip": theme.TEXT_MUTED,
    "running": (0.25, 0.55, 0.95, 1),
    "pending": theme.TEXT_MUTED,
}

_STATUS_LABELS = {
    "pass": "PASS",
    "fail": "FAIL",
    "skip": "SKIP",
    "running": "RUN",
    "pending": "...",
}


def _fmt_duration(ms: Optional[int]) -> str:
    if ms is None:
        return ""
    if ms < 1000:
        return f"{ms} ms"
    return f"{ms / 1000:.1f} s"


def _fmt_elapsed(s: int) -> str:
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60:02d}s"


class _StatusBadge(BoxLayout):
    """Small coloured pill showing a test's current state."""

    def __init__(self, **kwargs):
        super().__init__(orientation="vertical", **kwargs)
        self.size_hint = (None, None)
        self.size = (56, 20)
        with self.canvas.before:
            self._bg = Color(*theme.TEXT_MUTED)
            self._rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(
            pos=lambda w, v: setattr(self._rect, "pos", v),
            size=lambda w, v: setattr(self._rect, "size", v),
        )
        self._lbl = Label(
            text="...",
            color=(1, 1, 1, 1),
            font_size="10sp",
            bold=True,
            halign="center",
            valign="middle",
        )
        self._lbl.bind(size=lambda w, s: setattr(w, "text_size", s))
        self.add_widget(self._lbl)
        self.set_status("pending")

    def set_status(self, status: str) -> None:
        self._lbl.text = _STATUS_LABELS.get(status, status.upper())
        col = _STATUS_COLORS.get(status, theme.TEXT_MUTED)
        self._bg.rgba = col


class _TestRow(Panel):
    """One row per test. `update()` is called each poll tick to refresh
    status, duration, and detail without rebuilding the widget."""

    def __init__(self, test: Dict[str, Any], **kwargs):
        super().__init__(**kwargs)
        self.padding = (8, 4, 8, 4)
        self.spacing = 1
        self._tid = test.get("id", "")

        top = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=22, spacing=6
        )
        self.name_lbl = value_label(
            str(test.get("name") or self._tid), size="13sp"
        )
        self.name_lbl.size_hint_x = 1
        top.add_widget(self.name_lbl)
        self.badge = _StatusBadge()
        top.add_widget(self.badge)
        self.duration_lbl = small_label("", size="10sp")
        self.duration_lbl.size_hint_x = None
        self.duration_lbl.width = 56
        top.add_widget(self.duration_lbl)
        self.add_widget(top)

        self.detail_lbl = small_label("", size="11sp")
        self.detail_lbl.color = theme.TEXT_SECONDARY
        self.add_widget(self.detail_lbl)

        self.update(test)

    def update(self, test: Dict[str, Any]) -> None:
        status = test.get("status") or "pending"
        self.badge.set_status(status)
        self.duration_lbl.text = _fmt_duration(test.get("duration_ms"))
        detail = test.get("detail") or ""
        # Colour failing-row detail in red, otherwise secondary.
        if status == "fail":
            self.detail_lbl.color = theme.SEVERITY_ERROR
        else:
            self.detail_lbl.color = theme.TEXT_SECONDARY
        self.detail_lbl.text = str(detail)


class SystemTestScreen(Screen):
    """AP/STA-only test runner."""

    def __init__(
        self,
        connection: ConnectionManager,
        on_finish: Optional[Callable[[], None]] = None,
        **kwargs,
    ):
        super().__init__(name="system_test", **kwargs)
        self.connection = connection
        self._on_finish = on_finish

        self._current_mode: str = MODE_OFFLINE
        self._poll_event = None
        self._start_ts: Optional[float] = None
        self._elapsed_tick_event = None
        self._running = False
        self._last_status: Optional[Dict[str, Any]] = None
        # Rows keyed by test id so /test/status updates don't rebuild them.
        self._rows: Dict[str, _TestRow] = {}
        # Section header widgets by group name, used so we can insert
        # rows in discovery order.
        self._section_boxes: Dict[str, BoxLayout] = {}

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

        # Header: title + back
        header = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=32, spacing=6
        )
        title = value_label("System Test", size="16sp")
        title.size_hint_x = 1
        header.add_widget(title)
        back_btn = Button(
            text="< Back",
            size_hint_x=None,
            width=80,
            font_size="13sp",
            background_color=(0.40, 0.42, 0.48, 1),
            color=(1, 1, 1, 1),
        )
        back_btn.bind(on_release=lambda _b: self._back())
        header.add_widget(back_btn)
        root.add_widget(header)

        # Warning / intro panel
        intro = Panel()
        intro.padding = (10, 8, 10, 8)
        intro_title = value_label("Hardware test suite", size="14sp")
        intro.add_widget(intro_title)
        intro.add_widget(
            small_label(
                "Runs 18 unit / integration / commissioning tests.",
                size="11sp",
            )
        )
        intro.add_widget(
            small_label(
                "Estimated duration: 3-5 minutes.",
                size="11sp",
            )
        )
        # Multi-line warning
        warn = Label(
            text=_SAFETY_NOTES,
            color=theme.SEVERITY_WARN,
            font_size="11sp",
            halign="left",
            valign="top",
            size_hint_y=None,
        )
        warn.bind(
            size=lambda w, s: setattr(w, "text_size", (s[0], None)),
            texture_size=lambda w, s: setattr(w, "height", max(40, s[1] + 4)),
        )
        intro.add_widget(warn)
        root.add_widget(intro)

        # Run + action row
        action_row = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=40, spacing=6
        )
        self.run_btn = Button(
            text="Run System Test",
            font_size="14sp",
            background_color=(0.30, 0.55, 0.85, 1),
            color=(1, 1, 1, 1),
        )
        self.run_btn.bind(on_release=lambda _b: self._on_run_pressed())
        action_row.add_widget(self.run_btn)
        root.add_widget(action_row)

        # Progress + elapsed row
        progress_row = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=22, spacing=6
        )
        self.progress_lbl = small_label("Ready.", size="11sp")
        self.progress_lbl.size_hint_x = 1
        progress_row.add_widget(self.progress_lbl)
        self.elapsed_lbl = small_label("", size="11sp")
        self.elapsed_lbl.size_hint_x = None
        self.elapsed_lbl.width = 80
        self.elapsed_lbl.halign = "right"
        progress_row.add_widget(self.elapsed_lbl)
        root.add_widget(progress_row)

        self.progress = ProgressBar(
            max=1.0, value=0.0, size_hint_y=None, height=8
        )
        root.add_widget(self.progress)

        # Summary banner (hidden until test completes)
        self.summary_panel = Panel()
        self.summary_panel.padding = (10, 6, 10, 6)
        self.summary_title = value_label("", size="14sp")
        self.summary_panel.add_widget(self.summary_title)
        self.summary_detail = small_label("", size="11sp")
        self.summary_panel.add_widget(self.summary_detail)
        summary_actions = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=34, spacing=6
        )
        self.save_btn = Button(
            text="Save results",
            size_hint_x=None,
            width=130,
            font_size="12sp",
            background_color=(0.30, 0.55, 0.85, 1),
            color=(1, 1, 1, 1),
        )
        self.save_btn.bind(on_release=lambda _b: self._save_results())
        summary_actions.add_widget(self.save_btn)
        self.copy_btn = Button(
            text="Copy to clipboard",
            size_hint_x=None,
            width=160,
            font_size="12sp",
            background_color=(0.30, 0.32, 0.38, 1),
            color=(1, 1, 1, 1),
        )
        self.copy_btn.bind(on_release=lambda _b: self._copy_to_clipboard())
        summary_actions.add_widget(self.copy_btn)
        summary_actions.add_widget(BoxLayout())
        self.summary_panel.add_widget(summary_actions)
        self._summary_visible = False
        # We add/remove the summary panel from the root dynamically so it
        # doesn't take vertical space before the first test run.
        self._root_box = root

        # Scrollable test list
        scroll = ScrollView(do_scroll_x=False, do_scroll_y=True)
        self.list_box = BoxLayout(
            orientation="vertical",
            spacing=6,
            size_hint_y=None,
            padding=(0, 0, 0, 8),
        )
        self.list_box.bind(minimum_height=self.list_box.setter("height"))
        scroll.add_widget(self.list_box)
        root.add_widget(scroll)

        self.add_widget(root)

        self.connection.add_listener(self._on_connection_change)

    # ---- lifecycle ---------------------------------------------------------

    def on_pre_leave(self, *args):
        # Leaving the screen does NOT stop the test on the Pico (there is
        # no /test/cancel endpoint). Just stop polling locally.
        self._stop_polling()

    def _back(self) -> None:
        if self._on_finish:
            self._on_finish()

    def _on_connection_change(self, result: DetectResult) -> None:
        self._current_mode = result.mode
        direct = result.mode in (MODE_DIRECT, MODE_STA)
        self.run_btn.disabled = not direct or self._running
        self.run_btn.opacity = 1.0 if direct else 0.5
        if not direct and self._running:
            self._stop_polling()
            self.progress_lbl.text = "Connection lost - test may still be running on device."

    # ---- launch ------------------------------------------------------------

    def _on_run_pressed(self) -> None:
        if self._running:
            return
        if self._current_mode not in (MODE_DIRECT, MODE_STA):
            return
        confirm(
            "Start system test",
            "This activates the heater, fans, and vents briefly.\n"
            "Estimated duration: 3-5 minutes.\n\n"
            "Start the test now?",
            on_confirm=self._start_test,
            confirm_text="Start",
        )

    def _start_test(self) -> None:
        client = self.connection.client
        self.progress_lbl.text = "Starting test..."
        self.run_btn.disabled = True
        self._hide_summary()
        self._clear_rows()
        self.progress.value = 0.0
        self.elapsed_lbl.text = "0s"

        def work():
            return client.test_run()

        def done(result, err):
            if err is not None:
                self.progress_lbl.text = f"Start failed: {err}"
                self.run_btn.disabled = False
                return
            info = result or {}
            count = int(info.get("test_count", 0))
            self.progress_lbl.text = (
                f"Test running... ({count} tests)" if count else "Test running..."
            )
            self._running = True
            self._start_ts = time.monotonic()
            # Tick elapsed every second even if a poll is in flight.
            self._elapsed_tick_event = Clock.schedule_interval(
                lambda _dt: self._tick_elapsed(), 1.0
            )
            # Poll /test/status on an interval. First fire happens after
            # POLL_INTERVAL_S, so kick off an immediate one so the list
            # populates before that first tick.
            self._poll_once()
            self._poll_event = Clock.schedule_interval(
                lambda _dt: self._poll_once(), POLL_INTERVAL_S
            )

        call_async(work, done)

    # ---- polling -----------------------------------------------------------

    def _stop_polling(self) -> None:
        if self._poll_event is not None:
            self._poll_event.cancel()
            self._poll_event = None
        if self._elapsed_tick_event is not None:
            self._elapsed_tick_event.cancel()
            self._elapsed_tick_event = None
        self._running = False

    def _tick_elapsed(self) -> None:
        if self._start_ts is None:
            return
        self.elapsed_lbl.text = _fmt_elapsed(int(time.monotonic() - self._start_ts))

    def _poll_once(self) -> None:
        client = self.connection.client
        if client.config.base_url is None:
            return

        def work():
            return client.test_status()

        def done(result, err):
            if err is not None:
                # Don't kill the poll loop on a transient error - the
                # Pico might just be mid-test and slow to respond.
                self.progress_lbl.text = f"Poll error: {err}"
                return
            if not isinstance(result, dict):
                return
            self._apply_status(result)

        call_async(work, done)

    # ---- render ------------------------------------------------------------

    def _clear_rows(self) -> None:
        self.list_box.clear_widgets()
        self._rows.clear()
        self._section_boxes.clear()

    def _ensure_section(self, group: str) -> BoxLayout:
        box = self._section_boxes.get(group)
        if box is not None:
            return box
        header = small_label(group or "Tests", size="12sp")
        header.color = theme.TEXT_PRIMARY
        header.bold = True
        header.height = 22
        self.list_box.add_widget(header)
        box = BoxLayout(
            orientation="vertical",
            spacing=4,
            size_hint_y=None,
        )
        box.bind(minimum_height=box.setter("height"))
        self.list_box.add_widget(box)
        self._section_boxes[group] = box
        return box

    def _apply_status(self, data: Dict[str, Any]) -> None:
        self._last_status = data
        tests = data.get("tests") or []

        # Upsert rows
        for t in tests:
            tid = t.get("id")
            if not tid:
                continue
            row = self._rows.get(tid)
            if row is None:
                section = self._ensure_section(str(t.get("group") or ""))
                row = _TestRow(t)
                section.add_widget(row)
                self._rows[tid] = row
            else:
                row.update(t)

        # Progress bar: fraction of tests that have finished (pass/fail/skip).
        total = len(tests)
        done = sum(
            1
            for t in tests
            if t.get("status") in ("pass", "fail", "skip")
        )
        if total > 0:
            self.progress.value = done / total
        else:
            self.progress.value = 0.0

        passed = int(data.get("passed", 0))
        failed = int(data.get("failed", 0))
        skipped = int(data.get("skipped", 0))
        pending = int(data.get("pending", 0))

        if data.get("complete"):
            # Final tick: stop polling, lock in elapsed, show summary.
            self._tick_elapsed()
            self._stop_polling()
            self.run_btn.disabled = self._current_mode not in (
                MODE_DIRECT,
                MODE_STA,
            )
            self.progress.value = 1.0
            overall = data.get("overall") or ("pass" if failed == 0 else "fail")
            self.progress_lbl.text = (
                f"Complete: {passed} pass / {failed} fail / {skipped} skip"
            )
            self._show_summary(overall, passed, failed, skipped, total)
        else:
            self.progress_lbl.text = (
                f"Running: {done}/{total} complete "
                f"({passed} pass, {failed} fail, {pending} pending)"
            )

    # ---- summary -----------------------------------------------------------

    def _show_summary(
        self,
        overall: str,
        passed: int,
        failed: int,
        skipped: int,
        total: int,
    ) -> None:
        if not self._summary_visible:
            # Insert just above the scrollable test list. The summary
            # panel is the 5th child in the root box (after header,
            # intro, action row, progress row, progress bar).
            # Inserting at a fixed index keeps it above the scroll view.
            self._root_box.add_widget(self.summary_panel, index=1)
            self._summary_visible = True
        if overall == "pass":
            self.summary_title.text = "PASS"
            self.summary_title.color = theme.SEVERITY_OK
        else:
            self.summary_title.text = "FAIL"
            self.summary_title.color = theme.SEVERITY_ERROR
        self.summary_detail.text = (
            f"{passed} passed, {failed} failed, {skipped} skipped "
            f"({total} total)"
        )

    def _hide_summary(self) -> None:
        if self._summary_visible:
            self._root_box.remove_widget(self.summary_panel)
            self._summary_visible = False

    # ---- export ------------------------------------------------------------

    def _report_text(self) -> str:
        data = self._last_status or {}
        tests = data.get("tests") or []
        lines = []
        lines.append("Kiln System Test Report")
        lines.append(
            f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}"
        )
        overall = data.get("overall")
        if overall:
            lines.append(f"Overall: {overall.upper()}")
        lines.append(
            f"Passed: {int(data.get('passed', 0))}  "
            f"Failed: {int(data.get('failed', 0))}  "
            f"Skipped: {int(data.get('skipped', 0))}"
        )
        if self._start_ts is not None:
            lines.append(
                f"Elapsed: {_fmt_elapsed(int(time.monotonic() - self._start_ts))}"
            )
        lines.append("")
        current_group = None
        for t in tests:
            group = t.get("group") or ""
            if group != current_group:
                current_group = group
                lines.append(f"[{group}]")
            status = (t.get("status") or "").upper() or "?"
            duration = _fmt_duration(t.get("duration_ms"))
            name = t.get("name") or t.get("id") or ""
            detail = t.get("detail") or ""
            line = f"  {status:<6} {name}"
            if duration:
                line += f"  ({duration})"
            if detail:
                line += f"  - {detail}"
            lines.append(line)
        return "\n".join(lines) + "\n"

    def _save_results(self) -> None:
        if self._last_status is None:
            self.progress_lbl.text = "No results to save yet."
            return
        from pathlib import Path

        text = self._report_text()
        # Prefer the user's Downloads folder on desktop; fall back to the
        # app's user_data_dir (which the App class has already created).
        target_dir = Path.home() / "Downloads"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            from kivy.app import App as _App

            app = _App.get_running_app()
            target_dir = Path(app.user_data_dir if app else ".")
        fname = f"kiln_system_test_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        path = target_dir / fname
        try:
            path.write_text(text, encoding="utf-8")
        except Exception as e:
            self.progress_lbl.text = f"Save failed: {e}"
            return
        self.progress_lbl.text = f"Saved to {path}"

    def _copy_to_clipboard(self) -> None:
        if self._last_status is None:
            self.progress_lbl.text = "No results to copy yet."
            return
        try:
            Clipboard.copy(self._report_text())
        except Exception as e:
            self.progress_lbl.text = f"Copy failed: {e}"
            return
        self.progress_lbl.text = "Report copied to clipboard."
