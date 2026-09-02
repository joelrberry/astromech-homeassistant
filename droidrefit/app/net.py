# WiFi + MQTT + Home Assistant discovery. Optional layer — only runs when
# core.cfg["network_enabled"]. Commands land via app.control.
#
# deps: app.core, app.sound, app.servo, app.diag, app.ota, app.control, app.config

import network
import ujson
import uasyncio

from umqtt.simple import MQTTClient
from app import core, sound, servo, diag, ota, control, config

# ---- MQTT topics ----
# Every topic, HA unique_id and HA device id is prefixed with this droid's
# identity (core.cfg["topic_prefix"]) so multiple droids share one broker
# cleanly. net is imported after core.init(), so core.cfg is populated here.
# A returning single-unit user sets topic_prefix = "r2d2" and everything below
# is byte-identical to the pre-multi-unit firmware.
PREFIX = ((core.cfg or {}).get("topic_prefix") or "r2d2")
_P = PREFIX.encode()

MQTT_COMMAND_TOPIC = _P + b'/command'
MQTT_MODE_STATE_TOPIC = _P + b'/mode/state'
MQTT_DEBUG_SET_TOPIC = _P + b'/debug/set'
MQTT_DEBUG_STATE_TOPIC = _P + b'/debug/state'
MQTT_AVAILABILITY_TOPIC = _P + b'/availability'
MQTT_HEARTBEAT_TOPIC = _P + b'/heartbeat'

MQTT_SOUND_COMMAND_TOPIC = _P + b'/sound/command'
MQTT_SOUND_STATE_TOPIC = _P + b'/sound/state'
MQTT_VOLUME_SET_TOPIC = _P + b'/sound/volume/set'
MQTT_VOLUME_STATE_TOPIC = _P + b'/sound/volume/state'
# Bench-debugging hook for the DFPlayer wiring: publish "reset"/"status"/
# "filecount" here to get the module's raw reply logged (debug switch on).
MQTT_DIAG_SET_TOPIC = _P + b'/sound/diag/set'

# Device telemetry (app.diag): a periodic JSON blob + a one-shot retained
# reset-cause. Feed HA's diagnostic sensors.
MQTT_TELEMETRY_TOPIC = _P + b'/diag'
MQTT_RESET_CAUSE_TOPIC = _P + b'/diag/reset'

# Firmware update (app.ota): state JSON for HA's Update entity + a command topic.
MQTT_OTA_STATE_TOPIC = _P + b'/ota'
MQTT_OTA_SET_TOPIC = _P + b'/ota/set'

# --- Home Assistant mood tuning ---
# <prefix>/tune/<mood>/<knob>       retained current value
# <prefix>/tune/<mood>/<knob>/set   command
# <prefix>/tune/<mood>/reset        button -> revert this mood to firmware stock
# Knobs are 0..max, mid == firmware stock; an absent config key == stock, so
# servo/leds fall back to their hard-coded values. See servo._spd/_wait,
# leds._reaction_for.
_TUNE_PREFIX = _P + b'/tune/'
_TUNE_KNOBS = {
    "standby":      ("speed", "rest", "bright"),
    "awake":        ("speed", "rest", "bright"),
    "excited":      ("speed", "rest", "bright"),
    "alert":        ("speed", "rest", "bright"),
    "surveillance": ("speed", "bright"),
    "system_crash": ("speed", "bright"),
    "sleep":        ("bright",),
    "hologram":     ("bright",),
}
_KNOB_SPEC = {   # knob -> (min, max, step, default, HA label)
    "speed":  (0, 100, 5, 50, "Speed"),
    "rest":   (0, 100, 5, 50, "Restlessness"),
    "bright": (0, 200, 5, 100, "LED Bright"),
}


def _tune_topic(mood, knob):
    return _TUNE_PREFIX + mood.encode() + b'/' + knob.encode()


def _title(s):
    # MicroPython str has no .title() — capitalise each underscore-separated word
    return " ".join(w[:1].upper() + w[1:] for w in s.split("_"))


