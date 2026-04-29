"""Schedules screen (AP/STA only).

Phase 9: list schedules on the Pico SD card. Per-row actions:
    View      - open the editor read-only (always allowed)
    Duplicate - copy with '_copy' suffix, open editor for the new copy
    Edit      - open the editor writable (disabled for built-ins)
    Delete    - confirm then DELETE /schedules/{filename} (disabled for built-ins)

Built-in filenames are protected server-side (Pico returns 403). The client
greys out Edit/Delete for them too so the user isn't handed a button that
will always fail.

Navigation into the editor happens via a direct method call on the
ScheduleEditorScreen instance (fetched from the ScreenManager) so we can
pass the schedule payload before switching screens.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

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
from kilnapp.format import format_size
from kilnapp.widgets.cards import Panel, small_label, value_label
from kilnapp.widgets.dialog import confirm


# ---- helpers ---------------------------------------------------------------


def _duplicate_filename(filename: str) -> str:
    """Turn 'maple_05in.json' into 'maple_05in_copy.json'. If the suffix
    is already present append a digit so Duplicate-twice doesn't collide."""
    stem = filename[:-5] if filename.endswith(".json") else filename
    if stem.endswith("_copy"):
        return f"{stem}2.json"
    if "_copy" in stem:
        # e.g. 'maple_copy2' -> 'maple_copy3'
        base, _, tail = stem.rpartition("_copy")
        try:
            n = int(tail) + 1
        except ValueError:
            n = 2
        return f"{base}_copy{n}.json"
    return f"{stem}_copy.json"


def _duplicate_name(name: str) -> str:
    return f"{name} (copy)" if name else "Schedule (copy)"


# ---- row widgets -----------------------------------------------------------


def _row_button(text: str, *, danger: bool = False, disabled: bool = False) -> Button:
    btn = Button(
        text=text,
        size_hint_x=None,
        width=72,
        size_hint_y=None,
        height=30,
        font_size="11sp",
        background_color=(0.85, 0.30, 0.30, 1) if danger else (0.30, 0.55, 0.85, 1),
        color=(1, 1, 1, 1),
    )
    if disabled:
        btn.disabled = True
        btn.opacity = 0.45
    return btn


class _BuiltinBadge(BoxLayout):
    """Small grey pill reading 'BUILT-IN'."""

    def __init__(self, **kwargs):
        super().__init__(orientation="vertical", **kwargs)
        self.size_hint = (None, None)
        self.size = (64, 16)
        with self.canvas.before:
            self._bg = Color(*theme.TEXT_MUTED)
            self._rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(
            pos=lambda w, v: setattr(self._rect, "pos", v),
            size=lambda w, v: setattr(self._rect, "size", v),
        )
        lbl = Label(
            text="BUILT-IN",
            color=(1, 1, 1, 1),
            font_size="9sp",
            bold=True,
            halign="center",
            valign="middle",
        )
        lbl.bind(size=lambda w, s: setattr(w, "text_size", s))
        self.add_widget(lbl)


class _ScheduleRow(Panel):
    """A single schedule card in the list."""

    def __init__(
        self,
        info: Dict[str, Any],
        *,
        on_view: Callable[[Dict[str, Any]], None],
        on_duplicate: Callable[[Dict[str, Any]], None],
        on_edit: Callable[[Dict[str, Any]], None],
        on_delete: Callable[[Dict[str, Any]], None],
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.padding = (10, 6, 10, 6)
        self.spacing = 2
        self.info = info

        builtin = bool(info.get("builtin"))

        # Header: name + builtin badge
        header = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=22, spacing=6
        )
        name_lbl = value_label(str(info.get("name") or info.get("filename") or "?"), size="14sp")
        name_lbl.size_hint_x = 1
        header.add_widget(name_lbl)
        if builtin:
            header.add_widget(_BuiltinBadge())
        self.add_widget(header)

        # Detail: filename + species + thickness + stage count + size
        species = info.get("species") or "?"
        thickness = info.get("thickness_in")
        thickness_s = f"{thickness} in" if thickness is not None else "?"
        stage_count = info.get("stage_count", 0)
        size_s = format_size(int(info.get("size_bytes") or 0))
        self.add_widget(
            small_label(
                f"{species} - {thickness_s} - {stage_count} stages - {size_s}",
                size="11sp",
            )
        )
        self.add_widget(
            small_label(str(info.get("filename") or ""), size="11sp")
        )

        # Action row
        actions = BoxLayout(
            orientation="horizontal",
            size_hint_y=None,
            height=34,
            spacing=6,
            padding=(0, 4, 0, 0),
        )

        view_btn = _row_button("View")
        view_btn.bind(on_release=lambda _b: on_view(info))
        actions.add_widget(view_btn)

        dup_btn = _row_button("Duplicate")
        dup_btn.bind(on_release=lambda _b: on_duplicate(info))
        actions.add_widget(dup_btn)

        edit_btn = _row_button("Edit", disabled=builtin)
        if not builtin:
            edit_btn.bind(on_release=lambda _b: on_edit(info))
        actions.add_widget(edit_btn)

        del_btn = _row_button("Delete", danger=True, disabled=builtin)
        if not builtin:
            del_btn.bind(on_release=lambda _b: on_delete(info))
        actions.add_widget(del_btn)

        # Stretch spacer so buttons sit on the left
        actions.add_widget(BoxLayout())
        self.add_widget(actions)


