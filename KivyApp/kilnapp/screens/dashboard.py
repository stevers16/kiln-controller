"""Dashboard screen - Phase 3 (read-only MVP).

Polls /status from whichever endpoint ConnectionManager is currently using
(10s in AP/STA mode, 35s in Cottage mode per kivy_app_spec.md). Displays:

- Stage banner (one line, no progress bar yet - Phase 4)
- Lumber + Intake sensor columns (temp / RH)
- Moisture content (Ch1, Ch2)
- Equipment state row (heater, vents, exhaust fan, circulation fans)
- Rail currents (12V, 5V)
- Last update timestamp

Greys out all values when the connection is lost or data is older than the
"stale threshold" (3x the refresh interval).

No banners, no colour-coded deadbands, no action buttons - those land in
Phase 4. No LoRa block - that's Cottage-mode + Phase 4 territory.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from kivy.clock import Clock
from kivy.graphics import Color, Rectangle
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.screenmanager import Screen

from kilnapp import theme
from kilnapp.api.autodetect import (
    DetectResult,
    MODE_COTTAGE,
    MODE_DIRECT,
    MODE_OFFLINE,
    MODE_STA,
)
from kilnapp.api.client import call_async
from kilnapp.connection import ConnectionManager
from kilnapp.widgets.cards import Panel, small_label, value_label


REFRESH_AP_S = 10
REFRESH_COTTAGE_S = 35
STALE_MULTIPLIER = 3  # data older than N x refresh interval is greyed out

EM_DASH = "--"


def _fmt_temp(v: Optional[float]) -> str:
    if v is None:
        return EM_DASH
    return f"{v:.1f} C"


def _fmt_rh(v: Optional[float]) -> str:
    if v is None:
        return EM_DASH
    return f"{v:.1f} %"


def _fmt_mc(v: Optional[float]) -> str:
    if v is None:
        return EM_DASH
    return f"{v:.1f} %"


def _fmt_ohms(v: Optional[int]) -> str:
    if v is None:
        return EM_DASH
    if v >= 1_000_000:
        return f"{v / 1_000_000:.2f} Mohm"
    if v >= 1_000:
        return f"{v / 1_000:.1f} kohm"
    return f"{v} ohm"


def _fmt_pct(v: Optional[int]) -> str:
    if v is None:
        return EM_DASH
    return f"{v}%"


def _fmt_ma(v: Optional[float]) -> str:
    if v is None:
        return EM_DASH
    return f"{v:.0f} mA"


def _fmt_target(v: Optional[float], suffix: str) -> str:
    if v is None:
        return ""
    return f"target {v:.1f} {suffix}"


def _fmt_age(seconds: float) -> str:
    if seconds < 0:
        return "just now"
    if seconds < 90:
        return f"{int(seconds)}s ago"
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    return f"{hours:.1f}h ago"


class DashboardScreen(Screen):
    def __init__(self, connection: ConnectionManager, **kwargs):
        super().__init__(name="dashboard", **kwargs)
        self.connection = connection

        self._refresh_event = None
        self._in_flight = False
        self._last_data: Optional[Dict[str, Any]] = None
        self._last_data_at: Optional[float] = None  # local monotonic time
        self._current_mode: str = MODE_OFFLINE

        # ---- background ---------------------------------------------------
        with self.canvas.before:
            self._bg_color = Color(*theme.BG_DARK)
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(
            pos=lambda w, v: setattr(self._bg_rect, "pos", v),
            size=lambda w, v: setattr(self._bg_rect, "size", v),
        )

        # ---- scrollable content ------------------------------------------
        scroll = ScrollView(do_scroll_x=False, do_scroll_y=True)
        content = BoxLayout(
            orientation="vertical",
            padding=(12, 12, 12, 12),
            spacing=10,
            size_hint_y=None,
        )
        content.bind(minimum_height=content.setter("height"))

        # Stage banner - auto-sized to its two labels
        self.stage_banner = Panel()
        self.stage_title = value_label("No run active", size="16sp")
        self.stage_subtitle = small_label("", size="12sp")
        self.stage_banner.add_widget(self.stage_title)
        self.stage_banner.add_widget(self.stage_subtitle)
        content.add_widget(self.stage_banner)

        # Sensor row: Lumber | Intake side-by-side. The panels are forced to
        # size_hint_y=1 so they fill the row height the parent assigns.
        sensors = BoxLayout(orientation="horizontal", spacing=10, size_hint_y=None, height=130)
        self.lumber_panel, self.lumber_widgets = self._build_sensor_panel(
            "Lumber zone", show_targets=True, fill_height=True
        )
        self.intake_panel, self.intake_widgets = self._build_sensor_panel(
            "Intake", show_targets=False, fill_height=True
        )
        sensors.add_widget(self.lumber_panel)
        sensors.add_widget(self.intake_panel)
        content.add_widget(sensors)

        # Moisture content panel - auto-sized
        self.moisture_panel = Panel()
        self.moisture_panel.add_widget(small_label("Moisture content", bold=True))
        self.mc1_value = value_label("Ch1: " + EM_DASH)
        self.mc1_detail = small_label("")
        self.mc2_value = value_label("Ch2: " + EM_DASH)
        self.mc2_detail = small_label("")
        self.mc_target = small_label("")
        for w in (self.mc1_value, self.mc1_detail, self.mc2_value, self.mc2_detail, self.mc_target):
            self.moisture_panel.add_widget(w)
        content.add_widget(self.moisture_panel)

        # Equipment state panel - auto-sized
        self.equipment_panel = Panel()
        self.equipment_panel.add_widget(small_label("Equipment", bold=True))
        self.eq_heater = small_label("Heater: " + EM_DASH)
        self.eq_vents = small_label("Vents: " + EM_DASH)
        self.eq_exhaust = small_label("Exhaust fan: " + EM_DASH)
        self.eq_circ = small_label("Circulation fans: " + EM_DASH)
        for w in (self.eq_heater, self.eq_vents, self.eq_exhaust, self.eq_circ):
            self.equipment_panel.add_widget(w)
        content.add_widget(self.equipment_panel)

        # Rail currents - auto-sized
        self.rails_panel = Panel()
        self.rails_panel.add_widget(small_label("Rails", bold=True))
        rails_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=22, spacing=12)
        self.rail_12v = small_label("12V: " + EM_DASH)
        self.rail_5v = small_label("5V: " + EM_DASH)
        rails_row.add_widget(self.rail_12v)
        rails_row.add_widget(self.rail_5v)
        self.rails_panel.add_widget(rails_row)
        content.add_widget(self.rails_panel)

        # Footer: last update + refresh button
        footer = BoxLayout(orientation="horizontal", size_hint_y=None, height=44, spacing=8)
        self.footer_label = small_label("Waiting for data...", size="12sp")
        refresh_btn = Button(
            text="Refresh",
            size_hint_x=None,
            width=90,
            font_size="13sp",
            background_color=(0.30, 0.55, 0.85, 1),
            color=(1, 1, 1, 1),
        )
        refresh_btn.bind(on_release=lambda _b: self.refresh_now())
        footer.add_widget(self.footer_label)
        footer.add_widget(refresh_btn)
        content.add_widget(footer)

        scroll.add_widget(content)
        self.add_widget(scroll)

        # Subscribe to connection changes
        self.connection.add_listener(self._on_connection_change)
        # Tick every second to update the "Updated Xs ago" line and stale state
        Clock.schedule_interval(self._on_tick, 1.0)

    # ---- panel construction helpers ---------------------------------------

    def _build_sensor_panel(self, title: str, *, show_targets: bool, fill_height: bool = False):
        # When the panel is going into a side-by-side row with a fixed height,
        # we want it to fill that height (size_hint_y=1) instead of hugging
        # its content.
        if fill_height:
            panel = Panel(size_hint_y=1)
        else:
            panel = Panel()
        panel.add_widget(small_label(title, bold=True))
        temp_value = value_label("T " + EM_DASH)
        temp_target = small_label("")
        rh_value = value_label("RH " + EM_DASH)
        rh_target = small_label("")
        for w in (temp_value, temp_target, rh_value, rh_target):
            panel.add_widget(w)
        return panel, {
            "temp_value": temp_value,
            "temp_target": temp_target,
            "rh_value": rh_value,
            "rh_target": rh_target,
            "show_targets": show_targets,
        }

    # ---- connection wiring -------------------------------------------------

    def _on_connection_change(self, result: DetectResult) -> None:
        self._current_mode = result.mode
        self._reschedule()
        if result.mode != MODE_OFFLINE:
            # Pull a fresh status immediately on (re)connection
            self.refresh_now()
        else:
            # Don't clear the data - just let the stale logic grey it out
            self._update_footer()

    def _refresh_interval(self) -> int:
        if self._current_mode == MODE_COTTAGE:
            return REFRESH_COTTAGE_S
        return REFRESH_AP_S

    def _reschedule(self) -> None:
        if self._refresh_event is not None:
            self._refresh_event.cancel()
            self._refresh_event = None
        if self._current_mode == MODE_OFFLINE:
            return
        self._refresh_event = Clock.schedule_interval(
            lambda _dt: self.refresh_now(), self._refresh_interval()
        )

    # ---- the actual fetch --------------------------------------------------

    def refresh_now(self) -> None:
        if self._in_flight:
            return
        if self._current_mode == MODE_OFFLINE:
            return
        if self.connection.client.config.base_url is None:
            return
        self._in_flight = True
        client = self.connection.client

        def work():
            return client.status()

        def done(result, err):
            self._in_flight = False
            if err is not None or result is None:
                # Don't blow up - just leave stale data and update footer text.
                self.footer_label.text = f"Refresh failed: {err}"
                return
            self._last_data = result
            self._last_data_at = time.monotonic()
            self._render(result)
            self._update_footer()

        call_async(work, done)

    # ---- render ------------------------------------------------------------

    def _render(self, data: Dict[str, Any]) -> None:
        # Stage banner
        if data.get("run_active"):
            stage_idx = data.get("stage_index")
            stage_name = data.get("stage_name") or "Unknown stage"
            stage_type = (data.get("stage_type") or "").upper() or "RUN"
            elapsed = data.get("stage_elapsed_h")
            min_h = data.get("stage_min_h")
            stage_idx_str = f"Stage {stage_idx}" if stage_idx is not None else "Stage ?"
            self.stage_title.text = f"{stage_idx_str} - {stage_name} [{stage_type}]"
            elapsed_str = "elapsed --" if elapsed is None else f"elapsed {elapsed:.1f} h"
            min_str = "" if min_h is None else f" / min {min_h:.0f} h"
            schedule = data.get("schedule_name") or ""
            sched_str = f" - {schedule}" if schedule else ""
            self.stage_subtitle.text = f"{elapsed_str}{min_str}{sched_str}"
        elif data.get("cooldown"):
            self.stage_title.text = "Cooldown"
            self.stage_subtitle.text = ""
        else:
            self.stage_title.text = "No run active"
            self.stage_subtitle.text = ""

        # Lumber zone
        target_temp = data.get("target_temp_c")
        target_rh = data.get("target_rh_pct")
        self.lumber_widgets["temp_value"].text = "T " + _fmt_temp(data.get("temp_lumber"))
        self.lumber_widgets["temp_target"].text = _fmt_target(target_temp, "C")
        self.lumber_widgets["rh_value"].text = "RH " + _fmt_rh(data.get("rh_lumber"))
        self.lumber_widgets["rh_target"].text = _fmt_target(target_rh, "%")

        # Intake (no targets)
        self.intake_widgets["temp_value"].text = "T " + _fmt_temp(data.get("temp_intake"))
        self.intake_widgets["temp_target"].text = ""
        self.intake_widgets["rh_value"].text = "RH " + _fmt_rh(data.get("rh_intake"))
        self.intake_widgets["rh_target"].text = ""

        # Moisture content
        mc1 = data.get("mc_channel_1")
        mc2 = data.get("mc_channel_2")
        r1 = data.get("mc_resistance_1")
        r2 = data.get("mc_resistance_2")
        self.mc1_value.text = "Ch1: " + (_fmt_mc(mc1) if mc1 is not None else "Probe fault")
        self.mc1_detail.text = _fmt_ohms(r1)
        self.mc2_value.text = "Ch2: " + (_fmt_mc(mc2) if mc2 is not None else "Probe fault")
        self.mc2_detail.text = _fmt_ohms(r2)
        target_mc = data.get("target_mc_pct")
        if target_mc is not None and data.get("run_active"):
            self.mc_target.text = f"Target MC: {target_mc:.1f} %"
        else:
            self.mc_target.text = ""

        # Equipment
        heater_on = bool(data.get("heater_on"))
        self.eq_heater.text = "Heater: ON" if heater_on else "Heater: OFF"
        vent_open = bool(data.get("vent_open"))
        self.eq_vents.text = "Vents: OPEN" if vent_open else "Vents: CLOSED"
        ex_pct = data.get("exhaust_fan_pct") or 0
        ex_rpm = data.get("exhaust_fan_rpm")
        if ex_pct > 0:
            rpm_str = f", {ex_rpm} rpm" if ex_rpm is not None else ""
            self.eq_exhaust.text = f"Exhaust fan: ON {ex_pct}%{rpm_str}"
        else:
            self.eq_exhaust.text = "Exhaust fan: OFF"
        circ_on = bool(data.get("circ_fan_on"))
        circ_pct = data.get("circ_fan_pct")
        if circ_on:
            pct_str = f" {circ_pct}%" if circ_pct is not None else ""
            self.eq_circ.text = f"Circulation fans: ON{pct_str}"
        else:
            self.eq_circ.text = "Circulation fans: OFF"

        # Rails
        self.rail_12v.text = "12V: " + _fmt_ma(data.get("current_12v_ma"))
        self.rail_5v.text = "5V: " + _fmt_ma(data.get("current_5v_ma"))

        # Anything we just rendered is fresh - clear any greying
        self._set_stale(False)

    # ---- footer + stale handling ------------------------------------------

    def _on_tick(self, _dt: float) -> None:
        self._update_footer()

    def _is_stale(self) -> bool:
        if self._last_data_at is None:
            return True
        if self._current_mode == MODE_OFFLINE:
            return True
        age = time.monotonic() - self._last_data_at
        return age > STALE_MULTIPLIER * self._refresh_interval()

    def _set_stale(self, stale: bool) -> None:
        # Lightweight visual: tint all the value labels into TEXT_MUTED.
        target_color = theme.TEXT_MUTED if stale else theme.TEXT_PRIMARY
        for lbl in (
            self.stage_title,
            self.lumber_widgets["temp_value"],
            self.lumber_widgets["rh_value"],
            self.intake_widgets["temp_value"],
            self.intake_widgets["rh_value"],
            self.mc1_value,
            self.mc2_value,
        ):
            lbl.color = target_color

    def _update_footer(self) -> None:
        stale = self._is_stale()
        self._set_stale(stale)
        if self._last_data_at is None:
            if self._current_mode == MODE_OFFLINE:
                self.footer_label.text = "Offline - waiting for connection."
            else:
                self.footer_label.text = "Waiting for first response..."
            return
        age = time.monotonic() - self._last_data_at
        prefix = "Last LoRa packet" if self._current_mode == MODE_COTTAGE else "Updated"
        if self._current_mode == MODE_OFFLINE:
            self.footer_label.text = f"Offline. {prefix}: {_fmt_age(age)}"
        elif stale:
            self.footer_label.text = f"Stale data. {prefix}: {_fmt_age(age)}"
        else:
            self.footer_label.text = f"{prefix}: {_fmt_age(age)}"
