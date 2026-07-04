#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Recursive-descent code tracer for the Furby GameCode.bin firmware.

Linear disassembly can't tell code from data, so it invents instructions inside
tables and lies about what the firmware accesses. This walks control flow the way
the CPU does: start at the reset entry, follow every conditional branch (both
ways), call (target + return), and unconditional goto/jmp (target only), stop at
returns and unresolved computed jumps. Only words actually reached as instructions
are decoded — so the resulting memory-access map is trustworthy.

Assumes the image is a flat, word-addressed blob whose file word 0 == machine word
address `base` (default 0). Branch/call/goto targets that fall outside the file are
recorded as `external` rather than followed.
"""

from __future__ import annotations

import sys
from collections import defaultdict

import unsp_disasm as U

# instruction "flow" classes produced by the disassembler and what comes next:
#   ''    -> fall through
#   cond  -> target + fall through
#   jmp   -> target only        (goto / jmp / goto mr)
#   call  -> target + fall through
#   ret   -> nothing
#   idx   -> computed dest (pc = ...), unresolvable -> stop
NO_FALLTHROUGH = {"jmp", "ret"}


class Trace:
    def __init__(self, words, base=0):
        self.words = words
        self.base = base
        self.n = len(words)
        self.code = {}                 # word_addr -> Insn (decoded, reached)
        self.starts = set()            # discovered function/entry starts (call/goto targets)
        self.calls = defaultdict(int)  # target -> times called
        self.unresolved = []           # (pc, kind) computed jumps we couldn't follow
        self.external = defaultdict(int)  # off-image targets -> count
        self.reads = defaultdict(lambda: {"R": 0, "W": 0, "mode": "", "pc": None})

    def _idx(self, addr):
        """machine word address -> file word index, or None if outside the image."""
        i = addr - self.base
        return i if 0 <= i < self.n else None

    def harvest_call_targets(self):
        """Linear-scan the whole image for `call A22` (2-word, opcode f_40 / sub=001).
        A call's operand is almost always a genuine function entry, so these make
        excellent descent seeds — they reach functions only invoked via jump tables.
        We require the target to land in-image and on an even-ish boundary."""
        found = {}
        w = self.words
        for i in range(self.n - 1):
            op = w[i]
            # fxxx group, sub == 001, call pattern (op & 0xf3c0) == 0xf040
            if (op >> 12) == 0xf and (op & 0xf3c0) == 0xf040:
                target = (((op & 0x3f) << 16) | w[i + 1]) & 0x3fffff
                if self._idx(target) is not None:
                    found[target] = found.get(target, 0) + 1
        return found

    def run(self, entries, max_insn=5_000_000):
        work = list(entries)
        for e in entries:
            self.starts.add(e)
        seen_guard = 0
        while work:
            addr = work.pop()
            i = self._idx(addr)
            if i is None or addr in self.code:
                continue
            seen_guard += 1
            if seen_guard > max_insn:
                print("!! max_insn hit, stopping", file=sys.stderr)
                break

            ins = U.decode_at(self.words, i, self.base)
            self.code[addr] = ins

            # record any statically-resolvable memory access
            if ins.access and ins.access.addr is not None:
                a = ins.access
                e = self.reads[a.addr]
                e[a.kind] += 1
                e["mode"] = a.mode
                if e["pc"] is None:
                    e["pc"] = ins.pc

            nxt = addr + ins.length          # fall-through address

            # resolve branch/call/goto successors
            if ins.branch is not None:
                ti = self._idx(ins.branch)
                if ti is None:
                    self.external[ins.branch] += 1
                else:
                    if ins.flow in ("call", "jmp", "cond"):
                        if ins.flow in ("call", "jmp"):
                            self.starts.add(ins.branch)
                        if ins.flow == "call":
                            self.calls[ins.branch] += 1
                    work.append(ins.branch)
            elif ins.flow in ("idx",):
                self.unresolved.append((ins.pc, ins.text))
            elif ins.flow in ("jmp", "ret") and ins.branch is None:
                # goto mr / call mr / reti / pop pc — computed or terminal
                if "mr" in ins.text:
                    self.unresolved.append((ins.pc, ins.text))

            # fall-through unless this instruction never returns to the next word
            if ins.flow not in NO_FALLTHROUGH:
                work.append(nxt)

        return self

    # ---- reporting ------------------------------------------------------
    def coverage(self):
        code_words = sum(ins.length for ins in self.code.values())
        return {
            "insns": len(self.code),
            "code_words": code_words,
            "total_words": self.n,
            "pct": 100.0 * code_words / self.n if self.n else 0,
            "funcs": len(self.starts),
            "unresolved": len(self.unresolved),
            "external": len(self.external),
        }

    def io_map(self, lo=0x7000, hi=0x7fff):
        rows = [(a, e["R"], e["W"], e["mode"], e["pc"])
                for a, e in self.reads.items() if lo <= a <= hi]
        rows.sort(key=lambda r: -(r[1] + r[2]))
        return rows

    def listing(self, start, count):
        """Linear listing but annotated with whether each word was reached as code."""
        out = []
        addr = start
        shown = 0
        while shown < count and self._idx(addr) is not None:
            if addr in self.code:
                ins = self.code[addr]
                out.append("  " + U.format_insn(ins))
                addr += ins.length
            else:
                w = self.words[self._idx(addr)]
                out.append(f"  {addr:06x}:  {w:04x}        .dw 0x{w:04x}   ; (data / not reached)")
                addr += 1
            shown += 1
        return "\n".join(out)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="unSP recursive-descent tracer (Furby)")
    ap.add_argument("file")
    ap.add_argument("--base", type=lambda x: int(x, 0), default=0)
    ap.add_argument("--entry", type=lambda x: int(x, 0), action="append",
                    help="entry word address (repeatable). default: 0x20 (reset stub)")
    ap.add_argument("--harvest", action="store_true",
                    help="also seed descent from every call-target found in the image")
    ap.add_argument("--map", action="store_true", help="print the I/O access map")
    ap.add_argument("--funcs", action="store_true", help="list discovered function starts")
    ap.add_argument("--unresolved", action="store_true", help="list computed jumps (jump tables)")
    ap.add_argument("--list", type=lambda x: int(x, 0), help="annotated listing from this addr")
    ap.add_argument("--count", type=int, default=60)
    args = ap.parse_args()

    words = U.load_words(args.file)
    entries = list(args.entry or [0x20])
    tr = Trace(words, base=args.base)
    harvested = 0
    if args.harvest:
        ht = tr.harvest_call_targets()
        harvested = len(ht)
        entries += list(ht.keys())
    tr.run(entries)

    cov = tr.coverage()
    print(f"# entries: reset+{len(entries)-1} seeds"
          f"{f' ({harvested} harvested call-targets)' if args.harvest else ''}")
    print(f"# reached {cov['insns']} instructions "
          f"({cov['code_words']}/{cov['total_words']} words = {cov['pct']:.1f}% of image)")
    print(f"# discovered {cov['funcs']} function starts | "
          f"{cov['unresolved']} unresolved computed-jumps | "
          f"{cov['external']} off-image targets")

    if args.map:
        try:
            import gpl16250_regs as R
        except ImportError:
            R = None
        rows = tr.io_map()
        print(f"\n# I/O access map (0x7000-0x7fff), code-reachable only: {len(rows)} regs")
        print(f"  {'addr':>7} {'R':>5} {'W':>5}  {'register':<24} group")
        for a, r, w, mode, pc in rows[:40]:
            if R:
                name, grp = R.reg_name(a)
                name = name or "?"
            else:
                name, grp = "?", "?"
            print(f"  0x{a:04x} {r:5d} {w:5d}  {name:<24} {grp}")

    if args.funcs:
        fs = sorted(tr.starts)
        print(f"\n# {len(fs)} function starts (call/goto targets):")
        for f in fs[:80]:
            c = tr.calls.get(f, 0)
            print(f"  0x{f:06x}   ({c} calls)")

    if args.unresolved:
        print(f"\n# {len(tr.unresolved)} unresolved computed jumps (jump tables to map):")
        for pc, txt in tr.unresolved[:60]:
            print(f"  {pc:06x}:  {txt}")

    if args.list is not None:
        print(f"\n# annotated listing from 0x{args.list:06x}:")
        print(tr.listing(args.list, args.count))


if __name__ == "__main__":
    main()
