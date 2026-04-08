"""Placeholder screen used by the Phase 1 shell.

Each tab gets one of these. Real screens replace them in later phases:
- Dashboard: Phase 3-4
- History:   Phase 7
- Alerts:    Phase 5
- Runs:      Phase 6
- Settings:  Phase 2
"""

from kivy.graphics import Color, Rectangle
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.screenmanager import Screen

from kilnapp import theme


class PlaceholderScreen(Screen):
    """Single-screen placeholder showing the tab name and a 'coming soon' note."""

    def __init__(self, screen_name: str, title: str, note: str = "", **kwargs):
        super().__init__(name=screen_name, **kwargs)

        root = BoxLayout(orientation="vertical", padding=24, spacing=12)
        with root.canvas.before:
            self._bg_color = Color(*theme.BG_DARK)
            self._bg_rect = Rectangle(pos=root.pos, size=root.size)
        root.bind(
            pos=lambda w, v: setattr(self._bg_rect, "pos", v),
            size=lambda w, v: setattr(self._bg_rect, "size", v),
        )

        title_label = Label(
            text=title,
            color=theme.TEXT_PRIMARY,
            font_size="26sp",
            bold=True,
            size_hint_y=None,
            height=44,
            halign="center",
            valign="middle",
        )
        title_label.bind(size=lambda w, s: setattr(w, "text_size", s))
        root.add_widget(title_label)

        note_label = Label(
            text=note or "Placeholder screen - real content arrives in a later phase.",
            color=theme.TEXT_SECONDARY,
            font_size="14sp",
            halign="center",
            valign="top",
        )
        note_label.bind(size=lambda w, s: setattr(w, "text_size", s))
        root.add_widget(note_label)

        self.add_widget(root)
