"""ConnectionManager - ties storage, API client, and autodetect together.

Owns the single shared `KilnApiClient` and the latest `DetectResult`. Schedules
a 30-second retry whenever the app is offline (per kivy_app_spec.md > "Auto-
refresh retries connection every 30 seconds when offline.")

Listeners (e.g. the connection indicator widget) can subscribe via
`add_listener(callback)`. The callback receives a single `DetectResult` arg
on the Kivy main thread whenever detection completes.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Callable, List

from kivy.clock import Clock
from kivy.logger import Logger

from kilnapp.api.autodetect import (
    DetectResult,
    MODE_COTTAGE,
    MODE_OFFLINE,
    autodetect,
    is_direct_mode,
)
from kilnapp.api.client import KilnApiClient, call_async
from kilnapp.storage import Settings, SettingsStore


OFFLINE_RETRY_S = 30.0
# Don't re-push the RTC more often than this - the Pico's drift over a
# single session is small and the POST is a disruptive operation when the
# Pico is busy. 6h gives "once per day when the user opens the app" plus a
# safety margin for long-running sessions.
RTC_SYNC_MIN_INTERVAL_S = 6 * 3600
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
                Logger.warning(f"kiln: connection listener error: {e}")

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
            else:
                # We have a live connection - push our wall clock up to
                # the Pico if auto-sync is on and we haven't done it
                # recently. The Pico has no battery-backed RTC, so its
                # clock drifts or resets on every power cycle; without
                # this the event log timestamps and ntfy alerts are
                # junk. Pi4 mode is also OK to sync - the Pi4 /time
                # endpoint is a no-op if present, or the call just
                # fails harmlessly.
                self._maybe_sync_rtc()

        call_async(work, done)

    def _maybe_sync_rtc(self) -> None:
        """If auto-sync is enabled, decide whether to POST current unix
        time to the Pico's /time endpoint.

        The 6-hour rate limit assumes the Pico retains its clock between
        syncs. It doesn't - the Pico 2 W has no battery-backed RTC, so
        every power cycle drops it back to year 2021 regardless of when
        the app last synced. Before applying the rate limit, fetch
        /health and bypass the limit when the Pico reports rtc_set=False.
        """
        if not self.settings.auto_sync_rtc:
            return
        # Only AP/STA modes support /time (Pi4 daemon is read-only).
        if not is_direct_mode(self.last_result.mode):
            return
        client_snapshot = self.client

        def work():
            # Pull /health to see if the Pico's RTC is set. Falls back
            # to (None, None) on any error; the caller treats that as
            # "assume rtc is set and respect the rate limit".
            try:
                h = client_snapshot.health_current()
                rtc_set = bool((h or {}).get("rtc_set"))
                return (True, rtc_set)
            except Exception as e:  # noqa: BLE001
                Logger.warning(f"kiln: /health probe for RTC failed: {e}")
                return (False, None)

        def decide(result, _err):
            ok, rtc_set = result if result is not None else (False, None)
            now_s = int(time.time())
            age = now_s - int(self.settings.last_rtc_sync or 0)
            within_window = (
                age < RTC_SYNC_MIN_INTERVAL_S and self.settings.last_rtc_sync
            )
            # Force a sync when the Pico says its clock is unset, even
            # if we synced recently from this app's perspective. This
            # fires after any Pico power cycle.
            force = ok and rtc_set is False
            if within_window and not force:
                return
            self._push_rtc(now_s)

        call_async(work, decide)

    def _push_rtc(self, now_s: int) -> None:
        client_snapshot = self.client

        def work():
            return client_snapshot.set_time(now_s)

        def done(_result, err):
            if err is not None:
                Logger.warning(f"kiln: RTC sync failed: {err}")
                return
            # Persist the successful sync time so we don't hammer /time
            # on every reconnect. dataclasses.replace preserves any field
            # added later without needing to update this site.
            self.settings = dataclasses.replace(self.settings, last_rtc_sync=now_s)
            self.store.save(self.settings)
            Logger.info(f"kiln: RTC synced to unix ts {now_s}")

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
