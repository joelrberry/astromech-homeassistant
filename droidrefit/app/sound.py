# DFPlayer sound: the folder library, the player object, and the BUSY-pin
# playback-finished watcher.
#
# SD-card layout, adding clips, and the DFPlayer protocol: docs/dfplayer.md
#
# deps: app.core, app.hw, dfplayer (lib)

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
    # button can't stay stuck on.
    was_busy = False
    idle_polls = 0
    while True:
        busy_now = hw.busy_pin.value() == 0  # active-low
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
