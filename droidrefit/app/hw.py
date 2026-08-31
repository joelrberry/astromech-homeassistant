# Hardware wiring — pin numbers + the raw peripheral objects. The single place
# to check firmware pin assignments against the schematic. Leaf module (only
# imports `machine`).

from machine import Pin, UART

# --- pin map ---
SERVO_PIN = 18          # dome servo PWM signal
SERVO_FREQ = 50         # standard hobby-servo PWM rate (Hz)
DFPLAYER_TX_PIN = 17    # UART2 TX  -> DFPlayer RX
DFPLAYER_RX_PIN = 16    # UART2 RX  <- DFPlayer TX
BUSY_PIN = 4            # DFPlayer BUSY (active-low)
RESET_BTN_PIN = 0       # devkit BOOT button, doubles as the factory-reset hold
NEOPIXEL_PIN = 5        # WS2812 dome pixels (data); via 74AHCT1G125 on the PCB
NEOPIXEL_COUNT = 4      # 0 holoprojector, 1 logic display, 2 rear circle, 3 light bar

# Front-panel buttons — each wired pin <-> GND, read active-low with the
# internal pull-up (no external parts). Broken out on the PCB as J_BTN.
BTN_MODE_UP_PIN = 32
BTN_MODE_DOWN_PIN = 33
BTN_SOUND_PIN = 25

# --- peripherals (constructed once, at import) ---
# The servo PWM object (app.servo) and the NeoPixel object (app.leds) are built
# in their own modules, not here.
dfplayer_uart = UART(2, baudrate=9600,
                     tx=Pin(DFPLAYER_TX_PIN), rx=Pin(DFPLAYER_RX_PIN))
busy_pin = Pin(BUSY_PIN, Pin.IN, Pin.PULL_UP)
reset_btn = Pin(RESET_BTN_PIN, Pin.IN, Pin.PULL_UP)

btn_mode_up = Pin(BTN_MODE_UP_PIN, Pin.IN, Pin.PULL_UP)
btn_mode_down = Pin(BTN_MODE_DOWN_PIN, Pin.IN, Pin.PULL_UP)
btn_sound = Pin(BTN_SOUND_PIN, Pin.IN, Pin.PULL_UP)
