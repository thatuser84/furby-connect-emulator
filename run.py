#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Furby Connect emulator — friendly runner.

Boots the real Furby Connect firmware on the from-scratch GPL16258 (µ'nSP) core,
mounts its FAT filesystem (via HLE), drives the display pipeline on the real
frame interrupt, and reports what the machine does — including the live eye
palette, which it can export as a PNG.

You supply your own dumps (not distributed — Hasbro copyright):
    GameCode.bin                    the firmware image
    furby-nand (...).bin            the full NAND flash image

Usage:
    python3 run.py --gamecode path/to/GameCode.bin --nand path/to/furby-nand.bin
    python3 run.py ... --palette-png eye_palette.png     # export the loaded palette
    python3 run.py ... --frames 8                        # number of display frames to drive

The native core is auto-built (needs a C compiler + `sh emu/build.sh`).
"""
from __future__ import annotations
import argparse, os, struct, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
EMU = os.path.join(HERE, "emu")
sys.path.insert(0, EMU)


def ensure_built():
    so = os.path.join(EMU, "libunspcore.so")
    if not os.path.exists(so):
        print("[build] compiling native core (emu/build.sh) ...")
        subprocess.run(["sh", os.path.join(EMU, "build.sh")], check=True, cwd=EMU)


def rgb565(v):
    return ((v >> 11 & 31) * 255 // 31, (v >> 5 & 63) * 255 // 63, (v & 31) * 255 // 31)


def write_png(path, width, height, pixels, scale=6):
    """Minimal dependency-free PNG writer (8-bit RGB)."""
    import zlib
    def chunk(typ, data):
        c = typ + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            r, g, b = pixels[y * width + x]
            row += bytes([r, g, b]) * scale
        for _ in range(scale):
            rows.append(b"\x00" + bytes(row))
    ihdr = struct.pack(">IIBBBBB", width * scale, height * scale, 8, 2, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", zlib.compress(b"".join(rows), 9)))
        f.write(chunk(b"IEND", b""))


def main():
    ap = argparse.ArgumentParser(description="Boot the Furby Connect firmware in the emulator.")
    ap.add_argument("--gamecode", help="path to GameCode.bin")
    ap.add_argument("--nand", help="path to the full NAND image")
    ap.add_argument("--boot-insns", type=int, default=600_000_000, help="instructions to run for boot")
    ap.add_argument("--frames", type=int, default=8, help="display frames to drive (IRQ line 5)")
    ap.add_argument("--palette-png", default=None, help="export the loaded eye palette to this PNG")
    ap.add_argument("--eyes", metavar="PERSONALITY_DIR",
                    help="decode & dump a personality's eye animation (the display 'PPU') and exit")
    ap.add_argument("--eyes-out", default="eyes", help="output dir for --eyes frames")
    ap.add_argument("--monitor", action="store_true", help="live terminal monitor of the running emulator")
    args = ap.parse_args()

    # --eyes: run the display PPU (no firmware boot needed)
    if args.eyes:
        import furby_display
        furby_display.dump_eyes(args.eyes, args.eyes_out, palette=None, auto=True,
                                count=48, stride=furby_display.TILE_BYTES * furby_display.EYE_TILES)
        return
    if not args.gamecode or not args.nand:
        ap.error("--gamecode and --nand are required (unless using --eyes)")

    if args.monitor:
        import furby_monitor, sys
        sys.argv = ["furby_monitor", "--gamecode", args.gamecode, "--nand", args.nand]
        furby_monitor.main()
        return

    ensure_built()
    import unsp_native as NAT

    print(f"[load] firmware: {args.gamecode}")
    print(f"[load] NAND:     {args.nand}")
    img = open(args.gamecode, "rb").read()
    nand = open(args.nand, "rb").read()

    cpu = NAT.default_furby_cpu(img, nand_bytes=nand)   # filesystem HLE + display sync baked in
    cpu.add_hle(0x090f7f, 4)   # open()  -> handle
    cpu.add_hle(0x091c93, 5)   # read()  -> file bytes

    print(f"[boot] running {args.boot_insns:,} instructions ...")
    cpu.run(args.boot_insns)
    print(f"[boot] settled at LPC 0x{cpu.lpc():06x}  (find-file HLE calls: {cpu.hle_calls(0)})")

    # bring up the timer + display sync, then drive the real frame interrupt (line 5)
    cpu.set_timer(1, 20000)
    cpu.set_timer_status(0x78a0, 0x80)
    cpu.set_readclear(0x78a0, 0x80)
    print(f"[disp] driving {args.frames} frame interrupts (IRQ line 5) ...")
    for _ in range(args.frames):
        cpu.raise_irq(5)
        cpu.run(90_000_000)

    pal = [cpu.mmio_last(0x7300 + i) for i in range(256)]
    nz = sum(1 for v in pal if v)
    ppu_en = cpu.mmio_writes(0x707f)
    spr = sum(cpu.mmio_writes(0x7400 + i) for i in range(256))
    print("\n=== display state ===")
    print(f"  PPU enable (0x707f) writes : {ppu_en}")
    print(f"  palette colors loaded      : {nz}/256")
    print(f"  sprite-RAM writes          : {spr}")
    print("  status                     : "
          + ("PPU driven, eyes rendering (hardware compositing)" if ppu_en and spr else "display not reached"))

    if args.palette_png:
        pixels = [rgb565(v) for v in pal]
        write_png(args.palette_png, 16, 16, pixels, scale=16)
        print(f"\n[png] wrote the loaded palette to {args.palette_png}")


if __name__ == "__main__":
    main()
