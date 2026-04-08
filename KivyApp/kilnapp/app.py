"""Kiln Controller - Kivy App root.

Phase 1: bottom-nav shell with 5 placeholder screens, a top app bar, and a
static (Offline / red) connection indicator. No network code yet.
"""

from kivy.app import App
from kivy.core.window import Window
from kivy.graphics import Color, Rectangle
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.screenmanager import NoTransition, ScreenManager

from kilnapp import theme
from kilnapp.screens.placeholder import PlaceholderScreen
from kilnapp.widgets.bottom_nav import BottomNav
from kilnapp.widgets.top_bar import TopBar


# Phone-shaped window for desktop development. Real device sets its own size.
Window.size = (390, 780)


# Placeholder copy for each tab. Will be replaced by real screens later.
SCREEN_DEFS = [
    ("dashboard", "Dashboard", "Live status, sensor readings, equipment state. (Phase 3-4)"),
    ("history", "History", "Time-series plots from /history. (Phase 7)"),
    ("alerts", "Alerts", "Warnings and errors from the kiln. (Phase 5)"),
    ("runs", "Runs", "Past and current drying runs. (Phase 6)"),
    ("settings", "Settings", "Connection, API key, RTC sync, daemon info. (Phase 2)"),
]


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
        root = _Root()

        # Top bar
        self.top_bar = TopBar()
        root.add_widget(self.top_bar)

        # Screen manager with one Screen per tab
        self.screen_manager = ScreenManager(transition=NoTransition())
        for screen_name, title, note in SCREEN_DEFS:
            self.screen_manager.add_widget(
                PlaceholderScreen(screen_name=screen_name, title=title, note=note)
            )
        root.add_widget(self.screen_manager)

        # Bottom nav
        self.bottom_nav = BottomNav(on_select=self._switch_screen)
        root.add_widget(self.bottom_nav)

        # Default tab
        self._switch_screen("dashboard")

        # Phase 1: indicator is hard-coded to Offline. Phase 2 replaces this
        # with the autodetect result.
        self.top_bar.indicator.set_state("offline")

        return root

    def _switch_screen(self, screen_name: str) -> None:
        if screen_name not in self.screen_manager.screen_names:
            return
        self.screen_manager.current = screen_name
        # Update the top-bar title to match the active tab
        for sn, title, _note in SCREEN_DEFS:
            if sn == screen_name:
                self.top_bar.set_title(title)
                break
