# droidrefit application package. Entry point is /main.py -> app.main.run().
# Module graph is a DAG (MicroPython has no circular-import support):
#   hw, core, config, version   are leaves
#   sound -> core, hw       servo -> core, sound, hw    leds -> core, hw
#   diag  -> core           ota   -> core, version        fx  -> core, hw
#   oled  -> (machine, lib/ssd1306)      display -> core, oled
#   control -> core, sound, servo        buttons -> core, hw, control, fx
#   net   -> core, sound, servo, diag, ota, control
#   provisioning -> config, oled         main -> everything
# Read shared state as `core.state` / `core.cfg` at call time — never
# `from app.core import cfg` (it is None until core.init() runs).
