# Hardware Reference — Furby Connect (2016)

This document describes the Furby Connect's silicon and on-flash data, and catalogs the
primary-source datasheets bundled in [`docs/datasheets/`](datasheets/). Only facts confirmed
by a datasheet, the toy's own build configuration, the MAME `generalplus_gpl16250` reference,
or direct measurement against the NAND dump are stated here.

---

## 1. System-on-Chip

The Furby Connect is built around a **GeneralPlus GPL16258**, a member of the GPL16250
(a.k.a. `gpac800`) family — a low-cost 16-bit multimedia SoC with a Sunplus **µ'nSP** CPU
core. The part is identified directly by the toy's linker/build configuration
(`GameCode.lod`, `BODY=GPL16258VB_ROMLESS`), which also fixes:

| Property | Value | Source |
|---|---|---|
| CPU core | Sunplus µ'nSP, ISA revision 13 (`CurIsa=ISA13`) | `GameCode.lod` |
| Boot mode | ROM-less; boots from external NAND flash | `BODY=…_ROMLESS` |
| Word size | 16-bit, word-addressed | µ'nSP ISA |
| Address space | 22-bit (4 M-words) | GPL16250 datasheet |
| Stack | 2047 words (`nStackSize=2047`) | `GameCode.lod` |

### CPU core (µ'nSP)

- Registers: `SP, R1, R2, R3, R4, BP, SR, PC` (a shadow bank `R1–R4` exists for the
  fast-interrupt path).
- Status register `SR = [DS:6][N Z S C][CS:6]` — a 6-bit data segment, four condition
  flags, and a 6-bit code segment. The effective 22-bit program counter is `(CS << 16) | PC`.
- Interrupts: eight maskable IRQ lines plus a higher-priority FIQ. Vectors are a table of
  trampolines in SRAM (`0x6ff0` base on this firmware; FIQ region just below).

### Memory map (confirmed)

| Range (word addr) | Contents |
|---|---|
| `0x000000–0x006fff` | Internal SRAM |
| `0x007000–0x007fff` | Internal peripherals (MMIO) |
| `0x008000–0x02ffff` | Internal ROM / reserved |
| `0x030000–0x1fffff` | External chip-select space (CS0…) — direct view |
| `0x200000–0x3fffff` | External chip-select space — **banked** by register `0x7810` |

The banked window maps `realoffset = offset + (bank × 0x200000) − csbase`
(`bank = 0x7810 & 0x3f`, `csbase = 0x30000`). It routes to the external memories in
chip-select order: CS0/CS1 = SDRAM, CS2 = NAND.

### Peripheral registers used by the firmware (confirmed by trace)

| Register | Function |
|---|---|
| `0x7062`, `0x7070–0x707f` | PPU / display controller (enable, sprite-DMA source/dest/length) |
| `0x7300–0x73ff` | Display palette (RGB555) |
| `0x7400–0x77ff` | PPU sprite/attribute table (bank-selected by `0x707e`) |
| `0x7810` | Chip-select bank-switch |
| `0x7820–0x7824` | Chip-select region size/config (MCS0–MCS4) |
| `0x7850–0x7857` | NAND flash controller (command/address/data) |
| `0x7860–0x7881` | GPIO ports A–E (motor, sensors, eye-LCD lines) |
| `0x7942–0x7945` | SPI controller |
| `0x7a80–0x7a9f` | System DMA (4 channels × 8 parameters) |

---

## 2. External memory

- **NAND flash** — holds the firmware (`GameCode`) and a FAT32 filesystem of personality and
  asset files. The dump used here is OOB-stripped (512-byte pages). The NAND controller
  streams pages through data port `0x7854`.
- **SDRAM** — working memory for decoded graphics and audio. It is populated at runtime by
  the firmware's own NAND→SDRAM transfers, not by the boot ROM (see
  [`ARCHITECTURE.md`](ARCHITECTURE.md)).

---

## 3. Display

The Furby Connect presents one round **eye LCD**. The firmware composes each eye frame from a
cell-graphics format stored in flash; the GPL16258's tilemap PPU sprite path assembles the
128×128 eye from four 64×64 quarter-cels.

### On-flash eye-graphics format

| File | Contents |
|---|---|
| `*.CEL` | Pixels — 64×64 cels, `0xC00` bytes each; 3 bytes encode 4 six-bit palette indices (MSB-first). |
| `*.PAL` | Colors — 64-color RGB555 banks, `0x80` bytes each. |
| `*.SPR` | 16 animation playlists → frames; each frame = `[cel,pal ×4, 0xFFFF]` (four quarter-cels → one 128×128 eye). **Playlist 8 is the eye animation.** |

Each personality (`Base`, `Cat`, `DJ`, `Ninja`, `Pirate`, `PopStar`, `Princess`) carries its
own `CEL/PAL/SPR` set under `A:\Personalities\<name>\`. `Base` is the default generic eye.

---

## 4. Datasheets & primary sources

All bundled in [`docs/datasheets/`](datasheets/).

| File | What it is | Used for |
|---|---|---|
| `GPL16250_family_datasheet.pdf` | GeneralPlus GPL16250-family (GPL162004A / DSAE0033844) datasheet | Memory map, chip-select/bank registers, NAND & DMA controllers, PPU registers |
| `unSP_Programming_Tools_Manual.pdf` | Sunplus unSP programming-tools / toolchain manual | µ'nSP ISA, register model, calling convention, interrupt/vector layout |
| `GPL32611B_ProductBrief_related.pdf` | Product brief for a related GeneralPlus SoC (not the Furby part) | Cross-reference for shared peripheral blocks; **not authoritative for the GPL16258** |
| `furbish_dictionary.pdf` | Furby-language reference | Speech/personality context (not hardware) |

### Additional references (external)

- **MAME** `generalplus_gpl16250` — GPL-2.0 reference for the SoC's NAND bootstrap, CS/SDRAM
  layout, and PPU. Vendored locally under `ref/mame-unsp/` in the working tree.
- **Furby-ReConnect** (`furby.py`, l0ss/swarley) — documents the CEL/PAL/SPR graphics format.

> **Scope.** Sensor wiring, exact eye-LCD panel controller timing, and the audio codec's
> internals are not fully documented here because they are not yet confirmed from a primary
> source; they are omitted rather than guessed.
