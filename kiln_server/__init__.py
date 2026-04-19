"""kiln_server -- Pi4 cottage daemon for the solar wood drying kiln.

Receives LoRa telemetry from the Pico, stores it in SQLite, serves a
read-only REST API on port 8080, and pushes fault alerts to ntfy.sh.

Run as:  python3 -m kiln_server
"""

__version__ = "0.1.0"
