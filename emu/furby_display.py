#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Furby Connect eye display — the "PPU".

The Furby doesn't drive its round eye LCDs through the GPL16258's standard
sprite/tilemap PPU (those registers stay empty). Instead the firmware plays
pre-rendered eye animations stored in flash as personality graphics:

    <Personality>.CEL   6-bit-per-pixel tile data (the pixels, as palette indices)
    <Personality>.PAL   the color tables (RGB555, 64 colors per palette bank)
    <Personality>.SPR   cell/sprite placement list  (offset, x, size)
    <Personality>.SEQ   animation sequence          (which cells, timing)

This module decodes that format — reverse-engineered here and cross-checked
against the furbhax/Furby-Extra reference decodes — and renders the eyes the
way the panel shows them.

Tile format (from the WAHCKon "furbhax" teardown):
    * a tile is 48x16 bytes  ->  64x16 pixels at 6 bpp
    * pixels are packed 6 bits each, MSB-first across the row (64 per 48-byte row)
    * tiles group in blocks of 8; four stacked tiles make a 64x64 eye
    * each pixel value (0..63) indexes a 64-color RGB555 palette bank

Usage:
    python3 furby_display.py PERSONALITY_DIR --out eyes/           # dump frames
    python3 furby_display.py PERSONALITY_DIR --out eyes/ --palette 580
    # e.g. Base's default (blue "generic") eye lives at palette bank offset 580;
    #      Base's purple animation at 64.  --auto tries to pick a lively bank.
"""
from __future__ import annotations
import argparse, os, struct, zlib, glob

TILE_BYTES = 768          # 48 * 16
TILE_W, TILE_H = 64, 16
EYE_TILES = 4             # 4 stacked 64x16 tiles -> one 64x64 eye


# ---- 6bpp unpack ----------------------------------------------------------
def unpack_row(rowbytes: bytes, width: int = TILE_W) -> list[int]:
    """Unpack `width` 6-bit palette indices from a packed byte row (MSB-first)."""
    bits = 0
    nbits = 0
    out = []
    for b in rowbytes:
        bits = (bits << 8) | b
        nbits += 8
        while nbits >= 6 and len(out) < width:
            nbits -= 6
            out.append((bits >> nbits) & 0x3F)
    return out[:width]


def decode_tile(cel: bytes, off: int) -> list[list[int]]:
    return [unpack_row(cel[off + r * 48: off + r * 48 + 48]) for r in range(TILE_H)]


def decode_eye_indices(cel: bytes, off: int) -> list[list[int]]:
    """One 64x64 eye = four stacked 64x16 tiles, as raw palette indices."""
    rows: list[list[int]] = []
    for t in range(EYE_TILES):
        rows.extend(decode_tile(cel, off + t * TILE_BYTES))
    return rows


# ---- palette --------------------------------------------------------------
def rgb555(v: int) -> tuple[int, int, int]:
    return ((v >> 10 & 31) * 255 // 31, (v >> 5 & 31) * 255 // 31, (v & 31) * 255 // 31)


def load_palettes(pal_bytes: bytes) -> list[int]:
    """Return the palette as a flat list of 16-bit color words."""
    return [pal_bytes[i] | pal_bytes[i + 1] << 8 for i in range(0, len(pal_bytes) - 1, 2)]


def palette_bank(colors: list[int], offset: int) -> list[tuple[int, int, int]]:
    bank = colors[offset:offset + 64]
    bank += [0] * (64 - len(bank))
    return [rgb555(v) for v in bank]


def full_palette_offsets(colors: list[int]) -> list[int]:
    """Offsets of 64-color windows that are fully populated (candidate eye banks)."""
    outs = []
    for base in range(0, max(0, len(colors) - 64), 4):   # coarse stride keeps it fast
        if all(colors[base:base + 64]):
            outs.append(base)
    return outs


# ---- rendering ------------------------------------------------------------
def write_png(path: str, pixels: list[list[tuple[int, int, int]]], scale: int = 6) -> None:
    h = len(pixels)
    w = len(pixels[0])

    def chunk(typ: bytes, data: bytes) -> bytes:
        c = typ + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    rows = []
    for row in pixels:
        raw = bytearray()
        for (r, g, b) in row:
            raw += bytes((r, g, b)) * scale
        for _ in range(scale):
            rows.append(b"\x00" + bytes(raw))
    ihdr = struct.pack(">IIBBBBB", w * scale, h * scale, 8, 2, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", zlib.compress(b"".join(rows), 9)))
        f.write(chunk(b"IEND", b""))


def render_eye(cel: bytes, off: int, bank: list[tuple[int, int, int]]):
    return [[bank[i] for i in row] for row in decode_eye_indices(cel, off)]


def frame_variety(cel: bytes, off: int) -> int:
    return len({i for row in decode_eye_indices(cel, off) for i in row})


def is_complete_eye(cel: bytes, off: int, bank: list[tuple[int, int, int]]) -> bool:
    """A framed, centered eye: colorful, with a dark pupil in the middle and
    lit corners (the round eye sitting in its frame)."""
    if frame_variety(cel, off) < 44:
        return False
    img = render_eye(cel, off, bank)
    lum = lambda p: p[0] + p[1] + p[2]
    dark_center = sum(1 for y in range(26, 38) for x in range(26, 38) if lum(img[y][x]) < 200)
    bright_corner = sum(1 for y in (2, 3, 60, 61) for x in (2, 3, 60, 61) if lum(img[y][x]) > 600)
    return dark_center >= 90 and bright_corner >= 6


# Known-good palette banks per personality (the SPR/SEQ chain that maps each
# animation to its palette isn't fully decoded yet; these are verified by eye).
PALETTE_PRESETS = {
    "BASE": 64,        # Base's violet galaxy eye
    "GENERIC": 580,    # the default blue eye (Base.PAL bank 580)
    "CAT": 64, "DJ": 64, "NINJA": 64, "PIRATE": 64, "POPSTAR": 64, "PRINCESS": 64,
}


# ---- personality helpers --------------------------------------------------
def find_personality_files(pdir: str):
    cel = pal = None
    for p in glob.glob(os.path.join(pdir, "*")):
        u = p.upper()
        if u.endswith(".CEL"):
            cel = p
        elif u.endswith(".PAL"):
            pal = p
    return cel, pal


def detect_eye_palette(cel: bytes, colors: list[int], samples: int = 12) -> int:
    """Pick the palette bank that renders the most complete, framed eyes over a
    sample of cells — recovers each personality's own eye palette."""
    banks = full_palette_offsets(colors) or [64]
    # sample cell offsets that at least have image variety (skip flat cells)
    step = TILE_BYTES * EYE_TILES
    sample_offs = []
    off = 0
    while off + step <= len(cel) and len(sample_offs) < samples:
        if frame_variety(cel, off) >= 44:
            sample_offs.append(off)
        off += step
    best, best_bank = -1, banks[0]
    for pb in banks:
        bank = palette_bank(colors, pb)
        hits = sum(1 for o in sample_offs if is_complete_eye(cel, o, bank))
        if hits > best:
            best, best_bank = hits, pb
    return best_bank


