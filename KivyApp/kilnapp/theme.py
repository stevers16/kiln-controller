"""Shared colour and sizing constants for the Kiln Controller app.

Centralised here so screens stay visually consistent and so we can tweak
the palette in one place when we revisit styling later.
"""

from kivy.metrics import dp


# Backgrounds
BG_DARK = (0.10, 0.11, 0.13, 1)        # app background
BG_PANEL = (0.15, 0.16, 0.19, 1)       # cards, top/bottom bars
BG_PANEL_ACTIVE = (0.22, 0.24, 0.29, 1)  # selected nav tab

# Text
TEXT_PRIMARY = (0.95, 0.95, 0.97, 1)
TEXT_SECONDARY = (0.65, 0.66, 0.70, 1)
TEXT_MUTED = (0.45, 0.46, 0.50, 1)

# Status / connection-mode dots (matches spec section "Connection Modes")
DOT_DIRECT = (0.20, 0.78, 0.35, 1)   # green - Pico AP
DOT_COTTAGE = (0.25, 0.55, 0.95, 1)  # blue  - Pi4 daemon
DOT_STA = (0.95, 0.80, 0.20, 1)      # yellow - Pico STA
DOT_OFFLINE = (0.85, 0.25, 0.25, 1)  # red - no connection

# Severity (used later for alerts/banners)
SEVERITY_OK = (0.20, 0.78, 0.35, 1)
SEVERITY_WARN = (0.95, 0.65, 0.15, 1)
SEVERITY_NOTICE = (0.78, 0.55, 0.10, 1)
SEVERITY_ERROR = (0.85, 0.25, 0.25, 1)

# Sizing - density-independent so the bars are physically the same size on
# desktop and Android. Raw pixel values render as thin strips on high-DPI
# phones (3x density) and disappear behind the system status bar and the
# bottom gesture indicator.
TOP_BAR_HEIGHT = dp(56)
BOTTOM_NAV_HEIGHT = dp(64)
