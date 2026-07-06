#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Furby Connect emulator — desktop GUI.

A plain, utilitarian emulator front-end (in the spirit of an NES emulator): open a ROM,
it boots and runs the real firmware, shows the live eye, streams a log, and exposes a
debug console for running custom instructions. No skeuomorphic toy chrome — just the tools.

    python3 emu/furby_gui.py
    python3 run.py --gui

A "ROM" is either a packed FurbyROM (.fby) or a GameCode.bin (you'll be asked for the NAND).
"""
from __future__ import annotations
import os, sys, struct, threading, queue, tempfile, traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
import unsp_native as NAT
import unsp_disasm as UD
import furby_display as FD


# ---------------------------------------------------------------- FAT32 reader
class Fat:
    """Minimal FAT32 reader — pull a file's bytes by A:\\path from the NAND image."""
    def __init__(self, nand: bytes):
        self.d = nand
        self.bps = struct.unpack_from("<H", nand, 0x0b)[0]
        self.spc = nand[0x0d]
        rsvd = struct.unpack_from("<H", nand, 0x0e)[0]
        nfat = nand[0x10]
        fatsz = struct.unpack_from("<I", nand, 0x24)[0]
        self.root = struct.unpack_from("<I", nand, 0x2c)[0]
        self.fat0 = rsvd * self.bps
        self.data = (rsvd + nfat * fatsz) * self.bps

    def _next(self, cl):
        return struct.unpack_from("<I", self.d, self.fat0 + cl * 4)[0] & 0x0fffffff

    def _clbytes(self, cl):
        return self.data + (cl - 2) * self.spc * self.bps

    def _readdir(self, cl):
        out, lfn = [], ""
        while 2 <= cl < 0x0ffffff8:
            base = self._clbytes(cl)
            for o in range(0, self.spc * self.bps, 32):
                e = self.d[base + o: base + o + 32]
                if len(e) < 32 or e[0] == 0:
                    return out
                if e[0] == 0xe5:
                    continue
                if e[11] == 0x0f:                                   # LFN chunk
                    ch = e[1:11] + e[14:26] + e[28:32]
                    s = "".join(chr(ch[i] | ch[i + 1] << 8) for i in range(0, len(ch), 2))
                    lfn = s.split("\x00")[0].split("￿")[0] + lfn
                    continue
                nm = lfn
                if not nm:
                    b = e[0:8].decode("latin1").rstrip()
                    ext = e[8:11].decode("latin1").rstrip()
                    nm = b + ("." + ext if ext else "")
                lfn = ""
                stcl = (struct.unpack_from("<H", e, 20)[0] << 16) | struct.unpack_from("<H", e, 26)[0]
                sz = struct.unpack_from("<I", e, 28)[0]
                out.append((nm, stcl, sz))
            cl = self._next(cl)
        return out

    def read(self, path, limit=None):
        parts = [p for p in path.replace("\\", "/").split("/") if p and ":" not in p]
        cl, sz = self.root, 0
        for i, part in enumerate(parts):
            hit = next((e for e in self._readdir(cl) if e[0].upper() == part.upper()), None)
            if not hit:
                return None
            cl, sz = hit[1], hit[2]
        want = sz if limit is None else min(sz, limit)
        buf = bytearray()
        while 2 <= cl < 0x0ffffff8 and len(buf) < want:
            b = self._clbytes(cl)
            buf += self.d[b: b + self.spc * self.bps]
            cl = self._next(cl)
        return bytes(buf[:sz])


# ---------------------------------------------------------------- PPM helper
def idx_to_ppm(idx, bank, scale, path):
    """Write a palette-indexed frame to a binary PPM (P6) — Tk reads it natively."""
    h, w = len(idx), len(idx[0])
    hdr = ("P6\n%d %d\n255\n" % (w * scale, h * scale)).encode()
    body = bytearray()
    for row in idx:
        line = bytearray()
        for i in row:
            line += bytes(bank[i]) * scale
        for _ in range(scale):
            body += line
    with open(path, "wb") as f:
        f.write(hdr); f.write(body)


# ---------------------------------------------------------------- worker
class Worker(threading.Thread):
    """Owns the emulator on a background thread; commands in, events out (queues)."""
    def __init__(self, cmd_q, out_q):
        super().__init__(daemon=True)
        self.cmd_q, self.out_q = cmd_q, out_q
        self.cpu = None
        self.gc = self.nand = None
        self.words = None
        self.fat = None
        self.cel = self.spr = None
        self.tmp = tempfile.mkdtemp(prefix="furbygui_")

    def log(self, s):   self.out_q.put(("log", s))
    def state(self, s): self.out_q.put(("state", s))

    def run(self):
        while True:
            cmd, *args = self.cmd_q.get()
            try:
                getattr(self, "do_" + cmd)(*args)
            except Exception as e:
                self.log("! error: %s" % e)
                self.log(traceback.format_exc().strip().splitlines()[-1])

    # ---- lifecycle
    def do_load(self, gc_path, nand_path):
        self.log("loading firmware: %s" % os.path.basename(gc_path))
        self.gc = open(gc_path, "rb").read()
        self.log("loading NAND: %s (%d MiB)" % (os.path.basename(nand_path), len(open(nand_path,'rb').read()) // (1 << 20)))
        self.nand = open(nand_path, "rb").read()
        self.words = list(struct.unpack("<%dH" % (len(self.gc) // 2), self.gc[:len(self.gc) // 2 * 2]))
        self.cpu = NAT.default_furby_cpu(self.gc, nand_bytes=self.nand)   # loader HLE baked in
        self.cpu.add_hle(0x08fc17, 6)                                     # compositor count-cap
        try:
            self.fat = Fat(self.nand)
            self.cel = self.fat.read(r"A:\Personalities\BASE\BASE.CEL", limit=0x60000)
            self.spr = self.fat.read(r"A:\Personalities\BASE\BASE.SPR")
            self.log("resolved BASE.CEL (%d B slice) + BASE.SPR (%d B)" % (len(self.cel or b""), len(self.spr or b"")))
        except Exception as e:
            self.log("! FAT resolve failed (%s) — eye render disabled" % e)
        self.log("ROM loaded. run 'boot' (or File > Boot).")
        self.out_q.put(("loaded", None))

    def do_boot(self):
        if not self.cpu:
            self.log("! no ROM loaded"); return
        self.log("booting firmware (600M instructions)…")
        total = 600_000_000
        step = total // 20
        for i in range(20):
            self.cpu.run(step)
            self.out_q.put(("progress", (i + 1) * 5))
        self.log("boot settled at LPC 0x%06x" % self.cpu.lpc())
        self._post_state()
        self.out_q.put(("booted", None))

    def do_wake(self):
        if not self.cpu:
            self.log("! no ROM loaded"); return
        self.log("driving wake sequence…")
        self.cpu.set_autoclear(0x7961, 0x30); self.cpu.set_reador(0x7961, 0x80)
        self.cpu.set_autoclear(0x7072, 0xffff)
        for _ in range(10):
            self.cpu.raise_irq(5); self.cpu.run(400_000)
        self.cpu.poke(0x534f, 1)
        for _ in range(30):
            self.cpu.raise_irq(5); self.cpu.run(1_500_000)
        tiles = [self.cpu.spriteram_get(i) for i in (0, 4, 8, 12)]
        npal = sum(1 for i in range(256) if self.cpu.mmio_last(0x7300 + i))
        self.log("firmware composed the eye: PPU tiles %s, %d-color palette" % (tiles, npal))
        self._post_state()
        self.do_render()

    def do_render(self):
        if not (self.cpu and self.cel and self.spr):
            self.log("! cannot render (missing CEL/SPR)"); return
        def rgb(v): return ((v >> 10 & 31) * 255 // 31, (v >> 5 & 31) * 255 // 31, (v & 31) * 255 // 31)
        bank = [rgb(self.cpu.mmio_last(0x7300 + i)) for i in range(64)]  # firmware's live palette
        frames = FD.parse_spr(self.spr)[8]
        paths = []
        for n, fr in enumerate(frames):
            idx = FD.render_frame_indices(self.cel, fr)
            p = os.path.join(self.tmp, "f%02d.ppm" % n)
            idx_to_ppm(idx, bank, 3, p)
            paths.append(p)
        self.log("rendered %d-frame eye animation" % len(paths))
        self.out_q.put(("frames", paths))

    def do_reset(self):
        if not self.gc:
            self.log("! nothing to reset (no ROM loaded)"); return
        self.log("resetting…")
        self.cpu = NAT.default_furby_cpu(self.gc, nand_bytes=self.nand)
        self.cpu.add_hle(0x08fc17, 6)
        self.log("reset done (re-run 'boot').")
        self._post_state()

    # ---- console
    def do_cmd(self, text):
        t = text.strip()
        if not t:
            return
        self.log("> " + t)
        parts = t.split()
        op = parts[0].lower()
        def num(s): return int(s, 0)
        try:
            if op in ("help", "?"):
                self.log("commands: boot | wake | render | reset | run N | frame [N] | "
                         "peek A [N] | poke A V | reg | pc | dis A [N]")
            elif op == "boot":   self.do_boot()
            elif op == "wake":   self.do_wake()
            elif op == "render": self.do_render()
            elif op == "reset":  self.do_reset()
            elif op == "run":
                n = num(parts[1]) if len(parts) > 1 else 1_000_000
                self.cpu.run(n); self.log("ran %d insns -> LPC 0x%06x" % (n, self.cpu.lpc())); self._post_state()
            elif op == "frame":
                n = num(parts[1]) if len(parts) > 1 else 1
                for _ in range(n):
                    self.cpu.raise_irq(5); self.cpu.run(1_000_000)
                self.log("delivered %d frame IRQ(s) -> LPC 0x%06x" % (n, self.cpu.lpc())); self._post_state()
            elif op == "peek":
                a = num(parts[1]); n = num(parts[2]) if len(parts) > 2 else 1
                vals = struct.unpack("<%dH" % n, bytes(self.cpu.read_block(a, n * 2)))
                self.log("[%06x] " % a + " ".join("%04x" % v for v in vals))
            elif op == "poke":
                a, v = num(parts[1]), num(parts[2]); self.cpu.poke(a, v)
                self.log("poke [%06x] = %04x" % (a, v))
            elif op == "reg":   self._post_state(); self.log(self._regs())
            elif op == "pc":    self.log("LPC = 0x%06x" % self.cpu.lpc())
            elif op == "dis":
                a = num(parts[1]); n = num(parts[2]) if len(parts) > 2 else 8
                for _ in range(n):
                    txt = UD.format_insn(UD.decode_at(self.words, a - 0x50000)).split(";")[0].strip()
                    self.log("  %06x: %s" % (a, txt))
                    a += getattr(UD.decode_at(self.words, a - 0x50000), "length", 1)
            else:
                self.log("? unknown command '%s' (try 'help')" % op)
        except Exception as e:
            self.log("! %s" % e)

    def _regs(self):
        r = [self.cpu.getreg(i) for i in range(8)]
        nm = ["sp", "r1", "r2", "r3", "r4", "bp", "sr", "pc"]
        return " ".join("%s=%04x" % (nm[i], r[i]) for i in range(8))

    def _post_state(self):
        self.state("LPC 0x%06x   %s" % (self.cpu.lpc(), self._regs()))


# ---------------------------------------------------------------- GUI
class App:
    def __init__(self, root):
        self.root = root
        root.title("Furby Connect Emulator")
        root.geometry("900x620")
        self.cmd_q, self.out_q = queue.Queue(), queue.Queue()
        self.worker = Worker(self.cmd_q, self.out_q); self.worker.start()
        self.frames = []      # PhotoImage list
        self.frame_i = 0
        self._build()
        self.root.after(40, self._pump)

    def _build(self):
        # menu
        m = tk.Menu(self.root)
        fm = tk.Menu(m, tearoff=0)
        fm.add_command(label="Open ROM…", command=self.open_rom)
        fm.add_command(label="Open GameCode + NAND…", command=self.open_pair)
        fm.add_separator()
        fm.add_command(label="Quit", command=self.root.quit)
        m.add_cascade(label="File", menu=fm)
        em = tk.Menu(m, tearoff=0)
        em.add_command(label="Boot", command=lambda: self.cmd_q.put(("boot",)))
        em.add_command(label="Wake + Render Eye", command=lambda: self.cmd_q.put(("wake",)))
        em.add_command(label="Reset", command=lambda: self.cmd_q.put(("reset",)))
        m.add_cascade(label="Emulation", menu=em)
        self.root.config(menu=m)

        # toolbar
        tb = ttk.Frame(self.root, padding=4)
        tb.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(tb, text="Open ROM", command=self.open_rom).pack(side=tk.LEFT, padx=2)
        ttk.Button(tb, text="Boot", command=lambda: self.cmd_q.put(("boot",))).pack(side=tk.LEFT, padx=2)
        ttk.Button(tb, text="Wake + Render", command=lambda: self.cmd_q.put(("wake",))).pack(side=tk.LEFT, padx=2)
        ttk.Button(tb, text="Reset", command=lambda: self.cmd_q.put(("reset",))).pack(side=tk.LEFT, padx=2)
        self.prog = ttk.Progressbar(tb, length=140, maximum=100)
        self.prog.pack(side=tk.RIGHT, padx=4)

        # main split: display | log
        pan = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        pan.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(pan)
        self.canvas = tk.Canvas(left, width=384, height=384, bg="#101010", highlightthickness=0)
        self.canvas.pack(padx=8, pady=8)
        self.img_id = self.canvas.create_image(192, 192, anchor=tk.CENTER)
        self.canvas.create_text(192, 192, text="no ROM loaded", fill="#666", tags="hint")
        pan.add(left, weight=0)

        right = ttk.Frame(pan)
        ttk.Label(right, text="Log").pack(anchor=tk.W, padx=6)
        self.log = tk.Text(right, bg="#0c0c0c", fg="#c8c8c8", insertbackground="#c8c8c8",
                           font=("Menlo", 11), wrap=tk.NONE, height=20, borderwidth=0)
        sb = ttk.Scrollbar(right, command=self.log.yview); self.log.config(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y); self.log.pack(fill=tk.BOTH, expand=True, padx=6)
        pan.add(right, weight=1)

        # status + console
        self.status = tk.StringVar(value="ready — open a ROM")
        ttk.Label(self.root, textvariable=self.status, relief=tk.SUNKEN, anchor=tk.W,
                  font=("Menlo", 10)).pack(side=tk.BOTTOM, fill=tk.X)
        con = ttk.Frame(self.root, padding=4); con.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Label(con, text="›").pack(side=tk.LEFT)
        self.entry = ttk.Entry(con, font=("Menlo", 11)); self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self.entry.bind("<Return>", self.run_cmd)
        ttk.Button(con, text="Run", command=self.run_cmd).pack(side=tk.LEFT)
        self._log("Furby Connect emulator. Open a ROM, then Boot, then Wake + Render.")
        self._log("Debug console below — type 'help'.")

    # ---- actions
    def open_rom(self):
        p = filedialog.askopenfilename(title="Open ROM",
                filetypes=[("Furby ROM / GameCode", "*.fby *.bin *.BIN"), ("All files", "*")])
        if not p:
            return
        if p.lower().endswith(".fby"):
            try:
                import rom_pack
                gc, nand = rom_pack.load_for_emulator(p)
                gcf = os.path.join(self.worker.tmp, "gc.bin"); ndf = os.path.join(self.worker.tmp, "nand.bin")
                open(gcf, "wb").write(gc); open(ndf, "wb").write(nand)
                self.cmd_q.put(("load", gcf, ndf))
            except Exception as e:
                messagebox.showerror("ROM error", str(e))
        else:  # a GameCode.bin — ask for the NAND
            nand = filedialog.askopenfilename(title="Select the NAND image for this GameCode",
                        filetypes=[("NAND image", "*.bin *.BIN"), ("All files", "*")])
            if nand:
                self.cmd_q.put(("load", p, nand))

    def open_pair(self):
        gc = filedialog.askopenfilename(title="Select GameCode.bin")
        if not gc:
            return
        nand = filedialog.askopenfilename(title="Select the NAND image")
        if nand:
            self.cmd_q.put(("load", gc, nand))

    def run_cmd(self, *_):
        t = self.entry.get(); self.entry.delete(0, tk.END)
        if t.strip():
            self.cmd_q.put(("cmd", t))

    # ---- queue pump
    def _pump(self):
        try:
            while True:
                kind, payload = self.out_q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "state":
                    self.status.set(payload)
                elif kind == "progress":
                    self.prog["value"] = payload
                elif kind == "loaded":
                    self.canvas.delete("hint")
                elif kind == "booted":
                    self.prog["value"] = 0
                elif kind == "frames":
                    self._load_frames(payload)
        except queue.Empty:
            pass
        self.root.after(40, self._pump)

    def _log(self, s):
        self.log.insert(tk.END, s + "\n"); self.log.see(tk.END)

    def _load_frames(self, paths):
        self.frames = [tk.PhotoImage(file=p) for p in paths]
        self.frame_i = 0
        if self.frames:
            self._animate()

    def _animate(self):
        if not self.frames:
            return
        self.canvas.itemconfig(self.img_id, image=self.frames[self.frame_i])
        self.frame_i = (self.frame_i + 1) % len(self.frames)
        self.root.after(120, self._animate)


def main():
    root = tk.Tk()
    try:
        root.tk.call("ttk::style", "theme", "use", "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
