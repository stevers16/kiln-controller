"""kiln_server configuration.

This is the only file that differs between bench and cottage deployments.
Edit the values below to match the target Pi4.
"""

# "bench" or "cottage" -- surfaced in /health so the Kivy app can show which
# Pi4 it is talking to.
ENVIRONMENT = "cottage"

# --- LoRa SPI wiring (BCM GPIO numbering) ------------------------------------
SPI_BUS = 0
SPI_CE = 0  # CE0 -> GPIO8
DIO0_PIN = 25  # RX-done interrupt
RST_PIN = 17  # Ra-02 reset (active low)

# --- LoRa RF parameters (must match Pico TX side) ----------------------------
LORA_FREQ_MHZ = 433.0
LORA_SF = 9
LORA_BW_HZ = 125_000
LORA_CR = 5  # 4/5

# --- Database ---------------------------------------------------------------
DB_PATH = "/home/srelias/CottageKiln/kiln_data.db"

# --- REST API ---------------------------------------------------------------
API_HOST = "0.0.0.0"
API_PORT = 8080

# --- ntfy.sh notifications --------------------------------------------------
NTFY_URL = "https://ntfy.sh"
NTFY_TOPIC = "kiln-cottage-abc123"  # choose a unique topic name

# Do not re-notify the same alert code within this window (seconds).
# One-shot alerts (run_complete, equalizing_start, conditioning_start) bypass.
ALERT_SUPPRESS_S = 1800
