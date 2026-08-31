# Minimal DFPlayer Mini (YX5200) UART driver.
#
# Written for this project rather than vendored — the frame/checksum protocol
# is per the DFPlayer Mini datasheet and the widely-published community
# command tables, not copied from a specific library. Only the handful of
# commands this build needs (RESET, TF-card select, PLAY_FOLDER_TRACK,
# SET_VOLUME, status queries), all hand-validated against real hardware on
# the bench.
#
# Frame layout: 0x7E (start) | 0xFF (version) | 0x06 (length) | cmd |
# feedback | param1 | param2 | checksum_hi | checksum_lo | 0xEF (end)
# checksum = two's-complement (16-bit) of the sum of version..param2.

import time

CMD_RESET = 0x0C
CMD_SET_VOLUME = 0x06
CMD_SEL_DEV = 0x09
CMD_PLAY_FOLDER_TRACK = 0x0F
CMD_QUERY_STATUS = 0x42
CMD_QUERY_TF_FILE_COUNT = 0x48

DEV_TF_CARD = 0x02

_ERROR_CODES = {
    0x00: "module busy / initializing",
    0x02: "currently sleeping",
    0x06: "track not found",
}

_REPLY_NAMES = {
    0x3F: "init/status (bit0=USB bit1=TF bit2=PC online)",
    0x40: "ERROR",
    0x41: "ack",
    0x42: "status reply",
    0x48: "TF file count reply",
}


class DFPlayer:
    def __init__(self, uart, log=None):
        self.uart = uart
        # optional callable(*args) for diagnostics, e.g. this device's
        # dbg()/log_always() — kept optional so this module has no hard
        # dependency on main.py's logging setup.
        self._log = log or (lambda *a: None)

    def _build_frame(self, cmd, param1=0, param2=0, feedback=0):
        body = [0xFF, 0x06, cmd, feedback, param1, param2]
        checksum = (0 - sum(body)) & 0xFFFF
        return bytes([0x7E] + body + [(checksum >> 8) & 0xFF, checksum & 0xFF, 0xEF])

    def _send(self, cmd, param1=0, param2=0, feedback=0, wait_ms=50):
        frame = self._build_frame(cmd, param1, param2, feedback)
        try:
            self.uart.write(frame)
        except Exception as e:
            # No DFPlayer attached yet (not wired), or a transient UART
            # error — log and move on rather than crash the caller. Writing
            # to a UART with nothing on the other end is normally a silent
            # no-op, not an exception, but this guards the rare case anyway.
            self._log("[dfplayer] write failed:", e)
            return
        self._log_reply(cmd, wait_ms)

    def _log_reply(self, sent_cmd, wait_ms):
        # Best-effort, non-blocking: give the module a brief moment to
        # reply, then log whatever's there — not just errors. This is the
        # only way to tell "command sent and DFPlayer is silent because
        # nothing's wrong" apart from "command sent into a dead/miswired
        # UART" from software alone: with nothing attached, this finds
        # nothing and logs that fact explicitly instead of staying quiet.
        time.sleep_ms(wait_ms)
        try:
            n = self.uart.any()
            reply = self.uart.read(n) if n else None
        except Exception as e:
            self._log("[dfplayer] read after cmd 0x%02x failed: %s" % (sent_cmd, e))
            return
        if not reply:
            self._log("[dfplayer] no reply after cmd 0x%02x (nothing on RX — "
                       "check wiring/power if this happens for every command)" % sent_cmd)
            return
        found = False
        for i in range(len(reply) - 9):
            if reply[i] == 0x7E and reply[i + 9] == 0xEF:
                found = True
                frame = reply[i:i + 10]
                rcmd, p1, p2 = frame[3], frame[5], frame[6]
                name = _REPLY_NAMES.get(rcmd, "unknown 0x%02x" % rcmd)
                if rcmd == 0x40:
                    self._log("[dfplayer] reply to cmd 0x%02x: %s code=0x%02x (%s)" %
                              (sent_cmd, name, p2, _ERROR_CODES.get(p2, "unknown")))
                else:
                    self._log("[dfplayer] reply to cmd 0x%02x: %s params=0x%02x%02x" %
                              (sent_cmd, name, p1, p2))
        if not found:
            self._log("[dfplayer] got %d byte(s) after cmd 0x%02x, no valid frame: %s" %
                       (len(reply), sent_cmd, reply))

    def reset(self):
        # Triggers the module's own unsolicited init/status reply (0x3F)
        # regardless of the feedback bit — the most reliable single call
        # for "is anything even there" diagnostics.
        self._send(CMD_RESET, wait_ms=200)

    def query_status(self):
        self._send(CMD_QUERY_STATUS, feedback=1)

    def query_file_count(self):
        self._send(CMD_QUERY_TF_FILE_COUNT, feedback=1)

    def select_tf_card(self):
        self._send(CMD_SEL_DEV, param2=DEV_TF_CARD)

    def set_volume(self, level):
        # level: 0-30
        level = max(0, min(30, level))
        self._send(CMD_SET_VOLUME, param2=level)

    def play_folder_track(self, folder, track):
        # folder: 1-99, track: 1-255 (files named e.g. /03/007.mp3)
        self._send(CMD_PLAY_FOLDER_TRACK, param1=folder, param2=track)
