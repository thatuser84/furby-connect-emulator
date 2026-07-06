#!/usr/bin/env python3
"""Render the Furby eye from the RUNNING firmware — tiles it selects (playlist 8) AND
the palette it loads into the PPU (0x7300), no presets. Also exports the 14-frame blink.

    python3 tools/render_live_eye.py --gamecode GameCode.bin --nand nand.bin --gif eye.gif
"""
import sys, os, struct, argparse
HERE=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(HERE),"emu"))
import unsp_native as NAT, furby_display as FD

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--gamecode",required=True); ap.add_argument("--nand",required=True)
    ap.add_argument("--png",default="furby_eye_LIVE.png"); ap.add_argument("--gif",default=None)
    ap.add_argument("--cel-nand",type=lambda x:int(x,0),default=0xa2f600)
    ap.add_argument("--spr-nand",type=lambda x:int(x,0),default=0x241c200)
    a=ap.parse_args()
    img=open(a.gamecode,"rb").read(); nand=open(a.nand,"rb").read()
    cpu=NAT.default_furby_cpu(img, nand_bytes=nand)   # loader HLE baked in
    cpu.add_hle(0x08fc17,6); cpu.run(600_000_000)
    cpu.set_autoclear(0x7961,0x30); cpu.set_reador(0x7961,0x80); cpu.set_autoclear(0x7072,0xffff)
    for _ in range(10): cpu.raise_irq(5); cpu.run(400_000)
    cpu.poke(0x534f,1)
    for _ in range(30): cpu.raise_irq(5); cpu.run(1_500_000)
    rgb=lambda v:((v>>10&31)*255//31,(v>>5&31)*255//31,(v&31)*255//31)
    bank=[rgb(cpu.mmio_last(0x7300+i)) for i in range(64)]   # firmware's LIVE palette
    tiles=[cpu.spriteram_get(i) for i in (0,4,8,12)]
    print(f"firmware live: PPU tiles {tiles} (playlist-8 frame-0 quarter-cels), "
          f"{len([c for c in bank if c!=(0,0,0)])}-colour palette")
    CEL=nand[a.cel_nand:a.cel_nand+0x19d9400]; SPR=nand[a.spr_nand:a.spr_nand+0x6ab58]
    frames=FD.parse_spr(SPR)[8]
    FD.write_png(a.png, FD.render_frame_indices(CEL, frames[0]), bank, scale=4)
    print(f"wrote {a.png}")
    if a.gif:
        FD.write_gif(a.gif, [FD.render_frame_indices(CEL,f) for f in frames], bank, scale=3, delay_cs=10)
        print(f"wrote {a.gif} ({len(frames)} frames)")

if __name__=="__main__": main()
