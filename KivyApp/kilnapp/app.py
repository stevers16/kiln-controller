"""Kiln Controller - Kivy App root.

Phase 2: real Settings screen + ConnectionManager + live indicator. The other
four tabs are still placeholders.
"""

from kivy.app import App
from kivy.core.window import Window
from kivy.graphics import Color, Rectangle
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.screenmanager import NoTransition, ScreenManager

from kilnapp import theme
from kilnapp.api.autodetect import (
    MODE_COTTAGE,
    MODE_DIRECT,
    MODE_OFFLINE,
    MODE_STA,
    DetectResult,
)
from kilnapp.connection import ConnectionManager
from kilnapp.screens.dashboard import DashboardScreen
from kilnapp.screens.placeholder import PlaceholderScreen
from kilnapp.screens.settings import SettingsScreen
from kilnapp.storage import SettingsStore
from kilnapp.widgets.bottom_nav import BottomNav
from kilnapp.widgets.top_bar import TopBar


# Phone-shaped window for desktop development.
Window.size = (390, 780)


# Tabs that are still placeholders in this phase
PLACEHOLDER_DEFS = [
    ("history", "History", "Time-series plots from /history. (Phase 7)"),
    ("alerts", "Alerts", "Warnings and errors from the kiln. (Phase 5)"),
    ("runs", "Runs", "Past and current drying runs. (Phase 6)"),
]
TAB_TITLES = {sn: title for sn, title, _ in PLACEHOLDER_DEFS}
TAB_TITLES["dashboard"] = "Dashboard"
TAB_TITLES["settings"] = "Settings"


class _Root(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(orientation="vertical", **kwargs)
        with self.canvas.before:
            self._bg_color = Color(*theme.BG_DARK)
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(
            pos=lambda w, v: setattr(self._bg_rect, "pos", v),
            size=lambda w, v: setattr(self._bg_rect, "size", v),
        )


class KilnApp(App):
    title = "Kiln Controller"

    def build(self):
        # Persistent settings + connection manager
        self.settings_store = SettingsStore(self.user_data_dir)
        self.connection = ConnectionManager(self.settings_store)

        root = _Root()

        # Top bar
        self.top_bar = TopBar()
        root.add_widget(self.top_bar)

        # Screen manager - Dashboard first so the bottom-nav default lands there
        self.screen_manager = ScreenManager(transition=NoTransition())
        self.screen_manager.add_widget(DashboardScreen(connection=self.connection))
        for screen_name, title, note in PLACEHOLDER_DEFS:
            self.screen_manager.add_widget(
                PlaceholderScreen(screen_name=screen_name, title=title, note=note)
            )
        self.screen_manager.add_widget(SettingsScreen(connection=self.connection))
        root.add_widget(self.screen_manager)

        # Bottom nav
        self.bottom_nav = BottomNav(on_select=self._switch_screen)
        root.add_widget(self.bottom_nav)

        # Default tab
        self._switch_screen("dashboard")

        # Wire indicator -> connection manager
        self.connection.add_listener(self._on_connection_change)

        # Kick off the first detection cycle
        self.connection.detect()

        return root

    def _switch_screen(self, screen_name: str) -> None:
        if screen_name not in self.screen_manager.screen_names:
            return
        self.screen_manager.current = screen_name
        self.top_bar.set_title(TAB_TITLES.get(screen_name, "Kiln Controller"))

    def _on_connection_change(self, result: DetectResult) -> None:
        # Map detection result to indicator state
        mapping = {
            MODE_DIRECT: "direct",
            MODE_COTTAGE: "cottage",
            MODE_STA: "sta",
            MODE_OFFLINE: "offline",
        }
        self.top_bar.indicator.set_state(mapping.get(result.mode, "offline"))
