# droidrefit — an offline-first astromech droid

Custom MicroPython firmware for a stationary R2-D2 display build. It runs
**standalone** — three front-panel buttons (mode ▲ / mode ▼ / sound) drive the
dome servo, sound, and dome lights. WiFi + **Home Assistant** (pick a reaction
from a dashboard) are an optional layer, off by default; hold the Sound button
~5 s to set up WiFi. No RC transmitter.

> **Repo name is historical.** This started as a firmware swap on a *CyberBrick*
> ESP32-C3 core board. That board browned out under servo + NeoPixels + WiFi,
> so the project moved to a plain **ESP32** with an external 5 V supply and a
> from-scratch modular firmware. The reverse-engineering writeup from that first
> chapter is [docs/cyberbrick-origins.md](docs/cyberbrick-origins.md).

## What's here

| Path | What |
|---|---|
| [`droidrefit/`](droidrefit/) | the firmware — `app/` package, `lib/`, `boot.py`, `main.py`, `package.json` (OTA manifest) |
| [`tools/deploy.py`](tools/deploy.py) | one-command host→board sync (`mpremote`), also the initial-load tool |
| [`hardware/droidrefit-pcb/`](hardware/droidrefit-pcb/) | KiCad carrier board — sockets the ESP32 devkit + DFPlayer, one 5 V rail, USB-C in. [DESIGN.md](hardware/droidrefit-pcb/DESIGN.md) |
| [`docs/firmware.md`](docs/firmware.md) | **the firmware reference** — architecture, boot flow, control surfaces, OTA, recovery |
| [`docs/cyberbrick-origins.md`](docs/cyberbrick-origins.md) | the original CyberBrick writeup (retired board, but the LED/MQTT approach carried forward) |
| [`docs/project-notes.md`](docs/project-notes.md) | running history — decisions, bugs found and fixed |

## Hardware (as-built)

Plain ESP32 devkit · one dome servo (GPIO18) · DFPlayer Mini + speaker
(GPIO16/17, BUSY GPIO4) · 4× WS2812 dome lights (GPIO5, via a `74AHCT1G125`
level shifter) · 3 buttons (GPIO32/33/25, pin↔GND) · a shared **5 V, 3 A+**
rail everything hangs off directly. The
[carrier PCB](hardware/droidrefit-pcb/DESIGN.md) (100×70 mm, USB-C in,
JLCPCB-assembled) makes that permanent; a breadboard works for bench work if
you power the ESP32 over its own USB.

## Quick start

```
pip install mpremote
python3 tools/deploy.py --wipe
```

`deploy.py` stages the copy in safe mode and SHA-verifies every file, retrying
over a flaky USB link; a failed run leaves the board at a safe REPL, and
re-running converges. `--stay` leaves it in safe mode; `--format` reformats
the flash (last resort).

> **After flashing, hard power-cycle the board** (pull power / press EN — not
> just a soft reset). `deploy.py` ends with a soft reset, but hardware timers,
> the UART, PWM, and singleton driver objects survive that; only a cold boot
> gives the clean state the firmware actually runs in.

It boots straight into the app, **offline**. Use the buttons: mode ▲ / mode ▼
cycle the 7 reactions, Sound fires a clip.

**To add Home Assistant:** hold the Sound button ~5 s → the board reboots into
a WiFi AP **`R2-D2-XXXX`**. Join it from a phone, the setup page opens, enter
WiFi + a droid name (tick "Connect to Home Assistant" for the MQTT broker
fields), save. It reboots onto your network and HA auto-discovers it over MQTT
(Mode / Sound / Volume + diagnostic sensors). There is no always-on web panel.

**Updates go over USB** — after an edit, `python3 tools/deploy.py`. On-device
OTA exists but is opt-in, needs `network_enabled`, and — on a classic ESP32 —
a plain-HTTP LAN mirror (`ota_url`), because it can't TLS to GitHub while the
app runs. Detail in [docs/firmware.md](docs/firmware.md).

## Recovery

- `noboot.txt` at the flash root → boot straight to a clean REPL (`deploy.py` uses this)
- hold the Sound button ~5 s → reboot into the WiFi setup portal
- hold the ESP32 BOOT button ~10 s while running → factory reset (wipe config, reboot to portal)
- a crash-looping OTA build auto-rolls-back after 3 failed boots

## Credits

- **The droid** — [*Build-A-Droid: CyberBrick based robot kit*](https://makerworld.com/en/models/1549117-build-a-droid-cyberbrick-based-robot-kit)
  by **Neebick** on MakerWorld. That's the printed astromech this firmware
  drives; this repo is only the electronics + software, not the model.
  Licensed Creative Commons (see the model page for the exact variant and
  follow its attribution / non-commercial / share-alike terms).
- **`droidrefit/lib/umqtt/simple.py`** — vendored **unmodified** from
  [micropython-lib](https://github.com/micropython/micropython-lib/blob/master/micropython/umqtt.simple/umqtt/simple.py)
  (MIT). MicroPython ships no MQTT client.
- **`droidrefit/lib/dfplayer.py`** — a minimal DFPlayer Mini (YX5200) UART
  driver written for this project; the frame/checksum protocol is per the
  DFPlayer Mini datasheet and the widely-published community command tables,
  not copied from a specific library.
- **[MicroPython](https://micropython.org/)** (MIT) — the runtime.
- The earlier CyberBrick chapter leaned on CyberBrick's own `/bbl` driver
  library — see [docs/cyberbrick-origins.md](docs/cyberbrick-origins.md#acknowledgments).