def _disc(component, obj):
    return ('homeassistant/%s/%s/%s/config' % (component, PREFIX, obj)).encode()


MQTT_MODE_DISCOVERY_TOPIC = _disc('select', 'mode')
MQTT_SOUND_DISCOVERY_TOPIC = _disc('select', 'sound')
MQTT_VOLUME_DISCOVERY_TOPIC = _disc('number', 'sound_volume')
MQTT_DEBUG_DISCOVERY_TOPIC = _disc('switch', 'debug')
MQTT_HEARTBEAT_DISCOVERY_TOPIC = _disc('sensor', 'heartbeat')

# The old sound-satellite board published its own debug switch + heartbeat
# under the original 'r2d2' identity; clear those ghosts once, but only for a
# board that IS that original identity.
MQTT_ORPHANED_DISCOVERY_TOPICS = ([
    b'homeassistant/switch/r2d2/sound_debug/config',
    b'homeassistant/sensor/r2d2/sound_heartbeat/config',
] if PREFIX == 'r2d2' else [])


def on_mqtt_message(topic, msg):
    if topic == MQTT_DEBUG_SET_TOPIC:
        want_on = msg.strip().upper() == b'ON'
        core.state["debug"] = want_on
        print('[%s] [debug] set to' % core.timestamp(), want_on)
        return

    if topic == MQTT_DIAG_SET_TOPIC:
        cmd = msg.strip().lower()
        core.dbg("[diag] running", cmd)
        if cmd == b'reset':
            sound.player.reset()
        elif cmd == b'status':
            sound.player.query_status()
        elif cmd == b'filecount':
            sound.player.query_file_count()
        else:
            core.dbg("[diag] unknown command:", msg)
        return

    if topic == MQTT_VOLUME_SET_TOPIC:
        try:
            level = int(msg.strip())
        except ValueError:
            core.dbg("[mqtt] bad volume payload:", msg)
            return
        control.apply_volume(level)
        return

    if topic == MQTT_OTA_SET_TOPIC and ota.enabled():
        cmd = msg.strip().lower()
        if cmd in (b'install', b'update'):
            uasyncio.create_task(ota.update())
        else:
            uasyncio.create_task(ota.check())
        return

    if topic.startswith(_TUNE_PREFIX):
        _handle_tune(topic[len(_TUNE_PREFIX):], msg)
        return

    core.dbg("[mqtt] message on", topic, "->", msg)
    try:
        payload = ujson.loads(msg)
    except Exception as e:
        core.dbg("[mqtt] bad json:", msg, e)
        return

    if topic == MQTT_COMMAND_TOPIC:
        if not control.apply_mode(payload.get("mode")):
            core.dbg("[mqtt] unknown/missing mode in payload:", payload)
    elif topic == MQTT_SOUND_COMMAND_TOPIC:
        if not control.apply_sound(payload.get("sound")):
            core.dbg("[mqtt] unknown/missing sound in payload:", payload)


async def _publish_tune_soon(mood, knob):
    await uasyncio.sleep_ms(50)          # let check_msg() finish first
    publish_tune_state(mood, knob)


def _handle_tune(rest_topic, msg):
    parts = rest_topic.split(b'/')      # b'excited/speed/set' | b'excited/reset'
    if len(parts) == 2 and parts[1] == b'reset':
        mood = parts[0].decode()
        if mood in _TUNE_KNOBS:
            core.cfg = config.forget("tune_%s_" % mood)
            core.tune_gen += 1
            core.log_always("[tune]", mood, "reset to stock")
            uasyncio.create_task(_publish_tune_soon(mood, None))
        return
    if len(parts) == 3 and parts[2] == b'set':
        mood, knob = parts[0].decode(), parts[1].decode()
        if mood in _TUNE_KNOBS and knob in _TUNE_KNOBS[mood]:
            lo, hi = _KNOB_SPEC[knob][0], _KNOB_SPEC[knob][1]
            try:
                v = max(lo, min(hi, int(float(msg.strip()))))
            except (ValueError, TypeError):
                return
            core.cfg = config.save({"tune_%s_%s" % (mood, knob): v})
            core.tune_gen += 1
            core.log_always("[tune] %s %s = %d" % (mood, knob, v))
            uasyncio.create_task(_publish_tune_soon(mood, knob))


