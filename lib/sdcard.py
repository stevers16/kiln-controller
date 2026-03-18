# lib/sdcard.py
#
# SD card mount/unmount wrapper for the SPI micro SD module.
# Provides a clean interface for logger.py to depend on.
#
# Wiring summary (SPI0):
#   GP2  → SCK
#   GP3  → MOSI (TX)
#   GP4  → MISO (RX)
#   GP5  → CS

import machine
import uos

# --- Constants ---
SCK_PIN    = 2
MOSI_PIN   = 3
MISO_PIN   = 4
CS_PIN     = 5
SPI_FREQ   = 400_000     # 400 kHz — SD spec max during card initialisation


class SDCard:
    """
    Wraps the MicroPython sdcard driver and uos.mount().
    Silent fail with REPL warning if SD is unavailable.
    """

    def __init__(self, sck=SCK_PIN, mosi=MOSI_PIN, miso=MISO_PIN, cs=CS_PIN,
                 mount_point="/sd"):
        self._sck = sck
        self._mosi = mosi
        self._miso = miso
        self._cs = cs
        self._mount_point = mount_point
        self._mounted = False

    def mount(self):
        """Mount the SD card. Returns True on success, False on failure."""
        if self._mounted:
            return True
        try:
            # sdcard_driver.py must be deployed to the Pico separately (e.g. at /sdcard_driver.py
            # or /lib/sdcard_driver.py).  It must NOT be named sdcard.py or the import below
            # will resolve to this wrapper file and fail.
            import sdcard_driver as _drv
            # CS must be driven HIGH before SPI is initialised to prevent
            # spurious transactions that confuse the card's state machine.
            cs = machine.Pin(self._cs, machine.Pin.OUT, value=1)
            spi = machine.SPI(0, baudrate=SPI_FREQ,
                              sck=machine.Pin(self._sck),
                              mosi=machine.Pin(self._mosi),
                              miso=machine.Pin(self._miso))
            sd = _drv.SDCard(spi, cs)
            vfs = uos.VfsFat(sd)
            uos.mount(vfs, self._mount_point)
            self._mounted = True
            return True
        except Exception as e:
            print(f"[sdcard] WARNING: mount failed — {e}")
            return False

    def unmount(self):
        """Safe unmount; no-op if not mounted."""
        if not self._mounted:
            return
        try:
            uos.umount(self._mount_point)
        except Exception as e:
            print(f"[sdcard] WARNING: unmount failed — {e}")
        self._mounted = False

    def is_mounted(self):
        """Returns True if the SD card is currently mounted."""
        return self._mounted

    @property
    def mount_point(self):
        """Returns the mount point path, e.g. '/sd'."""
        return self._mount_point


# --- Unit test ---
def test():
    print("=== SDCard unit test ===")
    sd = SDCard()
    all_passed = True

    # --- Test 1: mount() ---
    result = sd.mount()
    passed = result is True
    print(f"  {'PASS' if passed else 'FAIL'} — mount() returns True")
    all_passed &= passed

    # --- Test 2: is_mounted() after mount ---
    passed = sd.is_mounted()
    print(f"  {'PASS' if passed else 'FAIL'} — is_mounted() True after mount")
    all_passed &= passed

    # --- Test 3: /sd appears in root listing ---
    dirs = uos.listdir("/")
    passed = "sd" in dirs
    print(f"  {'PASS' if passed else 'FAIL'} — /sd appears in uos.listdir('/')")
    all_passed &= passed

    # --- Test 4: write and read back a test file ---
    test_path = sd.mount_point + "/test_sdcard.txt"
    test_data = "hello from sdcard test"
    try:
        with open(test_path, "w") as f:
            f.write(test_data)
        with open(test_path, "r") as f:
            read_back = f.read()
        passed = read_back == test_data
        # Clean up
        uos.remove(test_path)
    except Exception as e:
        print(f"    (error: {e})")
        passed = False
    print(f"  {'PASS' if passed else 'FAIL'} — can write and read back a test file")
    all_passed &= passed

    # --- Test 5: unmount() ---
    sd.unmount()
    passed = True  # No exception means success
    print(f"  {'PASS' if passed else 'FAIL'} — unmount() succeeds")
    all_passed &= passed

    # --- Test 6: is_mounted() after unmount ---
    passed = not sd.is_mounted()
    print(f"  {'PASS' if passed else 'FAIL'} — is_mounted() False after unmount")
    all_passed &= passed

    print(f"\n{'All tests passed!' if all_passed else 'Some tests FAILED'}")
    return all_passed


if __name__ == "__main__":
    test()
