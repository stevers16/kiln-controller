"""ConnectionManager - ties storage, API client, and autodetect together.

Owns the single shared `KilnApiClient` and the latest `DetectResult`. Schedules
a 30-second retry whenever the app is offline (per kivy_app_spec.md > "Auto-
refresh retries connection every 30 seconds when offline.")

Listeners (e.g. the connection indicator widget) can subscribe via
`add_listener(callback)`. The callback receives a single `DetectResult` arg
on the Kivy main thread whenever detection completes.
"""

from __future__ import annotations

import time
from typing import Callable, List

from kivy.clock import Clock
from kivy.logger import Logger

from kilnapp.api.autodetect import DetectResult, MODE_OFFLINE, autodetect
from kilnapp.api.client import KilnApiClient, call_async
from kilnapp.storage import Settings, SettingsStore


OFFLINE_RETRY_S = 30.0
Listener = Callable[[DetectResult], None]


class ConnectionManager:
    def __init__(self, store: SettingsStore) -> None:
        self.store = store
        self.settings: Settings = store.load()
        self.client = KilnApiClient()
        self._apply_settings_to_client()
        self.last_result: DetectResult = DetectResult.offline()
        self._listeners: List[Listener] = []
        self._retry_event = None  # Clock event handle

    # ---- listeners ---------------------------------------------------------

    def add_listener(self, callback: Listener) -> None:
        self._listeners.append(callback)
        # Fire immediately with current state so the UI is consistent
        callback(self.last_result)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb(self.last_result)
            except Exception as e:  # noqa: BLE001
                print(f"[connection] listener error: {e}")

    # ---- settings management ----------------------------------------------

    def update_settings(self, new_settings: Settings) -> None:
        """Persist new settings, push them into the client, kick off detect."""
        self.settings = new_settings.normalised()
        self.store.save(self.settings)
        self._apply_settings_to_client()
        self.detect()

    def _apply_settings_to_client(self) -> None:
        # base_url and requires_auth get overwritten by detect(); the api_key
        # comes straight from settings every time.
        self.client.config.api_key = self.settings.api_key

    # ---- detection --------------------------------------------------------

    def detect(self, *, reason: str = "manual") -> None:
        """Run autodetect on a worker thread; update state on completion."""
        self._cancel_retry()

        settings_snapshot = self.settings  # captured by closure
        client_snapshot = self.client
        Logger.info(
            f"kiln: detect start ({reason}) override={settings_snapshot.connection_override} "
            f"pico_ap={settings_snapshot.pico_ip}:{settings_snapshot.pico_port} "
            f"pi4={settings_snapshot.pi4_ip}:{settings_snapshot.pi4_port} "
            f"pico_sta={settings_snapshot.pico_sta_ip}:{settings_snapshot.pico_port}"
        )
        t0 = time.monotonic()

        def work() -> DetectResult:
            return autodetect(client_snapshot, settings_snapshot)

        def done(result, err) -> None:
            elapsed = time.monotonic() - t0
            if err is not None or result is None:
                self.last_result = DetectResult.offline()
                Logger.warning(f"kiln: detect ({reason}) error after {elapsed:.1f}s: {err}")
            else:
                self.last_result = result
                if result.base_url is not None:
                    self.client.config.base_url = result.base_url
                    self.client.config.requires_auth = result.requires_auth
                else:
                    self.client.config.base_url = None
                Logger.info(
                    f"kiln: detect ({reason}) -> {result.mode} ({result.base_url}) in {elapsed:.1f}s"
                )
            self._notify()
            # Schedule a retry if we ended up offline
            if self.last_result.mode == MODE_OFFLINE:
                self._schedule_retry()

        call_async(work, done)

    # ---- retry timer -------------------------------------------------------

    def _schedule_retry(self) -> None:
        self._cancel_retry()
        Logger.info(f"kiln: offline; scheduling retry in {OFFLINE_RETRY_S:.0f}s")
        self._retry_event = Clock.schedule_once(
            lambda _dt: self.detect(reason="retry"), OFFLINE_RETRY_S
        )

    def _cancel_retry(self) -> None:
        if self._retry_event is not None:
            self._retry_event.cancel()
            self._retry_event = None
