# NeoPixel dome lighting — WS2812 pixels on GPIO5 (via the 74AHCT1G125 level
# shifter on the carrier PCB; usually fine straight off 3.3V on a short bench
# run). Pixel roles, photo-verified on the CyberBrick dome. Names below match
# R2 builder convention (HOLO/REAR are the round PSIs — Processor State
# Indicators — not literally the mechanical holoprojector; kept as HOLO/REAR
# in code/HA for continuity with the existing config/entities):
#   0 = front PSI (round)     1 = logic display, front (twin squares)
#   2 = rear PSI (round)      3 = logic display, rear (the long rectangle —
#                                  physically a second logic display, not a
#                                  plain accent bar; still called "BAR"/"light
#                                  bar" in code/HA for continuity)
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
PURPLE = (170, 0, 220)
YELLOW = (255, 255, 0)

# The "standard" PSI/logic-display colour scheme, established on standby and
# meant to become the template other moods migrate to (see LED_REACTIONS):
# front pixels live in the blue family, rear pixels in the green family. Once
# a mode adopts this scheme, mode-to-mode variety comes from *speed* (faster
# transitions) and, occasionally, a per-pixel treatment tweak — not new colours.
_FRONT_LOGIC_PALETTE = [BLUE] * 3 + [WHITE]    # front logic: mostly blue, twinkling white
_REAR_LOGIC_PALETTE = [GREEN] * 3 + [YELLOW]   # rear logic: mostly green, twinkling yellow


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
    # pattern: off | solid | blink | breathe | fade | fadeout | twinkle | rainbow
    def __init__(self):
        self.set("off")

    def set(self, pattern, color=WHITE, color2=OFF, period_ms=1000, bright=1.0):
        self.pattern = pattern
        self.color = color
        self.color2 = color2
        # twinkle's `color` is normally a single RGB tuple (cross-fades against
        # color2, as before) but may instead be a list/tuple of 3+ RGB tuples —
        # a richer multi-colour palette twinkle randomly picks from. Detected by
        # shape: a palette's first element is itself a tuple.
        if isinstance(color[0], (tuple, list)):
            self.palette = color
        else:
            self.palette = (color, color2)
        self.period = period_ms if period_ms > 0 else 1000
        self.bright = bright
        now = time.ticks_ms()
        self.t0 = now
        self._from = self.palette[0]
        self._to = self.palette[0]
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
        if p == "fade":
            # Deterministic, continuous cross-fade between color and color2 —
            # breathe's shape (smooth ease in/out, no randomness), applied to
            # two colours instead of one colour's brightness: full color ->
            # 50/50 blend at the midpoint -> full color2 -> back, repeating
            # every `period_ms`. (twinkle also blends colours, but randomly
            # and can repeat the same target twice in a row; `fade` always
            # alternates cleanly between exactly these two.)
            b = (1 - math.cos(ph * 6.2832)) / 2          # 0 -> 1 -> 0
            c = (int(self.color[0] + (self.color2[0] - self.color[0]) * b),
                 int(self.color[1] + (self.color2[1] - self.color[1]) * b),
                 int(self.color[2] + (self.color2[2] - self.color[2]) * b))
            return _scale(c, self.bright)
        if p == "fadeout":
            # One-shot: starts at full color/bright and decays linearly to OFF
            # over period_ms, then HOLDS at off (does not loop back up like
            # breathe does) — for "was bright, now winding down for good"
            # transitions rather than a continuous pulse.
            elapsed = time.ticks_diff(now, self.t0) / self.period
            if elapsed >= 1.0:
                return OFF
            return _scale(self.color, self.bright * (1.0 - elapsed))
        if p == "rainbow":
            return _scale(_hsv(ph, 1.0, 1.0), self.bright)
        if p == "twinkle":
            if time.ticks_diff(now, self._next) >= 0:
                self._from = self._to
                self._to = self.palette[core.rand_between(0, len(self.palette) - 1)]
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
# order [HOLO, LOGIC, REAR, BAR]. Ported from a backup of the original
# CyberBrick main.py's LED_REACTIONS (2026-09). Its `twinkle` had separate
# fade_ms/hold_ms and, for some modes, 3+ colours rather than 2 — collapsed
# here to period_ms = fade_ms + hold_ms (this engine randomises the interval
# around one period rather than doing an explicit hold-then-fade), and multi
# colour twinkles pass a *list* of colours as `color` (color2 unused, see
# PixelChannel.set). ORANGE -> AMBER, otherwise same names/values as the
# original where they matched this palette. `system_crash_extreme` wasn't
# ported — droidrefit has no such mode (the original's two entries were
# identical anyway).
LED_REACTIONS = {
    "standby": [("breathe", BLUE, OFF, 6000, 0.2)] * 4,   # fallback only — see
                                                           # STANDBY_PHASE_REACTIONS below
    # Fifth mode migrated to the "standby" colour scheme — same fade/twinkle
    # look as standby's idle phase, just a bit brighter (0.35 vs 0.2).
    # Unlike standby, awake is NOT in STANDBY_PHASE_REACTIONS, so this one
    # entry applies no matter what the wander behavior's phase is doing —
    # wandering doesn't change the lights (no "scanning" white solid/fadeout
    # treatment here; lights just stay normal).
    "awake": [("fade", RED, BLUE, 6000, 0.35),                    # front PSI: same scheme, brighter
              ("twinkle", _FRONT_LOGIC_PALETTE, OFF, 200, 0.35),  # front logic: same scheme, brighter
              ("fade", GREEN, YELLOW, 6000, 0.35),                # rear PSI: same scheme, brighter
              ("twinkle", _REAR_LOGIC_PALETTE, OFF, 200, 0.35)],  # rear logic: same scheme, brighter
    # Second mode migrated to the "standby" colour scheme — same as `alert`
    # (front PSI: red-heavy twinkle, not a fade; front logic blue/white;
    # rear PSI green/yellow fade; rear logic green/yellow). Servo/dome
    # movement for excited is untouched — still the frantic 260°/s dart.
    "excited": [("twinkle", [RED] * 3 + [BLUE], OFF, 500, 0.2),   # front PSI: red-heavy twinkle
                ("twinkle", _FRONT_LOGIC_PALETTE, OFF, 150, 0.2), # front logic: same scheme
                ("fade", GREEN, YELLOW, 1500, 0.2),               # rear PSI: same scheme
                ("twinkle", _REAR_LOGIC_PALETTE, OFF, 150, 0.2)], # rear logic: same scheme
    # Sixth mode migrated to the "standby" colour scheme. No servo change:
    # surveillance keeps its `_Sweep`. One tweak: the front PSI is a solid
    # bright white (the "actively watching" indicator) instead of standby's
    # red/blue fade; rear PSI + both logic displays keep standby's look
    # untouched.
    "surveillance": [("solid", WHITE, OFF, 0, 1.0),               # front PSI: bright white
                      ("twinkle", _FRONT_LOGIC_PALETTE, OFF, 200, 0.2),  # front logic: same scheme
                      ("fade", GREEN, YELLOW, 6000, 0.2),                # rear PSI: same scheme
                      ("twinkle", _REAR_LOGIC_PALETTE, OFF, 200, 0.2)],  # rear logic: same scheme
    # First mode migrated to the "standby" colour scheme (see
    # _FRONT_LOGIC_PALETTE above) — same front-blue/rear-green identity, just
    # faster transitions than standby's idle, plus one tweak: the front PSI
    # twinkles red<->blue (favouring red) instead of a smooth fade, so it
    # reads as more alert/urgent than standby's calm cross-fade. Servo/dome
    # movement for alert is untouched.
    "alert": [("twinkle", [RED] * 3 + [BLUE], OFF, 500, 0.2),   # front PSI: red-heavy twinkle
              ("twinkle", _FRONT_LOGIC_PALETTE, OFF, 150, 0.2), # front logic: same scheme, faster
              ("fade", GREEN, YELLOW, 1500, 0.2),               # rear PSI: same scheme, faster
              ("twinkle", _REAR_LOGIC_PALETTE, OFF, 150, 0.2)], # rear logic: same scheme, faster
    # Third mode migrated to the "standby" colour scheme. No servo change:
    # sleep keeps `_Hold(90)`, dome stays parked. Both PSIs off; the two logic
    # displays stay on their standby colours/speeds at a bare glimmer (0.05).
    "sleep": [("off", OFF, OFF, 0, 0),                            # front PSI: off
              ("twinkle", _FRONT_LOGIC_PALETTE, OFF, 200, 0.05),  # front logic: barely glimmering
              ("off", OFF, OFF, 0, 0),                            # rear PSI: off
              ("twinkle", _REAR_LOGIC_PALETTE, OFF, 200, 0.05)],  # rear logic: barely glimmering
    # Deliberately NOT migrated to the "standby" colour scheme — every other
    # mode is a variation on normal operation, but system_crash is meant to
    # look broken. Keeping the original chaotic all-pixel multi-colour
    # twinkle is the point: it should read as wrong, not as a fast/bright
    # version of the PSI/logic-display identity every other mood shares.
    "system_crash": [("twinkle", [RED, AMBER, WHITE, GREEN, PURPLE], OFF, 120, 1.0),
                      ("twinkle", [WHITE, RED, PURPLE, GREEN], OFF, 100, 1.0),
                      ("blink", RED, OFF, 90, 1.0),
                      ("twinkle", [AMBER, RED, WHITE], OFF, 110, 1.0)],
    # Fourth mode migrated to the "standby" colour scheme. No servo change:
    # hologram already holds still (`_Hold(90)`, no wander). One tweak: the
    # front PSI is a full-red blink (the original "on a call" indicator)
    # instead of standby's red/blue fade; rear PSI + both logic displays
    # keep standby's look untouched.
    "hologram": [("blink", RED, OFF, 700, 0.2),                 # front PSI: full red, flashing
                 ("twinkle", _FRONT_LOGIC_PALETTE, OFF, 200, 0.2),  # front logic: same scheme
                 ("fade", GREEN, YELLOW, 6000, 0.2),                # rear PSI: same scheme
                 ("twinkle", _REAR_LOGIC_PALETTE, OFF, 200, 0.2)],  # rear logic: same scheme
}

