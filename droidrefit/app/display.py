# OLED status screen. Auto-detects an SSD1306 on the I2C bus (app.oled); if
# there's none, display_task just idles. Renders device name / mode / a status
# line (now-playing, or what the head is doing) / volume bar / network line,
# redrawing only on change.
#
# deps: app.core, app.oled

import time
import uasyncio

try:
    import network
except ImportError:
    network = None

from app import core, oled

_REFRESH_MS = 400
_FORCE_MS = 5000

# what the head is doing while the servo is actively moving, per mode
_VERB = {
    "standby": "scanning",
    "awake": "watching",
    "excited": "darting",
    "surveillance": "scanning",
    "alert": "alert!",
    "system_crash": "!! glitch !!",
    # sleep / hologram hold still — no verb
}

_wlan = None


def _net_line():
    # bare IP (max 15 chars) when up; a word otherwise — "net  192.168.1.234"
    # would run past the 16-char line.
    global _wlan
    if network is None:
        return "no wifi"
    try:
        if _wlan is None:
            _wlan = network.WLAN(network.STA_IF)
        if _wlan.isconnected():
            return _wlan.ifconfig()[0]
        return "wifi offline"
    except Exception:
        return "wifi offline"


def _status_line(mode, sound, moving):
    if sound != core.SOUND_STATE_IDLE:
        return ">" + sound
    if moving:
        v = _VERB.get(mode)
        if v:
            return v + "..."
    return ""


def _render(d, name, mode, vol, status, net_line):
    d.fill(0)
    d.text(name[:16], 0, 0)
    d.hline(0, 11, 128, 1)
    d.text(mode[:16], 0, 18)
    d.text(status[:15], 6, 28)
    d.text("VOL", 0, 40)
    d.rect(34, 39, 92, 9, 1)
    w = (88 * max(0, min(30, vol))) // 30
    if w:
        d.fill_rect(36, 41, w, 5, 1)
    d.text(net_line[:16], 0, 54)
    d.show()


async def display_task():
    d = None
    for _ in range(4):                 # a panel can be slow to wake at power-on
        try:
            d = oled.open()
        except Exception:
            d = None
        if d is not None:
            break
        await uasyncio.sleep_ms(2000)
    if d is None:
        core.log_always("[display] no SSD1306 on I2C %d/%d — display off"
                        % (oled.SDA_PIN, oled.SCL_PIN))
        while True:                     # one line, then quiet forever
            await uasyncio.sleep_ms(3600000)

    core.log_always("[display] SSD1306 up")
    last = None
    last_draw = time.ticks_ms()
    while True:
        try:
            name = core.cfg.get("device_name") or "droidrefit"
            mode = core.state["mode"]
            vol = core.state["volume"]
            status = _status_line(mode, core.state["sound"],
                                  core.servo_state.get("moving"))
            net_line = _net_line()
            cur = (name, mode, vol, status, net_line)
            now = time.ticks_ms()
            if cur != last or time.ticks_diff(now, last_draw) > _FORCE_MS:
                _render(d, name, mode, vol, status, net_line)
                last = cur
                last_draw = now
        except Exception as e:
            core.dbg("[display] loop error:", e)
        await uasyncio.sleep_ms(_REFRESH_MS)
