# Orchestrator: load config, wire the modules, run the task set.
# Entry point is /main.py -> app.main.run().

import os
import time
import machine
import uasyncio

# ---- Config / boot branch ----
# app.config always returns a usable dict now (offline defaults when there's no
# /config.json). WiFi/MQTT only run when cfg["network_enabled"]. The setup
# portal is reached on demand: buttons.button_task (hold Sound ~5s) or the
# factory-reset hold writes /portal.flag and resets; we run the blocking AP
# portal here, before the async app starts.
from app import config

cfg = config.load()

try:
    os.stat("/portal.flag")
    os.remove("/portal.flag")
    import app.provisioning as _provisioning
    _provisioning.run(config.device_id())      # blocks; machine.reset() on save
except OSError:
    pass

# core.init() must run before anything reads core.cfg. Import order below is
# deliberate: core first, then the feature modules (control before net/buttons).
from app import core
core.init(cfg)

from app import hw, sound, servo, leds, diag, ota, control, net, buttons, display

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
                    core.log_always("[reset] wiping config, rebooting into setup portal")
                    try:
                        sound.player.play_folder_track(6, 1)  # scream = "resetting"
                    except Exception:
                        pass
                    await uasyncio.sleep_ms(1500)
                    config.wipe()
                    try:
                        open("/portal.flag", "w").close()
                    except OSError:
                        pass
                    machine.reset()
                elif not warned and held >= 3000:
                    core.log_always("[reset] keep holding %ds to wipe config"
                                    % (RESET_HOLD_WIPE_MS // 1000))
                    warned = True
        else:
            held_since = None
        await uasyncio.sleep_ms(100)


async def main():
    net_on = bool(core.cfg.get("network_enabled"))
    mqtt = net_on and bool(core.cfg.get("mqtt_enabled"))

    if not net_on:
        core.log_always("[boot] offline — hold the Sound button ~5s for WiFi setup")
    elif mqtt:
        # A broker that's down at boot must not brick the board — flag the link
        # down and let connection_watchdog bring it up when the broker returns.
        try:
            await net.establish_link()
        except Exception as e:
            core.log_always("[boot] MQTT link failed, watchdog will retry:", e)
            core.link_state["down"] = True
    else:
        await net.connect_wifi()   # join WiFi + NTP; no broker

    # Reaching main() at all means every module imported — good enough as the
    # "the new build works" signal. Clear any OTA rollback state.
    ota.confirm()

    sound.player.select_tf_card()
    sound.player.set_volume(core.state["volume"])

    tasks = [
        core.supervise("servo_task", servo.servo_task),
        core.supervise("led_task", leds.led_task),
        core.supervise("busy_monitor_task", sound.busy_monitor_task),
        core.supervise("button_task", buttons.button_task),
        core.supervise("reset_button_task", reset_button_task),
        core.supervise("display_task", display.display_task),
        core.supervise("log_task", core.log_task),
    ]
    if mqtt:
        tasks += [
            core.supervise("mqtt_task", net.mqtt_task),
            core.supervise("state_publish_task", net.state_publish_task),
            core.supervise("heartbeat_task", net.heartbeat_task),
            core.supervise("diag_task", net.diag_task),
            core.supervise("connection_watchdog", net.connection_watchdog),
        ]
    if mqtt and ota.enabled():
        tasks.append(core.supervise("ota_boot_check", _ota_boot_check))
    await uasyncio.gather(*tasks)


async def _ota_boot_check():
    # one-shot: check the mirror for a newer version a few seconds after boot
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
