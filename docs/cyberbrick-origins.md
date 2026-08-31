# Controlling a CyberBrick Astromech Droid from Home Assistant

> **Historical.** This documents the project's first chapter: replacing the
> firmware on a **CyberBrick ESP32-C3 core board**. That board was retired
> because its onboard regulation couldn't feed servo + 4 NeoPixels + WiFi
> without browning out. The project now runs on a plain ESP32 with a proper
> external supply and a from-scratch modular firmware — see the top-level
> [README](../README.md) and [docs/firmware.md](firmware.md). This writeup is
> kept for the reverse-engineering notes (the `/bbl` driver library, the LED
> engine, the MQTT/HA-discovery approach), most of which carried forward.

---

This is a writeup of how I replaced the stock CyberBrick RC firmware on an astromech droid build with custom MicroPython that connects directly to Home Assistant over WiFi/MQTT — no physical remote control involved. The droid runs nine distinct "reactions" (`standby`, `awake`, `excited`, `surveillance`, `alert`, `sleep`, `system_crash`, `system_crash_extreme`, `hologram`), each with independently animated dome lights and a servo behavior, all switchable from a Home Assistant dashboard.

If you've got a [CyberBrick](https://makerworld.com/en/cyberbrick) core board driving some kind of prop or droid and want smart-home control instead of (or in addition to) the RF remote, this should get you most of the way there.

## Background: what CyberBrick is

CyberBrick is a hobbyist RC platform from MakerWorld/Bambu Lab — a small ESP32-C3-based "core" board that runs MicroPython, paired with interchangeable "shields" (a transmitter shield with joysticks/switches, and a receiver shield with servo/motor/LED outputs). The stock setup: you build a config in CyberBrick's app (mapping joystick/switch channels to LEDs, servos, motors), upload it to the receiver, and drive it with a paired physical transmitter over a proprietary 2.4GHz link.

That's a great system if you want an RC vehicle. It's the wrong shape if you want a stationary display piece that reacts to commands from your smart home setup instead of a joystick.

## The approach

The core insight: the CyberBrick core board is just an ESP32-C3 running plain MicroPython, and it has full, ordinary WiFi and network stack access. The stock RC application (`rc_main.py` → `rc_module` → `BBL_Controller`) is just *one* MicroPython program that happens to run at boot — nothing stops you replacing `boot.py` entirely with your own script that talks to WiFi/MQTT instead of a paired transmitter, while still calling the same low-level hardware driver library (`/bbl` — LEDs, servos, motors, buzzer) that the stock app uses.

This is **not** third-party firmware flashing (which CyberBrick explicitly warns against and which can brick the board) — it's using the officially-supported MicroPython custom-project mechanism, just going further than a simple script.

Architecture:

```
Home Assistant  →  MQTT (Mosquitto)  →  CyberBrick core (custom boot.py/main.py)  →  /bbl drivers  →  LEDs + servo
```

No transmitter, no receiver pairing, no bridge hardware. The board joins your WiFi, subscribes to an MQTT command topic, and drives the hardware directly.

## Project structure

This repo mirrors the device's own root filesystem — copy it straight onto the board's storage as-is:

```
boot.py                   # trivial bootstrap: import main
main.py                   # everything else — WiFi/MQTT, reaction dispatch, LED/servo engines
config/
    __init__.py.example    # checked in — placeholder WiFi/MQTT values
    __init__.py            # gitignored — your real credentials, not tracked
umqtt/
    simple.py             # vendored from micropython-lib (see Acknowledgments)
```

Before flashing, set up your own credentials:

```sh
cp config/__init__.py.example config/__init__.py
# then edit config/__init__.py with your real WiFi SSID/password and MQTT broker/user/password
```

`config/__init__.py` is gitignored on purpose — it never gets committed, so your credentials don't end up in git history or on a public remote. `main.py` pulls them in with `from config import (WIFI_SSID, WIFI_PASSWORD, ...)`.

It has to be `__init__.py`, not e.g. `config.py` inside that folder. MicroPython treats any directory with no `__init__.py` as a valid (empty) "namespace package" — so `import config` would resolve straight to the empty `config/` directory *before* ever looking inside it for a same-named submodule, and `from config import WIFI_SSID` would fail with `ImportError: no module named 'config.WIFI_SSID'`. Putting the values directly in `__init__.py` sidesteps that: it's what actually runs when the package itself is imported, so there's no separate submodule left to be shadowed.

