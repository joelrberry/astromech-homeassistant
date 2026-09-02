# Firmware (`droidrefit/`)

MicroPython for a plain **ESP32** (stock `ESP32_GENERIC`, tested on v1.29)
driving an astromech droid: one dome servo, a DFPlayer Mini for sound, and a
4-pixel WS2812 dome-light string. **Offline-first** — it boots and runs
standalone, controlled by three front-panel buttons (mode ▲ / mode ▼ / sound).
WiFi + Home Assistant (MQTT) are an optional layer, **off by default**; hold the
Sound button ~5 s to bring up a WiFi setup portal. Updates are over USB
(`tools/deploy.py`).

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
| NeoPixel data | `GPIO5` | 4 pixels; via a `74AHCT1G125` 3.3→5 V shifter on the PCB (often fine direct on a short bench run). Skipped entirely when `leds_enabled` is false |
| Button: mode ▲ / mode ▼ / sound | `GPIO32` / `GPIO33` / `GPIO25` | each pin↔GND, internal pull-up, active-low. PCB header `J_BTN` |
| Piezo buzzer | `GPIO27` | `machine.PWM`; pin↔piezo↔GND. Skipped when `buzzer_enabled` is false |
| OLED (SSD1306 128×64) | `GPIO21` SDA / `GPIO22` SCL | I2C0 @ 400 kHz, addr 0x3C/0x3D. Auto-detected — absent = no display task. VCC off **3V3** |
| Factory-reset | `GPIO0` | the devkit BOOT button, held ~10 s while running |

Power: **5 V, 3 A+** into the rail. Servo, DFPlayer and pixels wire straight to
it; the ESP32 taps it at `VIN`. Realistic combined peak ~2 A (servo stall
~1 A + WiFi bursts ~0.5 A + 4 pixels white ~0.24 A + amp). The carrier board
takes 5 V over USB-C (16-pin receptacle + 5.1 kΩ CC pull-downs = "5 V device").
No 12 V input without adding a buck + bumping the bulk caps to 16–25 V.

## Getting it onto a board

```
pip install mpremote
python3 tools/deploy.py --wipe     # first load
python3 tools/deploy.py            # after an edit
```

`deploy.py` holds the board in **safe mode** for the whole copy (`noboot.txt`
+ reset, so the app isn't running and nothing competes for the flaky USB
link), copies `app/`, `lib/`, `boot.py`, `main.py` **one file at a time,
SHA-256-verifying each on the device and re-copying on mismatch**, then imports
every module. Only when all of that passes does it remove `noboot.txt` and
reboot into the app. A failed or interrupted run leaves the board safely at
the REPL — re-run and it converges (only unverified files are re-sent). It
never writes `/config.json`.

- `--stay` — deploy but leave the board in safe mode at the REPL.
- `--format` — reformat the flash filesystem first (last resort; also wipes
  `/config.json`). Use only if verified copies keep failing on the *same*
  file, which points at a corrupt filesystem rather than a flaky cable.

**Bringing up WiFi / Home Assistant** — hold the Sound button ~5 s (or do a
factory reset). The board reboots into a WiFi AP `R2-D2-XXXX` (WPA2, sticker
password); join it from a phone, the setup page auto-opens, enter WiFi + a
droid name, tick "Connect to Home Assistant" for the MQTT fields, save. It
reboots with `network_enabled = true` and connects. Until then it runs fully
offline on the buttons.

For bench work, skip the portal: create `app/config_baked.py` (gitignored,
copy `config_baked.py.example`) with your WiFi/MQTT — set `network_enabled: true`
in it to auto-connect.

## Architecture

`/boot.py` (adds `/lib` to the path) → `/main.py` (recovery checks, then
`from app.main import run`) → the `app` package. Module graph is a DAG
(MicroPython has no circular imports):

```
hw, core, config, version   leaves
sound  -> core, hw           servo -> core, sound, hw     leds -> core, hw
diag   -> core               ota   -> core, version      fx -> core, hw
oled   -> (machine, lib/ssd1306)      display -> core, oled
control -> core, sound, servo         buttons -> core, hw, control, fx
net    -> core, sound, servo, diag, ota, control
provisioning -> config, oled          main -> everything
```

