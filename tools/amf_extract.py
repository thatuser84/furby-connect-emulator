#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Furby Connect audio: unpack the .AMF megafiles.

Each personality ships a `<Name>.AMF` audio megafile plus the shared
`AudioMegafiles/*.bin`. Reverse-engineered container format:

  * a top-level offset table (u32 LE), sized by its own first entry
    (table_bytes == offsets[0]), each entry pointing at a *category*;
  * each category is a second-level u32 offset table pointing at *leaf clips*;
  * each leaf clip is:  [u32 length][u16 sample_rate = 16000][SACM audio data].

The audio itself is GeneralPlus **SACM** — a proprietary, entropy-coded codec
(leaf payload measures ~7.9 bits/byte, so it is not plain ADPCM). Decoding SACM
to PCM is a separate codec-RE task; this tool cracks the *container* and rewraps
each clip into a standard **GeneralPlus .a18** file (same `00 ff 00 ff /
"GENERALPLUS SP"` header the toy's own A18 assets use), so every clip becomes a
first-class file that a GeneralPlus/SACM decoder can consume.

Usage:
  python3 amf_extract.py Base.AMF --list                 # catalog clips
  python3 amf_extract.py Base.AMF --out clips/ [--limit N]  # extract as .a18
"""
from __future__ import annotations
import argparse, os, struct

A18_MAGIC = bytes.fromhex("00ff00ff") + b"GENERALPLUS SP\x00\x00"


def u32(b, o): return struct.unpack_from("<I", b, o)[0]
def u16(b, o): return struct.unpack_from("<H", b, o)[0]


def top_table(amf: bytes) -> list[int]:
    first = u32(amf, 0)
    if first % 4 or first < 8 or first > len(amf):
        raise SystemExit("not an AMF (bad top-table size)")
    return [u32(amf, i) for i in range(0, first, 4)]


def category_leaves(amf: bytes, start: int, end: int) -> list[tuple[int, int]]:
    """Second-level table in [start,end): monotonic u32 pointers to leaf clips."""
    end = min(end, len(amf))
    ptrs = []
    i = start
    while i + 4 <= end:
        p = u32(amf, i)
        if not (start < p <= len(amf)):
            break
        if ptrs and p <= ptrs[-1]:
            break
        ptrs.append(p)
        i += 4
    leaves = []
    for j, p in enumerate(ptrs):
        e = ptrs[j + 1] if j + 1 < len(ptrs) else len(amf)
        leaves.append((p, e))
    return leaves


def iter_clips(amf: bytes):
    """Yield (category, index, offset, length, rate, data) for every valid leaf."""
    top = top_table(amf)
    for c in range(len(top)):
        cs = top[c]
        ce = top[c + 1] if c + 1 < len(top) else len(amf)
        for i, (s, e) in enumerate(category_leaves(amf, cs, ce)):
            if s + 6 > len(amf):
                continue
            length = u32(amf, s)
            rate = u16(amf, s + 4)
            data = amf[s + 6:s + 6 + length]
            if rate in (8000, 16000, 22050, 32000, 44100) and 0 < length <= (e - s):
                yield c, i, s, length, rate, data


def make_a18(data: bytes, rate: int) -> bytes:
    """Wrap raw SACM payload in a minimal GeneralPlus .a18 header."""
    hdr = bytearray(0x40)
    hdr[0:len(A18_MAGIC)] = A18_MAGIC
    struct.pack_into("<H", hdr, 0x14, 0x02FE)     # SACM format tag (as seen in toy A18s)
    struct.pack_into("<H", hdr, 0x18, 0x0010)
    struct.pack_into("<H", hdr, 0x1A, rate)       # sample rate
    struct.pack_into("<I", hdr, 0x28, len(data))  # payload length
    return bytes(hdr) + data


def main():
    ap = argparse.ArgumentParser(description="Unpack Furby Connect .AMF audio megafiles")
    ap.add_argument("amf", help="path to a .AMF (or AudioMegafiles/*.bin)")
    ap.add_argument("--list", action="store_true", help="catalog clips and exit")
    ap.add_argument("--out", help="extract clips as .a18 into this dir")
    ap.add_argument("--limit", type=int, default=0, help="max clips to extract (0 = all)")
    a = ap.parse_args()

    amf = open(a.amf, "rb").read()
    clips = list(iter_clips(amf))
    rates = {}
    total = 0
    for _, _, _, ln, rate, _ in clips:
        rates[rate] = rates.get(rate, 0) + 1
        total += ln
    print(f"{os.path.basename(a.amf)}: {len(clips)} audio clips, "
          f"{total/1024:.0f} KiB SACM, rates={rates}")

    if a.list:
        for c, i, off, ln, rate, _ in clips[:40]:
            print(f"  cat{c:>3} clip{i:<4} @0x{off:08x}  {ln:>7} B  {rate} Hz")
        if len(clips) > 40:
            print(f"  … and {len(clips) - 40} more")
        return

    if a.out:
        os.makedirs(a.out, exist_ok=True)
        n = 0
        for c, i, off, ln, rate, data in clips:
            open(os.path.join(a.out, f"cat{c:03d}_clip{i:04d}.a18"), "wb").write(make_a18(data, rate))
            n += 1
            if a.limit and n >= a.limit:
                break
        print(f"extracted {n} clips as .a18 -> {a.out}/")


if __name__ == "__main__":
    main()
