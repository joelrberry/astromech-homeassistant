# Local web control panel — served on the droid's own WiFi, works with or
# without Home Assistant. Mode / sound / volume, no app needed.
#
# deps: app.core, app.net (apply_*), app.servo, app.sound, app.diag, app.ota

import ujson
import uasyncio

from app import core, net, servo, sound, diag, ota

_PORT = 80


def _state():
    s = core.state
    return ujson.dumps({"mode": s["mode"], "sound": s["sound"],
                        "volume": s["volume"], "name": core.cfg["device_name"]})


_STYLE = (
    ":root{--b:#1b1e24;--c:#eceef2;--a:#3a86ff;--m:#2a2f38}"
    "*{box-sizing:border-box}body{margin:0;font-family:system-ui,sans-serif;"
    "background:var(--b);color:var(--c);padding:1rem;max-width:32rem;margin:auto}"
    "h1{font-size:1.3rem;margin:.2rem 0 1rem}"
    ".grid{display:grid;grid-template-columns:repeat(2,1fr);gap:.5rem}"
    "button{font:inherit;padding:.9rem .5rem;border:1px solid var(--m);border-radius:.6rem;"
    "background:var(--m);color:var(--c);cursor:pointer}"
    "button.on{background:var(--a);border-color:var(--a);font-weight:700}"
    "h2{font-size:.85rem;text-transform:uppercase;letter-spacing:.05em;color:#9aa2ad;"
    "margin:1.4rem 0 .5rem}"
    "input[type=range]{width:100%}.row{display:flex;align-items:center;gap:.8rem}"
    "#pin{width:100%;padding:.6rem;margin-bottom:.6rem;border-radius:.5rem;"
    "border:1px solid var(--m);background:var(--m);color:var(--c)}"
    "#err{color:#ff6b6b;font-size:.85rem;min-height:1.1em}"
    "#diag{font-size:.8rem;color:#9aa2ad;line-height:1.5;white-space:pre-wrap;margin:0}")

# built once at import from the current behaviour/sound registries
_MODES = sorted(servo.SERVO_BEHAVIORS.keys())
_SOUNDS = sorted(sound.SOUND_FOLDERS.keys())


def _page():
    mbtns = "".join("<button data-mode=\"%s\">%s</button>" % (m, m) for m in _MODES)
    sbtns = "".join("<button data-sound=\"%s\">%s</button>" % (s, s) for s in _SOUNDS)
    pin_row = ("<input id=pin type=password placeholder='control PIN' "
               "autocomplete=off>") if core.cfg.get("web_pin") else ""
    return (
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>%s</title><style>%s</style>"
        "<h1 id=title>%s</h1>"
        "%s<div id=err></div>"
        "<h2>Mode</h2><div class=grid id=modes>%s</div>"
        "<h2>Sound</h2><div class=grid id=sounds>%s</div>"
        "<h2>Volume <span id=vlab></span></h2>"
        "<div class=row><input type=range id=vol min=0 max=30></div>"
        "<h2>Firmware</h2><div id=fw class=row>"
        "<span id=fwtxt>checking...</span>"
        "<button id=fwchk>Check</button><button id=fwupd hidden>Update</button></div>"
        "<h2>Diagnostics</h2><pre id=diag>...</pre>"
        "<script>%s</script>"
        % (core.cfg["device_name"], _STYLE, core.cfg["device_name"],
           pin_row, mbtns, sbtns, _JS))


