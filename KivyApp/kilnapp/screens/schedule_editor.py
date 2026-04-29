"""Schedule editor screen (AP/STA only).

Phase 9: structured per-stage editor. Opened by the Schedules list screen
in one of four modes:

    view      - display only; every field disabled, Save hidden
    edit      - edit an existing user schedule (filename fixed)
    duplicate - save-as a new filename (filename editable)
    new       - create a new schedule from scratch (filename editable)

Design notes
------------
- The spec suggests a RecycleView. In practice Kivy RecycleView does not
  play well with a mix of editable TextInputs and Spinners per row
  (row recycling mangles focus and loses partial input). Typical
  schedules have <12 stages, so we use a plain BoxLayout inside a
  ScrollView. Performance is not a concern at this scale.

- Stage rows hold live references to their own fields. Edits write back
  into the stage dict via `_collect_stage()` at save time - there is no
  data binding layer. This mirrors the one-shot collection pattern the
  existing Settings + Start Run screens use.

- Validation on Save mirrors the Pico's server-side validation
  (main.py handle_schedule_put) so the user sees inline errors rather
  than a 400 from the server. We still handle server 400s gracefully if
  the Pico adds stricter rules later.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from kivy.graphics import Color, Rectangle
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.screenmanager import Screen
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput

from kilnapp import theme
from kilnapp.api.autodetect import DetectResult, MODE_OFFLINE, is_direct_mode
from kilnapp.api.client import call_async
from kilnapp.connection import ConnectionManager
from kilnapp.widgets.cards import Panel, small_label, value_label
from kilnapp.widgets.dialog import confirm
from kilnapp.widgets.form import _FlatSpinnerOption


MODE_VIEW = "view"
MODE_EDIT = "edit"
MODE_DUPLICATE = "duplicate"
MODE_NEW = "new"

STAGE_TYPES = ("drying", "equalizing", "conditioning")
SPECIES_OPTIONS = ("maple", "beech", "oak", "pine", "other")
THICKNESS_OPTIONS = ("0.5", "1", "custom")

TEMP_MIN_C = 30.0
TEMP_MAX_C = 80.0
RH_MIN_PCT = 20.0
RH_MAX_PCT = 95.0

BUILTIN_FILENAMES = (
    "maple_05in.json",
    "maple_1in.json",
    "beech_05in.json",
    "beech_1in.json",
)


# ---- helpers ---------------------------------------------------------------


def _parse_float(text: str, *, allow_blank: bool = False) -> Optional[float]:
    text = (text or "").strip()
    if text == "":
        if allow_blank:
            return None
        raise ValueError("required")
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"'{text}' is not a number")


def _fmt_opt(v: Any) -> str:
    """Format a float/int for a text field; empty string for None."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _slugify(name: str) -> str:
    """Turn a schedule name into a safe filename stem."""
    out = []
    prev_us = False
    for ch in (name or "").lower().strip():
        if ch.isalnum():
            out.append(ch)
            prev_us = False
        elif ch in (" ", "-", "_", "."):
            if not prev_us and out:
                out.append("_")
                prev_us = True
    slug = "".join(out).strip("_")
    return slug or "schedule"


def _derive_filename(name: str) -> str:
    return f"{_slugify(name)}.json"


# ---- small field factories -------------------------------------------------


def _text_input(
    initial: str = "",
    *,
    hint: str = "",
    input_filter: Optional[str] = None,
    width: Optional[int] = None,
) -> TextInput:
    kwargs: Dict[str, Any] = dict(
        text=initial,
        multiline=False,
        hint_text=hint,
        input_filter=input_filter,
        size_hint_y=None,
        height=32,
        font_size="12sp",
        background_color=(1, 1, 1, 1),
        foreground_color=(0.05, 0.05, 0.07, 1),
        cursor_color=(0.05, 0.05, 0.07, 1),
        padding=(6, 6, 6, 6),
    )
    if width is not None:
        kwargs["size_hint_x"] = None
        kwargs["width"] = width
    return TextInput(**kwargs)


def _stage_spinner(initial: str, values) -> Spinner:
    return Spinner(
        text=initial,
        values=tuple(values),
        size_hint_y=None,
        height=32,
        font_size="12sp",
        background_color=(0.30, 0.32, 0.38, 1),
        color=theme.TEXT_PRIMARY,
        option_cls=_FlatSpinnerOption,
    )


