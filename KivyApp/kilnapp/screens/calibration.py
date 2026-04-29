"""Moisture Calibration screen (AP/STA only).

Phase 12: calibrate per-channel MC% offsets against a handheld reference
meter. Flow matches `Specs/kivy_app_spec.md` "Moisture Calibration":

  1. User taps "Take reading" -> GET /moisture/live shows raw ohms and
     corrected MC% (mc_pct returned by the Pico already includes the
     currently loaded offset).
  2. User enters a reference MC% per channel and taps "Apply". The screen
     computes a proposed offset from the reference and the live reading
     (without persisting anything).
  3. "Save" POSTs {channel_1_offset, channel_2_offset} to /calibration.
     The Pico writes calibration.json and updates the running probe.
  4. "Reset to defaults" zeros both offsets and saves.

Offset math
-----------
Firmware applies ``corrected_mc = raw_mc + offset``. The Pico's
``/moisture/live`` returns the *corrected* mc_pct with the current
offset already applied, so:

    raw_mc        = corrected_mc - current_offset
    new_offset    = reference_mc - raw_mc
                  = reference_mc - corrected_mc + current_offset

The proposed-offset preview shows ``raw_mc + new_offset`` which, by
construction, equals the user's reference value.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from kivy.graphics import Color, Rectangle
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.screenmanager import Screen

from kilnapp import theme
from kilnapp.api.autodetect import DetectResult, MODE_OFFLINE, is_direct_mode
from kilnapp.api.client import call_async
from kilnapp.connection import ConnectionManager
from kilnapp.widgets.cards import Panel, small_label, value_label
from kilnapp.widgets.dialog import confirm
from kilnapp.widgets.form import text_input


_NOTES = (
    "Calibrate with probes inserted in boards at operating temperature\n"
    "for best accuracy. Temperature correction is applied automatically\n"
    "during kiln runs.\n\n"
    "Channel 1 and Channel 2 are positional (stack location), not\n"
    "species-specific. Species correction is set in the drying schedule."
)


def _fmt_mc(v: Any) -> str:
    if v is None:
        return "--"
    try:
        return f"{float(v):.1f}%"
    except (TypeError, ValueError):
        return "--"


def _fmt_offset(v: Any) -> str:
    if v is None:
        return "--"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "--"
    if f >= 0:
        return f"+{f:.2f}"
    return f"{f:.2f}"


def _fmt_ohms(v: Any) -> str:
    if v is None:
        return "--"
    try:
        i = int(v)
    except (TypeError, ValueError):
        return "--"
    if i >= 1_000_000:
        return f"{i / 1_000_000:.2f} MOhm"
    if i >= 1_000:
        return f"{i / 1_000:.1f} kOhm"
    return f"{i} Ohm"


def _fmt_timestamp(ts: Any) -> str:
    if ts is None or ts == 0:
        return "never (RTC was not set)"
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return str(ts)
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except Exception:
        return str(ts)


def _primary_button(text: str, on_press, *, width: Optional[int] = None) -> Button:
    btn = Button(
        text=text,
        size_hint_y=None,
        height=36,
        font_size="13sp",
        background_color=(0.30, 0.55, 0.85, 1),
        color=(1, 1, 1, 1),
    )
    if width is not None:
        btn.size_hint_x = None
        btn.width = width
    btn.bind(on_release=lambda _b: on_press())
    return btn


def _danger_button(text: str, on_press) -> Button:
    btn = Button(
        text=text,
        size_hint_y=None,
        height=36,
        font_size="13sp",
        background_color=(0.85, 0.30, 0.30, 1),
        color=(1, 1, 1, 1),
    )
    btn.bind(on_release=lambda _b: on_press())
    return btn


class _ChannelPanel(Panel):
    """One per channel. Shows live readings + reference input + preview.

    Proposed offset / preview state lives on the panel itself; the parent
    reads them at Save time.
    """

    def __init__(self, channel_label: str, **kwargs):
        super().__init__(**kwargs)
        self.padding = (10, 8, 10, 8)
        self.spacing = 4

        # Persisted proposed offset (starts at the loaded offset) and the
        # raw MC% captured when the user tapped Apply. `None` means "no
        # pending change for this channel" - Save won't touch it.
        self.proposed_offset: Optional[float] = None
        self.current_offset: float = 0.0
        self._last_corrected_mc: Optional[float] = None
        self._last_raw_mc: Optional[float] = None

        self.add_widget(value_label(channel_label, size="15sp"))

        # Row 1: raw resistance
        self.row_ohms = small_label("Resistance: --")
        self.add_widget(self.row_ohms)

        # Row 2: corrected MC% (with current offset applied, from Pico)
        self.row_corrected = small_label("Corrected MC%: --")
        self.add_widget(self.row_corrected)

        # Row 3: raw MC% (computed client-side)
        self.row_raw = small_label("Raw MC%: --")
        self.add_widget(self.row_raw)

        # Row 4: current loaded offset
        self.row_current_offset = small_label("Current offset: +0.00 MC%")
        self.add_widget(self.row_current_offset)

        # Row 5: temp-correction indicator
        self.row_temp = small_label("")
        self.row_temp.color = theme.TEXT_MUTED
        self.add_widget(self.row_temp)

        # Spacer
        spacer = BoxLayout(size_hint_y=None, height=4)
        self.add_widget(spacer)

        # Reference input + Apply button row
        ref_row = BoxLayout(
            orientation="horizontal",
            size_hint_y=None,
            height=40,
            spacing=6,
        )
        ref_lbl = Label(
            text="Reference MC%:",
            color=theme.TEXT_SECONDARY,
            font_size="12sp",
            size_hint_x=None,
            width=130,
            halign="left",
            valign="middle",
        )
        ref_lbl.bind(size=lambda w, s: setattr(w, "text_size", s))
        ref_row.add_widget(ref_lbl)
        self.reference_input = text_input("", input_filter="float", hint="e.g. 12.4")
        ref_row.add_widget(self.reference_input)
        self.apply_btn = _primary_button("Apply", self._on_apply, width=70)
        ref_row.add_widget(self.apply_btn)
        self.add_widget(ref_row)

        # Preview line (shows proposed offset and preview corrected MC%)
        self.preview_lbl = small_label("")
        self.preview_lbl.color = theme.SEVERITY_OK
        self.add_widget(self.preview_lbl)

    # ---- state setters -----------------------------------------------------

    def set_live(self, reading: Dict[str, Any]) -> None:
        """Populate the live-reading rows from a /moisture/live channel
        dict. Also caches the raw MC% for offset math."""
        corrected = reading.get("mc_pct")
        ohms = reading.get("resistance_ohms")
        temp_c = reading.get("temp_c")
        temp_corr = bool(reading.get("temp_corrected"))

        self._last_corrected_mc = corrected
        if corrected is None:
            self._last_raw_mc = None
        else:
            self._last_raw_mc = float(corrected) - float(self.current_offset)

        self.row_ohms.text = f"Resistance: {_fmt_ohms(ohms)}"
        self.row_corrected.text = f"Corrected MC%: {_fmt_mc(corrected)}"
        self.row_raw.text = (
            f"Raw MC%: {_fmt_mc(self._last_raw_mc)}"
            if self._last_raw_mc is not None
            else "Raw MC%: --"
        )
        if temp_c is not None and temp_corr:
            try:
                self.row_temp.text = f"Temp-corrected @ {float(temp_c):.1f} C"
            except (TypeError, ValueError):
                self.row_temp.text = "Temp-corrected"
        elif temp_c is None:
            self.row_temp.text = "No lumber temperature available (correction skipped)"
        else:
            self.row_temp.text = ""

    def set_current_offset(self, offset: float) -> None:
        self.current_offset = float(offset)
        self.row_current_offset.text = (
            f"Current offset: {_fmt_offset(self.current_offset)} MC%"
        )
        # If we haven't proposed a new offset yet, track current so a Save
        # without editing still writes the same value (no drift).
        if self.proposed_offset is None:
            pass
        # Recompute raw MC% if we already have a reading.
        if self._last_corrected_mc is not None:
            self._last_raw_mc = float(self._last_corrected_mc) - self.current_offset
            self.row_raw.text = f"Raw MC%: {_fmt_mc(self._last_raw_mc)}"

    def clear_proposed(self) -> None:
        self.proposed_offset = None
        self.preview_lbl.text = ""
        self.reference_input.text = ""

    # ---- interactions ------------------------------------------------------

    def _on_apply(self) -> None:
        txt = (self.reference_input.text or "").strip()
        if not txt:
            self.preview_lbl.color = theme.SEVERITY_ERROR
            self.preview_lbl.text = "Enter a reference MC% first."
            return
        try:
            reference = float(txt)
        except ValueError:
            self.preview_lbl.color = theme.SEVERITY_ERROR
            self.preview_lbl.text = "Reference must be numeric."
            return
        if self._last_raw_mc is None:
            self.preview_lbl.color = theme.SEVERITY_ERROR
            self.preview_lbl.text = "Take a live reading first."
            return
        # new_offset = reference - raw
        new_offset = reference - self._last_raw_mc
        # Sanity check: clamp to +/-50 MC% so a typo doesn't write a wildly
        # out-of-band value. (Typical offsets are within a couple of MC%.)
        if abs(new_offset) > 50.0:
            self.preview_lbl.color = theme.SEVERITY_ERROR
            self.preview_lbl.text = (
                f"Proposed offset {new_offset:+.2f} is out of range."
            )
            return
        self.proposed_offset = new_offset
        self.preview_lbl.color = theme.SEVERITY_OK
        self.preview_lbl.text = (
            f"Proposed offset {_fmt_offset(new_offset)} MC% "
            f"(preview {reference:.1f}%). Tap Save to write."
        )

    def offset_to_save(self) -> float:
        """What offset should be POSTed for this channel at Save time."""
        if self.proposed_offset is None:
            return self.current_offset
        return float(self.proposed_offset)


class CalibrationScreen(Screen):
    """AP/STA-only moisture calibration screen."""

    def __init__(
        self,
        connection: ConnectionManager,
        on_finish: Optional[Callable[[], None]] = None,
        **kwargs,
    ):
        super().__init__(name="calibration", **kwargs)
        self.connection = connection
        self._on_finish = on_finish
        self._current_mode: str = MODE_OFFLINE
        self._calibrated_at: Any = None
        self._cal_source: str = "defaults"

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

        # Header
        header = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=32, spacing=6
        )
        title = value_label("Moisture Calibration", size="16sp")
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

        # Status line
        self.status_label = Label(
            text="",
            color=theme.TEXT_SECONDARY,
            font_size="11sp",
            size_hint_y=None,
            height=18,
            halign="left",
            valign="top",
        )
        self.status_label.bind(size=lambda w, s: setattr(w, "text_size", s))
        root.add_widget(self.status_label)

        # Scrollable content
        scroll = ScrollView(do_scroll_x=False, do_scroll_y=True)
        content = BoxLayout(
            orientation="vertical",
            size_hint_y=None,
            spacing=6,
            padding=(0, 0, 0, 8),
        )
        content.bind(minimum_height=content.setter("height"))

        # Current calibration panel
        self.cal_panel = Panel()
        self.cal_panel.padding = (10, 8, 10, 8)
        self.cal_panel.add_widget(value_label("Current calibration", size="14sp"))
        self.cal_ch1_lbl = small_label("Channel 1: +0.00 MC%")
        self.cal_panel.add_widget(self.cal_ch1_lbl)
        self.cal_ch2_lbl = small_label("Channel 2: +0.00 MC%")
        self.cal_panel.add_widget(self.cal_ch2_lbl)
        self.cal_ts_lbl = small_label("Last calibrated: --")
        self.cal_panel.add_widget(self.cal_ts_lbl)
        self.cal_source_lbl = small_label("")
        self.cal_source_lbl.color = theme.TEXT_MUTED
        self.cal_panel.add_widget(self.cal_source_lbl)
        content.add_widget(self.cal_panel)

        # Take reading (single button drives both channels - /moisture/live
        # returns both at once so there's no reason to split them).
        read_row = BoxLayout(
            orientation="horizontal",
            size_hint_y=None,
            height=40,
            spacing=6,
        )
        self.take_btn = _primary_button("Take reading", self._take_reading)
        read_row.add_widget(self.take_btn)
        content.add_widget(read_row)

        # Channel panels
        self.ch1 = _ChannelPanel("Channel 1")
        content.add_widget(self.ch1)
        self.ch2 = _ChannelPanel("Channel 2")
        content.add_widget(self.ch2)

        # Save + Reset
        action_row = BoxLayout(
            orientation="horizontal",
            size_hint_y=None,
            height=40,
            spacing=6,
        )
        self.save_btn = _primary_button("Save", self._save)
        action_row.add_widget(self.save_btn)
        self.reset_btn = _danger_button("Reset to defaults", self._reset)
        action_row.add_widget(self.reset_btn)
        content.add_widget(action_row)

        # Notes panel
        notes = Panel()
        notes.padding = (10, 8, 10, 8)
        notes.add_widget(value_label("Notes", size="13sp"))
        notes_lbl = Label(
            text=_NOTES,
            color=theme.TEXT_SECONDARY,
            font_size="11sp",
            halign="left",
            valign="top",
            size_hint_y=None,
        )
        notes_lbl.bind(
            size=lambda w, s: setattr(w, "text_size", (s[0], None)),
            texture_size=lambda w, s: setattr(w, "height", max(40, s[1] + 4)),
        )
        notes.add_widget(notes_lbl)
        content.add_widget(notes)

        scroll.add_widget(content)
        root.add_widget(scroll)

        self.add_widget(root)

        self.connection.add_listener(self._on_connection_change)

    # ---- lifecycle --------------------------------------------------------

    def on_pre_enter(self, *args):
        self._refresh_all()

    def _back(self) -> None:
        if self._on_finish is not None:
            self._on_finish()

    def _on_connection_change(self, result: DetectResult) -> None:
        self._current_mode = result.mode
        direct = is_direct_mode(result.mode)
        for btn in (self.take_btn, self.save_btn, self.reset_btn):
            btn.disabled = not direct
            btn.opacity = 1.0 if direct else 0.5

    # ---- refresh ----------------------------------------------------------

    def _refresh_all(self) -> None:
        """Fetch calibration then live reading. Kept sequential-ish (two
        independent async calls) so a slow response on one doesn't starve
        the other - same pattern as Runs/Logs screens."""
        if not is_direct_mode(self._current_mode):
            self.status_label.text = "Pico not reachable - calibration requires direct mode."
            return
        client = self.connection.client
        if client.config.base_url is None:
            return
        self.status_label.text = "Loading calibration..."
        # Clear proposed state so a fresh visit starts clean.
        self.ch1.clear_proposed()
        self.ch2.clear_proposed()

        def work_cal():
            return client.calibration_get()

        def done_cal(result, err):
            if err is not None:
                self.status_label.text = f"Calibration load failed: {err}"
                return
            self._apply_calibration(result or {})
            # Now kick off the live reading.
            self._take_reading()

        call_async(work_cal, done_cal)

    def _apply_calibration(self, data: Dict[str, Any]) -> None:
        ch1 = float(data.get("channel_1_offset") or 0.0)
        ch2 = float(data.get("channel_2_offset") or 0.0)
        self._calibrated_at = data.get("calibrated_at")
        self._cal_source = str(data.get("source") or "defaults")
        self.cal_ch1_lbl.text = f"Channel 1: {_fmt_offset(ch1)} MC%"
        self.cal_ch2_lbl.text = f"Channel 2: {_fmt_offset(ch2)} MC%"
        self.cal_ts_lbl.text = f"Last calibrated: {_fmt_timestamp(self._calibrated_at)}"
        if self._cal_source == "defaults":
            self.cal_source_lbl.text = (
                "No calibration file - factory defaults in use (0.0 offset)"
            )
        else:
            self.cal_source_lbl.text = f"Source: {self._cal_source}"
        self.ch1.set_current_offset(ch1)
        self.ch2.set_current_offset(ch2)

    def _take_reading(self) -> None:
        if not is_direct_mode(self._current_mode):
            return
        client = self.connection.client
        if client.config.base_url is None:
            return
        self.status_label.text = "Reading probes..."

        def work():
            return client.moisture_live()

        def done(result, err):
            if err is not None:
                self.status_label.text = f"Read failed: {err}"
                return
            if not isinstance(result, dict):
                self.status_label.text = "Read failed: unexpected response"
                return
            self.ch1.set_live(result.get("channel_1") or {})
            self.ch2.set_live(result.get("channel_2") or {})
            self.status_label.text = "Reading captured."

        call_async(work, done)

    # ---- save / reset -----------------------------------------------------

    def _save(self) -> None:
        if not is_direct_mode(self._current_mode):
            return
        pending_ch1 = self.ch1.proposed_offset is not None
        pending_ch2 = self.ch2.proposed_offset is not None
        if not pending_ch1 and not pending_ch2:
            self.status_label.text = "No pending changes - tap Apply first."
            return
        ch1 = self.ch1.offset_to_save()
        ch2 = self.ch2.offset_to_save()
        summary = (
            f"Save offsets?\n\n"
            f"Channel 1: {_fmt_offset(ch1)} MC%\n"
            f"Channel 2: {_fmt_offset(ch2)} MC%"
        )
        confirm(
            "Save calibration",
            summary,
            on_confirm=lambda: self._do_save(ch1, ch2),
            confirm_text="Save",
        )

    def _do_save(self, ch1: float, ch2: float) -> None:
        client = self.connection.client
        self.status_label.text = "Saving..."

        def work():
            return client.calibration_post(ch1, ch2)

        def done(result, err):
            if err is not None:
                self.status_label.text = f"Save failed: {err}"
                return
            self.status_label.text = "Calibration saved."
            # Re-fetch so the Current panel and offsets reflect the Pico.
            self._refresh_all()

        call_async(work, done)

    def _reset(self) -> None:
        if not is_direct_mode(self._current_mode):
            return
        confirm(
            "Reset calibration",
            "Reset both channel offsets to 0.0? This writes "
            "calibration.json on the SD card.",
            on_confirm=self._do_reset,
            confirm_text="Reset",
            danger=True,
        )

    def _do_reset(self) -> None:
        client = self.connection.client
        self.status_label.text = "Resetting..."

        def work():
            return client.calibration_post(0.0, 0.0)

        def done(result, err):
            if err is not None:
                self.status_label.text = f"Reset failed: {err}"
                return
            self.status_label.text = "Calibration reset."
            self._refresh_all()

        call_async(work, done)
