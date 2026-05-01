"""Connection mode indicator: a coloured dot + label.

Phase 1: static placeholder showing "Offline" (red). Phase 2 wires it to the
real autodetect result.
"""

from kivy.graphics import Color, Ellipse
from kivy.metrics import dp
from kivy.properties import ListProperty, StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.widget import Widget

from kilnapp import theme


class _Dot(Widget):
    color = ListProperty(theme.DOT_OFFLINE)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.size_hint = (None, None)
        self.size = (dp(14), dp(14))
        with self.canvas:
            self._color_instr = Color(*self.color)
            self._ellipse = Ellipse(pos=self.pos, size=self.size)
        self.bind(pos=self._sync, size=self._sync, color=self._sync_color)

    def _sync(self, *_):
        self._ellipse.pos = self.pos
        self._ellipse.size = self.size

    def _sync_color(self, *_):
        self._color_instr.rgba = self.color


class ConnectionIndicator(BoxLayout):
    """Coloured dot + short text label, used in the top app bar."""

    label_text = StringProperty("Offline")
    dot_color = ListProperty(theme.DOT_OFFLINE)

    def __init__(self, **kwargs):
        super().__init__(orientation="horizontal", spacing=8, **kwargs)
        self.size_hint = (None, 1)
        self.width = dp(110)
        self.padding = (0, 0, 8, 0)

        # Centre the dot vertically inside an anchor-style spacer
        dot_wrap = BoxLayout(orientation="vertical", size_hint=(None, 1), width=dp(14))
        dot_wrap.add_widget(Widget())  # top spacer
        self._dot = _Dot()
        dot_wrap.add_widget(self._dot)
        dot_wrap.add_widget(Widget())  # bottom spacer
        self.add_widget(dot_wrap)

        self._label = Label(
            text=self.label_text,
            color=theme.TEXT_PRIMARY,
            font_size="14sp",
            halign="left",
            valign="middle",
        )
        self._label.bind(size=lambda w, s: setattr(w, "text_size", s))
        self.add_widget(self._label)

        self.bind(label_text=self._on_label, dot_color=self._on_dot_color)

    def _on_label(self, _instance, value):
        self._label.text = value

    def _on_dot_color(self, _instance, value):
        self._dot.color = value

    def set_state(self, mode: str) -> None:
        """Set indicator from a mode string: 'direct', 'cottage', 'sta', 'offline'."""
        states = {
            "direct": (theme.DOT_DIRECT, "Direct"),
            "cottage": (theme.DOT_COTTAGE, "Cottage"),
            "sta": (theme.DOT_STA, "Pico STA"),
            "offline": (theme.DOT_OFFLINE, "Offline"),
        }
        color, label = states.get(mode, states["offline"])
        self.dot_color = color
        self.label_text = label
