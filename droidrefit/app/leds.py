# NeoPixel dome lighting — WS2812 pixels on GPIO5 (via the 74AHCT1G125 level
# shifter on the carrier PCB; usually fine straight off 3.3V on a short bench
# run). Pixel roles, photo-verified on the CyberBrick dome:
#   0 = front holoprojector   1 = logic display (twin squares)
#   2 = rear circle           3 = light bar
#
# One PixelChannel state machine per pixel; led_task swaps the per-mode set of
# channel configs on a mode change and renders every ~40ms. Independent of the
# servo except that it reads the same core.state["mode"].
#
# deps: app.core, app.hw

import math
import time
import machine
import neopixel
import uasyncio

from app import core, hw

_N = hw.NEOPIXEL_COUNT
_TICK_MS = 40                       # ~25 fps
HOLO, LOGIC, REAR, BAR = 0, 1, 2, 3

# Built lazily in led_task so a unit with leds_enabled=False never touches the
# NeoPixel / RMT path at all (each write() cycles an ESP-IDF RMT channel;
# pointless — and a potential internal-RAM drain — with nothing wired).
_np = None

# --- palette (kept dim-ish; brightness scales further per reaction) ---
OFF = (0, 0, 0)
WHITE = (255, 255, 255)
RED = (255, 0, 0)
BLUE = (0, 80, 255)
CYAN = (0, 200, 255)
GREEN = (0, 255, 60)
AMBER = (255, 110, 0)


def _scale(c, b):
    if b >= 1.0:
        return c
    return (int(c[0] * b), int(c[1] * b), int(c[2] * b))


def _hsv(h, s, v):
    # h,s,v in 0..1 -> (r,g,b) 0..255
    i = int(h * 6)
    f = h * 6 - i
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    r, g, b = ((v, t, p), (q, v, p), (p, v, t),
               (p, q, v), (t, p, v), (v, p, q))[i % 6]
    return (int(r * 255), int(g * 255), int(b * 255))


