#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Furby Connect eye display — the "PPU".

The Furby doesn't drive its round eye LCDs through the GPL16258's standard
sprite/tilemap PPU (those registers stay empty). It plays pre-rendered eye
animations stored in flash as per-personality graphics:

    <Personality>.CEL   the pixels: 64x64 cels, 6bpp palette indices
    <Personality>.PAL   the color tables: 64-color RGB555 banks (0x80 bytes each)
    <Personality>.SPR   animation playlists -> frames (each frame = 4 quarter-cels)
    <Personality>.SEQ   higher-level sequencing

Formats verified against Furby-ReConnect's `furby.py` (l0ss/swarley) and the
furbhax teardown, then reimplemented here dependency-free.

CEL: each cel is one 64x64 tile = 0xC00 bytes, stored as 64 rows x 48 bytes,
3 bytes -> 4 six-bit pixels (MSB-first), each pixel a palette index 0..63.

SPR: a 0xE0-byte header of 16 playlists (framecount:u16, t2_off:u32, layer:u32,
0x40), then per-playlist frame-pointer tables, then frames. Each frame is nine
u16s: [cel0,pal0, cel1,pal1, cel2,pal2, cel3,pal3, 0xFFFF] — four 64x64
quarter-cels laid out TL, TR, BL, BR into one 128x128 eye. Playlist 8 is the
eye animation.

Usage:
    python3 furby_display.py PERSONALITY_DIR --out eyes/ [--gif eye.gif]
"""
from __future__ import annotations
import argparse, glob, os, struct, zlib

CEL_BYTES = 0xC00        # one 64x64 cel
CEL_W = CEL_H = 64
EYE_PX = 128             # a frame is 128x128 (2x2 cels)
SPR_HEADER = 0xE0
FRAME_WORDS = 9          # cel0,pal0,cel1,pal1,cel2,pal2,cel3,pal3,terminator
QUAD_LAYOUT = [(0, 0), (0, 64), (64, 0), (64, 64)]   # TL, TR, BL, BR


# ---- CEL: 6bpp pixels ----------------------------------------------------
def unpack_row(rowbytes: bytes, width: int = CEL_W) -> list[int]:
    """48 bytes -> 64 six-bit palette indices (3 bytes = 4 pixels, MSB-first)."""
    out = []
    for i in range(0, len(rowbytes) - 2, 3):
        b0, b1, b2 = rowbytes[i], rowbytes[i + 1], rowbytes[i + 2]
        out.append(b0 >> 2)
        out.append(((b0 & 0x03) << 4) | (b1 >> 4))
        out.append(((b1 & 0x0F) << 2) | (b2 >> 6))
        out.append(b2 & 0x3F)
    return out[:width]


def decode_cel(cel: bytes, index: int) -> list[list[int]]:
    """Cel #index as a 64x64 grid of palette indices."""
    off = index * CEL_BYTES
    return [unpack_row(cel[off + r * 48: off + r * 48 + 48]) for r in range(CEL_H)]


# ---- PAL: RGB555 banks ---------------------------------------------------
def rgb555(v: int) -> tuple[int, int, int]:
    return ((v >> 10 & 31) * 255 // 31, (v >> 5 & 31) * 255 // 31, (v & 31) * 255 // 31)


def load_palettes(pal_bytes: bytes) -> list[int]:
    return [pal_bytes[i] | pal_bytes[i + 1] << 8 for i in range(0, len(pal_bytes) - 1, 2)]


def palette_bank(colors: list[int], offset: int) -> list[tuple[int, int, int]]:
    bank = colors[offset:offset + 64]
    bank += [0] * (64 - len(bank))
    return [rgb555(v) for v in bank]


# ---- SPR: playlists -> frames -------------------------------------------
def parse_spr(spr: bytes) -> list[list[list[int]]]:
    """Return 16 playlists, each a list of frames ([cel,pal] x4 + terminator)."""
    def frame_at(word_off: int) -> list[int]:
        o = word_off * 2
        return [struct.unpack_from("<H", spr, o + 2 * k)[0] for k in range(FRAME_WORDS)]

    playlists = []
    for w in range(16):
        o = w * 14
        framecount = struct.unpack_from("<H", spr, o)[0]
        t2_off = struct.unpack_from("<I", spr, o + 2)[0]
        offs = [struct.unpack_from("<I", spr, t2_off * 2 + 4 * i)[0] for i in range(framecount)]
        playlists.append([frame_at(p) for p in offs])
    return playlists


def eye_playlist_index(playlists: list) -> int:
    """The eye animation is playlist 8; fall back to the longest playlist."""
    if len(playlists) > 8 and playlists[8]:
        return 8
    return max(range(len(playlists)), key=lambda w: len(playlists[w]))


def render_frame_indices(cel: bytes, frame: list[int]) -> list[list[int]]:
    """Assemble a frame's four quarter-cels into a 128x128 index grid."""
    big = [[0] * EYE_PX for _ in range(EYE_PX)]
    for k, (ry, cx) in enumerate(QUAD_LAYOUT):
        rows = decode_cel(cel, frame[k * 2])
        for y in range(CEL_H):
            big[ry + y][cx:cx + CEL_W] = rows[y]
    return big


# ---- palette selection ---------------------------------------------------
PALETTE_PRESETS = {"BASE": 64}   # verified; others fall back to auto-detect


def _score(cel: bytes, frame: list[int], bank) -> float:
    """Colorful *and* smooth = a real eye (rainbow-noise palettes are jumpy)."""
    idx = render_frame_indices(cel, frame)
    vivid, jump, n = set(), 0, 0
    for y in range(EYE_PX):
        row = [bank[i] for i in idx[y]]
        for x in range(EYE_PX):
            r, g, b = row[x]
            if max(r, g, b) - min(r, g, b) > 40:
                vivid.add((r >> 4, g >> 4, b >> 4))
            if x < EYE_PX - 1:
                a = row[x + 1]
                jump += abs(r - a[0]) + abs(g - a[1]) + abs(b - a[2])
                n += 1
    return len(vivid) / (1 + (jump / max(1, n)) / 8)


def detect_palette(cel: bytes, frames: list, colors: list[int]) -> int:
    probe = max(frames, key=lambda f: _score(cel, f, palette_bank(colors, 64)))
    best = (-1.0, 0)
    for pb in range(0, max(1, len(colors) - 64), 32):
        s = _score(cel, probe, palette_bank(colors, pb))
        if s > best[0]:
            best = (s, pb)
    return best[1]


# ---- PNG / animated GIF --------------------------------------------------
def write_png(path: str, idx: list[list[int]], bank, scale: int = 4) -> None:
    h, w = len(idx), len(idx[0])

    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    rows = []
    for row in idx:
        raw = bytearray()
        for i in row:
            raw += bytes(bank[i]) * scale
        for _ in range(scale):
            rows.append(b"\x00" + bytes(raw))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w * scale, h * scale, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(b"".join(rows), 9)))
        f.write(chunk(b"IEND", b""))


