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
| **The eyes — decoded & rendered** | ✅ **working** | the display "PPU" + animated-GIF export — see below |
| NAND FTL — boot from raw dump | ✅ working | `run.py --nand-raw` reconstructs the logical image from a raw physical dump (+OOB), byte-exact, and boots the firmware on it |
| Audio megafile unpack | ✅ working | `.AMF` cracked → 1584 clips exported as `.a18` (`tools/amf_extract.py`) |
| Audio SACM → PCM decode | 🔬 frontier | proprietary entropy-coded codec; container done, PCM decode open |
| Single-file **FurbyROM (.fby)** | ✅ working | pack GameCode + NAND into one compressed file; `run.py --rom` boots it |

### The eyes 👁️

**The Furby's eyes render, in true color.** ([Watch the live animated eye.](https://claude.ai/code/artifact/0ccd9cea-9bec-4858-8ee6-a1f7fb1f3643))

It turned out the Furby does **not** drive its two round eye-LCDs through the
GPL16258's standard sprite/tilemap PPU (those registers stay empty — confirmed by
frame-diffs, write-histograms and PPU snapshots: no framebuffer, no display DMA). It
plays **pre-rendered eye animations from flash** — a custom cell-graphics format we
reverse-engineered here (cross-checked against the WAHCKon *furbhax* teardown):

- **`.CEL`** — the pixels: 64×64 cels (0xC00 bytes each), 3 bytes → 4 six-bit palette
  indices, MSB-first
- **`.PAL`** — the color tables: 64-color RGB555 banks (0x80 bytes each)
- **`.SPR`** — **16 animation playlists → frames**; each frame is `[cel0,pal0, cel1,pal1,
  cel2,pal2, cel3,pal3, 0xFFFF]` — four 64×64 quarter-cels laid TL/TR/BL/BR into one
  **128×128** eye. **Playlist 8 is the eye animation.**

`emu/furby_display.py` decodes this and renders each personality's real eye animation,
in the firmware's own frame order, to PNG frames + an animated GIF. Sample animation is
in [`eyes_sample/`](eyes_sample/); the live viewer is [`furby_eye.html`](furby_eye.html).

```bash
python3 run.py --eyes /path/to/Personalities/Base --gif base_eye.gif
```

*Nicety left:* the palette handle inside each frame is a fixed value (`0x10F2`) resolved
by the firmware, so per-personality color uses a verified preset (Base) or a
colorful-and-smooth auto-detect for the rest — the **shapes and animation are exact for
all 7 personalities**. Formats cross-checked against Furby-ReConnect's `furby.py`.

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
tools/ftl_reconstruct.py  rebuild the logical NAND from a raw physical dump (+OOB), byte-exact
tools/amf_extract.py      unpack a personality's .AMF audio megafile into .a18 clips
tools/rom_pack.py         pack GameCode + NAND into one compressed FurbyROM (.fby)
```

### Tools

```bash
# rebuild the logical filesystem image from a raw physical NAND dump (with OOB)
python3 tools/ftl_reconstruct.py --raw NANDmainFLASH.BIN --logical known-good.bin --rebuild out.bin

# unpack a personality's speech library (exports GeneralPlus .a18 clips)
python3 tools/amf_extract.py /path/to/Personalities/Base/Base.AMF --out clips/

# pack everything into one compressed .fby ROM, then boot from it
python3 tools/rom_pack.py build --gamecode GameCode.bin --nand nand.bin --out furby.fby
python3 run.py --rom furby.fby

# boot straight from a raw physical NAND dump (FTL-reconstruct then boot)
python3 run.py --gamecode GameCode.bin --nand-raw NANDmainFLASH.BIN --nand-ref known-good.bin

# dump / animate a personality's eyes
python3 run.py --eyes /path/to/Personalities/Base --gif base_eye.gif
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

- **[Furby-ReConnect](https://github.com/Furby-ReConnect/Furby)** — extracted firmware/NAND files, and `furby.py` (l0ss/swarley) which documents the DLC/CEL/SPR/PAL graphics format used by the eye PPU
- **[MAME](https://www.mamedev.org/)** `generalplus_gpl16250` — authoritative gpac800 NAND/SoC reference (GPL-2.0)
- **[bluefluff](https://github.com/Jeija/bluefluff)** — Furby Connect BLE/update-format RE

## License

Emulator code: MIT (see `LICENSE`). Firmware/NAND dumps are **not** included and remain
the property of their respective owners.
