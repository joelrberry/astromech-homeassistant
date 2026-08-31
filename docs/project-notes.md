# CyberBrick R2-D2 + Home Assistant — Project Notes

> **Historical.** The CyberBrick ESP32-C3 core board was retired (its onboard
> regulation browned out under servo + 4 NeoPixels + WiFi). Everything moved
> to a plain ESP32 with an external 5V rail — see the top-level
> [README](../README.md) and [firmware.md](firmware.md). This file is kept for
> *why* the WiFi/MQTT/Home-Assistant scaffolding and the bug fixes below look
> the way they do — that scaffolding carried forward nearly verbatim. The
> CyberBrick-specific parts (`SERVO_REACTIONS`, `LED_REACTIONS`, the `/bbl`
> driver library) did not; `firmware.md` documents what replaced them.

Continuation notes for picking this project up in a new environment (VSCode / Claude Code). This captures the full history: architecture decisions, bugs found and fixed, and current state, so a fresh session has complete context without needing the original chat.

> **2026-08-31 pivot — offline-first.** The droid now boots and runs standalone
> on three front-panel buttons (mode ▲ / mode ▼ / sound); WiFi + Home Assistant
> are an optional layer, off by default (`network_enabled`), reached by holding
> the Sound button ~5 s. The always-on web control panel (`app/webui.py`) was
> **removed** — its `asyncio.start_server` accept loop wedged after WiFi blips
> and leaked sockets from the ESP-IDF internal-RAM pool (`rmt: no mem` flood +
> dead web page). `firmware.md` is authoritative; the MQTT/HA scaffolding
> described below is unchanged and still used when networking is on.

## Goal

Control a CyberBrick-based R2-D2 astromech (stationary display piece) from Home Assistant via MQTT. No physical RC transmitter involved — Home Assistant is the sole controller. Multiple named "reactions" (light + servo behaviors) selectable from an HA dropdown, auto-discovered via MQTT Discovery.

## Architecture

The CyberBrick core board (ESP32-C3, MicroPython) has its stock `boot.py` completely replaced with a custom script that:
- Connects to WiFi + MQTT directly (no RC transmitter/receiver pairing at all)
- Calls the `/bbl` hardware driver library (`leds.py`, `servos.py`) directly, bypassing the stock RC app framework entirely
- Runs a small async task-based reaction dispatcher, driven by MQTT commands from Home Assistant

This is **not** third-party firmware flashing — it's a legitimate MicroPython custom project (CyberBrick's own supported mechanism), just going further than a simple script.

### Why this approach (history)

Two earlier approaches were explored and abandoned:
1. **Emulating transmitter joystick/switch voltages** into an unmodified transmitter shield — would work but requires extra bridge hardware (DAC/PWM circuit) to fake analog signals.
2. **ESP-NOW bridge** (based on rotorman/CyberBrick_ESPNOW) replacing the transmitter, talking to an unmodified receiver — cleaner, but means reimplementing the stock app's LED effects/motor curves from scratch since the community project's receiver code is a bare protocol demo.

Once a fully custom `boot.py` running directly on the receiver (WiFi/MQTT, no transmitter at all) proved to work, both were dropped as unnecessary.

## Hardware facts learned