def _lzw_uncompressed(indices: list[int], mcs: int) -> bytes:
    clear, end, code_size = 1 << mcs, (1 << mcs) + 1, mcs + 1
    bits = nb = 0
    buf = bytearray()

    def emit(code):
        nonlocal bits, nb
        bits |= code << nb
        nb += code_size
        while nb >= 8:
            buf.append(bits & 0xFF)
            bits >>= 8
            nb -= 8

    emit(clear)
    since = 0
    for v in indices:
        emit(v)
        since += 1
        if since == clear - 2:
            emit(clear)
            since = 0
    emit(end)
    if nb:
        buf.append(bits & 0xFF)
    return bytes(buf)


def write_gif(path: str, frames_idx: list, bank, scale: int = 2, delay_cs: int = 14) -> None:
    h = len(frames_idx[0]) * scale
    w = len(frames_idx[0][0]) * scale
    gct = b"".join(bytes(c) for c in (bank + [(0, 0, 0)] * 256)[:256])
    out = bytearray(b"GIF89a")
    out += struct.pack("<HH", w, h) + bytes((0xF7, 0, 0)) + gct
    out += b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
    for fr in frames_idx:
        px = []
        for row in fr:
            up = []
            for i in row:
                up += [i] * scale
            for _ in range(scale):
                px.extend(up)
        out += b"\x21\xf9\x04\x04" + struct.pack("<H", delay_cs) + b"\x00\x00"
        out += b"\x2c" + struct.pack("<HHHH", 0, 0, w, h) + b"\x00\x08"
        comp = _lzw_uncompressed(px, 8)
        for i in range(0, len(comp), 255):
            out += bytes((len(comp[i:i + 255]),)) + comp[i:i + 255]
        out += b"\x00"
    out += b"\x3b"
    open(path, "wb").write(bytes(out))


# ---- driver --------------------------------------------------------------
def _find(pdir, ext):
    hits = glob.glob(os.path.join(pdir, "*"))
    for p in hits:
        if p.upper().endswith(ext):
            return p
    return None


def dump_eyes(pdir: str, out: str, palette: int | None = None, gif: str | None = None):
    cel_p, pal_p, spr_p = _find(pdir, ".CEL"), _find(pdir, ".PAL"), _find(pdir, ".SPR")
    if not (cel_p and pal_p and spr_p):
        raise SystemExit(f"need .CEL/.PAL/.SPR in {pdir}")
    cel = open(cel_p, "rb").read()
    colors = load_palettes(open(pal_p, "rb").read())
    playlists = parse_spr(open(spr_p, "rb").read())
    frames = playlists[eye_playlist_index(playlists)]

    if palette is None:
        name = os.path.basename(pdir.rstrip("/")).upper()
        palette = PALETTE_PRESETS.get(name) or detect_palette(cel, frames, colors)
    bank = palette_bank(colors, palette)

    os.makedirs(out, exist_ok=True)
    idx_frames = [render_frame_indices(cel, f) for f in frames]
    for i, fr in enumerate(idx_frames):
        write_png(os.path.join(out, f"eye_{i:03d}.png"), fr, bank)
    if gif:
        write_gif(gif, idx_frames, bank)
        print(f"  animated eye -> {gif}")
    print(f"[{os.path.basename(pdir)}] eye animation: {len(frames)} frames @ palette {palette} -> {out}/")
    return len(frames)


def main():
    ap = argparse.ArgumentParser(description="Decode & render Furby Connect eye animations.")
    ap.add_argument("personality", help="a personality dir (…/Personalities/Base)")
    ap.add_argument("--out", default="eyes", help="output directory for PNG frames")
    ap.add_argument("--palette", type=int, default=None, help="palette bank offset (colors)")
    ap.add_argument("--gif", default=None, help="also write an animated GIF here")
    a = ap.parse_args()
    dump_eyes(a.personality, a.out, a.palette, a.gif)


if __name__ == "__main__":
    main()
