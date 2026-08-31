# DroidRefit servo reactions bench test -- cycles through named "reactions"
# (the same names as the CyberBrick's SERVO_REACTIONS in this project's
# main.py / PROJECT_NOTES.md), rewritten one at a time to use the smooth
# goto() ramp instead of the old raw duty_u16 stepping. No WiFi/MQTT here on
# purpose -- isolating just the servo, same as the plain sweep test this
# replaces. No LEDs wired to this board yet either, so standby/awake below
# are the servo-only skeleton of their original LED-synced behavior.
import time
import urandom
from machine import Pin, PWM

print("[boot] starting reaction cycle in 5s (Ctrl-C now to stay at the REPL instead)")
try:
    time.sleep(5)
except KeyboardInterrupt:
    print("[boot] startup interrupted -- reaction cycle NOT started")
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
    print("[boot] pausing 5s for head centering before reactions start...")
    time.sleep(5)

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
    # One function per named reaction (matching SERVO_REACTIONS' names),
    # rewritten one at a time. Each takes the current angle and how long to
    # run for, returns the final angle so the next reaction in the cycle
    # picks up smoothly from there.

    SWEEP_MIN = 1
    SWEEP_MAX = 179

    def sweep_reaction(name, angle, tick_ms, duration_s):
        # Shared by excited/surveillance/alert below -- same pattern, just
        # a different overall speed, expressed as a goto() duration derived
        # from the original raw tick_ms-per-degree rate.
        leg_ms = (SWEEP_MAX - SWEEP_MIN) * tick_ms
        print("[reaction] %s: full-range sweep (%dms/leg)" % (name, leg_ms))
        end = time.ticks_add(time.ticks_ms(), duration_s * 1000)
        while ticks_left(end):
            angle = goto(SWEEP_MAX, angle, leg_ms)
            angle = goto(SWEEP_MIN, angle, leg_ms)
        return angle

    def excited(angle, duration_s=6):
        # Original: raw 1-degree steps every 15ms.
        return sweep_reaction("excited", angle, 15, duration_s)

    def surveillance(angle, duration_s=6):
        # Original: raw 1-degree steps every 30ms (slower than excited).
        return sweep_reaction("surveillance", angle, 30, duration_s)

    def alert(angle, duration_s=6):
        # Original: raw 1-degree steps every 10ms (fastest of the sweeps).
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
        # Original: INSTANT snap to 145 (deliberately jarring -- "should
        # look like something broke"), then continuous small random
        # tremors (+/-6 deg) every 40ms. Per your note that the snap felt
        # too abrupt: the entry is now eased via goto() instead of an
        # instant jump. The trembling itself keeps similar character (still
        # quick/small) since that's presumably the intended "crash" look --
        # easy to gentle further from here: shrink JITTER_RANGE, or
        # lengthen the 80ms per-tremor duration below.
        BASE_ANGLE = 145
        JITTER_RANGE = 6
        print("[reaction] system_crash: easing into %d, then trembling" % BASE_ANGLE)
        angle = goto(BASE_ANGLE, angle, 600)  # was an instant jump -- now eased
        end = time.ticks_add(time.ticks_ms(), duration_s * 1000)
        while ticks_left(end):
            target = BASE_ANGLE + rand_between(-JITTER_RANGE, JITTER_RANGE)
            angle = goto(target, angle, 80)
        return angle

    def wander_reaction(name, angle, pause_min_ms, pause_max_ms, duration_s):
        # Original: slow deliberate glide to a random point (>=30 deg away
        # from current), then a real pause -- production pause is minutes
        # long (see callers below), scaled way down here just so the bench
        # cycle doesn't sit idle that whole time.
        MIN_TRAVEL = 30
        print("[reaction] %s: wander" % name)
        end = time.ticks_add(time.ticks_ms(), duration_s * 1000)
        while ticks_left(end):
            target = rand_between(SWEEP_MIN, SWEEP_MAX)
            attempts = 0
            while abs(target - angle) < MIN_TRAVEL and attempts < 10:
                target = rand_between(SWEEP_MIN, SWEEP_MAX)
                attempts += 1
            travel_ms = max(abs(target - angle) * 40, 300)  # ~1deg/40ms pace, same as original
            print("[reaction] %s: moving to %d" % (name, target))
            angle = goto(target, angle, travel_ms)
            time.sleep_ms(rand_between(pause_min_ms, pause_max_ms))
        return angle

    def awake(angle, duration_s=15):
        # Production pause: 120000-300000ms (2-5 min). Scaled to 3-6s here.
        return wander_reaction("awake", angle, 3000, 6000, duration_s)

    def standby(angle, duration_s=15):
        # Original standby_cycle: home to 90, idle for a long random
        # interval (production: 180000-300000ms), glide to a random point
        # (>=30 deg away), hold 5000ms, "suspend" (LEDs dim -- no LEDs on
        # this bench board, skipped) for 4000ms, back to idle, repeat.
        # Idle/hold/suspend all scaled down here for the bench cycle.
        IDLE_MIN_MS, IDLE_MAX_MS = 2000, 4000    # production: 180000-300000
        ACTIVE_HOLD_MS = 1500                     # production: 5000
        SUSPEND_MS = 1000                         # production: 4000
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

    # Registered (name, function) pairs, in the same order as the table in
    # PROJECT_NOTES.md.
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
