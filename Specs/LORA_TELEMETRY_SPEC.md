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
Pico 2W -> SPI -> Ra-02 (433 MHz) ~~LoRa~~ Ra-02 -> SPI -> ESP32-WROOM-32 -> WiFi -> Mosquitto -> phone
```

**Pico-side is transmit-only.** The Pico never listens for incoming LoRa packets. All
parameter changes and commands to the kiln are handled via the Wi-Fi AP / REST API interface.
This simplifies the firmware significantly and frees GP20 (previously reserved for a DIO0
interrupt) for use as the display button input.

---

## Hardware

### LoRa Modules

**Part:** AI-Thinker Ra-02 (SX1278, 433 MHz)
**Quantity:** 2
**Sourcing:** AliExpress (~$4-8 CAD total for both)
**Critical:** Order the **433 MHz Ra-02** (blue PCB), NOT the 915 MHz Ra-01 or Ra-01S.

**Ra-02 electrical characteristics:**
- Supply voltage: 3.3V (NOT 5V tolerant -- will be damaged by 5V)
- Logic levels: 3.3V
- Interface: SPI (mode 0)
- Max SPI clock: 10 MHz (use 1-4 MHz in practice)
- Current draw: ~120 mA TX, ~12 mA RX, ~1.5 mA idle

**Antenna:** Ra-02 has a u.FL connector. Options:
- u.FL to SMA pigtail + 433 MHz spring/whip antenna (most AliExpress listings include one)
- Bare quarter-wave wire antenna: 17.3 cm of solid wire soldered to u.FL centre pin with GND
  connected. Adequate for 200m with line-of-sight or light obstruction.

---

### Pico-Side Wiring (Kiln Enclosure)

The Ra-02 runs on 3.3V and connects via SPI1 on the Pico using the default SPI1 pin block
(GP10-GP13). DIO0 is **not connected** on the Pico side -- TX completion is confirmed by
polling the SX1278 IRQ flags register over SPI (see Firmware section). This frees GP20 for
use as the display button.

**Power:**

| Ra-02 Pin | Pico Pin       | Notes                        |
|-----------|----------------|------------------------------|
| VCC       | 3.3V (Pin 36)  | Ra-02 is 3.3V only           |
| GND       | GND (Pin 38)   | Common ground                |

**SPI and control:**

| Ra-02 Pin | Pico GPIO | Physical Pin | Notes                        |
|-----------|-----------|--------------|------------------------------|
| SCK       | GP10      | Pin 14       | SPI1 SCK (default)           |
| MOSI      | GP11      | Pin 15       | SPI1 TX (default)            |
| MISO      | GP12      | Pin 16       | SPI1 RX (default)            |
| NSS (CS)  | GP13      | Pin 17       | SPI1 CS -- active low        |
| RST       | GP28      | Pin 34       | Reset -- active low          |
| DIO0      | --        | not connected| Not needed (TX-only, polling)|

**GPIO map (Pico GPIO Map tab in BOM):**

| GPIO | Function                        | Notes                                    |
|------|---------------------------------|------------------------------------------|
| GP10 | SPI1 SCK -- LoRa Ra-02          |                                          |
| GP11 | SPI1 TX (MOSI) -- LoRa Ra-02   |                                          |
| GP12 | SPI1 RX (MISO) -- LoRa Ra-02   |                                          |
| GP13 | SPI1 CS -- LoRa Ra-02           | Active low                               |
| GP20 | Digital IN -- Display button    | Freed from LoRa DIO0; see display spec   |
| GP28 | Digital OUT -- LoRa RST         |                                          |

**Decoupling:** Place a 100 nF ceramic capacitor between Ra-02 VCC and GND, as close to the
module as possible. The Ra-02's RF switching causes supply transients.

---

### ESP32 Gateway Wiring

**Board:** KeeYees ESP32-WROOM-32 devboard, 38-pin narrow (owned)
**Programming:** Arduino IDE via USB (CP2102 onboard)

The ESP32 3.3V pin supplies the Ra-02 directly. All SPI signals are 3.3V -- no level shifting
required. DIO0 IS connected on the ESP32 side because the gateway uses interrupt-driven
receive via RadioLib.

**Power:**

| Ra-02 Pin | ESP32 Pin | Notes                        |
|-----------|-----------|------------------------------|
| VCC       | 3.3V      | From ESP32 onboard regulator |
| GND       | GND       | Common ground                |

**SPI and control (VSPI -- ESP32 hardware SPI):**

| Ra-02 Pin | ESP32 GPIO | Notes                        |
|-----------|------------|------------------------------|
| SCK       | GPIO18     | VSPI SCK                     |
| MOSI      | GPIO23     | VSPI MOSI                    |
| MISO      | GPIO19     | VSPI MISO                    |
| NSS (CS)  | GPIO5      | VSPI CS                      |
| DIO0      | GPIO4      | RX done interrupt (required) |
| RST       | GPIO14     | Reset line                   |

---

## Firmware Architecture

### Pico Side -- `lib/lora.py`

Follow the existing module pattern (class-based, pin numbers as constructor args, matching
`exhaust.py` template).

```python
# Instantiation example (from main.py)
lora = LoRa(
    spi_id=1,
    sck=10, mosi=11, miso=12,
    cs=13, rst=28,
    frequency=433_000_000
)
```

Note: no `irq` parameter -- DIO0 is not connected on the Pico side.

**Class responsibilities:**
- Initialise SPI1 using default pin block:
  `SPI(1, baudrate=1_000_000, sck=Pin(10), mosi=Pin(11), miso=Pin(12))`
- Configure SX1278 registers via SPI (frequency, bandwidth, spreading factor, coding rate)
- Expose `send(payload: bytes) -> bool` -- blocking send with polling-based completion check
- Expose `send_telemetry(data: dict) -> bool` -- serialises dict to JSON and calls `send()`
- Expose `send_alert(code: str, message: str) -> bool` -- for fault conditions
- No receive path on the Pico

**TX completion -- register polling (no DIO0 needed):**

Because DIO0 is not connected, TX completion is determined by polling the SX1278 IRQ flags
register (RegIrqFlags, address 0x12) over SPI. The TxDone flag is bit 3 (mask 0x08).

Procedure in `send()`:
1. Write payload to FIFO, set mode to TX (RegOpMode = 0x83)
2. Calculate maximum expected airtime for the payload length and LoRa parameters
3. Poll RegIrqFlags in a loop (5 ms intervals) until bit 3 (TxDone) is set, or timeout
4. Clear all IRQ flags by writing 0xFF to RegIrqFlags
5. Return device to sleep mode (RegOpMode = 0x80)
6. Return True on TxDone, False on timeout

For SF9 / 125 kHz / 4:5 coding rate, airtime for a 100-byte payload is approximately 330 ms.
Use a timeout of 2000 ms to give comfortable headroom across payload sizes.

This approach is functionally equivalent to interrupt-driven TX confirmation for a
transmit-only module on a 30-second heartbeat schedule. There is no meaningful latency
difference in practice.

**Suggested LoRa parameters (balance range vs. latency):**

| Parameter       | Value    | Notes                                              |
|-----------------|----------|----------------------------------------------------|
| Frequency       | 433.0 MHz| Within 433.05-434.79 MHz ISM band (Canada RSS-210) |
| Bandwidth       | 125 kHz  | Standard                                           |
| Spreading Factor| SF9      | ~250 bps, good for wooded 200m path               |
| Coding Rate     | 4/5      |                                                    |
| TX Power        | 17 dBm   | Ra-02 maximum; reduce if interference observed     |
| Preamble        | 8 symbols| Default                                            |

**Transmission schedule:**
- Telemetry heartbeat: every 30 seconds (configurable)
- Fault alerts: immediate, retried up to 3 times with 2s spacing
- SPI1 (LoRa) and SPI0 (SD card) are independent buses -- no bus contention risk

**Power note:** Ra-02 draws ~120 mA during TX. At 17 dBm on a 30-second interval,
duty cycle is well under 1% -- within the 10% limit for 433 MHz ISM use in Canada.

**Logger integration:** Accepts optional `logger=None`; calls `logger.event("lora", ...)`
on init, successful send, send timeout, and RST events.

---

### ESP32 Side -- Arduino Sketch

**Libraries required (Arduino IDE Library Manager):**
- `RadioLib` by Jan Gromes -- SX1276/78 support, well-maintained
- `PubSubClient` by Nick O'Leary -- MQTT client
- `ArduinoJson` by Benoit Blanchon -- JSON parsing

**Sketch structure:**

```
gateway/
  gateway.ino       -- setup(), loop()
  config.h          -- WiFi credentials, MQTT broker IP, topic prefixes
  lora_handler.cpp  -- RadioLib init, onReceive() callback
  mqtt_handler.cpp  -- MQTT connect/reconnect, publish helpers
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
2. RadioLib interrupt-driven receive: `radio.startReceive()` -> DIO0 fires -> `onReceive()`
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
  "vent_open": true,
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
  "message": "Lumber zone temp 85 deg C exceeds limit",
  "value": 85.0,
  "limit": 80.0
}
```

**Alert codes:**

| Code              | Condition                                            |
|-------------------|------------------------------------------------------|
| `OVER_TEMP`       | Either SHT31 zone exceeds stage limit                |
| `SENSOR_FAIL`     | SHT31 read error or timeout                          |
| `FAN_STALL`       | Exhaust fan tach reads 0 RPM when commanded on       |
| `HEATER_TIMEOUT`  | SSR on continuously beyond safety threshold          |
| `SD_FAIL`         | SD card write error                                  |
| `LORA_TIMEOUT`    | TX did not complete within timeout (TxDone not seen) |
| `STAGE_COMPLETE`  | Drying schedule stage transition (informational)     |
| `SCHEDULE_DONE`   | Full drying schedule completed                       |

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

### Option A -- ntfy.sh (recommended, simplest)

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

### Option B -- Home Assistant

If Home Assistant is already running on the Pi4, add an MQTT sensor for `kiln/telemetry`
and automation triggers on `kiln/alert`. No additional code needed beyond HA configuration.

---

## BOM Additions

| Item                                   | Qty | Est. Cost (CAD) | Notes                                    |
|----------------------------------------|-----|-----------------|------------------------------------------|
| AI-Thinker Ra-02 433 MHz LoRa module   | 2   | $6-10           | AliExpress; verify 433 MHz, not 915 MHz  |
| u.FL to SMA pigtail + 433 MHz antenna  | 2   | often included  | Check listing; otherwise ~$2 each        |
| 100 nF ceramic capacitor (decoupling)  | 2   | ~$0.10          | Likely already in parts stock            |

The ESP32-WROOM-32 devboard is already owned ($0).

---

## Testing Plan

### Bench Test (at home, short range)

1. Wire both Ra-02 modules per wiring tables above
2. Load minimal MicroPython `lora_test.py` on Pico -- sends a packet every 5s
3. Verify TxDone flag is seen via register polling within expected airtime window
4. Flash ESP32 gateway sketch with home WiFi credentials and laptop MQTT broker IP
5. Install Mosquitto on laptop: `brew install mosquitto` / `apt install mosquitto`
6. Subscribe on laptop: `mosquitto_sub -t "kiln/#" -v`
7. Verify packets appearing with correct JSON structure
8. Verify RSSI/SNR values are reasonable (will be very strong at short range -- expected)
9. Test alert path: force a LORA_TIMEOUT by pulling CS high during TX; verify alert fires

### Pre-Deployment Test (at cottage, full path)

1. Swap `config.h` to cottage WiFi credentials and Pi4 MQTT broker IP
2. Flash ESP32, place at cottage
3. Power kiln Pico from bench supply at kiln location
4. Monitor `kiln/gateway` topic -- watch `last_lora_rssi` and `packets_missed`
5. Target RSSI: better than -115 dBm (SX1278 sensitivity at SF9). Expect -90 to -105 dBm
   for 200m wooded path at 433 MHz.
6. If RSSI is marginal, increase spreading factor to SF10 or SF11 (reduces data rate but
   improves link budget by 3-6 dB per step)

---

## Open Items

- [ ] Source Ra-02 modules (AliExpress -- allow 3-4 weeks shipping)
- [ ] Decide on notification stack: ntfy.sh vs Home Assistant
- [ ] Implement `lib/lora.py` on Pico (Claude Code) -- no irq pin; TX polling only
- [ ] Implement ESP32 gateway sketch (Claude Code or Arduino IDE direct)
- [ ] Set up Mosquitto on laptop for bench testing