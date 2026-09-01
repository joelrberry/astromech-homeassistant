# DFPlayer Mini — how the droid uses it

Reference for the audio module: the SD-card layout, how to add clips, and what
the serial protocol can and can't do. Driver: [`droidrefit/lib/dfplayer.py`](../droidrefit/lib/dfplayer.py).
Playback logic: [`droidrefit/app/sound.py`](../droidrefit/app/sound.py).

## The module

"DFPlayer Mini" is a **family** of pin-compatible modules built around different
chips — YX5200, YX5300, MH2024K-24SS, GD3200B, JL AB23A, and more. They share
the frame protocol but **differ in which query commands work and how replies
are framed**. Anything past "play a track / set volume / read BUSY" should be
treated as module-specific.

This build talks to it over **UART2 at 9600 baud** (`GPIO17`→RX, `GPIO16`←TX,
1 kΩ in series on TX) and reads the **BUSY pin** (`GPIO4`, active-low) for
"is a track playing". That's the whole interface — deliberately minimal,
because it's the part that behaves the same on every module.

## SD card layout

- **FAT32**, card ≤ 32 GB.
- Numbered folders at the root: `01`, `02`, … `99` (two digits).
- Files inside: `001.mp3`, `002.mp3`, … up to `255.mp3` (three digits,
  zero-padded). A leading human name after the number is fine (`003 happy
  beep.mp3`) — the module keys off the leading number.
- **No gaps** in the numbering within a folder.
- Some clone chips index files by **the order they were copied to the card**,
  not by filename. If playback order matters, copy the files in numeric order,
  or run `fatsort` on the card afterwards.

The droid's folders:

| Folder | `SOUND_FOLDERS` name | Mood |
|---|---|---|
| `01` | `chat` | idle chatter / beeps |
| `02` | `gen` | general / misc |
| `03` | `happy` | pleased, excited |
| `04` | `leia` | the Leia message |
| `05` | `sad` | worried, mournful |
| `06` | `scream` | alarm / distress (also the `system_crash` cue) |
| `07` | `whistle` | short whistles |

## How playback works

`app/sound.py`:

```python
SOUND_FOLDERS = {"chat": (1, 18), "gen": (2, 19), "happy": (3, 7),
                 "leia": (4, 3), "sad": (5, 4), "scream": (6, 3),
                 "whistle": (7, 3)}
```

`{name: (folder_number, file_count)}` — **the counts are hard-coded.** A trigger
(button, MQTT, `system_crash`) calls `play_random_in_folder(name)`, which picks
`track = random(1..count)` and sends **play-folder-track** (`0x0F`) with that
`folder, track`. `busy_monitor_task` then watches BUSY to know when it finished.

### Adding clips to a folder

1. Copy the new file(s) into the folder as the next sequential number
   (`03/008.mp3`, `03/009.mp3`, …).
2. Bump that folder's count in `SOUND_FOLDERS` (`"happy": (3, 7)` → `(3, 9)`).
3. `python3 tools/deploy.py`.

One line changed, one flash. The folder *numbers* never change, so the mapping
above is stable.

## Serial protocol

Frame: `7E FF 06 <cmd> <feedback> <p1> <p2> <ck_hi> <ck_lo> EF` — full detail in
the `lib/dfplayer.py` header.

### Commands the firmware sends

| Cmd | Meaning | Used for |
|---|---|---|
| `0x0C` | reset | boot diagnostics (`player.reset()`) |
| `0x09` | select storage device | `select_tf_card()` at boot (p2 = `0x02` = TF) |
| `0x06` | set volume 0–30 | `set_volume()` |
| `0x0F` | play folder/track (p1 = folder, p2 = track) | every sound trigger |

### Query commands — available, **not used here**

| Cmd | Returns | Why unused |
|---|---|---|
| `0x4F` | total folder count on the card | not needed — folders are fixed |
| `0x47` / `0x48` | total file count (TF vs USB — **swaps by module**) | grand total, not per-folder |
| `0x4E` | files in the **current** folder | only valid *after* a track from that folder is selected, and unreliable on clones |
| `0x42` | play status (stopped / playing / paused) | BUSY pin covers it |
| `0x43` | current volume | firmware is the source of truth (`core.state["volume"]`) |
| `0x46` | firmware version | — |
| `0x4C` | index of the track currently playing | — |

### Replies

The module sends 10-byte reply frames — ACK (`0x41`, only if the feedback bit
is set), errors (`0x40` + a code), query answers, and **unsolicited** messages:
track-finished (`0x3D` on TF, with the finished track's index), card
inserted / removed (`0x3A` / `0x3B`), init after reset (`0x3F`).

`dfplayer._log_reply()` reads and **logs** whatever comes back right after a
command (best-effort, ~50 ms window) — turn the debug switch on to see it while
bench-testing wiring. **Nothing in the firmware acts on replies.**

## Things the DFPlayer can't do

- **No file listing, no names, no manifest.** It plays by number only.
- **No "does track N exist" query.** You find out by playing it and getting
  error reply `0x40` with code `0x06` ("track not found").
- **No reliable cross-module "track finished" event** — hence the BUSY pin.

## If the hard-coded counts ever become a maintenance pain

Three ways to make folders self-sizing, in rough order of robustness. **All of
them first require upgrading `dfplayer.py` to parse reply frames** (send a
query, read the specific 10-byte answer, return the value) — it only logs them
today.

1. **Overshoot-and-retry.** Drop the counts entirely. Pick a random index in a
   generous range, play it; if `0x40`/`0x06` comes back, retry lower and cache
   the folder's real ceiling. Folders grow to any size, zero bookkeeping;
   costs an occasional ~100 ms retry.
2. **Boot-time `0x4E` probe.** At startup, silently (volume 0) play track 1 of
   each folder, query `0x4E`, cache the count; fall back to configured defaults
   if the module doesn't answer. No runtime cost; depends on flaky `0x4E`.
3. **Counts in `config.json`.** Move `SOUND_FOLDERS` counts into config so
   adding files is a REPL command, not a reflash. Simplest; still manual.
