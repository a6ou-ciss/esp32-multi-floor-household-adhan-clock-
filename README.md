# ESP32 Multi-Floor Adhan Clock

A distributed Adhan system for a home with more than one floor. A central hub gets
prayer times for a chosen mosque from the Mawaqit API (through Kimono Labs) and tells
one speaker node per floor to start playing at the same instant. Each node plays the
Adhan from **its own flash memory**, so there is no SD card and no audio streamed
over Wi-Fi.

A standalone Python player for a single Raspberry Pi is also included.

## Status

| Component | State |
|---|---|
| `pi/raspberry_adhan_player.py` (API client, cache, grace period, latency lead, signal handling) | Working. Verify the times against your mosque. |
| Hub firmware (Wi-Fi, SNTP, DS3231, API fetch, UDP broadcast) | Planned |
| Node firmware (LittleFS audio, MP3 decode, I2S, timed start) | Planned |
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
    ESP32 + DS3231 RTC  │       ESP32 (audio in flash) -> I2S -> MAX98357A -> speaker
    Mawaqit API fetch   │       LittleFS: /adhan.mp3, /fajr.mp3
    NTP + timezone/DST  │       keeps its own copy of the schedule
    broadcasts PLAY ────┴─────► UDP "PLAY <prayer> <epoch_ms>"
```

How it works:

- The hub fetches and caches prayer times, then broadcasts `PLAY <prayer> <epoch_ms>`
  several seconds before the prayer and repeats the packet a few times. Nodes ignore
  duplicates (same prayer and epoch), so packet loss and Wi-Fi delay don't matter.
- `epoch_ms` is an absolute start time. Each node opens the file from flash and
  pre-fills the I2S buffer, then starts playback at
  `epoch_ms - LOCAL_LATENCY_MS + NODE_OFFSET_MS`.
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
│       ├── partitions.csv        # flash layout: app + LittleFS audio partition
│       ├── include/secrets.h.example
│       ├── data/                 # your MP3s go here (git-ignored); uploaded to flash
│       │   └── README.md
│       └── src/main.cpp
├── hardware/                     # planned: bom.csv, pinout.md, KiCad files
├── docs/                         # planned: protocol.md, calibration.md
├── audio/README.md               # how to prepare your audio files
└── tools/send_play.py            # planned: test UDP broadcaster
```

## Hardware

**Per speaker node (x2):**

- ESP32-WROOM-32E, ideally the 8 MB or 16 MB flash variant (4 MB works with a smaller file)
- MAX98357A I2S amplifier (mono, about 3 W into 4 ohm at 5 V)
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
| Status LED | 4 |
| User button | 32 |
| Volume pot (ADC1) | 34 |

No SD card is used, so GPIO 5, 18, 19 and 23 are free for your own additions.

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

## Preparing the audio (for the ESP32 nodes)

The audio is stored in a LittleFS partition in the node's flash. Space is limited:

| Flash size | Partition table | Space for audio |
|---|---|---|
| 4 MB | `firmware/node/partitions.csv` (as provided) | about 2.4 MB |
| 8 / 16 MB | edit `partitions.csv` to enlarge the `spiffs` entry | 5 MB or more |

Approximate file sizes for a 3-minute recording:

| Encoding | Size |
|---|---|
| 128 kbps stereo | about 2.9 MB (too big for the 4 MB layout) |
| 96 kbps mono | about 2.2 MB |
| 64 kbps mono | about 1.4 MB |

Re-encode to mono (the amplifier is mono anyway) with ffmpeg:

```bash
ffmpeg -i source.mp3 -ac 1 -ar 44100 -b:a 64k adhan.mp3
```

Put `adhan.mp3` (and optionally `fajr.mp3`) in `firmware/node/data/`. If you use both
files, their combined size must fit in the partition. Only use recordings you have the
right to use; audio files are git-ignored.

## Setup: ESP32 hub and nodes (planned)

The firmware is not written yet. The intended workflow:

1. Install [PlatformIO](https://platformio.org/).
2. In `firmware/hub` and `firmware/node`, copy `include/secrets.h.example` to
   `include/secrets.h` and fill in the Wi-Fi credentials, mosque slug, POSIX timezone
   and (for nodes) the latency and offset.
3. Flash each node, from `firmware/node`:

   ```bash
   pio run -t upload       # firmware
   pio run -t uploadfs     # uploads data/ (the MP3s) to the LittleFS partition
   ```

   Re-run `uploadfs` only when the audio changes. Running `upload` does not erase it,
   but changing `partitions.csv` does require re-running both.
4. Flash the hub from `firmware/hub` with `pio run -t upload`.
5. Power the hub and nodes, then use the hub's test button to broadcast a test `PLAY`.

## Setup: standalone Raspberry Pi player

Tested target: Raspberry Pi OS (Bullseye or Bookworm), Python 3.9+. No pip packages
are required. The Pi player reads audio from a normal file on its SD card; the
flash-storage approach above applies only to the ESP32 nodes.

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

- For Bluetooth output, pair the speaker with `bluetoothctl` and set the audio device.
  Bluetooth adds 150 to 350 ms of latency, so calibrate (below).
- Exit code 78 means a configuration error, and the unit file stops systemd from restarting on it.

## Latency calibration

1. Set `ADHAN_LATENCY_MS` (Pi) or `LOCAL_LATENCY_MS` (node) to 0.
2. Trigger a test Adhan and record a phone seconds-clock and the audio together.
3. Set the latency to the gap between the target second and the first audible sound.
   Reading from flash avoids SD card open delays, so node latency is usually lower than
   with an SD card, but measure your own setup.
4. For two floors, tune each node's `NODE_OFFSET_MS` by ear so the sound arrives together.
5. Keep SNTP sync short (about 15 minutes) so node clocks stay within a few milliseconds.

## Safety and limits

- The boards carry no mains voltage. Use certified 5 V USB adapters.
- UDP `PLAY` packets are unauthenticated: anyone on your LAN could trigger playback.
  Keep the system on a trusted network.
- Changing the audio means re-flashing the filesystem over USB (`uploadfs`); there is
  no over-the-air audio update in this design.
- Review any PCB design (ERC/DRC and datasheets) before ordering boards.
- This project is not a substitute for checking prayer times with your local mosque.

## License

MIT. See [LICENSE](LICENSE).