def dump_eyes(pdir: str, out: str, palette: int | None, auto: bool, count: int, stride: int):
    cel_path, pal_path = find_personality_files(pdir)
    if not cel_path or not pal_path:
        raise SystemExit(f"no .CEL/.PAL in {pdir} (Generic uses shared graphics)")
    cel = open(cel_path, "rb").read()
    colors = load_palettes(open(pal_path, "rb").read())

    if palette is None:
        name = os.path.basename(pdir.rstrip("/")).upper()
        if not auto and name in PALETTE_PRESETS:
            palette = PALETTE_PRESETS[name]
        else:
            palette = detect_eye_palette(cel, colors)
    bank = palette_bank(colors, palette)

    os.makedirs(out, exist_ok=True)
    saved = 0
    off = 0
    while off + EYE_TILES * TILE_BYTES <= len(cel) and saved < count:
        if is_complete_eye(cel, off, bank):                 # framed, centered eyes only
            write_png(os.path.join(out, f"eye_{saved:03d}.png"), render_eye(cel, off, bank))
            saved += 1
        off += stride
    print(f"[{os.path.basename(pdir)}] palette bank {palette} -> {saved} eye frames in {out}/")
    return saved


def main():
    ap = argparse.ArgumentParser(description="Decode & render Furby Connect eye animations.")
    ap.add_argument("personality", help="path to a personality dir (…/Personalities/Base)")
    ap.add_argument("--out", default="eyes", help="output directory for PNG frames")
    ap.add_argument("--palette", type=int, default=None, help="palette bank offset (colors)")
    ap.add_argument("--auto", action="store_true", help="auto-pick a lively palette bank")
    ap.add_argument("--count", type=int, default=48, help="max frames to render")
    ap.add_argument("--stride", type=int, default=TILE_BYTES * EYE_TILES, help="byte stride between frames")
    a = ap.parse_args()
    dump_eyes(a.personality, a.out, a.palette, a.auto, a.count, a.stride)


if __name__ == "__main__":
    main()
