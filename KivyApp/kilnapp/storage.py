"""Persistent local settings for the Kiln Controller app.

Wraps `kivy.storage.jsonstore.JsonStore` so the rest of the app sees a single
typed object. Stored under `App.user_data_dir/settings.json`. Keys come from
the spec section "Local Storage (app-side)" plus a couple of internal additions
(pico_port, pico_sta_ip) needed by autodetect.

The API key is obfuscated (NOT encrypted) before being written to disk so it
does not appear in plaintext in the settings file. This matches the spec's
"store obfuscated, not plaintext" requirement and is not intended as real
security.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from kivy.storage.jsonstore import JsonStore


SETTINGS_FILE = "settings.json"
SETTINGS_KEY = "settings"  # JsonStore is key/value; we use one composite key

# Light obfuscation for the API key on disk. Not real security.
_OBF_XOR = 0x5A


def _obfuscate(plain: str) -> str:
    if not plain:
        return ""
    raw = bytes(b ^ _OBF_XOR for b in plain.encode("utf-8"))
    return base64.b64encode(raw).decode("ascii")


def _deobfuscate(obf: str) -> str:
    if not obf:
        return ""
    try:
        raw = base64.b64decode(obf.encode("ascii"))
    except Exception:
        return ""
    return bytes(b ^ _OBF_XOR for b in raw).decode("utf-8", errors="replace")


# Allowed values for connection_override
OVERRIDE_AUTO = "auto"
OVERRIDE_DIRECT = "direct"
OVERRIDE_COTTAGE = "cottage"
OVERRIDES = (OVERRIDE_AUTO, OVERRIDE_DIRECT, OVERRIDE_COTTAGE)


@dataclass
class Settings:
    pico_ip: str = "192.168.4.1"
    pico_port: int = 80
    pico_sta_ip: str = "10.0.0.24"
    pi4_ip: str = ""
    pi4_port: int = 8080
    api_key: str = ""              # plaintext in memory; obfuscated on disk
    connection_override: str = OVERRIDE_AUTO
    last_rtc_sync: int = 0         # unix timestamp
    auto_sync_rtc: bool = True

    def normalised(self) -> "Settings":
        """Return a copy with whitespace stripped and override clamped."""
        s = Settings(
            pico_ip=(self.pico_ip or "").strip(),
            pico_port=int(self.pico_port or 80),
            pico_sta_ip=(self.pico_sta_ip or "").strip(),
            pi4_ip=(self.pi4_ip or "").strip(),
            pi4_port=int(self.pi4_port or 8080),
            api_key=self.api_key or "",
            connection_override=self.connection_override
            if self.connection_override in OVERRIDES
            else OVERRIDE_AUTO,
            last_rtc_sync=int(self.last_rtc_sync or 0),
            auto_sync_rtc=bool(self.auto_sync_rtc),
        )
        return s


class SettingsStore:
    """Load / save the Settings dataclass via Kivy JsonStore."""

    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, SETTINGS_FILE)
        self._store = JsonStore(self.path)

    def load(self) -> Settings:
        if not self._store.exists(SETTINGS_KEY):
            return Settings()
        raw = self._store.get(SETTINGS_KEY)
        defaults = Settings()
        return Settings(
            pico_ip=raw.get("pico_ip", defaults.pico_ip),
            pico_port=int(raw.get("pico_port", defaults.pico_port)),
            pico_sta_ip=raw.get("pico_sta_ip", defaults.pico_sta_ip),
            pi4_ip=raw.get("pi4_ip", defaults.pi4_ip),
            pi4_port=int(raw.get("pi4_port", defaults.pi4_port)),
            api_key=_deobfuscate(raw.get("api_key_obf", "")),
            connection_override=raw.get(
                "connection_override", defaults.connection_override
            ),
            last_rtc_sync=int(raw.get("last_rtc_sync", 0)),
            auto_sync_rtc=bool(raw.get("auto_sync_rtc", True)),
        )

    def save(self, settings: Settings) -> None:
        s = settings.normalised()
        self._store.put(
            SETTINGS_KEY,
            pico_ip=s.pico_ip,
            pico_port=s.pico_port,
            pico_sta_ip=s.pico_sta_ip,
            pi4_ip=s.pi4_ip,
            pi4_port=s.pi4_port,
            api_key_obf=_obfuscate(s.api_key),
            connection_override=s.connection_override,
            last_rtc_sync=s.last_rtc_sync,
            auto_sync_rtc=s.auto_sync_rtc,
        )
