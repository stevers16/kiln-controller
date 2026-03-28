# KIVY_APP_SPEC.md

Spec for the Kivy Android + desktop app for the kiln controller project.

---

## Overview

The app is the primary human interface for the kiln controller. It connects either
directly to the Pico 2W in AP mode (full control) or to the Pi4 daemon over cottage
WiFi (read-only monitoring). It is written in Python using the Kivy framework and
targets Android and desktop (Windows/macOS) for development.

---

## Connection Modes

### Auto-detect with manual override

On launch (and on manual refresh), the app attempts connections in order:

1. Try Pico AP IP (default `192.168.4.1`, port `80`) -- `GET /health`
2. If no response within 3 seconds, try Pi4 IP (user-configured), port `8080` --
   `GET /health`
3. If neither responds, show "No connection" state

A connection mode indicator is always visible in the navigation bar:
- Green dot + "Direct" -- connected to Pico AP
- Blue dot + "Cottage" -- connected to Pi4 daemon
- Red dot + "Offline" -- no connection

Manual override: a toggle in Settings forces a specific mode. When forced, the app
does not fall back to the other endpoint.

Auto-refresh retries connection every 30 seconds when offline.

### Mode-dependent UI

Screens and actions that are only available in AP (Direct) mode are hidden or
shown as disabled with a "Direct connection required" tooltip in Cottage mode.
The app never silently attempts a control action when not in AP mode.

---

## Authentication

All requests to the Pico REST API include the header:

```
X-Kiln-Key: <api_key>
```

The API key is configured in Settings and stored in app local storage. On first
launch, the app prompts the user to enter the key. The key must match the value
in `config.py` on the Pico.

The Pi4 daemon REST API does not require authentication (LAN-only, read-only).

---

## Navigation

Bottom navigation bar with five tabs, always visible:

| Tab | Icon | Available in Cottage mode |
|-----|------|--------------------------|
| Dashboard | home | Yes |
| History | chart-line | Yes |
| Alerts | bell | Yes |
| Runs | list | Yes |
| Settings | gear | Yes |

AP-only screens (System Test, Schedules, Logs, Start Run, Moisture Calibration,
Module Upload) are accessed from the Dashboard or Settings, not from the bottom
nav. They are not reachable in Cottage mode.

---

## Screens

---

### Dashboard

The primary at-a-glance screen. Auto-refreshes every 10 seconds in AP mode,
every 35 seconds in Cottage mode.

**Status banner (top):**
- Current stage name and number (e.g. "Stage 3 of 9 -- Drying")
- Stage type badge: DRYING / EQUALIZING / CONDITIONING / IDLE / COOLDOWN
- Stage elapsed time (e.g. "14h 32m elapsed")
- Stage progress bar: elapsed vs. min_duration_h (fills to 100% at min duration,
  does not go beyond -- MC% and time both required to advance drying stages)

**Water pan banner (conditional):**
- Shown whenever current stage_type is "equalizing" or "conditioning"
- Persistent yellow banner: "Water pans may be needed -- check RH actual vs target"
- Dismissed only when stage changes

**Fault banner (conditional, highest priority):**
- Red banner for: heater_fault, sensor_failure, temp_out_of_range (sustained),
  rh_out_of_range (sustained)
- Shows alert code + message; tapping navigates to Alerts screen
- Overrides water pan banner in display priority

**Sensor readings (two columns):**

Left column -- Lumber zone (SHT31 at 0x44):
- Temperature: actual / target (colour: green within deadband, amber outside,
  red if fault)
- Relative humidity: actual / target (same colour scheme)

Right column -- Intake zone (SHT31 at 0x45):
- Temperature (actual only -- no target for intake)
- Relative humidity (actual only)

**Moisture content:**
- Channel 1: MC% (corrected) + raw resistance
- Channel 2: MC% (corrected) + raw resistance
- Target MC% for current stage (if drying stage)
- "Probe fault" if reading is None

**Equipment state row:**
- Heater: ON (red) / OFF (grey)
- Vents: OPEN / CLOSED
- Exhaust fan: ON + speed% / OFF
- Circulation fans: ON + speed% / OFF

**Rail currents (system health):**
- 12V rail: current in mA
- 5V rail: current in mA
- Shown as small indicators, not primary content

**LoRa link quality (Cottage mode only):**
- Signal strength bar (5 levels) derived from RSSI
- Raw RSSI dBm and SNR values
- Timestamp of last received packet (e.g. "Last update: 28s ago")

**Last update timestamp** (always visible, bottom of screen):
- In AP mode: "Updated Xs ago"
- In Cottage mode: "Last LoRa packet: Xs ago"

