# Device telemetry — free heap, filesystem, uptime, reset cause, WiFi RSSI,
# CPU freq, MCU temp. snapshot() returns a plain dict; net publishes it to
# <prefix>/diag as JSON for Home Assistant diagnostic sensors, and webui serves
# it at /diag. Leaf-ish: imports core + stdlib/hw only.
#
# deps: app.core

import gc
import os
import time
import machine

try:
    import esp32
except ImportError:
    esp32 = None
try:
    import network
except ImportError:
    network = None

from app import core

_BOOT_MS = time.ticks_ms()

_RESET_NAMES = {}
for _name, _label in (("PWRON_RESET", "power-on"), ("HARD_RESET", "hard"),
                      ("WDT_RESET", "watchdog"), ("DEEPSLEEP_RESET", "deepsleep"),
                      ("SOFT_RESET", "soft"), ("BROWNOUT_RESET", "brownout")):
    _v = getattr(machine, _name, None)
    if _v is not None:
        _RESET_NAMES[_v] = _label

try:
    _rc = machine.reset_cause()
    RESET_CAUSE = _RESET_NAMES.get(_rc, "code-%s" % _rc)
except Exception:
    RESET_CAUSE = "unknown"
# On classic ESP32 a brownout reset is reported as PWRON — the ROM's
# "Brownout detector was triggered" serial line is the only sure tell.


def _mcu_temp_c():
    if esp32 is None:
        return None
    try:
        return round(esp32.mcu_temperature(), 1)          # S2/S3/C3: already C
    except Exception:
        pass
    try:
        return round((esp32.raw_temperature() - 32) / 1.8, 1)  # classic: F, rough
    except Exception:
        return None


def snapshot():
    gc.collect()
    free = gc.mem_free()
    used = gc.mem_alloc()
    d = {
        "reset_cause": RESET_CAUSE,
        "uptime_s": time.ticks_diff(time.ticks_ms(), _BOOT_MS) // 1000,
        "heap_free": free,
        "heap_used": used,
        "heap_free_pct": round(100 * free / (free + used), 1) if (free + used) else 0,
        "cpu_mhz": machine.freq() // 1000000,
    }
    try:
        st = os.statvfs("/")
        d["fs_free"] = st[0] * st[3]        # f_bsize * f_bfree
        d["fs_total"] = st[0] * st[2]       # f_bsize * f_blocks
    except Exception:
        pass
    d["time_synced"] = bool(core._time_synced)
    d["task_restarts"] = sum(core.task_restarts.values())
    d["reconnects"] = core.reconnects
    if network is not None:
        try:
            w = network.WLAN(network.STA_IF)
            if w.isconnected():
                d["rssi"] = w.status("rssi")
                d["ip"] = w.ifconfig()[0]
                try:
                    d["ssid"] = w.config("ssid")
                    d["channel"] = w.config("channel")
                except Exception:
                    pass
        except Exception:
            pass
    t = _mcu_temp_c()
    if t is not None:
        d["mcu_temp_c"] = t
    return d


def log_boot():
    d = snapshot()
    core.log_always("[boot] reset=%s  heap_free=%d (%s%%)  cpu=%dMHz"
                    % (d["reset_cause"], d["heap_free"], d["heap_free_pct"], d["cpu_mhz"]))
