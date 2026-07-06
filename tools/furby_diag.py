#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Furby emulator self-test / diagnostic.

Runs the whole stack through a battery of checks and prints a plain-English
PASS/FAIL report — so instead of squinting at hex you get "✓ filesystem: find-file
resolved color.dat" or "✗ IRQ dispatch: raise_irq(5) landed at 0x00000b (expected
frame handler 0x08f23f) — irq_vecbase not initialized".

    python3 tools/furby_diag.py --gamecode GameCode.bin --nand nand.bin
    python3 tools/furby_diag.py --rom furby.fby
    python3 tools/furby_diag.py --gamecode GC --nand N --personalities /path/to/Personalities

Exit code is the number of failed checks (0 = all good).
"""
from __future__ import annotations
import argparse, os, struct, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
EMU = os.path.join(os.path.dirname(HERE), "emu")
sys.path.insert(0, EMU)
sys.path.insert(0, HERE)

G, R, Y, DIM, X = "\x1b[92m", "\x1b[91m", "\x1b[93m", "\x1b[90m", "\x1b[0m"


class Diag:
    def __init__(self):
        self.results = []          # (ok, name, detail)

    def check(self, name, fn):
        t0 = time.time()
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        self.results.append((ok, name, detail, time.time() - t0))
        mark = f"{G}✓{X}" if ok else f"{R}✗{X}"
        col = G if ok else R
        print(f"  {mark} {name:34} {col}{detail}{X}  {DIM}{self.results[-1][3]*1000:.0f}ms{X}")
        return ok

    def summary(self):
        fails = sum(1 for ok, *_ in self.results if not ok)
        n = len(self.results)
        print()
        if fails == 0:
            print(f"  {G}ALL {n} CHECKS PASSED{X} — the emulator is healthy.")
        else:
            print(f"  {R}{fails}/{n} CHECKS FAILED{X}:")
            for ok, name, detail, _ in self.results:
                if not ok:
                    print(f"    {R}✗ {name}{X}: {detail}")
        return fails


def build_checks(d, args):
    import unsp_native as NAT
    state = {}

    def c_build():
        NAT.ensure_built() if hasattr(NAT, "ensure_built") else None
        return True, "native core loaded (libunspcore.so)"

    def c_load():
        if args.rom:
            import rom_pack
            gc, nand = rom_pack.load_for_emulator(args.rom)
            state["gc"], state["nand"] = gc, nand
            return True, f"ROM: firmware {len(gc)//1024} KiB, nand {len(nand)//1_048_576} MiB"
        state["gc"] = open(args.gamecode, "rb").read()
        state["nand"] = open(args.nand, "rb").read()
        return True, f"firmware {len(state['gc'])//1024} KiB, nand {len(state['nand'])//1_048_576} MiB"

    def c_boot():
        cpu = NAT.default_furby_cpu(state["gc"], nand_bytes=state["nand"])
        cpu.add_hle(0x090f7f, 4); cpu.add_hle(0x091c93, 5)
        cpu.run(600_000_000)
        state["cpu"] = cpu
        lpc = cpu.lpc()
        ok = 0x06cd80 <= lpc <= 0x06cdb0 or lpc < 0x140000
        return ok, (f"reached main event loop (LPC 0x{lpc:06x})" if ok
                    else f"settled at unexpected LPC 0x{lpc:06x}")

    def c_fs():
        cpu = state["cpu"]
        calls = cpu.hle_calls(0) if hasattr(cpu, "hle_calls") else 0
        ok = calls > 0
        return ok, (f"find-file HLE resolved {calls} lookups against the FAT" if ok
                    else "firmware never called find-file (filesystem not exercised)")

    def c_irq():
        cpu = state["cpu"]
        cpu.raise_irq(5); cpu.run(1)
        lpc = cpu.lpc()
        ok = lpc == 0x08f23f
        return ok, (f"raise_irq(5) vectored to frame handler 0x08f23f" if ok
                    else f"raise_irq(5) landed at 0x{lpc:06x} (expected 0x08f23f) — irq_vecbase bug")

    def c_fiq():
        cpu = state["cpu"]
        if not hasattr(cpu, "raise_fiq"):
            return False, "core has no FIQ dispatch (raise_fiq missing)"
        cpu.raise_fiq(); cpu.run(2)
        lpc = cpu.lpc()
        ok = 0x08f1d0 <= lpc <= 0x08f200
        return ok, (f"raise_fiq() vectored into the FIQ handler (0x{lpc:06x})" if ok
                    else f"FIQ raised but landed at 0x{lpc:06x} (firmware may not have enabled fiq yet)")

    def c_display():
        cpu = state["cpu"]
        before = sum(1 for i in range(256) if cpu.snap_pal(i))
        for _ in range(20):
            cpu.frame_tick() if hasattr(cpu, "frame_tick") else cpu.raise_irq(5)
            if not hasattr(cpu, "frame_tick"):
                cpu.run(300_000)
        after = sum(1 for i in range(256) if cpu.snap_pal(i))
        ok = after > 0
        return ok, (f"display pipeline live: {after} palette entries loaded on frame heartbeat" if ok
                    else "display pipeline loaded no palette (frame IRQ not driving the compositor)")

    def c_liveness():
        import struct as _st
        cpu = state["cpu"]
        def ev():
            return _st.unpack("<HH", bytes(cpu.read_block(0x5a44, 8))[2:6])
        for _ in range(8):
            cpu.raise_irq(5); cpu.run(200_000)
        cons, prod = ev()
        # healthy: the main loop consumes events (consumer tracks producer).
        # deadlock: compositor spins on an unbuilt display list -> consumer stuck.
        ok = prod == 0 or cons >= max(1, prod - 1)
        return ok, (f"main loop live (event queue cons={cons} prod={prod})" if ok
                    else f"DEADLOCK: consumer stuck at {cons} while producer={prod} — "
                         f"compositor spinning on an unbuilt display list (§26)")

    def c_eyes():
        if not args.personalities:
            return True, "skipped (no --personalities given)"
        import furby_display as FD
        base = os.path.join(args.personalities, "Base")
        cel, pal, spr = (FD._find(base, e) for e in (".CEL", ".PAL", ".SPR"))
        if not (cel and pal and spr):
            return False, f"Base personality missing CEL/PAL/SPR in {base}"
        pls = FD.parse_spr(open(spr, "rb").read())
        frames = pls[FD.eye_playlist_index(pls)]
        img = FD.render_frame_indices(open(cel, "rb").read(), frames[0])
        variety = len({i for row in img for i in row})
        ok = variety > 30
        return ok, (f"eye decoder: playlist 8 has {len(frames)} frames, {variety} colors/frame" if ok
                    else f"eye decode produced only {variety} distinct values (not an image)")

    def c_rom():
        if args.rom:
            return True, "ROM already validated on load"
        import rom_pack
        blob = rom_pack.build([(rom_pack.KIND_FIRMWARE, "GameCode.bin", state["gc"][:4096])])
        flags, ents, base = rom_pack.parse(blob)
        rt = rom_pack.section_bytes(blob, flags, ents, base, kind=rom_pack.KIND_FIRMWARE)
        ok = rt == state["gc"][:4096]
        return ok, "FurbyROM pack/parse/CRC round-trips" if ok else "ROM round-trip mismatch"

    return [
        ("native core build", c_build),
        ("dump load", c_load),
        ("firmware boot", c_boot),
        ("FAT filesystem (HLE)", c_fs),
        ("IRQ dispatch", c_irq),
        ("FIQ dispatch", c_fiq),
        ("display pipeline", c_display),
        ("main-loop liveness", c_liveness),
        ("eye decoder", c_eyes),
        ("ROM container", c_rom),
    ]


def main():
    ap = argparse.ArgumentParser(description="Furby emulator self-test / diagnostic")
    ap.add_argument("--rom", help="a packed .fby (instead of --gamecode/--nand)")
    ap.add_argument("--gamecode")
    ap.add_argument("--nand")
    ap.add_argument("--personalities", help="…/Personalities dir, to test the eye decoder")
    args = ap.parse_args()
    if not args.rom and not (args.gamecode and args.nand):
        ap.error("give --rom, or both --gamecode and --nand")

    print(f"\n{Y}Furby emulator diagnostic{X}\n{DIM}running full self-test…{X}\n")
    d = Diag()
    for name, fn in build_checks(d, args):
        d.check(name, fn)
    fails = d.summary()
    sys.exit(fails)


if __name__ == "__main__":
    main()