**AP mode action buttons (bottom, hidden in Cottage mode):**
- "Start Run" -- navigates to Start Run screen (disabled if run already active)
- "Stop Run" -- confirmation dialog, then POST /run/stop (shown only if run active)
- "Advance Stage" -- confirmation dialog, then POST /run/advance (shown only if
  run active and past min_duration_h)

---

### History Graphs

Time-series plots of logged data. Data source: Pi4 `/history` in Cottage mode,
Pico `/history` in AP mode.

**Time range selector (top):**
Predefined buttons: 1h | 6h | 24h | 72h | Full run
"Full run" loads data for the currently selected run (from Runs screen or current
active run).

**Run selector:**
Dropdown to choose which run's data to display. Defaults to current/most recent run.

**Plot tabs (five tabs):**

*Thermal*
- temp_lumber (solid line, primary colour)
- temp_intake (dashed line, secondary colour)
- target_temp stepped line (grey) -- derived from stage boundaries
- heater_on overlay (shaded band at bottom, red when on)
- Y axis: degrees C; X axis: elapsed time or wall clock time (toggle)

*Humidity*
- humidity_lumber (solid)
- humidity_intake (dashed)
- target_rh stepped line (grey)
- vent_open overlay (shaded band, blue when open)
- Y axis: % RH

*Moisture Content*
- channel_1 MC% (solid)
- channel_2 MC% (dashed)
- target_mc stepped line (grey, shown only during drying stages, null otherwise)
- Y axis: MC%

*Stage Timeline*
- Stage index as a step chart over time
- Stage name labels at each step boundary
- Run start/end markers

*Diagnostics*
- exhaust_fan_rpm (left Y axis)
- lora_rssi dBm (right Y axis, Cottage mode only)
- lora_snr (right Y axis, secondary, Cottage mode only)
- 12V rail current mA
- 5V rail current mA

**Implementation notes:**
- Use Kivy Garden graph widget or matplotlib embedded via FigureCanvasKivyAgg
- Columnar response from `/history` is unpacked client-side into per-field arrays
  before plotting -- do not iterate row-by-row for plot data
- Downsample server-side for ranges > 24h by passing `resolution` param to
  `/history` (see Pico REST API spec); Pi4 daemon handles decimation
- Empty state: "No data for this time range" placeholder

---

### Alerts

Scrollable chronological list of warnings and errors.

**Each alert row:**
- Timestamp (wall clock)
- Alert code (bold, e.g. HEATER_FAULT)
- Message text
- Value + limit if present (e.g. "temp=72.3 limit=65.0")
- Severity badge: WARNING (amber) / ERROR (red)

**Filter bar (top):**
- All | Errors only | Warnings only
- Run selector dropdown (filter by run)

**Empty state:** "No alerts recorded"

---

### Runs

List of all drying runs from the database.

**Each run row:**
- Run start date/time
- Schedule name
- Duration (if completed: total; if active: elapsed)
- Status badge: ACTIVE (green) / COMPLETED / INCOMPLETE

**Tap a run:**
- Opens a run detail view showing:
  - Schedule name and species/thickness label
  - Start/end timestamps and total duration
  - Stages completed
  - Final MC% readings at run end
  - "View history" button -- navigates to History Graphs with that run pre-selected

---

### System Test (AP mode only)

Runs all hardware unit tests on the Pico and streams results to the app.

**Launch:**
- "Run System Test" button
- Estimated duration shown (e.g. "~3-5 minutes including heater confirmation")
- Warning: "Ensure kiln is safe to operate during test -- heater and fans will
  activate briefly"
- Confirmation dialog before starting

**Results view (live-updating):**
- Each test is a row: test name | status | detail
- Status: PENDING (grey) | RUNNING (spinner) | PASS (green check) | FAIL (red X) |
  SKIP (grey dash)
- Tests grouped into sections:
  - Unit Tests (per module)
  - Integration Tests (cross-module, hardware in loop)
  - Commissioning Checks (heater temp rise, LoRa TX, RTC set)
- Progress bar: N of M tests complete
- Elapsed time

**Result summary (on completion):**
- Overall PASS / FAIL badge
- Count: X passed, Y failed, Z skipped
- "Save results" button -- downloads test report as timestamped text file
- "Copy to clipboard" button

**Polling model:**
- App POSTs to `/test/run` to start
- Polls `GET /test/status` every 1 second
- Pico streams results as tests complete; each poll returns full result array
  (idempotent, app diffs to detect new results)
- Final poll returns `"complete": true`

---

### Schedules (AP mode only)

List of schedule JSON files on the SD card.

**Each schedule row:**
- Schedule name (from JSON `name` field)
- Species + thickness label
- Number of stages
- Built-in badge (for the four factory schedules: maple_05in, maple_1in,
  beech_05in, beech_1in)
- Last modified date

