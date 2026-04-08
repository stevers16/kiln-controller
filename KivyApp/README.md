# Kiln Controller - Kivy App

Mobile/desktop interface to the Solar Wood Drying Kiln. Talks to the Pico 2 W
WiFi AP REST API for live control, or to the Pi4 `kiln_server` daemon for
read-only monitoring over cottage WiFi.

This folder is the entire app. The MicroPython firmware in the repo root and in
`../lib/` is unrelated and uses different conventions.

Spec: [`../Specs/kivy_app_spec.md`](../Specs/kivy_app_spec.md)
Status: [`../PROJECT.md`](../PROJECT.md) -- "KivyApp/" section.

## Targets

- **Primary:** Android phone, packaged via buildozer (final development phase)
- **Development / testing:** Desktop (Windows, macOS) - every phase must run
  on the desktop before being considered done

## Desktop setup (Windows)

From this `KivyApp/` folder:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
python main.py
```

## Desktop setup (macOS / Linux)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python main.py
```

## Project layout

This is built up incrementally. Today the layout is just `main.py`. By the end
of development it will look like:

```
KivyApp/
  .venv/                   # gitignored
  requirements.txt
  main.py                  # entry point
  kilnapp/                 # app package
    app.py
    api/                   # REST clients (Pico AP + Pi4 daemon)
    screens/               # one module per screen
    widgets/
    storage.py             # local persistent settings
  tests/                   # pytest, desktop only
```
