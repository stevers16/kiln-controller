"""Settings screen.

Phase 2 covers the Connection and Authentication sections of the spec, plus
the Test Connection buttons. RTC sync, daemon info, and About are added in
later phases when the dependent features land.
"""

from __future__ import annotations

from typing import Callable, Optional

from kivy.graphics import Color, Rectangle
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.screenmanager import Screen

from kilnapp import theme
from kilnapp.api.autodetect import DetectResult, MODE_DIRECT, MODE_OFFLINE, MODE_STA
from kilnapp.api.client import (
    AuthError,
    KilnApiClient,
    PROBE_TIMEOUT_S,
    TimeoutError_,
    call_async,
)
from kilnapp.connection import ConnectionManager
from kilnapp.storage import (
    OVERRIDE_AUTO,
    OVERRIDE_COTTAGE,
    OVERRIDE_DIRECT,
    OVERRIDE_STA,
    Settings,
)
from kilnapp.widgets.form import row, spinner, text_input


_OVERRIDE_LABELS = {
    OVERRIDE_AUTO: "Auto-detect",
    OVERRIDE_DIRECT: "Force Pico AP",
    OVERRIDE_STA: "Force Pico STA",
    OVERRIDE_COTTAGE: "Force Cottage (Pi4)",
}
_OVERRIDE_REVERSE = {v: k for k, v in _OVERRIDE_LABELS.items()}


def _section_header(text: str) -> Label:
    lbl = Label(
        text=text,
        color=theme.TEXT_PRIMARY,
        font_size="16sp",
        bold=True,
        size_hint_y=None,
        height=32,
        halign="left",
        valign="bottom",
    )
    lbl.bind(size=lambda w, s: setattr(w, "text_size", s))
    return lbl


def _button(text: str, on_press) -> Button:
    btn = Button(
        text=text,
        size_hint_y=None,
        height=40,
        font_size="14sp",
        background_color=(0.30, 0.55, 0.85, 1),
        color=(1, 1, 1, 1),
    )
    btn.bind(on_release=lambda _b: on_press())
    return btn


