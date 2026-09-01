# Servo motion: one rate-limited driver, modes as setpoint generators.
#
# servo_task is the ONLY thing that touches the servo. Each tick it asks the
# current behaviour "where should the head be?" and eases `pos` toward that
# under SERVO_MAX_ACCEL / per-behaviour speed limits. Because `pos` can only
# change by vel*dt and `vel` by accel*dt, the PWM output can never jump or
# spike — so switching modes mid-move is just a smooth setpoint redirect, no
# cancellation, no position/velocity discontinuity. Behaviours are plain state
# machines: target(now, pos) -> (setpoint_deg, max_speed_deg_s); recreated on
# every mode change.
#
# deps: app.core, app.sound, app.hw

import math
import time
import machine
import uasyncio

from app import core, sound, hw

# --- PWM signal params ---
MIN_US = 500
MAX_US = 2500
PERIOD_US = 1_000_000 // hw.SERVO_FREQ

SWEEP_MIN = 1
SWEEP_MAX = 179

# A servo samples its commanded position once per PWM period (20ms @ 50Hz) —
# the finest tick that means anything physically.
TICK_MS = 20
TICK_S = TICK_MS / 1000

# --- motion envelope ---
SERVO_MIN_ANGLE = 0
SERVO_MAX_ANGLE = 180
SERVO_MAX_ACCEL = 1200   # deg/s^2 — accel + decel ramp; main "snappy vs jerky" knob
ARRIVE_EPS = 1.0         # deg — "close enough" to count as arrived and stop

# Per-behaviour cruise speeds (deg/s). Old sweeps were ms/deg:
# alert 10 -> 100, surveillance 30 -> 33, wander 40 -> 25.
SPEED_ALERT = 100
SPEED_SURVEIL = 33
SPEED_WANDER = 25
SPEED_TREMBLE = 200
SPEED_SETTLE = 90


def angle_to_duty_u16(angle):
    pulse_us = MIN_US + (angle / 180) * (MAX_US - MIN_US)
    return int(pulse_us / PERIOD_US * 65535)


# Built here (not in app.hw) so it starts already centred at 90 — no duty-0
# twitch. core.servo_state["angle"] is the matching software estimate.
servo = machine.PWM(machine.Pin(hw.SERVO_PIN), freq=hw.SERVO_FREQ,
                    duty_u16=angle_to_duty_u16(90))


