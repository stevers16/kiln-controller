"""Start Run screen (AP/STA only).

Phase 8: three-step pre-run wizard rendered as a single scrollable page so
the user can scroll back and forth between steps on a phone without losing
state.

Step 1 - Select schedule
    Species + Thickness shortcut buttons pick a built-in filename from the
    species+thickness matrix. A fallback spinner lets the user choose any
    schedule on the Pico (including user-created ones that don't match
    a built-in species/thickness combo).

Step 2 - Run label (optional)
    Free-text input. Passed through in the POST body; current firmware
    ignores it (future Pi4 daemon / firmware can record it).

Step 3 - Pre-run checklist
    Six hard-coded checkboxes. Start is disabled until all are ticked AND
    a schedule has been selected.

On Start: confirm() modal, then POST /run/start, then on_finish() back to
Dashboard.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from kivy.clock import Clock
from kivy.graphics import Color, Rectangle
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
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


# Built-in schedule filename map. Species + thickness shortcut buttons
# pick from this matrix; anything else falls through to the manual spinner.
SHORTCUT_FILENAMES: Dict[tuple, str] = {
    ("maple", "0.5"): "maple_05in.json",
    ("maple", "1"): "maple_1in.json",
    ("beech", "0.5"): "beech_05in.json",
    ("beech", "1"): "beech_1in.json",
}

SPECIES_OPTIONS = [("Maple", "maple"), ("Beech", "beech"), ("Other", "other")]
THICKNESS_OPTIONS = [("0.5 in", "0.5"), ("1 in", "1"), ("Custom", "custom")]

CHECKLIST_ITEMS = [
    "Lumber loaded and stacked with spacers",
    "Moisture probes inserted into representative boards",
    "Water pans removed from kiln (initial drying stages)",
    "Kiln door sealed",
    "Extension cord connected and heater plugged in",
    "Adequate ventilation around kiln",
]

MANUAL_SPINNER_PLACEHOLDER = "Or choose manually"


# ---- small helpers ---------------------------------------------------------


def _fmt_duration_range(stages: List[Dict[str, Any]]) -> str:
    """Sum min_duration_h / max_duration_h across stages. Returns a
    human-readable label like '84-180 h' or '84+ h' when any max is unset."""
    min_total = 0.0
    max_total = 0.0
    any_open_ended = False
    for st in stages or []:
        min_h = st.get("min_duration_h") or 0
        min_total += float(min_h)
        max_h = st.get("max_duration_h")
        if max_h is None:
            any_open_ended = True
        else:
            max_total += float(max_h)
    if any_open_ended:
        return f"{min_total:.0f}+ h"
    if max_total <= min_total:
        return f"{min_total:.0f} h"
    return f"{min_total:.0f}-{max_total:.0f} h"


# ---- toggle button group ---------------------------------------------------


class _ChoiceButton(Button):
    """Flat button that visually indicates selected/unselected state."""

    def __init__(self, label: str, selected: bool = False, **kwargs):
        super().__init__(text=label, **kwargs)
        self.font_size = "13sp"
        self.color = (1, 1, 1, 1)
        self.size_hint_y = None
        self.height = 38
        self._set_selected(selected)

    def _set_selected(self, selected: bool) -> None:
        self.background_color = (
            (0.30, 0.55, 0.85, 1) if selected else (0.30, 0.32, 0.38, 1)
        )


# ---- the screen ------------------------------------------------------------


class StartRunScreen(Screen):
    """Three-step pre-run wizard, AP/STA only."""

    def __init__(
        self,
        connection: ConnectionManager,
        on_finish: Optional[Callable[[], None]] = None,
        **kwargs,
    ):
        super().__init__(name="start_run", **kwargs)
        self.connection = connection
        # `on_finish()` sends the user back to Dashboard whether they started
        # a run or cancelled. The app wires this to _navigate_to("dashboard").
        self._on_finish = on_finish

        self._in_flight = False
        self._submit_in_flight = False
        self._current_mode: str = MODE_OFFLINE

        # Selected state
        self._species: Optional[str] = None  # "maple", "beech", "other"
        self._thickness: Optional[str] = None  # "0.5", "1", "custom"
        self._selected_filename: Optional[str] = None
        self._selected_schedule: Optional[Dict[str, Any]] = None
        self._checklist_state: List[bool] = [False] * len(CHECKLIST_ITEMS)

        # Full list from /schedules, keyed by filename for spinner lookup.
        self._all_schedules: Dict[str, Dict[str, Any]] = {}

        # ---- background -------------------------------------------------
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
        title = value_label("Start Run", size="16sp")
        title.size_hint_x = 1
        header.add_widget(title)
        cancel_btn = Button(
            text="Cancel",
            size_hint_x=None,
            width=80,
            font_size="13sp",
            background_color=(0.40, 0.42, 0.48, 1),
            color=(1, 1, 1, 1),
        )
        cancel_btn.bind(on_release=lambda _b: self._cancel())
        header.add_widget(cancel_btn)
        root.add_widget(header)

        # Status / error line. Single-line, fixed height - mirrors the
        # small_label pattern used elsewhere. (Earlier draft wrapped text
        # and auto-grew via texture_size, which created a
        # size -> text_size -> texture_size -> height -> size feedback
        # loop that Kivy's clock flagged as runaway layout iteration.)
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
        self.status_label.bind(
            size=lambda w, s: setattr(w, "text_size", s),
        )
        root.add_widget(self.status_label)

        # Scrollable wizard content
        scroll = ScrollView(do_scroll_x=False, do_scroll_y=True)
        content = BoxLayout(
            orientation="vertical",
            spacing=8,
            size_hint_y=None,
            padding=(0, 0, 0, 8),
        )
        content.bind(minimum_height=content.setter("height"))

        # ---- Step 1: schedule picker -----------------------------------
        step1 = Panel()
        step1.add_widget(small_label("Step 1 - Select schedule", bold=True))

        step1.add_widget(small_label("Species"))
        species_row = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=38, spacing=6
        )
        self._species_buttons: Dict[str, _ChoiceButton] = {}
        for label, key in SPECIES_OPTIONS:
            btn = _ChoiceButton(label)
            btn.bind(on_release=lambda _b, k=key: self._on_species(k))
            self._species_buttons[key] = btn
            species_row.add_widget(btn)
        step1.add_widget(species_row)

        step1.add_widget(small_label("Thickness"))
        thickness_row = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=38, spacing=6
        )
        self._thickness_buttons: Dict[str, _ChoiceButton] = {}
        for label, key in THICKNESS_OPTIONS:
            btn = _ChoiceButton(label)
            btn.bind(on_release=lambda _b, k=key: self._on_thickness(k))
            self._thickness_buttons[key] = btn
            thickness_row.add_widget(btn)
        step1.add_widget(thickness_row)

        step1.add_widget(small_label("Or choose any schedule on the Pico"))
        self.manual_spinner = Spinner(
            text=MANUAL_SPINNER_PLACEHOLDER,
            values=[MANUAL_SPINNER_PLACEHOLDER],
            size_hint_y=None,
            height=36,
            font_size="13sp",
            background_color=(0.30, 0.32, 0.38, 1),
            color=theme.TEXT_PRIMARY,
            option_cls=_FlatSpinnerOption,
        )
        self.manual_spinner.bind(text=self._on_manual_pick)
        step1.add_widget(self.manual_spinner)

        # Selection preview: schedule name, species/thickness, stage count,
        # duration range.
        self.selection_name = small_label("No schedule selected", bold=True)
        self.selection_detail = small_label("", size="11sp")
        self.selection_duration = small_label("", size="11sp")
        step1.add_widget(self.selection_name)
        step1.add_widget(self.selection_detail)
        step1.add_widget(self.selection_duration)

        content.add_widget(step1)

        # ---- Step 2: run label (optional) ------------------------------
        step2 = Panel()
        step2.add_widget(small_label("Step 2 - Run label (optional)", bold=True))
        step2.add_widget(
            small_label(
                "Free text stored with the run (e.g. 'Workshop maple batch 1')",
                size="11sp",
            )
        )
        self.label_input = TextInput(
            text="",
            multiline=False,
            hint_text="Run label",
            size_hint_y=None,
            height=36,
            font_size="13sp",
            background_color=(1, 1, 1, 1),
            foreground_color=(0.05, 0.05, 0.07, 1),
            cursor_color=(0.05, 0.05, 0.07, 1),
            padding=(8, 8, 8, 8),
        )
        step2.add_widget(self.label_input)
        content.add_widget(step2)

        # ---- Step 3: pre-run checklist ---------------------------------
        step3 = Panel()
        step3.add_widget(small_label("Step 3 - Pre-run checklist", bold=True))
        self._checkbox_widgets: List[CheckBox] = []
        for i, text in enumerate(CHECKLIST_ITEMS):
            step3.add_widget(self._build_checklist_row(i, text))
        content.add_widget(step3)

        scroll.add_widget(content)
        root.add_widget(scroll)

        # ---- Start footer ----------------------------------------------
        footer = BoxLayout(
            orientation="horizontal",
            size_hint_y=None,
            height=42,
            spacing=6,
        )
        self.start_btn = Button(
            text="Start Run",
            font_size="14sp",
            background_color=(0.30, 0.55, 0.85, 1),
            color=(1, 1, 1, 1),
        )
        self.start_btn.disabled = True
        self.start_btn.opacity = 0.5
        self.start_btn.bind(on_release=lambda _b: self._on_start_pressed())
        footer.add_widget(self.start_btn)
        root.add_widget(footer)

        self.add_widget(root)

        # Subscribe to connection changes so we can bail if the user leaves
        # AP mode while this screen is up.
        self.connection.add_listener(self._on_connection_change)

    # ---- lifecycle ---------------------------------------------------------

    def on_pre_enter(self, *args):
        # Reset wizard state every time the screen is opened so the user
        # doesn't inherit stale selections from a prior visit.
        self._reset()
        self._load_schedules()

    # ---- connection change ------------------------------------------------

    def _on_connection_change(self, result: DetectResult) -> None:
        self._current_mode = result.mode
        # If the app drops out of AP/STA mode while the wizard is up, bail
        # back to Dashboard - the AP-only POST would fail anyway.
        if self.manager and self.manager.current == self.name:
            if not is_direct_mode(result.mode):
                self.status_label.text = (
                    "Direct connection lost - returning to Dashboard."
                )
                if self._on_finish:
                    Clock.schedule_once(lambda _dt: self._on_finish(), 1.0)

    # ---- reset ------------------------------------------------------------

    def _reset(self) -> None:
        self._species = None
        self._thickness = None
        self._selected_filename = None
        self._selected_schedule = None
        self._checklist_state = [False] * len(CHECKLIST_ITEMS)
        for btn in self._species_buttons.values():
            btn._set_selected(False)
        for btn in self._thickness_buttons.values():
            btn._set_selected(False)
        self.manual_spinner.text = MANUAL_SPINNER_PLACEHOLDER
        self.label_input.text = ""
        for cb in self._checkbox_widgets:
            cb.active = False
        self._update_selection_preview()
        self._update_start_enabled()
        self.status_label.text = ""

    # ---- schedule loading --------------------------------------------------

    def _load_schedules(self) -> None:
        if self._in_flight:
            return
        if not is_direct_mode(self._current_mode):
            self.status_label.text = "Direct connection required."
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
                return
            scheds = (result or {}).get("schedules") or []
            self._all_schedules = {s.get("filename"): s for s in scheds if s.get("filename")}
            labels = self._build_spinner_labels()
            # Always include the placeholder so the spinner can show "none
            # selected" when we first open the screen / after a reset.
            self.manual_spinner.values = [MANUAL_SPINNER_PLACEHOLDER] + labels
            self.status_label.text = (
                f"{len(scheds)} schedule{'s' if len(scheds) != 1 else ''} on Pico"
            )

        call_async(work, done)

    def _build_spinner_labels(self) -> List[str]:
        """Build human-readable labels for the manual-pick spinner.

        Format: '<name> (<filename>)' so the label is self-explanatory but
        we can still recover the filename by splitting on ' ('.
        """
        out = []
        for fname, info in self._all_schedules.items():
            name = info.get("name") or fname
            out.append(f"{name} ({fname})")
        out.sort()
        return out

    @staticmethod
    def _spinner_label_to_filename(label: str) -> Optional[str]:
        # '<name> (<filename>)' -> filename
        if label == MANUAL_SPINNER_PLACEHOLDER:
            return None
        if "(" not in label or not label.endswith(")"):
            return None
        return label.rsplit("(", 1)[1][:-1]

    # ---- step 1: schedule picker handlers ---------------------------------

    def _on_species(self, key: str) -> None:
        self._species = key
        for k, btn in self._species_buttons.items():
            btn._set_selected(k == key)
        self._try_resolve_shortcut()

    def _on_thickness(self, key: str) -> None:
        self._thickness = key
        for k, btn in self._thickness_buttons.items():
            btn._set_selected(k == key)
        self._try_resolve_shortcut()

    def _try_resolve_shortcut(self) -> None:
        """If species+thickness map to a built-in filename, select it.
        Otherwise (e.g. Other / Custom) clear any current selection and
        nudge the user to pick from the manual spinner."""
        if not (self._species and self._thickness):
            return
        fname = SHORTCUT_FILENAMES.get((self._species, self._thickness))
        if fname:
            self._select_filename(fname, from_shortcut=True)
            return
        # No built-in for this combo. Clear whatever was selected (shortcut
        # or manual) so the preview doesn't keep showing a stale schedule.
        self._selected_filename = None
        self._selected_schedule = None
        self.manual_spinner.unbind(text=self._on_manual_pick)
        self.manual_spinner.text = MANUAL_SPINNER_PLACEHOLDER
        self.manual_spinner.bind(text=self._on_manual_pick)
        self._update_selection_preview(hint="Choose a schedule manually below.")
        self._update_start_enabled()

    def _on_manual_pick(self, _spinner, text: str) -> None:
        fname = self._spinner_label_to_filename(text)
        if fname is None:
            return
        self._select_filename(fname, from_shortcut=False)

    def _select_filename(self, fname: str, *, from_shortcut: bool) -> None:
        info = self._all_schedules.get(fname)
        if info is None:
            # Shortcut file isn't on the Pico (e.g. user deleted a built-in
            # or renamed one). Let the user know instead of silently failing.
            self._selected_filename = None
            self._selected_schedule = None
            self._update_selection_preview(
                hint=f"Schedule '{fname}' not found on Pico."
            )
            self._update_start_enabled()
            return

        self._selected_filename = fname
        # Sync the spinner so the preview is unambiguous even when the
        # selection came from shortcut buttons.
        target_label = f"{info.get('name') or fname} ({fname})"
        if self.manual_spinner.text != target_label:
            # Avoid firing the spinner callback recursively
            self.manual_spinner.unbind(text=self._on_manual_pick)
            self.manual_spinner.text = target_label
            self.manual_spinner.bind(text=self._on_manual_pick)

        # Fetch full schedule for duration range / stage list. If we already
        # cached it, reuse.
        if fname in self._all_schedules and "stages" in self._all_schedules[fname]:
            self._selected_schedule = self._all_schedules[fname]
            self._update_selection_preview()
            self._update_start_enabled()
            return

        # Need to GET /schedules/{filename} to read stage list.
        self.status_label.text = f"Loading {fname}..."
        client = self.connection.client

        def work():
            return client.schedule_get(fname)

        def done(result, err):
            if err is not None or not isinstance(result, dict):
                self.status_label.text = f"Failed to load {fname}: {err}"
                return
            # Cache the full schedule back into the index so subsequent
            # selections of the same filename don't re-fetch.
            merged = dict(self._all_schedules.get(fname) or {})
            merged.update(result)
            self._all_schedules[fname] = merged
            self._selected_schedule = merged
            self.status_label.text = ""
            self._update_selection_preview()
            self._update_start_enabled()

        call_async(work, done)

    def _update_selection_preview(self, *, hint: Optional[str] = None) -> None:
        if self._selected_schedule is None:
            self.selection_name.text = hint or "No schedule selected"
            self.selection_detail.text = ""
            self.selection_duration.text = ""
            return
        sched = self._selected_schedule
        name = sched.get("name") or self._selected_filename or ""
        species = sched.get("species") or "?"
        thickness = sched.get("thickness_in")
        thickness_s = f"{thickness} in" if thickness is not None else "?"
        stages = sched.get("stages") or []
        self.selection_name.text = name
        self.selection_detail.text = (
            f"{species} - {thickness_s} - {len(stages)} stages"
        )
        if stages:
            self.selection_duration.text = (
                f"Estimated duration: {_fmt_duration_range(stages)}"
            )
        else:
            self.selection_duration.text = ""

    # ---- step 3: checklist -------------------------------------------------

    def _build_checklist_row(self, index: int, text: str) -> BoxLayout:
        row = BoxLayout(
            orientation="horizontal",
            size_hint_y=None,
            height=36,
            spacing=6,
        )
        cb = CheckBox(
            size_hint_x=None,
            width=32,
            color=theme.TEXT_PRIMARY,
        )
        cb.bind(active=lambda _w, v, i=index: self._on_check(i, v))
        self._checkbox_widgets.append(cb)
        row.add_widget(cb)
        lbl = Label(
            text=text,
            color=theme.TEXT_PRIMARY,
            font_size="13sp",
            halign="left",
            valign="middle",
        )
        lbl.bind(size=lambda w, s: setattr(w, "text_size", s))
        row.add_widget(lbl)
        return row

    def _on_check(self, index: int, value: bool) -> None:
        self._checklist_state[index] = bool(value)
        self._update_start_enabled()

    # ---- start gating ------------------------------------------------------

    def _update_start_enabled(self) -> None:
        ok = (
            self._selected_filename is not None
            and all(self._checklist_state)
            and not self._submit_in_flight
        )
        self.start_btn.disabled = not ok
        self.start_btn.opacity = 1.0 if ok else 0.5

    # ---- submit ------------------------------------------------------------

    def _on_start_pressed(self) -> None:
        if self._selected_filename is None:
            return
        name = (
            (self._selected_schedule or {}).get("name")
            or self._selected_filename
        )
        confirm(
            "Start Run",
            f"Start '{name}'? This will activate the heater and fans.",
            on_confirm=self._do_start,
            confirm_text="Start Run",
        )

    def _do_start(self) -> None:
        if self._submit_in_flight or self._selected_filename is None:
            return
        self._submit_in_flight = True
        self._update_start_enabled()
        self.status_label.text = "Starting run..."
        client = self.connection.client
        filename = self._selected_filename
        label = self.label_input.text.strip() or None

        def work():
            return client.run_start(filename, label=label)

        def done(result, err):
            self._submit_in_flight = False
            if err is not None:
                self.status_label.text = f"Start failed: {err}"
                self._update_start_enabled()
                return
            sched_name = (result or {}).get("schedule") or filename
            self.status_label.text = f"Run started: {sched_name}"
            # Hop back to Dashboard; it will see run_active=True on its next
            # /status poll and light up the banner + Stop/Advance buttons.
            if self._on_finish:
                Clock.schedule_once(lambda _dt: self._on_finish(), 0.5)

        call_async(work, done)

    # ---- cancel ------------------------------------------------------------

    def _cancel(self) -> None:
        if self._submit_in_flight:
            return
        if self._on_finish:
            self._on_finish()
