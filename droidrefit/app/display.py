# OLED status screen. Auto-detects an SSD1306 on the I2C bus (app.oled); if
# there's none, display_task just idles. Renders device name / mode / volume
# bar / now-playing / network line, redrawing only on change.
#
# deps: app.core, app.oled

import time
import uasyncio

try:
    import network
except ImportError:
    network = None

from app import core, oled

_REFRESH_MS = 500
_FORCE_MS = 5000

_wlan = None


def _net_line():
    global _wlan
    if network is None:
        return "net  n/a"
    try:
        if _wlan is None:
            _wlan = network.WLAN(network.STA_IF)
        if _wlan.isconnected():
            return "net  " + _wlan.ifconfig()[0]
        return "net  offline"
    except Exception:
        return "net  ?"


def _render(d, name, mode, vol, playing_label, net_line):
    d.fill(0)
    d.text(name[:16], 0, 0)
    d.hline(0, 11, 128, 1)
    d.text(mode[:16], 0, 18)
    d.text("VOL", 0, 32)
    d.rect(34, 31, 92, 9, 1)
    w = (88 * max(0, min(30, vol))) // 30
    if w:
        d.fill_rect(36, 33, w, 5, 1)
    if playing_label:
        d.text((">" + playing_label)[:16], 0, 44)
    d.text(net_line[:16], 0, 56)
    d.show()


async def display_task():
    d = oled.open()
    if d is None:
        core.log_always("[display] no SSD1306 on I2C %d/%d — display idle"
                        % (oled.SDA_PIN, oled.SCL_PIN))
        while True:
            await uasyncio.sleep_ms(3600000)

    core.log_always("[display] SSD1306 up")
    last = None
    last_draw = time.ticks_ms()
    while True:
        name = core.cfg.get("device_name") or "droidrefit"
        mode = core.state["mode"]
        vol = core.state["volume"]
        snd = core.state["sound"]
        playing = "" if snd == core.SOUND_STATE_IDLE else snd
        net_line = _net_line()

        cur = (name, mode, vol, playing, net_line)
        now = time.ticks_ms()
        if cur != last or time.ticks_diff(now, last_draw) > _FORCE_MS:
            try:
                _render(d, name, mode, vol, playing, net_line)
            except Exception as e:
                core.dbg("[display] render failed:", e)
            last = cur
            last_draw = now
        await uasyncio.sleep_ms(_REFRESH_MS)
