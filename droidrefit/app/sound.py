# DFPlayer sound: the folder library, the player object, and the BUSY-pin
# playback-finished watcher.
#
# SD-card layout, adding clips, and the DFPlayer protocol: docs/dfplayer.md
#
# deps: app.core, app.hw, dfplayer (lib)

import time
import urandom
import uasyncio

from dfplayer import DFPlayer
from app import core, hw

# name -> (folder number, file count). Straight from r2d2audio.md's "Sound
# library / folder structure" table. A category picks a random track within
# its folder each time — fits the "mood bucket" nature of the categories.
SOUND_FOLDERS = {
    "chat": (1, 18),
    "gen": (2, 19),
    "happy": (3, 7),
    "leia": (4, 3),
    "sad": (5, 4),
    "scream": (6, 3),
    "whistle": (7, 3),
}

# HA's MQTT select only publishes when the dropdown's displayed value changes,
# so picking the same category twice never re-sends. Fixed by resetting
# core.state["sound"] to this sentinel once BUSY reports playback finished
# (busy_monitor_task). A real word, never listed in SOUND_FOLDERS, so
# selecting it directly does nothing. Also the boot value.
SOUND_STATE_IDLE = core.SOUND_STATE_IDLE

player = DFPlayer(hw.dfplayer_uart, log=core.dbg)

# Live "is a clip playing" flag — busy_monitor_task is the single BUSY-pin
# reader; mood_sound_task and anything else consult is_playing().
_playing = False


def is_playing():
    return _playing


def play_random_in_folder(name):
    folder, count = SOUND_FOLDERS[name]
    track = (urandom.getrandbits(8) % count) + 1
    core.dbg("[sound] playing", name, "-> folder", folder, "track", track)
    player.play_folder_track(folder, track)


async def busy_monitor_task():
    # Resets core.state["sound"] to idle when a track finishes, so the UI can
    # un-light the button. Primary signal: the BUSY pin's playing->idle edge.
    # Fallback: if BUSY is never seen LOW within ~2s of the command (DFPlayer
    # slow to start, or a track shorter than the poll), clear it anyway so the
    # button can't stay stuck on. Also publishes the BUSY level as `_playing`.
    global _playing
    was_busy = False
    idle_polls = 0
    while True:
        busy_now = hw.busy_pin.value() == 0  # active-low
        _playing = busy_now
        if busy_now:
            idle_polls = 0
        elif core.state["sound"] != SOUND_STATE_IDLE:
            idle_polls += 1
            if was_busy or idle_polls > 13:   # edge, or ~2s with no BUSY
                core.dbg("[busy] playback finished")
                core.state["sound"] = SOUND_STATE_IDLE
                idle_polls = 0
        was_busy = busy_now
        await uasyncio.sleep_ms(150)


# --- per-mood audio (HA `tune_<mood>_audio` select + `tune_<mood>_audio_gap`) ---
# `audio` holds a SOUND_FOLDERS category name (or "off"/absent). On mood entry,
# and every audio_gap seconds after (jittered) while the mood holds, play a
# random clip from that category — unless a clip (this one or a user pick) is
# still going. Never touches core.state["sound"]; runs with or without a broker.
_MOOD_POLL_MS = 250
_MOOD_ENTRY_MS = 300        # let the mode settle before the on-entry clip
_MOOD_POST_FIRE_MS = 2500   # skip opportunities until BUSY has had time to assert


def _mood_audio(mood):
    try:
        return core.cfg.get("tune_%s_audio" % mood)
    except Exception:
        return None


def _mood_gap_ms(mood):
    try:
        g = core.cfg.get("tune_%s_audio_gap" % mood)
    except Exception:
        g = None
    return int(g) * 1000 if g else 0


def play_mood(mood):
    cat = _mood_audio(mood)
    if not cat or cat == "off" or cat not in SOUND_FOLDERS:
        return
    if _playing or core.state["sound"] != SOUND_STATE_IDLE:
        return
    play_random_in_folder(cat)


async def mood_sound_task():
    last_mode = None
    last_sig = None
    last_tune = core.tune_gen
    next_at = None
    suppress_until = time.ticks_ms()
    while True:
        mode = core.state["mode"]
        cat = _mood_audio(mode)
        armed = bool(cat) and cat != "off" and cat in SOUND_FOLDERS
        gap_ms = _mood_gap_ms(mode)
        now = time.ticks_ms()

        changed = mode != last_mode
        if changed or core.tune_gen != last_tune:
            sig = (cat, gap_ms)
            if changed or sig != last_sig:
                last_mode, last_sig = mode, sig
                if not armed:
                    next_at = None
                elif changed or next_at is None:   # entered, or just enabled
                    next_at = time.ticks_add(now, _MOOD_ENTRY_MS)
                elif not gap_ms:                    # was repeating, now entry-only
                    next_at = None
                # else: gap value changed -> let the pending deadline stand
            last_tune = core.tune_gen

        if (next_at is not None
                and time.ticks_diff(now, next_at) >= 0
                and time.ticks_diff(now, suppress_until) >= 0):
            if armed and not _playing and core.state["sound"] == SOUND_STATE_IDLE:
                play_random_in_folder(cat)
                suppress_until = time.ticks_add(now, _MOOD_POST_FIRE_MS)
            if gap_ms:
                next_at = time.ticks_add(
                    now, core.rand_ms(gap_ms * 6 // 10, gap_ms * 14 // 10))
            else:
                next_at = None
        await uasyncio.sleep_ms(_MOOD_POLL_MS)