def _clamp(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


def _approach(pos, vel, setpoint, max_vel, max_accel, dt):
    # Trapezoidal-velocity move toward `setpoint`: ramp `vel` toward the fastest
    # speed from which we can still brake to a stop exactly on arrival
    # (v = sqrt(2*a*d)), accel-limited both ways. Snaps to the target on the
    # final tick so discrete stepping can't overshoot. `max_accel` is per-move
    # (a behaviour can crack harder than the SERVO_MAX_ACCEL default).
    err = setpoint - pos
    dist = abs(err)
    if dist < ARRIVE_EPS and abs(vel) < ARRIVE_EPS:
        return setpoint, 0.0
    v_brake = math.sqrt(2 * max_accel * dist)
    v_cap = v_brake if v_brake < max_vel else max_vel
    v_des = v_cap if err > 0 else -v_cap
    dv_max = max_accel * dt
    dv = _clamp(v_des - vel, -dv_max, dv_max)
    vel += dv
    step = vel * dt
    if abs(step) >= dist:
        return setpoint, 0.0
    return pos + step, vel


def _pick_target(pos, min_travel=30):
    # A random angle in the sweep range at least `min_travel` deg from `pos`,
    # so a "wander" move is always a visible one. Bounded retry, then give up.
    target = core.rand_between(SWEEP_MIN, SWEEP_MAX)
    for _ in range(10):
        if abs(target - pos) >= min_travel:
            break
        target = core.rand_between(SWEEP_MIN, SWEEP_MAX)
    return target


class _Hold:
    # Ease to a fixed angle and stay. Modes: sleep, hologram.
    def __init__(self, angle, speed=SPEED_SETTLE):
        self.angle = angle
        self.speed = speed

    def target(self, now, pos):
        return self.angle, self.speed


class _Dart:
    # Snappy darts to random spots with a pause between; `double_pct` chance of
    # an immediate second dart ("double-take") before the next pause. Returns a
    # 3-tuple so it can crack harder than the default accel. Modes: alert
    # (measured), excited (frantic).
    def __init__(self, speed, accel, wait_min, wait_max, double_pct,
                 name="dart", min_travel=45):
        self.speed = speed
        self.accel = accel
        self.wait_min = wait_min
        self.wait_max = wait_max
        self.double_pct = double_pct
        self.name = name
        self.min_travel = min_travel
        self.goal = None
        self.phase = "wait"       # wait -> dart -> [dart again] -> wait ...
        self.until = None
        self.doubles = 0

    def target(self, now, pos):
        if self.goal is None:
            self.goal = pos
        if self.phase == "wait":
            if self.until is None:
                self.until = time.ticks_add(
                    now, core.rand_ms(self.wait_min, self.wait_max))
            elif time.ticks_diff(now, self.until) >= 0:
                self.goal = _pick_target(pos, self.min_travel)
                self.phase, self.until, self.doubles = "dart", None, 0
                core.dbg("[servo]", self.name, "dart ->", self.goal)
        elif self.phase == "dart":
            if abs(pos - self.goal) < ARRIVE_EPS:
                if self.doubles < 2 and core.rand_between(0, 99) < self.double_pct:
                    self.doubles += 1
                    self.goal = _pick_target(pos, 22)
                    core.dbg("[servo]", self.name, "double-take ->", self.goal)
                else:
                    self.phase, self.until = "wait", None
        return self.goal, self.speed, self.accel


class _Sweep:
    # Oscillate lo<->hi forever at a fixed speed. Flips a hair before the end
    # so the turnaround rounds off instead of stopping dead. Mode: surveillance.
    def __init__(self, lo, hi, speed):
        self.lo = lo
        self.hi = hi
        self.speed = speed
        self.goal = hi

    def target(self, now, pos):
        if abs(pos - self.goal) < 2.0:
            self.goal = self.lo if self.goal == self.hi else self.hi
            core.dbg("[servo] sweep ->", self.goal)
        return self.goal, self.speed


class _Wander:
    # phases: [move->home once, if given] -> wait(rand) -> move(random target)
    #         -> [hold_ms] -> [suspend_ms] -> wait ...
    # awake = no home / hold / suspend; standby homes to 90 and adds both waits.
    def __init__(self, wait_min, wait_max, speed,
                 hold_ms=0, suspend_ms=0, home=None):
        self.wait_min = wait_min
        self.wait_max = wait_max
        self.speed = speed
        self.hold_ms = hold_ms
        self.suspend_ms = suspend_ms
        self.until = None
        if home is None:
            self.goal = None
            self.phase = "wait"
        else:
            self.goal = home
            self.phase = "home"
            core.dbg("[servo] wander: home -> %d deg" % home)

    def _go(self, phase, now=0, ms=0, detail=""):
        self.phase = phase
        self.until = time.ticks_add(now, ms) if ms else None
        if phase != "wait":
            core.dbg("[servo] wander:", phase,
                     detail or (("%dms" % ms) if ms else ""))

    def _after_move(self, now):
        if self.hold_ms:
            self._go("hold", now, self.hold_ms)
        elif self.suspend_ms:
            self._go("suspend", now, self.suspend_ms)
        else:
            self._go("wait", now)

    def target(self, now, pos):
        if self.goal is None:
            self.goal = pos

        if self.phase == "home":
            if abs(pos - self.goal) < ARRIVE_EPS:
                self._go("wait", now)
        elif self.phase == "wait":
            if self.until is None:
                w = core.rand_ms(self.wait_min, self.wait_max)
                self.until = time.ticks_add(now, w)
                core.dbg("[servo] wander: idle %dms" % w)
            elif time.ticks_diff(now, self.until) >= 0:
                self.goal = _pick_target(pos)
                self._go("move", now, detail="-> %d deg" % self.goal)
        elif self.phase == "move":
            if abs(pos - self.goal) < ARRIVE_EPS:
                self._after_move(now)
        elif self.phase == "hold":
            if time.ticks_diff(now, self.until) >= 0:
                if self.suspend_ms:
                    self._go("suspend", now, self.suspend_ms)
                else:
                    self._go("wait", now)
        elif self.phase == "suspend":
            if time.ticks_diff(now, self.until) >= 0:
                self._go("wait", now)

        return self.goal, self.speed


class _Tremble:
    # Jitter around a base angle: repick base +/- jitter every ~90ms or on
    # arrival. Mode: system_crash (the scream is a SERVO_ON_ENTER hook).
    def __init__(self, base, jitter, speed):
        self.base = base
        self.jitter = jitter
        self.speed = speed
        self.goal = base
        self.next = None

    def target(self, now, pos):
        if (self.next is None or time.ticks_diff(now, self.next) >= 0
                or abs(pos - self.goal) < 1.5):
            self.goal = self.base + core.rand_between(-self.jitter, self.jitter)
            self.next = time.ticks_add(now, 90)
        return self.goal, self.speed


SERVO_BEHAVIORS = {  # mode -> zero-arg factory (fresh state machine per switch)
    "standby": lambda: _Wander(180000, 300000, SPEED_WANDER,
                               hold_ms=5000, suspend_ms=4000, home=90),
    "awake": lambda: _Wander(120000, 300000, SPEED_WANDER),
    # alert = the measured dart-and-pause; excited = a faster, twitchier version
    "excited": lambda: _Dart(260, 3400, 500, 1400, 70, name="excited", min_travel=30),
    "surveillance": lambda: _Sweep(SWEEP_MIN, SWEEP_MAX, SPEED_SURVEIL),
    "alert": lambda: _Dart(150, 2200, 1500, 3500, 40, name="alert"),
    "sleep": lambda: _Hold(90),
    "hologram": lambda: _Hold(90),
    "system_crash": lambda: _Tremble(145, 6, SPEED_TREMBLE),
}

SERVO_ON_ENTER = {  # one-shot side effects fired once when a mode is entered
    # Folder 06 = "scream" (r2d2audio.md sound table), 3 tracks — the "error" cue.
    "system_crash": lambda: sound.player.play_folder_track(
        6, core.rand_between(1, 3)),
}

SERVO_MODE_TIMEOUT = {  # mode -> ms it runs before auto-reverting to the mode
    "system_crash": 10000,  # that was active before it (or DEFAULT_MODE at boot)
}


async def servo_task():
    pos = float(core.servo_state["angle"])
    vel = 0.0
    last_mode = None
    behavior = None
    revert_to = None   # mode to fall back to when a timed mode expires
    revert_at = None   # ticks_ms deadline, or None
    while True:
        mode = core.state["mode"]
        if mode != last_mode:
            core.dbg("[servo] mode ->", mode)
            hook = SERVO_ON_ENTER.get(mode)
            if hook is not None:
                try:
                    hook()
                except Exception as e:
                    core.dbg("[servo] on-enter hook failed:", e)
            factory = SERVO_BEHAVIORS.get(mode, SERVO_BEHAVIORS[core.DEFAULT_MODE])
            behavior = factory()
            timeout = SERVO_MODE_TIMEOUT.get(mode)
            if timeout is None:
                revert_to = revert_at = None
            else:
                revert_to = last_mode if last_mode in SERVO_BEHAVIORS else core.DEFAULT_MODE
                revert_at = time.ticks_add(time.ticks_ms(), timeout)
                core.dbg("[servo] %s: revert to %s in %dms" % (mode, revert_to, timeout))
            last_mode = mode

        if revert_at is not None and time.ticks_diff(time.ticks_ms(), revert_at) >= 0:
            core.dbg("[servo] %s expired -> %s" % (mode, revert_to))
            core.state["mode"] = revert_to
            revert_to = revert_at = None

        now = time.ticks_ms()
        res = behavior.target(now, pos)
        setpoint, vmax = res[0], res[1]
        accel = res[2] if len(res) > 2 else SERVO_MAX_ACCEL
        setpoint = _clamp(setpoint, SERVO_MIN_ANGLE, SERVO_MAX_ANGLE)
        pos, vel = _approach(pos, vel, setpoint, vmax, accel, TICK_S)
        pos = _clamp(pos, SERVO_MIN_ANGLE, SERVO_MAX_ANGLE)
        servo.duty_u16(angle_to_duty_u16(pos))
        core.servo_state["angle"] = pos
        core.servo_state["moving"] = abs(vel) > 0.5   # deg/s — "visibly moving"
        await uasyncio.sleep_ms(TICK_MS)