class SettingsScreen(Screen):
    def __init__(
        self,
        connection: ConnectionManager,
        on_navigate: Optional[Callable[[str], None]] = None,
        **kwargs,
    ):
        super().__init__(name="settings", **kwargs)
        self.connection = connection
        self._on_navigate = on_navigate
        self._current_mode: str = MODE_OFFLINE
        self.connection.add_listener(self._on_connection_change)

        # Background
        with self.canvas.before:
            self._bg_color = Color(*theme.BG_DARK)
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(
            pos=lambda w, v: setattr(self._bg_rect, "pos", v),
            size=lambda w, v: setattr(self._bg_rect, "size", v),
        )

        scroll = ScrollView(do_scroll_x=False, do_scroll_y=True)
        form = BoxLayout(
            orientation="vertical",
            padding=(16, 16, 16, 16),
            spacing=8,
            size_hint_y=None,
        )
        form.bind(minimum_height=form.setter("height"))

        s = self.connection.settings

        # ---- Connection section -------------------------------------------
        form.add_widget(_section_header("Connection"))

        self.f_pico_ip = text_input(s.pico_ip)
        form.add_widget(row("Pico AP IP", self.f_pico_ip))

        self.f_pico_port = text_input(str(s.pico_port), input_filter="int")
        form.add_widget(row("Pico AP port", self.f_pico_port))

        self.f_pico_sta = text_input(s.pico_sta_ip)
        form.add_widget(row("Pico STA IP", self.f_pico_sta))

        self.f_pi4_ip = text_input(s.pi4_ip, hint="e.g. 10.0.0.50")
        form.add_widget(row("Pi4 IP", self.f_pi4_ip))

        self.f_pi4_port = text_input(str(s.pi4_port), input_filter="int")
        form.add_widget(row("Pi4 port", self.f_pi4_port))

        self.f_override = spinner(
            values=list(_OVERRIDE_LABELS.values()),
            initial=_OVERRIDE_LABELS.get(s.connection_override, _OVERRIDE_LABELS[OVERRIDE_AUTO]),
        )
        form.add_widget(row("Mode", self.f_override))

        # Test connection buttons
        test_box = BoxLayout(orientation="horizontal", size_hint_y=None, height=40, spacing=8)
        test_box.add_widget(_button("Test Pico", self._test_pico))
        test_box.add_widget(_button("Test Pi4", self._test_pi4))
        form.add_widget(test_box)

        # ---- Authentication section ---------------------------------------
        form.add_widget(_section_header("Authentication"))

        self.f_api_key = text_input(s.api_key, password=True, hint="Pico X-Kiln-Key")
        form.add_widget(row("API key", self.f_api_key))

        show_box = BoxLayout(orientation="horizontal", size_hint_y=None, height=36, spacing=8)
        show_box.add_widget(_button("Show / hide key", self._toggle_show_key))
        form.add_widget(show_box)

        # ---- AP-only tools -----------------------------------------------
        form.add_widget(_section_header("Tools (Direct only)"))
        self.schedules_btn = _button("Schedules", self._goto_schedules)
        form.add_widget(self.schedules_btn)
        self.system_test_btn = _button("System Test", self._goto_system_test)
        form.add_widget(self.system_test_btn)
        self.logs_btn = _button("Logs", self._goto_logs)
        form.add_widget(self.logs_btn)
        self.calibration_btn = _button("Moisture Calibration", self._goto_calibration)
        form.add_widget(self.calibration_btn)
        self._apply_tools_gate()

        # ---- Save + status -----------------------------------------------
        form.add_widget(_section_header(""))
        form.add_widget(_button("Save and reconnect", self._save))

        self.status_label = Label(
            text="",
            color=theme.TEXT_SECONDARY,
            font_size="13sp",
            size_hint_y=None,
            height=120,
            halign="left",
            valign="top",
        )
        self.status_label.bind(size=lambda w, s: setattr(w, "text_size", s))
        form.add_widget(self.status_label)

        scroll.add_widget(form)
        self.add_widget(scroll)

    # ---- AP-only tools gating ---------------------------------------------

    def _on_connection_change(self, result: DetectResult) -> None:
        self._current_mode = result.mode
        # Tool buttons don't exist until build() has run. Guard for the
        # race where the connection manager fires its first detect
        # before the widget tree is finished wiring up.
        if (
            hasattr(self, "schedules_btn")
            and hasattr(self, "system_test_btn")
            and hasattr(self, "logs_btn")
            and hasattr(self, "calibration_btn")
        ):
            self._apply_tools_gate()

    def _apply_tools_gate(self) -> None:
        direct = self._current_mode in (MODE_DIRECT, MODE_STA)
        for btn in (
            self.schedules_btn,
            self.system_test_btn,
            self.logs_btn,
            self.calibration_btn,
        ):
            btn.disabled = not direct
            btn.opacity = 1.0 if direct else 0.5

    def _goto_schedules(self) -> None:
        if self._current_mode not in (MODE_DIRECT, MODE_STA):
            return
        if self._on_navigate is not None:
            self._on_navigate("schedules")

    def _goto_system_test(self) -> None:
        if self._current_mode not in (MODE_DIRECT, MODE_STA):
            return
        if self._on_navigate is not None:
            self._on_navigate("system_test")

    def _goto_logs(self) -> None:
        if self._current_mode not in (MODE_DIRECT, MODE_STA):
            return
        if self._on_navigate is not None:
            self._on_navigate("logs")

    def _goto_calibration(self) -> None:
        if self._current_mode not in (MODE_DIRECT, MODE_STA):
            return
        if self._on_navigate is not None:
            self._on_navigate("calibration")

    # ---- helpers -----------------------------------------------------------

    def _collect(self) -> Settings:
        override = _OVERRIDE_REVERSE.get(self.f_override.text, OVERRIDE_AUTO)
        return Settings(
            pico_ip=self.f_pico_ip.text,
            pico_port=int(self.f_pico_port.text or "80"),
            pico_sta_ip=self.f_pico_sta.text,
            pi4_ip=self.f_pi4_ip.text,
            pi4_port=int(self.f_pi4_port.text or "8080"),
            api_key=self.f_api_key.text,
            connection_override=override,
            last_rtc_sync=self.connection.settings.last_rtc_sync,
            auto_sync_rtc=self.connection.settings.auto_sync_rtc,
        ).normalised()

    def _set_status(self, text: str) -> None:
        self.status_label.text = text

    def _toggle_show_key(self) -> None:
        self.f_api_key.password = not self.f_api_key.password

    def _save(self) -> None:
        self._set_status("Saving and reconnecting...")
        self.connection.update_settings(self._collect())

    # ---- test connection buttons ------------------------------------------

    def _test_pico(self) -> None:
        """Probe the Pico AP IP first, then the Pico STA IP, mirroring how
        autodetect treats both as 'Direct' (Pico) endpoints. Reports which
        one answered, plus an API-key check via /status.
        """
        s = self._collect()
        targets = []
        if s.pico_ip:
            targets.append(("AP", f"http://{s.pico_ip}:{s.pico_port}"))
        if s.pico_sta_ip:
            targets.append(("STA", f"http://{s.pico_sta_ip}:{s.pico_port}"))
        if not targets:
            self._set_status("Pico AP IP and Pico STA IP are both empty.")
            return

        target_summary = ", ".join(f"{name} ({url})" for name, url in targets)
        self._set_status(f"Testing Pico: {target_summary} ...")

        client = KilnApiClient()
        client.config.api_key = s.api_key
        client.config.requires_auth = True
        api_key = s.api_key

        def work():
            attempts = []  # list of (name, url, status_text)
            winner = None  # (name, url) of the first endpoint that answered
            for name, url in targets:
                try:
                    client.health(base_url=url, timeout=PROBE_TIMEOUT_S)
                except Exception as e:
                    attempts.append((name, url, f"unreachable ({e})"))
                    continue
                # /health responded - check the API key against /status
                if api_key:
                    try:
                        client._get("/status", base_url=url, timeout=PROBE_TIMEOUT_S)
                        attempts.append((name, url, "OK; API key OK"))
                    except AuthError:
                        attempts.append(
                            (name, url, "OK; API key REJECTED (HTTP 401)")
                        )
                    except Exception as e:
                        attempts.append((name, url, f"OK; /status error: {e}"))
                else:
                    attempts.append(
                        (name, url, "OK (no API key set - skipped /status)")
                    )
                if winner is None:
                    winner = (name, url)
            return attempts, winner

        def done(result, err):
            if err is not None:
                self._set_status(f"Test failed: {err}")
                return
            attempts, winner = result
            lines = [f"{name}: {status}" for name, _url, status in attempts]
            if winner is not None:
                lines.insert(0, f"Pico reachable via {winner[0]} ({winner[1]}).")
            else:
                lines.insert(0, "Pico unreachable on all configured endpoints.")
            self._set_status("\n".join(lines))

        call_async(work, done)

    def _test_pi4(self) -> None:
        s = self._collect()
        if not s.pi4_ip:
            self._set_status("Pi4 IP is empty.")
            return
        base = f"http://{s.pi4_ip}:{s.pi4_port}"
        self._set_status(f"Testing Pi4 at {base} ...")
        client = KilnApiClient()
        client.config.requires_auth = False
        client.config.base_url = base

        def work():
            return client.health(base_url=base, timeout=PROBE_TIMEOUT_S)

        def done(result, err):
            if err is not None:
                self._set_status(f"Pi4 unreachable: {err}")
                return
            self._set_status("Pi4 OK. /health responded.")

        call_async(work, done)
