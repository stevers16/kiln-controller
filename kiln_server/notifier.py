"""ntfy.sh push-notification helper with per-code suppression.

The daemon POSTs directly to ntfy.sh when an alert arrives. Notifications
never block the receive loop and never raise: HTTP errors are logged and
swallowed.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

try:
    import requests
except ImportError:  # keeps tests importable on dev machines without requests
    requests = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# Alerts in this set ignore the suppression window.
ONE_SHOT_CODES = {"run_complete", "equalizing_start", "conditioning_start",
                  "run_started"}


class Notifier:
    def __init__(
        self,
        url: str,
        topic: str,
        suppress_s: int = 1800,
    ):
        self.url = url.rstrip("/")
        self.topic = topic
        self.suppress_s = int(suppress_s)
        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()

    def send(self, code: str, message: Optional[str] = None) -> bool:
        """POST an alert to ntfy.sh. Returns True on success, False otherwise.

        Suppresses duplicate codes within `suppress_s` unless the code is a
        one-shot lifecycle alert.
        """
        if not self._should_send(code):
            log.debug("notifier: suppressing duplicate %s", code)
            return False

        if requests is None:
            log.warning("notifier: requests not installed; cannot send %s", code)
            return False

        body = f"{code}: {message}" if message else code
        try:
            resp = requests.post(
                f"{self.url}/{self.topic}",
                data=body.encode("utf-8"),
                headers={
                    "Priority": "high",
                    "Tags": "warning",
                    "Title": "Kiln Alert",
                },
                timeout=5,
            )
            if resp.status_code >= 400:
                log.warning(
                    "notifier: ntfy returned %d for %s", resp.status_code, code
                )
                return False
            return True
        except Exception as e:     # never let a push failure kill the daemon
            log.warning("notifier: post failed for %s: %s", code, e)
            return False

    # --- internal ----------------------------------------------------------

    def _should_send(self, code: str) -> bool:
        if code in ONE_SHOT_CODES:
            return True
        now = time.time()
        with self._lock:
            last = self._last_sent.get(code, 0.0)
            if now - last < self.suppress_s:
                return False
            self._last_sent[code] = now
        return True