# standby is special-cased: the original CyberBrick synced the LEDs to the
# servo's own _Wander phase (idle dim -> bright while moving/holding -> a slow
# pulse back down while "suspending") rather than running LEDs independently.
# Every other mode ignores servo state entirely; standby is the one exception,
# same as it was originally. Keyed by servo._Wander's `.phase` (servo.py),
# published each tick as core.servo_state["phase"].
# The two logic displays (index 1 front, index 3 rear/"BAR" — LIGHT_BAR is
# physically a second logic display, not a plain accent) never breathe, in any
# standby phase — each holds a steady brightness while quick-twinkling between
# its base hue and one accent colour (front: blue/white, rear: green/yellow).
# Bias toward the base hue is done with a weighted twinkle palette (it just
# appears more often in the list) rather than new engine code. The two round
# PSIs (index 0 front, index 2 rear) instead cross-fade continuously between a
# pair of colours via the `fade` pattern (front: red/blue, rear: green/yellow).
_STANDBY_IDLE = [("fade", RED, BLUE, 6000, 0.2),                     # HOLO (front PSI): red<->blue cross-fade
                 ("twinkle", _FRONT_LOGIC_PALETTE, OFF, 200, 0.2),   # LOGIC (front): blue, twinkling white
                 ("fade", GREEN, YELLOW, 6000, 0.2),                 # REAR (rear PSI): green<->yellow cross-fade
                 ("twinkle", _REAR_LOGIC_PALETTE, OFF, 200, 0.2)]    # BAR (rear logic display): green, twinkling yellow

