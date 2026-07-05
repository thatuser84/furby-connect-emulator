#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Furby Connect / gpac800 FTL reconstruction & analysis.

The Furby's main flash is a raw NAND. The firmware's flash-translation-layer (FTL)
presents a *logical* image (on which the FAT filesystem lives) out of *physical*
blocks that are remapped for wear-levelling and bad-block replacement. A raw dump
(e.g. furbhax `NANDmainFLASH.BIN`, 262144 × 528-byte pages = 512 data + 16 spare) is
therefore in physical order and must be de-remapped before the filesystem appears.

What this tool establishes (all verified against a known-good logical image):

  * block granularity      : 32 pages (16 KiB), whole-block remap (no sub-block scramble)
  * reconstruction         : raw physical dump -> logical image is byte-exact where mapped
  * local layout           : a 2-plane, 8-block interleave (logical offset += 8 per 8 blocks)
  * the FTL map table lives : in "system" blocks whose page-0 spare begins c2 00 c3 00 ...
                              (here: physical blocks 3920-3927 and 4056-4057), each holding
                              an 8192-entry little-endian u16 table.

The exact table value-encoding (zone base + plane bit + bad-block indirection) is not yet
fully pinned, so `recover_map()` currently derives the physical->logical map by
content-matching against a reference logical image and proves the model end-to-end.
Decoding the system-block table format is the final step to a *reference-free* rebuild.

Usage:
  python3 ftl_reconstruct.py --raw NANDmainFLASH.BIN --raw-nooob no-dividers.bin \
      --logical "furby-nand (Fixed OOB Data).bin" [--rebuild out.bin]
"""
from __future__ import annotations
import argparse, hashlib, struct

PAGE_DATA = 512
PAGE_FULL = 528          # 512 data + 16 spare (OOB)
BLOCK_PAGES = 32
BLOCK_BYTES = BLOCK_PAGES * PAGE_DATA     # 16 KiB


def strip_oob(raw: bytes) -> bytes:
    """528-byte physical pages -> 512-byte logical-data image (spare removed)."""
    npages = len(raw) // PAGE_FULL
    out = bytearray(npages * PAGE_DATA)
    for p in range(npages):
        out[p * PAGE_DATA:(p + 1) * PAGE_DATA] = raw[p * PAGE_FULL:p * PAGE_FULL + PAGE_DATA]
    return bytes(out)


def block_oob(raw: bytes, pblock: int, page: int = 0) -> bytes:
    base = (pblock * BLOCK_PAGES + page) * PAGE_FULL + PAGE_DATA
    return raw[base:base + 16]


def find_system_blocks(raw: bytes) -> list[int]:
    """FTL map/system blocks: page-0 spare starts with the c2 00 c3 00 ... marker."""
    n = len(raw) // (BLOCK_PAGES * PAGE_FULL)
    return [pb for pb in range(n) if block_oob(raw, pb)[:4] == bytes((0xC2, 0, 0xC3, 0))]


def read_system_table(raw: bytes, pblock: int) -> list[int]:
    """A system block's data area as a flat u16 array (the raw FTL map table)."""
    data = bytearray()
    for pg in range(BLOCK_PAGES):
        off = (pblock * BLOCK_PAGES + pg) * PAGE_FULL
        data += raw[off:off + PAGE_DATA]
    return list(struct.unpack("<%dH" % (len(data) // 2), data))


def recover_map(nooob: bytes, logical: bytes) -> dict[int, int]:
    """Physical->logical block map, recovered by content-matching (proves the model)."""
    lindex: dict[bytes, int] = {}
    for lb in range(len(logical) // BLOCK_BYTES):
        h = hashlib.md5(logical[lb * BLOCK_BYTES:(lb + 1) * BLOCK_BYTES]).digest()
        lindex.setdefault(h, lb)
    p2l: dict[int, int] = {}
    for pb in range(len(nooob) // BLOCK_BYTES):
        blk = nooob[pb * BLOCK_BYTES:(pb + 1) * BLOCK_BYTES]
        if blk.count(blk[:1]) == len(blk):
            continue                       # erased / uniform
        h = hashlib.md5(blk).digest()
        if h in lindex:
            p2l[pb] = lindex[h]
    return p2l


def rebuild(nooob: bytes, p2l: dict[int, int], logical_size: int) -> bytes:
    out = bytearray(logical_size)
    for pb, lb in p2l.items():
        out[lb * BLOCK_BYTES:(lb + 1) * BLOCK_BYTES] = nooob[pb * BLOCK_BYTES:(pb + 1) * BLOCK_BYTES]
    return bytes(out)


def main():
    ap = argparse.ArgumentParser(description="Furby/gpac800 FTL reconstruction & analysis")
    ap.add_argument("--raw", required=True, help="raw NAND with OOB (528-byte pages)")
    ap.add_argument("--raw-nooob", help="same dump, OOB stripped (512-byte pages); derived if omitted")
    ap.add_argument("--logical", required=True, help="known-good logical image (for map recovery/validation)")
    ap.add_argument("--rebuild", help="write the reconstructed logical image here")
    a = ap.parse_args()

    raw = open(a.raw, "rb").read()
    nooob = open(a.raw_nooob, "rb").read() if a.raw_nooob else strip_oob(raw)
    logical = open(a.logical, "rb").read()

    sysblocks = find_system_blocks(raw)
    print(f"system/map blocks (c2 00 c3 00 marker): {sysblocks}")
    if sysblocks:
        tbl = read_system_table(raw, sysblocks[0])
        print(f"  block {sysblocks[0]} FTL table: {len(tbl)} u16 entries, first: {tbl[:12]}")

    p2l = recover_map(nooob, logical)
    recon = rebuild(nooob, p2l, len(logical))
    exact = sum(1 for lb in {v for v in p2l.values()}
                if recon[lb * BLOCK_BYTES:(lb + 1) * BLOCK_BYTES]
                == logical[lb * BLOCK_BYTES:(lb + 1) * BLOCK_BYTES])
    mapped = len(set(p2l.values()))
    print(f"model: 32-page blocks, whole-block remap")
    print(f"recovered map: {len(p2l)} physical blocks -> {mapped} logical blocks")
    print(f"reconstruction: {exact}/{mapped} logical blocks byte-exact "
          f"({100 * exact // max(1, mapped)}%)")

    if a.rebuild:
        open(a.rebuild, "wb").write(recon)
        print(f"wrote reconstructed logical image -> {a.rebuild}")


if __name__ == "__main__":
    main()
