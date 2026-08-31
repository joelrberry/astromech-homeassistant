# On-device firmware update. Pulls app/ + lib/ from GitHub via `mip`, keeping a
# backup so root /main.py can roll back if the new build won't boot cleanly.
# boot.py and root main.py are NOT updated here (USB-flash only) so the
# bootstrap + rollback code is always known-good.
#
# Triggers: HA Update entity, web-panel button, MQTT, boot auto-check (net.py
# and app/main.py wire those in).
#
# deps: app.core, app.version

import gc
import os
import json
import machine
import uasyncio

try:
    import requests
except ImportError:                       # older builds
    import urequests as requests

from app import core, version

_OWNER_REPO = "joelrberry/astromech-homeassistant"
_BRANCH = "main"
_SUBDIR = "droidrefit"

FLAG = "/ota.flag"
BAK = "/bak"

# status: idle | available | updating | error
state = {"installed": version.VERSION, "latest": None, "status": "idle", "msg": ""}

_on_change = None          # net sets this -> called after each state change


def _touch():
    if _on_change:
        try:
            _on_change()
        except Exception:
            pass


def _base_url():
    return (core.cfg.get("ota_url")
            or "https://raw.githubusercontent.com/%s/%s/%s"
               % (_OWNER_REPO, _BRANCH, _SUBDIR))


def _prep_mem(where):
    # TLS to GitHub on a classic ESP32 needs a big contiguous block for the
    # mbedTLS handshake — collect hard first and log how much room we have.
    gc.collect()
    gc.collect()
    try:
        free = gc.mem_free()
        core.log_always("[ota] %s: free heap %d" % (where, free))
        return free
    except Exception:
        return None


def _mip_spec():
    u = core.cfg.get("ota_url")
    return u if u else "github:%s/%s" % (_OWNER_REPO, _SUBDIR)


async def check():
    _prep_mem("check")
    try:
        r = requests.get(_base_url() + "/app/version.py", timeout=10)
        txt = r.text
        r.close()
    except Exception as e:
        state["status"] = "error"
        state["msg"] = "check: %s" % e
        _touch()
        core.log_always("[ota] check failed:", e)
        return
    latest = None
    for line in txt.split("\n"):
        line = line.strip()
        if line.startswith("VERSION"):
            latest = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
    state["latest"] = latest
    if state["status"] != "updating":
        state["status"] = "available" if (latest and latest != version.VERSION) else "idle"
    core.log_always("[ota] installed=%s latest=%s" % (version.VERSION, latest))
    _touch()


def _managed_paths():
    _prep_mem("manifest")
    try:
        r = requests.get(_base_url() + "/package.json", timeout=10)
        pkg = json.loads(r.text)
        r.close()
        return [u[0] for u in pkg.get("urls", [])]
    except Exception:
        return []


def _copy(src, dst):
    with open(src, "rb") as fi:
        with open(dst, "wb") as fo:
            while True:
                b = fi.read(512)
                if not b:
                    break
                fo.write(b)


def _mkparents(path):
    cur = ""
    for part in path.strip("/").split("/")[:-1]:
        cur += "/" + part
        try:
            os.mkdir(cur)
        except OSError:
            pass


def _rmtree(path):
    try:
        for e in os.ilistdir(path):
            child = path + "/" + e[0]
            if e[1] & 0x4000:
                _rmtree(child)
            else:
                os.remove(child)
        os.rmdir(path)
    except OSError:
        pass


async def update():
    if state["status"] == "updating":
        return
    state["status"] = "updating"
    state["msg"] = ""
    _touch()
    core.log_always("[ota] updating -> %s" % state["latest"])

    paths = _managed_paths()
    if not paths:
        state["status"] = "error"
        state["msg"] = "no manifest"
        _touch()
        return
    try:
        _rmtree(BAK)
        os.mkdir(BAK)
        for p in paths:
            try:
                os.stat("/" + p)
            except OSError:
                continue                        # file not present yet — nothing to back up
            _mkparents(BAK + "/" + p)
            _copy("/" + p, BAK + "/" + p)
    except Exception as e:
        state["status"] = "error"
        state["msg"] = "backup: %s" % e
        _touch()
        return

    _prep_mem("install")
    try:
        import mip
        mip.install(_mip_spec(), target="/", version=_BRANCH, mpy=False)
    except Exception as e:
        state["status"] = "error"
        state["msg"] = "download: %s" % e
        _touch()
        core.log_always("[ota] download failed:", e)
        return

    with open(FLAG, "w") as f:
        json.dump({"to": state["latest"], "boots": 0}, f)
    core.log_always("[ota] downloaded, rebooting")
    await uasyncio.sleep_ms(500)
    machine.reset()


def confirm():
    # app/main.py calls this once the new build has proven it can run
    try:
        os.remove(FLAG)
    except OSError:
        return
    _rmtree(BAK)
    core.log_always("[ota] update confirmed:", version.VERSION)
