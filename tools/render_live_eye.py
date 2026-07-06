import sys,struct; sys.path.insert(0,"emu")
import furby_display as FD
nand=open("Furby-Files/furby-nand (Fixed OOB Data).bin","rb").read()
CEL=nand[0xa2f600:0xa2f600+0x19d9400]
PAL=nand[0x2415200:0x2415200+0x1580]
SPR=nand[0x241c200:0x241c200+0x6ab58]
playlists=FD.parse_spr(SPR); colors=FD.load_palettes(PAL)
frame0=playlists[8][0]
idx=FD.render_frame_indices(CEL, frame0)
print("total palette colors available:", len(colors))
# try the verified BASE preset bank 64, and a few nearby, pick the vivid+smooth one
for boff in (64, 4338%len(colors) if colors else 64):
    pass
# use the preset: bank 64
bank=FD.palette_bank(colors, 768)  # bank 12 = the blue generic eye
FD.write_png("docs/images/furby_eye_LIVE.png", idx, bank, scale=4)
nz=len([c for c in bank if c!=(0,0,0)])
print("bank 64: %d non-black colors; sample %s"%(nz,bank[:6]))
print("wrote /tmp/furby_eye_TRUTH2.png")
