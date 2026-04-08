"""Bottom navigation bar - five tab buttons.

Plain Kivy implementation (no KivyMD dependency). Each tab is a ToggleButton
in a shared group; selecting one switches the ScreenManager to the matching
screen and visually highlights the active tab.
"""

from kivy.graphics import Color, Rectangle
from kivy.properties import StringProperty
from kivy.uix.behaviors import ToggleButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label

from kilnapp import theme


class _NavTab(ToggleButtonBehavior, BoxLayout):
    """One bottom-nav tab. Two stacked labels (icon glyph + name)."""

    screen_name = StringProperty("")

    def __init__(self, label: str, screen_name: str, icon_glyph: str = "", **kwargs):
        super().__init__(orientation="vertical", **kwargs)
        self.screen_name = screen_name
        self.group = "kiln_bottom_nav"
        self.allow_no_selection = False
        self.size_hint_x = 1

        with self.canvas.before:
            self._bg_color = Color(*theme.BG_PANEL)
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(pos=self._sync_bg, size=self._sync_bg)

        self._icon_label = Label(
            text=icon_glyph or label[0],
            color=theme.TEXT_SECONDARY,
            font_size="18sp",
            halign="center",
            valign="bottom",
        )
        self._text_label = Label(
            text=label,
            color=theme.TEXT_SECONDARY,
            font_size="11sp",
            halign="center",
            valign="top",
        )
        self.add_widget(self._icon_label)
        self.add_widget(self._text_label)

        self.bind(state=self._on_state)

    def _sync_bg(self, *_):
        self._bg_rect.pos = self.pos
        self._bg_rect.size = self.size

    def _on_state(self, _instance, value):
        if value == "down":
            self._bg_color.rgba = theme.BG_PANEL_ACTIVE
            self._icon_label.color = theme.TEXT_PRIMARY
            self._text_label.color = theme.TEXT_PRIMARY
        else:
            self._bg_color.rgba = theme.BG_PANEL
            self._icon_label.color = theme.TEXT_SECONDARY
            self._text_label.color = theme.TEXT_SECONDARY


class BottomNav(BoxLayout):
    """Container for the five tabs. Calls `on_select(screen_name)` when tapped."""

    # The five tabs from kivy_app_spec.md > Navigation
    TABS = [
        ("Dashboard", "dashboard", "H"),
        ("History", "history", "~"),
        ("Alerts", "alerts", "!"),
        ("Runs", "runs", "="),
        ("Settings", "settings", "*"),
    ]

    def __init__(self, on_select=None, **kwargs):
        super().__init__(orientation="horizontal", **kwargs)
        self.size_hint_y = None
        self.height = theme.BOTTOM_NAV_HEIGHT
        self._on_select = on_select
        self._tabs: dict[str, _NavTab] = {}

        for label, screen_name, glyph in self.TABS:
            tab = _NavTab(label=label, screen_name=screen_name, icon_glyph=glyph)
            tab.bind(on_release=self._tab_pressed)
            self._tabs[screen_name] = tab
            self.add_widget(tab)

        # Default selection
        first = self.TABS[0][1]
        self._tabs[first].state = "down"

    def _tab_pressed(self, tab: _NavTab):
        if self._on_select is not None:
            self._on_select(tab.screen_name)

    def select(self, screen_name: str) -> None:
        """Programmatically select a tab without firing on_select."""
        tab = self._tabs.get(screen_name)
        if tab is not None and tab.state != "down":
            tab.state = "down"
