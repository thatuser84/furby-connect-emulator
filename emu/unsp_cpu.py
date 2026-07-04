#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
µ'nSP 2.0 / ISA 1.3 CPU core for the Furby Connect (GeneralPlus GPL16258).

Execution semantics are a faithful port of MAME's GPL-2.0 unSP core
(Segher Boessenkool / Ryan Holtz / David Haywood): the ALU flag math
(update_nzsc / update_nz), all 15 branch conditions, call/goto stack behaviour,
segment (CS/DS) carry, push/pop, and the shifter — verified line-for-line.

Word-addressed, 22-bit space (4 M-words). SR packs [DS:6][N Z S C][CS:6];
the code segment is SR&0x3f, so the live PC is LPC = (CS<<16)|PC.

This is the Phase-2 core: peripherals are stubbed via a simple MMIO hook at
0x7000-0x7FFF (Phase 4 fills them in). Everything below 0x7000 and the banked
ROM above is flat RAM backed by the firmware image.
"""

from __future__ import annotations

from array import array

# register indices
SP, R1, R2, R3, R4, BP, SR, PC = range(8)
REGNAME = ["sp", "r1", "r2", "r3", "r4", "bp", "sr", "pc"]

# SR flag bits
N = 0x0200
Z = 0x0100
S = 0x0080
C = 0x0040
N_SH, Z_SH, S_SH, C_SH = 9, 8, 7, 6

ADDR_MASK = 0x3fffff       # 22-bit
MMIO_LO, MMIO_HI = 0x7000, 0x7fff


class Bus:
    """Flat 22-bit word memory with an MMIO window at 0x7000-0x7FFF."""

    def __init__(self, image_words=None):
        self.mem = array("H", bytes(2 * 0x400000))   # 4 M-words, zeroed
        if image_words:
            n = min(len(image_words), len(self.mem))
            self.mem[:n] = array("H", image_words[:n])
        # MMIO state
        self.mmio_reads = {}     # addr -> count
        self.mmio_writes = {}    # addr -> count
        self.mmio_last = {}      # addr -> last value written
        # values returned when firmware polls a status register (ready-by-default)
        self.mmio_ready = {
            0x780f: 0x0002,      # P_PowerState: clock FSM settles at state 2 (passes both boot polls)
            0x7818: 0x0000,      # P_PLLCLKWait
        }
        # bits that read back as 0 (self-clearing "operation done" trigger bits)
        self.mmio_autoclear = {
            0x7819: 0x0002,      # P_Cache_Ctrl: invalidate bit auto-clears when done
        }
        self.log_mmio = False

    def r16(self, addr):
        addr &= ADDR_MASK
        if MMIO_LO <= addr <= MMIO_HI:
            self.mmio_reads[addr] = self.mmio_reads.get(addr, 0) + 1
            if addr in self.mmio_last:
                return self.mmio_last[addr] & ~self.mmio_autoclear.get(addr, 0)
            return self.mmio_ready.get(addr, 0x0000)
        return self.mem[addr]

    def w16(self, addr, val):
        addr &= ADDR_MASK
        val &= 0xffff
        if MMIO_LO <= addr <= MMIO_HI:
            self.mmio_writes[addr] = self.mmio_writes.get(addr, 0) + 1
            self.mmio_last[addr] = val
            if self.log_mmio:
                print(f"    MMIO W 0x{addr:04x} = 0x{val:04x}")
            return
        self.mem[addr] = val


class CPU:
    def __init__(self, bus, entry=0x20, cs=0):
        self.bus = bus
        self.r = [0, 0, 0, 0, 0, 0, 0, 0]
        self.sb = 0                 # hidden 4-bit shift value
        self.irq_en = False
        self.fiq_en = False
        self.cycles = 0
        self.insns = 0
        self.halted = False
        # HLE boot: jump straight to the reset stub we located (skip boot ROM)
        self.r[PC] = entry & 0xffff
        self.r[SR] = (self.r[SR] & 0xffc0) | (cs & 0x3f)

    # ---- segment-aware PC/addressing ----------------------------------
    def lpc(self):
        return (((self.r[SR] & 0x3f) << 16) | self.r[PC]) & ADDR_MASK

    def add_lpc(self, off):
        new = (self.lpc() + off) & ADDR_MASK
        self.r[PC] = new & 0xffff
        self.r[SR] = (self.r[SR] & 0xffc0) | ((new >> 16) & 0x3f)

    def lreg_i(self, reg):
        # ds:[reg] -> (DS<<16)|reg  (DS = SR bits 10-15)
        return (((self.r[SR] << 6) & 0x3f0000) | self.r[reg]) & ADDR_MASK

    def read16(self, a):
        return self.bus.r16(a)

    def write16(self, a, v):
        self.bus.w16(a, v)

    def push(self, val, reg):
        self.write16(self.r[reg], val & 0xffff)
        self.r[reg] = (self.r[reg] - 1) & 0xffff

    def pop(self, reg):
        self.r[reg] = (self.r[reg] + 1) & 0xffff
        return self.read16(self.r[reg])

    # ---- flags --------------------------------------------------------
    def update_nzsc(self, value, r0, r1):
        sr = self.r[SR] & ~(N | Z | S | C)
        if ((value >> 16) & 1) != ((((r0 ^ r1) >> 15) & 1)):
            sr |= S
        if (value >> 15) & 1:
            sr |= N
        if (value & 0xffff) == 0:
            sr |= Z
        if (value >> 16) & 1:
            sr |= C
        self.r[SR] = sr

    def update_nz(self, value):
        sr = self.r[SR] & ~(N | Z)
        if value & 0x8000:
            sr |= N
        if (value & 0xffff) == 0:
            sr |= Z
        self.r[SR] = sr

    # ---- top-level step ----------------------------------------------
    def step(self):
        op = self.read16(self.lpc())
        self.add_lpc(1)
        self.insns += 1
        op0 = op >> 12
        opa = (op >> 9) & 7
        op1 = (op >> 6) & 7
        if op0 == 0xf:
            self._fxxx(op)
        elif op0 < 0xf and opa == 7 and op1 < 2:
            self._jumps(op)
        elif op0 == 0xe:
            self._exxx(op)
        else:
            self._remaining(op)

    # ---- branches -----------------------------------------------------
    def _jumps(self, op):
        op0 = (op >> 12) & 15
        op1 = (op >> 6) & 7
        imm = op & 0x3f
        sr = self.r[SR]
        take = False
        if op0 == 0:   take = not (sr & C)                       # jb
        elif op0 == 1: take = bool(sr & C)                       # jae
        elif op0 == 2: take = not (sr & S)                       # jge
        elif op0 == 3: take = bool(sr & S)                       # jl
        elif op0 == 4: take = not (sr & Z)                       # jne
        elif op0 == 5: take = bool(sr & Z)                       # je
        elif op0 == 6: take = not (sr & N)                       # jpl
        elif op0 == 7: take = bool(sr & N)                       # jmi
        elif op0 == 8: take = (sr & (Z | C)) != C                # jbe
        elif op0 == 9: take = (sr & (Z | C)) == C                # ja
        elif op0 == 10: take = bool(sr & (Z | S))                # jle
        elif op0 == 11: take = not (sr & (Z | S))                # jg
        elif op0 == 12: take = ((sr & N) >> N_SH) == ((sr & S) >> S_SH)  # jvc
        elif op0 == 13: take = ((sr & N) >> N_SH) != ((sr & S) >> S_SH)  # jvs
        elif op0 == 14: take = True                              # jmp
        if take:
            self.add_lpc(imm if op1 == 0 else (-imm))

    # ---- ALU / push / pop --------------------------------------------
    def _do_alu(self, op0, r0, r1, r2, update):
        write = True
        if op0 == 0x00:      # add
            lres = r0 + r1
            if update: self.update_nzsc(lres, r0, r1)
        elif op0 == 0x01:    # adc
            c = 1 if (self.r[SR] & C) else 0
            lres = r0 + r1 + c
            if update: self.update_nzsc(lres, r0, r1)
        elif op0 == 0x02:    # sub
            nr1 = (~r1) & 0xffff
            lres = r0 + nr1 + 1
            if update: self.update_nzsc(lres, r0, nr1)
        elif op0 == 0x03:    # sbc
            c = 1 if (self.r[SR] & C) else 0
            nr1 = (~r1) & 0xffff
            lres = r0 + nr1 + c
            if update: self.update_nzsc(lres, r0, nr1)
        elif op0 == 0x04:    # cmp
            nr1 = (~r1) & 0xffff
            lres = r0 + nr1 + 1
            if update: self.update_nzsc(lres, r0, nr1)
            return lres, False
        elif op0 == 0x06:    # neg
            lres = (-r1) & 0x1ffff
            if update: self.update_nz(lres)
        elif op0 == 0x08:    # xor
            lres = r0 ^ r1
            if update: self.update_nz(lres)
        elif op0 == 0x09:    # load
            lres = r1
            if update: self.update_nz(lres)
        elif op0 == 0x0a:    # or
            lres = r0 | r1
            if update: self.update_nz(lres)
        elif op0 == 0x0b:    # and
            lres = r0 & r1
            if update: self.update_nz(lres)
        elif op0 == 0x0c:    # test
            lres = r0 & r1
            if update: self.update_nz(lres)
            return lres, False
        elif op0 == 0x0d:    # store
            self.write16(r2, r0)
            return r0, False
        else:
            return 0, False
        return lres, write

    def _remaining(self, op):
        op0 = op >> 12
        opa = (op >> 9) & 7
        op1 = (op >> 6) & 7
        opn = (op >> 3) & 7
        opb = op & 7
        lower = (op1 << 4) | op0

        # push
        if lower == 0x2d:
            r0 = opn
            r1 = opa
            while r0:
                self.push(self.r[r1], SP if opb == 0 else opb)
                r1 -= 1
                r0 -= 1
            return
        # pop / reti / retf
        if lower == 0x29:
            if op == 0x9a98:            # reti
                self.r[SR] = self.pop(SP)
                self.r[PC] = self.pop(SP)
                return
            if op == 0x9a90:            # retf
                self.r[SR] = self.pop(SP)
                self.r[PC] = self.pop(SP)
                return
            r0 = opn
            r1 = opa
            while r0:
                r1 += 1
                self.r[r1] = self.pop(opb)
                r0 -= 1
            return

        r0 = self.r[opa]
        r1 = 0
        r2 = 0

        if op1 == 0x00:                 # [bp+imm6]
            r2 = (self.r[BP] + (op & 0x3f)) & 0xffff
            if op0 != 0x0d:
                r1 = self.read16(r2)
        elif op1 == 0x01:               # imm6
            r1 = op & 0x3f
        elif op1 == 0x03:               # indirect
            lsb = opn & 3
            if opn & 4:                 # ds:[..]
                if lsb == 3:
                    self.r[opb] = (self.r[opb] + 1) & 0xffff
                    if self.r[opb] == 0: self.r[SR] = (self.r[SR] + 0x0400) & 0xffff
                r2 = self.lreg_i(opb)
                if op0 != 0x0d:
                    r1 = self.read16(r2)
                if lsb == 1:
                    self.r[opb] = (self.r[opb] - 1) & 0xffff
                    if self.r[opb] == 0xffff: self.r[SR] = (self.r[SR] - 0x0400) & 0xffff
                elif lsb == 2:
                    self.r[opb] = (self.r[opb] + 1) & 0xffff
                    if self.r[opb] == 0: self.r[SR] = (self.r[SR] + 0x0400) & 0xffff
            else:
                if lsb == 3:
                    self.r[opb] = (self.r[opb] + 1) & 0xffff
                r2 = self.r[opb]
                if op0 != 0x0d:
                    r1 = self.read16(r2)
                if lsb == 1:
                    self.r[opb] = (self.r[opb] - 1) & 0xffff
                elif lsb == 2:
                    self.r[opb] = (self.r[opb] + 1) & 0xffff
        elif op1 == 0x04:               # register / imm16 / [imm16] / shift
            if opn == 0:
                r1 = self.r[opb]
            elif opn == 1:
                r0 = self.r[opb]
                r1 = self.read16(self.lpc()); self.add_lpc(1)
            elif opn == 2:
                r0 = self.r[opb]
                r2 = self.read16(self.lpc()); self.add_lpc(1)
                if op0 != 0x0d:
                    r1 = self.read16(r2)
            elif opn == 3:
                r1 = r0
                r0 = self.r[opb]
                r2 = self.read16(self.lpc()); self.add_lpc(1)
            else:                        # asr
                shift = (self.r[opb] << 4) | self.sb
                if shift & 0x80000:
                    shift |= 0xf00000
                shift >>= (opn - 3)
                self.sb = shift & 0x0f
                r1 = (shift >> 4) & 0xffff
        elif op1 == 0x05:               # lsl / lsr
            if opn & 4:
                shift = ((self.r[opb] << 4) | self.sb) >> (opn - 3)
                self.sb = shift & 0x0f
                r1 = (shift >> 4) & 0xffff
            else:
                shift = ((self.sb << 16) | self.r[opb]) << (opn + 1)
                self.sb = (shift >> 16) & 0x0f
                r1 = shift & 0xffff
        elif op1 == 0x06:               # rol / ror
            shift = (((self.sb << 16) | self.r[opb]) << 4) | self.sb
            if opn & 4:
                shift >>= (opn - 3)
                self.sb = shift & 0x0f
            else:
                shift <<= (opn + 1)
                self.sb = (shift >> 20) & 0x0f
            r1 = (shift >> 4) & 0xffff
        elif op1 == 0x07:               # direct page-zero [imm6]
            r2 = op & 0x3f
            r1 = self.read16(r2)

        lres, write = self._do_alu(op0, r0, r1, r2, opa != 7)
        if write:
            if op1 == 0x04 and opn == 0x03:
                self.write16(r2, lres)
            else:
                self.r[opa] = lres & 0xffff

    # ---- fxxx group (call/goto/int/mul/...) ---------------------------
    def _fxxx(self, op):
        sub = (op & 0x01c0) >> 6
        if sub == 1 and (op & 0xf3c0) == 0xf040:          # call A22
            imm = self.read16(self.lpc()); self.add_lpc(1)
            self.push(self.r[PC], SP)
            self.push(self.r[SR], SP)
            self.r[PC] = imm
            self.r[SR] = (self.r[SR] & 0xffc0) | (op & 0x3f)
            return
        if sub == 2 and (op & 0xffc0) == 0xfe80:          # goto A22
            tgt = self.read16(self.lpc())
            self.r[PC] = tgt
            self.r[SR] = (self.r[SR] & 0xffc0) | (op & 0x3f)
            return
        if sub == 3 and (op & 0xffc0) == 0xfec0:          # goto mr
            self.r[PC] = self.r[R3]
            self.r[SR] = (self.r[SR] & 0xffc0) | (self.r[R4] & 0x3f)
            return
        if sub == 0:                                       # ds moves / mul
            if (op & 0xffc0) == 0xfe00:                     # ds = imm6
                self.r[SR] = (self.r[SR] & 0x03ff) | ((op & 0x3f) << 10)
                return
            if (op & 0xf1f8) == 0xf020:                     # r = ds
                self.r[op & 7] = (self.r[SR] >> 10) & 0x3f
                return
            if (op & 0xf1f8) == 0xf028:                     # ds = r
                self.r[SR] = (self.r[SR] & 0x03ff) | ((self.r[op & 7] & 0x3f) << 10)
                return
            if (op & 0xf1f8) in (0xf030, 0xf038):           # fr moves (ignore fr reg)
                return
            self._mul(op)
            return
        if sub == 5:                                       # system/control
            low = op & 0xff
            if low == 0x40: self.irq_en = self.fiq_en = False        # int off
            elif low == 0x41: self.irq_en = True                     # int irq
            elif low == 0x42: self.fiq_en = True                     # int fiq
            elif low == 0x43: self.irq_en = self.fiq_en = True       # int fiq,irq
            elif low in (0x48,): self.irq_en = False                 # irq off
            elif low in (0x49,): self.irq_en = True                  # irq on
            elif low in (0x4c,): self.fiq_en = False                 # fiq off
            elif low in (0x4e,): self.fiq_en = True                  # fiq on
            elif low in (0x60, 0x68, 0x70, 0x78):                    # break
                self.halted = True
            elif low in (0x65, 0x6d, 0x75, 0x7d):                    # nop
                pass
            elif low in (0x61, 0x69, 0x71, 0x79):                    # call mr
                self.push(self.r[PC], SP); self.push(self.r[SR], SP)
                self.r[PC] = self.r[R3]
                self.r[SR] = (self.r[SR] & 0xffc0) | (self.r[R4] & 0x3f)
            # divs/divq/exp/secbank/etc: not yet modelled (rare on the boot path)
            return
        # sub 4/6/7: mul/muls variants
        self._mul(op)

    def _mul(self, op):
        # basic MR = rd*rs -> R4:R3 (32-bit). Sign handling simplified for now.
        rd = (op >> 9) & 7
        rs = op & 7
        prod = (self.r[rd] * self.r[rs]) & 0xffffffff
        self.r[R3] = prod & 0xffff
        self.r[R4] = (prod >> 16) & 0xffff

    # ---- exxx group (bit ops, 16-bit shift) ---------------------------
    def _exxx(self, op):
        if (op & 0xf1c8) == 0xe000 or (op & 0xf1c0) == 0xe040:   # reg bitop
            b = (op >> 4) & 3
            rd = (op >> 9) & 7
            if (op & 0xf1c0) == 0xe040:
                bit = op & 0xf
            else:
                bit = self.r[op & 7] & 0xf
            self._bitop(b, rd, bit, mem=False)
            return
        if (op & 0xf1c0) in (0xe180, 0xe1c0) or (op & 0xf1c8) in (0xe100, 0xe140):
            b = (op >> 4) & 3
            rd = (op >> 9) & 7
            ds = (op & 0xf1c0) == 0xe1c0 or (op & 0xf1c8) == 0xe140
            if (op & 0xf1c0) in (0xe180, 0xe1c0):
                bit = op & 0xf
            else:
                bit = self.r[op & 7] & 0xf
            self._bitop(b, rd, bit, mem=True, ds=ds)
            return
        if (op & 0xf188) == 0xe108:                              # 16-bit shift
            rd = (op >> 9) & 7
            sh = (op >> 4) & 7
            rs = self.r[op & 7] & 0xf
            self._shift16(rd, sh, rs)
            return
        if (op & 0xf1f8) == 0xe008 or (op & 0xf180) == 0xe080:   # mul/muls
            self._mul(op)
            return

    def _bitop(self, b, rd, bit, mem, ds=False):
        if mem:
            addr = self.lreg_i(rd) if ds else self.r[rd]
            val = self.read16(addr)
        else:
            val = self.r[rd]
        mask = 1 << bit
        if b == 0:          # tstb
            self.r[SR] = (self.r[SR] & ~Z) | (0 if (val & mask) else Z)
            return
        elif b == 1: val |= mask          # setb
        elif b == 2: val &= ~mask         # clrb
        elif b == 3: val ^= mask          # invb
        val &= 0xffff
        if mem:
            self.write16(addr, val)
        else:
            self.r[rd] = val

    def _shift16(self, rd, sh, rs):
        v = self.r[rd]
        if sh == 0:   v = v >> rs                       # asr (approx)
        elif sh == 2: v = (v << rs) & 0xffff            # lsl
        elif sh == 4: v = v >> rs                       # lsr
        elif sh == 6: v = ((v << rs) | (v >> (16 - rs))) & 0xffff if rs else v   # rol
        elif sh == 7: v = ((v >> rs) | (v << (16 - rs))) & 0xffff if rs else v   # ror
        self.r[rd] = v & 0xffff

    # ---- run helpers --------------------------------------------------
    def run(self, n):
        for _ in range(n):
            if self.halted:
                break
            self.step()

    def state(self):
        cs = self.r[SR] & 0x3f
        return (f"LPC={self.lpc():06x} "
                + " ".join(f"{REGNAME[i]}={self.r[i]:04x}" for i in range(8))
                + f" [N={int(bool(self.r[SR]&N))} Z={int(bool(self.r[SR]&Z))}"
                + f" S={int(bool(self.r[SR]&S))} C={int(bool(self.r[SR]&C))} CS={cs:02x}]")


if __name__ == "__main__":
    import argparse
    import unsp_disasm as UD
    ap = argparse.ArgumentParser(description="unSP CPU core (Furby) — run & trace")
    ap.add_argument("file")
    ap.add_argument("--entry", type=lambda x: int(x, 0), default=0x20)
    ap.add_argument("--steps", type=int, default=200)
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        def enc(op0, opa, op1, opn, opb):
            return (op0 << 12) | (opa << 9) | (op1 << 6) | (opn << 3) | opb
        bus = Bus()
        c = CPU(bus, entry=0)
        # r1 = 0x1234 (load imm16: op0=9,op1=4,opn=1) ; r2 = 0x0001 ; r1 += r2 ; cmp r1,r1
        bus.mem[0] = enc(9, R1, 4, 1, 0); bus.mem[1] = 0x1234
        bus.mem[2] = enc(9, R2, 4, 1, 0); bus.mem[3] = 0x0001
        bus.mem[4] = enc(0, R1, 4, 0, R2)          # add r1, r2  (register form)
        bus.mem[5] = enc(4, R1, 4, 0, R1)          # cmp r1, r1  -> Z should set
        c.run(4)
        ok = (c.r[R1] == 0x1235 and c.r[R2] == 0x0001 and bool(c.r[SR] & Z))
        print("r1=%04x r2=%04x Z=%d" % (c.r[R1], c.r[R2], bool(c.r[SR] & Z)),
              "->", "PASS" if ok else "FAIL")
        raise SystemExit(0 if ok else 1)

    words = UD.load_words(args.file)
    bus = Bus(words)
    cpu = CPU(bus, entry=args.entry)
    visits = {}
    states = {}
    STALL = 4000     # identical FULL state (regs+sb) recurring => true no-progress spin
    for i in range(args.steps):
        if cpu.halted:
            print(f"[halted (break) at LPC={cpu.lpc():06x}] after {i} steps"); break
        lpc = cpu.lpc()
        if args.trace:
            ins = UD.decode_at(words, lpc, 0)
            print(cpu.state())
            print(f"   > {UD.format_insn(ins)}")
        visits[lpc] = visits.get(lpc, 0) + 1
        # a genuine spin repeats the *entire* CPU state; a working loop (memcpy,
        # countdown) changes registers each pass, so it won't trigger this.
        key = (lpc, tuple(cpu.r), cpu.sb)
        s = states.get(key, 0) + 1
        states[key] = s
        if s == STALL:
            hot = ", ".join(f"{a:04x}" for a, _ in
                            sorted(bus.mmio_reads.items(), key=lambda x: -x[1])[:3])
            print(f"[true spin: LPC={lpc:06x}, identical state x{STALL} after "
                  f"{i} steps — waiting on peripheral/IRQ (hot MMIO reads: {hot})]")
            break
        cpu.step()
    print("\nfinal:", cpu.state())
    print(f"distinct LPCs executed: {len(visits)} | instructions: {cpu.insns}")
    print(f"instructions executed: {cpu.insns}")
    hot_w = sorted(bus.mmio_writes.items(), key=lambda x: -x[1])[:8]
    hot_r = sorted(bus.mmio_reads.items(), key=lambda x: -x[1])[:8]
    print("MMIO writes:", ", ".join(f"{a:04x}x{n}" for a, n in hot_w))
    print("MMIO reads :", ", ".join(f"{a:04x}x{n}" for a, n in hot_r))