- **USB power ≠ output power.** The LED/servo output connectors live on a separate "hat" board powered by the battery, not USB. Code can run perfectly over USB with zero errors while nothing physically moves, because the hat board simply isn't powered. **Always verify final LED/servo behavior on battery power**, not USB.
- LED2 (GPIO20) drives a 4-pixel NeoPixel string on the dome. LED1 (GPIO21) is a separate single onboard status LED, not part of the dome.
- Confirmed physical LED pixel mapping (photo-verified): bit 0 = front holoprojector light, bit 1 = logic display (twin squares), bit 2 = rear circle, bit 3 = light bar.
- Dome servo is `servo_idx=1` (GPIO3). Full servo pinout, confirmed via [a Bambu Lab forum post](https://forum.bambulab.com/t/where-is-documentation-on-building-applications-for-cyberbrick/172330/18) that traced it in the `/bbl` `servos.py` source (`servo_idx=1`/GPIO3 matches what this project already verified independently, which is good corroboration for the rest): `servo_idx=2` → GPIO2, `servo_idx=3` → GPIO1, `servo_idx=4` → GPIO0. Channels 2-4 are still unused by this project — the dome is the only servo driven. Leg motors are physically connected but **intentionally not driven** — stationary display piece by design, not a walking robot.
- The physical dome is not perfectly mechanically centered at the software's 90° — worth calibrating/offsetting if precise centering ever matters.

## Real bugs found in CyberBrick's own `/bbl` library (not our code)

- **`servos.py`**: `timing_proc()`'s logic to clear the `step_en` flag on arrival is nested inside a condition that's false exactly when the servo has arrived — so `step_en` never actually clears. `set_angle_stepping()` + polling `step_en` doesn't work as documented. Worked around by driving `set_angle()`/raw PWM directly from our own loop instead.
- **`servos.set_angle()`** uses MicroPython's 10-bit `.duty()` (0-1023) but only spans values 25-127 across the full 0-180° range — ~0.57 duty units per degree, so most single-degree commands don't actually change the physical signal (visible "jumping"/chunkiness). **Fixed by bypassing `set_angle()` and writing `.duty_u16()` (16-bit) directly** to the underlying PWM object: `duty_u16 = int(1638 + angle * 36.3556)`, ~64x finer resolution.
- `reset_info()`'s docstring claims default `radPSec=4`; actual code default is `8.05` — irrelevant once the `set_angle()`/stepping path was abandoned.

## Real bugs found in `umqtt.simple` (third-party MQTT library, not ours)

- `check_msg()` sometimes reports "no message waiting" as `OSError(-1)` instead of returning cleanly — a known library quirk. Swallow specifically that error code, don't treat it as a real failure.
- `connect()` has **no timeout by default** — a bad/unreachable broker hangs forever with zero output. Always call with `timeout=10` and wrap in a retry loop.
- **No reconnect logic of any kind.** Real incident: HA stopped receiving updates from the board (mode/heartbeat/availability all went stale) while the dome kept moving the whole time — the LED/servo tasks don't touch MQTT at all, so that part was actually the architecture working as designed, not a bug. The real cause: `mqtt_task`'s `check_msg()` caught *every* `OSError` unconditionally (not just the harmless `-1` quirk above), so once the underlying socket died — WiFi blip, broker restart, anything — it just kept silently calling `check_msg()` on a dead socket forever, never raising, so `supervise()` never even saw a crash to restart from. `state_publish_task`/`heartbeat_task` held the same now-dead `client` object too, since it was passed in once as a fixed argument at startup. Fixed with: (1) a shared `conn = {"client": ...}` / `link_state = {"down": ...}` pair instead of passing `client` as a fixed arg, so a reconnect is picked up by every task on its next loop iteration automatically; (2) actually re-raising/flagging real errors instead of swallowing them all; (3) a new `connection_watchdog` task that checks WiFi + `link_state` every 15s and runs a full reconnect (fresh `MQTTClient`, re-subscribe, re-publish birth + discovery) when needed. One residual limit: `connect_wifi()`/`connect_mqtt()`/`ntptime.settime()` are still synchronous library calls under the hood, so a reconnect can briefly stall `led_task`/`servo_task` for a couple seconds — far better than the ~60s a fully blocking retry loop would cause, but not zero.

## Software gotchas worth remembering

- **File transfers can silently truncate** with no error at write time — always verify with `os.stat()` size + check the actual tail content matches what's expected after any file transfer of consequence. Arduino Lab's Files-panel drag-and-drop proved more reliable than pasting into the on-device editor for large files.
- **Ctrl-C stops a running async loop; Ctrl-B does not** (it only exits raw-REPL mode). A full power cycle is often needed between test iterations — hardware timers and singleton driver objects can survive a soft reset.
- `LEDController` can only hold **one** effect config at a time, even though it addresses 4 physical pixels — calling `set_led_effect()` twice with different masks overwrites rather than layers. Solved by bypassing it entirely and driving the raw `NeoPixel` object via a custom per-pixel state machine (`PixelChannel` class).
- `urandom.getrandbits(16)` only covers ~65 seconds — silently caps any attempt at multi-minute random intervals. Combine two 16-bit calls for real 32-bit randomness (`rand_ms()` helper).
- `uasyncio.gather()` aborts **all** sibling tasks if any one of them raises an unhandled exception — a bug in one task (e.g. `led_task`) can silently kill unrelated tasks (e.g. `heartbeat_task`). Fixed with a `supervise()` wrapper that catches exceptions per-task and restarts just that task, isolating failures.
- **A directory with no `__init__.py` still imports successfully as an empty "namespace package"** in MicroPython — it doesn't raise. This bit us directly: an initial `config/config.py` layout meant `import config` resolved straight to the empty `config/` directory (found via the root path that `boot.py`/`main.py` already rely on) *before* anything looked inside it for the same-named `config.py` submodule, so `from config import WIFI_SSID` failed with `ImportError: no module named 'config.WIFI_SSID'` — a real device error, not a hypothetical. Fixed by putting the values directly in `config/__init__.py` instead of a separate same-named file inside the package; that's what actually runs when the package itself is imported, so there's nothing left to be shadowed.

## Repo layout

Mirrors the device's root filesystem directly, since that's what gets copied onto the board:

- `boot.py` — trivial bootstrap (`import main`); runs on every power-on
- `main.py` — all actual logic (formerly a single monolithic `boot.py`): WiFi/MQTT, reaction dispatch, LED/servo engines
- `config/__init__.py.example` — checked in, placeholder WiFi/MQTT values
- `config/__init__.py` — gitignored, real credentials; created locally by copying the example file (must be named `__init__.py`, not `config.py` — see "Software gotchas worth remembering" above)
- `umqtt/simple.py` — vendored unmodified from micropython-lib (MIT), not our code

## Current firmware architecture (`main.py`)

- **WiFi**: connects on boot, disables power-save (`wlan.config(pm=wlan.PM_NONE)`) — ESP32's default WiFi power-saving causes intermittent multi-second latency stalls otherwise. ESP32-C3 is 2.4GHz only.
- **MQTT**: `umqtt.simple` (vendored at `/umqtt/simple.py`, not preinstalled), with the timeout/retry fix above. Registers a Last Will and Testament (`r2d2/availability` → `offline`, retained) so Home Assistant can detect an ungraceful disconnect near-instantly, plus a birth message (`online`) on successful connect.
- **Reaction dispatch**: incoming JSON on `r2d2/command` (`{"mode": "..."}`) sets `state["mode"]`; `led_task` and `servo_task` independently read that and apply the matching config from `LED_REACTIONS`/`SERVO_REACTIONS`.
- **LED engine**: custom `PixelChannel` class drives the raw NeoPixel directly, per-pixel independent state machine. Pattern types: `off`, `solid`, `blink`, `breathe` (sine pulse), `twinkle` (random cross-fade between colors, modeled on real R2-D2 logic-display behavior), `rainbow` (smooth HSV hue rotation, optional breathing envelope).
- **Servo engine**: `duty_u16`-based smooth driving. Servo "kinds": `center` (smooth home-to-90), `sweep` (continuous back-and-forth), `jitter` (small random trembling around a fixed point), `random_walk` (bounded random hops across a range), `wander` (slow deliberate glide to a random point + real pause before the next), `standby_cycle` (see below).
- **`standby` mode** is special: a full synced state machine shared between `led_task` and `servo_task` via `standby_state = {"phase": "idle"}` — idle (lights dim/off, still) → active (bright, servo moves) → suspending (dims back down) → repeat every ~3-5 minutes. Every other mode has LED and servo behavior running fully independently; this is the one exception.
- **Debug logging**: runtime-toggleable via an MQTT switch entity (`state["debug"]`, default off), publishes to `r2d2/log` in addition to console when on.
- **Real timestamps**: `sync_time()` calls `ntptime.settime()` once WiFi connects (ESP32-C3 has no battery-backed RTC, so without this the clock is meaningless — `time.ticks_ms()` just counts up from 0 on every power-on). `dbg()`/`heartbeat_task` use a `timestamp()` helper that returns a real `YYYY-MM-DD HH:MM:SSZ` date once synced, falling back to `boot+<ms>` before that (or forever, on a MicroPython build without `ntptime` — guarded with a try/except at import). Added after a debugging session where a stale, unrelated log file (see "Confirm the stock CyberBrick backup files" below) got mistaken for live output purely because nothing had a date on it.
  - **Real-world observed**: `[time] NTP sync failed ... [Errno 116] ETIMEDOUT` on first boot after adding this. `pool.ntp.org` round-robins across many volunteer servers of inconsistent reachability, and `ntptime.settime()` makes exactly one attempt with no retry of its own — a single ETIMEDOUT here doesn't mean NTP is actually blocked on this network. Fixed with two layers: `sync_time()` now retries 3x with a short pause between attempts, and `connection_watchdog` independently keeps calling it every 15s until it succeeds, regardless of WiFi/MQTT health — otherwise a still-unsynced clock would just wait for an unrelated link drop to trigger a retry as a side effect, which could be a very long time on an otherwise-stable connection.
  - **`log_always()`**: `sync_time()`'s retry/success/failure messages, `connect_wifi()`/`connect_mqtt()`'s status lines, and `connection_watchdog`'s unhealthy/reconnected/failed messages all publish to `r2d2/log` unconditionally now, not just to console — `dbg()` is now a thin wrapper around `log_always()` that adds the debug-switch gate back for everything else. Same reasoning as the heartbeat topic being independent of the debug flag: connectivity/reconnect/time-sync lifecycle events are worth seeing in HA without remembering to flip debug on first. Naturally degrades to console-only whenever no MQTT client exists yet to publish through — unavoidable during the WiFi/MQTT handshake itself (first boot, and the start of every reconnect), since that's precisely the moment nothing's connected yet; the watchdog's own "reconnected" line is guaranteed deliverable though, since by then a fresh working client exists.
- **Heartbeat**: publishes to a dedicated `r2d2/heartbeat` topic every 60s, independent of the debug flag — paired with an HA sensor using `expire_after: 180` (3x the interval) for automatic staleness detection. Payload is now a real date via `timestamp()`, not a raw tick count.
- **MQTT Discovery**: four auto-created HA entities — mode select, debug switch, heartbeat sensor — no manual YAML config needed. Discovery payloads are retained (persist across HA restarts even if the board is offline).
- **Task isolation**: every task runs through a `supervise()` wrapper so one task crashing can't take down its siblings via `uasyncio.gather()`.
- **Connection recovery**: `connection_watchdog` task detects a dropped WiFi/MQTT link and performs a full reconnect — see the `umqtt.simple` "No reconnect logic" entry above for why this exists.

## Current reactions (9 total, alphabetically sorted in the HA dropdown)

| Mode | Lights | Servo |
|---|---|---|
| `standby` | Synced cycle: dim/off → bright → dim (see above) | Synced with lights |
| `awake` | Rainbow front holo + twinkle logic display (test mode, same look as old standby) | Continuous slow "wander" — random glide + pause every 2-5 min |
| `excited` | Fast orange blink everywhere | Fast sweep, full 1-179° range |
| `surveillance` | Solid white front + twinkling logic display + breathing rear | Slow sweep |
| `alert` | All-red blink/twinkle | Fast sweep |
| `sleep` | Everything off except very slow/dim logic-display twinkle | Centered/still |
| `system_crash` | Chaotic multi-color twinkle/strobe | Snap to 145° then tight jitter |
| `system_crash_extreme` | Same lights as system_crash | Wide random walk instead of jitter |
| `hologram` | All 4 zones blink red in sync (700ms) — "on a call" indicator | Centered/still |

## Open / next steps

1. **Sound module**: decided on a DFPlayer Mini (UART-controlled MP3 player, real files off microSD, genuinely indexed via `play(track_id)`) for real movie-accurate R2-D2 sound clips. An earlier candidate (micha833/BuildaDroid_sound_generator, an Arduino Nano + PWM-triggered tone synthesizer) was evaluated and rejected — it only produces procedural generative chatter, not selectable real audio files. Still waiting on hardware + sound files to be sourced (movie-accurate clips should come from the astromech.net / R2 Builders Club community, not generated — that's copyrighted film audio). Planned integration: separate `r2d2/sound` MQTT topic (independent from `r2d2/command` so it doesn't disturb the active LED/servo mode) + a second HA select entity.
2. Physical dome mechanical centering — not yet calibrated.
3. Confirm the stock CyberBrick backup files (original `boot.py`, `/app`, `/bbl`, `/rc_config`) are stored somewhere durable off the board. `logging.0.log` at the repo root is very likely part of that original backup — its content (`[MAIN]SLAVE_IDX`, `[CTRL]ANALOG_CH`, `EFFECT:`, `PWRON_RESET`) is the *stock RC app's* own log format, not this project's — `main.py`/`boot.py` never write to a file at all, only to console + `r2d2/log` over MQTT. It got mistaken for live output during a debugging session purely because it has no dates on it (see "Real timestamps" above — this is exactly the problem that motivated adding those). **Still undecided**: keep it as a labeled stock-firmware backup artifact, or remove it now that it's more likely to cause confusion than help.
4. **`.mpy` precompilation** — deferred, not needed right now. Findings from researching it, so this doesn't need to be re-derived later:
   - MicroPython supports `boot.mpy`/`main.mpy` as direct drop-in replacements for `boot.py`/`main.py` at the filesystem root (confirmed in MicroPython's own docs) — our current `boot.py`/`main.py` split needs no restructuring to adopt this later.
   - Anything reached via a normal `import` is compilable the same way — so `main.py`, `config/__init__.py`, and `umqtt/simple.py` all qualify too.
   - If both a `.py` and `.mpy` of the same module exist on the device, the `.py` source wins — you'd upload only the `.mpy`, not both, to actually get the benefit.
   - **Real risk, why this is deferred**: `.mpy` bytecode is version-locked to the MicroPython build that compiled it — a mismatch fails at import time (`incompatible .mpy file`), which would break `main.py` from ever starting since CyberBrick ships a customized MicroPython fork. Compiling requires first checking the on-device version via REPL (`import sys; print(sys.implementation)`) and using a matching `mpy-cross` (available pinned per-release on PyPI, e.g. `pip install mpy-cross==1.24.1`; versions 1.12–1.28.0 confirmed available as of this writing).
   - **Confirmed target values (checked directly on the board)**: MPY version format `6`, sub-version `3` — i.e. mpy version "6.3". Per MicroPython's own compatibility table, version 6.3 corresponds to MicroPython **1.23.0 and later** (until whichever later release next bumps the mpy version — not yet checked). That narrows the candidate `mpy-cross` builds to try first (e.g. `pip install mpy-cross==1.23.0`), but since CyberBrick's firmware is a customized fork, still treat this as a strong starting point to test, not a guaranteed exact match — verify by actually compiling and importing a small test module on the board before trusting it for real files.
   - If revisited: keep `.py` files as the tracked-in-git source of truth, treat `.mpy` as a generated build artifact (e.g. a small script/Makefile producing a gitignored `build/` output), not something hand-maintained.

## Useful references

- Official CyberBrick receiver source + `/bbl` library: github.com/CyberBrick-Official/CyberBrick_Controller_Core
- CyberBrick MicroPython custom project docs: wiki.bambulab.com/en/makerworld/cyberbrick/ai-advanced-programming
- `umqtt.simple` source: github.com/micropython/micropython-lib/tree/master/micropython/umqtt.simple — vendored unmodified in this repo at `umqtt/simple.py`
- Full narrative writeup/tutorial (published): this repo's guide file
