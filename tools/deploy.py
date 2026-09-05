#!/usr/bin/env python3
"""Sync droidrefit/ firmware to a MicroPython board over USB, via mpremote.

    python3 tools/deploy.py [--port PORT] [--wipe] [--stay] [--format] [--no-mpy]

The board is held in SAFE MODE for the whole copy (noboot.txt + reset, so the
app isn't running), each file is copied then SHA-256-verified on the device and
re-copied on mismatch, and only once every file verifies and the modules import
does deploy.py remove noboot.txt and reboot into the app. A failed/interrupted
run leaves the board safely at the REPL — just run it again, it converges.

    --wipe     rm :app and :lib first (clean / first-time load)
    --stay     deploy but leave the board in safe mode at the REPL
    --format   reformat the flash filesystem first (LAST RESORT — also wipes
               /config.json; re-provision after). For when verified copies keep
               failing on the same file (corrupt fs, not a flaky link).
    --no-mpy   push plain .py instead of precompiled .mpy (see below) — for
               debugging with real on-device tracebacks, or if mpy-cross isn't
               installed.
    --port     serial port (default: let mpremote auto-detect)
    --run      accepted and ignored (reboot-after is now the default)

Pushes app/*.py, lib/**, boot.py, main.py. Never writes /config.json.
Skips app/config_baked.py (gitignored, bench-only) even if present locally —
it should never follow a dev machine's checkout onto a board.

By default, app/*.py and lib/** are precompiled to .mpy (via mpy-cross) before
pushing — smaller on flash, and skips the on-device parse+compile step at
import time, which matters on the classic ESP32's small heap (compiling from
source needs a transient buffer on top of the bytecode it produces; a .mpy
just loads). boot.py/main.py stay as plain .py (tiny, and readable at the REPL
for rescue). mpy-cross's bytecode format is version-locked to the MicroPython
build it targets — the installed mpy-cross version MUST match the board's
MicroPython version exactly, or every import fails with "incompatible .mpy
file" (a loud, safe failure, not a silent one). For v1.29.0:
pip install mpy-cross==1.29.0.post2 — bump this pin if the firmware is ever
upgraded. Never leaves both x.py and x.mpy for the same module on the device
(stale-extension cleanup on every push) to avoid import-order ambiguity.

Needs:  pip install mpremote mpy-cross==1.29.0.post2
"""

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "droidrefit"))

# boot.py/main.py stay as plain .py on-device even with mpy on — tiny files,
# and readable at the REPL if something goes wrong during the boot dance.
_MPY_EXEMPT = {"boot.py", "main.py"}

# app modules imported on the device as the post-copy integrity check
_VERIFY = ("app.config", "app.core", "app.hw", "app.version", "app.diag",
           "app.sound", "app.servo", "app.leds", "app.ota", "app.fx",
           "app.oled", "app.display", "app.control", "app.net", "app.buttons",
           "app.provisioning")

# device-side: SHA-256 of a file, printed with a prefix so we can pick it out
# of any REPL banner noise. One line, no compound statements — safest for
# `mpremote exec`. In safe mode reading a ~20 KB module into RAM is nothing.
_REMOTE_SHA = ("import hashlib,binascii;"
               "print('SHA:'+binascii.hexlify("
               "hashlib.sha256(open({path!r},'rb').read()).digest()).decode())")

_REMOTE_FORMAT = (
    "import os\n"
    "try:\n"
    " os.umount('/')\n"
    "except Exception:\n"
    " pass\n"
    "import flashbdev\n"
    "os.VfsLfs2.mkfs(flashbdev.bdev)\n"
    "os.mount(os.VfsLfs2(flashbdev.bdev),'/')\n"
    "print('FORMAT OK')"
)


