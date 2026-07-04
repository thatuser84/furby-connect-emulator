# Furby Connect — Hardware & Data Format Reference

> Working notes compiled from the `Furby-ReConnect/Furby` repo: the GeneralPlus
> silicon datasheets, the leaked `GameCode.lod` build config, the on-toy NAND
> dump, and the DLC parser in `furby.py`. Goal: enough documented ground truth
> to stand up an emulator of the toy.

---

## 1. The Silicon

The Furby Connect's brain is a **GeneralPlus GPL16258** (a.k.a. the GPL16250
family) — a low-cost 16-bit multimedia SoC built around Sunplus's **µ'nSP 2.0**
core. This is nailed down directly by the toy's own linker/build config,
`GameCode.lod`:

```ini
[ARCH]
BODY=GPL16258VB_ROMLESS;      ; the actual part — ROM-less variant, boots from NAND
SEC=ROM,60000,6FFFF,R,CS4;    ; code segment mapped at 0x60000–0x6FFFF on chip-select 4
BANK=20,FFFF;                 ; external bank window
LOCATE=IRQVec,FFF5;           ; IRQ vector parked at 0xFFF5

BodyName=GPCE2064C            ; dev-board body profile
CurIsa=ISA13                  ; µ'nSP 2.0 instruction set, revision 13
FlashType=SST                 ; SST serial flash on the programmer path
nStackSize=2047               ; 2047-word stack
```

Toolchain (also from `GameCode.lod`) is the GeneralPlus unSP suite:
`gcc` (unSP backend) → `xasm16` → `xlink16`, linked against `CMacro1216.lib`,
emitting an S-record (`GameCode.s37`) that becomes `GameCode.bin`.

### 1.1 GPL162xx family spec (from `GPL162004A.pdf` / `DSAE0033844.pdf`)

The repo ships the **GPL162004A** "Advanced 16-Bit SoC with µ'nSP 2.0"
datasheet as the closest public sibling. Core numbers:

