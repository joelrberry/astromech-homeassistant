# Orchestrator: load config, wire the modules, run the task set.
# Entry point is /main.py -> app.main.run().

import time
import machine
import uasyncio

# ---- Config / boot branch ----
# app.config reads /config.json (written by the first-run setup portal,
# app/provisioning.py), falling back to a bench app/config_baked.py — see
# config_baked.py.example. An unconfigured board (no config.json, no baked)
# drops into the portal here and never returns (it ends in machine.reset()).
from app import config

cfg = config.load()
if cfg is None:
    try:
        import app.provisioning as provisioning
    except ImportError:
        provisioning = None
    if provisioning is not None:
        provisioning.run(config.device_id())   # blocks; machine.reset() on save
    raise RuntimeError("unconfigured: no /config.json and no app/config_baked.py")

# core.init() must run before anything reads core.cfg (net reads it at connect
# time). Import order below is deliberate: core first, then the feature modules.
from app import core
core.init(cfg)

from app import hw, sound, servo, leds, diag, ota, net, webui

diag.log_boot()   # reset cause + free heap, first line after config


# ---- Factory-reset button ----
# The devkit's BOOT button is on GPIO0. Held LOW for RESET_HOLD_WIPE_MS while
# the firmware runs -> wipe /config.json and reboot into the setup portal, no
# password needed. GPIO0 is a strapping pin, so this is a while-running gesture
# only — a hold-at-boot would drop the ROM into serial-download mode instead.
RESET_HOLD_WIPE_MS = 10000


async def reset_button_task():
    held_since = None
    warned = False
    while True:
        if hw.reset_btn.value() == 0:  # active-low: pressed
            now = time.ticks_ms()
            if held_since is None:
                held_since = now
                warned = False
            else:
                held = time.ticks_diff(now, held_since)
                if held >= RESET_HOLD_WIPE_MS:
                    core.log_always("[reset] wiping config, rebooting to setup portal")
                    try:
                        sound.player.play_folder_track(6, 1)  # scream = "resetting"
                    except Exception:
                        pass
                    await uasyncio.sleep_ms(1500)
                    config.wipe()
                    machine.reset()
                elif not warned and held >= 3000:
                    core.log_always("[reset] keep holding %ds to wipe config"
                                    % (RESET_HOLD_WIPE_MS // 1000))
                    warned = True
        else:
            held_since = None
        await uasyncio.sleep_ms(100)


async def main():
    mqtt = core.cfg["mqtt_enabled"]

    if mqtt:
        # A broker that's down at boot must not brick the board — flag the link
        # down and let connection_watchdog bring it up when the broker returns.
        try:
            await net.establish_link()
        except Exception as e:
            core.log_always("[boot] MQTT link failed, watchdog will retry:", e)
            core.link_state["down"] = True
    else:
        await net.connect_wifi()   # join WiFi + NTP; no broker

    # Getting here means every app module imported and the link came up — a
    # good-enough "the new build works" signal. Clear the OTA rollback state.
    ota.confirm()

    sound.player.select_tf_card()
    sound.player.set_volume(core.state["volume"])

    tasks = [
        core.supervise("servo_task", servo.servo_task),
        core.supervise("led_task", leds.led_task),
        core.supervise("busy_monitor_task", sound.busy_monitor_task),
        core.supervise("reset_button_task", reset_button_task),
        core.supervise("log_task", core.log_task),
        core.supervise("web_server_task", webui.web_server_task),
    ]
    if mqtt:
        tasks += [
            core.supervise("mqtt_task", net.mqtt_task),
            core.supervise("state_publish_task", net.state_publish_task),
            core.supervise("heartbeat_task", net.heartbeat_task),
            core.supervise("diag_task", net.diag_task),
            core.supervise("connection_watchdog", net.connection_watchdog),
        ]
    tasks.append(core.supervise("ota_boot_check", _ota_boot_check))
    await uasyncio.gather(*tasks)


async def _ota_boot_check():
    # one-shot: check GitHub for a newer version a few seconds after boot
    # (establish_link already wired ota._on_change when MQTT is on). Never
    # auto-installs. Then idle forever — supervise keeps the coro alive.
    await uasyncio.sleep_ms(8000)
    try:
        await ota.check()
    except Exception as e:
        core.dbg("[ota] boot check failed:", e)
    while True:
        await uasyncio.sleep_ms(3600000)


def run():
    uasyncio.run(main())
