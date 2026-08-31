# droidrefit carrier PCB — fab package

Regenerated 2026-08-30 (USB-C power input; silkscreen labels de-overlapped).
2-layer, 100 × 70 mm, rounded
corners (r=3 mm), 4× M3 mounting holes. **DRC: 0 errors.**

## Order the bare board

Upload **`droidrefit-pcb-gerbers.zip`** (RS-274X gerbers + Excellon drill).

| Setting | Value |
|---|---|
| Layers | 2 |
| Dimensions | 100 × 70 mm |
| Thickness | 1.6 mm |
| Min track / clearance | 0.25 mm / 0.15 mm (well within any fab's spec) |
| Min drill | 0.3 mm |
| Copper | GND pour both layers, solid pad connection, ~32 stitching vias |

## SMD assembly (JLCPCB "Assembly")

Upload:
- **`droidrefit-pcb-BOM-JLC.csv`** — 11 line items, 17 designators
- **`droidrefit-pcb-CPL-JLC.csv`** — 17 placements, all top side

Every designator in the BOM matches one in the CPL and vice-versa (17 each).
The BOM lists designators comma-separated, **not** as ranges — JLC's parser
rejects `C5-C9` and wants `C5,C6,C7,C8,C9`. If you ever re-export from KiCad,
re-expand any ranges before uploading.

This BOM/CPL is sized for JLC's **Economic PCBA** (no per-side setup fee).
Includes **J1, the USB-C receptacle** (SMD with 4 through-hole anchor posts —
JLC assembles these routinely). Basic parts: the 4 resistor values, 100 nF,
AO3401A, 5.1 k. Extended (~$3 setup each): LED, PTC fuse, JST-SH,
74AHCT1G125, USB-C.

**The 4 electrolytics (C1–C4) are deliberately NOT in these files.** SMD
aluminium electrolytic cans are reflow-temperature-restricted, so JLC only
runs them on Standard PCBA ($25/side setup + break-off rails). They are easy
to hand-solder (two large pads each) — see the hand-solder list below.

### Power input — USB-C, 5 V

J1 is a 16-pin USB-C receptacle. R6/R7 are the **5.1 kΩ CC pull-downs** that
tell any USB-C source "5 V device." Plug in any USB-C cable + phone charger /
power bank / laptop port. No PD chip — a 5 V/3 A charger delivers what the
board's ~2 A peak needs. (F1 PTC is 2 A-hold / 4 A-trip; Q1 is reverse-
polarity — mostly moot on USB-C but harmless.)

### Check rotations in JLC's preview

KiCad and JLC disagree on the 0° orientation of a few packages. JLC's upload
wizard draws each part on the board — rotate any that look wrong. Check:
**Q1** (SOT-23), **U3** (SOT-23-5), **D1** (LED cathode),
**J1** (USB-C mouth must face the board edge).

## Hand-soldered afterwards (NOT in the JLC files)

| Ref | Part | LCSC (ref) |
|---|---|---|
| C1, C4 | 1000 µF 10 V SMD electrolytic, CP_Elec_8×10.5 (D8) | C310838 |
| C2, C3 | 470 µF 10 V SMD electrolytic, CP_Elec_6.3×7.7 | C335982 |
| J2 | 1×3 2.54 mm pin header (servo) | C49257 |
| J5 | JST-PH 2.0 mm 2-pin (speaker) | C131337 |
| U1 socket | **2× 1×15 female header, 2.54 mm** — buy 2 | — |
| U2 socket | **2× 1×8 female header, 2.54 mm** — buy 2 | — |

C1–C4 mount on the top-side silk outlines. Mind polarity: the **+** stripe
on the footprint marks pin 1; the can's marked stripe is the **−** terminal.

Then plug in: **ESP32 DevKit 30-pin** (your Elegoo one) and a **DFPlayer
Mini**. Loads on the connectors: dome servo → J2, R2 LED board via a Bambu
**XC016** JST-SH cable → J3, 4–8 Ω speaker → J5. Power: any USB-C cable → J1.

JP1 = bare solder-bridge pads, ships bridged DIN→P1 / V+→P2 / GND→P3; cut &
re-bridge if the LED board's SH1.0 order differs.

## Known cosmetic DRC items (do not block fab)

- **7 silk-over-pad** — ref text on JP1, mounting holes, J5. JLC auto-clips.
- **1 footprint-lib mismatch on U2** — from a DFPlayer silk tweak. Right-click
  U2 → Update Footprint from Library, or ignore.
- Clearance rule is 0.15 mm in the project. If KiCad ever shows a 0.1988 mm
  flag at JP1, re-set Board Setup → Constraints → Min clearance = 0.15.

## Files

```
droidrefit-pcb-gerbers.zip     → order the bare board
droidrefit-pcb-BOM-JLC.csv     → JLC assembly BOM (SMD only)
droidrefit-pcb-CPL-JLC.csv     → JLC placement file (SMD only, incl. USB-C)
droidrefit-pcb-BOM-full.csv    → everything incl. hand-solder parts
droidrefit-pcb-fab-preview.svg → quick visual check
*.g?? *.drl *-drl_map.pdf      → loose gerbers, drill, drill maps
```
