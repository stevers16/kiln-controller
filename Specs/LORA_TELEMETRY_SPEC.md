# LoRa Telemetry Module — Hardware & Firmware Spec
**Kiln Controller Project**
**Status:** Pending hardware sourcing
**Scope:** Covers Pico-side LoRa transmitter and ESP32-side LoRa gateway

---

## Overview

The kiln Pico 2W transmits periodic telemetry and fault alerts via a LoRa radio link to an
ESP32 gateway at the cottage (~200m, wooded path following cleared driveway). The ESP32
forwards data over WiFi to an MQTT broker (Mosquitto on Pi4 when deployed; laptop for bench
testing). A mobile phone subscribes to alert topics for notifications.

```
Pico 2W → SPI → Ra-02 (433 MHz) ~~LoRa~~ Ra-02 → SPI → ESP32-WROOM-32 → WiFi → Mosquitto → phone
```

---

## Hardware

### LoRa Modules

**Part:** AI-Thinker Ra-02 (SX1278, 433 MHz)
**Quantity:** 2
**Sourcing:** AliExpress (~$4–8 CAD total for both)
**Critical:** Order the **433 MHz Ra-02** (blue PCB), NOT the 915 MHz Ra-01 or Ra-01S.

**Ra-02 electrical characteristics:**
- Supply voltage: 3.3V (NOT 5V tolerant — will be damaged by 5V)
- Logic levels: 3.3V
- Interface: SPI (mode 0)
- Max SPI clock: 10 MHz (use 1–4 MHz in practice)
- Current draw: ~120 mA TX, ~12 mA RX, ~1.5 mA idle

**Antenna:** Ra-02 has a u.FL connector. Options:
- u.FL to SMA pigtail + 433 MHz spring/whip antenna (most AliExpress listings include one)
- Bare quarter-wave wire antenna: 17.3 cm of solid wire soldered directly to u.FL centre pin
  with GND connected. Adequate for 200m with line-of-sight or light obstruction.

---

### Pico-Side Wiring (Kiln Enclosure)

The Ra-02 runs on 3.3V and connects via SPI1 on the Pico using the default SPI1 pin block
(GP10–GP13). To free up GP12 and GP13 for SPI1, the moisture probe AC excitation channels
are moved from GP12/GP13 to GP6/GP7 — both are plain digital outputs and the move has no
functional impact on the moisture probe circuit.

**Power:**

| Ra-02 Pin | Pico Pin | Notes |
|-----------|----------|-------|
| VCC | 3.3V (Pin 36) | Ra-02 is 3.3V only |
| GND | GND (Pin 38) | Common ground |

**SPI and control:**

| Ra-02 Pin | Pico GPIO | Physical Pin | Notes |
|-----------|-----------|--------------|-------|
| SCK | GP10 | Pin 14 | SPI1 SCK (default) |
| MOSI | GP11 | Pin 15 | SPI1 TX (default) |
| MISO | GP12 | Pin 16 | SPI1 RX (default) |
| NSS (CS) | GP13 | Pin 17 | SPI1 CS (default) — active low |
| DIO0 | GP20 | Pin 26 | Interrupt — TX done / RX done |
| RST | GP28 | Pin 34 | Reset — active low (repurposed from spare ADC) |

**GPIO map changes required (Pico GPIO Map tab in BOM):**

| GPIO | Previous Assignment | New Assignment |
|------|-------------------|----------------|
| GP6 | (unassigned) | Digital OUT — Moisture probe AC excite Ch1 |
| GP7 | (unassigned) | Digital OUT — Moisture probe AC excite Ch2 |
| GP10 | (unassigned) | SPI1 SCK — LoRa Ra-02 |
| GP11 | (unassigned) | SPI1 TX (MOSI) — LoRa Ra-02 |
| GP12 | Digital OUT — AC excite Ch1 | SPI1 RX (MISO) — LoRa Ra-02 |
| GP13 | Digital OUT — AC excite Ch2 | SPI1 CS — LoRa Ra-02 (active low) |
| GP20 | (unassigned) | Digital IN — LoRa DIO0 interrupt |
| GP28 | Spare ADC | Digital OUT — LoRa RST |

**Decoupling:** Place a 100 nF ceramic capacitor between Ra-02 VCC and GND, as close to the
module as possible. The Ra-02's RF switching causes supply transients.

---

### ESP32 Gateway Wiring

**Board:** KeeYees ESP32-WROOM-32 devboard, 38-pin narrow (owned)
**Programming:** Arduino IDE via USB (CP2102 onboard)

The ESP32 3.3V pin supplies the Ra-02 directly. All SPI signals are 3.3V — no level shifting
required.

**Power:**

| Ra-02 Pin | ESP32 Pin | Notes |
|-----------|-----------|-------|
| VCC | 3.3V | From ESP32 onboard regulator |
| GND | GND | Common ground |

**SPI and control (VSPI — ESP32 hardware SPI):**