**Actions per schedule:**
- View -- read-only table view of all stages
- Duplicate -- creates a copy with "_copy" suffix; opens editor
- Edit -- opens Schedule Editor (disabled for built-in schedules with a tooltip:
  "Duplicate to edit")
- Delete -- confirmation dialog; disabled for built-in schedules

**"New schedule" button:**
- Opens Schedule Editor with a blank template (single drying stage pre-populated)

---

### Schedule Editor (AP mode only)

Structured per-stage table editor.

**Header fields:**
- Schedule name (text field)
- Species (dropdown: maple / beech / oak / pine / other)
- Thickness (dropdown: 0.5 in / 1 in / custom)

**Stage table:**
Each row represents one stage with editable fields:
- Stage name (text, e.g. "Stage 1 - Initial warm-up")
- Stage type (dropdown: drying / equalizing / conditioning)
- Target temp C (numeric, 30-80)
- Target RH % (numeric, 20-95)
- Target MC % (numeric, required for drying; greyed out and set to null for
  equalizing/conditioning)
- Min duration h (numeric)
- Max duration h (numeric or blank for no limit)

**Stage controls:**
- Add stage (appends a row)
- Delete stage (remove row, with confirmation if not the last)
- Reorder (drag handle on each row)

**Validation on save:**
- All required fields present
- Stage types consistent with target_mc_pct rules (drying requires numeric MC%;
  equalizing/conditioning require null)
- Temperatures in valid range
- Min duration > 0
- At least one stage

**Save:**
- Validates, then PUTs JSON to Pico `/schedules/{filename}`
- Filename derived from schedule name (lowercase, underscores, .json suffix)
- Success: navigates back to Schedules list
- Error: shows inline validation messages or server error

---

### Logs (AP mode only)

List of log file sets on the SD card, one set per run.

**Each log set row:**
- Run date/time (from filename timestamp)
- Two file badges: EVENT LOG | DATA CSV
- File sizes

**Actions per log set:**
- View event log -- opens scrollable in-app text viewer (event_YYYYMMDD_HHMM.txt)
- Download event log -- saves to device Downloads folder
- Download data CSV -- saves to device Downloads folder
- Delete log set -- confirmation dialog; deletes both files for that run

**Storage indicator (top):**
- SD card used / total (from GET /sdcard/info)
- Warning if > 80% full

**In-app event log viewer:**
- Scrollable monospace text
- Filter by level: ALL | INFO | WARN | ERROR
- Search bar (substring match)

---

### Start Run (AP mode only)

Pre-run setup flow before starting a drying run.

**Step 1 -- Select schedule:**
- Species buttons: Maple | Beech | Other
- Thickness buttons: 0.5 inch | 1 inch | Custom
- Selecting species + thickness auto-selects the matching built-in schedule
  (maple_05in, maple_1in, beech_05in, beech_1in)
- "Or choose manually" -- opens schedule picker from full list
- Selected schedule name shown with stage count and estimated duration range

**Step 2 -- Run label (optional):**
- Free text label stored with the run record (e.g. "Workshop maple boards batch 1")

**Step 3 -- Pre-run checklist:**
Each item is a checkbox the user must tick before Start is enabled:
- [ ] Lumber loaded and stacked with spacers
- [ ] Moisture probes inserted into representative boards
- [ ] Water pans removed from kiln (initial drying stages)
- [ ] Kiln door sealed
- [ ] Extension cord connected and heater plugged in
- [ ] Adequate ventilation around kiln

**Start button:**
- Enabled only when all checklist items ticked
- Confirmation dialog: "Start [schedule name]? This will activate the heater and fans."
- POSTs to `/run/start` with schedule filename and label
- On success: navigates to Dashboard

---

### Moisture Calibration (AP mode only)

Calibrate per-channel MC% correction offsets against a handheld reference meter.

**Live readings panel:**
For each channel (channel_1, channel_2):
- Raw resistance (ohms)
- Corrected MC% (using current calibration offset)
- Current calibration offset (delta MC%)
- "Take reading" button -- triggers a fresh probe read

**Calibration entry:**
For each channel:
- Reference MC% field: user enters reading from handheld meter
- "Apply" button: computes offset = reference - corrected_before_offset; previews
  new corrected MC% with proposed offset applied
- "Save" button: POSTs updated calibration to `/calibration`; Pico writes
  calibration.json to SD card

**Reset:**
- "Reset to defaults" button: sets both offsets to 0.0; confirms before applying

**Notes panel:**
- "Calibrate with probes inserted in boards at operating temperature for best
  accuracy. Temperature correction is applied automatically during kiln runs."
- "Channel 1 and Channel 2 are positional (stack location), not species-specific.
  Species correction is set in the drying schedule."

**Current calibration display:**
- Shows channel_1 offset, channel_2 offset, last calibrated timestamp
- If calibration.json not found on SD: "No calibration file -- factory defaults
  in use (0.0 offset)"

