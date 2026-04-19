"""Connection mode autodetect.

Implements the probe order from kivy_app_spec.md > "Auto-detect with manual
override":

    1. Pico AP   (default 192.168.4.1:80)   GET /health, 3s timeout
    2. Pi4       (user IP, port 8080)       GET /health, 3s timeout
    3. Pico STA  (default 10.0.0.24:80)     GET /health, 3s timeout
    4. otherwise -> Offline

Override modes (from the Settings > Mode dropdown):

    auto     - try all three in order (default)
    direct   - Pico AP first, Pico STA fallback (never Pi4)
    sta      - Pico STA only (needed when Pi4 is up on the same LAN;
               Auto would land on Pi4 first and never reach the Pico)
    cottage  - Pi4 only

When forced, we do NOT fall back to the other endpoints.

This module never touches Kivy. It is called from a worker thread by
ConnectionManager and the result is delivered to the main thread via
`call_async`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from kilnapp.api.client import KilnApiClient, PROBE_TIMEOUT_S
from kilnapp.storage import (
    OVERRIDE_AUTO,
    OVERRIDE_COTTAGE,
    OVERRIDE_DIRECT,
    OVERRIDE_STA,
    Settings,
)


# Mode strings - used by the indicator widget too
MODE_DIRECT = "direct"     # Pico AP
MODE_COTTAGE = "cottage"   # Pi4 daemon
MODE_STA = "sta"           # Pico station mode
MODE_OFFLINE = "offline"


@dataclass
class DetectResult:
    mode: str                       # one of MODE_*
    base_url: Optional[str]         # None if offline
    requires_auth: bool             # True for Pico, False for Pi4

    @classmethod
    def offline(cls) -> "DetectResult":
        return cls(mode=MODE_OFFLINE, base_url=None, requires_auth=False)


def _url(host: str, port: int) -> Optional[str]:
    host = (host or "").strip()
    if not host:
        return None
    return f"http://{host}:{int(port)}"


def _probe(client: KilnApiClient, url: str) -> bool:
    try:
        client.health(base_url=url, timeout=PROBE_TIMEOUT_S)
        return True
    except Exception:
        return False


def autodetect(client: KilnApiClient, settings: Settings) -> DetectResult:
    """Probe endpoints in order and return the first one that responds.

    Honours `settings.connection_override`. When forced, only the matching
    endpoint(s) are probed and Offline is returned if none answer.
    """
    pico_ap = _url(settings.pico_ip, settings.pico_port)
    pi4 = _url(settings.pi4_ip, settings.pi4_port)
    pico_sta = _url(settings.pico_sta_ip, settings.pico_port)

    override = settings.connection_override or OVERRIDE_AUTO

    if override == OVERRIDE_DIRECT:
        # Force Direct: try Pico AP, then Pico STA. Never fall back to Pi4.
        if pico_ap and _probe(client, pico_ap):
            return DetectResult(MODE_DIRECT, pico_ap, requires_auth=True)
        if pico_sta and _probe(client, pico_sta):
            return DetectResult(MODE_STA, pico_sta, requires_auth=True)
        return DetectResult.offline()

    if override == OVERRIDE_STA:
        # Force STA: only try the Pico STA IP. Exists because Auto will
        # always land on Pi4 (middle of the probe order) when the Pi4
        # daemon is up on the same LAN, leaving no way to reach the
        # Pico directly for API work.
        if pico_sta and _probe(client, pico_sta):
            return DetectResult(MODE_STA, pico_sta, requires_auth=True)
        return DetectResult.offline()

    if override == OVERRIDE_COTTAGE:
        # Force Cottage: only try Pi4.
        if pi4 and _probe(client, pi4):
            return DetectResult(MODE_COTTAGE, pi4, requires_auth=False)
        return DetectResult.offline()

    # Auto: spec order is Pico AP -> Pi4 -> Pico STA
    if pico_ap and _probe(client, pico_ap):
        return DetectResult(MODE_DIRECT, pico_ap, requires_auth=True)
    if pi4 and _probe(client, pi4):
        return DetectResult(MODE_COTTAGE, pi4, requires_auth=False)
    if pico_sta and _probe(client, pico_sta):
        return DetectResult(MODE_STA, pico_sta, requires_auth=True)
    return DetectResult.offline()