def publish_tune_state(mood=None, knob=None):
    client = core.conn["client"]
    if client is None:
        return
    moods = (mood,) if mood else _TUNE_KNOBS
    for m in moods:
        for k in _TUNE_KNOBS.get(m, ()):
            if knob and k != knob:
                continue
            v = core.cfg.get("tune_%s_%s" % (m, k), _KNOB_SPEC[k][3])
            try:
                client.publish(_tune_topic(m, k), str(v).encode(), retain=True)
            except Exception as e:
                core.dbg("[tune] state publish failed:", e)
                core.link_state["down"] = True
                return


# ---- tasks ----
async def mqtt_task():
    while True:
        client = core.conn["client"]
        if client is not None:
            try:
                client.check_msg()
            except OSError as e:
                if e.args and e.args[0] == -1:
                    pass  # known umqtt.simple quirk: "no message waiting"
                else:
                    core.dbg("[mqtt] check_msg error:", e)
                    core.link_state["down"] = True
        await uasyncio.sleep_ms(100)


async def state_publish_task():
    last_mode = None
    last_sound = None
    last_volume = None
    last_debug = None
    while True:
        client = core.conn["client"]
        if client is not None:
            try:
                if core.state["mode"] != last_mode:
                    client.publish(MQTT_MODE_STATE_TOPIC, core.state["mode"].encode(), retain=True)
                    last_mode = core.state["mode"]
                if core.state["sound"] != last_sound:
                    client.publish(MQTT_SOUND_STATE_TOPIC, core.state["sound"].encode(), retain=True)
                    last_sound = core.state["sound"]
                if core.state["volume"] != last_volume:
                    client.publish(MQTT_VOLUME_STATE_TOPIC, str(core.state["volume"]).encode(), retain=True)
                    last_volume = core.state["volume"]
                if core.state["debug"] != last_debug:
                    client.publish(MQTT_DEBUG_STATE_TOPIC, b'ON' if core.state["debug"] else b'OFF', retain=True)
                    last_debug = core.state["debug"]
            except Exception as e:
                core.dbg("[state_publish] publish failed:", e)
                core.link_state["down"] = True
        await uasyncio.sleep_ms(200)


async def heartbeat_task():
    # Independent of the debug flag on purpose — an HA expire_after sensor
    # relies on this for liveness even with debug logging off. Also re-asserts
    # the availability topic each cycle: if a stale LWT 'offline' ever lands on
    # it (e.g. a broker restart, or a half-open old connection reaped late),
    # the entities recover within a minute instead of staying dead until the
    # next reconnect.
    while True:
        client = core.conn["client"]
        if client is not None:
            try:
                client.publish(MQTT_AVAILABILITY_TOPIC, b'online', retain=True)
                client.publish(MQTT_HEARTBEAT_TOPIC, core.timestamp().encode())
            except Exception as e:
                core.dbg("[heartbeat] publish failed:", e)
                core.link_state["down"] = True
        await uasyncio.sleep_ms(60000)


def publish_ota_state():
    # ota._on_change points here — publish the HA Update entity's state.
    client = core.conn["client"]
    if client is None:
        return
    s = ota.state
    payload = {"installed_version": s["installed"],
               "latest_version": s["latest"] or s["installed"],
               "in_progress": s["status"] == "updating"}
    try:
        client.publish(MQTT_OTA_STATE_TOPIC, ujson.dumps(payload).encode(), retain=True)
    except Exception as e:
        core.dbg("[ota] state publish failed:", e)
        core.link_state["down"] = True