class PixelChannel:
    # pattern: off | solid | blink | breathe | twinkle | rainbow
    def __init__(self):
        self.set("off")

    def set(self, pattern, color=WHITE, color2=OFF, period_ms=1000, bright=1.0):
        self.pattern = pattern
        self.color = color
        self.color2 = color2
        self.period = period_ms if period_ms > 0 else 1000
        self.bright = bright
        now = time.ticks_ms()
        self.t0 = now
        self._from = color
        self._to = color
        self._next = now

    def rgb(self, now):
        p = self.pattern
        if p == "off":
            return OFF
        if p == "solid":
            return _scale(self.color, self.bright)

        ph = (time.ticks_diff(now, self.t0) % self.period) / self.period

        if p == "blink":
            c = self.color if ph < 0.5 else self.color2
            return _scale(c, self.bright)
        if p == "breathe":
            b = (1 - math.cos(ph * 6.2832)) / 2          # 0 -> 1 -> 0
            return _scale(self.color, self.bright * (0.12 + 0.88 * b))
        if p == "rainbow":
            return _scale(_hsv(ph, 1.0, 1.0), self.bright)
        if p == "twinkle":
            if time.ticks_diff(now, self._next) >= 0:
                self._from = self._to
                self._to = self.color if core.rand_between(0, 1) else self.color2
                self.t0 = now
                self._next = time.ticks_add(
                    now, core.rand_ms(self.period // 2, self.period * 2))
            span = time.ticks_diff(self._next, self.t0)
            f = time.ticks_diff(now, self.t0) / span if span > 0 else 1.0
            if f > 1.0:
                f = 1.0
            c = (int(self._from[0] + (self._to[0] - self._from[0]) * f),
                 int(self._from[1] + (self._to[1] - self._from[1]) * f),
                 int(self._from[2] + (self._to[2] - self._from[2]) * f))
            return _scale(c, self.bright)
        return OFF


# mode -> [ (pattern, color, color2, period_ms, bright) ] x NEOPIXEL_COUNT,
# order [HOLO, LOGIC, REAR, BAR]. Starting point — tune on real pixels.
LED_REACTIONS = {
    "standby": [("breathe", BLUE, OFF, 6000, 0.35)] * 4,
    "awake": [("solid", BLUE, OFF, 0, 0.5),
              ("twinkle", CYAN, BLUE, 1400, 0.8),
              ("solid", BLUE, OFF, 0, 0.4),
              ("solid", BLUE, OFF, 0, 0.4)],
    "excited": [("blink", AMBER, BLUE, 220, 1.0)] * 4,
    "surveillance": [("breathe", RED, OFF, 1500, 0.9),
                     ("twinkle", GREEN, CYAN, 900, 0.9),
                     ("solid", BLUE, OFF, 0, 0.4),
                     ("blink", BLUE, OFF, 1100, 0.7)],
    "alert": [("blink", RED, OFF, 170, 1.0)] * 4,
    "sleep": [("off", OFF, OFF, 0, 0),
              ("breathe", BLUE, OFF, 9000, 0.12),
              ("off", OFF, OFF, 0, 0),
              ("off", OFF, OFF, 0, 0)],
    "system_crash": [("blink", RED, AMBER, 80, 1.0)] * 4,
    "hologram": [("breathe", CYAN, OFF, 650, 0.9),
                 ("solid", CYAN, OFF, 0, 0.5),
                 ("solid", CYAN, OFF, 0, 0.5),
                 ("solid", CYAN, OFF, 0, 0.5)],
}


def _reaction_for(mode):
    # LED_REACTIONS entry for `mode`, with the HA 'tune_<mode>_bright' knob
    # applied (0..200, 100 == as authored). Absent knob -> authored table.
    spec = LED_REACTIONS.get(mode, LED_REACTIONS[core.DEFAULT_MODE])
    try:
        k = core.cfg.get("tune_%s_bright" % mode)
    except Exception:
        k = None
    if k is None or k == 100:
        return spec
    f = k / 100.0
    return [(p, c, c2, per, max(0.0, min(1.0, br * f)))
            for (p, c, c2, per, br) in spec]


def _all_off():
    if _np is None:
        return
    for i in range(_N):
        _np[i] = OFF
    _np.write()


_CUES = {"portal": (0, 40, 120), "button": (60, 60, 60)}


def cue(name):
    # Synchronous one-shot flash for feedback from outside led_task (e.g. the
    # button handler before a reboot). No-op if pixels aren't running.
    if _np is None:
        return
    c = _CUES.get(name, (40, 40, 40))
    try:
        for i in range(_N):
            _np[i] = c
        _np.write()
        time.sleep_ms(120)
        _all_off()
    except Exception:
        pass


async def led_task():
    global _np
    if not core.cfg.get("leds_enabled", True):
        core.log_always("[leds] disabled (leds_enabled=false) — NeoPixel path idle")
        while True:
            await uasyncio.sleep_ms(3600000)

    _np = neopixel.NeoPixel(machine.Pin(hw.NEOPIXEL_PIN), _N)
    chans = [PixelChannel() for _ in range(_N)]
    last_mode = None
    last_tune = core.tune_gen
    try:
        while True:
            mode = core.state["mode"]
            if mode != last_mode or core.tune_gen != last_tune:
                spec = _reaction_for(mode)
                for i in range(_N):
                    pat, c, c2, per, br = spec[i] if i < len(spec) else ("off", OFF, OFF, 0, 0)
                    chans[i].set(pat, c, c2, per, br)
                core.dbg("[leds] mode ->", mode)
                last_mode = mode
                last_tune = core.tune_gen
            now = time.ticks_ms()
            for i in range(_N):
                _np[i] = chans[i].rgb(now)
            _np.write()
            await uasyncio.sleep_ms(_TICK_MS)
    finally:
        _all_off()
