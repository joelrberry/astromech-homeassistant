# droidrefit application package. Entry point is /main.py -> app.main.run().
# Module graph is a DAG (MicroPython has no circular-import support):
#   hw, core, config, version   are leaves
#   sound -> core, hw       servo -> core, sound, hw    leds -> core, hw
#   diag  -> core           ota   -> core, version
#   net   -> core, sound, servo, diag, ota
#   webui -> core, net, servo, sound, diag, ota
#   provisioning -> config, core        main -> everything
# Read shared state as `core.state` / `core.cfg` at call time — never
# `from app.core import cfg` (it is None until core.init() runs).
