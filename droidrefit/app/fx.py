# Piezo buzzer — UI feedback tones (the DFPlayer only plays SD tracks, it can't
# beep). One PWM output on hw.BUZZER_PIN, built lazily like servo.py's PWM.
# Cues are fire-and-forget: callers do fx.play("mode"); a cue already running
# is left to finish and the new one is dropped.
#
# deps: app.core, app.hw

import machine
import uasyncio

from app import core, hw

_pwm = None
_ok = True          # set False if PWM init ever fails
_busy = False


def _enabled():
    try:
        return _ok and core.cfg.get("buzzer_enabled", True)
    except Exception:
        return False


def _get_pwm():
    global _pwm, _ok
    if _pwm is None:
        try:
            _pwm = machine.PWM(machine.Pin(hw.BUZZER_PIN), freq=1000, duty_u16=0)
        except Exception as e:
            _ok = False
            core.dbg("[fx] buzzer init failed:", e)
    return _pwm


def _pitch(level):
    # volume 0..30 -> ~600..2000 Hz, so a held ramp is audibly rising/falling
    level = max(0, min(30, level))
    return 600 + (1400 * level) // 30


# name -> list of (freq_hz, ms); freq 0 == rest
_CUES = {
    "mode":         [(1600, 25)],
    "saved":        [(1400, 60), (0, 40), (1900, 90)],
    "error":        [(300, 180)],
    "portal_enter": [(900, 70), (0, 30), (1300, 70), (0, 30), (1800, 110)],
}


async def _seq(notes):
    global _busy
    pwm = _get_pwm()
    if pwm is None:
        _busy = False
        return
    try:
        for freq, ms in notes:
            if freq > 0:
                pwm.freq(int(freq))
                pwm.duty_u16(30000)
            else:
                pwm.duty_u16(0)
            await uasyncio.sleep_ms(int(ms))
    except Exception as e:
        core.dbg("[fx] tone failed:", e)
    finally:
        try:
            pwm.duty_u16(0)
        except Exception:
            pass
        _busy = False


def _notes_for(name, level):
    if name == "tick":
        return [(_pitch(level if level is not None else 15), 30)]
    return _CUES.get(name)


def play(name, level=None):
    """Fire a named cue (or a volume 'tick' at `level`). Non-blocking."""
    global _busy
    if not _enabled() or _busy:
        return
    notes = _notes_for(name, level)
    if not notes:
        return
    _busy = True
    try:
        uasyncio.create_task(_seq(notes))
    except Exception:
        _busy = False        # no running loop (shouldn't happen from a task)


async def cue(name, level=None):
    """Await a cue to completion — for use right before a machine.reset()."""
    if not _enabled():
        return
    notes = _notes_for(name, level)
    if notes:
        await _seq(notes)
