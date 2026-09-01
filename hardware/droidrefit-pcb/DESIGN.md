# droidrefit carrier PCB — design spec

A single carrier board that consolidates the whole "droidrefit" R2‑D2 build
(the plain‑ESP32 replacement for the retired CyberBrick) onto one PCB with a
proper shared 5 V rail. Replaces the breadboard described in `../../docs/firmware.md`.

**Decided (from Q&A 2026‑08‑29):**
- ESP32 = the existing Elegoo devkit, **socketed** on female headers (removable).
  Confirmed **30‑pin** (15/side), **24.5 mm** between the two pin banks →
  custom symbol `droidrefit:ESP32_DevKit_30pin` (DOIT DevKit v1 pinout).
- DFPlayer Mini likewise **socketed** on female headers →
  custom symbol `droidrefit:DFPlayer_Mini` (16‑pin, counter‑clockwise numbering).
- 5 V input via **USB-C** (changed from barrel jack 2026-08-30): `J1` =
  16-pin USB-C receptacle (`C2765186`) + `R6`/`R7` 5.1 kΩ CC pull-downs →
  any USB-C cable/charger/power bank gives 5 V. No PD chip. Fully SMD, so
  JLC assembles the power input too. F1 PTC now 1812 2 A-hold / 4 A-trip.
- NeoPixel data line goes through a level shifter (3.3 V → 5 V) —
  using the **single‑gate 74AHCT1G125** (SOT‑23‑5) rather than the SO‑14 quad;
  one buffer is all that's needed and it's a JLC catalog part.
- Fab: **JLCPCB assembly** — SMD passives / IC / FET / connectors placed by JLC;
  the two module sockets and any THT connectors hand‑soldered after.

## Device‑to‑device connections (verified against firmware + netlist)

Firmware‑proven (bench‑tested in `droidrefit/bench/test-boot*.py`):
- ESP32 GPIO17 → R2 1k → DFPlayer RX(2); DFPlayer TX(3) → ESP32 GPIO16
  (`main.py:157`, `UART(2, tx=Pin(17), rx=Pin(16))`)
- DFPlayer BUSY(16) → ESP32 GPIO4 direct, active‑low; ESP32 internal pull‑up
  only (`main.py:226` `Pin(4, Pin.IN, Pin.PULL_UP)`) — no external part
- ESP32 GPIO18 → servo signal (`main.py:150`, `PWM(Pin(18), freq=50)`)
- DFPlayer SPK_2(6)/SPK_1(8) → speaker connector J5

NeoPixel path (firmware written — `app/leds.py` — not yet bench-tested with real pixels):
- ESP32 GPIO5 → 74AHCT1G125 → R3 330 → JP1 → J3 (XC016) → LED board

All of the above are present in the schematic netlist.

## Status

- [x] Project + custom symbol library created, registered project‑scope.
- [x] Schematic drawn, wired, **ERC clean (0 errors)**. Netlist verified.
      (2 cosmetic "symbol doesn't match library copy" warnings on the two
      custom symbols — a create/round‑trip formatting diff, no netlist impact.)
- [x] Footprints assigned (provisional — see notes) + 3 custom footprints built
      (`droidrefit.pretty`): ESP32 30-pin socket (24.5 mm rows), DFPlayer socket
      (2.54 mm pitch, **15.24 mm / 0.6 in rows — confirmed from board spec**),
      NeoPixel bridge field.
- [x] Board created from schematic — 25 footprints, 20 nets.
- [x] Board outline 100×70 mm rounded-rect, 4× M3 mounting holes
      (top corners, mid-left, bottom-right — offset to clear J1).
- [x] Placement cleaned — functional grouping, **0 DRC errors**, 0 courtyard
      overlaps. Remaining 27 DRC warnings = 23 silk-over-copper (ref text over
      THT pads, fix in silk-polish pass) + 4 custom-footprint metadata notes.
      Layout: U1 left · U2+C2+R2+J5 top · U3/R3/JP1/C4/J3 mid-right ·
      power (J1/F1/Q1/R1/C1) bottom-left · servo (C3/C8/J2) bottom-right.
