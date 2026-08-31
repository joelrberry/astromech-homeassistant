#!/usr/bin/env python3
"""Timestamped serial capture that does NOT reset the ESP32 on connect.

    python3 tools/serial-log.py [--port PORT] [--baud 115200] [--out FILE]

Unlike mpremote (which soft-resets the board when it connects), this opens the
port with DTR/RTS held low so the board keeps running untouched. It reconnects
automatically when the port disappears — a hard reset or brownout re-enumerates
USB — so it captures the ROM banner on the *next* reboot, including the
`Brownout detector was triggered` line that `machine.reset_cause()` can't see
on a classic ESP32.

Leave it running overnight to catch an unexplained reboot. Ctrl-C to stop.

Needs:  pip install pyserial
"""

import argparse
import datetime
import glob
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial not installed.  pip install pyserial")

_PORT_GLOBS = ("/dev/cu.usbserial*", "/dev/cu.usbmodem*", "/dev/cu.wchusbserial*",
               "/dev/cu.SLAB_USBtoUART*", "/dev/ttyUSB*", "/dev/ttyACM*")


def pick_port():
    for pat in _PORT_GLOBS:
        m = sorted(glob.glob(pat))
        if m:
            return m[0]
    return None


def _ts():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def open_no_reset(port, baud):
    s = serial.Serial()
    s.port = port
    s.baudrate = baud
    s.timeout = 1
    s.dtr = False          # applied on open() -> auto-reset circuit stays idle
    s.rts = False
    s.open()
    return s


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--out", default="serial-%s.log"
                    % datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    a = ap.parse_args()

    out = open(a.out, "a", buffering=1)
    print("logging to %s   (Ctrl-C to stop)" % a.out)

    def emit(line):
        rec = "[%s] %s" % (_ts(), line)
        print(rec)
        out.write(rec + "\n")
        out.flush()

    try:
        while True:
            port = a.port or pick_port()
            if not port:
                emit("<no serial port — waiting>")
                time.sleep(2)
                continue
            try:
                s = open_no_reset(port, a.baud)
            except Exception as e:
                emit("<open %s failed: %s>" % (port, e))
                time.sleep(2)
                continue
            emit("<connected to %s @ %d (no reset)>" % (port, a.baud))
            buf = b""
            try:
                while True:
                    chunk = s.read(256)
                    if not chunk:
                        continue
                    buf += chunk
                    while b"\n" in buf:
                        ln, buf = buf.split(b"\n", 1)
                        emit(ln.rstrip(b"\r").decode("utf-8", "replace"))
            except (serial.SerialException, OSError) as e:
                emit("<port lost: %s — reconnecting>" % e)
                try:
                    s.close()
                except Exception:
                    pass
                time.sleep(1)
    except KeyboardInterrupt:
        emit("<stopped>")


if __name__ == "__main__":
    main()
