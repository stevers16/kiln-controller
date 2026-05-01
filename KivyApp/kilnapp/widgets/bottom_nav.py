"""Bottom navigation bar - five tab buttons.

Plain Kivy implementation (no KivyMD dependency). Each tab is a ToggleButton
in a shared group; selecting one switches the ScreenManager to the matching
screen and visually highlights the active tab.

Text-only labels for now: Kivy's bundled Roboto doesn't include the
Unicode dingbats / geometric glyphs that would otherwise read as icons,
and Kivy doesn't fall back to system fonts on Android, so anything not
in Roboto renders as a tofu box. Real Material icons would need a
bundled icon font (deferred).
"""

from kivy.graphics import Color, Rectangle
from kivy.properties import StringProperty
from kivy.uix.anchorlayout import AnchorLayout
from kivy.uix.behaviors import ToggleButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label

from kilnapp import theme


class _NavTab(ToggleButtonBehavior, AnchorLayout):
    """One bottom-nav tab. Single centered text label."""

    screen_name = StringProperty("")

    def __init__(self, label: str, screen_name: str, icon_glyph: str = "", **kwargs):
        super().__init__(anchor_x="center", anchor_y="center", **kwargs)
        self.screen_name = screen_name
        self.group = "kiln_bottom_nav"
        self.allow_no_selection = False
        self.size_hint_x = 1

        with self.canvas.before:
            self._bg_color = Color(*theme.BG_PANEL)
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(pos=self._sync_bg, size=self._sync_bg)

        self._icon_label = None  # kept for back-compat with _on_state
        self._text_label = Label(
            text=label,
            color=theme.TEXT_SECONDARY,
            font_size="13sp",
            bold=True,
            halign="center",
            valign="middle",
        )
        self.add_widget(self._text_label)

        self.bind(state=self._on_state)

    def _sync_bg(self, *_):
        self._bg_rect.pos = self.pos
        self._bg_rect.size = self.size

    def _on_state(self, _instance, value):
        if value == "down":
            self._bg_color.rgba = theme.BG_PANEL_ACTIVE
            self._text_label.color = theme.TEXT_PRIMARY
        else:
            self._bg_color.rgba = theme.BG_PANEL
            self._text_label.color = theme.TEXT_SECONDARY


class BottomNav(BoxLayout):
    """Container for the five tabs. Calls `on_select(screen_name)` when tapped."""

    # The five tabs from kivy_app_spec.md > Navigation. Glyphs are
    # BMP-Unicode symbols that render in the default Roboto bundle.
    TABS = [
        ("Dashboard", "dashboard", "⌂"),  # ⌂  HOUSE
        ("History",   "history",   "⧗"),  # ⧗  TIMER / hourglass-like
        ("Alerts",    "alerts",    "⚠"),  # ⚠  WARNING SIGN
        ("Runs",      "runs",      "☰"),  # ☰  TRIGRAM (list)
        ("Settings",  "settings",  "⚙"),  # ⚙  GEAR
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
        """Programmatically select a tab without firing on_select.

        ToggleButtonBehavior's group de-selection only runs on touch press
        (`_do_press`), not on programmatic `state` writes. So we manually
        put every other tab back to "normal" before flipping the target
        to "down" - otherwise we end up with multiple tabs highlighted.
        """
        tab = self._tabs.get(screen_name)
        if tab is None:
            return
        for name, t in self._tabs.items():
            if name != screen_name and t.state == "down":
                t.state = "normal"
        if tab.state != "down":
            tab.state = "down"
