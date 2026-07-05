#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FurbyROM (.fby) — a single-file container for everything the emulator needs.

Bundles the firmware, the NAND image (or a raw OOB dump), and any extra assets
into one compressed file with a table of contents, so the emulator loads a ROM in
one shot instead of juggling loose dumps.

Layout (all little-endian):

    magic      8   "FURBYROM"
    version    u16  = 1
    flags      u16  bit0 = sections are zlib-compressed
    n_entries  u32
    --- TOC, n_entries of: ---
      kind        u16     see KIND_* (firmware / nand / nand_raw / asset)
      name_len    u16
      name        name_len bytes, utf-8
      offset      u64     into the data section
      stored_len  u64     bytes on disk (compressed if flag set)
      orig_len    u64     uncompressed length
      crc32       u32     of the uncompressed bytes
    --- DATA ---
      concatenated (optionally zlib-compressed) section bytes

Usage:
    python3 rom_pack.py build --gamecode GameCode.bin --nand nand.bin \
        [--nand-raw NANDmainFLASH.BIN] [--asset name=path ...] --out furby.fby
    python3 rom_pack.py list furby.fby
    python3 rom_pack.py extract furby.fby NAME --out file       # unpack one section
"""
from __future__ import annotations
import argparse, struct, zlib

MAGIC = b"FURBYROM"
VERSION = 1
FLAG_ZLIB = 1

KIND_FIRMWARE = 0     # GameCode.bin
KIND_NAND = 1         # logical NAND image (the emulator boots from this)
KIND_NAND_RAW = 2     # raw physical NAND dump (+OOB), reconstructed on load
KIND_ASSET = 3        # anything else (personality dir tarball, notes, …)
KIND_NAMES = {0: "firmware", 1: "nand", 2: "nand_raw", 3: "asset"}


def build(sections: list[tuple[int, str, bytes]], compress: bool = True) -> bytes:
    """sections: list of (kind, name, raw_bytes) -> the .fby bytes."""
    flags = FLAG_ZLIB if compress else 0
    toc = bytearray()
    data = bytearray()
    for kind, name, raw in sections:
        blob = zlib.compress(raw, 9) if compress else raw
        nb = name.encode("utf-8")
        toc += struct.pack("<HH", kind, len(nb)) + nb
        toc += struct.pack("<QQQI", len(data), len(blob), len(raw), zlib.crc32(raw) & 0xFFFFFFFF)
        data += blob
    head = MAGIC + struct.pack("<HHI", VERSION, flags, len(sections))
    return bytes(head + toc + data)


def parse(rom: bytes):
    """Return (flags, [ (kind,name,offset,stored_len,orig_len,crc), … ], data_base)."""
    if rom[:8] != MAGIC:
        raise SystemExit("not a FurbyROM (.fby)")
    ver, flags, n = struct.unpack_from("<HHI", rom, 8)
    if ver != VERSION:
        raise SystemExit(f"unsupported .fby version {ver}")
    entries = []
    p = 16
    for _ in range(n):
        kind, nl = struct.unpack_from("<HH", rom, p); p += 4
        name = rom[p:p + nl].decode("utf-8"); p += nl
        off, slen, olen, crc = struct.unpack_from("<QQQI", rom, p); p += 28
        entries.append((kind, name, off, slen, olen, crc))
    return flags, entries, p


def section_bytes(rom: bytes, flags, entries, data_base, name=None, kind=None) -> bytes:
    for k, nm, off, slen, olen, crc in entries:
        if (name is not None and nm == name) or (kind is not None and k == kind):
            blob = rom[data_base + off: data_base + off + slen]
            raw = zlib.decompress(blob) if (flags & FLAG_ZLIB) else blob
            if zlib.crc32(raw) & 0xFFFFFFFF != crc:
                raise SystemExit(f"CRC mismatch on section '{nm}'")
            return raw
    raise KeyError(name or kind)


def load_for_emulator(path: str):
    """Return (gamecode_bytes, nand_bytes) from a .fby, reconstructing raw NAND if present."""
    rom = open(path, "rb").read()
    flags, entries, base = parse(rom)
    gamecode = section_bytes(rom, flags, entries, base, kind=KIND_FIRMWARE)
    try:
        nand = section_bytes(rom, flags, entries, base, kind=KIND_NAND)
    except KeyError:
        raw = section_bytes(rom, flags, entries, base, kind=KIND_NAND_RAW)
        import os, sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import ftl_reconstruct as FTL
        nooob = FTL.strip_oob(raw) if len(raw) % FTL.PAGE_FULL == 0 else raw
        raise SystemExit("nand_raw in ROM needs a reference to reconstruct; pack a logical --nand")
    return gamecode, nand


def main():
    ap = argparse.ArgumentParser(description="FurbyROM (.fby) single-file container")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="pack files into a .fby")
    b.add_argument("--gamecode", required=True)
    b.add_argument("--nand", help="logical NAND image")
    b.add_argument("--nand-raw", help="raw physical NAND dump (+OOB)")
    b.add_argument("--asset", action="append", default=[], metavar="NAME=PATH",
                   help="extra section (repeatable)")
    b.add_argument("--no-compress", action="store_true")
    b.add_argument("--out", required=True)

    l = sub.add_parser("list", help="show a .fby's contents"); l.add_argument("rom")
    e = sub.add_parser("extract", help="unpack one section"); e.add_argument("rom")
    e.add_argument("name"); e.add_argument("--out", required=True)
    a = ap.parse_args()

    if a.cmd == "build":
        secs = [(KIND_FIRMWARE, "GameCode.bin", open(a.gamecode, "rb").read())]
        if a.nand:
            secs.append((KIND_NAND, "nand.bin", open(a.nand, "rb").read()))
        if a.nand_raw:
            secs.append((KIND_NAND_RAW, "nand_raw.bin", open(a.nand_raw, "rb").read()))
        for spec in a.asset:
            name, _, path = spec.partition("=")
            secs.append((KIND_ASSET, name, open(path, "rb").read()))
        rom = build(secs, compress=not a.no_compress)
        open(a.out, "wb").write(rom)
        orig = sum(len(r) for _, _, r in secs)
        print(f"wrote {a.out}: {len(secs)} sections, {orig/1e6:.1f} MB -> {len(rom)/1e6:.1f} MB "
              f"({100*len(rom)//max(1,orig)}%)")

    elif a.cmd == "list":
        rom = open(a.rom, "rb").read()
        flags, entries, _ = parse(rom)
        print(f"FurbyROM v{VERSION}  compressed={bool(flags & FLAG_ZLIB)}  {len(entries)} sections")
        for k, nm, off, slen, olen, crc in entries:
            print(f"  [{KIND_NAMES.get(k, k):8}] {nm:16} {olen:>12,} B  (stored {slen:>12,})  crc {crc:08x}")

    elif a.cmd == "extract":
        rom = open(a.rom, "rb").read()
        flags, entries, base = parse(rom)
        open(a.out, "wb").write(section_bytes(rom, flags, entries, base, name=a.name))
        print(f"extracted '{a.name}' -> {a.out}")


if __name__ == "__main__":
    main()
