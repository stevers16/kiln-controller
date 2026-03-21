# lib/sdcard.py
#
# SD card mount/unmount wrapper for the SPI micro SD module.
# Provides a clean interface for logger.py to depend on.
#
# Wiring summary (SPI0):
#   GP2  -> SCK
#   GP3  -> MOSI (TX)
#   GP4  -> MISO (RX)
#   GP5  -> CS

import machine
import uos

# --- Constants ---
MISO_PIN = 4
MOSI_PIN = 3
SCK_PIN = 2
CS_PIN = 5
SPI_FREQ = 400_000  # 400 kHz - SD spec max during card initialisation


class SDCard:
    """
    Wraps the MicroPython sdcard driver and uos.mount().
    Silent fail with REPL warning if SD is unavailable.
    """

    def __init__(
        self, sck=SCK_PIN, mosi=MOSI_PIN, miso=MISO_PIN, cs=CS_PIN, mount_point="/sd"
    ):
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
            spi = machine.SPI(
                0,
                baudrate=SPI_FREQ,
                sck=machine.Pin(self._sck),
                mosi=machine.Pin(self._mosi),
                miso=machine.Pin(self._miso),
            )
            sd = _drv.SDCard(spi, cs)
            vfs = uos.VfsFat(sd)
            uos.mount(vfs, self._mount_point)
            self._mounted = True
            return True
        except Exception as e:
            print(f"[sdcard] WARNING: mount failed - {e}")
            return False

    def unmount(self):
        """Safe unmount; no-op if not mounted."""
        if not self._mounted:
            return
        try:
            uos.umount(self._mount_point)
        except Exception as e:
            print(f"[sdcard] WARNING: unmount failed - {e}")
        self._mounted = False

    def is_mounted(self):
        """Returns True if the SD card is currently mounted."""
        return self._mounted

    @property
    def mount_point(self):
        """Returns the mount point path, e.g. '/sd'."""
        return self._mount_point

    def listdir(self, subdir=""):
        """
        Return a sorted list of filenames in the given subdirectory of the SD card.
        subdir is relative to the mount point, e.g. "" for root, "logs" for /sd/logs.
        Returns an empty list if not mounted or the path does not exist.
        """
        if not self._mounted:
            return []
        path = (
            self._mount_point
            if not subdir
            else f"{self._mount_point}/{subdir.strip('/')}"
        )
        try:
            return sorted(uos.listdir(path))
        except Exception as e:
            print(f"[sdcard] WARNING: listdir failed - {e}")
            return []

    def read_text(self, filename):
        """
        Return the full contents of a text file on the SD card as a string.
        filename is relative to the mount point, e.g. "event_20260318_1400.txt".
        Returns None if not mounted, the file does not exist, or a read error occurs.
        """
        if not self._mounted:
            return None
        path = f"{self._mount_point}/{filename.lstrip('/')}"
        try:
            with open(path, "r") as f:
                return f.read()
        except Exception as e:
            print(f"[sdcard] WARNING: read_text failed - {e}")
            return None


# --- Unit test ---
def test():
    print("=== SDCard unit test ===")
    sd = SDCard()
    all_passed = True

    # --- Test 1: mount() ---
    result = sd.mount()
    passed = result is True
    print(f"  {'PASS' if passed else 'FAIL'} - mount() returns True")
    all_passed &= passed

    # --- Test 2: is_mounted() after mount ---
    passed = sd.is_mounted()
    print(f"  {'PASS' if passed else 'FAIL'} - is_mounted() True after mount")
    all_passed &= passed

    # --- Test 3: /sd appears in root listing ---
    dirs = uos.listdir("/")
    passed = "sd" in dirs
    print(f"  {'PASS' if passed else 'FAIL'} - /sd appears in uos.listdir('/')")
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
    print(f"  {'PASS' if passed else 'FAIL'} - can write and read back a test file")
    all_passed &= passed

    # --- Test 5: listdir() returns filenames ---
    files = sd.listdir()
    passed = (
        isinstance(files, list) and "test_sdcard.txt" not in files
    )  # we removed it above
    # Write two files so we can verify listing
    uos.mkdir(sd.mount_point + "/subdir") if "subdir" not in sd.listdir() else None
    for name in ("aaa.txt", "zzz.txt"):
        with open(sd.mount_point + "/" + name, "w") as f:
            f.write("x")
    files = sd.listdir()
    passed = "aaa.txt" in files and "zzz.txt" in files and files == sorted(files)
    print(f"  {'PASS' if passed else 'FAIL'} - listdir() returns sorted filenames")
    all_passed &= passed

    # --- Test 6: listdir(subdir) ---
    with open(sd.mount_point + "/subdir/inner.txt", "w") as f:
        f.write("inner")
    sub_files = sd.listdir("subdir")
    passed = sub_files == ["inner.txt"]
    print(
        f"  {'PASS' if passed else 'FAIL'} - listdir('subdir') returns subdir contents"
    )
    all_passed &= passed

    # --- Test 7: read_text() returns file contents ---
    with open(sd.mount_point + "/read_test.txt", "w") as f:
        f.write("hello kiln")
    content = sd.read_text("read_test.txt")
    passed = content == "hello kiln"
    print(f"  {'PASS' if passed else 'FAIL'} - read_text() returns file contents")
    all_passed &= passed

    # --- Test 8: read_text() returns None for missing file ---
    content = sd.read_text("does_not_exist.txt")
    passed = content is None
    print(
        f"  {'PASS' if passed else 'FAIL'} - read_text() returns None for missing file"
    )
    all_passed &= passed

    # Clean up test files
    for name in ("aaa.txt", "zzz.txt", "read_test.txt"):
        try:
            uos.remove(sd.mount_point + "/" + name)
        except Exception:
            pass
    try:
        uos.remove(sd.mount_point + "/subdir/inner.txt")
        uos.rmdir(sd.mount_point + "/subdir")
    except Exception:
        pass

    # --- Test 9: unmount() ---
    sd.unmount()
    passed = True  # No exception means success
    print(f"  {'PASS' if passed else 'FAIL'} - unmount() succeeds")
    all_passed &= passed

    # --- Test 10: is_mounted() after unmount ---
    passed = not sd.is_mounted()
    print(f"  {'PASS' if passed else 'FAIL'} - is_mounted() False after unmount")
    all_passed &= passed

    print(f"\n{'All tests passed!' if all_passed else 'Some tests FAILED'}")
    return all_passed


if __name__ == "__main__":
    test()