_JS = """
var E=function(s){return document.querySelector(s)},pin=E('#pin');
function post(body){
 return fetch('/set',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},
  body:body+(pin?('&pin='+encodeURIComponent(pin.value)):'')})
 .then(function(r){if(r.status===403){E('#err').textContent='Wrong PIN';return null}
  E('#err').textContent='';return r.json()}).then(render)}
function render(st){
 if(!st)return;
 E('#title').textContent=st.name;
 document.querySelectorAll('#modes button').forEach(function(b){
  b.classList.toggle('on',b.dataset.mode===st.mode)});
 document.querySelectorAll('#sounds button').forEach(function(b){
  b.classList.toggle('on',b.dataset.sound===st.sound)});
 var v=E('#vol');if(document.activeElement!==v)v.value=st.volume;
 E('#vlab').textContent=st.volume;
}
document.getElementById('modes').addEventListener('click',function(e){
 if(e.target.dataset.mode)post('mode='+e.target.dataset.mode)});
document.getElementById('sounds').addEventListener('click',function(e){
 if(e.target.dataset.sound)post('sound='+e.target.dataset.sound)});
E('#vol').addEventListener('change',function(){post('volume='+this.value)});
E('#vol').addEventListener('input',function(){E('#vlab').textContent=this.value});
function poll(){fetch('/state').then(function(r){return r.json()}).then(render).catch(function(){})}
poll();setInterval(poll,1500);
function fw(action){
 var b=action?('action='+action):'';
 return fetch('/ota',{method:action?'POST':'GET',
  headers:{'Content-Type':'application/x-www-form-urlencoded'},
  body:action?(b+(pin?('&pin='+encodeURIComponent(pin.value)):'')):undefined})
 .then(function(r){return r.json()}).then(fwrender).catch(function(){})}
function fwrender(o){
 if(!o)return;
 var t=o.installed;
 if(o.status==='updating')t='updating\\u2026 do not power off';
 else if(o.status==='available')t=o.installed+'  \\u2192  '+o.latest+' available';
 else if(o.status==='error')t=o.installed+'  (check failed: '+(o.msg||'')+')';
 E('#fwtxt').textContent=t;
 E('#fwupd').hidden=(o.status!=='available');
 E('#fwchk').disabled=(o.status==='updating');
}
E('#fwchk').addEventListener('click',function(){fw('check')});
E('#fwupd').addEventListener('click',function(){
 if(confirm('Update firmware and reboot?'))fw('install')});
fw();setInterval(fw,5000);
function fmtdur(s){var h=s/3600|0,m=s%3600/60|0;return h?(h+'h '+m+'m'):(m+'m '+(s%60|0)+'s')}
function fmtb(b){return b>1048576?(b/1048576).toFixed(1)+' MB':(b/1024|0)+' KB'}
function drender(d){
 var L=[];
 L.push('uptime      '+fmtdur(d.uptime_s));
 L.push('reset       '+d.reset_cause);
 L.push('free heap   '+fmtb(d.heap_free)+'  ('+d.heap_free_pct+'%)');
 if(d.fs_free!=null)L.push('free flash  '+fmtb(d.fs_free)+(d.fs_total?(' / '+fmtb(d.fs_total)):''));
 if(d.ip)L.push('ip          '+d.ip);
 if(d.ssid)L.push('wifi        '+d.ssid+(d.channel?(' ch'+d.channel):'')+(d.rssi!=null?('  '+d.rssi+' dBm'):''));
 else if(d.rssi!=null)L.push('wifi        '+d.rssi+' dBm');
 L.push('time sync   '+(d.time_synced?'yes':'no'));
 L.push('restarts    '+d.task_restarts+'   reconnects '+d.reconnects);
 if(d.mcu_temp_c!=null)L.push('mcu temp    '+d.mcu_temp_c+' \\u00b0C');
 L.push('cpu         '+d.cpu_mhz+' MHz');
 E('#diag').textContent=L.join('\\n');
}
function dpoll(){fetch('/diag').then(function(r){return r.json()}).then(drender).catch(function(){})}
dpoll();setInterval(dpoll,15000);
"""


# ---- tiny async HTTP ----
async def _read_form(reader, headers):
    clen = 0
    for h in headers:
        if h[:15].lower() == b"content-length:":
            try:
                clen = int(h.split(b":", 1)[1].strip())
            except ValueError:
                clen = 0
    body = b""
    while len(body) < clen:
        chunk = await reader.read(min(512, clen - len(body)))
        if not chunk:
            break
        body += chunk
    form = {}
    for pair in body.decode().split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            form[k] = _unquote(v)
    return form


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


async def _send(writer, code, ctype, body):
    if isinstance(body, str):
        body = body.encode()
    hdr = ("HTTP/1.1 %d OK\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
           "Connection: close\r\n\r\n" % (code, ctype, len(body)))
    writer.write(hdr.encode())
    writer.write(body)
    await writer.drain()


async def _handle(reader, writer):
    try:
        line = await reader.readline()
        if not line:
            return
        try:
            method, path, _ = line.split(b" ", 2)
        except ValueError:
            return
        method = method.decode()
        path = path.decode().split("?", 1)[0]

        headers = []
        while True:
            h = await reader.readline()
            if not h or h == b"\r\n":
                break
            headers.append(h.strip())

        if path == "/" and method == "GET":
            await _send(writer, 200, "text/html", _page())
        elif path == "/state":
            await _send(writer, 200, "application/json", _state())
        elif path == "/diag":
            await _send(writer, 200, "application/json", ujson.dumps(diag.snapshot()))
        elif path == "/ota" and method == "GET":
            await _send(writer, 200, "application/json", ujson.dumps(ota.state))
        elif path == "/set" and method == "POST":
            form = await _read_form(reader, headers)
            wp = core.cfg.get("web_pin")
            if wp and form.get("pin") != wp:
                await _send(writer, 403, "application/json", '{"error":"pin"}')
            else:
                if "mode" in form:
                    net.apply_mode(form["mode"])
                elif "sound" in form:
                    net.apply_sound(form["sound"])
                elif "volume" in form:
                    net.apply_volume(form["volume"])
                await _send(writer, 200, "application/json", _state())
        elif path == "/ota" and method == "POST":
            form = await _read_form(reader, headers)
            wp = core.cfg.get("web_pin")
            if wp and form.get("pin") != wp:
                await _send(writer, 403, "application/json", '{"error":"pin"}')
            else:
                act = form.get("action", "check")
                uasyncio.create_task(ota.update() if act == "install" else ota.check())
                await _send(writer, 200, "application/json", ujson.dumps(ota.state))
        else:
            await _send(writer, 404, "text/plain", "not found")
    except Exception as e:
        core.dbg("[web] request error:", e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


_server = None


async def web_server_task():
    global _server
    if _server is not None:          # a supervise() restart — free the old socket
        try:
            _server.close()
            await _server.wait_closed()
        except Exception:
            pass
        _server = None
    _server = await uasyncio.start_server(_handle, "0.0.0.0", _PORT)
    core.log_always("[web] control panel on :%d" % _PORT)
    await _server.wait_closed()