# ---- the screen ------------------------------------------------------------


class SchedulesScreen(Screen):
    """Top-level schedule list. AP/STA only."""

    def __init__(
        self,
        connection: ConnectionManager,
        on_finish: Optional[Callable[[], None]] = None,
        **kwargs,
    ):
        super().__init__(name="schedules", **kwargs)
        self.connection = connection
        self._on_finish = on_finish

        self._in_flight = False
        self._delete_in_flight = False
        self._current_mode: str = MODE_OFFLINE
        self._schedules: List[Dict[str, Any]] = []

        # background
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

        # Header row
        header = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=32, spacing=6
        )
        title = value_label("Schedules", size="16sp")
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
            valign="middle",
            shorten=True,
            shorten_from="right",
        )
        self.status_label.bind(size=lambda w, s: setattr(w, "text_size", s))
        root.add_widget(self.status_label)

        # Top actions
        top_actions = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=36, spacing=6
        )
        new_btn = Button(
            text="New schedule",
            size_hint_x=None,
            width=140,
            font_size="13sp",
            background_color=(0.30, 0.55, 0.85, 1),
            color=(1, 1, 1, 1),
        )
        new_btn.bind(on_release=lambda _b: self._on_new())
        top_actions.add_widget(new_btn)

        refresh_btn = Button(
            text="Refresh",
            size_hint_x=None,
            width=90,
            font_size="13sp",
            background_color=(0.30, 0.32, 0.38, 1),
            color=(1, 1, 1, 1),
        )
        refresh_btn.bind(on_release=lambda _b: self._load())
        top_actions.add_widget(refresh_btn)
        top_actions.add_widget(BoxLayout())
        root.add_widget(top_actions)

        # Scrollable list
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

    def on_pre_enter(self, *args):
        self._load()

    def _on_connection_change(self, result: DetectResult) -> None:
        self._current_mode = result.mode
        if self.manager and self.manager.current == self.name:
            if not is_direct_mode(result.mode):
                self.status_label.text = (
                    "Direct connection lost - schedules are AP-only."
                )
                self._clear_list()

    def _back(self) -> None:
        if self._on_finish:
            self._on_finish()

    # ---- load ---------------------------------------------------------------

    def _load(self) -> None:
        if self._in_flight:
            return
        if not is_direct_mode(self._current_mode):
            self.status_label.text = "Direct connection required."
            self._clear_list()
            return
        if self.connection.client.config.base_url is None:
            return
        self._in_flight = True
        self.status_label.text = "Loading schedules..."
        client = self.connection.client

        def work():
            return client.schedules()

        def done(result, err):
            self._in_flight = False
            if err is not None:
                self.status_label.text = f"Load failed: {err}"
                self._clear_list()
                return
            scheds = (result or {}).get("schedules") or []
            self._schedules = sorted(
                scheds, key=lambda s: (not s.get("builtin"), s.get("name") or "")
            )
            self._render()
            self.status_label.text = (
                f"{len(scheds)} schedule{'s' if len(scheds) != 1 else ''} on Pico"
            )

        call_async(work, done)

    def _clear_list(self) -> None:
        self.list_box.clear_widgets()
        self._schedules = []

    def _render(self) -> None:
        self.list_box.clear_widgets()
        if not self._schedules:
            self.list_box.add_widget(
                small_label("No schedules found on SD card.", size="12sp")
            )
            return
        for info in self._schedules:
            self.list_box.add_widget(
                _ScheduleRow(
                    info,
                    on_view=self._on_view,
                    on_duplicate=self._on_duplicate,
                    on_edit=self._on_edit,
                    on_delete=self._on_delete,
                )
            )

    # ---- editor transitions ------------------------------------------------

    def _editor_screen(self):
        if self.manager is None:
            return None
        if "schedule_editor" not in self.manager.screen_names:
            return None
        return self.manager.get_screen("schedule_editor")

    def _open_editor_with_full(
        self,
        filename: Optional[str],
        schedule: Optional[Dict[str, Any]],
        *,
        mode: str,
    ) -> None:
        editor = self._editor_screen()
        if editor is None:
            self.status_label.text = "Editor unavailable."
            return
        editor.load(schedule=schedule, filename=filename, mode=mode)
        self.manager.current = "schedule_editor"

    def _fetch_and_open(
        self,
        filename: str,
        *,
        mode: str,
        transform: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        target_filename: Optional[str] = None,
    ) -> None:
        """Load the full JSON for `filename` from the Pico, then open the
        editor. Used by View / Edit / Duplicate.

        `transform` mutates the schedule before opening (used by Duplicate
        to rename). `target_filename` overrides the filename stored on the
        editor (used by Duplicate / New).
        """
        if not is_direct_mode(self._current_mode):
            self.status_label.text = "Direct connection required."
            return
        self.status_label.text = f"Loading {filename}..."
        client = self.connection.client

        def work():
            return client.schedule_get(filename)

        def done(result, err):
            if err is not None or not isinstance(result, dict):
                self.status_label.text = f"Failed to load {filename}: {err}"
                return
            schedule = dict(result)
            if transform is not None:
                schedule = transform(schedule)
            fname = target_filename if target_filename is not None else filename
            self.status_label.text = ""
            self._open_editor_with_full(fname, schedule, mode=mode)

        call_async(work, done)

    def _on_new(self) -> None:
        if not is_direct_mode(self._current_mode):
            self.status_label.text = "Direct connection required."
            return
        # Blank template: single drying stage pre-populated per spec.
        template = {
            "name": "New schedule",
            "species": "maple",
            "thickness_in": 1.0,
            "stages": [
                {
                    "name": "Stage 1",
                    "stage_type": "drying",
                    "target_temp_c": 38,
                    "target_rh_pct": 80,
                    "target_mc_pct": 25.0,
                    "min_duration_h": 8,
                    "max_duration_h": 36,
                }
            ],
        }
        self._open_editor_with_full(None, template, mode="new")

    def _on_view(self, info: Dict[str, Any]) -> None:
        self._fetch_and_open(info["filename"], mode="view")

    def _on_duplicate(self, info: Dict[str, Any]) -> None:
        src_filename = info["filename"]
        new_filename = _duplicate_filename(src_filename)

        def transform(sched: Dict[str, Any]) -> Dict[str, Any]:
            sched = dict(sched)
            sched["name"] = _duplicate_name(str(sched.get("name") or ""))
            return sched

        self._fetch_and_open(
            src_filename,
            mode="duplicate",
            transform=transform,
            target_filename=new_filename,
        )

    def _on_edit(self, info: Dict[str, Any]) -> None:
        if info.get("builtin"):
            return
        self._fetch_and_open(info["filename"], mode="edit")

    def _on_delete(self, info: Dict[str, Any]) -> None:
        if info.get("builtin"):
            return
        filename = info["filename"]
        name = info.get("name") or filename
        confirm(
            "Delete schedule",
            f"Delete '{name}' ({filename})? This cannot be undone.",
            on_confirm=lambda: self._do_delete(filename),
            confirm_text="Delete",
            danger=True,
        )

    def _do_delete(self, filename: str) -> None:
        if self._delete_in_flight:
            return
        self._delete_in_flight = True
        self.status_label.text = f"Deleting {filename}..."
        client = self.connection.client

        def work():
            return client.schedule_delete(filename)

        def done(result, err):
            self._delete_in_flight = False
            if err is not None:
                self.status_label.text = f"Delete failed: {err}"
                return
            self.status_label.text = f"Deleted {filename}."
            self._load()

        call_async(work, done)
