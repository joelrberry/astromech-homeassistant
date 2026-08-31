#!/usr/bin/env python3
"""Sync droidrefit/ firmware to a MicroPython board over USB, via mpremote.

    python3 tools/deploy.py [--port PORT] [--wipe] [--stay] [--format]

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
    --port     serial port (default: let mpremote auto-detect)
    --run      accepted and ignored (reboot-after is now the default)

Pushes app/*.py, lib/**, boot.py, main.py. Never writes /config.json.

Needs:  pip install mpremote
"""

import argparse
import hashlib
import os
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "droidrefit"))

# app modules imported on the device as the post-copy integrity check
_VERIFY = ("app.config", "app.core", "app.hw", "app.version", "app.diag",
           "app.sound", "app.servo", "app.leds", "app.ota", "app.net",
           "app.webui", "app.provisioning")

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


def _enter_safe_mode(port):
    print("safe mode: writing noboot.txt + reboot")
    _mpremote(port, "exec", "open('noboot.txt','w').close()", retries=3)
    _mpremote(port, "soft-reset", check=False)
    time.sleep(1.5)


def _file_list():
    app_dir = os.path.join(ROOT, "app")
    pairs = []
    for f in sorted(x for x in os.listdir(app_dir) if x.endswith(".py")):
        pairs.append((os.path.join(app_dir, f), ":app/" + f))
    pairs.append((os.path.join(ROOT, "lib", "dfplayer.py"), ":lib/dfplayer.py"))
    pairs.append((os.path.join(ROOT, "lib", "umqtt", "simple.py"),
                  ":lib/umqtt/simple.py"))
    pairs.append((os.path.join(ROOT, "boot.py"), ":boot.py"))
    pairs.append((os.path.join(ROOT, "main.py"), ":main.py"))
    return pairs


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port")
    ap.add_argument("--wipe", action="store_true")
    ap.add_argument("--stay", action="store_true")
    ap.add_argument("--format", dest="do_format", action="store_true")
    ap.add_argument("--run", action="store_true",
                    help="accepted and ignored (reboot-after is the default)")
    a = ap.parse_args()

    if subprocess.run(["mpremote", "version"], capture_output=True).returncode != 0:
        sys.exit("mpremote not found on PATH.  pip install mpremote")
    if not os.path.isdir(os.path.join(ROOT, "app")):
        sys.exit("can't find %s/app — run from the repo" % ROOT)

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

    pairs = _file_list()
    print("copying + verifying %d files" % len(pairs))
    failed = [rpath for local, rpath in pairs if not _put(p, local, rpath)]

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
        print("run it:  mpremote fs rm :noboot.txt  &&  mpremote soft-reset")
        return

    print("removing noboot.txt + reboot")
    _mpremote(p, "fs", "rm", ":noboot.txt", retries=3)
    _mpremote(p, "soft-reset", check=False)
    print("\ndone — board rebooting into the app.")


if __name__ == "__main__":
    main()
