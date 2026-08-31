# First-run setup portal. Runs (synchronously, blocking) when the board has no
# config: brings up its own WiFi AP + a captive DNS + an HTTP form. On save it
# writes /config.json and reboots into normal operation.
#
# No app framework here — this runs before app.main starts the async loop.
# deps: app.config

import time
import machine
import network

try:
    import socket
except ImportError:                      # pragma: no cover
    import usocket as socket
try:
    import select
except ImportError:                      # pragma: no cover
    import uselect as select

from app import config

AP_IP = "192.168.4.1"

# Paths various OSes probe to detect a captive portal — 302 them all to the form
# so the "Sign in to network" sheet pops up automatically.
_PROBES = (
    "/generate_204", "/gen_204", "/hotspot-detect.html", "/success.txt",
    "/library/test/success.html", "/ncsi.txt", "/connecttest.txt",
    "/canonical.html", "/redirect", "/fwlink", "/kindle-wifi/wifistub.html",
)


def _ap_credentials(device_id):
    suffix = device_id.rsplit("-", 1)[-1] if "-" in device_id else device_id[-6:]
    return "R2-D2-" + suffix.upper(), "setup-" + suffix.lower()


def _scan(sta):
    try:
        found = sta.scan()   # (ssid, bssid, channel, rssi, security, hidden)
    except Exception:
        return []
    best = {}
    for n in found:
        try:
            ssid = n[0].decode()
        except Exception:
            continue
        if ssid and (ssid not in best or n[3] > best[ssid]):
            best[ssid] = n[3]
    return sorted(best, key=lambda s: -best[s])