## What I initially considered (and abandoned)

Worth documenting the false starts, since they're informative:

1. **Emulating the transmitter's joystick/switch signals electrically.** The X12 transmitter shield's inputs are simple analog voltages (joysticks, ~0–2.5V range on a 12-bit ADC) and digital grounds (buttons/switches) — in theory you could wire a second microcontroller to fake these signals into an *unmodified* transmitter, which would then talk to an unmodified receiver exactly as if a human were operating it. This is real and would work, but it means building a whole second piece of hardware (a DAC or PWM+filter circuit) just to reimplement something we could do far more directly.

2. **Replacing the transmitter with an ESP-NOW bridge.** CyberBrick's receiver-to-transmitter link uses ESP-NOW (a WiFi-adjacent protocol), and a community project ([rotorman/CyberBrick_ESPNOW](https://github.com/rotorman/CyberBrick_ESPNOW)) has reverse-engineered this protocol to let an RC radio control an unmodified CyberBrick receiver directly. This is a legitimately good approach if you want to keep the receiver's official app/config running unmodified — but it meant reimplementing the stock app's LED effects and motor curves in a from-scratch receiver-side script, since the community project's receiver code is a bare protocol demo, not the full stock feature set.

Once I actually got a REPL connection to the board and found the receiver could run a **fully custom `boot.py`** with direct WiFi/MQTT access, both of the above became unnecessary — there's no need to fake a transmitter at all when the receiver itself can just talk to Home Assistant.

## Step 1: Get a REPL connection to the board

You need physical/USB access to the CyberBrick core board once, to replace `boot.py`.

1. Plug the core board into your computer via USB-C
2. Find the serial port:
   - **macOS**: `ls /dev/tty.*` — look for something like `/dev/tty.usbmodem21101`
   - **Windows**: Device Manager → Ports (COM & LPT)
   - **Linux**: `ls /dev/ttyACM*`
