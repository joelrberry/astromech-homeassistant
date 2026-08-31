# /main.py — entry point (must live at the flash root; the runtime runs it
# after boot.py). All real code is the `app` package; third-party libs in /lib.
# This file and boot.py are NOT touched by OTA (app/ + lib/ only), so the
# bootstrap and the rollback below are always known-good.
import sys
import os
import time

if "/lib" not in sys.path:
    sys.path.append("/lib")


# --- OTA rollback -----------------------------------------------------------
# app/ota.py leaves /ota.flag after downloading a new build and backs the old
# files up to /bak. If the new build can't boot cleanly a few times running,
# restore /bak. Plain file ops only — the fresh app/ might be broken.
def _rmtree(p):
    try:
        for e in os.ilistdir(p):
            c = p + "/" + e[0]
            _rmtree(c) if (e[1] & 0x4000) else os.remove(c)
        os.rmdir(p)
    except OSError:
        pass


def _restore(bak, dst):
    try:
        entries = list(os.ilistdir(bak))
    except OSError:
        return
    for name, typ, *_ in entries:
        src = bak + "/" + name
        tgt = dst + "/" + name
        if typ & 0x4000:
            try:
                os.mkdir(tgt)
            except OSError:
                pass
            _restore(src, tgt)
        else:
            with open(src, "rb") as fi:
                with open(tgt, "wb") as fo:
                    while True:
                        b = fi.read(512)
                        if not b:
                            break
                        fo.write(b)


try:
    import json as _json
    import machine as _machine
    with open("/ota.flag") as _f:
        _ota = _json.load(_f)
    _ota["boots"] = _ota.get("boots", 0) + 1
    with open("/ota.flag", "w") as _f:
        _json.dump(_ota, _f)
    if _ota["boots"] > 2:
        print("[ota] new build failed %d boots — rolling back" % _ota["boots"])
        _restore("/bak", "")
        try:
            os.remove("/ota.flag")
        except OSError:
            pass
        _rmtree("/bak")
        _machine.reset()
except OSError:
    pass


# --- recovery hatches, checked before the app starts ---
# 1. /noboot.txt sentinel file: hard skip. Create it from the REPL
#      open("noboot.txt", "w").close()
#    reset -> clean REPL. Remove it
#      os.remove("noboot.txt")
#    and reset to run normally.
# 2. A short Ctrl-C window: hit Ctrl-C during the countdown for the same thing
#    without needing the file.
_skip = False
try:
    os.stat("noboot.txt")
    _skip = True
    print("\n" + "=" * 54)
    print("  SAFE MODE  —  noboot.txt present, app NOT started")
    print("  run normally:  import os; os.remove('noboot.txt')  then reset")
    print("=" * 54 + "\n")
except OSError:
    pass

if not _skip:
    try:
        print("[boot] app starting in 2s  (Ctrl-C for REPL)")
        time.sleep(2)
    except KeyboardInterrupt:
        _skip = True
        print("[boot] interrupted — staying at the REPL")

if not _skip:
    from app.main import run
    run()
