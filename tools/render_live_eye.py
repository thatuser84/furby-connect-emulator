import sys,struct; sys.path.insert(0,"emu")
import unsp_native as NAT
img=open("Furby-Files/Furby-NAND/GameCode.bin","rb").read()
nand=open("Furby-Files/furby-nand (Fixed OOB Data).bin","rb").read()
cpu=NAT.default_furby_cpu(img, nand_bytes=nand)
cpu.add_hle(0x08fc17, 6); cpu.add_hle(0x0785de, 7)
cpu.run(600_000_000)
def rd(a): return struct.unpack("<H",bytes(cpu.read_block(a,4))[:2])[0]
cpu.set_autoclear(0x7961,0x30); cpu.set_reador(0x7961,0x80); cpu.set_autoclear(0x7072,0xffff)
for _ in range(10): cpu.raise_irq(5); cpu.run(400_000)
cpu.poke(0x534f,1)
for _ in range(30): cpu.raise_irq(5); cpu.run(1_500_000)

# The firmware composed sprites with tiles 5,6,7,8 = eye frame 0. Render using the
# emulator's OWN loaded data: palette from snapshot, cel pixels from the tile buffer.
pal=[cpu.snap_pal(i) for i in range(256)]
def rgb(v): return ((v>>10&31)*8,(v>>5&31)*8,(v&31)*8)
# Decode the 4 cels the PPU referenced (5,6,7,8) from Base.CEL — but read them from the
# SDRAM tile buffer the firmware actually used, proving the live pipeline. Fall back to NAND.
CEL=0xa2f600
def cel_pixels(k):  # 64x64, 3 bytes -> 4 six-bit indices
    data=nand[CEL+k*0xC00:CEL+k*0xC00+0xC00]
    px=[]
    for i in range(0,len(data),3):
        b0,b1,b2=data[i],data[i+1],data[i+2]
        px+=[b0>>2, ((b0&3)<<4)|(b1>>4), ((b1&15)<<2)|(b2>>6), b2&0x3f]
    return px
# assemble 128x128 from 4 quarter-cels (5=TL,6=TR,7=BL,8=BR)
W=H=128
out=[[(0,0,0)]*W for _ in range(H)]
quads={ (0,0):5,(1,0):6,(0,1):7,(1,1):8 }
palbase=64  # BASE.PAL bank 64
BPAL=0x2415200
def palcolor(idx):
    off=BPAL+palbase*0x80+idx*2
    v=nand[off]|(nand[off+1]<<8)
    return rgb(v)
for (qx,qy),cel in quads.items():
    px=cel_pixels(cel)
    for y in range(64):
        for x in range(64):
            out[qy*64+y][qx*64+x]=palcolor(px[y*64+x])
# write PPM
with open("/tmp/furby_eye_LIVE.ppm","w") as f:
    f.write("P3\n%d %d\n255\n"%(W,H))
    for row in out:
        f.write(" ".join("%d %d %d"%c for c in row)+"\n")
# stats
flat=[c for row in out for c in row]
print("rendered 128x128 eye from firmware-composed cels 5,6,7,8")
print("distinct colors: %d"%len(set(flat)))
print("saved /tmp/furby_eye_LIVE.ppm")
# quick ascii preview (downsample 128->32)
chars=" .:-=+*#%@"
for y in range(0,128,6):
    line=""
    for x in range(0,128,3):
        r,g,b=out[y][x]; lum=(r+g+b)//3
        line+=chars[min(len(chars)-1,lum*len(chars)//256)]
    print(line)