- [x] **Routing — complete.** Design rules (0.2 clr / 0.25 trk / 0.6 via),
      `Power` netclass (0.8 mm) in the .kicad_pro. **B.Cu = GND pour.**
      - 13 nets hand-routed first (power front-end, both UARTs, NeoPixel
        section), then **Freerouting 2.1.0** autorouted the rest: +5V tree,
        SERVO_PWM, SPK_P/N, DFP_BUSY, and GND stitching vias.
      - Result: **120 tracks, 18 vias, every net routed**.
      - **Toolchain note:** Freerouting 2.3.0 needs Java 25; installed JRE is
        Temurin 21, so pinned to Freerouting **2.1.0** (`~/.kicad-mcp/freerouting.jar`).
        To use 2.3.x later, install a JRE 25+.
      - **B.Cu GND pour filled** (in KiCad, `B`) 2026‑08‑29 — ~6352 mm² of
        7000, all THT/SMD GND pads tie in (THT via thermal‑relief spokes,
        SMD via stitching vias). The 15 "via dangling" warnings cleared.
      - Clearance error **cleared**: it was the Default net class pinned at
        0.20 mm and Freerouting's +5V trace landing 0.1988 mm from JP1's GND
        pad. Not a physical problem — lowered the net‑class clearance to
        **0.15 mm** (JLCPCB standard is 0.127 mm), `.kicad_pro`
        `net_settings`. **DRC now 0 errors.**
      - **Remaining 12 DRC warnings, all cosmetic:** 1 lib_footprint_mismatch
        on U2 (silk fix — "Update Footprint from Library" clears it);
        4 custom‑footprint metadata notes; 7 silk‑over‑copper (ref text over
        pads — JLC auto‑clips; move the text in the silk‑polish pass).
