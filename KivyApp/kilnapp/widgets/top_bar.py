"""Top app bar: title on the left, connection indicator on the right."""

from kivy.graphics import Color, Rectangle
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label

from kilnapp import theme
from kilnapp.widgets.conn_indicator import ConnectionIndicator


class TopBar(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(orientation="horizontal", **kwargs)
        self.size_hint_y = None
        self.height = theme.TOP_BAR_HEIGHT
        self.padding = (16, 0, 8, 0)
        self.spacing = 8

        with self.canvas.before:
            self._bg_color = Color(*theme.BG_PANEL)
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(pos=self._sync_bg, size=self._sync_bg)

        self._title = Label(
            text="Kiln Controller",
            color=theme.TEXT_PRIMARY,
            font_size="18sp",
            bold=True,
            halign="left",
            valign="middle",
        )
        self._title.bind(size=lambda w, s: setattr(w, "text_size", s))
        self.add_widget(self._title)

        self.indicator = ConnectionIndicator()
        self.add_widget(self.indicator)

    def _sync_bg(self, *_):
        self._bg_rect.pos = self.pos
        self._bg_rect.size = self.size

    def set_title(self, text: str) -> None:
        self._title.text = text
