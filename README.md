# Controlling a CyberBrick Astromech Droid from Home Assistant

This is a writeup of how I replaced the stock CyberBrick RC firmware on an astromech droid build with custom MicroPython that connects directly to Home Assistant over WiFi/MQTT — no physical remote control involved. The droid now runs five distinct "reactions" (standby, excited, surveillance, alert, sleep), each with independently animated dome lights and a servo sweep, all switchable from a Home Assistant dashboard.

If you've got a [CyberBrick](https://makerworld.com/en/cyberbrick) core board driving some kind of prop or droid and want smart-home control instead of (or in addition to) the RF remote, this should get you most of the way there.

## Background: what CyberBrick is

CyberBrick is a hobbyist RC platform from MakerWorld/Bambu Lab — a small ESP32-C3-based "core" board that runs MicroPython, paired with interchangeable "shields" (a transmitter shield with joysticks/switches, and a receiver shield with servo/motor/LED outputs). The stock setup: you build a config in CyberBrick's app (mapping joystick/switch channels to LEDs, servos, motors), upload it to the receiver, and drive it with a paired physical transmitter over a proprietary 2.4GHz link.

That's a great system if you want an RC vehicle. It's the wrong shape if you want a stationary display piece that reacts to commands from your smart home setup instead of a joystick.

## The approach

The core insight: the CyberBrick core board is just an ESP32-C3 running plain MicroPython, and it has full, ordinary WiFi and network stack access. The stock RC application (`rc_main.py` → `rc_module` → `BBL_Controller`) is just *one* MicroPython program that happens to run at boot — nothing stops you replacing `boot.py` entirely with your own script that talks to WiFi/MQTT instead of a paired transmitter, while still calling the same low-level hardware driver library (`/bbl` — LEDs, servos, motors, buzzer) that the stock app uses.

This is **not** third-party firmware flashing (which CyberBrick explicitly warns against and which can brick the board) — it's using the officially-supported MicroPython custom-project mechanism, just going further than a simple script.

Architecture:

```
Home Assistant  →  MQTT (Mosquitto)  →  CyberBrick core (custom boot.py)  →  /bbl drivers  →  LEDs + servo
```

No transmitter, no receiver pairing, no bridge hardware. The board joins your WiFi, subscribes to an MQTT command topic, and drives the hardware directly.

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
- **Workaround used**: skip `set_angle_stepping()`/`timing_proc()` entirely for continuous motion (like a sweep). Just call `set_angle()` repeatedly from your own loop with your own step size and delay — simpler and fully predictable.
- Also: the docstring for `reset_info()` claims a default rotation speed of 4 rad/sec; the actual code default is `8.05` — worth knowing if you're tuning speed via that path (irrelevant if you use the `set_angle()` workaround above).

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

Since `LEDController` can't run independent effects per pixel, here's a small standalone engine that drives the raw `NeoPixel` object with per-pixel state — `off`, `solid`, `blink`, `breathe`, and `twinkle` (a randomized color cross-fade, modeled on how real R2-D2 replica "logic displays" behave — they fade between key colors and pause for a random hold at each, rather than blinking on a fixed clock):

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

A "reaction" is then just a dict mapping pixel index → `(pattern, params)`:

```python
FRONT_HOLO, LOGIC_DISPLAY, REAR_CIRCLE, LIGHT_BAR = 0, 1, 2, 3
BLUE, WHITE, ORANGE, RED = (0,40,90), (255,255,255), (255,110,0), (255,0,0)

LED_REACTIONS = {
    "standby": {
        FRONT_HOLO:    ('breathe', {'rgb': BLUE, 'period_ms': 3000}),
        LOGIC_DISPLAY: ('twinkle', {'colors': [BLUE, WHITE], 'fade_ms': 700, 'hold_ms': 900}),
        REAR_CIRCLE:   ('off', {}),
        LIGHT_BAR:     ('off', {}),
    },
    "alert": {
        FRONT_HOLO:    ('blink', {'rgb': RED, 'period_ms': 200}),
        LOGIC_DISPLAY: ('twinkle', {'colors': [RED, WHITE], 'fade_ms': 100, 'hold_ms': 80}),
        REAR_CIRCLE:   ('blink', {'rgb': RED, 'period_ms': 200}),
        LIGHT_BAR:     ('blink', {'rgb': RED, 'period_ms': 100}),
    },
    # ...more reactions
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

For MQTT, there's no built-in client — vendor [`umqtt.simple`](https://github.com/micropython/micropython-lib/blob/master/micropython/umqtt.simple/umqtt/simple.py) onto the board (save it as `/umqtt/simple.py`).

Two real bugs worth patching around:

```python
async def mqtt_task(client):
    while True:
        try:
            client.check_msg()
        except OSError as e:
            # umqtt.simple has a known issue where "no message waiting"
            # is sometimes reported as OSError(-1) instead of returning
            # cleanly — swallow specifically that case.
            if not (e.args and e.args[0] == -1):
                print("mqtt error:", e)
        await uasyncio.sleep_ms(100)