---

### Module Upload (AP mode only)

Upload updated Python module files or schedule JSON files to the Pico over WiFi.

**Warning banner (persistent, always shown):**
"Uploading a broken module may render the kiln inoperable and require USB
recovery via mpremote. Keep a USB cable accessible. The Pico will reboot after
upload."

**File picker:**
- Accepts .py files and .json files only
- Shows selected filename and file size
- Rejects other file types with an error message

**Target path:**
- Pre-filled based on filename:
  - .py files: `lib/<filename>` (e.g. `lib/exhaust.py`)
  - .json files: `schedules/<filename>` (e.g. `schedules/maple_05in.json`)
  - `main.py` detected by name: target path set to `/main.py` with an additional
    warning: "Uploading main.py replaces the entry point. A syntax error will
    prevent the kiln from booting."
- Target path is editable

**Upload:**
- "Upload" button
- Progress bar (bytes sent / total)
- Success: "Upload complete. Pico rebooting..." then reconnection attempt after 5s
- Error: server error message displayed

**Installed modules list:**
- GET /modules returns list of installed .py files with sizes and last-modified
  timestamps
- Shown below the upload form for reference

---

### Settings

**Connection section:**
- Pico AP IP address (default: 192.168.4.1)
- Pico AP port (default: 80)
- Pi4 IP address
- Pi4 port (default: 8080)
- Connection mode override: Auto-detect | Force Direct | Force Cottage
- "Test connection" button for each endpoint

**Authentication section:**
- API key field (masked, show/hide toggle)
- "Save key" button

**RTC sync section:**
- "Sync Pico clock now" button -- POSTs current Unix timestamp to `/time`
- Last synced timestamp (from app local storage)
- "Auto-sync on connect" toggle (default: on)

**Daemon info section (populated from Pi4 /health):**
- Daemon environment: bench / cottage
- Daemon uptime
- Total packets received
- Last packet timestamp
- ntfy.sh topic name (display only)

**About section:**
- App version
- Pico firmware version (from GET /version)
- Connected Pico uptime (from GET /health)

---

## Data Flow Summary

### AP mode (Pico direct)

```
App --> HTTP GET/POST (port 80, X-Kiln-Key header) --> Pico REST API
     <-- JSON response
```

Refresh intervals:
- Dashboard: 10s auto-refresh
- History: on demand (time range change or manual refresh)
- Alerts: on demand
- System test: 1s polling during active test

### Cottage WiFi mode (Pi4 daemon)

```
App --> HTTP GET (port 8080, no auth) --> Pi4 REST API --> SQLite
     <-- JSON response
```

Refresh intervals:
- Dashboard: 35s auto-refresh
- History: on demand
- Alerts: on demand

---

## Error Handling

- HTTP timeout (>5s): show "Connection timeout" toast; mark connection as lost
- HTTP 401 (wrong API key): show "Authentication failed -- check API key in Settings"
- HTTP 500: show server error message from response body if present
- JSON parse error: show "Unexpected response from device"
- All errors logged to app local log (accessible via Settings > App Log)
- Connection lost during active test: show "Connection lost -- test may still be
  running on device"; offer "Reconnect and resume polling" button

---

## Local Storage (app-side)

Stored in app data directory (Android: internal storage; desktop: user config dir):

| Key | Content |
|-----|---------|
| `pico_ip` | Pico AP IP address |
| `pi4_ip` | Pi4 IP address |
| `pi4_port` | Pi4 port |
| `api_key` | Pico API key (store obfuscated, not plaintext) |
| `connection_override` | auto / direct / cottage |
| `last_rtc_sync` | Unix timestamp of last successful RTC sync |
| `app_log` | Rolling last-100-lines app event log |

---

## Implementation Notes

- Target Kivy version: 2.3.x (current stable at time of writing)
- Use `kivymd` for Material Design widgets (cards, buttons, badges, bottom nav)
- History plots: use `kivy_garden.matplotlib` or embed `matplotlib` figures directly
- Android build: use `buildozer`; target Android API 31+
- Desktop: standard `python -m kivy` invocation; test on macOS and Windows
- All network calls are async (use `asyncio` or `threading` -- do not block the
  Kivy main thread)
- Schedule JSON editor: use a `RecycleView` for the stage table to handle
  schedules with many stages without performance issues
- Columnar history data from `/history`: unpack fields array + rows array into
  per-field lists before passing to matplotlib -- do not build dicts per row

---

## Open Items for Implementation

- Chart library final selection (kivy_garden.matplotlib vs. plotly in a WebView)
- Buildozer spec for Android packaging (permissions: INTERNET, WRITE_EXTERNAL_STORAGE)
- App icon and splash screen
- ntfy.sh topic configuration UI (if operator wants to change topic without SSH
  into Pi4)