| Ra-02 Pin | ESP32 GPIO | Notes |
|-----------|------------|-------|
| SCK | GPIO18 | VSPI SCK |
| MOSI | GPIO23 | VSPI MOSI |
| MISO | GPIO19 | VSPI MISO |
| NSS (CS) | GPIO5 | VSPI CS |
| DIO0 | GPIO4 | RX/TX done interrupt |
| RST | GPIO14 | Reset line |

---

## Firmware Architecture

### Pico Side — `lib/lora.py`

Follow the existing module pattern (class-based, pin numbers as constructor args, matching
`exhaust.py` template).

```python
# Instantiation example (from main.py)
lora = LoRa(
    spi_id=1,
    sck=10, mosi=11, miso=12,
    cs=13, irq=20, rst=28,
    frequency=433_000_000
)
```

**Class responsibilities:**
- Initialise SPI1 using default pin block:
  `SPI(1, baudrate=1_000_000, sck=Pin(10), mosi=Pin(11), miso=Pin(12))`
- Configure SX1278 registers via SPI (frequency, bandwidth, spreading factor, coding rate)
- Expose `send(payload: bytes)` — blocking send with timeout
- Expose `send_telemetry(data: dict)` — serialises dict to JSON and calls `send()`
- Expose `send_alert(code: str, message: str)` — for fault conditions
- No receive path needed on the Pico (one-way telemetry is sufficient unless remote parameter
  updates are added later)

**Suggested LoRa parameters (balance range vs. latency):**

| Parameter | Value | Notes |
|-----------|-------|-------|
| Frequency | 433.0 MHz | Within 433.05–434.79 MHz ISM band (Canada RSS-210) |
| Bandwidth | 125 kHz | Standard |
| Spreading Factor | SF9 | ~250 bps, good for wooded 200m path |
| Coding Rate | 4/5 | |
| TX Power | 17 dBm | Ra-02 maximum; reduce if interference observed |
| Preamble | 8 symbols | Default |

**Transmission schedule:**
- Telemetry heartbeat: every 30 seconds (configurable)
- Fault alerts: immediate, retried up to 3 times with 2s spacing
- Do not transmit during SD card writes if SPI bus is shared (not an issue here — separate SPI)

**Power note:** Ra-02 draws ~120 mA during TX. At 17 dBm, a 30-second interval means
<1% duty cycle — well within the 10% duty cycle limit for 433 MHz ISM use in Canada.

---

### ESP32 Side — Arduino Sketch

**Libraries required (Arduino IDE Library Manager):**
- `RadioLib` by Jan Gromeš — SX1276/78 support, well-maintained
- `PubSubClient` by Nick O'Leary — MQTT client
- `ArduinoJson` by Benoît Blanchon — JSON parsing

**Sketch structure:**

```
gateway/
  gateway.ino       — setup(), loop()
  config.h          — WiFi credentials, MQTT broker IP, topic prefixes
  lora_handler.cpp  — RadioLib init, onReceive() callback
  mqtt_handler.cpp  — MQTT connect/reconnect, publish helpers
```

**`config.h` (the only file that changes between bench and cottage deployment):**

```cpp
// Bench testing
#define WIFI_SSID     "HomeNetwork"
#define WIFI_PASSWORD "homepassword"
#define MQTT_BROKER   "192.168.1.xx"   // laptop IP running Mosquitto

// Cottage deployment (swap these)
// #define WIFI_SSID     "CottageNetwork"
// #define WIFI_PASSWORD "cottagepassword"
// #define MQTT_BROKER   "192.168.x.xx"  // Pi4 IP
```

**Operation:**
1. On boot: connect WiFi, connect MQTT broker, initialise RadioLib SX1278
2. RadioLib interrupt-driven receive: `radio.startReceive()` → DIO0 fires → `onReceive()`
3. `onReceive()`: read packet, parse JSON, publish to MQTT topic
4. Loop: maintain WiFi + MQTT connections, reconnect if dropped
5. Publish RSSI and SNR alongside each payload (useful for link quality monitoring)

---

## MQTT Topic Structure

**Root prefix:** `kiln/`

All values published as JSON payloads. QoS 0 for telemetry, QoS 1 for alerts.

### Telemetry (heartbeat, every 30s)

**Topic:** `kiln/telemetry`

```json
{
  "ts": 1700000000,
  "stage": "drying",
  "temp_lumber": 52.3,
  "temp_intake": 28.1,
  "humidity_lumber": 61.2,
  "humidity_intake": 44.5,
  "exhaust_fan_rpm": 1850,
  "exhaust_fan_pct": 75,
  "circ_fan_on": true,
  "heater_on": false,
  "vent_intake_pct": 40,
  "vent_exhaust_pct": 60,
  "rssi": -87,
  "snr": 7.2
}
```

### Alerts (fault conditions)

**Topic:** `kiln/alert`

```json
{
  "ts": 1700000000,
  "code": "OVER_TEMP",
  "message": "Lumber zone temp 85°C exceeds limit",
  "value": 85.0,
  "limit": 80.0
}
```

