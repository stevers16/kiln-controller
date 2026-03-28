# config.py -- deploy to Pico root
# Change AP_PASSWORD and API_KEY before first deployment.

VERSION = "1.0.0"

# WiFi AP
AP_SSID = "KilnController"
AP_PASSWORD = "KampSteve"  # WPA2; empty string for open AP

# REST API
API_KEY = "MapleBeech"  # must match Kivy app Settings

# LoRa
USE_MOCK_LORA = True  # set False when Ra-02 hardware is installed
LORA_SF = 9
LORA_FREQ_MHZ = 433.0

# Schedule
DEFAULT_SCHEDULE = "maple_1in.json"

# Display
DISPLAY_TIMEOUT_S = 30  # seconds before backlight off; 0 to disable

# Logging
LOG_FLUSH_INTERVAL_S = 120  # seconds between forced SD flushes
