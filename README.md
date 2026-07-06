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
| Behavior/personality state machine | ✅ working | dispatcher `0x06158b`, state var `[0x4e8c]`, wake/animation selection |
| Graphics resource loader (NAND→SDRAM) | ✅ working | HLE of the firmware's file→SDRAM load; populates the eye buffers |
| **Live eye — firmware composes & renders it** | ✅ **working** | firmware selects playlist 8, writes PPU sprite tiles + palette, composes the 128×128 eye — see below |
| Offline eye decoder | ✅ working | `emu/furby_display.py` decodes CEL/PAL/SPR to PNG/GIF for any personality |
| NAND FTL — boot from raw dump | ✅ working | `run.py --nand-raw` reconstructs the logical image from a raw physical dump (+OOB), byte-exact, and boots the firmware on it |
| Audio megafile unpack | ✅ working | `.AMF` cracked → 1584 clips exported as `.a18` (`tools/amf_extract.py`) |
| Audio SACM → PCM decode | 🔬 frontier | proprietary entropy-coded codec; container done, PCM decode open |
| Single-file **FurbyROM (.fby)** | ✅ working | pack GameCode + NAND into one compressed file; `run.py --rom` boots it |
| Self-test / **diagnostic** | ✅ working | `run.py --diag` runs every subsystem and reports plain-English PASS/FAIL |
| **Desktop GUI** | ✅ working | `run.py --gui` — open a ROM, it boots & runs; live eye viewport, log, and a debug console for custom instructions |

### The eye 👁️

**The real firmware composes and renders its own eye, live.**

![The Furby Connect eye, composed by its running firmware](docs/images/furby_eye_LIVE.png)

The emulated GPL16258 boots the firmware, which mounts its filesystem, runs its behavior
state machine, loads its graphics into SDRAM, selects the eye animation (playlist 8), and
composes the 128×128 eye through its PPU sprite path — all from the running firmware. The
frame above is rendered from the tiles the firmware selected and the palette it loaded; the
full 14-frame blink is in [`docs/images/furby_eye_anim.gif`](docs/images/furby_eye_anim.gif).

```bash
# boot the firmware and render its live eye (still + animation)
python3 tools/render_live_eye.py --gamecode GameCode.bin --nand nand.bin --png eye.png --gif eye.gif
```

### GUI

A plain desktop front-end (like an NES emulator — no toy chrome):

```bash
python3 run.py --gui
```

Open a ROM (a `.fby`, or a `GameCode.bin` + its NAND), then **Boot** and **Wake + Render**.
The window shows the live eye, a scrolling log, and a debug console (`peek`/`poke`/`run`/
`frame`/`dis`/… — type `help`) for running custom instructions against the live machine.

The eye-graphics format (`CEL`/`PAL`/`SPR`) and the runtime load path are documented in
[`docs/HARDWARE.md`](docs/HARDWARE.md) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). An
offline decoder (`emu/furby_display.py`, `run.py --eyes`) can also dump any personality's eye
animation directly from its files.

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
emu/furby_gui.py     desktop GUI emulator (open ROM -> runs; live eye, log, debug console)
run.py               friendly runner (--gui for the desktop app; --eyes to dump an
                     eye animation; boot + drive display + report otherwise)
furby_eye.html       standalone live viewer: the eye animating in true color
tools/ftl_reconstruct.py  rebuild the logical NAND from a raw physical dump (+OOB), byte-exact
tools/amf_extract.py      unpack a personality's .AMF audio megafile into .a18 clips
tools/rom_pack.py         pack GameCode + NAND into one compressed FurbyROM (.fby)
tools/furby_diag.py       self-test: boots + checks every subsystem, readable PASS/FAIL
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

Expected output: the firmware boots, the filesystem HLE resolves its files, the graphics
resource loader populates SDRAM, and the firmware composes its eye through the PPU (sprite
tiles + palette). Use `tools/render_live_eye.py` (above) to render the composed eye.

### Hacking on it

`emu/unsp_native.py`'s `default_furby_cpu()` is the entry point; the C core rebuilds with
`sh emu/build.sh`. The disassembler (`emu/unsp_disasm.py`) and tracer make it easy to explore
the firmware. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for how boot, resource
loading, and eye composition work.

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how the emulator and firmware run: boot,
  the HLE hooks, graphics resource loading, and live eye composition
- [`docs/HARDWARE.md`](docs/HARDWARE.md) — the GPL16258 / µ'nSP silicon, memory map, display
  format, and the bundled datasheets ([`docs/datasheets/`](docs/datasheets/))
- [`docs/REVERSE_ENGINEERING_LOG.md`](docs/REVERSE_ENGINEERING_LOG.md) — the full
  chronological RE record (how each subsystem was reverse-engineered; historical)

## Credits & references

- **[Furby-ReConnect](https://github.com/Furby-ReConnect/Furby)** — extracted firmware/NAND files, and `furby.py` (l0ss/swarley) which documents the DLC/CEL/SPR/PAL graphics format used by the eye PPU
- **[MAME](https://www.mamedev.org/)** `generalplus_gpl16250` — authoritative gpac800 NAND/SoC reference (GPL-2.0)
- **[bluefluff](https://github.com/Jeija/bluefluff)** — Furby Connect BLE/update-format RE

## License

Emulator code: MIT (see `LICENSE`). Firmware/NAND dumps are **not** included and remain
the property of their respective owners.
