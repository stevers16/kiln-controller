"""Kiln Controller - Kivy app entry point.

The actual app lives in the `kilnapp/` package. This file just imports it
and starts Kivy. Run from this folder with the venv activated:

    python main.py
"""

from kilnapp.app import KilnApp


if __name__ == "__main__":
    KilnApp().run()