async def diag_task():
    # Periodic telemetry blob for HA's diagnostic sensors. reset_cause is a
    # boot constant — published once (retained) by establish_link, not here.
    while True:
        client = core.conn["client"]
        if client is not None:
            try:
                d = diag.snapshot()
                d.pop("reset_cause", None)
                client.publish(MQTT_TELEMETRY_TOPIC, ujson.dumps(d).encode())
            except Exception as e:
                core.dbg("[diag] publish failed:", e)
                core.link_state["down"] = True
        await uasyncio.sleep_ms(30000)


async def connection_watchdog():
    # umqtt.simple has no reconnect logic of its own (see PROJECT_NOTES.md).
    # Check WiFi + link_state every 15s; full reconnect if either looks bad.
    wlan = network.WLAN(network.STA_IF)
    while True:
        await uasyncio.sleep_ms(15000)

        if wlan.isconnected() and not core._time_synced:
            core.sync_time()

        if wlan.isconnected() and not core.link_state["down"]:
            continue
        core.log_always("[watchdog] link unhealthy (wifi=%s, flagged_down=%s) — reconnecting" %
                        (wlan.isconnected(), core.link_state["down"]))
        try:
            await establish_link()
            core.reconnects += 1
            core.log_always("[watchdog] reconnected (#%d)" % core.reconnects)
        except Exception as e:
            core.log_always("[watchdog] reconnect attempt failed, will retry:", e)


