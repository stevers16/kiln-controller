"""Card / panel widgets used by the Dashboard.

Plain Kivy boxes with a flat background colour. Phase 3 deliberately keeps the
styling minimal - the goal is correct data flow first, polish second.
"""

from kivy.graphics import Color, Rectangle
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label

from kilnapp import theme


class Panel(BoxLayout):
    """Vertical box with a flat dark-panel background.

    Auto-sizes its height to fit its children unless the caller passes an
    explicit `size_hint_y` (e.g. for side-by-side panels in an HBox where the
    parent gives them equal width and we want them to stretch to fill that
    row's height).
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("orientation", "vertical")
        kwargs.setdefault("padding", (dp(10), dp(6), dp(10), dp(6)))
        kwargs.setdefault("spacing", dp(4))
        # Default: hug content vertically. Caller can override.
        auto_size = "size_hint_y" not in kwargs and "height" not in kwargs
        if auto_size:
            kwargs["size_hint_y"] = None
        super().__init__(**kwargs)
        if auto_size:
            self.bind(minimum_height=self.setter("height"))
        with self.canvas.before:
            self._bg_color = Color(*theme.BG_PANEL)
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(
            pos=lambda w, v: setattr(self._bg_rect, "pos", v),
            size=lambda w, v: setattr(self._bg_rect, "size", v),
        )


def small_label(
    text: str, *, color=None, bold: bool = False, size: str = "13sp"
) -> Label:
    lbl = Label(
        text=text,
        color=color or theme.TEXT_SECONDARY,
        font_size=size,
        bold=bold,
        size_hint_y=None,
        height=dp(22),
        halign="left",
        valign="middle",
    )
    lbl.bind(size=lambda w, s: setattr(w, "text_size", s))
    return lbl


def value_label(text: str, *, color=None, size: str = "17sp") -> Label:
    lbl = Label(
        text=text,
        color=color or theme.TEXT_PRIMARY,
        font_size=size,
        bold=True,
        size_hint_y=None,
        height=dp(26),
        halign="left",
        valign="middle",
    )
    lbl.bind(size=lambda w, s: setattr(w, "text_size", s))
    return lbl
