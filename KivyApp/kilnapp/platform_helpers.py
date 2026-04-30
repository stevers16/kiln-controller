"""Platform-specific helpers shared across screens.

The Kivy app runs on desktop (Windows / macOS / Linux) and on Android via
buildozer. The two environments differ in three places we care about:

1. Where to write user-visible files (Downloads vs the app sandbox).
2. How to pick a file from the device (Kivy FileChooser vs the system
   document picker / SAF surfaced by plyer).
3. Whether the app needs to request runtime permissions on launch.

This module centralises all three so the screens themselves stay
platform-agnostic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Optional

from kivy.app import App
from kivy.logger import Logger
from kivy.utils import platform


IS_ANDROID = platform == "android"


def download_dir() -> Path:
    """Resolve a writable target directory for user-visible saves.

    Desktop: ``~/Downloads`` if writable, else the app's user_data_dir.
    Android: the app's user_data_dir (private app sandbox). Saving to the
    public Downloads folder requires either WRITE_EXTERNAL_STORAGE on
    legacy API levels or a MediaStore call on API 29+. For Phase 15 we
    keep files inside the sandbox; the user can pull them via USB/MTP or
    we can wire a Share-sheet flow later.
    """
    app = App.get_running_app()
    sandbox = Path(app.user_data_dir if app else ".")

    if IS_ANDROID:
        return sandbox

    target = Path.home() / "Downloads"
    try:
        target.mkdir(parents=True, exist_ok=True)
        return target
    except Exception:
        return sandbox


def request_android_permissions() -> None:
    """Request the runtime permissions the app needs on Android.

    INTERNET and ACCESS_NETWORK_STATE are install-time permissions on
    every API level - declaring them in buildozer.spec is enough; no
    runtime prompt is required. This stays as a hook so future
    permissions (camera, NFC, scoped storage media) can be wired in
    without touching `app.py`.
    """
    if not IS_ANDROID:
        return
    try:
        from android.permissions import request_permissions, Permission  # type: ignore

        request_permissions([Permission.INTERNET])
    except Exception as e:  # noqa: BLE001
        Logger.warning(f"kiln: android permission request failed: {e}")


def pick_file(
    on_select: Callable[[str], None],
    on_cancel: Optional[Callable[[], None]] = None,
    filters: Optional[List[str]] = None,
) -> bool:
    """Open the system file picker on Android.

    Returns True if the platform-native picker was launched, False if
    the caller should fall back to its own (Kivy FileChooser) UI. Plyer
    delivers the chosen path asynchronously via ``on_select(path_str)``;
    on cancel the callback is not fired and we lose the option to react,
    which is fine for our flow.
    """
    if not IS_ANDROID:
        return False
    try:
        from plyer import filechooser  # type: ignore

        def _on_selection(selection):
            if not selection:
                if on_cancel is not None:
                    on_cancel()
                return
            on_select(selection[0])

        filechooser.open_file(on_selection=_on_selection, filters=filters or [])
        return True
    except Exception as e:  # noqa: BLE001
        Logger.warning(f"kiln: plyer filechooser unavailable, falling back: {e}")
        return False
