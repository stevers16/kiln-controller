"""Kiln Controller - Kivy App root.

Phase 7: the five-tab shell is now fully wired - Dashboard, History, Alerts,
Runs, Settings are all real screens. No placeholders remain.
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
from kilnapp.screens.alerts import AlertsScreen
from kilnapp.screens.dashboard import DashboardScreen
from kilnapp.screens.history import HistoryScreen
from kilnapp.screens.runs import RunsScreen
from kilnapp.screens.settings import SettingsScreen
from kilnapp.screens.start_run import StartRunScreen
from kilnapp.storage import SettingsStore
from kilnapp.widgets.bottom_nav import BottomNav
from kilnapp.widgets.top_bar import TopBar


# Phone-shaped window for desktop development.
Window.size = (390, 780)


TAB_TITLES = {
    "dashboard": "Dashboard",
    "history": "History",
    "alerts": "Alerts",
    "runs": "Runs",
    "settings": "Settings",
    "start_run": "Start Run",
}


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
        self.screen_manager.add_widget(
            DashboardScreen(
                connection=self.connection,
                on_navigate=self._navigate_to,
            )
        )
        self.history_screen = HistoryScreen(connection=self.connection)
        self.screen_manager.add_widget(self.history_screen)
        self.alerts_screen = AlertsScreen(connection=self.connection)
        self.screen_manager.add_widget(self.alerts_screen)
        self.screen_manager.add_widget(
            RunsScreen(connection=self.connection, on_navigate=self._navigate_to)
        )
        self.screen_manager.add_widget(SettingsScreen(connection=self.connection))
        # AP-only Start Run wizard. Not a bottom-nav tab; reached from the
        # Dashboard's "Start Run" action button.
        self.screen_manager.add_widget(
            StartRunScreen(
                connection=self.connection,
                on_finish=lambda: self._navigate_to("dashboard"),
            )
        )
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

    def _navigate_to(self, screen_name: str, **kwargs) -> None:
        """Programmatic navigation: switch the screen AND sync the bottom nav.

        Used by inter-screen actions like the fault banner -> Alerts tap.
        Accepts keyword args per target screen:
          - "history": optional `run_id` to preselect in the run dropdown.
          - "alerts":  optional `run_id` to filter the alerts list to.
        """
        if screen_name == "history":
            run_id = kwargs.get("run_id")
            if run_id is not None:
                self.history_screen.preselect_run(run_id)
        elif screen_name == "alerts":
            run_id = kwargs.get("run_id")
            if run_id is not None:
                self.alerts_screen.preselect_run(run_id)
        self._switch_screen(screen_name)
        self.bottom_nav.select(screen_name)

    def _on_connection_change(self, result: DetectResult) -> None:
        # Map detection result to indicator state
        mapping = {
            MODE_DIRECT: "direct",
            MODE_COTTAGE: "cottage",
            MODE_STA: "sta",
            MODE_OFFLINE: "offline",
        }
        self.top_bar.indicator.set_state(mapping.get(result.mode, "offline"))
