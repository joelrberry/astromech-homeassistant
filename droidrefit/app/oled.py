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
    """An SSD1306_I2C if a panel answers on the bus, else None. Never raises."""
    try:
        i2c = I2C(0, sda=Pin(SDA_PIN), scl=Pin(SCL_PIN), freq=400000)
        found = i2c.scan()
    except Exception:
        return None
    addr = None
    for a in _ADDRS:
        if a in found:
            addr = a
            break
    if addr is None:
        return None
    try:
        from ssd1306 import SSD1306_I2C
        d = SSD1306_I2C(128, 64, i2c, addr=addr)
        d.fill(0)
        d.show()
        return d
    except Exception:
        return None
