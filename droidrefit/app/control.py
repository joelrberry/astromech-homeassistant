# The single control path — every input surface (MQTT, the setup portal, the
# front-panel buttons) funnels mode / sound / volume changes through here.
# Just sets core.state[...] and fires the side effect; servo_task and led_task
# react to state, sound plays immediately. No network here.
#
# deps: app.core, app.sound, app.servo

from app import core, sound, servo

# Order the mode ▲/▼ buttons step through. system_crash is a transient reaction
# (auto-reverts), not a resting mode — reachable from Home Assistant, not the
# button cycle.
MODE_CYCLE = ("standby", "awake", "excited", "surveillance",
              "alert", "hologram", "sleep")


def apply_mode(mode):
    if mode not in servo.SERVO_BEHAVIORS:
        return False
    if mode != core.state["mode"]:
        core.log_always("[mode]", mode)
    core.state["mode"] = mode
    return True


def cycle_mode(step):
    """Step the current mode +1 / -1 through MODE_CYCLE, wrapping."""
    cur = core.state["mode"]
    try:
        i = MODE_CYCLE.index(cur)
    except ValueError:
        i = 0                       # currently in a mode outside the cycle (e.g. system_crash)
        step = 0
    nxt = MODE_CYCLE[(i + step) % len(MODE_CYCLE)]
    return apply_mode(nxt)


def apply_sound(name):
    if name not in sound.SOUND_FOLDERS:
        return False
    core.log_always("[sound]", name)
    core.state["sound"] = name
    sound.play_random_in_folder(name)
    return True


def play_any_sound():
    """A random category — what the front-panel Sound button fires."""
    cats = list(sound.SOUND_FOLDERS.keys())
    return apply_sound(cats[core.rand_between(0, len(cats) - 1)])


def apply_volume(level):
    try:
        level = int(level)
    except (ValueError, TypeError):
        return False
    level = max(0, min(30, level))
    if level != core.state["volume"]:
        core.log_always("[volume]", level)
    core.state["volume"] = level
    sound.player.set_volume(level)
    return True


def nudge_volume(step):
    """Bump volume by step, clamped 0-30. Returns the new level."""
    new = max(0, min(30, core.state["volume"] + step))
    apply_volume(new)
    return new
