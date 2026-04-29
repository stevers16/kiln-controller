"""Module Upload screen (AP/STA only).

Phase 13: upload .py or schedule .json files from the local filesystem
to the Pico. Covers the two firmware upload endpoints:

  - .py files  -> PUT /modules/{path}     (Pico reboots on success)
  - .json files -> PUT /schedules/{name}  (no reboot; strict schedule schema)

Per firmware `handle_module_upload` (main.py), only `main.py` (root) and
`lib/*.py` are accepted for module uploads. The client pre-fills the
target path from the picked filename and special-cases `main.py` so the
user sees an explicit "replaces entry point" warning before they upload.

Schedule JSON uploads route to `/schedules/{filename}`. The firmware
validates the full schedule schema (name/species/stages plus per-stage
fields), so a valid schedule JSON is required - a bare JSON file won't
be accepted. This is the same endpoint the Schedules editor already uses.

Reconnect flow
--------------
After a .py upload the firmware responds with `{rebooting: true}` and
calls machine.reset() ~1s later. The screen schedules up to 30s of
`/health` polls (3s interval) so the user sees when the Pico comes back.
JSON uploads don't trigger a reboot.

Notes on progress
-----------------
The spec calls for a bytes-sent-of-total progress bar. Modules are
small (tens of KB), so a single `requests.put()` call without chunked
upload is fast enough that a busy progress animation is more honest
than a progress bar. A "Uploading..." status line plus spinner-style
elapsed counter is what the user gets.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from kivy.clock import Clock
from kivy.graphics import Color, Rectangle
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.progressbar import ProgressBar
from kivy.uix.scrollview import ScrollView
from kivy.uix.screenmanager import Screen

from kilnapp import theme
from kilnapp.api.autodetect import DetectResult, MODE_OFFLINE, is_direct_mode
from kilnapp.api.client import call_async
from kilnapp.connection import ConnectionManager
from kilnapp.format import format_size
from kilnapp.widgets.cards import Panel, small_label, value_label
from kilnapp.widgets.dialog import confirm
from kilnapp.widgets.form import text_input


_WARNING_TEXT = (
    "Uploading a broken module may render the kiln inoperable\n"
    "and require USB recovery via mpremote. Keep a USB cable\n"
    "accessible. The Pico will reboot after a .py upload."
)

_MAIN_PY_WARNING = (
    "main.py replaces the entry point. A syntax error will\n"
    "prevent the kiln from booting."
)

_MAX_BYTES = 512 * 1024  # matches firmware cap
_RECONNECT_POLL_S = 3.0
_RECONNECT_TIMEOUT_S = 30.0


def _target_for_filename(fname: str) -> str:
    """Spec: .py -> lib/<fname> unless the name is main.py; .json -> schedules/<fname>."""
    if not fname:
        return ""
    name = Path(fname).name  # drop any directory component from the pick
    low = name.lower()
    if low == "main.py":
        return "main.py"
    if low.endswith(".py"):
        return f"lib/{name}"
    if low.endswith(".json"):
        return f"schedules/{name}"
    return name  # leave as-is; the "unsupported type" check will catch it


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


class _FilePickerPopup(Popup):
    """Standard Kivy FileChooser inside a Popup. On Android the Phase 15
    Buildozer pass will switch this to plyer/filechooser; desktop uses
    the built-in widget which is reliable on Windows/macOS/Linux.
    """

    def __init__(self, on_select: Callable[[str], None], **kwargs):
        body = BoxLayout(orientation="vertical", spacing=6, padding=6)
        # Only show .py / .json in the picker - filters are case-sensitive
        # on the FileChooser, so list both cases.
        self._chooser = FileChooserListView(
            filters=["*.py", "*.json"],
            size_hint_y=1,
        )
        body.add_widget(self._chooser)

        btn_row = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=40, spacing=8
        )
        cancel_btn = Button(
            text="Cancel",
            font_size="13sp",
            background_color=(0.40, 0.42, 0.48, 1),
            color=(1, 1, 1, 1),
        )
        cancel_btn.bind(on_release=lambda _b: self.dismiss())
        btn_row.add_widget(cancel_btn)
        select_btn = Button(
            text="Select",
            font_size="13sp",
            background_color=(0.30, 0.55, 0.85, 1),
            color=(1, 1, 1, 1),
        )

        def _confirm(_b):
            sel = self._chooser.selection
            if not sel:
                return
            self.dismiss()
            on_select(sel[0])

        select_btn.bind(on_release=_confirm)
        btn_row.add_widget(select_btn)
        body.add_widget(btn_row)

        super().__init__(
            title="Choose a .py or .json file",
            content=body,
            size_hint=(0.95, 0.9),
            auto_dismiss=True,
            title_size="14sp",
            **kwargs,
        )


class ModuleUploadScreen(Screen):
    """AP/STA-only module/schedule uploader."""

    def __init__(
        self,
        connection: ConnectionManager,
        on_finish: Optional[Callable[[], None]] = None,
        **kwargs,
    ):
        super().__init__(name="module_upload", **kwargs)
        self.connection = connection
        self._on_finish = on_finish
        self._current_mode: str = MODE_OFFLINE
        self._selected_path: Optional[Path] = None
        self._uploading = False
        self._reconnect_event = None
        self._reconnect_start: float = 0.0
        self._upload_start: float = 0.0

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
        title = value_label("Module Upload", size="16sp")
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

        # Scrollable content
        scroll = ScrollView(do_scroll_x=False, do_scroll_y=True)
        content = BoxLayout(
            orientation="vertical",
            size_hint_y=None,
            spacing=6,
            padding=(0, 0, 0, 8),
        )
        content.bind(minimum_height=content.setter("height"))

        # Warning banner - always visible per spec
        warn_panel = Panel()
        warn_panel.padding = (10, 8, 10, 8)
        with warn_panel.canvas.before:
            Color(0.20, 0.05, 0.05, 1)
            warn_panel._warn_bg = Rectangle(pos=warn_panel.pos, size=warn_panel.size)
        warn_panel.bind(
            pos=lambda w, v: setattr(w._warn_bg, "pos", v),
            size=lambda w, v: setattr(w._warn_bg, "size", v),
        )
        warn_title = value_label("WARNING", size="14sp")
        warn_title.color = theme.SEVERITY_ERROR
        warn_panel.add_widget(warn_title)
        warn_body = Label(
            text=_WARNING_TEXT,
            color=theme.TEXT_PRIMARY,
            font_size="12sp",
            halign="left",
            valign="top",
            size_hint_y=None,
        )
        warn_body.bind(
            size=lambda w, s: setattr(w, "text_size", (s[0], None)),
            texture_size=lambda w, s: setattr(w, "height", max(40, s[1] + 4)),
        )
        warn_panel.add_widget(warn_body)
        content.add_widget(warn_panel)

        # File picker + selected file info
        picker_panel = Panel()
        picker_panel.padding = (10, 8, 10, 8)
        picker_panel.add_widget(value_label("Source file", size="14sp"))

        pick_row = BoxLayout(
            orientation="horizontal",
            size_hint_y=None,
            height=40,
            spacing=6,
        )
        self.pick_btn = _primary_button("Browse...", self._open_picker)
        pick_row.add_widget(self.pick_btn)
        picker_panel.add_widget(pick_row)

        self.selected_lbl = small_label("No file selected")
        picker_panel.add_widget(self.selected_lbl)
        self.selected_size_lbl = small_label("")
        picker_panel.add_widget(self.selected_size_lbl)
        content.add_widget(picker_panel)

        # Target path
        target_panel = Panel()
        target_panel.padding = (10, 8, 10, 8)
        target_panel.add_widget(value_label("Target path on Pico", size="14sp"))
        target_panel.add_widget(
            small_label(
                ".py -> lib/<name> (or main.py)  |  .json -> schedules/<name>"
            )
        )
        self.target_input = text_input("", hint="e.g. lib/exhaust.py")
        target_panel.add_widget(self.target_input)

        # main.py extra warning (hidden until triggered)
        self.main_warning = Label(
            text=_MAIN_PY_WARNING,
            color=theme.SEVERITY_ERROR,
            font_size="11sp",
            halign="left",
            valign="top",
            size_hint_y=None,
            height=0,
            opacity=0.0,
        )
        self.main_warning.bind(
            size=lambda w, s: setattr(w, "text_size", (s[0], None)),
        )
        target_panel.add_widget(self.main_warning)
        content.add_widget(target_panel)

        # Upload row
        upload_row = BoxLayout(
            orientation="horizontal",
            size_hint_y=None,
            height=40,
            spacing=6,
        )
        self.upload_btn = _primary_button("Upload", self._on_upload_pressed)
        upload_row.add_widget(self.upload_btn)
        content.add_widget(upload_row)

        # Status + progress
        self.status_lbl = Label(
            text="",
            color=theme.TEXT_SECONDARY,
            font_size="12sp",
            size_hint_y=None,
            height=22,
            halign="left",
            valign="middle",
        )
        self.status_lbl.bind(size=lambda w, s: setattr(w, "text_size", s))
        content.add_widget(self.status_lbl)

        self.progress = ProgressBar(
            max=1.0, value=0.0, size_hint_y=None, height=8
        )
        content.add_widget(self.progress)

        # Installed modules list (refreshes on demand + after upload)
        installed_panel = Panel()
        installed_panel.padding = (10, 8, 10, 8)
        hdr = BoxLayout(
            orientation="horizontal",
            size_hint_y=None,
            height=24,
            spacing=6,
        )
        hdr.add_widget(value_label("Installed modules", size="14sp"))
        self.refresh_btn = Button(
            text="Refresh",
            size_hint_x=None,
            width=80,
            size_hint_y=None,
            height=24,
            font_size="11sp",
            background_color=(0.30, 0.55, 0.85, 1),
            color=(1, 1, 1, 1),
        )
        self.refresh_btn.bind(on_release=lambda _b: self._refresh_modules())
        hdr.add_widget(self.refresh_btn)
        installed_panel.add_widget(hdr)
        self.modules_box = BoxLayout(
            orientation="vertical",
            size_hint_y=None,
            spacing=2,
        )
        self.modules_box.bind(minimum_height=self.modules_box.setter("height"))
        installed_panel.add_widget(self.modules_box)
        content.add_widget(installed_panel)

        scroll.add_widget(content)
        root.add_widget(scroll)
        self.add_widget(root)

        # React to target text changes so the main.py warning updates live.
        self.target_input.bind(text=lambda _i, _v: self._update_main_warning())
        self.connection.add_listener(self._on_connection_change)

    # ---- lifecycle --------------------------------------------------------

    def on_pre_enter(self, *args):
        # Stop any stale reconnect timer and reset progress UI.
        self._cancel_reconnect()
        self.progress.value = 0.0
        # Clear status only when not mid-upload (a user who somehow
        # re-enters mid-stream shouldn't lose the current state).
        if not self._uploading:
            self.status_lbl.text = ""
        self._refresh_modules()

    def _back(self) -> None:
        if self._uploading:
            self.status_lbl.text = "Please wait for the upload to finish."
            return
        self._cancel_reconnect()
        if self._on_finish is not None:
            self._on_finish()

    def _on_connection_change(self, result: DetectResult) -> None:
        self._current_mode = result.mode
        direct = is_direct_mode(result.mode)
        self.pick_btn.disabled = not direct
        self.pick_btn.opacity = 1.0 if direct else 0.5
        self.upload_btn.disabled = not direct or self._uploading
        self.upload_btn.opacity = (
            1.0 if (direct and not self._uploading) else 0.5
        )
        self.refresh_btn.disabled = not direct
        if direct and not self._uploading:
            self._refresh_modules()

    # ---- picker -----------------------------------------------------------

    def _open_picker(self) -> None:
        if not is_direct_mode(self._current_mode):
            return
        popup = _FilePickerPopup(on_select=self._on_file_picked)
        popup.open()

    def _on_file_picked(self, path_str: str) -> None:
        p = Path(path_str)
        ext = p.suffix.lower()
        if ext not in (".py", ".json"):
            self.selected_lbl.text = f"Rejected: {p.name} (only .py / .json)"
            self.selected_lbl.color = theme.SEVERITY_ERROR
            self.selected_size_lbl.text = ""
            self._selected_path = None
            self.target_input.text = ""
            self._update_main_warning()
            return
        try:
            size = p.stat().st_size
        except Exception as e:
            self.selected_lbl.text = f"Cannot read: {e}"
            self.selected_lbl.color = theme.SEVERITY_ERROR
            self.selected_size_lbl.text = ""
            self._selected_path = None
            return
        if size > _MAX_BYTES:
            self.selected_lbl.text = (
                f"Rejected: {p.name} is {format_size(size)}, max {format_size(_MAX_BYTES)}"
            )
            self.selected_lbl.color = theme.SEVERITY_ERROR
            self.selected_size_lbl.text = ""
            self._selected_path = None
            return

        self._selected_path = p
        self.selected_lbl.color = theme.TEXT_SECONDARY
        self.selected_lbl.text = f"Selected: {p.name}"
        self.selected_size_lbl.text = f"Size: {format_size(size)}"
        self.target_input.text = _target_for_filename(p.name)
        self._update_main_warning()
        self.status_lbl.text = ""

    def _update_main_warning(self) -> None:
        target = (self.target_input.text or "").strip().lstrip("/")
        if target == "main.py":
            self.main_warning.opacity = 1.0
            self.main_warning.height = 40
        else:
            self.main_warning.opacity = 0.0
            self.main_warning.height = 0

    # ---- upload -----------------------------------------------------------

    def _on_upload_pressed(self) -> None:
        if self._uploading:
            return
        if not is_direct_mode(self._current_mode):
            return
        if self._selected_path is None:
            self.status_lbl.text = "Pick a file first."
            return
        target = (self.target_input.text or "").strip().lstrip("/")
        if not target:
            self.status_lbl.text = "Target path cannot be empty."
            return
        low = target.lower()
        if low.endswith(".py"):
            if not (low == "main.py" or low.startswith("lib/")):
                self.status_lbl.text = (
                    "Python upload target must be main.py or lib/<name>.py"
                )
                return
            endpoint = "module"
        elif low.endswith(".json"):
            if not low.startswith("schedules/"):
                self.status_lbl.text = (
                    "Schedule target must be under schedules/<name>.json"
                )
                return
            endpoint = "schedule"
        else:
            self.status_lbl.text = "Target must end in .py or .json."
            return

        # Confirm before doing anything destructive.
        if endpoint == "module":
            if target == "main.py":
                msg = (
                    "Upload main.py?\n\nThis replaces the kiln's entry point.\n"
                    "A syntax error will prevent the kiln from booting.\n"
                    "The Pico will reboot after upload."
                )
                danger = True
            else:
                msg = (
                    f"Upload to {target}?\n\nThe Pico will reboot after\n"
                    "the write completes."
                )
                danger = False
        else:
            msg = f"Upload to {target}?\n\nNo reboot; schedule JSON is validated server-side."
            danger = False

        confirm(
            "Confirm upload",
            msg,
            on_confirm=lambda: self._do_upload(target, endpoint),
            confirm_text="Upload",
            danger=danger,
        )

    def _do_upload(self, target: str, endpoint: str) -> None:
        path = self._selected_path
        if path is None:
            return
        try:
            body = path.read_bytes()
        except Exception as e:
            self.status_lbl.text = f"Read failed: {e}"
            return
        if len(body) > _MAX_BYTES:
            self.status_lbl.text = (
                f"File grew to {format_size(len(body))}; exceeds {format_size(_MAX_BYTES)} cap."
            )
            return

        self._uploading = True
        self._upload_start = time.monotonic()
        self.upload_btn.disabled = True
        self.upload_btn.opacity = 0.5
        self.progress.value = 0.1  # indeterminate-ish - firmware writes in one shot
        self.status_lbl.text = f"Uploading {format_size(len(body))} to {target}..."

        client = self.connection.client

        def work():
            if endpoint == "module":
                return client.module_upload(target, body)
            # schedule - requires JSON-parsed body
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception as e:
                raise RuntimeError(f"Invalid JSON: {e}") from e
            filename = target.split("/", 1)[1]
            return client.schedule_put(filename, payload)

        def done(result, err):
            self._uploading = False
            self.upload_btn.disabled = not is_direct_mode(self._current_mode)
            self.upload_btn.opacity = 1.0 if not self.upload_btn.disabled else 0.5
            if err is not None:
                self.progress.value = 0.0
                self.status_lbl.text = f"Upload failed: {err}"
                return
            self.progress.value = 1.0
            if endpoint == "module":
                self.status_lbl.text = (
                    f"Upload complete. Pico rebooting... (waiting up to {int(_RECONNECT_TIMEOUT_S)}s)"
                )
                self._start_reconnect_watch()
            else:
                stages = 0
                if isinstance(result, dict):
                    stages = int(result.get("stage_count") or 0)
                self.status_lbl.text = (
                    f"Schedule saved"
                    + (f" ({stages} stages)." if stages else ".")
                )
                self._refresh_modules()

        call_async(work, done)

    # ---- reconnect watch --------------------------------------------------

    def _start_reconnect_watch(self) -> None:
        self._cancel_reconnect()
        self._reconnect_start = time.monotonic()
        # First probe after a short delay so the Pico has a chance to reboot.
        self._reconnect_event = Clock.schedule_interval(
            lambda _dt: self._tick_reconnect(), _RECONNECT_POLL_S
        )

    def _cancel_reconnect(self) -> None:
        if self._reconnect_event is not None:
            self._reconnect_event.cancel()
            self._reconnect_event = None

    def _tick_reconnect(self) -> None:
        elapsed = time.monotonic() - self._reconnect_start
        if elapsed >= _RECONNECT_TIMEOUT_S:
            self._cancel_reconnect()
            self.status_lbl.text = (
                "Pico did not respond within 30s. Verify it booted "
                "(LED + display) before uploading again."
            )
            return
        client = self.connection.client

        def work():
            return client.health_current()

        def done(result, err):
            if err is not None:
                # Still rebooting - next tick will try again.
                self.status_lbl.text = (
                    f"Waiting for Pico to come back... {int(elapsed)}s elapsed"
                )
                return
            self._cancel_reconnect()
            fw = ""
            if isinstance(result, dict):
                fw = str(result.get("firmware_version") or "")
            self.status_lbl.text = (
                f"Pico is back online" + (f" (firmware {fw})." if fw else ".")
            )
            # Nudge the connection manager so indicator + other screens
            # repick the reborn endpoint.
            self.connection.detect()
            self._refresh_modules()

        call_async(work, done)

    # ---- installed modules list ------------------------------------------

    def _refresh_modules(self) -> None:
        if not is_direct_mode(self._current_mode):
            self.modules_box.clear_widgets()
            self.modules_box.add_widget(
                small_label("Not connected to Pico.")
            )
            return
        client = self.connection.client
        if client.config.base_url is None:
            return

        def work():
            return client.modules_list()

        def done(result, err):
            self.modules_box.clear_widgets()
            if err is not None:
                self.modules_box.add_widget(
                    small_label(f"Load failed: {err}")
                )
                return
            modules: List[Dict[str, Any]] = []
            if isinstance(result, dict):
                modules = list(result.get("modules") or [])
            if not modules:
                self.modules_box.add_widget(small_label("No modules reported."))
                return
            for m in modules:
                path = str(m.get("path") or "?")
                size = int(m.get("size_bytes") or 0)
                mod = str(m.get("modified") or "")
                line = f"{path}   {format_size(size)}"
                if mod:
                    line += f"   {mod}"
                self.modules_box.add_widget(small_label(line))

        call_async(work, done)
