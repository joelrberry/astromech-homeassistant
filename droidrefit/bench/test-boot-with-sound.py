# DroidRefit combined bench test: servo reactions + DFPlayer, on one board,
# sharing the external 5V rail (servo and DFPlayer both wired directly to
# the rail now, not through the ESP32's own pins -- see droidrefit.md).
# No WiFi/MQTT yet on purpose -- this pass only proves servo + DFPlayer work
# together on shared power, same incremental approach as everything else in
# this project. The servo-only version of this test is preserved as
# test-boot.py.
import time
import urandom
from machine import Pin, PWM, UART
from dfplayer import DFPlayer

print("[boot] starting in 5s (Ctrl-C now to stay at the REPL instead)")
try:
    time.sleep(5)
except KeyboardInterrupt:
    print("[boot] startup interrupted -- nothing started")
else:
    SERVO_PIN = 18   # per droidrefit.md's pinout plan
    FREQ = 50        # standard hobby-servo PWM rate

    # Standard hobby-servo pulse-width convention: ~500us (0 deg) to
    # ~2500us (180 deg) over a 20ms period at 50Hz -- a starting point, not
    # a verified calibration. Treat what you actually observe as ground
    # truth, not this formula.
    MIN_US = 500
    MAX_US = 2500
    PERIOD_US = 1_000_000 // FREQ

    def angle_to_duty_u16(angle):
        pulse_us = MIN_US + (angle / 180) * (MAX_US - MIN_US)
        return int(pulse_us / PERIOD_US * 65535)

    # Initialize directly at 90 -- a definition, not a guess: it's the first
    # thing we ever command, so our tracked `angle` and the real PWM output
    # are in sync from this line on, no persistence/homing dance needed.
    angle = 90
    servo = PWM(Pin(SERVO_PIN), freq=FREQ, duty_u16=angle_to_duty_u16(angle))
    print("[servo] initialized at 90 on GPIO%d" % SERVO_PIN)

    # Servo is now actively holding 90 (electrical center) -- pause here so
    # the dome head can be attached/adjusted on the gear while it's at a
    # known reference position, before anything starts moving.
    print("[servo] >>> CENTER THE HEAD NOW -- servo is holding 90 degrees <<<")
    print("[boot] pausing 5s for head centering before anything starts...")
    time.sleep(5)

    # ---- DFPlayer ----
    # UART2 (GPIO17=TX, GPIO16=RX) + Busy on GPIO4 -- confirmed working
    # pins from the sound-satellite bring-up, now wired to this board
    # instead, drawing power from the shared 5V rail rather than a separate
    # USB-C supply. See r2d2audio.md/droidrefit.md for the full history.
    DFPLAYER_UART_ID = 2
    DFPLAYER_TX_PIN = 17
    DFPLAYER_RX_PIN = 16

    dfplayer_uart = UART(DFPLAYER_UART_ID, baudrate=9600,
                          tx=Pin(DFPLAYER_TX_PIN), rx=Pin(DFPLAYER_RX_PIN))
    player = DFPlayer(dfplayer_uart, log=print)
    print("[dfplayer] UART initialized (TX=GPIO%d, RX=GPIO%d)" %
          (DFPLAYER_TX_PIN, DFPLAYER_RX_PIN))

    # A moment to let the module finish its own power-on/SD-card-read
    # sequence before we talk to it -- same lesson as the original bench
    # test in r2d2audio.md.
    time.sleep_ms(1500)
    player.select_tf_card()
    player.set_volume(7)  # kept quiet for now while bench testing

    print("[dfplayer] playing confirmation sound (folder 01, track 001) "
          "to verify the link works on shared rail power")
    player.play_folder_track(1, 1)

    # A servo only samples its commanded position once per PWM period (20ms
    # at 50Hz) -- the finest tick spacing that means anything physically.
    TICK_MS = 20

    def goto(target_angle, current_angle, duration_ms, ease=True):
        # Specify a TIME instead of a step size. `ease=True` applies a
        # smoothstep curve (accelerate into the move, decelerate out of it)
        # instead of constant-speed linear stepping.
        steps = max(1, duration_ms // TICK_MS)
        delta = target_angle - current_angle
        for i in range(1, steps + 1):
            t = i / steps
            if ease:
                t = t * t * (3 - 2 * t)  # smoothstep
            servo.duty_u16(angle_to_duty_u16(current_angle + delta * t))
            time.sleep_ms(TICK_MS)
        servo.duty_u16(angle_to_duty_u16(target_angle))
        return target_angle

    def rand_between(lo, hi):
        return lo + (urandom.getrandbits(8) % (hi - lo + 1))

    def ticks_left(end):
        return time.ticks_diff(end, time.ticks_ms()) > 0

    # ---- Reactions ----
    # Same 8 reactions as test-boot.py, unmodified -- this pass verifies
    # servo + DFPlayer work together on shared power, not that their
    # behaviors are coupled yet (that's a later step).

    SWEEP_MIN = 1
    SWEEP_MAX = 179

    def sweep_reaction(name, angle, tick_ms, duration_s):
        leg_ms = (SWEEP_MAX - SWEEP_MIN) * tick_ms
        print("[reaction] %s: full-range sweep (%dms/leg)" % (name, leg_ms))
        end = time.ticks_add(time.ticks_ms(), duration_s * 1000)
        while ticks_left(end):
            angle = goto(SWEEP_MAX, angle, leg_ms)
            angle = goto(SWEEP_MIN, angle, leg_ms)
        return angle

    def excited(angle, duration_s=6):
        return sweep_reaction("excited", angle, 15, duration_s)

    def surveillance(angle, duration_s=6):
        return sweep_reaction("surveillance", angle, 30, duration_s)

    def alert(angle, duration_s=6):
        return sweep_reaction("alert", angle, 10, duration_s)

    def center_reaction(name, angle, duration_s):
        print("[reaction] %s: center and hold" % name)
        angle = goto(90, angle, 800)
        time.sleep(duration_s)
        return angle

    def sleep(angle, duration_s=3):
        return center_reaction("sleep", angle, duration_s)

    def hologram(angle, duration_s=3):
        return center_reaction("hologram", angle, duration_s)

    def system_crash(angle, duration_s=6):
        BASE_ANGLE = 145
        JITTER_RANGE = 6
        # Folder 06 = "scream" (per r2d2audio.md's sound library table) --
        # the best fit of what's loaded for an "error"/something-wrong cue.
        # 3 tracks in that folder, pick one at random.
        SCREAM_FOLDER = 6
        SCREAM_TRACKS = 3
        print("[reaction] system_crash: easing into %d, then trembling" % BASE_ANGLE)
        player.play_folder_track(SCREAM_FOLDER, rand_between(1, SCREAM_TRACKS))
        angle = goto(BASE_ANGLE, angle, 600)
        end = time.ticks_add(time.ticks_ms(), duration_s * 1000)
        while ticks_left(end):
            target = BASE_ANGLE + rand_between(-JITTER_RANGE, JITTER_RANGE)
            angle = goto(target, angle, 80)
        return angle

    def wander_reaction(name, angle, pause_min_ms, pause_max_ms, duration_s):
        MIN_TRAVEL = 30
        print("[reaction] %s: wander" % name)
        end = time.ticks_add(time.ticks_ms(), duration_s * 1000)
        while ticks_left(end):
            target = rand_between(SWEEP_MIN, SWEEP_MAX)
            attempts = 0
            while abs(target - angle) < MIN_TRAVEL and attempts < 10:
                target = rand_between(SWEEP_MIN, SWEEP_MAX)
                attempts += 1
            travel_ms = max(abs(target - angle) * 40, 300)
            print("[reaction] %s: moving to %d" % (name, target))
            angle = goto(target, angle, travel_ms)
            time.sleep_ms(rand_between(pause_min_ms, pause_max_ms))
        return angle

    def awake(angle, duration_s=15):
        return wander_reaction("awake", angle, 3000, 6000, duration_s)

    def standby(angle, duration_s=15):
        IDLE_MIN_MS, IDLE_MAX_MS = 2000, 4000
        ACTIVE_HOLD_MS = 1500
        SUSPEND_MS = 1000
        MIN_TRAVEL = 30
        print("[reaction] standby: homing to 90")
        angle = goto(90, angle, 1500)
        end = time.ticks_add(time.ticks_ms(), duration_s * 1000)
        while ticks_left(end):
            print("[reaction] standby: idle")
            time.sleep_ms(rand_between(IDLE_MIN_MS, IDLE_MAX_MS))
            target = rand_between(SWEEP_MIN, SWEEP_MAX)
            attempts = 0
            while abs(target - angle) < MIN_TRAVEL and attempts < 10:
                target = rand_between(SWEEP_MIN, SWEEP_MAX)
                attempts += 1
            print("[reaction] standby: waking, moving to", target)
            angle = goto(target, angle, max(abs(target - angle) * 40, 300))
            time.sleep_ms(ACTIVE_HOLD_MS)
            print("[reaction] standby: suspending")
            time.sleep_ms(SUSPEND_MS)
        return angle

    REACTIONS = [
        ("standby", standby),
        ("awake", awake),
        ("excited", excited),
        ("surveillance", surveillance),
        ("alert", alert),
        ("sleep", sleep),
        ("system_crash", system_crash),
        ("hologram", hologram),
    ]

    print("[boot] reaction harness ready -- %d reaction(s) registered" % len(REACTIONS))

    while True:
        for name, fn in REACTIONS:
            print("\n=== running reaction: %s ===" % name)
            angle = fn(angle)
            print("=== finished reaction: %s (angle=%d) ===" % (name, angle))
            time.sleep(1)
