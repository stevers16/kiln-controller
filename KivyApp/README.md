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

## Android build (Phase 15)

Android packaging uses [buildozer](https://buildozer.readthedocs.io/),
run from WSL2 with the Android SDK + NDK already installed.

### One-time setup (WSL2)

In your Ubuntu/Debian WSL2 instance:

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv \
    git zip unzip openjdk-17-jdk autoconf libtool pkg-config zlib1g-dev \
    libncurses5-dev libncursesw5-dev libtinfo5 cmake libffi-dev libssl-dev
pip3 install --user --upgrade buildozer cython==0.29.36
```

Tell buildozer where your existing SDK/NDK lives. Edit
`buildozer.spec` and uncomment / fill in the two lines:

```ini
android.sdk_path = /home/<you>/Android/Sdk
android.ndk_path = /home/<you>/Android/Sdk/ndk/<version>
```

(`buildozer.spec` does not expand `$HOME`, so use absolute paths.
Don't commit your personal paths back to git; one option is
`git update-index --skip-worktree buildozer.spec` after editing.)

Confirm your SDK has API 33 + the matching build-tools 33.0.x
installed, since `buildozer.spec` targets `android.api = 33`. If not,
either install them via Android Studio's SDK Manager or drop
`android.api` to a version you do have.

### Build

From the repo's `KivyApp/` directory inside WSL2:

```bash
./scripts/build-android.sh
```

That just wraps `buildozer android debug`. The first build pulls
the python-for-android recipes and compiles native deps (numpy,
matplotlib, pillow, ...); subsequent builds reuse the
`.buildozer/` cache. APK lands in `bin/`.

`adb install -r bin/kilncontroller-*-debug.apk` deploys to a phone
on USB. To stream logcat, `adb logcat | grep python` filters down
to Kivy/Python output.

### Notes

- `android.skip_update = True` is set in the spec so buildozer
  trusts the existing SDK rather than trying to update it.
- If the matplotlib 3.10.8 recipe fails to build, pin to a
  version with a battle-tested recipe (`matplotlib==3.4.3` or
  `3.5.3`) in both `requirements.txt` and `buildozer.spec`'s
  `requirements =` line and rebuild.
- Icon and presplash assets aren't bundled yet; buildozer ships
  defaults. Drop `data/icon.png` and `data/presplash.png` into
  `KivyApp/` and uncomment the matching lines in `buildozer.spec`
  to override.

### Runtime notes on Android

- HTTP to the Pico AP is cleartext (no TLS). Cleartext is permitted
  by buildozer's default template through API 33; tighter versions
  later will need a custom `network_security_config.xml`.
- File saves (Log download, System Test report) go to the app's
  private sandbox (`App.user_data_dir`). The status line on each
  screen shows the full path; pull them off the phone via USB/MTP
  or a file-manager app with sandbox access.
- Module Upload uses the Android system document picker (SAF) via
  `plyer.filechooser`. Desktop falls through to the Kivy widget.

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