def _mpremote(port, *args, check=True, retries=0, capture=False):
    cmd = ["mpremote"]
    if port:
        cmd += ["connect", port]
    cmd += [str(a) for a in args]
    if not capture:
        print("  $ " + " ".join(cmd))
    attempt = 0
    while True:
        r = subprocess.run(cmd, capture_output=capture,
                           text=True if capture else None)
        if r.returncode == 0:
            return r
        attempt += 1
        if attempt <= retries:
            time.sleep(0.5)
            continue
        if check:
            if capture and r.stderr:
                sys.stderr.write(r.stderr)
            sys.exit("\n!! mpremote failed (%d): %s"
                     % (r.returncode, " ".join(str(a) for a in args)))
        return r


def _sha_local(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _sha_remote(port, rpath):
    """SHA-256 of :rpath on the device, or None if it couldn't be read."""
    devpath = rpath[1:] if rpath.startswith(":") else rpath
    code = _REMOTE_SHA.format(path=devpath)
    for _ in range(3):
        r = _mpremote(port, "exec", code, check=False, capture=True)
        if r.returncode == 0:
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if line.startswith("SHA:"):
                    return line[4:]
        time.sleep(0.4)
    return None


def _put(port, local, rpath, tries=4):
    want = _sha_local(local)
    for n in range(1, tries + 1):
        _mpremote(port, "fs", "cp", local, rpath, check=False)
        got = _sha_remote(port, rpath)
        if got == want:
            print("  ok  %s" % rpath)
            return True
        why = "hash mismatch" if got else "unreadable"
        print("  !!  %s  attempt %d/%d (%s)" % (rpath, n, tries, why))
        time.sleep(0.5)
    return False


def _mpy_cross_ok():
    r = subprocess.run([sys.executable, "-m", "mpy_cross", "--version"],
                       capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip()


def _compile_mpy(src, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    r = subprocess.run([sys.executable, "-m", "mpy_cross", src, "-o", out_path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("\n!! mpy-cross failed on %s:\n%s" % (src, r.stderr))


def _enter_safe_mode(port):
    # A real hardware reset ("reset", DTR/RTS EN-pin toggle), not
    # "soft-reset" (Ctrl-D, MicroPython VM only) — a soft-reset leaves
    # ESP-IDF C-level driver singletons (WiFi's netif/wifi_started state
    # among them) stale, which can surface later as an esp_netif
    # "duplicate key" / "wifi:init nvs: failed" wedge on the next boot that
    # actually brings WiFi up. See droidrefit-firmware-status memory.
    print("safe mode: writing noboot.txt + reboot")
    _mpremote(port, "exec", "open('noboot.txt','w').close()", retries=3)
    _mpremote(port, "reset", check=False)
    time.sleep(1.5)


# Gitignored, bench-only files that must never follow a dev machine's local
# checkout onto a board — config_baked.py carries real WiFi/MQTT credentials
# and skips the setup portal entirely if present, which is never what you want
# on a board being provisioned normally.
_SKIP_FILES = {"config_baked.py"}


def _file_list(mpy_dir, use_mpy):
    """Returns (local, rpath, stale_rpath) triples. `stale_rpath` is the
    counterpart-extension path to rm on the device first (or None) — keeps
    a .py and a .mpy for the same module from ever coexisting there."""
    app_dir = os.path.join(ROOT, "app")
    entries = []
    for f in sorted(x for x in os.listdir(app_dir)
                    if x.endswith(".py") and x not in _SKIP_FILES):
        entries.append((os.path.join(app_dir, f), "app/" + f))
    entries.append((os.path.join(ROOT, "lib", "dfplayer.py"), "lib/dfplayer.py"))
    entries.append((os.path.join(ROOT, "lib", "ssd1306.py"), "lib/ssd1306.py"))
    entries.append((os.path.join(ROOT, "lib", "umqtt", "simple.py"),
                    "lib/umqtt/simple.py"))
    entries.append((os.path.join(ROOT, "boot.py"), "boot.py"))
    entries.append((os.path.join(ROOT, "main.py"), "main.py"))

    triples = []
    for local, rrel in entries:
        # rrel-based, not basename — droidrefit/app/main.py is a real,
        # sizable app module and should compile like the rest of app/; only
        # the two root-level bootstrap files are exempt.
        compile_this = use_mpy and rrel not in _MPY_EXEMPT
        if compile_this:
            mpy_rel = rrel[:-3] + ".mpy"
            out = os.path.join(mpy_dir, mpy_rel)
            _compile_mpy(local, out)
            triples.append((out, ":" + mpy_rel, ":" + rrel))
        else:
            triples.append((local, ":" + rrel, ":" + rrel[:-3] + ".mpy"))
    return triples


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port")
    ap.add_argument("--wipe", action="store_true")
    ap.add_argument("--stay", action="store_true")
    ap.add_argument("--format", dest="do_format", action="store_true")
    ap.add_argument("--no-mpy", dest="no_mpy", action="store_true",
                    help="push plain .py instead of precompiled .mpy")
    ap.add_argument("--run", action="store_true",
                    help="accepted and ignored (reboot-after is the default)")
    a = ap.parse_args()

    if subprocess.run(["mpremote", "version"], capture_output=True).returncode != 0:
        sys.exit("mpremote not found on PATH.  pip install mpremote")
    if not os.path.isdir(os.path.join(ROOT, "app")):
        sys.exit("can't find %s/app — run from the repo" % ROOT)

    use_mpy = not a.no_mpy
    if use_mpy:
        ver = _mpy_cross_ok()
        if not ver:
            sys.exit("mpy-cross not found.  pip install mpy-cross==1.29.0.post2\n"
                     "(must match the board's MicroPython version exactly — "
                     "see the module docstring)  or pass --no-mpy")
        print("mpy-cross: %s" % ver)

    p = a.port

    _enter_safe_mode(p)

    if a.do_format:
        print("\n!! --format will ERASE the flash filesystem, including "
              "/config.json.")
        if input("   type 'format' to proceed: ").strip() != "format":
            sys.exit("aborted")
        r = _mpremote(p, "exec", _REMOTE_FORMAT, check=False, capture=True)
        if r.returncode != 0 or "FORMAT OK" not in (r.stdout or ""):
            sys.exit("\n!! reformat over the REPL failed. Do it from the host "
                     "instead:\n   python3 -m esptool --port %s erase_flash\n"
                     "   then re-flash MicroPython and re-run this script."
                     % (p or "<port>"))
        print("  filesystem reformatted")
        _enter_safe_mode(p)   # board reset during mkfs mount dance — re-assert

    if a.wipe:
        print("wiping :app :lib")
        _mpremote(p, "fs", "rm", "-r", ":app", check=False)
        _mpremote(p, "fs", "rm", "-r", ":lib", check=False)

    print("mkdir tree")
    for d in (":app", ":lib", ":lib/umqtt"):
        _mpremote(p, "fs", "mkdir", d, check=False)

    with tempfile.TemporaryDirectory(prefix="droidrefit-mpy-") as mpy_dir:
        triples = _file_list(mpy_dir, use_mpy)
        print("copying + verifying %d files%s"
             % (len(triples), " (precompiled .mpy)" if use_mpy else ""))
        for _, rpath, stale in triples:
            if stale != rpath:
                _mpremote(p, "fs", "rm", stale, check=False)   # no stray dupe
        failed = [rpath for local, rpath, _ in triples if not _put(p, local, rpath)]

    if failed:
        print("\n!! FAILED to verify: " + ", ".join(failed))
        print("   noboot.txt left in place — the board is at a safe REPL and")
        print("   will NOT run a half-copied app. Re-run this script (it only")
        print("   re-sends files that don't already verify). If the same file")
        print("   keeps failing, see --format / check the USB cable+port.")
        sys.exit(1)

    print("import check")
    _mpremote(p, "exec", "import %s; print('IMPORT OK')" % ", ".join(_VERIFY),
              retries=2)

    if a.stay:
        print("\n--stay: board left in safe mode (noboot.txt present).")
        print("run it:  mpremote fs rm :noboot.txt  &&  mpremote reset")
        return

    print("removing noboot.txt + reboot")
    _mpremote(p, "fs", "rm", ":noboot.txt", retries=3)
    _mpremote(p, "reset", check=False)   # hard reset — see _enter_safe_mode
    print("\ndone.")


if __name__ == "__main__":
    main()
