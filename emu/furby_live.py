#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Furby Connect — live CLI player.

Plays a personality's real eye animation (SPR playlist 8) right in the terminal
using 24-bit-color half-block characters — two eyes, side by side, looping like
the toy. Pure Python, no dependencies.

    python3 furby_live.py /path/to/Personalities/Base
    python3 furby_live.py /path/to/Personalities/Base --palette 64 --fps 10 --size 40

Ctrl-C to quit.
"""
from __future__ import annotations
import argparse, glob, os, sys, time

import furby_display as FD   # same directory


def downscale(idx_rgb, size):
    """Nearest-neighbor downscale a 128x128 RGB grid to size x size."""
    n = len(idx_rgb)
    out = []
    for y in range(size):
        row = []
        sy = y * n // size
        for x in range(size):
            sx = x * n // size
            row.append(idx_rgb[sy][sx])
        out.append(row)
    return out


def frame_to_ansi(rgb, bg=(9, 12, 15)):
    """Two vertical pixels per char via the upper-half block ▀ (fg=top, bg=bottom)."""
    h = len(rgb)
    w = len(rgb[0])
    lines = []
    for y in range(0, h - 1, 2):
        parts = []
        for x in range(w):
            t = rgb[y][x]
            b = rgb[y + 1][x]
            t = bg if t == (0, 0, 0) else t
            b = bg if b == (0, 0, 0) else b
            parts.append(f"\x1b[38;2;{t[0]};{t[1]};{t[2]}m\x1b[48;2;{b[0]};{b[1]};{b[2]}m▀")
        lines.append("".join(parts) + "\x1b[0m")
    return lines


def main():
    ap = argparse.ArgumentParser(description="Play a Furby's eye animation in the terminal.")
    ap.add_argument("personality", help="a personality dir (…/Personalities/Base)")
    ap.add_argument("--palette", type=int, default=None, help="palette bank offset (colors)")
    ap.add_argument("--fps", type=float, default=9.0, help="frames per second")
    ap.add_argument("--size", type=int, default=40, help="eye size in terminal cells")
    ap.add_argument("--once", action="store_true", help="play once instead of looping")
    a = ap.parse_args()

    cel_p, pal_p, spr_p = (FD._find(a.personality, e) for e in (".CEL", ".PAL", ".SPR"))
    if not (cel_p and pal_p and spr_p):
        sys.exit(f"need .CEL/.PAL/.SPR in {a.personality}")
    cel = open(cel_p, "rb").read()
    colors = FD.load_palettes(open(pal_p, "rb").read())
    playlists = FD.parse_spr(open(spr_p, "rb").read())
    frames = playlists[FD.eye_playlist_index(playlists)]

    name = os.path.basename(a.personality.rstrip("/")).upper()
    palette = a.palette if a.palette is not None else (FD.PALETTE_PRESETS.get(name) or
                                                       FD.detect_palette(cel, frames, colors))
    bank = FD.palette_bank(colors, palette)

    # pre-render every frame as two eyes' worth of ANSI lines
    rendered = []
    for f in frames:
        rgb = [[bank[i] for i in row] for row in FD.render_frame_indices(cel, f)]
        small = downscale(rgb, a.size)
        left = frame_to_ansi(small)
        right = frame_to_ansi([list(reversed(r)) for r in small])   # mirror
        rendered.append([l + "  " + r for l, r in zip(left, right)])

    title = f"  \x1b[1;96mFURBY CONNECT\x1b[0m  \x1b[90m{name} · emulator · live\x1b[0m"
    sys.stdout.write("\x1b[2J\x1b[?25l")   # clear, hide cursor
    try:
        i = 0
        while True:
            sys.stdout.write("\x1b[H\n" + title + "\n\n")
            sys.stdout.write("\n".join(rendered[i]))
            sys.stdout.write(f"\n\n  \x1b[90mframe {i+1:02d}/{len(frames)}   Ctrl-C to quit\x1b[0m\n")
            sys.stdout.flush()
            i += 1
            if i >= len(frames):
                if a.once:
                    break
                i = 0
            time.sleep(1.0 / a.fps)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?25h\x1b[0m\n")   # restore cursor


if __name__ == "__main__":
    main()
