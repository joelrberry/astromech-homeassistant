# Front-panel buttons: mode up / mode down / sound. All active-low (pin<->GND,
# internal pull-up). 50 ms poll.
#
#   Mode ▲ / ▼ : tap = cycle reaction; press-and-hold (>0.5s) = volume up/down
#                (±1 every 0.25s while held, buzzer tick per step)
#   Sound      : tap = random clip
#   Mode ▲ + Sound held 5 s = reboot into the WiFi setup portal (deliberate
#                two-button chord — hard to trigger by accident)
#
# deps: app.core, app.hw, app.control, app.fx

import time
import machine
import uasyncio

from app import core, hw, control, fx

_POLL_MS = 50
_TAP_MS = 500            # mode press shorter than this = cycle; longer = volume
_VOL_STEP_MS = 250       # volume repeat rate while a mode button is held
_SOUND_TAP_MS = 800      # sound press shorter than this = play a clip
_CHORD_HOLD_MS = 5000    # Mode ▲ + Sound this long = setup portal
PORTAL_FLAG = "/portal.flag"

# Debug toggles — flip locally while bench-testing wiring, leave off in a build.
_DEBUG_PINS = False       # log every raw edge on the 3 button pins
_DEBUG_NO_PORTAL = False  # disable the portal chord (e.g. if a pin reads stuck-low)


def _pressed(pin):
    return pin.value() == 0


async def _enter_portal():
    core.log_always("[buttons] portal chord — rebooting into WiFi setup")
    try:
        with open(PORTAL_FLAG, "w") as f:
            f.write("1")
    except OSError as e:
        core.log_always("[buttons] could not write portal flag:", e)
        return
    try:
        from app import leds
        leds.cue("portal")
    except Exception:
        pass
    try:
        await fx.cue("portal_enter")
    except Exception:
        pass
    await uasyncio.sleep_ms(150)
    machine.reset()


class _ModeBtn:
    """Tap = cycle_mode(step); hold past _TAP_MS = volume ±1 while held."""

    def __init__(self, pin, step):
        self.pin = pin
        self.step = step
        self.since = None
        self.role = None        # None | "pending" | "volume"
        self.vnext = 0

    def poll(self, now, suppressed):
        if suppressed:
            self.since = None
            self.role = None
            return
        if _pressed(self.pin):
            if self.since is None:
                self.since = now
                self.role = "pending"
            elif self.role == "pending" and time.ticks_diff(now, self.since) >= _TAP_MS:
                self.role = "volume"
                fx.play("tick", control.nudge_volume(self.step))
                self.vnext = time.ticks_add(now, _VOL_STEP_MS)
            elif self.role == "volume" and time.ticks_diff(now, self.vnext) >= 0:
                fx.play("tick", control.nudge_volume(self.step))
                self.vnext = time.ticks_add(self.vnext, _VOL_STEP_MS)
        else:
            if self.since is not None and self.role == "pending":
                control.cycle_mode(self.step)
                fx.play("mode")
            self.since = None
            self.role = None


async def button_task():
    up = _ModeBtn(hw.btn_mode_up, +1)
    dn = _ModeBtn(hw.btn_mode_down, -1)
    snd_since = None
    chord_since = None

    _pins = ((hw.btn_mode_up, "mode_up(32)"),
             (hw.btn_mode_down, "mode_dn(33)"),
             (hw.btn_sound, "sound(25)"))
    _last = [p.value() for p, _ in _pins]
    if _DEBUG_PINS:
        core.log_always("[btn] initial:", ["%s=%d" % (n, v)
                        for (_, n), v in zip(_pins, _last)])

    while True:
        now = time.ticks_ms()
        p_up = _pressed(hw.btn_mode_up)
        p_snd = _pressed(hw.btn_sound)

        if _DEBUG_PINS:
            for i, (pin, name) in enumerate(_pins):
                v = pin.value()
                if v != _last[i]:
                    core.log_always("[btn] %s -> %s" %
                                    (name, "RELEASED(1)" if v else "PRESSED(0)"))
                    _last[i] = v

        # --- chord: Mode ▲ + Sound ---
        chord = p_up and p_snd
        if chord:
            if chord_since is None:
                chord_since = now
                snd_since = None            # clear so the release fires no tap
            elif (not _DEBUG_NO_PORTAL
                  and time.ticks_diff(now, chord_since) >= _CHORD_HOLD_MS):
                await _enter_portal()        # does not return
        else:
            chord_since = None

        # --- mode buttons (Mode ▲ suppressed while the chord is engaged) ---
        up.poll(now, suppressed=chord)
        dn.poll(now, suppressed=False)

        # --- sound: tap = clip (no lone-hold action) ---
        if chord:
            snd_since = None
        elif p_snd:
            if snd_since is None:
                snd_since = now
        else:
            if snd_since is not None and time.ticks_diff(now, snd_since) < _SOUND_TAP_MS:
                control.play_any_sound()
            snd_since = None

        await uasyncio.sleep_ms(_POLL_MS)
