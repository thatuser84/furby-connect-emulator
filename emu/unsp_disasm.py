#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
unSP (Sunplus micro'nSP) ISA 1.3 disassembler for the Furby Connect firmware.

The Furby's GameCode.bin is built with `xasm16 -t5` (GameCode.lod: CurIsa=ISA13),
i.e. the unSP 1.3 instruction subset running on a GeneralPlus GPL16258.

Decode semantics are a faithful Python port of MAME's GPL-2.0 unSP disassembler
(Segher Boessenkool / David Haywood). We target m_iso = 13, so ISA>=12 features
(ds/fr moves, goto mr, irq/fiq enables) are decoded; ISA 2.0-only forms are not.

The machine is WORD-addressed: one instruction word = 16 bits, and every address
here is a *word* address. A 2-word instruction carries a 16-bit immediate/address
(`ximm`) in the following word.

Beyond pretty-printing, the disassembler records every *statically resolvable*
memory access (direct page-zero `[imm6]` and absolute `[A16]`), so a whole-image
scan yields a cross-reference of exactly what the firmware reads and writes — the
first step to locating the memory-mapped peripheral registers.
"""

from __future__ import annotations   # lazy annotations: `int | None` on py3.9

import struct
from dataclasses import dataclass, field

REGS = ["sp", "r1", "r2", "r3", "r4", "bp", "sr", "pc"]
ALUOPS = ["add", "adc", "sub", "sbc", "cmp", "?", "neg", "?",
          "xor", "load", "or", "and", "test", "store", "?", "?"]
JUMPS = ["jb", "jae", "jge", "jl", "jne", "je", "jpl", "jmi",
         "jbe", "ja", "jle", "jg", "jvc", "jvs", "jmp", "jinv"]
BITOPS = ["tstb", "setb", "clrb", "invb"]
LSFT = ["asr", "asror", "lsl", "lslor", "lsr", "lsror", "rol", "ror"]
SIGNMODES = ["uu", "us", "su?", "ss"]
FORMS = ["[{r}]", "[{r}--]", "[{r}++]", "[++{r}]"]

# ops that only take one register operand (dest==src required in some forms)
ALU_ONE = {4, 6, 9, 12}   # cmp, neg, load, test
PC = 7                    # index of 'pc' in REGS


@dataclass
class Access:
    """A statically-resolvable memory reference produced by one instruction."""
    kind: str            # 'R' or 'W'
    addr: int | None     # resolved word address, or None if dynamic (reg/bp-rel)
    mode: str            # human tag: 'A16', 'imm6', 'bp+imm6', 'ind', ...


@dataclass
class Insn:
    pc: int
    words: list          # the 1 or 2 raw words consumed
    length: int          # 1 or 2
    text: str            # disassembly
    access: Access | None = None
    branch: int | None = None   # static target word address, if a jump/call/goto
    flow: str = ""              # '', 'cond', 'jmp', 'call', 'ret', 'idx' (computed)


def _s(word, ximm, pc):
    """Decode one instruction. Returns Insn."""
    if word == 0x0000 or word == 0xffff:
        return Insn(pc, [word], 1, "--")

    op0 = word >> 12
    if op0 == 0xf:
        return _fxxx(word, ximm, pc)

    opA = (word >> 9) & 7
    op1 = (word >> 6) & 7
    if op0 < 15 and opA == PC and op1 < 2:
        return _jump(word, pc)
    if op0 == 0xe:
        return _exxx(word, pc)
    return _remaining(word, ximm, pc)


def _jump(word, pc):
    op0 = (word >> 12) & 15
    op1 = (word >> 6) & 7
    opimm = word & 0x3f
    target = (pc + 1 + opimm) if op1 == 0 else (pc + 1 - opimm)
    target &= 0x3fffff
    mn = JUMPS[op0]
    flow = "jmp" if mn == "jmp" else "cond"
    return Insn(pc, [word], 1, f"{mn} 0x{target:06x}", branch=target, flow=flow)


def _alu_text(op0, opA, operand):
    a = REGS[opA]
    forms = {
        0: f"{a} += {operand}", 1: f"{a} += {operand}, carry",
        2: f"{a} -= {operand}", 3: f"{a} -= {operand}, carry",
        4: f"cmp {a}, {operand}", 6: f"{a} = -{operand}",
        8: f"{a} ^= {operand}", 9: f"{a} = {operand}",
        10: f"{a} |= {operand}", 11: f"{a} &= {operand}",
        12: f"test {a}, {operand}",
    }
    return forms.get(op0, f"<bad alu {op0}>")


def _remaining(word, ximm, pc):
    op0 = word >> 12
    opA = (word >> 9) & 7
    op1 = (word >> 6) & 7
    opN = (word >> 3) & 7
    opB = word & 7
    opimm = word & 0x3f
    key = (op1 << 4) | op0
    dest_is_pc = (opA == PC)

    def flow_for(store=False):
        # writing to pc via ALU = computed/indexed jump
        return "idx" if (dest_is_pc and not store) else ""

    # --- op1 = 0 : base+displacement [bp+imm6] ---
    if op1 == 0:
        if op0 == 0x0d:  # store: [bp+imm6] = rA
            t = f"[bp+0x{opimm:02x}] = {REGS[opA]}"
            return Insn(pc, [word], 1, t, Access("W", None, "bp+imm6"))
        if op0 in (5, 7, 14, 15):
            return Insn(pc, [word], 1, "<bad>")
        t = _alu_text(op0, opA, f"[bp+0x{opimm:02x}]")
        return Insn(pc, [word], 1, t, Access("R", None, "bp+imm6"), flow=flow_for())

    # --- op1 = 1 : 6-bit immediate ---
    if op1 == 1:
        if op0 in (5, 7, 13, 14, 15):
            return Insn(pc, [word], 1, "<bad>")
        t = _alu_text(op0, opA, f"0x{opimm:02x}")
        return Insn(pc, [word], 1, t, flow=flow_for())

    # --- op1 = 2 : push/pop (everything else BAD) ---
    if op1 == 2:
        if op0 == 0x09:  # 0x29
            if word == 0x9a90:
                return Insn(pc, [word], 1, "retf", flow="ret")
            if word == 0x9a98:
                return Insn(pc, [word], 1, "reti", flow="ret")
            if opA + 1 < 8 and opA + opN < 8:
                t = f"pop {REGS[opA+1]}, {REGS[opA+opN]} from [{REGS[opB]}]"
                # pop into pc == return
                fl = "ret" if (opA + opN >= PC >= opA + 1) else ""
                return Insn(pc, [word], 1, t, flow=fl)
            return Insn(pc, [word], 1, "<bad>")
        if op0 == 0x0d:  # 0x2d push
            if opA + 1 >= opN and opA < opN + 7:
                t = f"push {REGS[opA+1-opN]}, {REGS[opA]} to [{REGS[opB]}]"
                return Insn(pc, [word], 1, t)
            return Insn(pc, [word], 1, "<bad>")
        return Insn(pc, [word], 1, "<bad>")

    # --- op1 = 3 : indirect memory [Rs] / [Rs--] / [Rs++] / [++Rs] (+ds:) ---
    if op1 == 3:
        ds = "ds:" if (opN & 4) else ""
        form = ds + FORMS[opN & 3].format(r=REGS[opB])
        if op0 == 0x0d:
            return Insn(pc, [word], 1, f"{form} = {REGS[opA]}", Access("W", None, "ind"))
        if op0 in (5, 7, 14, 15):
            return Insn(pc, [word], 1, "<bad>")
        t = _alu_text(op0, opA, form)
        return Insn(pc, [word], 1, t, Access("R", None, "ind"), flow=flow_for())

    # --- op1 = 4 : register / imm16 / direct [A16] / shift, chosen by opN ---
    if op1 == 4:
        if op0 == 0x0d:  # 0x4d
            if opN == 3 and opA == opB:
                t = f"[0x{ximm:04x}] = {REGS[opB]}"
                return Insn(pc, [word, ximm], 2, t, Access("W", ximm, "A16"))
            return Insn(pc, [word, ximm] if opN in (1, 2, 3) else [word],
                        2 if opN in (1, 2, 3) else 1, "<bad>")
        if op0 in (5, 7, 14, 15):
            return Insn(pc, [word], 1, "<bad>")
        if opN == 0:                      # register direct
            t = _alu_text(op0, opA, REGS[opB])
            return Insn(pc, [word], 1, t, flow=flow_for())
        if opN == 1:                      # 16-bit immediate (2 words)
            t = _alu_text(op0, opA, f"0x{ximm:04x}")
            return Insn(pc, [word, ximm], 2, t, flow=flow_for())
        if opN == 2:                      # direct memory read [A16] (2 words)
            t = _alu_text(op0, opA, f"[0x{ximm:04x}]")
            return Insn(pc, [word, ximm], 2, t, Access("R", ximm, "A16"), flow=flow_for())
        if opN == 3:                      # direct memory store [A16] = rB op rA
            t = f"[0x{ximm:04x}] {ALUOPS[op0]}= {REGS[opA]}"
            return Insn(pc, [word, ximm], 2, t, Access("W", ximm, "A16"))
        # opN >= 4: arithmetic-shift-right by (opN&3)+1
        t = _alu_text(op0, opA, f"{REGS[opB]} asr {(opN & 3) + 1}")
        return Insn(pc, [word], 1, t, flow=flow_for())

    # --- op1 = 5 : shift lsl/lsr ---
    if op1 == 5:
        if op0 in (5, 7, 13, 14, 15):
            return Insn(pc, [word], 1, "<bad>")
        sh = "lsl" if (opN & 4) == 0 else "lsr"
        t = _alu_text(op0, opA, f"{REGS[opB]} {sh} {(opN & 3) + 1}")
        return Insn(pc, [word], 1, t, flow=flow_for())

    # --- op1 = 6 : rotate rol/ror ---
    if op1 == 6:
        if op0 in (5, 7, 13, 14, 15):
            return Insn(pc, [word], 1, "<bad>")
        ro = "rol" if (opN & 4) == 0 else "ror"
        t = _alu_text(op0, opA, f"{REGS[opB]} {ro} {(opN & 3) + 1}")
        return Insn(pc, [word], 1, t, flow=flow_for())

    # --- op1 = 7 : direct page-zero [imm6] ---
    if op1 == 7:
        if op0 == 0x0d:
            return Insn(pc, [word], 1, f"[0x{opimm:02x}] = {REGS[opA]}",
                        Access("W", opimm, "imm6"))
        if op0 in (5, 7, 14, 15):
            return Insn(pc, [word], 1, "<bad>")
        t = _alu_text(op0, opA, f"[0x{opimm:02x}]")
        return Insn(pc, [word], 1, t, Access("R", opimm, "imm6"), flow=flow_for())

    return Insn(pc, [word], 1, "<unhandled>")


def _mul_text(word):
    rs = word & 7
    srd = (word >> 8) & 1
    rd = (word >> 9) & 7
    srs = (word >> 12) & 1
    sign = (srd << 1) | srs
    return f"mr = {REGS[rd]}*{REGS[rs]} {SIGNMODES[sign]}"


def _muls_text(word):
    rs = word & 7
    size = (word >> 3) & 0xf
    srd = (word >> 8) & 1
    rd = (word >> 9) & 7
    srs = (word >> 12) & 1
    sign = (srd << 1) | srs
    if size == 0:
        size = 16
    return f"mr = [{REGS[rd]}]*[{REGS[rs]}] {SIGNMODES[sign]},{size}"


# low-byte -> mnemonic for the fxxx sub=101 control/system group (ISA>=12)
_F101 = {
    0x40: "int off", 0x41: "int irq", 0x42: "int fiq", 0x43: "int fiq,irq",
    0x44: "fir_mov on", 0x45: "fir_mov off",
    0x46: "fraction off", 0x47: "fraction on",
    0x48: "irq off", 0x49: "irq on", 0x4a: "secbank off", 0x4b: "secbank on",
    0x4c: "fiq off", 0x4d: "irqnest off", 0x4e: "fiq on", 0x4f: "irqnest on",
    0x60: "break", 0x68: "break", 0x70: "break", 0x78: "break",
    0x65: "nop", 0x6d: "nop", 0x75: "nop", 0x7d: "nop",
    0x61: "call mr", 0x69: "call mr", 0x71: "call mr", 0x79: "call mr",
    0x62: "divs mr, r2", 0x6a: "divs mr, r2", 0x72: "divs mr, r2", 0x7a: "divs mr, r2",
    0x63: "divq mr, r2", 0x6b: "divq mr, r2", 0x73: "divq mr, r2", 0x7b: "divq mr, r2",
    0x64: "r2 = exp r4", 0x6c: "r2 = exp r4", 0x74: "r2 = exp r4", 0x7c: "r2 = exp r4",
}


def _fxxx(word, ximm, pc):
    sub = (word & 0x01c0) >> 6
    if sub == 0:   # 000: ds16 / ds-reg / fr-reg / else MUL
        if (word & 0xffc0) == 0xfe00:
            return Insn(pc, [word], 1, f"ds = 0x{word & 0x3f:02x}")
        if (word & 0xf1f8) == 0xf020:
            return Insn(pc, [word], 1, f"{REGS[word & 7]} = ds")
        if (word & 0xf1f8) == 0xf028:
            return Insn(pc, [word], 1, f"ds = {REGS[word & 7]}")
        if (word & 0xf1f8) == 0xf030:
            return Insn(pc, [word], 1, f"{REGS[word & 7]} = fr")
        if (word & 0xf1f8) == 0xf038:
            return Insn(pc, [word], 1, f"fr = {REGS[word & 7]}")
        return Insn(pc, [word], 1, _mul_text(word))
    if sub == 1:   # 001: CALL A22 (2 words)
        if (word & 0xf3c0) == 0xf040:
            opimm = word & 0x3f
            target = ((opimm << 16) | ximm) & 0x3fffff
            return Insn(pc, [word, ximm], 2, f"call 0x{target:06x}",
                        branch=target, flow="call")
        return Insn(pc, [word], 1, "<dunno f001>")
    if sub == 2:   # 010: GOTO/JMPF A22 (2 words) else MULS
        if (word & 0xffc0) == 0xfe80:
            opimm = word & 0x3f
            target = ((opimm << 16) | ximm) & 0x3fffff
            return Insn(pc, [word, ximm], 2, f"goto 0x{target:06x}",
                        branch=target, flow="jmp")
        return Insn(pc, [word], 1, _muls_text(word))
    if sub == 3:   # 011: goto mr else MULS
        if (word & 0xffc0) == 0xfec0:
            return Insn(pc, [word], 1, "goto mr", flow="jmp")
        return Insn(pc, [word], 1, _muls_text(word))
    if sub == 4:   # 100: MUL ss
        if (word & 0xf1f8) == 0xf108:
            return Insn(pc, [word], 1, _mul_text(word))
        return Insn(pc, [word], 1, "<dunno f100>")
    if sub == 5:   # 101: system/control group
        mn = _F101.get(word & 0xff)
        if mn:
            fl = "call" if mn == "call mr" else ("jmp" if mn == "goto mr" else "")
            return Insn(pc, [word], 1, mn, flow=fl)
        return Insn(pc, [word], 1, "<undefined f101>")
    # sub 6/7: MULS (EXTOP is ISA2.0 only, not reachable at iso=13)
    return Insn(pc, [word], 1, _muls_text(word))


def _exxx(word, pc):
    if (word & 0xf1c8) == 0xe000:
        b = (word >> 4) & 3
        return Insn(pc, [word], 1, f"{BITOPS[b]} {REGS[(word>>9)&7]},{REGS[word&7]}")
    if (word & 0xf1c0) == 0xe040:
        b = (word >> 4) & 3
        return Insn(pc, [word], 1, f"{BITOPS[b]} {REGS[(word>>9)&7]},{word&0xf}")
    if (word & 0xf1c0) == 0xe180:
        b = (word >> 4) & 3
        return Insn(pc, [word], 1, f"{BITOPS[b]} [{REGS[(word>>9)&7]}],{word&0xf}",
                    Access("W", None, "ind"))
    if (word & 0xf1c0) == 0xe1c0:
        b = (word >> 4) & 3
        return Insn(pc, [word], 1, f"{BITOPS[b]} ds:[{REGS[(word>>9)&7]}],{word&0xf}",
                    Access("W", None, "ind"))
    if (word & 0xf1c8) == 0xe100:
        b = (word >> 4) & 3
        return Insn(pc, [word], 1, f"{BITOPS[b]} [{REGS[(word>>9)&7]}],{REGS[word&7]}",
                    Access("W", None, "ind"))
    if (word & 0xf1c8) == 0xe140:
        b = (word >> 4) & 3
        return Insn(pc, [word], 1, f"{BITOPS[b]} ds:[{REGS[(word>>9)&7]}],{REGS[word&7]}",
                    Access("W", None, "ind"))
    if (word & 0xf1f8) == 0xe008:
        return Insn(pc, [word], 1, _mul_text(word))
    if (word & 0xf180) == 0xe080:
        return Insn(pc, [word], 1, _muls_text(word))
    if (word & 0xf188) == 0xe108:
        rd = (word >> 9) & 7
        sh = (word >> 4) & 7
        rs = word & 7
        return Insn(pc, [word], 1, f"{REGS[rd]} = {REGS[rd]} {LSFT[sh]} {REGS[rs]}")
    return Insn(pc, [word], 1, "<dunno exxx>")


# ---------------------------------------------------------------------------
# top-level helpers over a word buffer
# ---------------------------------------------------------------------------
def decode_at(words, i, base=0):
    """Decode the instruction at word-index i of list `words`. Returns Insn."""
    w = words[i]
    ximm = words[i + 1] if (i + 1) < len(words) else 0
    return _s(w, ximm, base + i)


def load_words(path, byte_off=0, byte_len=None):
    with open(path, "rb") as f:
        data = f.read()
    if byte_len is not None:
        data = data[byte_off:byte_off + byte_len]
    else:
        data = data[byte_off:]
    if len(data) & 1:
        data = data[:-1]
    return list(struct.unpack("<%dH" % (len(data) // 2), data))


def disassemble(words, base=0, start=0, count=None):
    """Yield Insn linearly. `start` and returned pc are word addresses (base-rel)."""
    i = start
    end = len(words) if count is None else min(len(words), start + count * 2)
    while i < end:
        ins = decode_at(words, i, base)
        yield ins
        i += ins.length


def format_insn(ins):
    raw = " ".join(f"{w:04x}" for w in ins.words)
    tag = ""
    if ins.access:
        a = ins.access
        loc = f"@0x{a.addr:04x}" if a.addr is not None else "(dyn)"
        tag = f"   ; {a.kind} {a.mode} {loc}"
    elif ins.branch is not None:
        tag = f"   ; -> 0x{ins.branch:06x} [{ins.flow}]"
    elif ins.flow:
        tag = f"   ; [{ins.flow}]"
    return f"{ins.pc:06x}:  {raw:<10}  {ins.text}{tag}"


def access_map(words, base=0):
    """Scan the whole image; return sorted list of (addr, reads, writes, sample_pc)."""
    acc = {}
    i = 0
    while i < len(words):
        ins = decode_at(words, i, base)
        if ins.access and ins.access.addr is not None:
            a = ins.access
            e = acc.setdefault(a.addr, {"R": 0, "W": 0, "mode": a.mode, "pc": ins.pc})
            e[a.kind] += 1
        i += ins.length
    return sorted(([addr, e["R"], e["W"], e["mode"], e["pc"]]
                   for addr, e in acc.items()), key=lambda x: x[0])


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="unSP ISA 1.3 disassembler (Furby)")
    ap.add_argument("file")
    ap.add_argument("--base", type=lambda x: int(x, 0), default=0,
                    help="word address of the first word (default 0)")
    ap.add_argument("--off", type=lambda x: int(x, 0), default=0,
                    help="byte offset into the file to start (default 0)")
    ap.add_argument("--count", type=int, default=64,
                    help="number of instructions to show (default 64)")
    ap.add_argument("--map", action="store_true",
                    help="instead: whole-image static memory-access map")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        # a few known encodings straight from the MAME source comments
        cases = [
            (0xf146, 0, "fraction off"),
            (0xf940, 0, "int off"),
            (0xf160, 0, "break"),
            (0xf165, 0, "nop"),
            (0xfec0, 0, "goto mr"),
        ]
        ok = True
        for w, x, want in cases:
            got = _s(w, x, 0).text
            flag = "OK " if got == want else "XX "
            if got != want:
                ok = False
            print(f"  {flag}{w:04x} -> {got!r} (want {want!r})")
        # call16 / goto16 shape
        print("  call:", _s(0xf040, 0x1234, 0).text, "len", _s(0xf040, 0x1234, 0).length)
        print("  goto:", _s(0xfe80, 0xabcd, 0).text, "len", _s(0xfe80, 0xabcd, 0).length)
        print("SELFTEST", "PASS" if ok else "FAIL")
        raise SystemExit(0 if ok else 1)

    words = load_words(args.file, byte_off=args.off)
    if args.map:
        rows = access_map(words, base=args.base)
        print(f"# static memory-access map: {len(rows)} distinct addresses")
        print(f"# {'addr':>8}  {'R':>5} {'W':>5}  mode     first-pc")
        for addr, r, w, mode, spc in rows:
            print(f"  0x{addr:04x}  {r:5d} {w:5d}  {mode:<7}  {spc:06x}")
    else:
        for ins in disassemble(words, base=args.base, start=0, count=args.count):
            print(format_insn(ins))
