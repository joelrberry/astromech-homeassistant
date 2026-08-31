# WiFi + MQTT + Home Assistant discovery, and the command entry points
# (apply_mode / apply_sound / apply_volume) shared by MQTT and the web panel.
#
# deps: app.core, app.sound, app.servo, app.diag

import network
import ujson
import uasyncio

from umqtt.simple import MQTTClient
from app import core, sound, servo, diag, ota

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


# ---- command entry points (MQTT + web panel both call these) ----
def apply_mode(mode):
    if mode not in servo.SERVO_BEHAVIORS:
        return False
    core.dbg("[mqtt] switching to mode:", mode)
    core.state["mode"] = mode
    return True


def apply_sound(name):
    if name not in sound.SOUND_FOLDERS:
        return False
    core.state["sound"] = name
    sound.play_random_in_folder(name)
    return True


def apply_volume(level):
    try:
        level = int(level)
    except (ValueError, TypeError):
        return False
    level = max(0, min(30, level))
    core.dbg("[mqtt] setting volume to", level)
    core.state["volume"] = level
    sound.player.set_volume(level)
    return True


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
        apply_volume(level)
        return

    if topic == MQTT_OTA_SET_TOPIC and ota.enabled():
        cmd = msg.strip().lower()
        if cmd in (b'install', b'update'):
            uasyncio.create_task(ota.update())
        else:
            uasyncio.create_task(ota.check())
        return

    core.dbg("[mqtt] message on", topic, "->", msg)
    try:
        payload = ujson.loads(msg)
    except Exception as e:
        core.dbg("[mqtt] bad json:", msg, e)
        return

    if topic == MQTT_COMMAND_TOPIC:
        if not apply_mode(payload.get("mode")):
            core.dbg("[mqtt] unknown/missing mode in payload:", payload)
    elif topic == MQTT_SOUND_COMMAND_TOPIC:
        if not apply_sound(payload.get("sound")):
            core.dbg("[mqtt] unknown/missing sound in payload:", payload)


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
    emit(_disc('sensor', 'reset_cause'), {
        "name": "Reset Cause", "unique_id": "reset_cause",
        "state_topic": MQTT_RESET_CAUSE_TOPIC.decode(),
        "entity_category": "diagnostic",
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
    core.log_always("[wifi] connected:", wlan.isconnected(), "  control page: http://%s/  (http://%s.local/)"
                    % (ip, core.cfg["hostname"]))
    if wlan.isconnected():
        core.net_generation += 1
    core.sync_time()


async def wifi_monitor_task():
    # The ESP32 auto-reconnects WiFi on its own, often without connection_watchdog
    # ever calling connect_wifi(). Catch that False->True edge too so webui can
    # rebuild a listener that the drop left stale.
    wlan = network.WLAN(network.STA_IF)
    was = wlan.isconnected()
    while True:
        await uasyncio.sleep_ms(2000)
        now = wlan.isconnected()
        if now and not was:
            core.net_generation += 1
            core.log_always("[wifi] reassociated (net gen %d)" % core.net_generation)
        was = now


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
    if ota.enabled():
        client.subscribe(MQTT_OTA_SET_TOPIC)
    core.conn["client"] = client
    core.link_state["down"] = False

    client.publish(MQTT_AVAILABILITY_TOPIC, b'online', retain=True)
    client.publish(MQTT_RESET_CAUSE_TOPIC, diag.RESET_CAUSE.encode(), retain=True)
    publish_discovery(client)
    if ota.enabled():
        ota._on_change = publish_ota_state
        publish_ota_state()
    core.log_always("[link] established, subscribed + discovery published")
    return client
