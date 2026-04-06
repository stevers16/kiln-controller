#!/usr/bin/env python3
"""
update_lib.py

Copies main.py, config.py, and all files from the local lib/ directory to
the connected Pico using mpremote.

Usage:
    python update_lib.py

Requirements:
    mpremote must be installed: pip install mpremote
"""

import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).parent
LIB_DIR = ROOT_DIR / "lib"
# ROOT_FILES = ["config.py"]
ROOT_FILES = ["boot.py", "main.py", "config.py"]


def run(cmd):
    print(f"  {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}")
        return False
    return True


def main():
    lib_files = sorted(LIB_DIR.glob("*.py"))
    if not lib_files:
        print(f"No .py files found in {LIB_DIR}")
        sys.exit(1)

    # Copy root files (main.py, config.py)
    root_files = [ROOT_DIR / f for f in ROOT_FILES if (ROOT_DIR / f).exists()]
    total = len(root_files) + len(lib_files)
    print(f"Copying {total} file(s) to Pico ...")

    all_ok = True
    for f in root_files:
        ok = run(["mpremote", "cp", str(f), f":{f.name}"])
        if not ok:
            all_ok = False

    # Ensure /lib exists on the Pico (only create if missing)
    check = subprocess.run(
        ["mpremote", "ls", ":lib/"],
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        print("  /lib/ not found on Pico, creating...")
        run(["mpremote", "mkdir", ":lib"])

    for f in lib_files:
        ok = run(["mpremote", "cp", str(f), f":lib/{f.name}"])
        if not ok:
            all_ok = False

    if all_ok:
        print(f"\nAll {total} files copied successfully.")
    else:
        print("\nSome files failed to copy - check errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
