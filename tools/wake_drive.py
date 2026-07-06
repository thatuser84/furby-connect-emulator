#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Drive the Furby firmware through its real wake sequence (docs/HANDOFF.md §29–§30).

For most of this project the firmware booted then sat awake-but-idle. This script opens
the three gates that keep it idle and shows the *real firmware* march its behavior state
machine and drive the display pipeline live:

  1. clamp the compositor's absurd display-list child-counts (HLE id=6) -> breaks the
     unbuilt-list deadlock so the state machine can advance;
  2. supply a wake reason ([0x534f]) -> state 2 -> 3 -> 4;
  3. emulate the eye-LCD controller busy/ready status (0x7961) -> state 4 finishes its
     SPI transfer and reaches state 5.

Reports the state march + display activity. (The pixel *content* is still a format-offset
frontier — see §30.)

    python3 tools/wake_drive.py --gamecode GameCode.bin --nand nand.bin
"""
from __future__ import annotations
import argparse, os, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "emu"))


def main():
    ap = argparse.ArgumentParser(description="Drive the firmware's real wake sequence")
    ap.add_argument("--gamecode", required=True)
    ap.add_argument("--nand", required=True)
    ap.add_argument("--wake", type=lambda x: int(x, 0), default=1, help="wake reason for [0x534f]")
    a = ap.parse_args()
    import unsp_native as NAT

    img = open(a.gamecode, "rb").read()
    nand = open(a.nand, "rb").read()
    cpu = NAT.default_furby_cpu(img, nand_bytes=nand)

    cpu.add_hle(0x08fc17, 6)                       # gate 1: compositor count-cap
    print("[boot] running firmware ...")
    cpu.run(600_000_000)

    def rd(x): return struct.unpack("<H", bytes(cpu.read_block(x, 4))[:2])[0]
    def state(): return rd(0x4e8c)

    cpu.set_autoclear(0x7961, 0x30)               # gate 3: eye-LCD busy bits clear
    cpu.set_reador(0x7961, 0x80)                   #         eye-LCD ready bit set
    for _ in range(10):
        cpu.raise_irq(5); cpu.run(400_000)
    print(f"[wake] firmware idle in state {state()}; injecting wake reason [0x534f]={a.wake}")
    cpu.poke(0x534f, a.wake)                       # gate 2: wake reason

    seen, spi0 = [], cpu.mmio_writes(0x7942)
    for _ in range(24):
        cpu.raise_irq(5); cpu.run(1_000_000)
        s = state()
        if not seen or seen[-1] != s:
            seen.append(s)
    pal = sum(1 for i in range(256) if cpu.mmio_last(0x7300 + i))
    spi = cpu.mmio_writes(0x7942) - spi0
    print(f"[march] behavior state sequence: {' -> '.join(map(str, seen))}")
    print(f"[disp]  palette entries loaded : {pal}")
    print(f"[disp]  SPI-TX (eye-LCD) writes: {spi}")
    print(f"[disp]  PPU-enable (0x707f)    : {cpu.mmio_writes(0x707f)}")
    ok = seen and seen[-1] >= 4 and spi > 0
    print("\n" + ("✓ firmware drove its wake sequence + display pipeline live"
                  if ok else "✗ wake sequence did not reach the display transfer"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