**Alert codes:**

| Code | Condition |
|------|-----------|
| `OVER_TEMP` | Either SHT31 zone exceeds stage limit |
| `SENSOR_FAIL` | SHT31 read error or timeout |
| `FAN_STALL` | Exhaust fan tach reads 0 RPM when commanded on |
| `HEATER_TIMEOUT` | SSR has been on continuously beyond safety threshold |
| `SD_FAIL` | SD card write error |
| `STAGE_COMPLETE` | Drying schedule stage transition (informational) |
| `SCHEDULE_DONE` | Full drying schedule completed |

### Status (on-demand or on change)

**Topic:** `kiln/status`

```json
{
  "ts": 1700000000,
  "state": "running",
  "schedule_name": "maple_1inch",
  "stage_index": 2,
  "stage_name": "main_dry",
  "stage_elapsed_min": 143,
  "stage_duration_min": 480
}
```

### Gateway health

**Topic:** `kiln/gateway`

Published by ESP32 every 60s:

```json
{
  "ts": 1700000000,
  "uptime_s": 3600,
  "wifi_rssi": -62,
  "last_lora_rssi": -87,
  "last_lora_snr": 7.2,
  "packets_received": 120,
  "packets_missed": 2
}
```

---

## Phone Notifications

### Option A — ntfy.sh (recommended, simplest)

The Pi4 (or laptop during testing) runs a small subscriber script that listens to `kiln/alert`
and HTTP-POSTs to ntfy.sh. Free tier is adequate.

```python
# On Pi4/laptop: kiln_notify.py
import paho.mqtt.client as mqtt
import requests

def on_message(client, userdata, msg):
    import json
    data = json.loads(msg.payload)
    requests.post(
        "https://ntfy.sh/your-kiln-topic",   # choose a unique topic name
        data=f"{data['code']}: {data['message']}",
        headers={"Priority": "high", "Tags": "fire"}
    )

client = mqtt.Client()
client.on_message = on_message
client.connect("localhost", 1883)
client.subscribe("kiln/alert")
client.loop_forever()
```

Install ntfy app on phone, subscribe to `your-kiln-topic`. Done.

### Option B — Home Assistant

If Home Assistant is already running on the Pi4, add an MQTT sensor for `kiln/telemetry`
and automation triggers on `kiln/alert`. No additional code needed beyond HA configuration.

---

## BOM Additions

| Item | Qty | Est. Cost (CAD) | Notes |
|------|-----|-----------------|-------|
| AI-Thinker Ra-02 433 MHz LoRa module | 2 | $6–10 | AliExpress; verify 433 MHz, not 915 MHz |
| u.FL to SMA pigtail + 433 MHz antenna | 2 | often included | Check listing; otherwise ~$2 each |
| 100 nF ceramic capacitor (decoupling) | 2 | ~$0.10 | Likely already in parts stock |

The ESP32-WROOM-32 devboard is already owned ($0).

---

## Testing Plan

### Bench Test (at home, short range)

1. Wire both Ra-02 modules per wiring tables above
2. Load minimal MicroPython `lora_test.py` on Pico — sends a packet every 5s
3. Flash ESP32 gateway sketch with home WiFi credentials and laptop MQTT broker IP
4. Install Mosquitto on laptop: `brew install mosquitto` / `apt install mosquitto`
5. Subscribe on laptop: `mosquitto_sub -t "kiln/#" -v`
6. Verify packets appearing with correct JSON structure
7. Verify RSSI/SNR values are reasonable (will be very strong at short range — that's fine)
8. Test alert path: force an alert code from Pico, verify ntfy.sh notification arrives on phone

### Pre-Deployment Test (at cottage, full path)

1. Swap `config.h` to cottage WiFi credentials and Pi4 MQTT broker IP
2. Flash ESP32, place at cottage
3. Power kiln Pico from bench supply at kiln location
4. Monitor `kiln/gateway` topic — watch `last_lora_rssi` and `packets_missed`
5. Target RSSI: better than -115 dBm (SX1278 sensitivity at SF9). Expect -90 to -105 dBm
   for 200m wooded path at 433 MHz.
6. If RSSI is marginal, increase spreading factor to SF10 or SF11 (reduces data rate but
   improves link budget by 3–6 dB per step)

---

## Open Items

- [ ] Confirm GP28 availability for LoRa RST (no ADC use planned — treat as resolved unless moisture probe ambient reference is needed on GP28)
- [ ] Source Ra-02 modules (AliExpress — allow 3–4 weeks shipping)
- [ ] Decide on notification stack: ntfy.sh vs Home Assistant
- [ ] Update BOM GPIO map tab: move AC excite Ch1/Ch2 to GP6/GP7; add GP10–13 (SPI1 LoRa), GP20 (IRQ), GP28 (RST)
- [ ] Implement `lib/lora.py` on Pico (Claude Code)
- [ ] Implement ESP32 gateway sketch (Claude Code or Arduino IDE direct)
- [ ] Set up Mosquitto on laptop for bench testing