# ---- connect / discovery ----
def publish_discovery(client):
    device_info = {
        "identifiers": [PREFIX],
        "name": core.cfg["device_name"],
        "manufacturer": "CyberBrick (custom firmware)",
    }

    def emit(disc_topic, payload):
        payload["device"] = device_info
        payload["unique_id"] = PREFIX + "_" + payload["unique_id"]
        client.publish(disc_topic, ujson.dumps(payload).encode(), retain=True)

    emit(MQTT_MODE_DISCOVERY_TOPIC, {
        "name": "Mode",
        "unique_id": "mode_select",
        "options": sorted(servo.SERVO_BEHAVIORS.keys()),
        "state_topic": MQTT_MODE_STATE_TOPIC.decode(),
        "command_topic": MQTT_COMMAND_TOPIC.decode(),
        "command_template": '{"mode": "{{ value }}"}',
        "availability_topic": MQTT_AVAILABILITY_TOPIC.decode(),
    })
    emit(MQTT_SOUND_DISCOVERY_TOPIC, {
        "name": "Sound",
        "unique_id": "sound_select",
        "options": [core.SOUND_STATE_IDLE] + sorted(sound.SOUND_FOLDERS.keys()),
        "state_topic": MQTT_SOUND_STATE_TOPIC.decode(),
        "command_topic": MQTT_SOUND_COMMAND_TOPIC.decode(),
        "command_template": '{"sound": "{{ value }}"}',
        "availability_topic": MQTT_AVAILABILITY_TOPIC.decode(),
    })
    emit(MQTT_VOLUME_DISCOVERY_TOPIC, {
        "name": "Sound Volume",
        "unique_id": "sound_volume_number",
        "min": 0, "max": 30, "step": 1,
        "state_topic": MQTT_VOLUME_STATE_TOPIC.decode(),
        "command_topic": MQTT_VOLUME_SET_TOPIC.decode(),
        "availability_topic": MQTT_AVAILABILITY_TOPIC.decode(),
    })
    emit(MQTT_DEBUG_DISCOVERY_TOPIC, {
        "name": "Debug Logging",
        "unique_id": "debug_switch",
        "state_topic": MQTT_DEBUG_STATE_TOPIC.decode(),
        "command_topic": MQTT_DEBUG_SET_TOPIC.decode(),
        "availability_topic": MQTT_AVAILABILITY_TOPIC.decode(),
    })
    emit(MQTT_HEARTBEAT_DISCOVERY_TOPIC, {
        "name": "Heartbeat",
        "unique_id": "heartbeat_sensor",
        "state_topic": MQTT_HEARTBEAT_TOPIC.decode(),
        "expire_after": 180,
    })
    if ota.enabled():
        emit(_disc('update', 'firmware'), {
            "name": "Firmware",
            "unique_id": "firmware_update",
            "state_topic": MQTT_OTA_STATE_TOPIC.decode(),
            "command_topic": MQTT_OTA_SET_TOPIC.decode(),
            "payload_install": "install",
            "entity_category": "config",
        })

    # --- diagnostic sensors: all read one JSON blob, land in HA's collapsed
    #     "Diagnostic" section on the device page ---
    _tele = MQTT_TELEMETRY_TOPIC.decode()

    def diag_sensor(obj, name, key, unit=None, dev_class=None,
                    state_class="measurement"):
        p = {"name": name, "unique_id": obj, "state_topic": _tele,
             "value_template": "{{ value_json.%s }}" % key,
             "entity_category": "diagnostic"}
        if unit:
            p["unit_of_measurement"] = unit
        if dev_class:
            p["device_class"] = dev_class
        if state_class:
            p["state_class"] = state_class
        emit(_disc('sensor', obj), p)

    diag_sensor("heap_free", "Free Memory", "heap_free", "B")
    diag_sensor("heap_free_pct", "Free Memory %", "heap_free_pct", "%")
    diag_sensor("fs_free", "Filesystem Free", "fs_free", "B")
    diag_sensor("uptime", "Uptime", "uptime_s", "s", "duration",
                state_class="total_increasing")
    diag_sensor("rssi", "WiFi Signal", "rssi", "dBm", "signal_strength")
    diag_sensor("cpu_mhz", "CPU Frequency", "cpu_mhz", "MHz", state_class=None)
    diag_sensor("mcu_temp", "MCU Temperature", "mcu_temp_c", "°C", "temperature")
    diag_sensor("ip", "IP Address", "ip", state_class=None)
    diag_sensor("wifi_ssid", "WiFi Network", "ssid", state_class=None)
    diag_sensor("task_restarts", "Task Restarts", "task_restarts",
                state_class="total_increasing")
    diag_sensor("reconnects", "Reconnects", "reconnects",
                state_class="total_increasing")
    diag_sensor("wifi_assoc", "WiFi Associations", "wifi_assoc",
                state_class="total_increasing")
    diag_sensor("idf_free", "Internal RAM Free", "idf_free", "B")
    diag_sensor("idf_min_free", "Internal RAM Low-Water", "idf_min_free", "B")
    emit(_disc('sensor', 'reset_cause'), {
        "name": "Reset Cause", "unique_id": "reset_cause",
        "state_topic": MQTT_RESET_CAUSE_TOPIC.decode(),
        "entity_category": "diagnostic",
    })

    # --- mood tuning: a number per applicable knob + a reset button per mood,
    #     all in HA's collapsed Configuration section ---
    for m in sorted(_TUNE_KNOBS):
        title = _title(m)
        for k in _TUNE_KNOBS[m]:
            lo, hi, step, _dflt, lbl = _KNOB_SPEC[k]
            emit(_disc('number', 'tune_%s_%s' % (m, k)), {
                "name": "%s %s" % (title, lbl),
                "unique_id": "tune_%s_%s" % (m, k),
                "min": lo, "max": hi, "step": step, "mode": "slider",
                "state_topic": _tune_topic(m, k).decode(),
                "command_topic": (_tune_topic(m, k) + b'/set').decode(),
                "entity_category": "config",
                "availability_topic": MQTT_AVAILABILITY_TOPIC.decode(),
            })
        emit(_disc('button', 'tune_%s_reset' % m), {
            "name": "%s Reset" % title,
            "unique_id": "tune_%s_reset" % m,
            "command_topic": (_TUNE_PREFIX + m.encode() + b'/reset').decode(),
            "payload_press": "reset",
            "entity_category": "config",
            "availability_topic": MQTT_AVAILABILITY_TOPIC.decode(),
        })

    for topic in MQTT_ORPHANED_DISCOVERY_TOPICS:
        client.publish(topic, b'', retain=True)