```

```python
def connect_mqtt(client_id, broker, user, password):
    from umqtt.simple import MQTTClient
    client = MQTTClient(client_id, broker, port=1883, user=user, password=password)
    # umqtt.simple's connect() has NO timeout by default — a bad/unreachable
    # broker can hang FOREVER with zero output. Always pass an explicit one.
    for attempt in range(5):
        try:
            client.connect(timeout=10)
            return client
        except Exception as e:
            print("mqtt connect attempt", attempt, "failed:", e)
            time.sleep(2)
    raise RuntimeError("could not connect to MQTT")
```

## Step 6: Home Assistant MQTT Discovery

Rather than hand-configuring entities in `configuration.yaml`, the board can announce itself to Home Assistant automatically via [MQTT Discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery) — publish a small JSON payload to a well-known topic, and HA creates the entity on the spot.

```python
import ujson

def publish_discovery(client, modes):
    device_info = {
        "identifiers": ["r2d2"],
        "name": "R2D2 Astromech",
        "manufacturer": "CyberBrick (custom firmware)",
    }
    payload = {
        "name": "R2D2 Mode",
        "unique_id": "r2d2_mode_select",
        "options": list(modes),
        "state_topic": "r2d2/mode/state",
        "command_topic": "r2d2/command",
        "command_template": '{"mode": "{{ value }}"}',
        "device": device_info,
    }
    client.publish(b"homeassistant/select/r2d2/mode/config",
                    ujson.dumps(payload).encode(), retain=True)
```

This gets you a dropdown entity in Home Assistant with your reaction names as options, with zero manual YAML. Discovery payloads are retained on the broker, so the entity persists across HA restarts even if the board is offline.

A nice bonus: since debug logging goes over MQTT too (`client.publish(b'r2d2/log', line)`), you can watch live debug output from anywhere via HA's MQTT integration → **Listen to a topic** → `r2d2/log`, no serial cable required once it's deployed.

## Step 7: Tie it together

The full reaction dispatch loop:

```python
state = {"mode": "standby"}
_applied_led_mode = None

async def led_task():
    global _applied_led_mode
    while True:
        if state["mode"] != _applied_led_mode:
            apply_led_reaction(LED_REACTIONS[state["mode"]])
            _applied_led_mode = state["mode"]
        led_tick()
        await uasyncio.sleep_ms(20)

def on_mqtt_message(topic, msg):
    payload = ujson.loads(msg)
    mode = payload.get("mode")
    if mode in LED_REACTIONS:
        state["mode"] = mode

async def main():
    connect_wifi(WIFI_SSID, WIFI_PASSWORD)
    client = connect_mqtt(MQTT_CLIENT_ID, MQTT_BROKER, MQTT_USER, MQTT_PASSWORD)
    client.set_callback(on_mqtt_message)
    client.subscribe(b'r2d2/command')
    publish_discovery(client, LED_REACTIONS.keys())
    await uasyncio.gather(led_task(), servo_task(), mqtt_task(client))

uasyncio.run(main())
```

Save this as `/boot.py` on the board (via the Files panel, not paste), and it runs standalone on every power-on — no computer required.

## Gotchas that cost the most debugging time

Worth calling these out explicitly since they were the least obvious:

1. **USB power ≠ output power.** On this board, the LED/servo output connectors live on a separate "hat" board powered by the battery — not by USB. Testing over USB alone, everything *looked* like it should work (no errors, code ran fine) but nothing physically moved or lit up, because the output hardware simply wasn't powered. If your board has a similar split power design, always do final behavioral testing on battery power, not USB.

2. **Silent file truncation.** As mentioned above — large pastes/transfers can silently cut off mid-file with no error. Symptom: the script does nothing, or crashes deep inside a library with a confusing error (an `IndexError` or unexpected missing keyword argument) that doesn't obviously point at "the file is incomplete." Always verify `os.stat()` size and check the actual tail of a file after transferring it.

3. **Leftover state across REPL test sessions.** Singleton driver objects (`LEDController`, `ServosController`) and hardware timers can survive what looks like a reset, causing wildly inconsistent behavior between test runs. When debugging gets confusing, a full power cycle is often faster than trying to reason about what state might still be alive.

## Where this could go next

- A physical piezo buzzer on the free buzzer output, using the RTTTL player for short "droid chirp" sounds on mode changes
- One-shot animations (a "greeting" sequence) separate from persistent modes
- More reactions now that the dispatch pattern is proven — this scales to as many as you want

---

*Built through an extended live debugging session — happy to answer questions if you're attempting something similar on your own CyberBrick build.*
