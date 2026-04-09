"""HTTP client for the Pico AP and Pi4 daemon REST APIs.

Phase 2: only the methods autodetect + Settings need are implemented (`health`,
`status`). Later phases extend this with `history`, `alerts`, `runs`, `start`,
`stop`, `advance`, etc.

Threading model
---------------
`requests` is blocking, but Kivy's main thread must never block. The
`call_async` helper runs a function on a worker thread and delivers the
result to the Kivy main thread via `Clock.schedule_once`. All callers in the
app should use `call_async`; never call client methods on the main thread.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

import requests
from kivy.clock import Clock


DEFAULT_TIMEOUT_S = 5.0
PROBE_TIMEOUT_S = 3.0


class ApiError(Exception):
    """Anything that prevented us from getting a usable response."""


class AuthError(ApiError):
    """HTTP 401 - bad or missing API key."""


class TimeoutError_(ApiError):
    """Network timeout."""


@dataclass
class ClientConfig:
    base_url: Optional[str] = None
    api_key: str = ""
    requires_auth: bool = True   # Pico requires it; Pi4 does not
    timeout: float = DEFAULT_TIMEOUT_S


class KilnApiClient:
    """Thin wrapper over `requests`.

    A single instance is shared by the whole app. The `ConnectionManager`
    rewrites `config.base_url`, `config.api_key`, and `config.requires_auth`
    every time autodetect picks a new endpoint.
    """

    def __init__(self) -> None:
        self.config = ClientConfig()

    # ---- low-level helpers -------------------------------------------------

    def _headers(self, auth_required: bool) -> dict:
        h = {"Accept": "application/json"}
        if auth_required and self.config.requires_auth and self.config.api_key:
            h["X-Kiln-Key"] = self.config.api_key
        return h

    def _get(
        self,
        path: str,
        *,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        auth: bool = True,
    ) -> Any:
        url = (base_url or self.config.base_url or "").rstrip("/") + path
        if not url.startswith("http"):
            raise ApiError("no base url configured")
        try:
            resp = requests.get(
                url,
                headers=self._headers(auth),
                timeout=timeout or self.config.timeout,
            )
        except requests.Timeout as e:
            raise TimeoutError_(str(e)) from e
        except requests.RequestException as e:
            raise ApiError(str(e)) from e

        if resp.status_code == 401:
            raise AuthError("unauthorized (HTTP 401)")
        if resp.status_code >= 400:
            raise ApiError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as e:
            raise ApiError(f"non-JSON response: {e}") from e

    # ---- public endpoints (Phase 2 subset) ---------------------------------

    def health(
        self,
        *,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """GET /health - no auth on Pico, no auth on Pi4."""
        return self._get("/health", base_url=base_url, timeout=timeout, auth=False)

    def status(self) -> Any:
        """GET /status - requires auth on Pico."""
        return self._get("/status")

    def health_current(self) -> Any:
        """GET /health on the currently configured base URL."""
        return self._get("/health", auth=False)

    # ---- threading helper --------------------------------------------------


def call_async(
    func: Callable[[], Any],
    on_result: Callable[[Any, Optional[Exception]], None],
) -> None:
    """Run `func` on a worker thread; deliver `(result, error)` to the Kivy
    main thread via Clock.schedule_once.

    Exactly one of result/error will be non-None.
    """

    def worker() -> None:
        try:
            result = func()
            err: Optional[Exception] = None
        except Exception as e:  # noqa: BLE001 - we deliberately catch all
            result = None
            err = e

        def deliver(_dt: float) -> None:
            on_result(result, err)

        Clock.schedule_once(deliver, 0)

    threading.Thread(target=worker, daemon=True).start()
