"""Small form-row helpers used by the Settings screen."""

from typing import Optional

from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.spinner import Spinner, SpinnerOption
from kivy.uix.textinput import TextInput

from kilnapp import theme


class _FlatSpinnerOption(SpinnerOption):
    """Spinner dropdown row with no inter-item border/gap.

    Plain SpinnerOption inherits Button, which draws a 1-pixel border that
    looks like a gap between items. Forcing background_normal/background_down
    to empty strings drops the border.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.background_normal = ""
        self.background_down = ""
        self.background_color = (0.30, 0.32, 0.38, 1)
        self.color = theme.TEXT_PRIMARY
        self.font_size = "14sp"
        self.height = dp(36)


def label(text: str, *, width: float = dp(130)) -> Label:
    lbl = Label(
        text=text,
        color=theme.TEXT_SECONDARY,
        font_size="14sp",
        size_hint_x=None,
        width=width,
        halign="left",
        valign="middle",
    )
    lbl.bind(size=lambda w, s: setattr(w, "text_size", s))
    return lbl


def text_input(
    initial: str = "",
    *,
    password: bool = False,
    multiline: bool = False,
    input_filter: Optional[str] = None,
    hint: str = "",
) -> TextInput:
    return TextInput(
        text=initial,
        password=password,
        multiline=multiline,
        input_filter=input_filter,
        hint_text=hint,
        size_hint_y=None,
        height=dp(34),
        font_size="14sp",
        background_color=(1, 1, 1, 1),
        foreground_color=(0.05, 0.05, 0.07, 1),
        cursor_color=(0.05, 0.05, 0.07, 1),
        padding=(dp(8), dp(8), dp(8), dp(8)),
    )


def row(label_text: str, field) -> BoxLayout:
    box = BoxLayout(
        orientation="horizontal",
        size_hint_y=None,
        height=dp(36),
        spacing=dp(8),
    )
    box.add_widget(label(label_text))
    box.add_widget(field)
    return box


def spinner(values, initial: str) -> Spinner:
    return Spinner(
        text=initial,
        values=values,
        size_hint_y=None,
        height=dp(36),
        font_size="14sp",
        background_color=(0.30, 0.32, 0.38, 1),
        color=theme.TEXT_PRIMARY,
        option_cls=_FlatSpinnerOption,
    )