def _col_label(text: str, *, width: int) -> Label:
    lbl = Label(
        text=text,
        color=theme.TEXT_SECONDARY,
        font_size="10sp",
        size_hint_x=None,
        width=width,
        size_hint_y=None,
        height=16,
        halign="left",
        valign="middle",
    )
    lbl.bind(size=lambda w, s: setattr(w, "text_size", s))
    return lbl


# ---- stage row -------------------------------------------------------------


class _StageRow(BoxLayout):
    """One stage row: name | type | temp | rh | mc | min_h | max_h | del.

    Stages appear one per Panel so the visual 'card' language of the rest
    of the app carries over. A horizontal strip holds the compact fields,
    with a full-width name field on top.
    """

    def __init__(
        self,
        stage: Dict[str, Any],
        *,
        on_delete: Callable[["_StageRow"], None],
        read_only: bool = False,
        **kwargs,
    ):
        super().__init__(orientation="vertical", **kwargs)
        self.size_hint_y = None
        self.bind(minimum_height=self.setter("height"))
        self.spacing = 2
        self.padding = (8, 6, 8, 6)
        self._on_delete = on_delete
        self._read_only = read_only

        with self.canvas.before:
            self._bg_color = Color(*theme.BG_PANEL)
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(
            pos=lambda w, v: setattr(self._bg_rect, "pos", v),
            size=lambda w, v: setattr(self._bg_rect, "size", v),
        )

        # Row header: stage number label on left, delete button on right
        header = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=22, spacing=6
        )
        self.index_label = small_label("Stage", bold=True, size="12sp")
        self.index_label.size_hint_x = 1
        header.add_widget(self.index_label)
        self.del_btn = Button(
            text="Delete",
            size_hint_x=None,
            width=72,
            size_hint_y=None,
            height=22,
            font_size="10sp",
            background_color=(0.85, 0.30, 0.30, 1),
            color=(1, 1, 1, 1),
        )
        self.del_btn.bind(on_release=lambda _b: self._delete_pressed())
        if read_only:
            self.del_btn.disabled = True
            self.del_btn.opacity = 0
        header.add_widget(self.del_btn)
        self.add_widget(header)

        # Name field (full width)
        self.add_widget(small_label("Stage name", size="10sp"))
        self.name_input = _text_input(
            str(stage.get("name") or ""),
            hint="e.g. Stage 1 - Initial warm-up",
        )
        self.add_widget(self.name_input)

        # Type spinner
        self.add_widget(small_label("Stage type", size="10sp"))
        stype_initial = str(stage.get("stage_type") or "drying")
        if stype_initial not in STAGE_TYPES:
            stype_initial = "drying"
        self.type_spinner = _stage_spinner(stype_initial, STAGE_TYPES)
        self.type_spinner.bind(text=lambda _s, v: self._on_type_change(v))
        self.add_widget(self.type_spinner)

        # Compact numeric row: temp | rh | mc
        label_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=14, spacing=6)
        for t in ("Temp (C)", "RH (%)", "MC (%)"):
            label_row.add_widget(_col_label(t, width=110))
        label_row.add_widget(BoxLayout())
        self.add_widget(label_row)

        field_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=32, spacing=6)
        self.temp_input = _text_input(
            _fmt_opt(stage.get("target_temp_c")),
            input_filter="float",
            width=110,
        )
        self.rh_input = _text_input(
            _fmt_opt(stage.get("target_rh_pct")),
            input_filter="float",
            width=110,
        )
        self.mc_input = _text_input(
            _fmt_opt(stage.get("target_mc_pct")),
            input_filter="float",
            width=110,
            hint="drying",
        )
        field_row.add_widget(self.temp_input)
        field_row.add_widget(self.rh_input)
        field_row.add_widget(self.mc_input)
        field_row.add_widget(BoxLayout())
        self.add_widget(field_row)

        # Duration row: min_h | max_h
        dur_label_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=14, spacing=6)
        dur_label_row.add_widget(_col_label("Min duration (h)", width=170))
        dur_label_row.add_widget(_col_label("Max duration (h, blank=unlimited)", width=260))
        dur_label_row.add_widget(BoxLayout())
        self.add_widget(dur_label_row)

        dur_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=32, spacing=6)
        self.min_h_input = _text_input(
            _fmt_opt(stage.get("min_duration_h")),
            input_filter="float",
            width=170,
        )
        self.max_h_input = _text_input(
            _fmt_opt(stage.get("max_duration_h")),
            input_filter="float",
            width=260,
            hint="blank = unlimited",
        )
        dur_row.add_widget(self.min_h_input)
        dur_row.add_widget(self.max_h_input)
        dur_row.add_widget(BoxLayout())
        self.add_widget(dur_row)

        # Initial mc gating based on type
        self._on_type_change(stype_initial)

        if read_only:
            for f in (
                self.name_input,
                self.temp_input,
                self.rh_input,
                self.mc_input,
                self.min_h_input,
                self.max_h_input,
            ):
                f.readonly = True
                f.background_color = (0.85, 0.85, 0.87, 1)
            self.type_spinner.disabled = True

    def _on_type_change(self, stype: str) -> None:
        # MC% is required for drying, forbidden for equalizing/conditioning.
        # In view/disabled state Spinner still fires this callback on load,
        # so keep readonly semantics consistent with _read_only.
        if stype == "drying":
            self.mc_input.disabled = False
            self.mc_input.opacity = 1.0
        else:
            self.mc_input.text = ""
            self.mc_input.disabled = True
            self.mc_input.opacity = 0.5
        if self._read_only:
            self.mc_input.readonly = True

    def _delete_pressed(self) -> None:
        self._on_delete(self)

    def set_index(self, idx: int) -> None:
        self.index_label.text = f"Stage {idx}"

    def collect(self) -> Dict[str, Any]:
        """Convert the row's widget values into a stage dict.

        Raises ValueError with a user-friendly message if a required
        field is missing or invalid.
        """
        name = self.name_input.text.strip()
        if not name:
            raise ValueError("stage name is required")
        stype = self.type_spinner.text
        if stype not in STAGE_TYPES:
            raise ValueError(f"invalid stage type '{stype}'")

        try:
            temp = _parse_float(self.temp_input.text)
        except ValueError as e:
            raise ValueError(f"temp: {e}")
        if not (TEMP_MIN_C <= temp <= TEMP_MAX_C):
            raise ValueError(f"temp {temp} out of range {TEMP_MIN_C}-{TEMP_MAX_C} C")

        try:
            rh = _parse_float(self.rh_input.text)
        except ValueError as e:
            raise ValueError(f"RH: {e}")
        if not (RH_MIN_PCT <= rh <= RH_MAX_PCT):
            raise ValueError(f"RH {rh} out of range {RH_MIN_PCT}-{RH_MAX_PCT} %")

        if stype == "drying":
            try:
                mc = _parse_float(self.mc_input.text)
            except ValueError as e:
                raise ValueError(f"MC: drying stage requires a numeric target MC%")
            if mc <= 0:
                raise ValueError("MC must be > 0")
        else:
            # equalizing / conditioning: forbid MC
            if self.mc_input.text.strip():
                raise ValueError(
                    f"{stype} stages must leave target MC% blank"
                )
            mc = None

        try:
            min_h = _parse_float(self.min_h_input.text)
        except ValueError as e:
            raise ValueError(f"min duration: {e}")
        if min_h <= 0:
            raise ValueError("min duration must be > 0 h")

        try:
            max_h = _parse_float(self.max_h_input.text, allow_blank=True)
        except ValueError as e:
            raise ValueError(f"max duration: {e}")
        if max_h is not None and max_h < min_h:
            raise ValueError(
                f"max duration ({max_h}) must be >= min duration ({min_h})"
            )

        return {
            "name": name,
            "stage_type": stype,
            "target_temp_c": temp,
            "target_rh_pct": rh,
            "target_mc_pct": mc,
            "min_duration_h": min_h,
            "max_duration_h": max_h,
        }


