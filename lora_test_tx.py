# lora_test_tx.py -- Pico-side LoRa TX test
#
# Sends a small numbered message every 5 seconds using lib/lora.py.
# Run on the Pico with:   mpremote run lora_test_tx.py
#
# Expected output on Pi4 receiver: numbered JSON messages with
# incrementing sequence numbers and timestamps.

import time
from lib.lora import LoRa

try:
    import ujson as json
except ImportError:
    import json


def main():
    print("=== LoRa TX Test (Pico) ===")
    print("Initialising radio...")

    lora = LoRa()

    if not lora.is_initialised:
        print("FAIL: Radio did not initialise. Check wiring.")
        return

    print("Radio ready")
    print("Sending a message every 5 seconds. Ctrl-C to stop.\n")

    seq = 0
    while True:
        seq += 1
        msg = {
            "seq": seq,
            "ts": time.time(),
            "msg": f"hello from pico #{seq}",
        }
        payload = json.dumps(msg).encode()
        ok = lora.send(payload)
        status = "OK" if ok else "FAIL"
        print(f"  TX #{seq} ({len(payload)} bytes) -> {status}")
        time.sleep(5)


try:
    main()
except KeyboardInterrupt:
    print("\nStopped by user.")
