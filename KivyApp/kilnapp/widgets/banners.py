"""Stage progress, water-pan, and fault banners shown on the Dashboard.

These match kivy_app_spec.md > Dashboard:

* Stage banner: stage name + type + elapsed/min progress bar (always shown
  when a run is active or in cooldown)
* Water pan banner: yellow notice when stage_type is "equalizing" or
  "conditioning"
* Fault banner: red notice for any fault alert; tapping calls back into the
  app to navigate to the Alerts tab; takes priority over the water pan banner
"""

from __future__ import annotations

from typing import Callable, Optional

from kivy.graphics import Color, Rectangle
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.progressbar import ProgressBar

from kilnapp import theme
from kilnapp.alerts import humanise


# ---- helpers --------------------------------------------------------------


class _ColoredBox(BoxLayout):
    """A BoxLayout with a flat colored background and auto-sized height.

    `bg` is consumed via kwargs so this class is safe to mix in alongside
    other Kivy bases (e.g. ButtonBehavior) whose own __init__ chains call
    `super().__init__(**kwargs)`.
    """

    def __init__(self, bg=None, **kwargs):
        kwargs.setdefault("orientation", "vertical")
        kwargs.setdefault("padding", (12, 6, 12, 6))
        kwargs.setdefault("spacing", 2)
        kwargs.setdefault("size_hint_y", None)
        super().__init__(**kwargs)
        self.bind(minimum_height=self.setter("height"))
        with self.canvas.before:
            self._bg_color = Color(*(bg if bg is not None else theme.BG_PANEL))
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(
            pos=lambda w, v: setattr(self._bg_rect, "pos", v),
            size=lambda w, v: setattr(self._bg_rect, "size", v),
        )

    def set_bg(self, color) -> None:
        self._bg_color.rgba = color


def _line(text: str, *, color, size: str = "13sp", bold: bool = False) -> Label:
    lbl = Label(
        text=text,
        color=color,
        font_size=size,
        bold=bold,
        size_hint_y=None,
        height=18,
        halign="left",
        valign="middle",
    )
    lbl.bind(size=lambda w, s: setattr(w, "text_size", s))
    return lbl


# ---- stage banner ---------------------------------------------------------


class StageBanner(_ColoredBox):
    """Stage name, type badge, elapsed/min h, and a progress bar."""

    def __init__(self, **kwargs):
        super().__init__(bg=theme.BG_PANEL, **kwargs)
        self._title = _line("No run active", color=theme.TEXT_PRIMARY, size="15sp", bold=True)
        self._subtitle = _line("", color=theme.TEXT_SECONDARY, size="11sp")
        self._progress = ProgressBar(
            max=100, value=0, size_hint_y=None, height=6
        )
        self.add_widget(self._title)
        self.add_widget(self._subtitle)
        self.add_widget(self._progress)

    def show_idle(self) -> None:
        self._title.text = "No run active"
        self._subtitle.text = ""
        self._progress.value = 0

    def show_cooldown(self) -> None:
        self._title.text = "Cooldown"
        self._subtitle.text = ""
        self._progress.value = 0

    def show_run(
        self,
        *,
        stage_index,
        stage_name: str,
        stage_type: Optional[str],
        elapsed_h: Optional[float],
        min_h: Optional[float],
        schedule_name: Optional[str],
    ) -> None:
        idx_str = f"Stage {stage_index}" if stage_index is not None else "Stage ?"
        type_str = (stage_type or "").upper() or "RUN"
        self._title.text = f"{idx_str} - {stage_name} [{type_str}]"

        elapsed_str = "elapsed --" if elapsed_h is None else f"elapsed {elapsed_h:.1f} h"
        min_str = "" if min_h is None else f" / min {min_h:.0f} h"
        sched_str = f" - {schedule_name}" if schedule_name else ""
        self._subtitle.text = f"{elapsed_str}{min_str}{sched_str}"

        # Progress: caps at 100%, no overflow
        if elapsed_h is not None and min_h and min_h > 0:
            pct = max(0.0, min(100.0, 100.0 * elapsed_h / min_h))
        else:
            pct = 0.0
        self._progress.value = pct


# ---- water pan banner -----------------------------------------------------


class WaterPanBanner(_ColoredBox):
    """Yellow advisory shown during equalizing/conditioning stages."""

    BG = (0.55, 0.45, 0.10, 1)

    def __init__(self, **kwargs):
        super().__init__(bg=self.BG, **kwargs)
        self.add_widget(
            _line(
                "Water pans may be needed - check RH actual vs target",
                color=(1, 1, 1, 1),
                size="13sp",
                bold=True,
            )
        )


# ---- fault banner ---------------------------------------------------------


class _AlertBanner(ButtonBehavior, _ColoredBox):
    """Shared base for FaultBanner / NoticeBanner.

    Shows an UPPERCASE prefix ("FAULT" or "NOTICE"), one or more alert codes,
    and a "Tap to view alerts" subtitle. Tapping invokes the `on_tap` callback.
    """

    PREFIX = "ALERT"

    def __init__(self, bg, on_tap: Optional[Callable[[], None]] = None, **kwargs):
        super().__init__(bg=bg, **kwargs)
        self._on_tap = on_tap
        self._title = _line("", color=(1, 1, 1, 1), size="14sp", bold=True)
        self._subtitle = _line(
            "Tap to view alerts", color=(1, 1, 1, 0.85), size="11sp"
        )
        self.add_widget(self._title)
        self.add_widget(self._subtitle)
        self.bind(on_release=self._handle_tap)

    def _handle_tap(self, *_):
        if self._on_tap is not None:
            self._on_tap()

    def set_alerts(self, alerts) -> None:
        if not alerts:
            self._title.text = ""
            return
        readable = [humanise(c) for c in alerts]
        head = " / ".join(readable[:2])
        more = "" if len(readable) <= 2 else f" (+{len(readable) - 2} more)"
        self._title.text = f"{self.PREFIX}: {head}{more}"


class FaultBanner(_AlertBanner):
    """Red banner for hardware/firmware faults."""

    PREFIX = "FAULT"

    def __init__(self, on_tap: Optional[Callable[[], None]] = None, **kwargs):
        super().__init__(bg=theme.SEVERITY_ERROR, on_tap=on_tap, **kwargs)


class NoticeBanner(_AlertBanner):
    """Amber banner for procedural / batch issues."""

    PREFIX = "NOTICE"

    def __init__(self, on_tap: Optional[Callable[[], None]] = None, **kwargs):
        super().__init__(bg=theme.SEVERITY_NOTICE, on_tap=on_tap, **kwargs)
