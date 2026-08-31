# Runtime configuration store for the droidrefit firmware.
#
# The live config is /config.json at the filesystem root, written by the
# first-run setup portal (app/provisioning.py). This module loads / saves /
# wipes it. For bench work you can skip the portal: create app/config_baked.py
# (gitignored, see config_baked.py.example) and load() falls back to it
# whenever /config.json is absent.

try:
    import ujson as json
except ImportError:
    import json
try:
    import uos as os
except ImportError:
    import os
try:
    import network
except ImportError:
    network = None

CONFIG_PATH = "/config.json"

# Every key the firmware may read, with a safe default. load() and save() both
# normalise against this, so a partial or hand-edited file can't crash boot.
_DEFAULTS = {
    "wifi_ssid": "",
    "wifi_pass": "",
    "device_name": "R2-D2",
    "device_id": "",
    "hostname": "",
    "mqtt_enabled": False,
    "mqtt_broker": "",
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_pass": "",
    "topic_prefix": "",
    "web_pin": "",
    "ota_url": "",   # HTTP mirror base for OTA, e.g. http://192.168.1.50:8000/droidrefit
                     # (empty -> pull from GitHub via mip; required on classic ESP32)
    "leds_enabled": True,   # False on a unit with no WS2812 pixels wired — the
                            # NeoPixel/RMT write path is skipped entirely
    "buzzer_enabled": True, # False on a unit with no piezo on hw.BUZZER_PIN
    "network_enabled": False,  # offline-first: WiFi/MQTT only run when this is
                               # True. The setup portal sets it. Reach the portal
                               # with the Sound-button hold or the factory reset.
    "configured": False,
}


def device_id():
    """Stable per-board id from the STA MAC, e.g. 'r2d2-a1b2c3'."""
    if network is None:
        return "r2d2-000000"
    try:
        wlan = network.WLAN(network.STA_IF)
        try:
            mac = wlan.config("mac")
        except Exception:
            wlan.active(True)
            mac = wlan.config("mac")
        return "r2d2-%02x%02x%02x" % (mac[3], mac[4], mac[5])
    except Exception:
        return "r2d2-000000"


def slug(s):
    """Lowercase a-z0-9 and single dashes; safe for a hostname / topic prefix."""
    out = ""
    for ch in str(s).lower():
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            out += ch
        elif ch in " -_" and out and out[-1] != "-":
            out += "-"
    return out.strip("-")


def _normalise(d):
    cfg = dict(_DEFAULTS)
    if d:
        for k in _DEFAULTS:
            if d.get(k) is not None:
                cfg[k] = d[k]
    if not cfg["device_id"]:
        cfg["device_id"] = device_id()
    # topic_prefix is the per-droid MQTT identity: it's the MQTT client id, the
    # root of the r2d2-style topic tree, every Home Assistant unique_id, and the
    # HA device identifier. It MUST be unique per droid on a shared broker.
    # Default: a slug of the droid name, else the MAC-derived id. A returning
    # single-unit user sets it to "r2d2" to keep their existing entities/topics.
    if not cfg["topic_prefix"]:
        cfg["topic_prefix"] = slug(cfg["device_name"]) or slug(cfg["device_id"]) or "r2d2"
    else:
        cfg["topic_prefix"] = slug(cfg["topic_prefix"]) or "r2d2"
    if not cfg["hostname"]:
        cfg["hostname"] = slug(cfg["device_name"]) or cfg["topic_prefix"]
    try:
        cfg["mqtt_port"] = int(cfg["mqtt_port"])
    except (ValueError, TypeError):
        cfg["mqtt_port"] = 1883
    cfg["mqtt_enabled"] = bool(cfg["mqtt_enabled"])
    cfg["network_enabled"] = bool(cfg["network_enabled"])
    cfg["leds_enabled"] = bool(cfg["leds_enabled"])
    cfg["buzzer_enabled"] = bool(cfg["buzzer_enabled"])
    cfg["configured"] = bool(cfg["configured"])
    return cfg


def _read_json():
    for path in (CONFIG_PATH, CONFIG_PATH + ".tmp"):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            continue
    return None


def _read_baked():
    try:
        from app import config_baked as _baked
    except ImportError:
        return None
    if hasattr(_baked, "CONFIG"):
        d = dict(_baked.CONFIG)
    else:
        # legacy flat constants
        names = {
            "WIFI_SSID": "wifi_ssid", "WIFI_PASSWORD": "wifi_pass",
            "MQTT_BROKER": "mqtt_broker", "MQTT_PORT": "mqtt_port",
            "MQTT_USER": "mqtt_user", "MQTT_PASSWORD": "mqtt_pass",
            "MQTT_CLIENT_ID": "device_id",
        }
        d = {}
        for old, new in names.items():
            if hasattr(_baked, old):
                d[new] = getattr(_baked, old)
    if not d:
        return None
    d.setdefault("configured", True)
    if "mqtt_enabled" not in d:
        d["mqtt_enabled"] = bool(d.get("mqtt_broker"))
    # A bench config's MQTT client id is also its topic prefix, so the bench
    # board stays on the same topic tree / HA entities as before.
    if d.get("device_id") and "topic_prefix" not in d:
        d["topic_prefix"] = d["device_id"]
    return d


def load():
    """Always a normalised config dict.

    Precedence: /config.json -> app/config_baked.py (bench) -> offline defaults.
    Offline-first: a missing or half-written config is just an offline config
    (network_enabled stays False). WiFi/MQTT are gated on network_enabled, not
    on the presence of a config — so this never returns None and the board
    always boots into the app.
    """
    raw = _read_json()
    if raw is None:
        raw = _read_baked()
    return _normalise(raw or {})


def save(updates):
    """Merge `updates` into the current config, normalise, atomically write."""
    current = _read_json() or {}
    current.update(updates)
    cfg = _normalise(current)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f)
    try:
        os.remove(CONFIG_PATH)
    except OSError:
        pass
    os.rename(tmp, CONFIG_PATH)
    return cfg


def wipe():
    """Delete config.json (+ any stale tmp) — next boot enters provisioning."""
    for p in (CONFIG_PATH, CONFIG_PATH + ".tmp"):
        try:
            os.remove(p)
        except OSError:
            pass
