# Firmware (`droidrefit/`)

MicroPython for a plain **ESP32** (stock `ESP32_GENERIC`, tested on v1.29)
driving an astromech droid: one dome servo, a DFPlayer Mini for sound, and a
4-pixel WS2812 dome-light string. Controlled from Home Assistant over MQTT
**and** from a built-in local web page — both, or either. First-run WiFi/MQTT
setup is a captive portal; updates are over-the-air from this repo.

Replaces the retired CyberBrick core board (see
[cyberbrick-origins.md](cyberbrick-origins.md) and
[project-notes.md](project-notes.md)) — the reason for the move was a real
brownout: servo + LEDs + WiFi through CyberBrick's onboard regulation. The fix
is an external 5 V rail everything hangs off directly; the
[carrier PCB](../hardware/droidrefit-pcb/DESIGN.md) makes that permanent.

## Hardware

| Signal | GPIO | Notes |
|---|---|---|
| Dome servo PWM | `GPIO18` | 50 Hz, `machine.PWM` |
| DFPlayer UART2 TX / RX | `GPIO17` / `GPIO16` | 9600 baud |
| DFPlayer BUSY | `GPIO4` | active-low, playback-finished detect |
| NeoPixel data | `GPIO5` | 4 pixels; via a `74AHCT1G125` 3.3→5 V shifter on the PCB (often fine direct on a short bench run) |
| Factory-reset / safe-mode | `GPIO0` | the devkit BOOT button, read while running |

Power: **5 V, 3 A+** into the rail. Servo, DFPlayer and pixels wire straight to
it; the ESP32 taps it at `VIN`. Realistic combined peak ~2 A (servo stall
~1 A + WiFi bursts ~0.5 A + 4 pixels white ~0.24 A + amp). The carrier board
takes 5 V over USB-C (16-pin receptacle + 5.1 kΩ CC pull-downs = "5 V device").
No 12 V input without adding a buck + bumping the bulk caps to 16–25 V.

## Getting it onto a board

```
pip install mpremote
python3 tools/deploy.py --wipe --run      # first load
python3 tools/deploy.py                    # after an edit
```

`deploy.py` copies `app/`, `lib/`, `boot.py`, `main.py`, then imports every
module on the device so a bad transfer fails loudly instead of at next boot.
It never touches `/config.json` or `/noboot.txt`.

**First boot with no config** → the board hosts an open-ended WiFi AP
`R2-D2-XXXX` (WPA2, sticker password). Join it from any phone; the setup page
auto-opens. Enter WiFi + a droid name, tick "Connect to Home Assistant" for
the MQTT fields, save. It reboots onto your network.

For bench work, skip the portal: create `app/config_baked.py` (gitignored,
copy `config_baked.py.example`) with your WiFi/MQTT and it's used whenever
there's no `/config.json`.

## Architecture

`/boot.py` (adds `/lib` to the path) → `/main.py` (recovery checks, then
`from app.main import run`) → the `app` package. Module graph is a DAG
(MicroPython has no circular imports):

```
hw, core, config, version   leaves
sound  -> core, hw           servo -> core, sound, hw     leds -> core, hw
diag   -> core               ota   -> core, version
net    -> core, sound, servo, diag, ota
webui  -> core, net, servo, sound, diag, ota
provisioning -> config, core         main -> everything
```

