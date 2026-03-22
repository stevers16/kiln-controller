#!/usr/bin/env python3
"""
update_lib.py

Copies all files from the local lib/ directory to /lib/ on the connected Pico
using mpremote.

Usage:
    python update_lib.py

Requirements:
    mpremote must be installed: pip install mpremote
"""

import subprocess
import sys
from pathlib import Path

LIB_DIR = Path(__file__).parent / "lib"


def run(cmd):
    print(f"  {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}")
        return False
    return True


def main():
    files = sorted(LIB_DIR.glob("*.py"))
    if not files:
        print(f"No .py files found in {LIB_DIR}")
        sys.exit(1)

    print(f"Copying {len(files)} file(s) to Pico /lib/ ...")

    # Ensure /lib exists on the Pico (only create if missing)
    check = subprocess.run(
        ["mpremote", "ls", ":lib/"],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        print("  /lib/ not found on Pico, creating...")
        run(["mpremote", "mkdir", ":lib"])

    all_ok = True
    for f in files:
        ok = run(["mpremote", "cp", str(f), f":lib/{f.name}"])
        if not ok:
            all_ok = False

    if all_ok:
        print("\nAll files copied successfully.")
    else:
        print("\nSome files failed to copy - check errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