# ---- the screen ------------------------------------------------------------


class ScheduleEditorScreen(Screen):
    """Schedule viewer + editor. AP/STA only."""

    def __init__(
        self,
        connection: ConnectionManager,
        on_finish: Optional[Callable[[], None]] = None,
        **kwargs,
    ):
        super().__init__(name="schedule_editor", **kwargs)
        self.connection = connection
        self._on_finish = on_finish

        self._mode: str = MODE_NEW
        self._original_filename: Optional[str] = None
        self._current_mode_conn: str = MODE_OFFLINE
        self._submit_in_flight = False
        self._stage_rows: List[_StageRow] = []
        # Last name we auto-derived a filename from. Per-instance so two
        # editor screens (or the same screen reused across runs) don't
        # share state.
        self._last_auto_name: str = ""

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

        # Header
        header = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=32, spacing=6
        )
        self.title_label = value_label("Schedule Editor", size="16sp")
        self.title_label.size_hint_x = 1
        header.add_widget(self.title_label)
        back_btn = Button(
            text="< Back",
            size_hint_x=None,
            width=80,
            font_size="13sp",
            background_color=(0.40, 0.42, 0.48, 1),
            color=(1, 1, 1, 1),
        )
        back_btn.bind(on_release=lambda _b: self._cancel())
        header.add_widget(back_btn)
        root.add_widget(header)

        # Status
        self.status_label = Label(
            text="",
            color=theme.TEXT_SECONDARY,
            font_size="11sp",
            size_hint_y=None,
            height=32,
            halign="left",
            valign="top",
            shorten=False,
        )
        self.status_label.bind(size=lambda w, s: setattr(w, "text_size", s))
        root.add_widget(self.status_label)

        # Scrollable content
        scroll = ScrollView(do_scroll_x=False, do_scroll_y=True)
        self.content_box = BoxLayout(
            orientation="vertical",
            spacing=8,
            size_hint_y=None,
            padding=(0, 0, 0, 8),
        )
        self.content_box.bind(minimum_height=self.content_box.setter("height"))

        # Header panel: name / species / thickness / filename
        self.header_panel = Panel()
        self.header_panel.add_widget(small_label("Schedule info", bold=True))

        self.header_panel.add_widget(small_label("Schedule name", size="11sp"))
        self.name_input = _text_input("", hint="e.g. My custom maple schedule")
        self.name_input.bind(text=lambda _w, _v: self._on_name_change())
        self.header_panel.add_widget(self.name_input)

        self.header_panel.add_widget(small_label("Species", size="11sp"))
        self.species_spinner = _stage_spinner("maple", SPECIES_OPTIONS)
        self.header_panel.add_widget(self.species_spinner)

        self.header_panel.add_widget(small_label("Thickness (in)", size="11sp"))
        self.thickness_spinner = _stage_spinner("1", THICKNESS_OPTIONS)
        self.header_panel.add_widget(self.thickness_spinner)

        # Custom thickness numeric field, only visible when thickness == 'custom'
        self.thickness_custom_input = _text_input(
            "", hint="e.g. 0.75", input_filter="float", width=160
        )
        self.thickness_custom_input.size_hint_x = None
        self.header_panel.add_widget(self.thickness_custom_input)
        self.thickness_spinner.bind(text=lambda _s, v: self._on_thickness_change(v))

        self.header_panel.add_widget(small_label("Filename", size="11sp"))
        self.filename_input = _text_input("", hint="auto-derived from name")
        self.header_panel.add_widget(self.filename_input)
        self.header_panel.add_widget(
            small_label(
                "(.json is appended automatically if missing)",
                size="10sp",
            )
        )

        self.content_box.add_widget(self.header_panel)

        # Stages list header
        stages_header = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=26, spacing=6
        )
        stages_header.add_widget(value_label("Stages", size="14sp"))
        self.add_stage_btn = Button(
            text="+ Add stage",
            size_hint_x=None,
            width=120,
            size_hint_y=None,
            height=26,
            font_size="12sp",
            background_color=(0.30, 0.55, 0.85, 1),
            color=(1, 1, 1, 1),
        )
        self.add_stage_btn.bind(on_release=lambda _b: self._add_stage_pressed())
        stages_header.add_widget(self.add_stage_btn)
        self.content_box.add_widget(stages_header)

        # Container for stage rows
        self.stages_box = BoxLayout(
            orientation="vertical", size_hint_y=None, spacing=6
        )
        self.stages_box.bind(minimum_height=self.stages_box.setter("height"))
        self.content_box.add_widget(self.stages_box)

        scroll.add_widget(self.content_box)
        root.add_widget(scroll)

        # Footer: Save / Cancel
        footer = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=42, spacing=6
        )
        self.save_btn = Button(
            text="Save",
            font_size="14sp",
            background_color=(0.30, 0.55, 0.85, 1),
            color=(1, 1, 1, 1),
        )
        self.save_btn.bind(on_release=lambda _b: self._on_save_pressed())
        footer.add_widget(self.save_btn)

        cancel_btn = Button(
            text="Cancel",
            size_hint_x=None,
            width=100,
            font_size="14sp",
            background_color=(0.40, 0.42, 0.48, 1),
            color=(1, 1, 1, 1),
        )
        cancel_btn.bind(on_release=lambda _b: self._cancel())
        footer.add_widget(cancel_btn)
        root.add_widget(footer)

        self.add_widget(root)

        self.connection.add_listener(self._on_connection_change)

    # ---- external API (called from Schedules list) -----------------------

    def load(
        self,
        *,
        schedule: Optional[Dict[str, Any]],
        filename: Optional[str],
        mode: str,
    ) -> None:
        """Populate the editor from a schedule dict. Called by the
        Schedules list screen before navigating to this screen."""
        if mode not in (MODE_VIEW, MODE_EDIT, MODE_DUPLICATE, MODE_NEW):
            mode = MODE_VIEW
        self._mode = mode
        self._original_filename = filename if mode == MODE_EDIT else None

        sched = schedule or {}
        name = str(sched.get("name") or "")
        species = str(sched.get("species") or "maple")
        thickness = sched.get("thickness_in")
        stages = sched.get("stages") or []

        # Header fields
        self.name_input.text = name
        if species not in SPECIES_OPTIONS:
            # Extend dropdown on the fly so existing schedules with odd
            # species values still edit cleanly.
            self.species_spinner.values = tuple(list(SPECIES_OPTIONS) + [species])
        self.species_spinner.text = species

        # Thickness: 0.5 / 1 / custom. Any other numeric value -> custom.
        if thickness is None:
            thickness_label = "1"
            custom_text = ""
        else:
            thickness_f = float(thickness)
            if abs(thickness_f - 0.5) < 1e-6:
                thickness_label = "0.5"
                custom_text = ""
            elif abs(thickness_f - 1.0) < 1e-6:
                thickness_label = "1"
                custom_text = ""
            else:
                thickness_label = "custom"
                custom_text = _fmt_opt(thickness_f)
        self.thickness_spinner.text = thickness_label
        self.thickness_custom_input.text = custom_text
        self._on_thickness_change(thickness_label)

        # Filename
        if filename:
            self.filename_input.text = filename
        else:
            self.filename_input.text = _derive_filename(name)

        # Stages
        self.stages_box.clear_widgets()
        self._stage_rows = []
        if not stages:
            # New/blank schedule: ensure at least one stage exists
            stages = [
                {
                    "name": "Stage 1",
                    "stage_type": "drying",
                    "target_temp_c": 38,
                    "target_rh_pct": 80,
                    "target_mc_pct": 25.0,
                    "min_duration_h": 8,
                    "max_duration_h": 36,
                }
            ]
        for stage in stages:
            self._append_stage_row(stage)
        self._renumber()

        self._apply_mode()
        self.status_label.text = ""

    # ---- mode application --------------------------------------------------

    def _apply_mode(self) -> None:
        """Apply view/edit/duplicate/new semantics to the widget states.

        - MODE_VIEW: disable all inputs + hide Save / Add / Delete
        - MODE_EDIT: lock filename (can't rename a file by editing); other fields live
        - MODE_DUPLICATE, MODE_NEW: everything editable
        """
        read_only = self._mode == MODE_VIEW

        # Title reflects mode
        if self._mode == MODE_VIEW:
            self.title_label.text = "View schedule"
        elif self._mode == MODE_EDIT:
            self.title_label.text = "Edit schedule"
        elif self._mode == MODE_DUPLICATE:
            self.title_label.text = "Duplicate schedule"
        else:
            self.title_label.text = "New schedule"

        # Read-only state
        for field in (
            self.name_input,
            self.thickness_custom_input,
            self.filename_input,
        ):
            field.readonly = read_only
            if read_only:
                field.background_color = (0.85, 0.85, 0.87, 1)
            else:
                field.background_color = (1, 1, 1, 1)

        self.species_spinner.disabled = read_only
        self.thickness_spinner.disabled = read_only

        # Filename is locked when editing an existing schedule (renaming
        # requires a delete + re-save to avoid orphan files).
        if self._mode == MODE_EDIT:
            self.filename_input.readonly = True
            self.filename_input.background_color = (0.85, 0.85, 0.87, 1)

        # Save + Add Stage hidden in view mode
        if read_only:
            self.save_btn.disabled = True
            self.save_btn.opacity = 0
            self.add_stage_btn.disabled = True
            self.add_stage_btn.opacity = 0
        else:
            self.save_btn.disabled = False
            self.save_btn.opacity = 1
            self.add_stage_btn.disabled = False
            self.add_stage_btn.opacity = 1

        # Push read-only to each stage row too by rebuilding with the flag
        # (cheaper than toggling attributes individually).
        self._rebuild_stage_rows_for_mode()

    def _rebuild_stage_rows_for_mode(self) -> None:
        read_only = self._mode == MODE_VIEW
        new_rows: List[_StageRow] = []
        # Collect current values so we don't lose in-progress edits when
        # mode changes mid-flight (rare but cheap).
        current_stages: List[Dict[str, Any]] = []
        for row in self._stage_rows:
            current_stages.append(self._row_values_best_effort(row))
        self.stages_box.clear_widgets()
        for stage in current_stages:
            row = _StageRow(
                stage,
                on_delete=self._delete_stage_row,
                read_only=read_only,
            )
            self.stages_box.add_widget(row)
            new_rows.append(row)
        self._stage_rows = new_rows
        self._renumber()

    @staticmethod
    def _row_values_best_effort(row: _StageRow) -> Dict[str, Any]:
        """Read a row's values without raising for invalid content. Used
        when rebuilding for mode changes, which must not throw."""
        def _f(t: str) -> Optional[float]:
            t = (t or "").strip()
            if not t:
                return None
            try:
                return float(t)
            except ValueError:
                return None

        stype = row.type_spinner.text if row.type_spinner.text in STAGE_TYPES else "drying"
        return {
            "name": row.name_input.text,
            "stage_type": stype,
            "target_temp_c": _f(row.temp_input.text),
            "target_rh_pct": _f(row.rh_input.text),
            "target_mc_pct": _f(row.mc_input.text),
            "min_duration_h": _f(row.min_h_input.text),
            "max_duration_h": _f(row.max_h_input.text),
        }

    # ---- connection gating -------------------------------------------------

    def _on_connection_change(self, result: DetectResult) -> None:
        self._current_mode_conn = result.mode
        if self.manager and self.manager.current == self.name:
            if not is_direct_mode(result.mode):
                self.status_label.text = (
                    "Direct connection lost - returning to Schedules."
                )
                # Do not force navigate immediately; the user may want to
                # copy their edits out. Just disable Save.
                self.save_btn.disabled = True
                self.save_btn.opacity = 0.5

    # ---- name / thickness reactive updates ---------------------------------

    def _on_name_change(self) -> None:
        if self._mode in (MODE_NEW, MODE_DUPLICATE):
            # Regenerate filename suggestion as the user types, but only
            # if the user hasn't manually edited the filename field away
            # from the derived form.
            derived = _derive_filename(self.name_input.text)
            current = self.filename_input.text.strip()
            if current == "" or current == _derive_filename(
                self._last_auto_name or ""
            ):
                self.filename_input.text = derived
            self._last_auto_name = self.name_input.text
        else:
            self._last_auto_name = self.name_input.text

    def _on_thickness_change(self, value: str) -> None:
        if value == "custom":
            self.thickness_custom_input.opacity = 1.0
            self.thickness_custom_input.disabled = False
            self.thickness_custom_input.height = 32
        else:
            self.thickness_custom_input.opacity = 0
            self.thickness_custom_input.disabled = True
            self.thickness_custom_input.height = 0

    # ---- stage row management ----------------------------------------------

    def _append_stage_row(self, stage: Dict[str, Any]) -> None:
        row = _StageRow(
            stage,
            on_delete=self._delete_stage_row,
            read_only=self._mode == MODE_VIEW,
        )
        self.stages_box.add_widget(row)
        self._stage_rows.append(row)

    def _add_stage_pressed(self) -> None:
        # Copy the last row's values (if any) as a starting point so the
        # user isn't re-entering temp/RH progressions from zero.
        template: Dict[str, Any] = {
            "name": f"Stage {len(self._stage_rows) + 1}",
            "stage_type": "drying",
            "target_temp_c": 40,
            "target_rh_pct": 70,
            "target_mc_pct": 20.0,
            "min_duration_h": 8,
            "max_duration_h": 36,
        }
        if self._stage_rows:
            last = self._row_values_best_effort(self._stage_rows[-1])
            for k in ("stage_type", "target_temp_c", "target_rh_pct", "min_duration_h", "max_duration_h"):
                if last.get(k) is not None:
                    template[k] = last[k]
            template["name"] = f"Stage {len(self._stage_rows) + 1}"
            # Default new stage to drying so MC% is editable
            template["stage_type"] = "drying"
        self._append_stage_row(template)
        self._renumber()

    def _delete_stage_row(self, row: _StageRow) -> None:
        if self._mode == MODE_VIEW:
            return
        if len(self._stage_rows) <= 1:
            self.status_label.text = "Cannot delete the last stage - a schedule needs at least one."
            return
        confirm(
            "Delete stage",
            "Remove this stage from the schedule?",
            on_confirm=lambda: self._do_delete_stage(row),
            confirm_text="Delete",
            danger=True,
        )

    def _do_delete_stage(self, row: _StageRow) -> None:
        if row not in self._stage_rows:
            return
        self.stages_box.remove_widget(row)
        self._stage_rows.remove(row)
        self._renumber()

    def _renumber(self) -> None:
        for i, row in enumerate(self._stage_rows, start=1):
            row.set_index(i)

    # ---- save --------------------------------------------------------------

    def _collect(self) -> Dict[str, Any]:
        """Read all widgets into a schedule dict. Raises ValueError on
        invalid input. Throws before anything is sent over the wire."""
        name = self.name_input.text.strip()
        if not name:
            raise ValueError("schedule name is required")

        species = self.species_spinner.text.strip()
        if not species:
            raise ValueError("species is required")

        thickness_label = self.thickness_spinner.text
        if thickness_label == "0.5":
            thickness_in: Optional[float] = 0.5
        elif thickness_label == "1":
            thickness_in = 1.0
        elif thickness_label == "custom":
            custom = self.thickness_custom_input.text.strip()
            if not custom:
                raise ValueError("custom thickness is required")
            try:
                thickness_in = float(custom)
            except ValueError:
                raise ValueError(f"custom thickness '{custom}' is not a number")
            if thickness_in <= 0:
                raise ValueError("thickness must be > 0")
        else:
            thickness_in = None

        if not self._stage_rows:
            raise ValueError("at least one stage is required")

        stages: List[Dict[str, Any]] = []
        for i, row in enumerate(self._stage_rows, start=1):
            try:
                stage = row.collect()
            except ValueError as e:
                raise ValueError(f"Stage {i}: {e}")
            stages.append(stage)

        return {
            "name": name,
            "species": species,
            "thickness_in": thickness_in,
            "stages": stages,
        }

    def _target_filename(self) -> str:
        raw = self.filename_input.text.strip()
        if not raw:
            raise ValueError("filename is required")
        if not raw.endswith(".json"):
            raw = raw + ".json"
        # Strip any accidental leading path
        raw = raw.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        return raw

    def _on_save_pressed(self) -> None:
        if self._submit_in_flight:
            return
        if self._mode == MODE_VIEW:
            return
        try:
            schedule = self._collect()
            filename = self._target_filename()
        except ValueError as e:
            self.status_label.text = f"Invalid: {e}"
            return

        if filename in BUILTIN_FILENAMES and self._mode != MODE_EDIT:
            # Mode == edit is already blocked upstream (Schedules greys
            # it out), but belt-and-braces: the Pico rejects builtins.
            self.status_label.text = (
                f"'{filename}' is a built-in filename. Change the filename."
            )
            return

        if not is_direct_mode(self._current_mode_conn):
            self.status_label.text = "Direct connection required."
            return

        msg = f"Save schedule '{schedule['name']}' to '{filename}'?"
        if self._mode == MODE_EDIT:
            msg = f"Overwrite '{filename}'?"
        confirm(
            "Save schedule",
            msg,
            on_confirm=lambda: self._do_save(filename, schedule),
            confirm_text="Save",
        )

    def _do_save(self, filename: str, schedule: Dict[str, Any]) -> None:
        self._submit_in_flight = True
        self.save_btn.disabled = True
        self.save_btn.opacity = 0.5
        self.status_label.text = f"Saving {filename}..."

        client = self.connection.client

        def work():
            return client.schedule_put(filename, schedule)

        def done(result, err):
            self._submit_in_flight = False
            self.save_btn.disabled = False
            self.save_btn.opacity = 1.0
            if err is not None:
                self.status_label.text = f"Save failed: {err}"
                return
            self.status_label.text = f"Saved {filename}."
            if self._on_finish:
                self._on_finish()

        call_async(work, done)

    def _cancel(self) -> None:
        if self._submit_in_flight:
            return
        if self._on_finish:
            self._on_finish()