- [x] **LCSC parts assigned** (via `jlcsearch.tscircuit.com` — the full JLC
      DB download 503'd). R4 removed; F1 settled on an **1812 2 A‑hold /
      4 A‑trip PTC** (C18198342) after a 2920/3 A version made the power
      corner un‑routable.
- [x] **USB-C power input** (2026-08-30): barrel jack → `J1` 16-pin USB-C
      (`C2765186`) + `R6`/`R7` 5.1 kΩ CC pull-downs. Routed (CC straight to
      R6/R7, VBUS up-and-over to F1, GND via the pours). **14 SMD line items.**
- [x] **GND pours both layers, refilled, solid pad connection**; ~32 grid
      stitching vias. **DRC: 0 errors.**
- [x] **DNP** on J2/J5/U1/U2/JP1 **+ C1–C4** (out of CPL). CPL hand-filtered to
      the SMD parts JLC places (incl. USB-C, which auto-excluded as "has THT pads").
- [x] **C1–C4 electrolytics moved to hand-solder (2026-08-30).** JLC flags SMD
      aluminium electrolytic cans as Standard-PCBA-only (reflow temperature);
      Standard adds $25/side setup + break-off rails (board → 100×80). Keeping
      Economic PCBA instead: C1–C4 set `dnp yes` + `in_pos_files no` in the sch,
      dropped from BOM-JLC/CPL-JLC, listed in `BOM-full.csv` as "hand-solder"
      and in the README hand-solder table. Silk outlines + refs still plotted.
- [x] **Fab package regenerated** (2026-08-30, silk labels de-overlapped by
      user) → `fab/`: gerbers zip, Excellon drill (+PDF maps),
      `BOM-JLC.csv` (11 lines / 17 desig) + `CPL-JLC.csv` (17), `BOM-full.csv`,
      `fab-preview.svg`, `2d-top.png`, `README.md`.
- [x] **ESP32 socket pinout VERIFIED** against the user's board photo
      (`fab/BoardImage.HEIC`, 2026-08-30) — both headers match the footprint
      exactly (standard DOIT DevKit v1). Added silk orientation markers to
      U1: "EN / ANT" one end, "VIN / devkit USB end" the other, pin-1 arrow.
      The devkit's own USB-C ends up on the same edge as the carrier's USB-C.
      U2 (DFPlayer) verified from the photo too: **µSD card slot is on the
      pin 8/9 (SPK/DAC) end**, opposite the "DFPlayer Mini" text / pin-1 /
      VCC end. Silk fixed — "microSD card slot this end" now on U2's south
      side (pin 8/9), pin-1 chevron on the north-left (VCC). U2 NOT rotated
      (keeps the UART pins facing the ESP32): the card ejects ~15 mm toward
      the board interior at the ~8 mm socket height — clear of everything
      until C1 (~30 mm away). Reach over the board to swap cards. Rotate U2
      180° only if edge-facing card access matters more than short UART runs.
- [ ] Silk polish (7 ref-text-over-pad warnings) + U2 footprint re-sync — cosmetic.
- [ ] Order: bare board from gerbers zip; **Economic** SMD assembly from
      BOM-JLC + CPL-JLC (check rotations in JLC's preview — esp. Q1/U3/D1/J1);
      hand-solder C1–C4 + J2/J5 + 2 module sockets; plug in ESP32 + DFPlayer.

### Provisional footprint notes
- Q1 = SOT-23 placeholder; real part is Id ≥ 6 A → likely DPAK/SOT-89.
- C1/C4 = CP_Elec_8x10.5, C2/C3 = CP_Elec_6.3x7.7 — verify against the actual
  1000 µF/470 µF 10 V parts chosen.
- J1 = CUI PJ-102AH horizontal (has a switch pad; symbol is 2-pin).

## As‑built net names (schematic)

`+5V` `+3V3` `GND` · `VBUS_IN` (jack→fuse) · `VFUSED` (fuse→P‑FET source) ·
`SERVO_PWM` (IO18) · `NPX_IN` (IO5→buffer) · `NPX_DATA_5V` (buffer→R3) ·
`NPX_DATA` (R3→J3/J4) · `ESP_TX2` (IO17→R2) · `TX2_DFP` (R2→DFP RX) ·
`RX2_DFP` (IO16↔DFP TX) · `DFP_BUSY` (IO4↔DFP BUSY, direct) ·
`SPK_P`/`SPK_N` (DFP pins 6/8 → J5) · `LED_A` (R5→D1).

## Firmware pin map (source of truth: `droidrefit/main.py`, `docs/firmware.md`)

| ESP32 GPIO | Net            | Goes to                              | Notes |
|-----------:|----------------|--------------------------------------|-------|
| 17         | `TX2_DFP`      | DFPlayer RX (pin 2), via 1 k series  | 1 k is the standard DFPlayer noise‑reduction resistor |
| 16         | `RX2_DFP`      | DFPlayer TX (pin 3), direct          | DFPlayer TX idles ~3.3 V, safe |
| 4          | `DFP_BUSY`     | DFPlayer BUSY (pin 16), direct       | active‑low; firmware sets the ESP32 internal pull‑up (`Pin.PULL_UP`) — no external resistor, matches the proven bench wiring |
| 18         | `SERVO_PWM`    | Servo connector signal               | 50 Hz hobby servo |
| 5          | `NPX_IN`       | 74AHCT1G125 A (pin 2)                | shifter input. GPIO5 kept per `docs/firmware.md` (user decision 2026‑08‑29) — it's a boot‑strapping pin (internal pull‑up, fine in practice); accept a possible brief LED flash at power‑on. |
| —          | `NPX_DATA`     | 74AHCT125 Y1 (pin 3) → 330 Ω → NeoPixel connector(s) | 5 V logic out |
| 32         | `BTN_MODE_UP` | J_BTN pin 2 → button → GND           | firmware internal pull‑up, active‑low; no external part |
| 33         | `BTN_MODE_DN` | J_BTN pin 3 → button → GND           | " |
| 25         | `BTN_SOUND`   | J_BTN pin 4 → button → GND           | " — tap = sound; Mode▲+Sound 5 s = WiFi setup |
| 27         | `BUZZER`      | → piezo → GND (J_BUZZER)             | `machine.PWM` UI feedback. Bare piezo disc + ~100 Ω series R for a quiet beep; option: 2N7002 low‑side + piezo from +5 V for louder |
| 21         | `I2C_SDA`    | J_OLED pin 3 → SSD1306 SDA           | ESP32 default I2C SDA. Firmware auto‑detects the OLED (`i2c.scan()`); absent = no display task |
| 22         | `I2C_SCL`    | J_OLED pin 4 → SSD1306 SCL           | ESP32 default I2C SCL |
| 1          | `UART0_TX`   | J_UART pin 3                         | REPL / boot console TX. Also on the devkit's onboard Micro‑USB → do **not** connect a USB‑TTL cable to J_UART and plug the devkit's USB at the same time (bus contention) |
| 3          | `UART0_RX`   | J_UART pin 4                         | REPL / boot console RX |
| VIN        | `+5V`          | shared rail                          | devkit's onboard LDO feeds its own logic |
| 3V3        | `+3V3`         | J_OLED pin 1 (SSD1306 VCC)           | now used — the devkit's onboard regulator (AMS1117, fed from +5V) powers the ~20 mA OLED. R4 pull‑up still removed (redundant with the firmware internal PU). If no OLED is fitted, 3V3 is simply unloaded. |
| GND        | `GND`          | —                                    | |

Unused 74AHCT125 inputs (A2..A4) tied to GND; unused `/OE2../OE4` tied to GND;
`/OE1` tied to GND (buffer 1 always enabled).

## Power tree

```
J_PWR (5V barrel, centre +)
  └─ F1  PTC resettable fuse (Ihold ≥ 3 A)
       └─ Q1  P‑FET reverse‑polarity protection (high‑side, Id ≥ 6 A, low Rds(on))
            └─ +5V rail ──┬─ C_BULK 1000 µF / 10 V (low‑ESR) + 100 nF
                          ├─ ESP32 VIN            (+ 100 nF)
                          ├─ DFPlayer VCC         (+ 470 µF + 100 nF, close to socket)
                          ├─ Servo connector V+   (+ 470 µF + 100 nF, close to connector)
                          ├─ NeoPixel connector V+(+ 1000 µF + 100 nF, close to connector)
                          ├─ 74AHCT125 VCC        (+ 100 nF)
                          └─ PWR LED + 2 k        (green, "rail live")
```

Rail sizing target: ~2 A realistic peak, 3 A+ supply (see `docs/firmware.md`).
Trace/pour: dedicated +5V and GND copper pours, ≥ 2 mm equivalent for the
servo + NeoPixel + DFPlayer branch; wide fill everywhere else.

## Connectors

| Ref     | Type                                   | Pins                    | For |
|---------|----------------------------------------|-------------------------|-----|
| J_PWR   | DC barrel jack 2.1 mm, centre +        | +, −                    | 5 V input |
| J_ESP   | 2 × 1×19 female socket, 2.54 mm        | full devkit             | ESP32 devkit — **row spacing TBD, must measure the physical Elegoo board** |
| J_DFP   | 2 × 1×8 female socket, 2.54 mm         | DFPlayer Mini 16‑pin    | row spacing **15.24 mm (0.6 in)** — confirmed |
| J_SERVO | 1×3 header, 2.54 mm, friction lock     | SIG / +5V / GND         | dome servo — confirm lead order before crimping |
| J3 (J_NPX) | JST‑SH 1.0 mm 3‑pin (SMD)           | pins → JP1 P1/P2/P3     | mates the **Bambu XC016** cable (300 mm straight‑through JST‑SH 1.0 3‑pin) to the droid's existing multi‑LED board |
| JP1     | pin‑order **solder‑bridge field** (custom)| DIN/V+/GND ↔ P1/P2/P3  | the LED board's SH1.0 order can't be measured — JP1 maps DATA/+5V/GND to any of the 3 connector pins. Ships bridged DIN‑P1 / V+‑P2 / GND‑P3; cut & re‑bridge to correct it. V+ = **+5V** (confirmed). |
| J_SPK   | JST‑PH 2.0 mm 2‑pin                    | SPK+ / SPK−             | 4–8 Ω speaker, DFPlayer pins 6 & 8 — differential, do **not** ground |
| J_BTN   | 1×4 header, 2.54 mm                    | GND / IO32 / IO33 / IO25 | front‑panel buttons: mode ▲ (IO32), mode ▼ (IO33), sound (IO25). Each wires between its signal pin and GND; firmware internal pull‑up, **no external resistors**. Tap = cycle / hold = volume; Mode ▲ + Sound 5 s = WiFi setup. |
| J_BUZZER | 1×2 header, 2.54 mm (or onboard piezo pads) | IO27 / GND         | piezo buzzer for UI feedback. Direct off IO27 through ~100 Ω for a quiet beep; footprint should also allow a 2N7002 low‑side driver + piezo from +5 V. Skipped in firmware when `buzzer_enabled` is false. |
| J_OLED  | 1×4 header, 2.54 mm                    | 3V3 / GND / IO21 / IO22 | SSD1306 128×64 I2C status display (addr 0x3C). ~20 mA off **3V3**, not +5V. Firmware auto‑detects — no display = no cost. Shows name/mode/volume/net while running, and the AP name+password+URL while the setup portal is up. Pin order matches the common 4‑pin SSD1306 module (GND/VCC/SCL/SDA varies by module — confirm silk before crimping). Add 4.7 kΩ SDA/SCL pull‑ups to 3V3 on the board (most modules already have them → ~2.3 kΩ effective, still fine; keeps a bare panel working). |
| J_UART  | 1×4 header, 2.54 mm                    | 3V3 / GND / IO1(TX0) / IO3(RX0) | serial console / `deploy.py` for a **boxed unit** without opening it — plug a USB‑TTL cable here (leave its 5V/VCC lead off; 3V3 pin is only a reference/optional). No onboard USB‑serial chip on the carrier; the carrier's USB‑C is power‑only. **Never** use this and the devkit's own Micro‑USB at once. No auto‑reset wiring → press EN / power‑cycle to reset. |

### Updating a boxed unit

The carrier's USB‑C is **power only** — no data lines, no USB‑serial chip. Once
the droid is in its box there are three ways in:

1. **`J_UART`** + a USB‑TTL cable — `deploy.py` / serial without opening the box.
2. **Devkit Micro‑USB** — reachable only via a box cutout; the auto‑reset /
   auto‑flash path.
3. **OTA** (`app/ota.py`, opt‑in) — wireless once the droid is on WiFi with an
   `ota_url` mirror set. The "never touch it again" path.

## DFPlayer Mini pinout reference (16‑pin, for J_DFP wiring)

1 VCC · 2 RX · 3 TX · 4 DAC_R · 5 DAC_L · 6 SPK_2 · 7 GND · 8 SPK_1 ·
9 IO_1 · 10 GND · 11 IO_2 · 12 ADKEY_1 · 13 ADKEY_2 · 14 USB_DP · 15 USB_DM · 16 BUSY

Only VCC/RX/TX/SPK_2/GND/SPK_1/BUSY are wired; DAC/IO/ADKEY/USB left unconnected
(with `no_connect` flags).

## BOM (JLCPCB — exact LCSC part numbers assigned during part selection)

| Ref            | Value / part                          | Package  | JLC tier (target) |
|----------------|---------------------------------------|----------|-------------------|
| U3             | 74AHCT1G125 (single-gate buffer)      | SOT‑23‑5 | Extended |
| Q1             | P‑MOSFET, −20 V, Id ≥ 6 A, low Rds    | SOT‑23 / DPAK | Basic |
| F1             | PTC resettable fuse, Ihold ≥ 3 A      | 1812     | Basic |
| D1             | LED, green                            | 0603     | Basic |
| C_BULK, C_NPX  | 1000 µF / 10 V, low‑ESR               | SMD can  | Extended |
| C_DFP, C_SERVO | 470 µF / 10 V                         | SMD can  | Extended |
| C1..C5         | 100 nF X7R                            | 0603     | Basic |
| R_DFP          | 1 kΩ                                  | 0603     | Basic |
| R_NPX          | 330 Ω                                 | 0603     | Basic |
| R_LED          | 2 kΩ                                  | 0603     | Basic |
| R_GATE (R1)    | 100 kΩ (P‑FET gate)                   | 0603     | Basic |
| J_PWR          | DC barrel jack 2.1 mm                 | THT      | Extended |
| J_NPX          | JST‑SH 1.0 mm 3‑pin                   | SMD      | Basic/Extended |
| J_SPK          | JST‑PH 2.0 mm 2‑pin                   | THT      | Basic |
| J_ESP, J_DFP, J_SERVO, J_NPX2 | 2.54 mm sockets/headers | THT      | hand‑solder, not JLC placed |
| MH1..MH4       | M3 mounting holes                     | —        | — |

## Board

- ~2‑layer, size follows the devkit footprint + a strip for power/DFPlayer/connectors.
  Rough guess 70 × 60 mm, finalised after placement.
- Layer 1: signal + GND pour. Layer 2: +5V pour where the branch runs, GND elsewhere.
- 4 × M3 mounting holes, corners.
- Silk: label every connector, mark J_PWR polarity, mark J_SERVO pin 1, mark
  J_NPX / J_NPX2 pin 1 with a "verify ring order" note.

## Open items to resolve before routing

1. **Elegoo devkit variant** — 30‑pin vs 38‑pin, and row spacing (0.9 in vs 1.0 in).
   Physically measure the board on hand. Affects J_ESP footprint + the GPIO the
   pins land on.
2. **DFPlayer socket row spacing** — RESOLVED: 15.24 mm (0.6 in), 2.54 mm pitch.
3. **J_SERVO pin order** — match the dome servo's existing 3‑pin lead.
4. **NeoPixel SH1.0 pin order** — RESOLVED as a build‑time solder‑bridge (JP1),
   since the LED board can't be probed. Default bridge = DIN/V+/GND (pin 1→3).
   J3 gender: XC016 is a plug‑both‑ends cable; J3 must be the mating SH1.0
   **top/side‑entry SMD socket** — confirm entry direction at layout.
5. RESOLVED — spare GPIOs are broken out as `J_BTN` (IO32/33/25 + GND) for the
   three front-panel buttons; firmware drives them with internal pull-ups.

## Build sequence (KiCad MCP)

1. `create_project` at this path.
2. Schematic: place J_ESP (as ESP32 devkit symbol), J_DFP, U1, Q1/F1 power
   front‑end, connectors, passives. Wire per the pin map above. ERC.
3. Assign footprints + LCSC parts (download JLC parts DB first).
4. `create_board_from_schematic`, board outline, place, pours, route, DRC.
5. Gerbers + drill + BOM + CPL for JLCPCB.
