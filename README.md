# Furby Connect Emulator

A **from-scratch emulator of the discontinued Furby Connect (2016)** that boots and
runs the toy's *real* firmware — emulating its GeneralPlus **GPL16258** SoC (a Sunplus
**µ'nSP** CPU + peripherals) instruction-by-instruction, mounting its on-flash FAT
filesystem, and driving its display hardware.

Built by reverse-engineering the datasheets and the firmware, cross-checked against
MAME's `generalplus_gpl16250` reference. No prior emulator of this toy existed.

> **Dumps not included.** The firmware (`GameCode.bin`) and NAND image are
> Hasbro-copyrighted and are **not** distributed here. You supply your own; the code
> is what's public.

---

## What works (accurate as of this commit)

| Subsystem | Status | Notes |
|---|---|---|
| µ'nSP CPU core (ISA 1.3 + EXTOP) | ✅ working | native C, validated bit-for-bit vs a pure-Python reference for 250k steps |
| Memory map + boot HLE | ✅ working | GameCode loads at machine `0x050000`, reset entry `0x050020` |
| Interrupts + timer + IRQ vectoring | ✅ working | vector table at RAM `0x6ff0`; real frame IRQ = **line 5** |
| NAND flash controller | ✅ working | reads byte-perfect (verified vs raw dump); 512B pages (OOB-stripped image) |
| System DMA (4ch) + banked window | ✅ working | NAND→RAM streaming, `0x7810` bank switching |
| FAT32 filesystem | ✅ working (HLE) | `find-file` / `open` / `read` resolved against the parsed FAT |
| Boot → wake → timekeeping → self-check | ✅ working | firmware runs its real startup all the way through |
| Display pipeline (PPU enable, palette, sprites) | ✅ **driven with real data** | `0x707f` enabled; **107 live RGB565 palette colors + sprite RAM** loaded from zero |
| Autonomous animation | ✅ working | driven by the real event interrupt (IRQ line 5) |
| **The eyes — decoded & rendered** | ✅ **working** | the display "PPU" — see below |
| Audio playback | 🔲 not started | |

### The eyes 👁️

**The Furby's eyes render, in true color.** ([Watch the live animated eye.](https://claude.ai/code/artifact/0ccd9cea-9bec-4858-8ee6-a1f7fb1f3643))

It turned out the Furby does **not** drive its two round eye-LCDs through the
GPL16258's standard sprite/tilemap PPU (those registers stay empty — confirmed by
frame-diffs, write-histograms and PPU snapshots: no framebuffer, no display DMA). It
plays **pre-rendered eye animations from flash** — a custom cell-graphics format we
reverse-engineered here (cross-checked against the WAHCKon *furbhax* teardown):

- **`.CEL`** — the pixels, as 6-bit palette indices, in 48×16-byte tiles (64×16 px);
  four stacked tiles make a 64×64 eye
- **`.PAL`** — the color tables, 64-color RGB555 banks (each personality/animation
  uses its own bank — Base's violet eye at bank 64, the default blue eye at 580)
- **`.SPR` / `.SEQ`** — cell placement + animation sequencing (partially decoded)

`emu/furby_display.py` decodes this and renders the eyes. Sample frames are in
[`eyes_sample/`](eyes_sample/); the live animated viewer is
[`furby_eye.html`](furby_eye.html).

```bash
python3 run.py --eyes /path/to/Personalities/Base --eyes-out eyes/
```

*Remaining:* the `.SEQ`/`.SPR` chain that maps each animation frame to its exact
palette isn't fully decoded, so per-personality palettes currently use verified
presets / a best-effort auto-detect rather than the firmware's own mapping.

---

## Architecture

Python orchestrates; the CPU runs in native C for speed.

```
emu/unsp_core.c      native µ'nSP CPU + peripherals (NAND, DMA, banked window,
                     interrupts, timer) + the filesystem/display HLE hooks
emu/unsp_native.py   ctypes binding + memory map, boot, and the default_furby_cpu()
                     one-call setup (this is the emulator you run)
emu/unsp_cpu.py      pure-Python reference core (used to validate the C core)
emu/unsp_disasm.py   µ'nSP ISA 1.3 disassembler
emu/unsp_trace.py    recursive-descent tracer
emu/gpl16250_regs.py peripheral register names
emu/furby_display.py the eye "PPU" — decodes the CEL/PAL cell-graphics into eye frames
run.py               friendly runner (boot + drive display + report; or --eyes to
                     dump a personality's eye animation)
furby_eye.html       standalone live viewer: the eye animating in true color
```

## Quick start

```bash
# 1. build the native core (needs a C compiler)
sh emu/build.sh

# 2. boot the firmware (supply your own dumps)
python3 run.py \
    --gamecode /path/to/GameCode.bin \
    --nand "/path/to/furby-nand (Fixed OOB Data).bin" \
    --palette-png eye_palette.png
```

Expected output: the firmware boots, the filesystem HLE resolves its files, the
display pipeline lights up (`0x707f` enabled, ~100+ palette colors, sprite RAM
populated), and `eye_palette.png` shows the live eye palette.

### Hacking on it

`emu/unsp_native.py`'s `default_furby_cpu()` is the entry point; the C core rebuilds
with `sh emu/build.sh`. The disassembler and tracer make it easy to explore the
firmware. `docs/` has the full hardware notes, the phase-by-phase build log, and the
`HANDOFF.md` deep-dive (root causes, the FAT/OOB analysis, the display path, and the
exact next steps for the compositor).

## Documentation

- [`docs/HANDOFF.md`](docs/HANDOFF.md) — the living deep-dive: everything working, every
  root cause, the display path, and the next milestone (most detailed)
- [`docs/HARDWARE.md`](docs/HARDWARE.md) — the GPL16258 / µ'nSP hardware
- [`docs/EMULATOR_PLAN.md`](docs/EMULATOR_PLAN.md) — the original plan
- [`docs/PHASE*_NOTES.md`](docs/) — the build log, phase by phase

## Credits & references

- **[Furby-ReConnect](https://github.com/Furby-ReConnect/Furby)** — extracted firmware/NAND files and prior RE
- **[MAME](https://www.mamedev.org/)** `generalplus_gpl16250` — authoritative gpac800 NAND/SoC reference (GPL-2.0)
- **[bluefluff](https://github.com/Jeija/bluefluff)** — Furby Connect BLE/update-format RE

## License

Emulator code: MIT (see `LICENSE`). Firmware/NAND dumps are **not** included and remain
the property of their respective owners.