async def connect_wifi():
    # The blocking calls here (wlan.connect, ntptime.settime inside sync_time)
    # do not yield; only the retry loop's wait does.
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    try:
        # best-effort mDNS: many routers/OSes then resolve <hostname>.local
        network.hostname(core.cfg["hostname"])
    except Exception:
        pass
    if not wlan.isconnected():
        core.log_always("[wifi] connecting to", core.cfg["wifi_ssid"])
        wlan.connect(core.cfg["wifi_ssid"], core.cfg["wifi_pass"])
        for _ in range(30):
            if wlan.isconnected():
                break
            await uasyncio.sleep_ms(1000)

    try:
        wlan.config(pm=wlan.PM_NONE)
        core.log_always("[wifi] power save disabled (PM_NONE)")
    except Exception as e:
        core.log_always("[wifi] could not set PM_NONE:", e)

    ip = wlan.ifconfig()[0] if wlan.isconnected() else "?"
    core.log_always("[wifi] connected:", wlan.isconnected(), " ip:", ip)
    if wlan.isconnected():
        core.net_generation += 1     # 'WiFi Associations' diagnostic counter
    core.sync_time()


def _hard_close(client):
    # umqtt.simple.disconnect() is `sock.write(...); sock.close()` — if the link
    # is already dead the write raises and close() never runs, leaking the fd.
    # MicroPython doesn't reliably close sockets on GC, so a night of reconnects
    # exhausts the ~8-16 socket pool. Force the close in its own guard.
    if client is None:
        return
    try:
        client.disconnect()
    except Exception:
        pass
    try:
        client.sock.close()
    except Exception:
        pass


async def connect_mqtt():
    # Client id = the droid's prefix. Same id on reconnect means the broker
    # cleanly takes over the old session (no stray Last-Will 'offline').
    client = MQTTClient(PREFIX, core.cfg["mqtt_broker"],
                        port=core.cfg["mqtt_port"], user=core.cfg["mqtt_user"],
                        password=core.cfg["mqtt_pass"])
    client.set_callback(on_mqtt_message)
    client.set_last_will(MQTT_AVAILABILITY_TOPIC, b'offline', retain=True, qos=0)
    for attempt in range(5):
        try:
            client.connect(timeout=10)
            core.log_always("[mqtt] connected on attempt", attempt + 1)
            return client
        except Exception as e:
            core.log_always("[mqtt] connect attempt", attempt + 1, "failed:", e)
            try:
                client.sock.close()      # umqtt leaves it open on a failed connect
            except Exception:
                pass
            await uasyncio.sleep_ms(2000)
    raise RuntimeError("Could not connect to MQTT after 5 attempts")


async def establish_link():
    _hard_close(core.conn["client"])    # free the old socket before making a new one
    core.conn["client"] = None

    await connect_wifi()
    client = await connect_mqtt()
    client.subscribe(MQTT_COMMAND_TOPIC)
    client.subscribe(MQTT_DEBUG_SET_TOPIC)
    client.subscribe(MQTT_SOUND_COMMAND_TOPIC)
    client.subscribe(MQTT_VOLUME_SET_TOPIC)
    client.subscribe(MQTT_DIAG_SET_TOPIC)
    client.subscribe(_TUNE_PREFIX + b'#')
    if ota.enabled():
        client.subscribe(MQTT_OTA_SET_TOPIC)
    core.conn["client"] = client
    core.link_state["down"] = False

    client.publish(MQTT_AVAILABILITY_TOPIC, b'online', retain=True)
    client.publish(MQTT_RESET_CAUSE_TOPIC, diag.RESET_CAUSE.encode(), retain=True)
    publish_discovery(client)
    publish_tune_state()
    if ota.enabled():
        ota._on_change = publish_ota_state
        publish_ota_state()
    core.log_always("[link] established, subscribed + discovery published")
    return client
