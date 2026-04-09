"""Simple yes/no confirmation dialog built on kivy.uix.popup.Popup."""

from typing import Callable, Optional

from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup

from kilnapp import theme


def confirm(
    title: str,
    message: str,
    *,
    on_confirm: Callable[[], None],
    confirm_text: str = "Confirm",
    cancel_text: str = "Cancel",
    danger: bool = False,
) -> Popup:
    """Show a modal confirmation popup. Returns the Popup instance."""
    body = BoxLayout(orientation="vertical", padding=12, spacing=12)

    msg_label = Label(
        text=message,
        color=theme.TEXT_PRIMARY,
        font_size="14sp",
        halign="center",
        valign="middle",
    )
    msg_label.bind(size=lambda w, s: setattr(w, "text_size", s))
    body.add_widget(msg_label)

    button_row = BoxLayout(
        orientation="horizontal", size_hint_y=None, height=44, spacing=8
    )
    cancel_btn = Button(
        text=cancel_text,
        font_size="14sp",
        background_color=(0.40, 0.42, 0.48, 1),
        color=(1, 1, 1, 1),
    )
    confirm_color = (0.85, 0.30, 0.30, 1) if danger else (0.30, 0.55, 0.85, 1)
    confirm_btn = Button(
        text=confirm_text,
        font_size="14sp",
        background_color=confirm_color,
        color=(1, 1, 1, 1),
    )
    button_row.add_widget(cancel_btn)
    button_row.add_widget(confirm_btn)
    body.add_widget(button_row)

    popup = Popup(
        title=title,
        content=body,
        size_hint=(0.85, None),
        height=200,
        auto_dismiss=False,
        title_size="15sp",
    )
    cancel_btn.bind(on_release=lambda _b: popup.dismiss())

    def _confirm(_b):
        popup.dismiss()
        on_confirm()

    confirm_btn.bind(on_release=_confirm)
    popup.open()
    return popup
