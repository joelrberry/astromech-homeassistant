# Shared runtime state + primitives. Imported by every feature module.
#
# DAG rule (MicroPython has no circular-import support): feature modules do
#   `from app import core`
# and read `core.state` / `core.cfg` / `core.dbg` at CALL time. Importing the
# dict/function objects with `from app.core import state, dbg` is fine (they
# exist at import and are shared by reference). NEVER `from app.core import
# cfg` — it is None until core.init() runs.

import time
import uasyncio
import urandom

try:
    import ntptime
except ImportError:
    ntptime = None

# ---- small shared constants ----
DEFAULT_MODE = "standby"
DEFAULT_VOLUME = 7  # kept quiet, per the bench-test tuning in droidrefit/boot.py
SOUND_STATE_IDLE = "idle"

# The r2d2/log topic. Plain here; app.net owns the rest of the topic tree and
# may later prefix these per-device.
LOG_TOPIC = b'r2d2/log'

# ---- shared mutable state ----
# state    — the four HA-visible knobs; every control path writes it, tasks read it
# servo_state — live best-estimate of the horn angle, written every servo tick
# conn / link_state — the current MQTTClient (swapped whole on reconnect) + health
state = {"mode": DEFAULT_MODE, "sound": SOUND_STATE_IDLE,
         "volume": DEFAULT_VOLUME, "debug": False}
servo_state = {"angle": 90, "moving": False}
conn = {"client": None}
link_state = {"down": False}

# health counters (app.diag reports them)
task_restarts = {}   # task name -> times supervise() has restarted it
reconnects = 0       # times connection_watchdog ran a full reconnect

# bumped by net.connect_wifi() on every association; surfaced as the
# 'WiFi Associations' diagnostic sensor (a flapping-link tell).
net_generation = 0

cfg = None          # the loaded config dict — set by init()
_time_synced = False


def init(loaded_cfg):
    global cfg
    cfg = loaded_cfg


# ---- randomness ----
def rand_between(lo, hi):
    # 8 bits of randomness only spans 0-255 — plenty for angle-sized ranges
    # (0-179) but silently useless for anything wider. Use rand_ms() for spans
    # that run into the thousands.
    return lo + (urandom.getrandbits(8) % (hi - lo + 1))


def rand_ms(lo, hi):
    # urandom.getrandbits only goes up to 16 bits per call (~65s of ms).
    # Combine two 16-bit reads for a full 32-bit range so multi-minute spans
    # are actually reachable, not silently capped near the low end.
    span = hi - lo
    r = (urandom.getrandbits(16) << 16) | urandom.getrandbits(16)
    return lo + (r % (span + 1))


# ---- time ----
def timestamp():
    # Real wall-clock date once NTP has synced; falls back to a boot-relative
    # tick count before that (or forever, on a build lacking ntptime).
    if _time_synced:
        y, mo, d, h, mi, s, *_ = time.localtime()
        return '%04d-%02d-%02d %02d:%02d:%02dZ' % (y, mo, d, h, mi, s)
    return 'boot+%dms' % time.ticks_ms()


def sync_time():
    # ESP32 has no battery-backed RTC, so without this the clock is meaningless
    # for anything pulled off the device later (logs, heartbeat).
    global _time_synced
    if ntptime is None:
        log_always("[time] ntptime not available on this build, skipping sync")
        return
    for attempt in range(3):
        try:
            ntptime.settime()
            _time_synced = True
            log_always("[time] synced via NTP:", timestamp())
            return
        except Exception as e:
            log_always("[time] NTP sync attempt", attempt + 1, "failed:", e)
            time.sleep_ms(300)


# ---- logging ----
# log_always()/dbg() format + print immediately, then queue the line. log_task
# drains the queue one line per ~20ms with an await between publishes, so a
# burst of adjacent log calls in one scheduler slice can't interleave and
# truncate on umqtt.simple's socket (the "ander: idle 209477ms" bug).
_LOGQ_MAX = 40
_logq = []


def log_always(*args):
    line = '[%s] %s' % (timestamp(), ' '.join(str(a) for a in args))
    print(line)
    _logq.append(line)
    if len(_logq) > _LOGQ_MAX:
        _logq.pop(0)  # drop oldest — console still has it; keep recent for MQTT


def dbg(*args):
    if state["debug"]:
        log_always(*args)


async def log_task():
    while True:
        client = conn["client"]
        if _logq and client is not None:
            line = _logq.pop(0)
            try:
                client.publish(LOG_TOPIC, line.encode())
            except Exception:
                link_state["down"] = True
            await uasyncio.sleep_ms(20)
        else:
            await uasyncio.sleep_ms(50)


# ---- task supervisor ----
async def supervise(name, coro_func, *args):
    # Runs a task and restarts it if it ever raises — without this,
    # uasyncio.gather() aborts ALL sibling tasks the moment any ONE throws.
    # A supervised task is meant to run forever; if it *returns* that's a bug —
    # back off and log it rather than tight-looping the re-spawn.
    while True:
        try:
            await coro_func(*args)
            log_always("[supervisor]", name, "returned unexpectedly — restarting in 2s")
        except Exception as e:
            task_restarts[name] = task_restarts.get(name, 0) + 1
            log_always("[supervisor]", name, "crashed:", e, "- restarting in 2s")
        await uasyncio.sleep_ms(2000)