3. Install [Arduino Lab for MicroPython](https://labs.arduino.cc/en/labs/micropython) (free) — gives you both a REPL console and a file browser in one tool
4. Connect to the port you found. You should land at a `>>>` prompt.

**Back up the stock files first**, using Arduino Lab's Files panel (not the REPL) — this is your restore point:
- `boot.py`
- everything under `/app`
- everything under `/bbl`
- `/rc_config`

### A few REPL gotchas worth knowing up front

- **Large pastes into the raw REPL can silently truncate.** Twice during this project, a multi-thousand-character file transfer via the on-device text editor ended up cut off mid-file, with *no error at write time* — only discovered later via `os.stat()` and checking the actual tail of the file against what it should end with. **Use Arduino Lab's Files panel (drag/drop a file from your computer) rather than pasting into the on-device editor** for anything of real size — it's far more reliable.
- **Ctrl-C stops a running loop. Ctrl-B does not** — it only exits "raw REPL" mode back to the interactive prompt; a running async loop keeps going in the background underneath you, causing confusing mixed output from old and new test runs.
- **A soft reset (Ctrl-D) doesn't always fully reset hardware state.** Timers and singleton driver objects can survive a Ctrl-D. When in doubt, do a full power cycle (unplug everything, plug back in) between test iterations.
- If pasting multi-line code directly at the `>>>` prompt gives you `IndentationError`, the REPL's own auto-indent is colliding with your pasted indentation. Use **Ctrl-E** (paste mode), paste, then **Ctrl-D** to run it as-is.

## Step 2: Understand the hardware driver library

Before writing anything, it's worth reading the actual source of the driver modules you'll be calling — CyberBrick's docs don't cover everything, and there are real bugs worth knowing about upfront.

### LEDs (`/bbl/leds.py`)

```python
from leds import LEDController
led2 = LEDController("LED2")   # singleton per channel; LED2 = GPIO20, LED1 = GPIO21
led2.set_led_effect(mod, duration_ms, repeat_count, led_index, rgb)
```

- `mod`: `0` = solid, `1` = blink, `2` = **breathing** (a sine-wave pulse — not exposed anywhere in CyberBrick's own config UI, only usable by calling this directly)
- `led_index`: a 4-bit mask, one bit per physical NeoPixel on that output (`0b1111` = all four)
- **Important limitation**: `LEDController` only holds **one** effect configuration at a time, even though it addresses 4 physical pixels. Calling `set_led_effect()` twice with two different masks doesn't layer — the second call just overwrites the first. If you want different pixels doing genuinely different things simultaneously (which you probably do), you need to bypass `LEDController` and drive the raw `NeoPixel` object yourself (see Step 4).
- `set_led_effect()` only configures the target state — nothing animates until you call `led2.timing_proc()` repeatedly (every ~10ms) in a loop.

### Servos (`/bbl/servos.py`)

```python
from servos import ServosController
servos = ServosController()   # one instance controls all 4 channels
servos.set_angle(servo_idx, angle)                     # immediate jump, 0-180°
servos.set_angle_stepping(servo_idx, angle, step_speed) # gradual move
```

- **Real bug found in this file**: `timing_proc()`'s logic to clear the internal `step_en` flag back to `False` on arrival is nested inside a conditional that becomes false exactly when the servo *has* arrived — so `step_en` never actually clears, and polling it to detect "movement finished" doesn't work as the docstring implies.
- **Second bug**: `set_angle()` uses MicroPython's 10-bit `.duty()` (0-1023), but only spans values 25-127 across the full 0-180° range — about 0.57 duty units per degree, so most single-degree commands don't actually change the physical signal (visible "jumping"/chunkiness).
- **Workaround used**: skip `set_angle()`/`set_angle_stepping()`/`timing_proc()` entirely. Drive the underlying PWM object directly with the 16-bit `.duty_u16()` call instead — about 64x finer resolution:
  ```python
  def angle_to_duty_u16(angle):
      return int(1638 + angle * 36.3556)

  servo_pwm = servos.servos_map[SERVO_IDX - 1]
  servo_pwm.duty_u16(angle_to_duty_u16(angle))
  ```
  Continuous motion (sweeps, wandering, jitter) is then just your own loop calling this repeatedly with your own step size and delay — simpler and fully predictable.
- Also: the docstring for `reset_info()` claims a default rotation speed of 4 rad/sec; the actual code default is `8.05` — irrelevant once you're on the `duty_u16()` path above.

### Buzzer (`/bbl/buzzer.py`)

There's a full RTTTL ringtone-format player (`MusicController`) in here, not just a simple tone generator — genuinely capable of short "chirp" style sequences if you have a piezo speaker wired up. **Watch for a pin conflict**: `BUZZER2` shares GPIO20 with `LED2` — if you're using the dome LED output, only `BUZZER1` (GPIO21) is free.

## Step 3: Confirm your physical LED layout

If your build has multiple addressable LEDs behind one output (like a 4-pixel dome ring), don't guess which bit of `led_index` maps to which physical position — check empirically:

```python
import sys
sys.path.append('/bbl')
from leds import LEDController

led2 = LEDController("LED2")

def show_only(bit_index):
    mask = 1 << bit_index
    led2.set_led_effect(0, 0, 255, mask, 0xFFFFFF)
    led2.timing_proc()

show_only(0)  # then 1, 2, 3 — note what physically lights up each time
```

On my build this mapped to: bit 0 = front holoprojector light, bit 1 = logic-display twin squares, bit 2 = rear circle, bit 3 = light bar.

## Step 4: The multi-zone LED animation engine

Since `LEDController` can't run independent effects per pixel, here's a small standalone engine that drives the raw `NeoPixel` object with per-pixel state. Pattern types in the shipped `main.py`: `off`, `solid`, `blink`, `breathe` (sine pulse), `twinkle` (a randomized color cross-fade, modeled on how real R2-D2 replica "logic displays" behave — they fade between key colors and pause for a random hold at each, rather than blinking on a fixed clock), and `rainbow` (smooth HSV hue rotation with an optional breathing envelope). The core of it:

```python
import sys
sys.path.append('/bbl')
from machine import Pin
from leds import NeoPixel
import time, math, urandom

pin = Pin(20, Pin.OUT)  # your LED output's GPIO
np = NeoPixel(pin, 4, timing=0)


class PixelChannel:
    def __init__(self):
        self.pattern = 'off'
        self.params = {}
        self._from = (0, 0, 0)
        self._to = (0, 0, 0)
        self._start = 0
        self._dur = 1
        self._hold_until = None

    def set(self, pattern, **params):
        self.pattern = pattern
        self.params = params
        if pattern == 'twinkle':
            self._hold_until = None
            self._pick_target(time.ticks_ms())

    def _pick_target(self, now):
        colors = self.params.get('colors', [(0, 40, 90), (255, 255, 255)])
        self._from = self._to
        self._to = colors[urandom.getrandbits(8) % len(colors)]
        self._start = now
        self._dur = self.params.get('fade_ms', 500)

    def color(self, now):
        p, params = self.pattern, self.params
        if p == 'off':
            return (0, 0, 0)
        if p == 'solid':
            return params.get('rgb', (255, 255, 255))
        if p == 'blink':
            period = params.get('period_ms', 500)
            on = (now % period) < (period // 2)
            return params.get('rgb', (255, 255, 255)) if on else (0, 0, 0)
        if p == 'breathe':
            period = params.get('period_ms', 1500)
            phase = (now % period) / period
            b = (1 + math.sin(2 * math.pi * phase - math.pi / 2)) / 2
            r, g, bl = params.get('rgb', (255, 255, 255))
            return (int(r * b), int(g * b), int(bl * b))
        if p == 'twinkle':
            if self._hold_until is not None and now >= self._hold_until:
                self._pick_target(now)
            elapsed = time.ticks_diff(now, self._start)
            if elapsed >= self._dur:
                if self._hold_until is None:
                    hold_max = params.get('hold_ms', 400)
                    self._hold_until = now + (urandom.getrandbits(8) % hold_max) + 50
                return self._to
            frac = elapsed / self._dur
            f, t = self._from, self._to
            return (int(f[0]+(t[0]-f[0])*frac), int(f[1]+(t[1]-f[1])*frac), int(f[2]+(t[2]-f[2])*frac))
        return (0, 0, 0)


channels = [PixelChannel() for _ in range(4)]

def apply_led_reaction(cfg):
    for idx, (pattern, params) in cfg.items():
        channels[idx].set(pattern, **params)

def led_tick():
    now = time.ticks_ms()
    for i, ch in enumerate(channels):
        np[i] = ch.color(now)
    np.write()
```

*(the `rainbow` pattern and its HSV helper are a bit more code — see `main.py` for the complete, current version)*

A "reaction" is then just a dict mapping pixel index → `(pattern, params)`:

```python
FRONT_HOLO, LOGIC_DISPLAY, REAR_CIRCLE, LIGHT_BAR = 0, 1, 2, 3
BLUE, WHITE, ORANGE, RED = (0,40,90), (255,255,255), (255,110,0), (255,0,0)

LED_REACTIONS = {
    "alert": {
        FRONT_HOLO:    ('blink', {'rgb': RED, 'period_ms': 200}),
        LOGIC_DISPLAY: ('twinkle', {'colors': [RED, WHITE], 'fade_ms': 100, 'hold_ms': 80}),
        REAR_CIRCLE:   ('blink', {'rgb': RED, 'period_ms': 200}),
        LIGHT_BAR:     ('blink', {'rgb': RED, 'period_ms': 100}),
    },
    # ...more reactions — see the "Current reactions" table below and main.py
}
```

## Step 5: WiFi + MQTT

The core board's `network` module is ordinary MicroPython WiFi:

```python
import network, time

def connect_wifi(ssid, password):
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(ssid, password)
        for _ in range(30):
            if wlan.isconnected():
                break
            time.sleep(1)
    try:
        wlan.config(pm=wlan.PM_NONE)  # disable WiFi power-save — see note below
    except Exception as e:
        print("could not disable power save:", e)
    print("connected:", wlan.isconnected(), wlan.ifconfig())
```

**One important gotcha**: ESP32 chips enable WiFi power-saving by default once connected, which periodically cycles the radio off between beacon intervals. This can introduce intermittent multi-second (sometimes tens-of-seconds) latency spikes that look like random "stalls" — `wlan.config(pm=wlan.PM_NONE)` disables it. Note this constant isn't available on every MicroPython build; wrap it in a try/except.

**ESP32-C3 is 2.4GHz only.** If your network has separate 2.4/5GHz SSIDs, make sure you're connecting to the 2.4GHz one.

For MQTT, there's no built-in client — this repo vendors [`umqtt.simple`](https://github.com/micropython/micropython-lib/blob/master/micropython/umqtt.simple/umqtt/simple.py) at `umqtt/simple.py` (see [Acknowledgments](#acknowledgments)), which you copy to the board at `/umqtt/simple.py` along with everything else.

Two real bugs worth patching around:

```python
async def mqtt_task():
    while True:
        client = conn["client"]  # see "Reconnecting" below for why this
        if client is not None:   # isn't just a fixed argument
            try:
                client.check_msg()
            except OSError as e:
                # umqtt.simple has a known issue where "no message waiting"
                # is sometimes reported as OSError(-1) instead of returning
                # cleanly — swallow specifically that case. Anything else
                # is a real link failure, and must NOT be swallowed too —
                # see "Reconnecting" below for why that matters.
                if e.args and e.args[0] == -1:
                    pass
                else:
                    print("mqtt error:", e)
                    link_state["down"] = True
        await uasyncio.sleep_ms(100)
```

```python
async def connect_mqtt():
    from umqtt.simple import MQTTClient
    client = MQTTClient(MQTT_CLIENT_ID, MQTT_BROKER, port=1883, user=MQTT_USER, password=MQTT_PASSWORD)
    # umqtt.simple's connect() has NO timeout by default — a bad/unreachable
    # broker can hang FOREVER with zero output. Always pass an explicit one.
    for attempt in range(5):
        try:
            client.connect(timeout=10)
            return client
        except Exception as e:
            print("mqtt connect attempt", attempt, "failed:", e)
            await uasyncio.sleep_ms(2000)
    raise RuntimeError("could not connect to MQTT")
```

Credentials for both of these come from `config/__init__.py` (see [Project structure](#project-structure)) rather than being hardcoded — `main.py` does `from config import (WIFI_SSID, WIFI_PASSWORD, MQTT_BROKER, MQTT_PORT, MQTT_USER, MQTT_PASSWORD, MQTT_CLIENT_ID)`.

The shipped `main.py` also registers a **Last Will and Testament** on connect (`client.set_last_will(...)`) — the broker auto-publishes `r2d2/availability` → `offline` (retained) if the board ever drops off ungracefully (WiFi loss, power loss, crash), paired with a birth message (`online`) once connected. That's what backs the HA availability/heartbeat entities in Step 6.

### Reconnecting

`umqtt.simple` has no reconnect logic of its own, and it's easy to accidentally paper over that: an early version of `mqtt_task` here caught *every* `OSError` from `check_msg()`, not just the harmless `-1` quirk above. Once the underlying socket died — a WiFi blip, a broker restart, anything — it just kept silently calling `check_msg()` on a dead socket forever, never raising, so nothing ever noticed or recovered. In practice this looked like the board falling silent in Home Assistant while everything else (LEDs, servo) kept running fine, since those don't touch MQTT at all.

The fix has two parts. First, `client` is a shared mutable reference (`conn = {"client": ...}`) instead of a fixed argument to each task — a reconnect creates a *new* `MQTTClient`, and every task needs to pick that up on its next loop iteration rather than keep talking to the one it originally started with. Second, a `connection_watchdog` task checks WiFi + a `link_state["down"]` flag (set by any task that hits a real failure) every 15s, and runs a full reconnect — fresh client, re-subscribe, re-publish birth message and discovery — when needed. `connect_wifi()` and `connect_mqtt()` are both `async def` with `await uasyncio.sleep_ms(...)` in their retry loops rather than blocking `time.sleep()`, specifically so a reconnect attempt doesn't freeze the LED/servo animations while it runs — a real risk once reconnecting can happen at any time from a background task, not just once at boot.

One residual limit: the individual connect calls themselves (`wlan.connect()`, `client.connect()`, `ntptime.settime()` below) are still synchronous library calls under the hood, so a reconnect can briefly stall animations for a couple of seconds — far better than the ~60s a fully blocking retry loop would cause, but not zero.

`connection_watchdog`'s status lines (link unhealthy, reconnected, reconnect failed) — along with `connect_wifi()`/`connect_mqtt()`'s own — all publish to `r2d2/log` unconditionally via `log_always()` (see "Real timestamps" below), not gated behind the debug switch, so a reconnect happening is actually visible in Home Assistant. The one unavoidable gap: during the handshake itself there's usually no MQTT client yet to publish through (first boot, and the start of every reconnect), so those specific lines land console-only in practice — the "reconnected" confirmation line is the one guaranteed to make it through, since a working client exists by the time it fires.

### Real timestamps

ESP32-C3 has no battery-backed RTC, so `time.ticks_ms()` — used for all the animation timing elsewhere in this project — just counts milliseconds since the last power-on and resets to 0 every boot. That's fine for animation math, but useless for knowing *when* something happened, which matters once you're pulling logs or heartbeat values off the device later. `main.py` calls `ntptime.settime()` once WiFi connects, and a small `timestamp()` helper returns a real date once that's synced (falling back to `boot+<ms>` before that, or forever on a MicroPython build without `ntptime` — guarded with a try/except at import). `dbg()` and the heartbeat payload both use it.

`ntptime.settime()` makes exactly one attempt with no retry, and `pool.ntp.org` round-robins across many volunteer servers of inconsistent reachability — a single `ETIMEDOUT` doesn't mean NTP is actually blocked. `sync_time()` retries 3x, and `connection_watchdog` (see "Reconnecting" above) keeps retrying it independently every 15s until it succeeds. Its retry/success/failure messages publish to `r2d2/log` unconditionally via a `log_always()` helper, not gated behind the debug switch — `dbg()` is just a thin wrapper adding that gate back for everything else — same reasoning as the heartbeat topic itself being independent of the debug flag: connectivity/time-sync lifecycle events are worth seeing in HA without remembering to flip debug on first.

## Step 6: Home Assistant MQTT Discovery

Rather than hand-configuring entities in `configuration.yaml`, the board announces itself to Home Assistant automatically via [MQTT Discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery) — publish a small JSON payload to a well-known topic, and HA creates the entity on the spot. The shipped firmware publishes three entities this way: a mode **select** (the reaction dropdown), a debug-logging **switch**, and a heartbeat **sensor**.

```python
import ujson

def publish_discovery(client, modes):
    device_info = {
        "identifiers": ["r2d2"],
        "name": "R2D2 Astromech",
        "manufacturer": "CyberBrick (custom firmware)",
    }
    mode_payload = {
        "name": "R2D2 Mode",
        "unique_id": "r2d2_mode_select",
        "options": sorted(modes),
        "state_topic": "r2d2/mode/state",
        "command_topic": "r2d2/command",
        "command_template": '{"mode": "{{ value }}"}',
        "availability_topic": "r2d2/availability",
        "device": device_info,
    }
    client.publish(b"homeassistant/select/r2d2/mode/config",
                    ujson.dumps(mode_payload).encode(), retain=True)
```

*(the debug switch and heartbeat sensor discovery payloads follow the same shape — see `publish_discovery()` in `main.py` for the complete version)*

This gets you a dropdown entity in Home Assistant with your reaction names as options, plus the switch/sensor, with zero manual YAML. Discovery payloads are retained on the broker, so the entities persist across HA restarts even if the board is offline.

A nice bonus: since debug logging goes over MQTT too (`client.publish(b'r2d2/log', line)`, gated by the debug switch), you can watch live debug output from anywhere via HA's MQTT integration → **Listen to a topic** → `r2d2/log`, no serial cable required once it's deployed. The heartbeat sensor (`r2d2/heartbeat`, published every 60s, `expire_after: 180`) gives HA automatic staleness detection independent of debug logging.

## Step 7: Tie it together

The full reaction dispatch loop, split across two files — `boot.py` (trivial, runs on every power-on) and `main.py` (everything else):

```python
# boot.py
import main
```

```python
# main.py (abridged — see the file itself for the complete version)
state = {"mode": "standby", "debug": False}
conn = {"client": None}
_applied_led_mode = None

async def led_task():
    global _applied_led_mode
    while True:
        cfg = LED_REACTIONS.get(state["mode"], LED_REACTIONS[DEFAULT_MODE])
        if state["mode"] != _applied_led_mode:
            apply_led_reaction(cfg)
            _applied_led_mode = state["mode"]
        led_tick()
        await uasyncio.sleep_ms(20)

def on_mqtt_message(topic, msg):
    payload = ujson.loads(msg)
    mode = payload.get("mode")
    if mode in LED_REACTIONS:
        state["mode"] = mode

async def establish_link():
    # shared by boot and connection_watchdog's reconnect path
    await connect_wifi()
    client = await connect_mqtt()
    client.set_callback(on_mqtt_message)
    client.subscribe(b'r2d2/command')
    conn["client"] = client
    publish_discovery(client, LED_REACTIONS.keys())
    return client

async def main():
    await establish_link()
    await uasyncio.gather(
        supervise("led_task", led_task),
        supervise("servo_task", servo_task),
        supervise("mqtt_task", mqtt_task),
        supervise("state_publish_task", state_publish_task),
        supervise("heartbeat_task", heartbeat_task),
        supervise("connection_watchdog", connection_watchdog),
    )

uasyncio.run(main())
```

The real `main.py` wraps each task in a `supervise()` helper — without it, `uasyncio.gather()` aborts *every* sibling task the moment any single one raises an unhandled exception, so a bug in (say) `led_task` would silently kill `heartbeat_task` too. `supervise()` catches per-task and restarts just that task, isolating failures. `connection_watchdog` is the odd one out — see "Reconnecting" in Step 5 for what it does and why the `client` reference is a shared dict rather than a fixed argument.

Copy `boot.py`, `main.py`, `config/__init__.py` (created per [Project structure](#project-structure)), and `umqtt/simple.py` onto the board's root filesystem via Arduino Lab's Files panel (not paste), and it runs standalone on every power-on — no computer required.

## Current reactions

| Mode | Lights | Servo |
|---|---|---|
| `standby` | Synced cycle: dim/off → bright → dim, every ~3-5 min | Synced with lights — glides to a new spot during the "active" phase |
| `awake` | Rainbow front holo + twinkle logic display | Continuous slow "wander" — random glide + pause every 2-5 min |
| `excited` | Fast orange blink everywhere | Fast sweep, full 1-179° range |
| `surveillance` | Solid white front + twinkling logic display + breathing rear | Slow sweep |
| `alert` | All-red blink/twinkle | Fast sweep |
| `sleep` | Everything off except very slow/dim logic-display twinkle | Centered/still |
| `system_crash` | Chaotic multi-color twinkle/strobe | Snap to 145° then tight jitter |
| `system_crash_extreme` | Same lights as `system_crash` | Wide random walk instead of jitter |
| `hologram` | All 4 zones blink red in sync (700ms) — "on a call" indicator | Centered/still |

`standby` is the one mode where lights and servo are driven by a single shared state machine (idle → active → suspending) instead of running independently — see `PROJECT_NOTES.md` for the full rationale.

## Gotchas that cost the most debugging time

Worth calling these out explicitly since they were the least obvious:

1. **USB power ≠ output power.** On this board, the LED/servo output connectors live on a separate "hat" board powered by the battery — not by USB. Testing over USB alone, everything *looked* like it should work (no errors, code ran fine) but nothing physically moved or lit up, because the output hardware simply wasn't powered. If your board has a similar split power design, always do final behavioral testing on battery power, not USB.

2. **Silent file truncation.** As mentioned above — large pastes/transfers can silently cut off mid-file with no error. Symptom: the script does nothing, or crashes deep inside a library with a confusing error (an `IndexError` or unexpected missing keyword argument) that doesn't obviously point at "the file is incomplete." Always verify `os.stat()` size and check the actual tail of a file after transferring it.

3. **Leftover state across REPL test sessions.** Singleton driver objects (`LEDController`, `ServosController`) and hardware timers can survive what looks like a reset, causing wildly inconsistent behavior between test runs. When debugging gets confusing, a full power cycle is often faster than trying to reason about what state might still be alive.

## Where this could go next

- **Sound**: a DFPlayer Mini (UART-controlled MP3 player, real files off microSD, indexed via `play(track_id)`) for real movie-accurate R2-D2 sound clips, on a separate `r2d2/sound` MQTT topic + a second HA select entity so it doesn't disturb the active LED/servo mode. Still waiting on hardware + sound files (movie-accurate clips should come from the astromech.net / R2 Builders Club community, not generated).
- Physical dome mechanical centering — not yet calibrated; the dome isn't perfectly centered at software's 90°.
- A piezo buzzer on the free buzzer output (`BUZZER1`, GPIO21), using the `/bbl/buzzer.py` RTTTL player for short "droid chirp" sounds on mode changes.
- One-shot animations (a "greeting" sequence) separate from the persistent modes.

## Acknowledgments

- [`umqtt.simple`](https://github.com/micropython/micropython-lib/blob/master/micropython/umqtt.simple/umqtt/simple.py) from [micropython-lib](https://github.com/micropython/micropython-lib) (MIT licensed) — vendored unmodified in this repo at `umqtt/simple.py`, since MicroPython has no MQTT client built in.
- CyberBrick's own `/bbl` hardware driver library (`leds.py`, `servos.py`) — not vendored here (it ships with the board), but this project calls into it directly. See [CyberBrick_Controller_Core](https://github.com/CyberBrick-Official/CyberBrick_Controller_Core).

---

*Built through an extended live debugging session — happy to answer questions if you're attempting something similar on your own CyberBrick build.*