| Block | Spec |
|---|---|
| **CPU** | µ'nSP 2.0, 16-bit, up to **96 MHz**, 4 KB I-cache, embedded ICE/JTAG |
| **Registers** | R1–R4, R8–R15 (GP), PC, SP, BP, SR (segment) |
| **Interrupts** | 3× FIQ + 8× IRQ + `BREAK` software interrupt (28 sources total on 162004A) |
| **Internal SRAM** | 28K-word (`0x0000–0x6FFF`), single-cycle, on CPU-local bus, shared CPU/DMA/LCD |
| **Internal ROM** | 64K×16 (mask ROM; the Furby part is the ROM-*less* variant → boots external NAND) |
| **Sound (SPU)** | **16 hardware channels, each PCM8 / PCM16 / ADPCM**, HW dynamic volume compressor, MP3 (software) |
| **Audio out** | Dual 16-bit DAC (`DACOL`/`DACOR`), stereo, 16×16 FIFO per channel |
| **Audio in** | Differential MIC (`MICP`/`MICN`), 12-bit ADC, AGC |
| **Display (5.7)** | **STN-LCD** ≤320×240 (1/4-bit mono/gray) **+ TFT-LCD** (RGB565 parallel / serial delta·stripe RGB / MPU-type; `VSYNC HSYNC DE DCLK DATA`) — **this drives the eyes** |
| **Memory ctrl** | 5 banks × up to 256 pages × 64K words; SDRAM/ROM/SRAM/NOR/**NAND w/ 1/4/8-bit ECC** |
| **PLL/clock** | 3 PLLs: fast (15–96 MHz, 3 MHz/step), 27 MHz, 12 MHz; sysclk 32.768k / 12M / 96M |
| **Timers (5.10)** | 6× 16-bit (A–F); A/B/C do PWM/capture/compare; **TimeBase ctrl off 32768 Hz → 1 Hz–1024 Hz IRQs** (firmware heartbeat) |
| **Watchdog (5.11)** | must be cleared within a software-set period or **the CPU hard-resets the whole system** |
| **Power modes** | Normal / Wait / Halt / Halt2 / Sleep (wake on interrupt / timer / key-change) |
| **I/O** | Ports IOA–IOE, key-scan (≤88 keys + velocity); note IOA[7:0] shared with LCD data[7:0] |
| **Other** | 4-ch DMA, UART/IrDA, 2× SPI (master/slave), 2× SD/MMC, USB mini-host/device |

For the Furby the relevant subset is: **µ'nSP core + NAND-with-ECC boot +
16-channel SPU + stereo DAC + MIC/ADC + GPIO + timers + watchdog**, with the
**TFT-LCD controller driving the round eyes** (our 128×128 CEL framebuffer +
`A1R5G5B5` palette → RGB565), the **motor on a timer PWM**, and sensors on
GPIO/ADC. The genuinely-unused block is the **video *input*** (5.5,
camera/motion-detect) — **not** the LCD, which an earlier draft of this doc
wrongly dismissed as unused. The LCD *is* the eyes.

> `GPL32611BV10(ProductBrief).pdf` is flagged `(not furby)` in the repo — it's an
> **ARM7TDMI** multimedia chip (JPEG/MPEG4/face-detect). Included only as a
> GeneralPlus family reference; the Furby is µ'nSP, not ARM.

### 1.2 `GameCode.bin` firmware image

The extracted firmware (`Furby-Files/Furby-NAND/GameCode.bin`, ~917 KB) opens
with a GeneralPlus image signature and vector block:

```
0000: 50 47 70 73 73 69 69 70 70 73 47 02 47 ff 47 00   PGpssiippsG.G.G.
0010: c7 fe 47 00 00 00 05 00 70 0d 00 00 ...
```

The interleaved `47 xx` words are the µ'nSP reset/IRQ vector table (`GOTO`
targets), consistent with `LOCATE=IRQVec,FFF5`.

---

## 2. On-Toy NAND Layout

The dumped filesystem (`Furby-Files/Furby-NAND/`) is a flat, name-typed store.
The full raw dump (`furby-nand (Fixed OOB Data).bin`, ~114 MB) and the
personality WAV pack (~645 MB) are **Git-LFS pointers** in this clone — pull
them with `git lfs pull` if you need the raw NAND with OOB/ECC bytes.

```
Furby-NAND/
├── GameCode.bin / .lod        # firmware image + build/link config
├── FurbyData.dat              # persistent state (name, mood, pairing flags…)
├── Checksum.dat               # 18-byte integrity record (see §2.1)
├── color.dat / language.dat   # 1-byte selectors (eye colour / language)
├── AudioMegafiles/            # ROM audio banks in AMF container
│   ├── AudioAntenna_v1.bin     #   9 tracks  (BLE "antenna" chirps)
│   └── TestAudio_ROM_v2.bin     #  98 tracks (factory/diagnostic)
├── Downloads/
│   └── TOY.AMF                # 107 downloaded tracks (last DLC audio)
├── GameLogic/                 # µ'nSP-word state machines (see §5)
│   ├── MealCravings.bin
│   ├── PersonalityModifiers.bin
│   └── StateResponse.bin
├── Graphics/                  # boot/debug eye assets in DLC section formats
│   ├── LRGB.PAL/.CEL/.SPR
│   └── DebugFont_*
├── Personalities/            # one folder per built-in personality
│   ├── Base/ Cat/ DJ/ Ninja/ Pirate/ PopStar/ Princess/ Generic/
│   └── <NAME>.{PAL,CEL,SPR,XLS,AMF,APL,LPS,SEQ,MTR,FIR,FIT,INT,LED}
└── Slots/                    # fixed-size DLC download slots
    ├── Slots.STB              # UTF-16LE string table indexing the slots
    ├── A500k_1..6.SLT         # 6 × ~500 KB audio slots
    ├── A1000k_1..6.SLT        # 6 × 1 MB audio slots (0x100000 each)
    └── T2000k_1..2.SLT        # 2 × ~2 MB "text"/content slots
```

### 2.1 Small state files

- **`FurbyData.dat`** (1662 B) — mostly zero; a handful of leading fields
  (`01 00 … 03 00 03 00 … ff 00`) hold selectors/counters for saved state.
- **`Checksum.dat`** (18 B) — three little-endian dwords:
  `64CE677D  8CEDF69C  64CE677D` (a value + complement + repeat → integrity check).
- **`color.dat` / `language.dat`** — single-byte enum selectors.
- **`Slots.STB`** — UTF-16LE: `"...\Slots\T2000k_1.SLT"` etc., a path table the
  firmware walks to bind logical slots to physical `.SLT` files.

---

## 3. The DLC / Personality Container Format

Every personality **and** every downloadable DLC uses the same on-disk
container. `furby.py` is effectively a complete, round-trip-verified spec for
it. A file is a **0x288-byte header** followed by up to 16 sections laid out
back-to-back.

### 3.1 Header (`HEADER_section`)

```
+0x000  magic   "F\0U\0R\0B\0Y\0" + 23×\0 + 78 56 34 12 02 00 08 00
+0x030  16 section-descriptor slots × 38 bytes each  (total header = 0x288)
```

Each present section descriptor = `"DLC_0000."` (UTF-16-ish prefix) +
2-byte-spaced section tag + a rolling counter (starts `0x0040CFB5`, `+0x1A` per
section) + `uint32` section length. Sections always appear in this fixed slot
order:

`[?] PAL SPR CEL XLS [?] [?] [?] [?] AMF APL LPS SEQ MTR [?] [?]`

Unused/rare tags seen on disk: `FIR FIT CMR INT LED`.

### 3.2 Section catalogue

| Tag | Name | Contents | Unit |
|---|---|---|---|
| **PAL** | Palettes | 64-colour palettes, weird 16-bit RGBA (see §3.3) | 0x80 B each |
| **CEL** | Cels | 64×64 eye sprites, **6-bit indexed**, 4 px packed per 3 bytes | 0xC00 B/frame |
| **SPR** | Sprites | frame playlists + composited frames (quarter-frame refs), 16 channels | — |
| **XLS** | Action tree | 4-level tree mapping **action codes** → response sequences | 6/6/20/10 B tiers |
| **AMF** | Audio | `a18` (GeneralPlus ADPCM) track blob (see §4) | — |
| **APL** | Audio playlists | ordered lists of AUDIO / PAUSE / EOF tokens | 2 B words |
| **LPS** | Lip sync | mouth open/close phrase streams (`0x8xxx` open / `0x1xxx` shut) | 2 B words |
| **SEQ** | Sequences | ties a response together: playlist + motor + eye-anim select | 2 B words |
| **MTR** | Motor | servo/motion animations (single motor + cam) | 2 B words |

### 3.3 The palette quirk (16-bit "RGBA")

Colours are stored 16-bit little-endian and unpacked oddly (from
`PAL_section`):

```
R = (c & 0b0111110000000000) >> 7     # 5 bits → top of a byte
G = (c & 0b0000001111100000) >> 2     # 5 bits
B = (c & 0b0000000000011111) << 3     # 5 bits
A = (c & 0b1000000000000000) >> 8     # top bit is *inverted* alpha:
                                       #   bit set → transparent, clear → opaque
```

So it's effectively `A1 R5 G5 B5`, alpha-inverted. Cels are 6-bit palette
indices (0–63) → look up in the active 64-entry palette.

### 3.4 Action codes & response routing

A response is triggered by a 4-tuple action code `(i, j, k, l)` walked through
the `XLS` tree:

```
XLS[i][j][k][l].seq  →  SEQ[seq]              # which sequence
SEQ[seq][1] - 0x4546 →  APL index             # which audio playlist
APL[...] AUDIO tokens →  AMF track numbers     # which actual samples
SEQ[seq][3:]         →  0x8xxx/0xAxxx eye-anim + inter-clip delays
SEQ[seq][2]          →  MTR select (motor motion)
```

`furby.py`'s `replace_audio((75,0,0,0), [...a18...])` and
`trigger_custom_graphics()` helpers ride exactly this chain — that's the hook a
custom-content / emulator layer targets.

---

## 4. Audio — the `a18` / AMF format

- **Container (AMF):** `uint32 track_count`, then `track_count × uint32` offsets,
  then each track as `uint32 length` + payload. Verified identical across DLC
  `AMF` sections **and** the NAND `AudioMegafiles/*.bin`, `Downloads/TOY.AMF`,
  and each personality's `*.AMF` (e.g. `AudioAntenna_v1.bin` → 9 tracks;
  `TestAudio_ROM_v2.bin` → 98; `TOY.AMF` → 107; `Base.AMF` → 0x844).
- **Codec (a18):** GeneralPlus ADPCM, **16 kHz** mono. Raw-in-DLC tracks are
  headerless (just `length` + samples); standalone `.a18` files may carry the
  30-byte header `00 FF 00 FF "GENERALPLUS SP" 00 00` which `add_track()` strips
  at `0x30`.
- **Encoding into the toy:** there's no pure-Python a18 *encoder* in the repo yet
  — the README lists "implement an a18 codec for `.wav` conversion" as an open
  task. External refs (Hacksby, bluefluff) cover the codec.

---

## 5. Game-logic blobs

`GameLogic/*.bin` are streams of little-endian 16-bit words describing simple
state machines, e.g. `StateResponse.bin` (70 B):

```
00 00 | 32 00 33 00 64 00 50 00 | 00 00 14 00 | 32 00 10 00 32 00 37 00 ...
```

Values like `0x32` (50), `0x64` (100), `0x14` (20) read as thresholds / weights
(mood %, meal cravings, personality modifiers). Not yet fully mapped —
cataloguing these is the main reverse-engineering gap for behaviour fidelity.

---

## 6. Furbish

`furbish_dictionary.pdf` is a full English→Furbish lexicon (useful for labelling
audio tracks and scripting responses). Sampler:

| English | Furbish | | English | Furbish |
|---|---|---|---|---|
| yes / affirmative | `ee` | | love | `may-may` |
| no / stop | `boo` | | friend / buddy | `noo-lah` |
| big | `dah` | | hug | `may-lah` |
| little | `dee` | | play | `loo-lay` |
| food / feed | `ah-tah` | | sleep | `way-loh` |
| thinking / mind | `way` | | thank you! | `dah-kah-oo-nye` |
| party time | `dah-noh-lah` | | wassup? | `doo-oo-tye?` |

---

## 7. How content reaches the toy (BLE)

The Furby Connect pairs over **Bluetooth LE**. The official *Furby Connect
World* app pushes DLC in ~20-byte GATT writes; the firmware reassembles them
into a `.SLT` slot / `TOY.AMF`, then the action-tree/sequence machinery plays
them. Prior art for the BLE side (out of scope of this repo but the emulator's
other half):

- **bluefluff** (Jeija) — BLE protocol, `GeneralPlusFile` upload flow.
- **FurBLE** (Paul Stone) — Web-Bluetooth controller.
- **Furbhax** (L0ss & Swarley) — DLC upload tooling.
- **Hacksby** (Igor Afanasyev) — a18 audio implementation.

---

## 8. Emulator target — what to implement

To emulate the toy end-to-end, three layers:

1. **CPU/SoC layer** — a µ'nSP 2.0 (ISA13) interpreter + GPL16258 memory map
   (28K-word SRAM at `0x0000`, code segment at `0x60000`, NAND-backed banks,
   IRQ vec `0xFFF5`), executing `GameCode.bin`. Heaviest lift; only needed for
   *cycle-true* behaviour.
2. **Behaviour layer** (pragmatic path) — skip the CPU; reimplement the
   action-tree → SEQ → APL → AMF/MTR/LPS/eye pipeline directly from the parsed
   personality files. This is the level `furby.py` already operates at, so it's
   the fast route to a "virtual Furby" that reacts, speaks, blinks and moves.
3. **Transport layer** — a fake BLE peripheral speaking the Furby GATT profile so
   the real app can drive the emulator (or a headless trigger for testing).

**Recommended first milestone:** parse a personality (e.g. `Personalities/DJ/`),
render its cels+palettes to an on-screen eye, wire the SEQ/APL/AMF chain to a
16 kHz a18 decoder, and fire responses by action code. That's a playable Furby
without touching a single µ'nSP opcode.

---

*Sources in-repo: `GameCode.lod`, `datasheets/GPL162004A.pdf`,
`datasheets/unSP Programming Tools User's Manual`, `furby.py`,
`Furby-Files/Furby-NAND/`. External protocol refs credited in §7.*
