# DroidRefit NeoPixel bench test -- first-ever hardware run of the dome
# WS2812 string (see hardware/droidrefit-pcb/DESIGN.md and the "NeoPixel dome
# bring-up" plan). Standalone: no WiFi/MQTT, no app/ package, isolates the
# wiring + the stock `neopixel` module from app/leds.py's PixelChannel engine.
# No LEDs driven by anything else while this runs.
#
# Confirmed dome-board connector pinout (per the user, opposite of the PCB's
# JP1 shipped guess): pin1 GND, pin2 5V, pin3 Signal.
#
# Wiring for this bench run (see the bring-up plan for the "why"):
#   - ESP32 stays on its own USB (serial).
#   - Pixel V+ from an isolated 5V source (USB-to-2-wire lead), NOT the ESP32
#     rail -- keeps a bad pixel string from browning out the board under test.
#   - Pixel GND tied to BOTH the isolated 5V source's GND AND an ESP32 GND pin
#     -- common ground is mandatory, the data signal is referenced to it.
#   - Data: ESP32 GPIO5 -> (optional 330-470ohm series resistor) -> pixel DATA.
#     No level shifter this pass -- 3.3V direct, per app/leds.py's "usually
#     fine on a short bench run" note. If colors are wrong/flickery even at
#     low brightness, that's the shifter's job on the real PCB.
#   - GPIO5 is an ESP32 boot-strapping pin (internal pull-up) -- a brief flash
#     at power-on is normal, not a wiring fault.
import time
from machine import Pin
from neopixel import NeoPixel

print("[boot] starting NeoPixel test in 5s (Ctrl-C now to stay at the REPL instead)")
try:
    time.sleep(5)
except KeyboardInterrupt:
    print("[boot] startup interrupted -- test NOT started")
else:
    NEOPIXEL_PIN = 5   # hw.NEOPIXEL_PIN
    N = 4              # hw.NEOPIXEL_COUNT
    ROLES = ("holoprojector", "logic display", "rear circle", "light bar")

    # If colors come out wrong even at low brightness on the first pass, flip
    # this to 0 (400kHz -- what the retired CyberBrick firmware used) and
    # rerun. 1 = 800kHz WS2812/WS2812B, the MicroPython default.
    TIMING = 1

    np = NeoPixel(Pin(NEOPIXEL_PIN), N, timing=TIMING)
    print("[np] %d pixels on GPIO%d, timing=%d" % (N, NEOPIXEL_PIN, TIMING))

    OFF = (0, 0, 0)
    DIM_RED = (64, 0, 0)
    DIM_GREEN = (0, 64, 0)
    DIM_BLUE = (0, 0, 64)

    def show(colors):
        for i, c in enumerate(colors):
            np[i] = c
        np.write()

    def all_off():
        show([OFF] * N)

    def walk_test():
        # Confirms pixel count, data integrity, and the index<->physical
        # position mapping (index 0 should be the holoprojector, etc. --
        # app/leds.py's HOLO/LOGIC/REAR/BAR order, photo-verified on the
        # original CyberBrick dome but never checked on this wiring).
        print("\n=== walk test: one pixel at a time, dim white, 1s each ===")
        for i in range(N):
            all_off()
            np[i] = (30, 30, 30)
            np.write()
            print("[walk] pixel %d lit -- expect: %s" % (i, ROLES[i]))
            time.sleep(1)
        all_off()

    def color_order_test():
        # neopixel.NeoPixel takes (R,G,B) tuples and transmits GRB on the wire
        # (the WS2812 standard) -- app/leds.py's palette assumes this. If a
        # "red" command shows green (or blue), the string isn't standard GRB
        # and app/leds.py will need a bpp/reorder fix before it's usable.
        print("\n=== color order test: all 4 pixels, dim, 2s each ===")
        for name, c in (("RED", DIM_RED), ("GREEN", DIM_GREEN), ("BLUE", DIM_BLUE)):
            show([c] * N)
            print("[color] commanded %s -- pixels should look %s" % (name, name))
            time.sleep(2)
        all_off()

    def brightness_ramp():
        # All-white ramp -- the highest-current test here (~60mA/pixel at
        # full white, ~240mA for all 4). Watch the serial console for a
        # brownout/reset, not just the pixels; that's the actual point of
        # this step given the board's brownout history.
        print("\n=== brightness ramp: all white, 0->255 ===")
        for level in (0, 32, 64, 96, 128, 160, 192, 224, 255):
            show([(level, level, level)] * N)
            print("[ramp] white level =", level)
            time.sleep(1)
        all_off()

    print("[boot] test harness ready -- Ctrl-C at any time to stop and go dark")
    try:
        while True:
            walk_test()
            color_order_test()
            brightness_ramp()
            print("\n[boot] cycle complete, pausing 3s before repeating\n")
            time.sleep(3)
    except KeyboardInterrupt:
        pass
    finally:
        all_off()
        print("[boot] pixels off, test stopped")
