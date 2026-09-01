# I2C OLED (SSD1306 128x64) — the panel handle, shared by app.display (running
# status) and app.provisioning (portal shows the AP details). Leaf: machine +
# lib/ssd1306 only, so provisioning can import it without pulling in core.
#
# Wiring: SDA -> GPIO21, SCL -> GPIO22, VCC -> 3V3, GND -> GND. Auto-detected;
# no panel on the bus -> open() returns None and callers no-op.

from machine import Pin, I2C

SDA_PIN = 21
SCL_PIN = 22
_ADDRS = (0x3C, 0x3D)


def open():
    """An SSD1306_I2C if a panel answers, else None. Never raises, never slow.

    No i2c.scan() — on a bus with weak/absent pull-ups every probe times out
    (~170 ms) and scan() blocks the event loop for ~19 s. Instead just try to
    init the panel at each known address; a missing panel fails on the first
    write (one bounded transaction), a present one comes up immediately.
    """
    try:
        try:
            i2c = I2C(0, sda=Pin(SDA_PIN), scl=Pin(SCL_PIN), freq=400000,
                      timeout=50000)          # 50 ms/transaction cap where supported
        except TypeError:
            i2c = I2C(0, sda=Pin(SDA_PIN), scl=Pin(SCL_PIN), freq=400000)
    except Exception:
        return None
    for addr in _ADDRS:
        try:
            from ssd1306 import SSD1306_I2C
            d = SSD1306_I2C(128, 64, i2c, addr=addr)
            d.fill(0)
            d.show()
            return d
        except Exception:
            continue
    return None
