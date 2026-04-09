"""Small form-row helpers used by the Settings screen."""

from typing import Optional

from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput

from kilnapp import theme


def label(text: str, *, width: int = 130) -> Label:
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
        height=36,
        font_size="14sp",
        background_color=(1, 1, 1, 1),
        foreground_color=(0.05, 0.05, 0.07, 1),
        cursor_color=(0.05, 0.05, 0.07, 1),
        padding=(8, 8, 8, 8),
    )


def row(label_text: str, field) -> BoxLayout:
    box = BoxLayout(
        orientation="horizontal",
        size_hint_y=None,
        height=44,
        spacing=8,
    )
    box.add_widget(label(label_text))
    box.add_widget(field)
    return box


def spinner(values, initial: str) -> Spinner:
    return Spinner(
        text=initial,
        values=values,
        size_hint_y=None,
        height=36,
        font_size="14sp",
        background_color=(0.30, 0.32, 0.38, 1),
        color=theme.TEXT_PRIMARY,
    )