# ---- form / URL decoding ----
def _unquote(s):
    s = s.replace("+", " ")
    if "%" not in s:
        return s
    out, i = "", 0
    while i < len(s):
        if s[i] == "%" and i + 2 < len(s):
            try:
                out += chr(int(s[i + 1:i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out += s[i]
        i += 1
    return out


def _parse_form(body):
    d = {}
    for pair in body.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            d[_unquote(k)] = _unquote(v)
    return d


# ---- HTML ----
def _esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


_STYLE = ("body{font-family:system-ui,sans-serif;max-width:26rem;margin:1.5rem auto;"
          "padding:0 1rem;color:#222}h1{font-size:1.3rem}label{display:block;margin:.8rem 0 .2rem;"
          "font-weight:600}input{width:100%;padding:.5rem;font-size:1rem;box-sizing:border-box}"
          "fieldset{margin:1rem 0;border:1px solid #ccc;border-radius:6px}"
          "button{margin-top:1.2rem;padding:.7rem 1.2rem;font-size:1rem}"
          ".msg{background:#fde;border:1px solid #c99;padding:.6rem;border-radius:6px}"
          ".hint{color:#666;font-weight:400;font-size:.85rem}[hidden]{display:none}")


def _page(device_id, ssids, form=None, msg=""):
    form = form or {}
    opts = "".join("<option value=\"%s\">" % _esc(s) for s in ssids)
    v = lambda k, d="": _esc(form.get(k, d))
    checked = " checked" if form.get("mqtt_enabled") == "on" else ""
    return (
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>R2-D2 setup</title><style>%s</style>"
        "<h1>R2-D2 setup</h1>"
        "<p class=hint>Device: %s</p>"
        "%s"
        "<form method=post action=/save>"
        "<label>WiFi network"
        "<input name=wifi_ssid list=ssids autocapitalize=off autocorrect=off value=\"%s\" required></label>"
        "<datalist id=ssids>%s</datalist>"
        "<label>WiFi password<input name=wifi_pass type=password></label>"
        "<label>Droid name<input name=device_name value=\"%s\" placeholder='R2-D2'></label>"
        "<label><input type=checkbox name=mqtt_enabled onchange=\"m.hidden=m.disabled=!this.checked\"%s style=width:auto> "
        "Connect to Home Assistant (MQTT)</label>"
        "<fieldset id=m%s><legend>MQTT broker</legend>"
        "<label>Broker address<input name=mqtt_broker value=\"%s\" "
        "autocapitalize=off autocorrect=off spellcheck=false></label>"
        "<label>Port<input name=mqtt_port value=\"%s\" placeholder=1883 inputmode=numeric></label>"
        "<label>Username<input name=mqtt_user value=\"%s\" "
        "autocapitalize=off autocorrect=off spellcheck=false></label>"
        "<label>Password<input name=mqtt_pass type=password></label>"
        "<label>Home Assistant id "
        "<span class=hint>used in MQTT topics &amp; entity ids &mdash; must be "
        "unique per droid. Upgrading an existing R2-D2? Set this to <b>r2d2</b>."
        "</span>"
        "<input name=topic_prefix id=tp value=\"%s\" required "
        "autocapitalize=off autocorrect=off></label></fieldset>"
        "<label>Control-page PIN <span class=hint>(optional)</span>"
        "<input name=web_pin value=\"%s\"></label>"
        "<button>Save &amp; restart</button></form>"
        "<p class=hint>After it restarts, the droid appears in Home Assistant "
        "automatically (if enabled) and hosts its own control page on your WiFi.</p>"
        "<script>var f=document.forms[0],n=f.device_name,p=f.tp,touched=false;"
        "function sl(s){return s.toLowerCase().replace(/[^a-z0-9]+/g,'-')"
        ".replace(/^-+|-+$/g,'')}"
        "p.addEventListener('input',function(){touched=true});"
        "n.addEventListener('input',function(){if(!touched)p.value=sl(n.value)||'r2d2'});"
        "</script>"
        % (_STYLE, _esc(device_id),
           ("<p class=msg>%s</p>" % _esc(msg)) if msg else "",
           v("wifi_ssid"), opts, v("device_name"), checked,
           "" if checked else " hidden disabled",
           v("mqtt_broker"), v("mqtt_port", "1883"), v("mqtt_user"),
           v("topic_prefix") or _esc(config.slug(form.get("device_name", "")) or "r2d2"),
           v("web_pin")))


def _saved_page(name):
    return ("<!doctype html><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Saved</title><style>%s</style>"
            "<h1>Saved</h1><p>%s is restarting and joining your WiFi. "
            "You can close this page and reconnect your phone to your normal "
            "network.</p>" % (_STYLE, _esc(name)))


# ---- HTTP ----
def _send(conn, code, ctype, body, extra=""):
    if isinstance(body, str):
        body = body.encode()
    reason = {200: "OK", 302: "Found", 400: "Bad Request"}.get(code, "OK")
    hdr = ("HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
           "Connection: close\r\n%s\r\n" % (code, reason, ctype, len(body), extra))
    try:
        conn.send(hdr.encode())
        if body:
            conn.send(body)
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


def _read_request(conn):
    conn.settimeout(4)
    try:
        req = conn.recv(2048)
    except Exception:
        return None, None, b""
    if not req:
        return None, None, b""
    head, _, body = req.partition(b"\r\n\r\n")
    try:
        method, path, _ = head.split(b"\r\n", 1)[0].split(b" ", 2)
        method, path = method.decode(), path.decode()
    except Exception:
        return None, None, b""
    if method == "POST":
        clen = 0
        for h in head.split(b"\r\n"):
            if h.lower().startswith(b"content-length:"):
                try:
                    clen = int(h.split(b":", 1)[1].strip())
                except ValueError:
                    clen = 0
        while len(body) < clen:
            try:
                chunk = conn.recv(1024)
            except Exception:
                break
            if not chunk:
                break
            body += chunk
    return method, path.split("?", 1)[0], body


def _do_save(conn, device_id, ssids, form):
    ssid = form.get("wifi_ssid", "").strip()
    name = form.get("device_name", "").strip() or "R2-D2"
    if not ssid:
        _send(conn, 200, "text/html",
              _page(device_id, ssids, form, "WiFi network is required"))
        return
    # blank topic_prefix -> config._normalise derives it from the droid name
    prefix = config.slug(form.get("topic_prefix", "").strip())
    updates = {
        "wifi_ssid": ssid,
        "wifi_pass": form.get("wifi_pass", ""),
        "device_name": name,
        "topic_prefix": prefix or config.slug(name) or "r2d2",
        "web_pin": form.get("web_pin", "").strip(),
        "mqtt_enabled": form.get("mqtt_enabled", "") == "on",
        "configured": True,
    }
    if updates["mqtt_enabled"]:
        broker = form.get("mqtt_broker", "").strip()
        if not broker:
            _send(conn, 200, "text/html",
                  _page(device_id, ssids, form,
                        "MQTT is enabled but the broker address is blank"))
            return
        updates["mqtt_broker"] = broker
        updates["mqtt_port"] = form.get("mqtt_port", "1883").strip() or "1883"
        updates["mqtt_user"] = form.get("mqtt_user", "")
        updates["mqtt_pass"] = form.get("mqtt_pass", "")
    try:
        config.save(updates)
    except Exception as e:
        _send(conn, 200, "text/html",
              _page(device_id, ssids, form, "Could not save: %s" % e))
        return
    print("[setup] saved config for SSID %r, name %r — rebooting" % (ssid, name))
    _send(conn, 200, "text/html", _saved_page(name))
    time.sleep(2)
    machine.reset()


def _handle(conn, device_id, st):
    method, path, body = _read_request(conn)
    if method is None:
        try:
            conn.close()
        except Exception:
            pass
        return

    if method == "POST" and path == "/save":
        _do_save(conn, device_id, st["ssids"], _parse_form(body.decode()))
        return
    if method == "GET" and path == "/scan":
        st["ssids"] = _scan(st["sta"])
        _send(conn, 200, "application/json",
              "[" + ",".join('"%s"' % s for s in st["ssids"]) + "]")
        return
    if path != "/" or path in _PROBES:
        _send(conn, 302, "text/plain", "",
              extra="Location: http://%s/\r\n" % AP_IP)
        return
    _send(conn, 200, "text/html", _page(device_id, st["ssids"]))


# ---- DNS (answer every A query with the AP's own IP) ----
def _dns_reply(data):
    if len(data) < 12:
        return None
    i = 12
    while i < len(data) and data[i] != 0:
        i += 1 + data[i]
    qend = i + 5  # null label + qtype(2) + qclass(2)
    if qend > len(data):
        return None
    return (data[:2] + b"\x81\x80" + data[4:6] + b"\x00\x01"
            + b"\x00\x00\x00\x00" + data[12:qend]
            + b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04"
            + bytes(int(x) for x in AP_IP.split(".")))


def run(device_id):
    ssid, password = _ap_credentials(device_id)

    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    ssids = _scan(sta)  # scan before the AP is up — avoids AP/STA channel churn

    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    try:
        ap.config(essid=ssid, password=password)
    except Exception:
        ap.config(essid=ssid)  # fall back to an open AP if WPA setup is rejected
        password = "(open)"
    try:
        ap.ifconfig((AP_IP, "255.255.255.0", AP_IP, AP_IP))
    except Exception:
        pass

    print("[setup] unconfigured — captive portal up")
    print("[setup]   join WiFi:  %s   password: %s" % (ssid, password))
    print("[setup]   then open:  http://%s/" % AP_IP)

    dns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dns.setblocking(False)
    dns.bind(("0.0.0.0", 53))

    http = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        http.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except Exception:
        pass
    http.bind(("0.0.0.0", 80))
    http.listen(4)
    http.setblocking(False)

    poll = select.poll()
    poll.register(dns, select.POLLIN)
    poll.register(http, select.POLLIN)

    st = {"ssids": ssids, "sta": sta}
    while True:
        for sock, _ev in poll.poll(2000):
            if sock is dns:
                try:
                    data, addr = dns.recvfrom(320)
                    reply = _dns_reply(data)
                    if reply:
                        dns.sendto(reply, addr)
                except Exception:
                    pass
            else:
                try:
                    conn, _addr = http.accept()
                except Exception:
                    continue
                try:
                    _handle(conn, device_id, st)
                except Exception as e:
                    print("[setup] request error:", e)
                    try:
                        conn.close()
                    except Exception:
                        pass