# "Scanning" (move/hold) and its wind-down (suspend) touch the front PSI
# ONLY — the front/rear logic displays and the rear PSI keep doing their idle
# thing (_STANDBY_IDLE[1:]) uninterrupted through the whole standby cycle.
# Front PSI: idle red/blue fade -> bright solid white while scanning -> a
# one-shot fade to off as it finishes -> back to the idle fade once "wait"
# resumes (a fresh `_reaction_for` call on the next phase change resets it).
_STANDBY_REST = _STANDBY_IDLE[1:]   # LOGIC, REAR (PSI), BAR — always this

STANDBY_PHASE_REACTIONS = {
    "wait":    _STANDBY_IDLE,                                             # idle: red/blue fade
    "home":    _STANDBY_IDLE,
    "move":    [("solid", WHITE, OFF, 0, 1.0)] + _STANDBY_REST,           # scanning: bright white
    "hold":    [("solid", WHITE, OFF, 0, 1.0)] + _STANDBY_REST,           # scanning: bright white
    "suspend": [("fadeout", WHITE, OFF, 4000, 1.0)] + _STANDBY_REST,      # winding down: fades to off once
}


def _reaction_for(mode):
    # LED_REACTIONS entry for `mode`, with the HA 'tune_<mode>_bright' knob
    # applied (0..200, 100 == as authored). Absent knob -> authored table.
    # standby alone reads the servo's current wander phase (see
    # STANDBY_PHASE_REACTIONS above); every other mode ignores servo state.
    if mode == "standby":
        phase = core.servo_state.get("phase")
        spec = STANDBY_PHASE_REACTIONS.get(phase, STANDBY_PHASE_REACTIONS["wait"])
    else:
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
    last_phase = None
    try:
        while True:
            mode = core.state["mode"]
            # standby's LEDs also follow the servo's wander phase; every other
            # mode's phase read is None (harmless, never triggers a rebuild).
            phase = core.servo_state.get("phase") if mode == "standby" else None
            if mode != last_mode or core.tune_gen != last_tune or phase != last_phase:
                spec = _reaction_for(mode)
                for i in range(_N):
                    pat, c, c2, per, br = spec[i] if i < len(spec) else ("off", OFF, OFF, 0, 0)
                    chans[i].set(pat, c, c2, per, br)
                core.dbg("[leds] mode ->", mode)
                last_mode = mode
                last_tune = core.tune_gen
                last_phase = phase
            now = time.ticks_ms()
            for i in range(_N):
                _np[i] = chans[i].rgb(now)
            _np.write()
            await uasyncio.sleep_ms(_TICK_MS)
    finally:
        _all_off()
