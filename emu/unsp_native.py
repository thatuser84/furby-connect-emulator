#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ctypes binding to the native µ'nSP core (libunspcore.so, built from unsp_core.c).

Python stays the orchestrator: this wrapper loads the firmware, configures the
few peripheral quirks, runs the core at native speed, and exposes registers /
memory / MMIO counters for the GUI, debugger and tracer. Same surface as the
pure-Python CPU so callers can swap backends.
"""

from __future__ import annotations

import ctypes
import os

SP, R1, R2, R3, R4, BP, SR, PC = range(8)
_N, _Z, _S, _C = 0x0200, 0x0100, 0x0080, 0x0040

_here = os.path.dirname(os.path.abspath(__file__))
_libpath = os.path.join(_here, "libunspcore.so")
if not os.path.exists(_libpath):
    raise RuntimeError(f"{_libpath} not found — run emu/build.sh first")
_lib = ctypes.CDLL(_libpath)

_p = ctypes.c_void_p
_u16 = ctypes.c_uint16
_u32 = ctypes.c_uint32
_u64 = ctypes.c_uint64
_sigs = {
    "cpu_new": (_p, []),
    "cpu_free": (None, [_p]),
    "cpu_reset": (None, [_p, _u32, _u32]),
    "cpu_load": (None, [_p, ctypes.c_char_p, _u32]),
    "cpu_load_at": (None, [_p, _u32, ctypes.c_char_p, _u32]),
    "cpu_load_nand": (None, [_p, ctypes.c_char_p, _u32]),
    "cpu_set_nand_page_size": (None, [_p, _u32]),
    "cpu_set_nand_oob_emul": (None, [_p, ctypes.c_int]),
    "cpu_wrhist_enable": (None, [_p]),
    "cpu_wrhist_reset": (None, [_p]),
    "cpu_wrhist_get": (_u32, [_p, _u32]),
    "cpu_wlog_set": (None, [_p, _u32]),
    "cpu_wlog_n": (_u32, [_p]),
    "cpu_wlog_get": (_u16, [_p, _u32]),
    "cpu_wlog_reset": (None, [_p]),
    "cpu_spriteram_get": (_u16, [_p, _u32]),
    "cpu_snap_valid": (ctypes.c_int, [_p]),
    "cpu_snap_spr": (_u16, [_p, _u32]),
    "cpu_snap_pal": (_u16, [_p, _u32]),
    "cpu_snap_reg": (_u16, [_p, _u32]),
    "cpu_palwpc_n": (_u32, [_p]),
    "cpu_palwpc_get": (_u32, [_p, _u32]),
    "cpu_nand_reads": (_u64, [_p]),
    "cpu_bootcopy": (None, [_p, _u32, _u32, _u32]),
    "cpu_set_ready": (None, [_p, _u32, _u16]),
    "cpu_set_autoclear": (None, [_p, _u32, _u16]),
    "cpu_set_csram_words": (None, [_p, _u32]),
    "cpu_set_reador": (None, [_p, _u32, _u16]),
    "cpu_set_readclear": (None, [_p, _u32, _u16]),
    "cpu_mmio_reads": (_u32, [_p, _u32]),
    "cpu_mmio_writes": (_u32, [_p, _u32]),
    "cpu_mmio_last": (_u16, [_p, _u32]),
    "cpu_getreg": (_u16, [_p, ctypes.c_int]),
    "cpu_setreg": (None, [_p, ctypes.c_int, _u16]),
    "cpu_getsb": (_u32, [_p]),
    "cpu_halted": (ctypes.c_int, [_p]),
    "cpu_insns": (_u64, [_p]),
    "cpu_lpc": (_u32, [_p]),
    "cpu_peek": (_u16, [_p, _u32]),
    "cpu_read_block": (None, [_p, _u32, _u32, ctypes.POINTER(_u16)]),
    "cpu_poke": (None, [_p, _u32, _u16]),
    "cpu_run": (_u64, [_p, _u64]),
    "cpu_step": (None, [_p]),
    "cpu_set_cs_trap": (None, [_p, _u32]),
    "cpu_add_hle": (ctypes.c_int, [_p, _u32, ctypes.c_int]),
    "cpu_clear_hle": (None, [_p]),
    "cpu_hle_calls": (_u64, [_p, ctypes.c_int]),
    "cpu_set_hle_debug": (None, [_p, ctypes.c_int]),
    "cpu_run_until": (_u64, [_p, _u32, _u64]),
    "cpu_add_watch": (ctypes.c_int, [_p, _u32]),
    "cpu_clear_watch": (None, [_p]),
    "cpu_watch_hits": (_u64, [_p, ctypes.c_int]),
    "cpu_nlog_n": (_u32, [_p]),
    "cpu_nlog_get": (_u32, [_p, _u32, ctypes.c_int]),
    "cpu_nlog_reset": (None, [_p]),
    "cpu_set_timer": (None, [_p, ctypes.c_int, _u64]),
    "cpu_set_timer_status": (None, [_p, _u32, _u16]),
    "cpu_raise_irq": (None, [_p, ctypes.c_int]),
    "cpu_raise_fiq": (None, [_p]),
    "cpu_set_fiq_vec": (None, [_p, _u32]),
    "cpu_irq_taken": (_u64, [_p]),
    "cpu_trapped": (ctypes.c_int, [_p]),
    "cpu_trap_from": (_u32, [_p]),
    "cpu_trap_to": (_u32, [_p]),
}
for name, (res, args) in _sigs.items():
    fn = getattr(_lib, name)
    fn.restype = res
    fn.argtypes = args


# GPL16258 address map (from MAME gpl16250_nand bootstrap + empirical proof):
#   0x000000-0x006FFF  internal RAM (SRAM)   — populated by the init copy
#   0x007000-0x007FFF  peripherals (MMIO)
#   0x050000+          the loaded GameCode image (boot dest, header byte 0x15/0x16)
#                      machine 0x050000+i == file word i ; reset entry = 0x050020
# Verified: main->file 0x82, second-init 0x08465a->file 0x03465a (clean code).
ROM_BASE = 0x050000
RESET_ENTRY = 0x0020        # + ROM_BASE via CS=0x05  -> LPC 0x050020


class NativeCPU:
    def __init__(self, image_bytes=None, entry=0x20, cs=0, flat=True):
        self.c = _lib.cpu_new()
        if image_bytes is not None and flat:
            _lib.cpu_load(self.c, bytes(image_bytes), len(image_bytes))
        _lib.cpu_reset(self.c, entry, cs)

    def load_at(self, dest_word, data):
        _lib.cpu_load_at(self.c, dest_word, bytes(data), len(data))
    def load_nand(self, data):
        _lib.cpu_load_nand(self.c, bytes(data), len(data))
    def set_nand_page_size(self, sz):
        _lib.cpu_set_nand_page_size(self.c, sz)
    def set_nand_oob_emul(self, on):
        _lib.cpu_set_nand_oob_emul(self.c, 1 if on else 0)
    def wrhist_enable(self): _lib.cpu_wrhist_enable(self.c)
    def wrhist_reset(self): _lib.cpu_wrhist_reset(self.c)
    def wrhist_get(self, blk): return _lib.cpu_wrhist_get(self.c, blk)
    def wlog_set(self, addr): _lib.cpu_wlog_set(self.c, addr)
    def wlog_n(self): return _lib.cpu_wlog_n(self.c)
    def wlog_get(self, i): return _lib.cpu_wlog_get(self.c, i)
    def wlog_reset(self): _lib.cpu_wlog_reset(self.c)
    def spriteram_get(self, i): return _lib.cpu_spriteram_get(self.c, i)
    def snap_valid(self): return bool(_lib.cpu_snap_valid(self.c))
    def snap_spr(self, i): return _lib.cpu_snap_spr(self.c, i)
    def snap_pal(self, i): return _lib.cpu_snap_pal(self.c, i)
    def snap_reg(self, i): return _lib.cpu_snap_reg(self.c, i)
    def palwpc_n(self): return _lib.cpu_palwpc_n(self.c)
    def palwpc_get(self, i): return _lib.cpu_palwpc_get(self.c, i)
    @property
    def nand_reads(self): return _lib.cpu_nand_reads(self.c)
    def bootcopy(self, dest, src, nwords):
        _lib.cpu_bootcopy(self.c, dest, src, nwords)
    def reset(self, entry=0x20, cs=0):
        _lib.cpu_reset(self.c, entry, cs)

    def __del__(self):
        if getattr(self, "c", None):
            _lib.cpu_free(self.c)
            self.c = None

    # execution
    def run(self, n):  return _lib.cpu_run(self.c, n)
    def step(self):    _lib.cpu_step(self.c)

    # state
    @property
    def r(self):       return [_lib.cpu_getreg(self.c, i) for i in range(8)]
    def getreg(self, i):        return _lib.cpu_getreg(self.c, i)
    def setreg(self, i, v):     _lib.cpu_setreg(self.c, i, v & 0xffff)
    @property
    def sb(self):      return _lib.cpu_getsb(self.c)
    @property
    def insns(self):   return _lib.cpu_insns(self.c)
    @property
    def halted(self):  return bool(_lib.cpu_halted(self.c))
    def lpc(self):     return _lib.cpu_lpc(self.c)

    # memory
    def peek(self, a):        return _lib.cpu_peek(self.c, a)
    def read_block(self, start, count):
        buf=(ctypes.c_uint16*count)(); _lib.cpu_read_block(self.c, start, count, buf); return bytes(buf)
    def poke(self, a, v):     _lib.cpu_poke(self.c, a, v & 0xffff)

    # peripheral config / introspection
    def set_ready(self, a, v):      _lib.cpu_set_ready(self.c, a, v & 0xffff)
    def set_autoclear(self, a, m):  _lib.cpu_set_autoclear(self.c, a, m & 0xffff)
    def set_csram_words(self, n): _lib.cpu_set_csram_words(self.c, n)
    def set_reador(self, a, m):     _lib.cpu_set_reador(self.c, a, m & 0xffff)
    def set_readclear(self, a, m):  _lib.cpu_set_readclear(self.c, a, m & 0xffff)
    def mmio_reads(self, a):        return _lib.cpu_mmio_reads(self.c, a)
    def mmio_writes(self, a):       return _lib.cpu_mmio_writes(self.c, a)
    def mmio_last(self, a):         return _lib.cpu_mmio_last(self.c, a)

    def set_cs_trap(self, limit):   _lib.cpu_set_cs_trap(self.c, limit)
    def add_hle(self, pc, hid):     return _lib.cpu_add_hle(self.c, pc, hid)
    def clear_hle(self):            _lib.cpu_clear_hle(self.c)
    def hle_calls(self, idx):       return _lib.cpu_hle_calls(self.c, idx)
    def set_hle_debug(self, on):    _lib.cpu_set_hle_debug(self.c, 1 if on else 0)
    def run_until(self, pc, maxn):  return _lib.cpu_run_until(self.c, pc, maxn)
    def add_watch(self, addr):      return _lib.cpu_add_watch(self.c, addr)
    def clear_watch(self):          _lib.cpu_clear_watch(self.c)
    def watch_hits(self, idx):      return _lib.cpu_watch_hits(self.c, idx)
    def nlog_n(self):               return _lib.cpu_nlog_n(self.c)
    def nlog_get(self, i, f):       return _lib.cpu_nlog_get(self.c, i, f)
    def nlog_reset(self):           _lib.cpu_nlog_reset(self.c)
    def set_timer(self, line, period):  _lib.cpu_set_timer(self.c, line, period)
    def set_timer_status(self, a, bits): _lib.cpu_set_timer_status(self.c, a, bits)
    def raise_irq(self, line):          _lib.cpu_raise_irq(self.c, line)
    def raise_fiq(self):                _lib.cpu_raise_fiq(self.c)
    def set_fiq_vec(self, v):           _lib.cpu_set_fiq_vec(self.c, v)

    # Registers: enum { SP, R1, R2, R3, R4, BP, SR, PC }
    _SP, _SR, _PC = 0, 6, 7

    def frame_tick(self, line=5, budget=400_000):
        """Deliver one display-frame interrupt (line 5 -> 0x08f23f) to the running
        firmware and let it run the frame: the handler posts the frame event and the
        firmware advances its display pipeline. Uses the proper vectored IRQ path."""
        self.raise_irq(line)
        self.run(budget)
    @property
    def irq_taken(self):    return _lib.cpu_irq_taken(self.c)
    @property
    def trapped(self):    return bool(_lib.cpu_trapped(self.c))
    @property
    def trap_from(self):  return _lib.cpu_trap_from(self.c)
    @property
    def trap_to(self):    return _lib.cpu_trap_to(self.c)

    def state(self):
        r = self.r
        sr = r[SR]
        names = ["sp", "r1", "r2", "r3", "r4", "bp", "sr", "pc"]
        return (f"LPC={self.lpc():06x} "
                + " ".join(f"{names[i]}={r[i]:04x}" for i in range(8))
                + f" [N={int(bool(sr&_N))} Z={int(bool(sr&_Z))}"
                + f" S={int(bool(sr&_S))} C={int(bool(sr&_C))} CS={sr&0x3f:02x}]")


def default_furby_cpu(image_bytes, nand_bytes=None):
    """NativeCPU with the correct memory map, boot entry, NAND controller + quirks.

    Boot ROM loads GameCode to machine 0x050000 (header dest) and starts at 0x050020.
    If `nand_bytes` is given, the NAND controller (0x7850-0x7856) serves it, so the
    firmware can stream personality/audio blocks out of flash into SDRAM."""
    cpu = NativeCPU(flat=False)
    cpu.load_at(ROM_BASE, image_bytes)               # image at machine 0x050000
    if nand_bytes is not None:
        cpu.load_nand(nand_bytes)                    # full NAND for the controller
    cpu.reset(entry=RESET_ENTRY, cs=ROM_BASE >> 16)  # LPC = 0x050020
    cpu.set_ready(0x780f, 0x0002)      # P_PowerState clock FSM settles at state 2
    cpu.set_autoclear(0x7819, 0x0002)  # P_Cache_Ctrl invalidate bit self-clears
    cpu.set_reador(0x7943, 0x0007)     # SPI status: report transfer-ready (else the
                                       # post-checksum SPI busy-wait spins forever)
    # NAND status 0x7850 now handled by the controller (bit15=ready) — no stub needed.
    NOP = 0xf165                       # bypass the "PROGRAM ROM" fail-spin (like MAME)
    cpu.poke(0x0846f6, NOP)
    # HLE the FAT32 find-file-by-name (0x078730): resolve names against the parsed
    # filesystem in the NAND, since the OOB-stripped dump lacks the FTL metadata the
    # firmware's own block scan needs. Without this the GameCode.bin self-checksum
    # loops forever on the 0xffffffff "not found" sentinel and the eyes never render.
    # (Roadmap §8: remove once a real OOB-preserving dump / FTL model exists.)
    if nand_bytes is not None:
        cpu.add_hle(0x078730, 2)   # find-file-by-name -> size
        cpu.add_hle(0x090f7f, 4)   # open(name,mode) -> handle (resolve via FAT)
        cpu.add_hle(0x091c93, 5)   # read-byte(handle) -> next file byte
        # display sync: the eye-LCD pipeline waits on 0x707c bit15 (vblank/ready)
        # and reads 0x707f status bit7; model them so the compositor advances.
        cpu.set_reador(0x707c, 0x8000)
        cpu.set_reador(0x707f, 0x0080)
    return cpu


if __name__ == "__main__":
    import argparse, time
    import unsp_disasm as UD
    ap = argparse.ArgumentParser(description="native µ'nSP core runner")
    ap.add_argument("file")
    ap.add_argument("--entry", type=lambda x: int(x, 0), default=0x20)
    ap.add_argument("--steps", type=int, default=50_000_000)
    ap.add_argument("--bench", action="store_true")
    args = ap.parse_args()

    with open(args.file, "rb") as f:
        img = f.read()
    cpu = default_furby_cpu(img, entry=args.entry)

    t0 = time.time()
    # run in chunks so we can watch for reaching main / spinning
    chunk = 2_000_000
    done = 0
    reached_main = None
    while done < args.steps:
        before = cpu.lpc()
        n = cpu.run(min(chunk, args.steps - done))
        done += n
        if reached_main is None and cpu.lpc() >= 0x050000:
            reached_main = cpu.insns
        if cpu.halted:
            print("[halted]"); break
        if n < chunk and cpu.lpc() == before:
            break
    dt = time.time() - t0
    print(cpu.state())
    print(f"instructions: {cpu.insns:,} in {dt:.2f}s = {cpu.insns/max(dt,1e-9)/1e6:.1f} M insn/s")
    if reached_main:
        print(f"reached main (0x05xxxx) at instruction {reached_main:,}")
