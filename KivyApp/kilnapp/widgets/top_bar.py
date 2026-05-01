"""Top app bar: title on the left, connection indicator on the right."""

from kivy.graphics import Color, Rectangle
from kivy.metrics import dp
from kivy.uix.anchorlayout import AnchorLayout
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label

from kilnapp import theme
from kilnapp.widgets.conn_indicator import ConnectionIndicator


class TopBar(BoxLayout):
    """Horizontal bar with title on the left, connection indicator on the
    right. Both children are wrapped in AnchorLayouts (anchor_y=center)
    with FIXED sizes so AnchorLayout has stable inputs to anchor against
    - earlier attempts that bound the label size to texture_size left
    Android with a stale (0, 0) initial size that AnchorLayout couldn't
    re-anchor after the texture rendered.
    """

    def __init__(self, **kwargs):
        super().__init__(orientation="horizontal", **kwargs)
        self.size_hint_y = None
        self.height = theme.TOP_BAR_HEIGHT
        self.padding = (dp(16), 0, dp(8), 0)
        self.spacing = dp(8)

        with self.canvas.before:
            self._bg_color = Color(*theme.BG_PANEL)
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(pos=self._sync_bg, size=self._sync_bg)

        # Title - fixed size so AnchorLayout has a stable target. Width
        # is generous enough to fit any title we have ("Moisture
        # Calibration" is the longest at ~20 chars, ~dp(220) at 18sp
        # bold). Text wraps inside via halign="left".
        title_anchor = AnchorLayout(anchor_x="left", anchor_y="center")
        self._title = Label(
            text="Kiln Controller",
            color=theme.TEXT_PRIMARY,
            font_size="18sp",
            bold=True,
            halign="left",
            valign="middle",
            size_hint=(None, None),
            size=(dp(240), dp(28)),
            text_size=(dp(240), dp(28)),
        )
        title_anchor.add_widget(self._title)
        self.add_widget(title_anchor)

        # Indicator - its own AnchorLayout cell pinned to the right.
        ind_anchor = AnchorLayout(
            anchor_x="right",
            anchor_y="center",
            size_hint_x=None,
            width=dp(120),
        )
        self.indicator = ConnectionIndicator()
        # Force fixed height so AnchorLayout has a stable target.
        self.indicator.size_hint_y = None
        self.indicator.height = dp(28)
        ind_anchor.add_widget(self.indicator)
        self.add_widget(ind_anchor)

    def _sync_bg(self, *_):
        self._bg_rect.pos = self.pos
        self._bg_rect.size = self.size

    def set_title(self, text: str) -> None:
        self._title.text = text
