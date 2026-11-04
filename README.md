
# ESP32 Multi-Floor Adhan Clock

A distributed Adhan system for a home with more than one floor. A central hub gets
prayer times for a chosen mosque from the Mawaqit API (through Kimono Labs) and tells
one speaker node per floor to start playing at the same instant. Each node plays the
Adhan from its own microSD card, so no audio is streamed over Wi-Fi.

A standalone Python player for a single Raspberry Pi is also included.

## Status

| Component | State |
|---|---|
| `pi/raspberry_adhan_player.py` (API client, cache, grace period, latency lead, signal handling) | Working. Verify the times against your mosque. |
| Hub firmware (Wi-Fi, SNTP, DS3231, API fetch, UDP broadcast) | Planned |
| Node firmware (SD, MP3 decode, I2S, timed start) | Planned |
| `tools/send_play.py` (test broadcaster) | Planned |
| Hub / node PCBs | Schematic-level design only, unverified |

> **API parsing is unverified.** The response schema of the Kimono Mawaqit endpoints
> was not available when the Python script was written, so it searches for the five
> prayers heuristically and refuses to play if it cannot find five valid, increasing
> times. Always run `--show-times` and compare against your mosque's published times.

## Architecture

```
                  Home Wi-Fi router
           ┌────────────┼─────────────┐
    [CENTRAL HUB]       │       [SPEAKER NODE x2]
    ESP32 + DS3231 RTC  │       ESP32 -> I2S -> MAX98357A -> speaker
    Mawaqit API fetch   │       microSD: adhan.mp3 / fajr.mp3
    NTP + timezone/DST  │       keeps its own copy of the schedule
    broadcasts PLAY ────┴─────► UDP "PLAY <prayer> <epoch_ms>"
```

How it works:

- The hub fetches and caches prayer times, then broadcasts `PLAY <prayer> <epoch_ms>`
  several seconds before the prayer and repeats the packet a few times. Nodes ignore
  duplicates (same prayer and epoch), so packet loss and Wi-Fi delay don't matter.
- `epoch_ms` is an absolute start time. Each node opens the file and pre-fills the I2S
  buffer, then starts playback at `epoch_ms - LOCAL_LATENCY_MS + NODE_OFFSET_MS`.
- Nodes also keep the schedule locally, so they can still play if the hub is down.
- For a single speaker, skip the ESP32s and run the Pi player on its own.

### Packet format (planned)

```
PLAY <prayer> <epoch_ms>
e.g.  PLAY fajr 1767254400000
```

`<prayer>` is one of `fajr dhuhr asr maghrib isha`. Packets are sent to the broadcast
address on `UDP_PORT` (default 5005). They are unauthenticated (see Safety).

## Repository layout

```
esp32-multi-floor-adhan-clock/
├── README.md
├── LICENSE
├── .gitignore
├── .env.example
├── pi/                           # standalone Raspberry Pi player (working)
│   ├── raspberry_adhan_player.py
│   ├── config.json.example
│   └── systemd/adhan.service
├── firmware/                     # planned
│   ├── hub/                      # PlatformIO project
│   │   ├── platformio.ini
│   │   ├── include/secrets.h.example
│   │   └── src/main.cpp
│   └── node/
│       ├── platformio.ini
│       ├── include/secrets.h.example
│       └── src/main.cpp
├── hardware/                     # planned: bom.csv, pinout.md, KiCad files
├── docs/                         # planned: protocol.md, calibration.md
├── audio/README.md               # where to put your own adhan files
└── tools/send_play.py            # planned: test UDP broadcaster
```

## Hardware

**Per speaker node (x2):**