Shared state lives in `core` (`state`, `servo_state`, `conn`, `link_state`,
`cfg`). Feature modules do `from app import core` and read `core.X` at call
time — never `from app.core import cfg` (it's `None` until `core.init()`).
Every long-lived task runs under `core.supervise()`, which restarts just that
task on a crash and counts restarts (surfaced in diagnostics).

`app/main.py` `main()`: if `/portal.flag` exists → run the setup portal (blocks,
reboots on save). Else `core.init(cfg)`, import modules, and — only when
`cfg["network_enabled"]` — connect WiFi (+ MQTT if `mqtt_enabled`). Then
`ota.confirm()` and gather the task set: `servo`, `led`, `busy_monitor`,
`button`, `reset_button`, `log` always; the MQTT tasks only when networked.

### Recovery hatches (root `/main.py`, self-contained — no `app` import)

| Hatch | Trigger | Effect |
|---|---|---|
| Safe mode | `noboot.txt` present at FS root | print a banner, don't start the app → clean REPL (`tools/deploy.py` uses this) |
| Ctrl-C | any time the app / asyncio loop is running | drops to the REPL (not a boot-time countdown — that was removed as a stray byte on the serial line could trip it) |
| OTA rollback | `/ota.flag` present and boot count > 2 | restore `/bak` (pre-update files), reboot the old build |
| Setup portal | **Mode ▲ + Sound** held ~5 s | write `/portal.flag`, reboot → AP portal. Self-recovers after 5 min idle; a Cancel link exits without saving |
| Factory reset | hold `GPIO0` ~10 s while running | scream, wipe `/config.json`, write `/portal.flag`, reboot → portal |

## Config store — `app/config.py`

`/config.json` at the FS root is the live config, written by the portal.
`load()` **always** returns a normalised dict — no file just means offline
defaults (`network_enabled = false`); `save()` is atomic; `wipe()` clears it.
Key flags: `network_enabled` (gate on all WiFi/MQTT), `mqtt_enabled`,
`leds_enabled` (skip the NeoPixel/RMT path), `buzzer_enabled`. `topic_prefix`
(a slug of the droid name, or `r2d2`) is the single per-droid identity — MQTT
client id, the whole topic tree, every HA `unique_id`, the HA device id.

## Control — buttons (`app/buttons.py` → `app/control.py`)

Three buttons on `GPIO32/33/25`, pin↔GND, internal pull-up, active-low —
`button_task` polls at 50 ms:

- **mode ▲ / mode ▼** — **tap** (<0.5 s) steps `core.state["mode"]` through
  `control.MODE_CYCLE` (the 7 resting modes; `system_crash` is HA-only),
  wrapping. **Press-and-hold** (>0.5 s) = volume ±1 every 0.25 s while held.
- **sound** — tap (<0.8 s) fires a random sound category.
- **Mode ▲ + Sound** held ~5 s — the config-portal chord (deliberate; a lone
  button can't trigger it).

`control.apply_mode / apply_sound / apply_volume / nudge_volume / cycle_mode`
are the single code path — buttons, MQTT and the portal all call them.

**Feedback:** `app/fx.py` drives a piezo on `GPIO27` (`machine.PWM`) for a
volume tick (pitch tracks the level), a mode-change blip, the portal-entry
chirp, and save/error tones. The dome NeoPixels also encode the current mode
(`leds.LED_REACTIONS`).

## Control — Home Assistant *(only when `network_enabled`)*

MQTT auto-discovery publishes on connect. Topics are `<prefix>/…`:

| Entity | Command | State |
|---|---|---|
| Mode (select, 8) | `<prefix>/command` `{"mode": …}` | `<prefix>/mode/state` |
| Sound (select, 7 folders + idle) | `<prefix>/sound/command` `{"sound": …}` | `<prefix>/sound/state` |
| Volume (number 0–30) | `<prefix>/sound/volume/set` | `<prefix>/sound/volume/state` |
| Debug logging (switch) | `<prefix>/debug/set` | `<prefix>/debug/state` |
| Firmware (update) — *only if `ota_url` set* | `<prefix>/ota/set` `install` | `<prefix>/ota` |
| Heartbeat (sensor) | — | `<prefix>/heartbeat` (60 s, `expire_after` 180) |
| Diagnostics (sensors) | — | `<prefix>/diag` JSON + `<prefix>/diag/reset` |
| Mood tuning (numbers + reset button, one HA sub-device per mood) | `<prefix>/tune/<mood>/<knob>/set`, `<prefix>/tune/<mood>/reset` (`reset`) | `<prefix>/tune/<mood>/<knob>` (retained) |

`<prefix>/log` carries `log_always()` output (queued and drained one line per
~20 ms so bursts don't truncate on the socket). Availability is re-asserted
every heartbeat so a stray LWT `offline` self-heals.

MQTT commands land via `control.apply_*` — same path as the buttons.

There is **no always-on web control panel** — it was removed (its accept loop
wedged after WiFi blips and leaked sockets). The only web surface is the
first-run setup portal (`app/provisioning.py`), reached on demand.

### Tuning moods from Home Assistant

A curated set of per-mood knobs — enough to reshape a mood without a reflash,
not the full parameter surface. Each mood is published as its **own HA
sub-device** (`"<droid> <Mood>"`, linked to the droid via `via_device`), so HA
auto-generates a **separate card per mood**. Changes take effect **within one
behaviour tick** (`core.tune_gen` is bumped; `servo_task` / `led_task` rebuild
the current behaviour) and are **persisted** to `config.json`, so they survive a
reboot.

| Knob | Range (stock) | Effect |
|---|---|---|
| Speed | 0–100 (50) | multiplies the mood's cruise speed by `knob/50`, clamped to 15–300 °/s |
| Restlessness | 0–100 (50) | scales `_Wander`/`_Dart` wait times by `50/knob` (higher = twitchier), floored at 200/400 ms |
| LED Bright | 0–200 (100) | multiplies every pixel's brightness for that mood by `knob/100`, clamped to 0–1 |

Which knobs each mood gets depends on its behaviour class: `_Wander` /
`_Dart` moods (standby, awake, excited, alert) get all three; `_Sweep`
(surveillance) and `_Tremble` (system_crash) get Speed + LED Bright; `_Hold`
(sleep, hologram) gets LED Bright only. An **`<mood> Reset`** button drops that
mood's stored knobs (`config.forget("tune_<mood>_")`) — an absent key just means
"use the firmware value", so reset is exact and leaves other moods untouched.
`mosquitto_sub -t '<prefix>/tune/#'` shows the retained current values.

## Servo — `app/servo.py`

One always-running `servo_task` owns the PWM. Each 20 ms tick it asks the
current mode's behaviour for a setpoint and rate-limits `pos` toward it
(`_approach`: trapezoidal velocity, accel- and speed-limited, snaps on the
final tick so it never overshoots). Because `pos`/`vel` can't jump, a mode
change at any instant is a smooth redirect — no task cancellation. A behaviour's
`target()` returns `(setpoint, max_speed)` or `(setpoint, max_speed,
max_accel)` — the 3rd element lets a mode crack harder than the
`SERVO_MAX_ACCEL` default.

Behaviours (`SERVO_BEHAVIORS[mode]`, fresh instance per switch; they never touch
the servo): `_Hold` (sleep, hologram), `_Sweep` (surveillance — edge-to-edge
oscillation), `_Wander` (standby, awake — long idle, then a slow move to a
random spot), `_Dart` (alert ≈ measured 150°/s darts with 1.5–3.5 s pauses;
excited ≈ frantic 260°/s with 0.5–1.4 s pauses and frequent "double-takes" —
same class, different params), `_Tremble` (system_crash jitter). `SERVO_ON_ENTER` fires one-shot side effects
(system_crash → scream); `SERVO_MODE_TIMEOUT` auto-reverts system_crash to the
prior mode after 10 s. No position persistence — starts at 90° every boot.

## LEDs — `app/leds.py`

`PixelChannel` per-pixel state machine: `off` / `solid` / `blink` / `breathe`
/ `twinkle` / `rainbow`. `LED_REACTIONS[mode]` is four
`(pattern, colour, colour2, period, bright)` tuples for
`[holoprojector, logic display, rear circle, light bar]`. `led_task` renders
at ~25 fps, independent of the servo (both just read `core.state["mode"]`).
Starter patterns — tune on real pixels.

## Sound — `app/sound.py`

`SOUND_FOLDERS` maps a category to `(folder, count)` (counts hard-coded); a
trigger plays a random track in that folder. `busy_monitor_task` watches BUSY
to clear `state["sound"]` when playback ends (with a ~2 s fallback for very
short tracks), so the UI can un-light the button.

SD-card layout, how to add clips, and what the DFPlayer serial protocol can and
can't do: **[dfplayer.md](dfplayer.md)**.

## Display — `app/display.py`

Optional SSD1306 128×64 OLED on I2C0 (`GPIO21`/`22`). `app/oled.py` (`machine` +
vendored `lib/ssd1306.py` — no `core` dep, so `provisioning` can use it too)
scans the bus at startup; no panel → `display_task` idles, everything else runs
unchanged. When present it shows device name / current mode / a status line /
volume bar / network line, redrawing on change. The status line shows the
playing sound (`>chat`) or, while the servo is actively moving,
`scanning… / watching… / …` per mode (`servo_task` writes
`core.servo_state["moving"]`). The setup **portal** draws the AP name, password
and URL on the same screen.

## Diagnostics — `app/diag.py`

`snapshot()` → reset cause, uptime, free heap (%), **ESP-IDF internal-RAM free +
low-water** (`idf_free` / `idf_min_free` — a separate pool from the MicroPython
heap; sockets, RMT channels and WiFi buffers come from here), free/total flash,
CPU MHz, RSSI, IP, WiFi SSID + channel, MCU temp, time-synced, task-restart
count, reconnect count, WiFi-association count (`wifi_assoc`). Published to
`<prefix>/diag` every 30 s. Reset cause is a one-shot retained publish — on a
**classic ESP32 a brownout reports as power-on**, so a brownout and a real
power-cycle look identical here; the serial console (`Brownout detector was
triggered`) is the only tell, see `tools/serial-log.py`.

## Updating

**Normal path: USB** — `python3 tools/deploy.py` (see *Getting it onto a
board* above). That's the whole update story for most builds.

### On-device OTA — `app/ota.py`, opt-in

Off unless `core.cfg["ota_url"]` (or `ota_enabled`) is set. `ota.enabled()`
gates the HA Update entity, the `<prefix>/ota/set` subscription, and the boot
auto-check — and OTA needs `network_enabled` anyway.

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
