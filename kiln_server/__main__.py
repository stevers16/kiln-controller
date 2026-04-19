"""kiln_server entry point.

Wires the SX1278 radio, SQLite database, ntfy notifier, and Flask API
together, then runs until SIGTERM.

Run with:
    python3 -m kiln_server
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time

from . import config
from .api import create_app
from .database import Database
from .lora_receiver import LoraReceiver, SX1278
from .notifier import Notifier

log = logging.getLogger("kiln_server")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def main() -> int:
    _configure_logging()
    log.info("kiln_server starting (environment=%s)", config.ENVIRONMENT)

    db = Database(config.DB_PATH)
    notifier = Notifier(
        url=config.NTFY_URL,
        topic=config.NTFY_TOPIC,
        suppress_s=config.ALERT_SUPPRESS_S,
    )

    # --- Radio -------------------------------------------------------------
    try:
        radio = SX1278(
            spi_bus=config.SPI_BUS,
            spi_ce=config.SPI_CE,
            rst_pin=config.RST_PIN,
            dio0_pin=config.DIO0_PIN,
            freq_hz=int(config.LORA_FREQ_MHZ * 1_000_000),
        )
    except ImportError as e:
        log.error("kiln_server: Pi-only modules missing (%s). "
                  "Install spidev and RPi.GPIO on the Pi4.", e)
        return 1
    except Exception as e:
        log.exception("kiln_server: failed to open SX1278 radio: %s", e)
        return 1

    radio.reset()
    version = radio.read_reg(0x42)
    if version != 0x12:
        log.error("kiln_server: SX1278 version register=0x%02X (expected 0x12). "
                  "Check wiring and that SPI is enabled.", version)
        radio.close()
        return 1
    log.info("kiln_server: SX1278 detected (ver 0x%02X)", version)
    radio.configure_rx(sf=config.LORA_SF, cr=config.LORA_CR)

    # --- Receiver thread ---------------------------------------------------
    receiver = LoraReceiver(radio=radio, db=db, notifier=notifier)
    receiver.start()

    # --- Flask API ---------------------------------------------------------
    app = create_app(db=db, receiver=receiver, environment=config.ENVIRONMENT)

    stop_event = threading.Event()

    def _shutdown(signum, frame):
        log.info("kiln_server: signal %d received, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Flask's dev server is fine for a LAN-only, low-QPS API on the Pi4.
    # use_reloader must be False so we stay a single process and signals land
    # on our handler above.
    def _serve() -> None:
        try:
            app.run(
                host=config.API_HOST,
                port=config.API_PORT,
                threaded=True,
                use_reloader=False,
                debug=False,
            )
        except Exception as e:
            log.exception("kiln_server: API server crashed: %s", e)
            stop_event.set()

    api_thread = threading.Thread(target=_serve, name="flask-api", daemon=True)
    api_thread.start()
    log.info("kiln_server: REST API on http://%s:%d",
             config.API_HOST, config.API_PORT)

    # --- Wait for shutdown -------------------------------------------------
    try:
        while not stop_event.is_set():
            time.sleep(1)
    finally:
        log.info("kiln_server: stopping receiver and closing radio")
        receiver.stop()
        receiver.join(timeout=3)
        radio.close()
        db.close()

    log.info("kiln_server: exited cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