Shared state lives in `core` (`state`, `servo_state`, `conn`, `link_state`,
`cfg`). Feature modules do `from app import core` and read `core.X` at call
time — never `from app.core import cfg` (it's `None` until `core.init()`).
Every long-lived task runs under `core.supervise()`, which restarts just that
task on a crash and counts restarts (surfaced in diagnostics).

`app/main.py` `main()`: connect (WiFi + MQTT, or WiFi only if MQTT is off) →
`ota.confirm()` → gather the task set. MQTT tasks only run when
`cfg["mqtt_enabled"]`.

### Recovery hatches (root `/main.py`, self-contained — no `app` import)

| Hatch | Trigger | Effect |
|---|---|---|
| Safe mode | `noboot.txt` present at FS root | print a banner, don't start the app → clean REPL |
| Ctrl-C window | during the 2 s boot countdown | same |
| OTA rollback | `/ota.flag` present and boot count > 2 | restore `/bak` (pre-update files), reboot the old build |
| Factory reset | hold `GPIO0` ~10 s while running | scream, wipe `/config.json`, reboot to the portal |
| Connect-fail fallback | (planned) WiFi never associates for ~5 min | reboot to the portal |

## Config store — `app/config.py`

`/config.json` at the FS root is the live config, written by the portal.
`load()` returns a normalised dict or `None` (unconfigured); `save()` is
atomic; `wipe()` clears it. `topic_prefix` (a slug of the droid name, or
`r2d2`) is the single per-droid identity — MQTT client id, the whole topic
tree, every HA `unique_id`, the HA device id. Set it to `r2d2` on a returning
single unit to keep existing entities.

## Control — Home Assistant

MQTT auto-discovery publishes on connect. Topics are `<prefix>/…`:

| Entity | Command | State |
|---|---|---|
| Mode (select, 8) | `<prefix>/command` `{"mode": …}` | `<prefix>/mode/state` |
| Sound (select, 7 folders + idle) | `<prefix>/sound/command` `{"sound": …}` | `<prefix>/sound/state` |
| Volume (number 0–30) | `<prefix>/sound/volume/set` | `<prefix>/sound/volume/state` |
| Debug logging (switch) | `<prefix>/debug/set` | `<prefix>/debug/state` |
| Firmware (update) — *only if `ota_url` set* | `<prefix>/ota/set` `install` | `<prefix>/ota` |
| Heartbeat (sensor) | — | `<prefix>/heartbeat` (60 s, `expire_after` 180) |
| Diagnostics (12 sensors) | — | `<prefix>/diag` JSON + `<prefix>/diag/reset` |

`<prefix>/log` carries `log_always()` output (queued and drained one line per
~20 ms so bursts don't truncate on the socket). Availability is re-asserted
every heartbeat so a stray LWT `offline` self-heals.

`net.apply_mode/apply_sound/apply_volume` are the single code path — MQTT, the
web panel, and any future button all call them.

## Control — local web panel (`app/webui.py`)

Async HTTP on port 80, always up (with or without MQTT). Reach it at the IP
logged on boot, or `http://<hostname>.local/` (best-effort mDNS via
`network.hostname()`). Mode + sound buttons (sound button lights while
playing), volume slider, live-polled state, and a **Diagnostics** readout. A
**Firmware** section (installed → latest, Update button) appears only when
`ota_url` is set. Optional `web_pin` gates the state-changing POSTs.

## Servo — `app/servo.py`

One always-running `servo_task` owns the PWM. Each 20 ms tick it asks the
current mode's behaviour for a setpoint and rate-limits `pos` toward it
(`_approach`: trapezoidal, `SERVO_MAX_ACCEL` + per-mode `SPEED_*`). Because
`pos`/`vel` can't jump, a mode change at any instant is a smooth redirect — no
task cancellation. Behaviours are plain state machines (`_Hold`, `_Sweep`,
`_Wander`, `_Tremble`) built by `SERVO_BEHAVIORS[mode]`; they never touch the
servo. `SERVO_ON_ENTER` fires one-shot side effects (system_crash → scream);
`SERVO_MODE_TIMEOUT` auto-reverts system_crash to the prior mode after 10 s.
No position persistence — starts at 90° every boot.

## LEDs — `app/leds.py`

`PixelChannel` per-pixel state machine: `off` / `solid` / `blink` / `breathe`
/ `twinkle` / `rainbow`. `LED_REACTIONS[mode]` is four
`(pattern, colour, colour2, period, bright)` tuples for
`[holoprojector, logic display, rear circle, light bar]`. `led_task` renders
at ~25 fps, independent of the servo (both just read `core.state["mode"]`).
Starter patterns — tune on real pixels.

## Sound — `app/sound.py`

`SOUND_FOLDERS` maps a category to `(folder, count)`; a trigger plays a random
track in that folder. `busy_monitor_task` watches BUSY to clear
`state["sound"]` when playback ends (with a ~2 s fallback for very short
tracks), so the UI can un-light the button.

## Diagnostics — `app/diag.py`

`snapshot()` → reset cause, uptime, free heap (%), free/total flash, CPU MHz,
RSSI, IP, WiFi SSID + channel, MCU temp, time-synced, task-restart count,
reconnect count. Published to `<prefix>/diag` every 30 s (HA diagnostic
sensors) and served at `GET /diag`. Reset cause is a one-shot retained publish.

## Updating

**Normal path: USB** — `python3 tools/deploy.py` (see *Getting it onto a
board* above). That's the whole update story for most builds.

### On-device OTA — `app/ota.py`, opt-in

Off unless `core.cfg["ota_url"]` is set (`config.save({"ota_url": "..."})` from
the REPL). `ota.enabled()` gates the HA Update entity, the web-panel Firmware
section, the `<prefix>/ota/set` subscription, and the boot auto-check — with no
`ota_url` none of that is published, so there's no dead button.

**Why opt-in:** a classic ESP32 can't complete a TLS handshake to GitHub while
the app is running. mbedTLS needs ~16 KB *contiguous* and MicroPython's GC
doesn't compact, so even with ~65 KB free the allocation fails `ENOMEM`. OTA
therefore pulls **plain HTTP from a LAN mirror** — any static server over the
`droidrefit/` tree (`python3 -m http.server` in the repo root, or drop it in
Home Assistant's `config/www/`). `ota_url` is that base, e.g.
`http://homeassistant.local:8123/local/droidrefit`. The `mip`/GitHub path is
still in the code for PSRAM boards (S2/S3) but untested there.

- **Version**: `app/version.py` `VERSION`. A release = bump it, commit, push,
  refresh the mirror.
- **Manifest**: `droidrefit/package.json` — `check()` reads `version.py`,
  `_http_pull()` downloads each listed path to `<path>.ota` then renames them
  all in once every file has arrived (a mid-download failure leaves the running
  build intact).
- **Backup / rollback**: `update()` copies the managed files to `/bak` and
  writes `/ota.flag`; root `/main.py` counts boots in it and restores `/bak`
  after 3 failures (a crash while `/ota.flag` exists forces a reset so the
  counter advances). `app/main.py` calls `ota.confirm()` once the link is up.
  `boot.py` and root `main.py` are **not** OTA-managed — USB flash only — so
  the bootstrap + rollback are always known-good.
- **Known gap**: a bad build that *hangs* rather than crashes never advances
  the boot counter — recovery there is `noboot.txt` or a USB re-flash.

## Bench tools — `droidrefit/bench/`

`test-boot.py` (servo only) and `test-boot-with-sound.py` (servo + DFPlayer)
— standalone scripts, flash one as `main.py` to smoke-test a bare board.
**Pre-refactor**: they use the old blocking `goto()` motion, not
`app/servo.py`'s driver.