- ESP32-WROOM-32E (the original ESP32; other variants work for this design too, since
  Bluetooth isn't used)
- MAX98357A I2S amplifier (mono, about 3 W into 4 ohm at 5 V)
- microSD module (SPI mode)
- 4 ohm passive speaker
- 5 V / 2 A USB power supply

**Hub (x1):**

- ESP32 dev board
- DS3231 RTC module with CR2032 battery
- Optional SSD1306 OLED (I2C)
- 5 V USB power supply

### Speaker node pin map

| Function | GPIO |
|---|---|
| I2S BCLK | 26 |
| I2S LRCLK (WS) | 25 |
| I2S DOUT | 22 |
| Amp SD_MODE (mute) | 27 (100k pull-down so the amp is off at boot) |
| SD CS | 5 |
| SD SCK | 18 |
| SD MISO | 19 |
| SD MOSI | 23 |
| Status LED | 4 |
| User button | 32 |
| Volume pot (ADC1) | 34 |

### Hub pin map

| Function | GPIO |
|---|---|
| I2C SDA (DS3231 at 0x68, OLED at 0x3C) | 21 |
| I2C SCL | 22 |
| Mode button | 32 |
| Test-broadcast button | 33 |
| Status LED | 4 |
| Wi-Fi LED | 16 |

Avoid GPIO 0, 2, 12 and 15 for other uses, since they are strapping pins that affect boot.

## Wiring (prototype with breakout boards)

### Node: ESP32 to MAX98357A

| MAX98357A pin | Connect to |
|---|---|
| VIN | 5 V |
| GND | GND |
| BCLK | GPIO 26 |
| LRC | GPIO 25 |
| DIN | GPIO 22 |
| SD | GPIO 27 (with 100k pull-down to GND) |
| GAIN | Leave unconnected for the default 9 dB (see the datasheet to change it) |
| Speaker + / - | Speaker terminals |

The amplifier's outputs are bridge-tied. **Never connect either speaker terminal to
ground.**

### Node: ESP32 to microSD module (SPI)

| SD module pin | Connect to |
|---|---|
| CS | GPIO 5 |
| SCK | GPIO 18 |
| MISO | GPIO 19 |
| MOSI | GPIO 23 |
| VCC | 3.3 V (or 5 V only if the module has an onboard regulator and level shifting) |
| GND | GND |

Format the card as FAT32 and put `adhan.mp3` (and optionally `fajr.mp3`) in its root.

### Hub: ESP32 to DS3231 (and OLED on the same bus)

| Module pin | Connect to |
|---|---|
| SDA | GPIO 21 |
| SCL | GPIO 22 |
| VCC | 3.3 V |
| GND | GND |

### Power

Use a certified 5 V USB adapter per location (rated 2 A for nodes). Nothing on these
boards touches mains voltage.

## Setup: standalone Raspberry Pi player

Tested target: Raspberry Pi OS (Bullseye or Bookworm), Python 3.9+. No pip packages
are required.

```bash
sudo apt update && sudo apt install -y mpg123 python3
git clone https://github.com/<your-user>/esp32-multi-floor-adhan-clock.git
cd esp32-multi-floor-adhan-clock/pi
cp config.json.example config.json        # then edit it

# 1. Find your mosque slug
python3 raspberry_adhan_player.py --search "Paris"

# 2. Verify the times against the mosque's published times
python3 raspberry_adhan_player.py --mosque <slug> --show-times

# 3. Test audio (put your own athan.mp3 next to the script first)
python3 raspberry_adhan_player.py --test-audio --volume 60

# 4. Run as a service
sudo cp systemd/adhan.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now adhan
journalctl -u adhan -f
```

Configuration precedence: CLI flags, then environment variables, then `config.json`,
then defaults. The script reads real environment variables and does **not** load
`.env` files by itself. To use `.env.example`, either:

- add `EnvironmentFile=/home/pi/adhan/.env` to the `[Service]` section of the systemd unit, or
- for a manual run: `set -a; source .env; set +a; python3 raspberry_adhan_player.py`

Notes:

- Audio files are not committed (they may be copyrighted). See `audio/README.md`.
- For Bluetooth output, pair the speaker with `bluetoothctl` and set the audio device.
  Bluetooth adds 150 to 350 ms of latency, so calibrate (below).
- Exit code 78 means a configuration error, and the unit file stops systemd from restarting on it.

## Setup: ESP32 hub and nodes (planned)

The firmware is not written yet. The intended workflow:

1. Install [PlatformIO](https://platformio.org/).
2. In `firmware/hub` and `firmware/node`, copy `include/secrets.h.example` to
   `include/secrets.h` and fill in the Wi-Fi credentials, mosque slug, POSIX timezone
   and (for nodes) the latency and offset.
3. Flash each board with `pio run -t upload`.
4. Put an SD card with `adhan.mp3` (and optionally `fajr.mp3`) in each node.
5. Power the hub and nodes, then use the hub's test button to broadcast a test `PLAY`.

## Latency calibration

1. Set `ADHAN_LATENCY_MS` (Pi) or `LOCAL_LATENCY_MS` (node) to 0.
2. Trigger a test Adhan and record a phone seconds-clock and the audio together.
3. Set the latency to the gap between the target second and the first audible sound.
   Typical values are 100 to 300 ms for wired audio and more for Bluetooth, but always
   measure your own setup.
4. For two floors, tune each node's `NODE_OFFSET_MS` by ear so the sound arrives together.
5. Keep SNTP sync short (about 15 minutes) so node clocks stay within a few milliseconds.

## Safety and limits

- The boards carry no mains voltage. Use certified 5 V USB adapters.
- UDP `PLAY` packets are unauthenticated: anyone on your LAN could trigger playback.
  Keep the system on a trusted network.
- Review any PCB design (ERC/DRC and datasheets) before ordering boards.
- This project is not a substitute for checking prayer times with your local mosque.

## License

MIT. See [LICENSE](LICENSE).
