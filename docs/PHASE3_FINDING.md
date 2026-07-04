# Phase 3 pre-work — the boot/memory-map blocker (diagnosed)

I went to add interrupts/timers next, but instrumenting the native core first
showed that's **not** what's blocking us. Here's the real story, proven.

## Symptom
Running real `GameCode.bin`, the CPU spends ~25 million instructions inside the
`__sn_init_table` startup **RAM-init copy loop** (hot LPC = `0x6d`), then jumps
into a data region (`0x0600xx`), executes garbage, and finally flings itself to an
unmapped segment (`CS=0x1d/0x36`). It never cleanly runs `main`.

## Root cause (confirmed)
The init routine reads its copy-descriptor table via `ds:[r1++]` with
**`DS=0x05, r1=0x00a6` → address `0x0500a6`**. The "entries" it reads are garbage:

```
count=0x6b7a src=0x7278 dst=0x6f5b ...   (random; outer count = 28417 entries)
```

Because **`0x0500a6` is main's own code** (main starts at `0x050082`). The startup
is interpreting *instructions as a copy table*, so it copies megabytes of noise
and corrupts itself.

## Why: linked addresses ≠ file offsets
Our emulator currently assumes a **flat identity map** (file word *i* = machine
address *i*). But `GameCode.lod` says the code was linked elsewhere:

```
SEC=ROM,60000,6FFFF     ; code ROM window at 0x60000-0x6FFFF  (CS=0x06)
BANK=20,FFFF
LOCATE=IRQVec,FFF5
```

So machine addresses (what `DS:r1`, `CALL A22`, `GOTO A22` use) are **banked**, and
map to NAND/file offsets through a translation we haven't built. Under identity
mapping, `0x0500a6` lands on the wrong bytes → garbage table.

This is **Risk R1/R3** from the plan (boot HLE + memory map), landing right on
schedule as Phase 3.

## What Phase 3 must do
1. **Reconstruct the address map.** Use `GameCode.lod` (`SEC`/`BANK`/`LOCATE`) plus
   the boot-ROM behaviour to map machine word-addresses → file/NAND offsets. Very
   likely: low SRAM `0x0000-0x6FFF` (boot code copied in), then banked ROM windows
   for CS=0x05/0x06/... that don't equal the raw file offset.
2. **HLE the boot loader** so the `__sn_init_table` pointer (and DS) resolve to the
   *real* init table in ROM, not into code. Then the RAM-init copy becomes small
   and correct, and `main` gets called with SRAM properly populated.
3. Re-validate: the init copy should process a handful of sane entries (small
   counts, ordered addresses), then `call 0x050082` (main) with a clean RAM image.

## Tooling added this pass
- `cpu_set_cs_trap()` / `trapped` / `trap_from` / `trap_to` in the native core +
  `unsp_native.py` — stop-and-report when execution leaves the sane code segments.
  This is how we caught the runaway; it'll verify the Phase 3 fix too.

## Bottom line
Interrupts/timers were the wrong next thing — they'd sit on a broken memory model.
The CPU core is correct; what it needs now is a **truthful address space**. That's
Phase 3, and the diagnosis above makes it concrete.

---

# UPDATE — CS0 memory map implemented (big win)

From MAME's `gpl16250_nand` bootstrap + `gpl162xx_soc` map:
```
0x000000-0x006FFF  internal RAM (SRAM)
0x007000-0x007FFF  peripherals (MMIO)
0x008000-0x02FFFF  internal ROM (bootstrap; not in our dump)
0x030000+          external CS0 = the GameCode image   (then CS1, CS2 ...)
0x200000-0x3FFFFF  banked external via P_BankSwitch_Ctrl (0x7810)
```
Bootstrap: header byte `0x15/0x16` → `dest` (ours = 0), copy first block to RAM,
start at `dest+0x20`. So **the ROM must load at machine `0x030000`, not flat at 0**,
and the boot block is copied down to RAM 0x0000.

Implemented in `unsp_core.c` (`cpu_load_at`, `cpu_bootcopy`) + `unsp_native.py`
(`default_furby_cpu` now maps CS0 at 0x030000 and HLE-copies the boot block).

**Verified fix:**
- init table at machine `0x0500a4` → file `0x0200a4`: outer_count now **1** (was
  28417 garbage). `main` at `0x050082` → file `0x020082` = **real coherent code**.
- The `__sn_init_table` copy that spun **25,000,000** instructions now completes in
  **~495,000** — and execution runs cleanly through main-init routines (real
  loops, calls, `retf`s) instead of copying noise.

**Next boundary (was):** `call 0x08465a` (CS=0x08) landed on non-code.

---

# RESOLVED — the base was 0x050000, not 0x030000

Re-read the boot header correctly: `dest = byte[0x15]<<8 | byte[0x16]<<16`, and our
bytes are `[0x15]=0x00, [0x16]=0x05` → **dest = 0x050000**. The boot ROM loads the
whole GameCode image to machine **0x050000** and starts at **0x050020**. So
`machine 0x050000 + i == file word i`. (`−0x030000` only *looked* right for main
because both offsets happen to hit valid code there.)

Proof: machine `0x08465a` → file `0x03465a` is a clean function —
`push bp; sp -= 9; call 0x0845ce; …init 0x7874/0x787c/0x7888`. `0x0904e0` →
`0x0404e0` clean too. `−0x030000` gave `0x05465a` = audio data.

Fix (`unsp_native.py`): load image at `0x050000`, reset at PC=0x20 / CS=0x05.

**Result:**
- **Blows straight past `0x08465a`** into the real second-init routine.
- Runs **200,000,000 instructions with no trap/derail** (was: garbage at 25M).
- Touches **42 distinct peripheral registers** (GPIO writes, etc.) — real init.
- Now sits in a **peripheral poll loop on `0x7850`** (read 485k×) and a RAM-init
  copy — i.e. genuine firmware waiting on hardware we haven't modeled.

**Memory map is now correct.** The remaining work is **Phase 4**: model the
peripherals the firmware polls (starting with `0x7850`), then interrupts/timers so
the main loop ticks. The CPU + address space are done and truthful.
