# Architecture — How the Emulator and Firmware Run

This document explains, in operational detail, how the emulator executes the Furby Connect's
firmware and how that firmware brings up its eye. It describes only mechanisms that are
confirmed in the implementation or by trace; where a behavior depends on hardware detail that
is not established from a primary source, it is called out explicitly rather than assumed.

---

## 1. Emulator structure

The emulator is split between a native core and a Python layer.

| Component | Role |
|---|---|
| `emu/unsp_core.c` | Native µ'nSP CPU + peripherals (SRAM/MMIO, NAND controller, system DMA, banked CS window, interrupts, timer) and the high-level-emulation (HLE) hooks. Compiled to `libunspcore.so`. |
| `emu/unsp_native.py` | `ctypes` binding to the core; the memory map, boot setup, and `default_furby_cpu()` — the one call that produces a ready-to-run machine. |
| `emu/unsp_cpu.py` | Pure-Python reference core, used to validate the native core (bit-for-bit over 250k steps). |
| `emu/unsp_disasm.py` | µ'nSP ISA disassembler. |
| `emu/furby_display.py` | Decoder for the CEL/PAL/SPR eye-graphics format (used to render captured frames). |
| `emu/furby_gui.py` | Desktop GUI (`run.py --gui`): loads a ROM, drives boot/wake, shows the live eye, a log, and a debug console. |

Execution model: `default_furby_cpu(gamecode, nand)` loads the firmware, wires the NAND
image to the controller, installs the HLE hooks, and resets to the boot entry. The caller
then advances the machine with `cpu.run(n)` (run `n` instructions) and delivers display-frame
interrupts with `cpu.raise_irq(5)`.

### High-level-emulation (HLE) hooks

A few firmware routines are replaced by native handlers. Each is a deliberate, documented
substitution for a subsystem whose real implementation depends on data the emulator does not
reconstruct (chiefly the flash translation layer that the OOB-stripped dump lacks):

| Hooked routine | Handler | Purpose |
|---|---|---|
| `0x078730` find-file | FAT32 name resolution | Resolve a path against the parsed filesystem. |
| `0x090f7f` open | VFS open | Allocate a handle for a resolved file. |
| `0x091c93` read-byte | VFS read | Return the next file byte (cluster-chain walk). |
| `0x0785de` load-file | **Resource loader** | Resolve a file and copy its bytes into the destination CS/SDRAM buffer. |

The **resource loader** hook is what makes the eye render. See §3.

---

## 2. Firmware boot sequence

1. **Reset.** GameCode is loaded at machine word address `0x050000` and execution begins at
   `0x050020`. The chip-select configuration registers (`0x7820–0x7824`) are initialized to
   the values the boot ROM would write.
2. **Self-check and filesystem.** The firmware verifies its image and mounts its FAT32
   filesystem, opening early configuration files (`Slots.STB`, `color.dat`, `FurbyData.dat`,
   …). The find-file/open/read HLE serves these from the NAND image.
3. **Main loop.** The firmware settles into its event loop (around `0x06cd8b`), driven by the
   display-frame interrupt on IRQ line 5.

## 3. Graphics resource loading (the critical step)

The GPL16250-family boot ROM copies **only** the firmware code block into RAM; it does **not**
load graphics into SDRAM (confirmed against MAME's bootstrap). Instead, the firmware loads its
own graphics at runtime, driven by a resource manifest.

- **Manifest.** A table in GameCode (`0x097766`, 0x1a words per group) maps a *group* to the
  filenames of its assets: `PAL, CEL, SPR, AMF, …`. Group 2 = the `Base` personality (the
  default eye).
- **Loader.** `load_group_graphics` (`0x07ed5b`) reads the manifest and, for each asset,
  calls the file loader `0x0785de(name, dest)`, which opens the file and block-reads it into
  a destination address in the banked SDRAM window. For `Base`:
  - `BASE.PAL` → `0x22def0`
  - `BASE.SPR` → `0x3b7610`
  - personality `CEL` tiles are streamed on demand into the render buffer at `0x232000`.

Because the destination lives in the banked SDRAM window, the emulator's HLE loader writes it
through the same address decode the render later uses to read it. With this in place the SDRAM
buffers hold real data; without it they read as uninitialized flash and the render consumes
garbage.

## 4. Eye composition and render

Once the graphics are resident:

1. The firmware selects an animation from the `SPR` playlists. **Playlist 8** is the eye
   animation (14 frames). Frame 0 references cels 5, 6, 7, 8.
2. It writes the frame's four quarter-cel tile indices into the PPU sprite table
   (`0x7400+`) and loads the corresponding RGB555 palette into the palette registers
   (`0x7300+`).
3. It kicks the PPU sprite/display DMA (`0x7070`=source, `0x7071`=dest, writing `0x7072`
   triggers the transfer) and the display controller composites the 128×128 eye.

The emulator observes this directly: after boot the running firmware writes sprite tiles
`5,6,7,8` and a 63-color palette, and the palette it loads is the `Base` blue-eye bank. The
rendered frame — reconstructed from the firmware's own selected tiles and loaded palette — is
the generic Furby Connect eye. `tools/render_live_eye.py` reproduces this and also exports the
full 14-frame animation.

---

## 5. Reproducing the eye

```bash
python3 tools/render_live_eye.py \
    --gamecode /path/to/GameCode.bin \
    --nand     /path/to/furby-nand.bin \
    --png eye.png --gif eye.gif
```

This boots the firmware, lets it load and select its eye animation, reads the tiles and
palette it produced, and writes a still (`eye.png`) plus the animation (`eye.gif`).

---

## 6. Known-modeled vs. deliberately-omitted

**Modeled and confirmed:** µ'nSP CPU, memory map, interrupts/timer, NAND controller, system
DMA, banked CS/SDRAM window (write-tracked), FAT32 (via HLE), the resource loader, PPU sprite
path, palette, and the eye graphics format.

**Deliberately omitted** (not established from a primary source, therefore not guessed):

- The exact sensor/BLE inputs that drive the *retail* wake transition. The eye pipeline is
  exercised by driving the display-frame interrupt and the behavior state; the specific
  real-world wake event is not modeled.
- The audio SACM codec's decode (the container is parsed; PCM decode is not implemented).
- Precise eye-LCD panel controller timing.

These boundaries are intentional: the emulator reproduces what is verifiable and marks the
rest, rather than presenting unconfirmed behavior as fact.
