# Front-panel buttons: mode up / mode down / sound. All active-low (pin<->GND,
# internal pull-up). Modelled on main.reset_button_task — 50 ms poll, act on the
# press edge. The sound button doubles as the config-portal trigger: hold it
# ~5 s and the board reboots into the WiFi setup portal.
#
# deps: app.core, app.hw, app.control

import time
import machine
import uasyncio

from app import core, hw, control

_POLL_MS = 50
_REPEAT_MS = 400          # mode buttons auto-repeat while held
_SOUND_TAP_MS = 800       # a sound-button press shorter than this = play a clip
_PORTAL_HOLD_MS = 5000    # hold sound this long = reboot into the setup portal
PORTAL_FLAG = "/portal.flag"


def _pressed(pin):
    return pin.value() == 0


def _enter_portal():
    core.log_always("[buttons] portal hold — rebooting into WiFi setup")
    try:
        with open(PORTAL_FLAG, "w") as f:
            f.write("1")
    except OSError as e:
        core.log_always("[buttons] could not write portal flag:", e)
        return
    try:
        from app import leds
        leds.cue("portal")           # best-effort visual ack
    except Exception:
        pass
    time.sleep_ms(300)
    machine.reset()


def _mode_button(pin, step, since, now):
    """Fire cycle_mode on the press edge, then auto-repeat while held.
    Returns the updated 'pressed since' timestamp (None when released)."""
    if not _pressed(pin):
        return None
    if since is None:
        control.cycle_mode(step)
        return now
    if time.ticks_diff(now, since) >= _REPEAT_MS:
        control.cycle_mode(step)
        return time.ticks_add(since, _REPEAT_MS)
    return since


async def button_task():
    up_since = down_since = sound_since = None
    portal_fired = False

    while True:
        now = time.ticks_ms()

        up_since = _mode_button(hw.btn_mode_up, +1, up_since, now)
        down_since = _mode_button(hw.btn_mode_down, -1, down_since, now)

        # --- sound: tap = clip, long hold = config portal ---
        if _pressed(hw.btn_sound):
            if sound_since is None:
                sound_since = now
                portal_fired = False
            elif (not portal_fired
                  and time.ticks_diff(now, sound_since) >= _PORTAL_HOLD_MS):
                portal_fired = True
                _enter_portal()      # does not return (machine.reset)
        else:
            if sound_since is not None and not portal_fired:
                if time.ticks_diff(now, sound_since) < _SOUND_TAP_MS:
                    control.play_any_sound()
            sound_since = None
            portal_fired = False

        await uasyncio.sleep_ms(_POLL_MS)
