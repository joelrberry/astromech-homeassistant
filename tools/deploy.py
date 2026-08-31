#!/usr/bin/env python3
"""Sync droidrefit/ firmware to a MicroPython board over USB, via mpremote.

    python3 tools/deploy.py [--port PORT] [--wipe] [--run]

    --wipe   remove :app and :lib first (use for a clean / first-time load)
    --run    soft-reset the board when done so it runs the new code
    --port   serial port (default: let mpremote auto-detect)

Pushes app/*.py, lib/**, boot.py, main.py. Never touches the device's
/config.json or /noboot.txt. Ends by importing every app module on the board
so a truncated transfer fails here instead of at the next boot.

Needs:  pip install mpremote
"""

import argparse
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "droidrefit"))

# app modules to import as the post-copy integrity check
_VERIFY = ("app.config", "app.core", "app.hw", "app.version", "app.diag",
           "app.sound", "app.servo", "app.leds", "app.ota", "app.net",
           "app.webui", "app.provisioning")


def _mpremote(port, *args, check=True):
    cmd = ["mpremote"]
    if port:
        cmd += ["connect", port]
    cmd += [str(a) for a in args]
    print("  $ " + " ".join(cmd))
    rc = subprocess.run(cmd).returncode
    if check and rc != 0:
        sys.exit("\n!! mpremote failed (%d): %s" % (rc, " ".join(str(a) for a in args)))
    return rc


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port")
    ap.add_argument("--wipe", action="store_true")
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()

    if subprocess.run(["mpremote", "version"], capture_output=True).returncode != 0:
        sys.exit("mpremote not found on PATH.  pip install mpremote")

    if not os.path.isdir(os.path.join(ROOT, "app")):
        sys.exit("can't find %s/app — run from the repo" % ROOT)

    app_files = sorted(f for f in os.listdir(os.path.join(ROOT, "app"))
                       if f.endswith(".py"))
    # config_baked.py is bench-only credentials; push it if it's here (dev
    # machine), skip it silently if not (a "clean" checkout / prod image).

    p = a.port

    if a.wipe:
        print("wiping :app :lib")
        _mpremote(p, "fs", "rm", "-r", ":app", check=False)
        _mpremote(p, "fs", "rm", "-r", ":lib", check=False)

    print("mkdir tree")
    for d in (":app", ":lib", ":lib/umqtt"):
        _mpremote(p, "fs", "mkdir", d, check=False)   # fine if it exists

    # one chained mpremote call: all copies + the import check
    chain = []

    def add_cp(local, remote):
        if chain:
            chain.append("+")
        chain.extend(["fs", "cp", local, remote])

    for f in app_files:
        add_cp(os.path.join(ROOT, "app", f), ":app/" + f)
    add_cp(os.path.join(ROOT, "lib", "dfplayer.py"), ":lib/dfplayer.py")
    add_cp(os.path.join(ROOT, "lib", "umqtt", "simple.py"), ":lib/umqtt/simple.py")
    add_cp(os.path.join(ROOT, "boot.py"), ":boot.py")
    add_cp(os.path.join(ROOT, "main.py"), ":main.py")

    chain += ["+", "exec", "import %s; print('IMPORT OK')" % ", ".join(_VERIFY)]

    print("copying %d files + verifying" % (len(app_files) + 4))
    _mpremote(p, *chain)

    if a.run:
        print("soft-reset")
        _mpremote(p, "soft-reset", check=False)

    print("\ndone.")


if __name__ == "__main__":
    main()
