"""Dashboard screen.

Phase 3 added the read-only sensor / equipment / rails layout. Phase 4 layers
on:

- StageBanner with progress bar (elapsed vs min_duration_h)
- WaterPanBanner during equalizing/conditioning stages
- FaultBanner with tap-to-Alerts navigation, overrides water pan banner
- Action button row (Start / Stop / Advance) hidden in Cottage mode, gated
  by run state, with a confirmation dialog before each POST
- Deadband colour coding on Lumber temp/RH (green within +/- TEMP_DEADBAND_C
  / RH_DEADBAND_PCT, amber outside, red on sensor fault). Same scheme later
  for moisture once we have per-stage MC tolerances.
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
from kilnapp.alerts import split_alerts
from kilnapp.api.client import call_async
from kilnapp.connection import ConnectionManager
from kilnapp.widgets.banners import (
    FaultBanner,
    NoticeBanner,
    StageBanner,
    WaterPanBanner,
)
from kilnapp.widgets.cards import Panel, small_label, value_label
from kilnapp.widgets.dialog import confirm


REFRESH_AP_S = 10
REFRESH_COTTAGE_S = 35
STALE_MULTIPLIER = 3  # data older than N x refresh interval is greyed out

# Match lib/schedule.py constants
TEMP_DEADBAND_C = 2.0
RH_DEADBAND_PCT = 8.0

# Stage types that ask the user to consider water pans
EQUALIZING = "equalizing"
CONDITIONING = "conditioning"

EM_DASH = "--"


# ---- formatters -----------------------------------------------------------


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


def _deadband_color(
    actual: Optional[float],
    target: Optional[float],
    deadband: float,
    *,
    cap_only: bool = False,
) -> tuple:
    """Colour for a sensor value relative to a target.

    Default mode (cap_only=False) treats target as a setpoint: green within
    +/- deadband, amber outside, red on sensor fault. Used for temperature.

    cap_only=True treats target as a maximum allowable value: green if
    actual is below target+deadband (anything lower is fine, the kiln is
    drying ahead of plan), amber if above. Used for lumber RH (RH targets
    in FPL schedules are upper bounds, not setpoints).
    """
    if actual is None:
        return theme.SEVERITY_ERROR
    if target is None:
        return theme.TEXT_PRIMARY
    if cap_only:
        if actual <= target + deadband:
            return theme.SEVERITY_OK
        return theme.SEVERITY_WARN
    if abs(actual - target) <= deadband:
        return theme.SEVERITY_OK
    return theme.SEVERITY_WARN


def _mc_color(actual: Optional[float], target: Optional[float]) -> tuple:
    """Colour for an MC% reading: green when at/below target (drying done
    for this probe), amber when still above target, red on probe fault."""
    if actual is None:
        return theme.SEVERITY_ERROR
    if target is None:
        return theme.TEXT_PRIMARY
    if actual <= target:
        return theme.SEVERITY_OK
    return theme.SEVERITY_WARN


# ---- the screen -----------------------------------------------------------


class DashboardScreen(Screen):
    def __init__(
        self,
        connection: ConnectionManager,
        on_navigate=None,
        **kwargs,
    ):
        super().__init__(name="dashboard", **kwargs)
        self.connection = connection
        # `on_navigate(screen_name)` lets us push the user to other tabs
        # (e.g. tap fault banner -> Alerts).
        self._on_navigate = on_navigate

        self._refresh_event = None
        self._in_flight = False
        self._action_in_flight = False
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
            padding=(10, 8, 10, 8),
            spacing=6,
            size_hint_y=None,
        )
        content.bind(minimum_height=content.setter("height"))

        # Stage banner (always present)
        self.stage_banner = StageBanner()
        content.add_widget(self.stage_banner)

        # Advisory banner slot - exactly one (or none) of fault / notice /
        # water-pan is added to `content` at a time. Display priority:
        # fault > notice > water-pan.
        self.fault_banner = FaultBanner(on_tap=self._goto_alerts)
        self.notice_banner = NoticeBanner(on_tap=self._goto_alerts)
        self.water_pan_banner = WaterPanBanner()
        self._current_advisory = None  # widget currently in `content`
        self._content = content  # so _set_advisory can manipulate it

        # Sensor row: Lumber | Intake
        sensors = BoxLayout(orientation="horizontal", spacing=8, size_hint_y=None, height=104)
        self.lumber_panel, self.lumber_widgets = self._build_sensor_panel(
            "Lumber zone", show_targets=True, fill_height=True
        )
        self.intake_panel, self.intake_widgets = self._build_sensor_panel(
            "Intake", show_targets=False, fill_height=True
        )
        sensors.add_widget(self.lumber_panel)
        sensors.add_widget(self.intake_panel)
        content.add_widget(sensors)

        # Moisture content panel
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

        # Equipment state panel
        self.equipment_panel = Panel()
        self.equipment_panel.add_widget(small_label("Equipment", bold=True))
        self.eq_heater = small_label("Heater: " + EM_DASH)
        self.eq_vents = small_label("Vents: " + EM_DASH)
        self.eq_exhaust = small_label("Exhaust fan: " + EM_DASH)
        self.eq_circ = small_label("Circulation fans: " + EM_DASH)
        for w in (self.eq_heater, self.eq_vents, self.eq_exhaust, self.eq_circ):
            self.equipment_panel.add_widget(w)
        content.add_widget(self.equipment_panel)

        # Rail currents
        self.rails_panel = Panel()
        self.rails_panel.add_widget(small_label("Rails", bold=True))
        rails_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=22, spacing=12)
        self.rail_12v = small_label("12V: " + EM_DASH)
        self.rail_5v = small_label("5V: " + EM_DASH)
        rails_row.add_widget(self.rail_12v)
        rails_row.add_widget(self.rail_5v)
        self.rails_panel.add_widget(rails_row)
        content.add_widget(self.rails_panel)

        # Action button row (Start / Stop / Advance / Refresh). Hidden in
        # Cottage mode and dynamically rebuilt as run state changes. Refresh
        # is always present so the user can poke /status manually.
        self.actions_row = BoxLayout(
            orientation="horizontal",
            size_hint_y=None,
            height=38,
            spacing=6,
        )
        content.add_widget(self.actions_row)

        # Footer: last update line only (single line, no button)
        self.footer_label = small_label("Waiting for data...", size="11sp")
        self.footer_label.height = 18
        content.add_widget(self.footer_label)

        scroll.add_widget(content)
        self.add_widget(scroll)

        # Subscribe to connection changes
        self.connection.add_listener(self._on_connection_change)
        # 1 Hz tick for "Updated Xs ago" + stale state
        Clock.schedule_interval(self._on_tick, 1.0)

    # ---- panel construction helpers ---------------------------------------

    def _build_sensor_panel(self, title: str, *, show_targets: bool, fill_height: bool = False):
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
        self._refresh_action_row()
        if result.mode != MODE_OFFLINE:
            self.refresh_now()
        else:
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
        run_active = bool(data.get("run_active"))
        cooldown = bool(data.get("cooldown"))
        if run_active:
            self.stage_banner.show_run(
                stage_index=data.get("stage_index"),
                stage_name=data.get("stage_name") or "Unknown stage",
                stage_type=data.get("stage_type"),
                elapsed_h=data.get("stage_elapsed_h"),
                min_h=data.get("stage_min_h"),
                schedule_name=data.get("schedule_name"),
            )
        elif cooldown:
            self.stage_banner.show_cooldown()
        else:
            self.stage_banner.show_idle()

        # Advisory banner: fault > notice > water-pan. INFO-tier codes
        # (stage_advance, equalizing_start, ...) are silently filtered out.
        active_alerts = data.get("active_alerts") or []
        fault_details = data.get("fault_details") or []
        faults, notices = split_alerts(active_alerts, fault_details=fault_details)
        stage_type = (data.get("stage_type") or "").lower()
        if faults:
            self.fault_banner.set_alerts(faults)
            self._set_advisory(self.fault_banner)
        elif notices:
            self.notice_banner.set_alerts(notices)
            self._set_advisory(self.notice_banner)
        elif run_active and stage_type in (EQUALIZING, CONDITIONING):
            self._set_advisory(self.water_pan_banner)
        else:
            self._set_advisory(None)

        # Lumber zone with deadband colours
        target_temp = data.get("target_temp_c")
        target_rh = data.get("target_rh_pct")
        temp_l = data.get("temp_lumber")
        rh_l = data.get("rh_lumber")
        self.lumber_widgets["temp_value"].text = "T " + _fmt_temp(temp_l)
        self.lumber_widgets["temp_target"].text = _fmt_target(target_temp, "C")
        self.lumber_widgets["rh_value"].text = "RH " + _fmt_rh(rh_l)
        self.lumber_widgets["rh_target"].text = _fmt_target(target_rh, "%")
        if run_active:
            self.lumber_widgets["temp_value"].color = _deadband_color(
                temp_l, target_temp, TEMP_DEADBAND_C
            )
            # RH targets are upper bounds, not setpoints - anything at or
            # below target is fine.
            self.lumber_widgets["rh_value"].color = _deadband_color(
                rh_l, target_rh, RH_DEADBAND_PCT, cap_only=True
            )
        else:
            # No targets while idle - just signal sensor health
            self.lumber_widgets["temp_value"].color = (
                theme.SEVERITY_ERROR if temp_l is None else theme.TEXT_PRIMARY
            )
            self.lumber_widgets["rh_value"].color = (
                theme.SEVERITY_ERROR if rh_l is None else theme.TEXT_PRIMARY
            )

        # Intake (no targets)
        temp_i = data.get("temp_intake")
        rh_i = data.get("rh_intake")
        self.intake_widgets["temp_value"].text = "T " + _fmt_temp(temp_i)
        self.intake_widgets["temp_target"].text = ""
        self.intake_widgets["rh_value"].text = "RH " + _fmt_rh(rh_i)
        self.intake_widgets["rh_target"].text = ""
        self.intake_widgets["temp_value"].color = (
            theme.SEVERITY_ERROR if temp_i is None else theme.TEXT_PRIMARY
        )
        self.intake_widgets["rh_value"].color = (
            theme.SEVERITY_ERROR if rh_i is None else theme.TEXT_PRIMARY
        )

        # Moisture content
        mc1 = data.get("mc_channel_1")
        mc2 = data.get("mc_channel_2")
        r1 = data.get("mc_resistance_1")
        r2 = data.get("mc_resistance_2")
        target_mc = data.get("target_mc_pct")
        self.mc1_value.text = "Ch1: " + (_fmt_mc(mc1) if mc1 is not None else "Probe fault")
        self.mc1_detail.text = _fmt_ohms(r1)
        self.mc2_value.text = "Ch2: " + (_fmt_mc(mc2) if mc2 is not None else "Probe fault")
        self.mc2_detail.text = _fmt_ohms(r2)
        # MC colour coding: drying always reduces MC, so a stage is "done" for
        # this probe when actual <= target. Above target = still drying (amber);
        # at/below target = on track (green); None = probe fault (red). Only
        # apply when a drying stage is active and a target is set.
        if run_active and target_mc is not None:
            self.mc1_value.color = _mc_color(mc1, target_mc)
            self.mc2_value.color = _mc_color(mc2, target_mc)
            self.mc_target.text = f"Target MC: {target_mc:.1f} %"
        else:
            self.mc1_value.color = (
                theme.SEVERITY_ERROR if mc1 is None else theme.TEXT_PRIMARY
            )
            self.mc2_value.color = (
                theme.SEVERITY_ERROR if mc2 is None else theme.TEXT_PRIMARY
            )
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

        # Stale grey-out (note: deadband colours above are overwritten if
        # the data ages out)
        self._set_stale(False)

        # Action buttons depend on run state - rebuild now
        self._refresh_action_row()

    # ---- advisory banner slot ---------------------------------------------

    def _set_advisory(self, banner) -> None:
        """Replace the current advisory banner (or remove it). Insert it
        directly below the StageBanner so layout order stays consistent."""
        if self._current_advisory is banner:
            return
        if self._current_advisory is not None:
            self._content.remove_widget(self._current_advisory)
            self._current_advisory = None
        if banner is not None:
            # StageBanner is at index 0 from the top, but Kivy's BoxLayout
            # children list is reversed: last-added has index 0. We want
            # the advisory immediately *below* the stage banner visually,
            # which means just before it in add order. Use the explicit
            # `index` arg.
            children = self._content.children  # reversed (last added = index 0)
            # Stage banner was the first widget added -> highest index
            stage_idx = children.index(self.stage_banner)
            self._content.add_widget(banner, index=stage_idx)
            self._current_advisory = banner

    def _goto_alerts(self) -> None:
        if self._on_navigate is not None:
            self._on_navigate("alerts")

    # ---- action buttons (Start / Stop / Advance) --------------------------

    def _is_direct_mode(self) -> bool:
        return self._current_mode in (MODE_DIRECT, MODE_STA)

    def _refresh_action_row(self) -> None:
        """Rebuild the action button row based on current mode + run state.

        Button matrix (Direct/STA mode only - row hidden in Cottage):
        - run_active                    : Stop Run [+ Advance Stage if past min]
        - cooldown && not run_active    : Shutdown
        - idle (neither)                : Start Run
        Refresh is always present and pinned to the right.
        """
        self.actions_row.clear_widgets()
        self.actions_row.height = 38

        data = self._last_data or {}
        run_active = bool(data.get("run_active"))
        cooldown = bool(data.get("cooldown"))
        direct = self._is_direct_mode()

        if direct:
            if run_active:
                stop_btn = self._action_button("Stop Run", danger=True)
                stop_btn.bind(on_release=lambda _b: self._on_stop_pressed())
                self.actions_row.add_widget(stop_btn)

                # Advance button - only if past min_duration. The Pico's
                # /run/advance endpoint does NOT enforce min duration server
                # side, so the client must gate it.
                elapsed_h = data.get("stage_elapsed_h")
                min_h = data.get("stage_min_h")
                if elapsed_h is not None and min_h is not None and elapsed_h >= min_h:
                    adv_btn = self._action_button("Advance Stage")
                    adv_btn.bind(on_release=lambda _b: self._on_advance_pressed())
                    self.actions_row.add_widget(adv_btn)
            elif cooldown:
                shutdown_btn = self._action_button("Shutdown", danger=True)
                shutdown_btn.bind(on_release=lambda _b: self._on_shutdown_pressed())
                self.actions_row.add_widget(shutdown_btn)
            else:
                start_btn = self._action_button("Start Run")
                start_btn.bind(on_release=lambda _b: self._on_start_pressed())
                self.actions_row.add_widget(start_btn)

        # Refresh is always present, and pinned to the right
        refresh_btn = self._action_button("Refresh")
        refresh_btn.size_hint_x = None
        refresh_btn.width = 90
        refresh_btn.bind(on_release=lambda _b: self.refresh_now())
        self.actions_row.add_widget(refresh_btn)

    def _action_button(self, text: str, *, danger: bool = False) -> Button:
        return Button(
            text=text,
            font_size="14sp",
            background_color=(0.85, 0.30, 0.30, 1) if danger else (0.30, 0.55, 0.85, 1),
            color=(1, 1, 1, 1),
        )

    def _on_start_pressed(self) -> None:
        # Open the three-step Start Run wizard (AP-only screen). The wizard
        # handles schedule pick, run label, checklist, and the actual POST;
        # it calls on_finish() to return us here.
        if self._on_navigate is not None:
            self._on_navigate("start_run")

    def _on_stop_pressed(self) -> None:
        confirm(
            "Stop Run",
            "Stop the active drying run? The heater will be turned off "
            "and the kiln will return to idle.",
            on_confirm=self._do_stop,
            confirm_text="Stop Run",
            danger=True,
        )

    def _on_advance_pressed(self) -> None:
        confirm(
            "Advance Stage",
            "Advance the run to the next stage now?",
            on_confirm=self._do_advance,
            confirm_text="Advance",
        )

    def _on_shutdown_pressed(self) -> None:
        confirm(
            "Shutdown Kiln",
            "End cooldown and put the kiln fully off? "
            "Heater off, fans off, vents closed.",
            on_confirm=self._do_shutdown,
            confirm_text="Shutdown",
            danger=True,
        )

    def _do_stop(self) -> None:
        self._do_action(
            lambda: self.connection.client.run_stop("manual"),
            success_msg="Run stopped.",
        )

    def _do_advance(self) -> None:
        self._do_action(
            lambda: self.connection.client.run_advance(),
            success_msg="Stage advanced.",
        )

    def _do_shutdown(self) -> None:
        self._do_action(
            lambda: self.connection.client.run_shutdown(),
            success_msg="Kiln shut down.",
        )

    def _do_action(self, func, success_msg: str) -> None:
        if self._action_in_flight:
            return
        self._action_in_flight = True
        self.footer_label.text = "Sending command..."

        def done(result, err):
            self._action_in_flight = False
            if err is not None:
                self.footer_label.text = f"Command failed: {err}"
                return
            self.footer_label.text = success_msg
            # Force an immediate status refresh so the UI reflects the change
            self.refresh_now()

        call_async(func, done)

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
        # Lightweight visual: tint the headline labels into TEXT_MUTED.
        if stale:
            for lbl in (
                self.lumber_widgets["temp_value"],
                self.lumber_widgets["rh_value"],
                self.intake_widgets["temp_value"],
                self.intake_widgets["rh_value"],
                self.mc1_value,
                self.mc2_value,
            ):
                lbl.color = theme.TEXT_MUTED

    def _update_footer(self) -> None:
        stale = self._is_stale()
        if stale:
            self._set_stale(True)
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
