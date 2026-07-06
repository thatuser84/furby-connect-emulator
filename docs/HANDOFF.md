# Furby Connect Emulator — Handoff Brief / Expert Prompt

You are picking up a **from-scratch emulator of the Furby Connect (2016) toy**,
built to run its real firmware. The hardware emulation is **complete and working**:
the firmware boots, services interrupts, keeps time, and streams its personality
data out of a 114 MB NAND dump. **One goal remains: get the eyes to display.** The
display-enable code path is fully identified but never reached — the firmware sits
in an upstream fixed-point computation loop and never advances to it. Your job is a
**firmware reverse-engineering campaign** to find and clear that final gate.

Everything lives in `~/Downloads/nyx/Furby/`. Read this whole brief first.

---

## 1. The hardware (verified)

- **SoC:** GeneralPlus **GPL16258** (gpac800 variant) — µ'nSP 2.0 CPU (ISA 1.3),
  16-bit **word-addressed**, 22-bit space. Confirmed from the toy's own
  `GameCode.lod` build config.
- **Memory map** (machine word addresses):
  - `0x000000-0x006FFF` internal SRAM (RAM)
  - `0x007000-0x007FFF` peripherals (MMIO)
  - `0x050000+` the GameCode image (boot dest; **`machine 0x050000 == file word 0`**,
    verified — reset entry is `0x050020`)
  - `0x200000-0x3FFFFF` banked external window (NAND) via `P_BankSwitch_Ctrl 0x7810`
- **Peripheral register map:** `emu/gpl16250_regs.py` (names confirmed vs MAME).
  Key: `0x707f` PPU-enable, `0x7300` palette, `0x7400` sprite RAM, `0x7000` PPU
  ctrl, `0x7850-0x7857` NAND controller, `0x7a80-0x7a9f` system DMA, `0x78b0`
  TimeBase, `0x78a0` interrupt controller, `0x7960` ADC.
- **Reference (authoritative):** MAME `src/devices/cpu/unsp/*` and
  `src/devices/machine/generalplus_gpl162xx_soc.cpp` / `..._gpl1625x_soc.cpp` /
  `..._gpl_dma.cpp`. Local copies in `Furby/ref/mame-unsp/`.

---

## 2. What's built (all in `Furby/emu/`)

- **`unsp_core.c`** — native µ'nSP CPU + all peripherals (built via `build.sh` →
  `libunspcore.so`). Implements: full ALU/flags, branches, call/goto/ret, CS/DS
  segments, push/pop, **EXTOP (ISA 2.0, incl. ext regs R8-R15)**, **16-bit shifts
  incl. asror/lslor/lsror**, **EXP/DIVQ**, **secbank**, **signed MUL**, interrupts +
  timer, **NAND controller**, **system DMA**, **banked window**, plus debug hooks
  (`cs_trap`, telemetry).
- **`unsp_native.py`** — ctypes binding. `default_furby_cpu(img, nand_bytes=...)`
  sets up the correct memory map, boot, and the peripheral quirks. Same API as the
  Python core so it's swappable.
- **`unsp_cpu.py`** — pure-Python reference core (validated bit-for-bit vs the C
  core for 250k steps). Use it for lock-step debugging.
- **`unsp_disasm.py`** — faithful ISA-1.3 disassembler (ported from MAME).
- **`unsp_trace.py`** — recursive-descent tracer + call-target harvesting.
- **`gpl16250_regs.py`** — peripheral register name map.
- Docs: `HARDWARE.md`, `EMULATOR_PLAN.md`, `emu/PHASE1_NOTES.md`,
  `PHASE2_NOTES.md`, `PHASE3_FINDING.md`, `PHASE4_NOTES.md` (read PHASE3/4 — they
  contain the full journey and every fix).

### How to run
```sh
cd ~/Downloads/nyx/Furby && sh emu/build.sh
python3 - <<'PY'
import sys; sys.path.insert(0,"emu")
import unsp_native as NAT
img  = open("Furby-Files/Furby-NAND/GameCode.bin","rb").read()
nand = open("Furby-Files/furby-nand (Fixed OOB Data).bin","rb").read()   # git-lfs pulled
cpu = NAT.default_furby_cpu(img, nand_bytes=nand)
cpu.run(600_000_000)                     # boot
cpu.set_timer(1, 20000); cpu.set_timer_status(0x78a0,0x80); cpu.set_readclear(0x78a0,0x80)
cpu.run(300_000_000)                     # main loop
print("lpc=%06x nand=%d dma writes to 0x707f=%d" %
      (cpu.lpc(), cpu.nand_reads, cpu.mmio_writes(0x707f)))
PY
```
Native core runs ~30-36 M instructions/sec. The Furby image is `GameCode.bin`; the
full flash is `furby-nand (Fixed OOB Data).bin` (114 MB, OOB-stripped, 512-byte
pages; GameCode header `PGpssiipps` is at NAND byte `0x1e5200` = word `0xf2900`).

---

## 3. Current state (exactly where it's stuck)

The firmware **boots → wakes (timer IRQ) → keeps time → loads personality/resource
data** (≈22 M NAND reads, ≈10.5 k DMA transfers). It then runs its scheduler,
hot in a fixed-point math loop around **`0x091ed3`** (an `EXP`/normalize helper),
polling the ADC `0x7964`. It **never** writes `0x707f` (PPU never enabled), so the
eyes never turn on.

### The display-enable path is FULLY IDENTIFIED
- **`0x0574f6` = `set_display_enable(arg)`**: `arg==1` → read `0x707f`, `|= 0x02`,
  write back (**bit1 = eyes on**); `arg==0` → disable.
- **Only caller: `0x07eb56`**, inside **`display_init`** (~`0x07eaxx-0x07ebxx`),
  which builds a display-config parameter block (`0x02,0x08,0x08,0x0080,...`), calls
  `0x058bb0`, then `set_display_enable(1)`, then `0x05744f`.
- **`display_init` is never executed** — the call to it is never reached.

**So the entire problem reduces to: why does the scheduler never call
`display_init`?** Either (a) it's stuck in the upstream computation loop (which may
still be fed subtly-wrong data), or (b) a state-machine condition gating that call
is never satisfied.

---

## 4. Fixes already made (do not redo)

Root-caused and fixed, each verified: the memory map (`0x050000` base, not flat);
**EXTOP** (the ISR saves R8-R15 via it — this fixed a register-corruption crash);
the **16-bit shift** instructions (`&0x1f` mask, arithmetic `asr`, and the
multi-word `asror/lslor/lsror` that write R3/R4); **EXP** and **DIVQ**; **secbank**
(bank-active flag); **signed MUL**; the **NAND geometry** (dropped the OOB `<<shift`
since the image is OOB-stripped → this jumped NAND reads 4.3M→22M, DMA 2k→10.5k).

Known non-fix: mapping the non-banked window `0x020000-0x1fffff` to NAND **breaks
boot** — that range is SDRAM the firmware writes to, not cs-space.

---

## 5. Your campaign (concrete next steps)

1. **Trace `display_init`'s call graph upward.** Find who calls the function
   containing `0x07eb56`, then who calls *that*, up to a top-level scheduler state.
   The disassembler + a caller-search (grep the image words for `call` opcodes:
   `0xf040|seg` then `imm16`) are your tools. Find the **conditional branch** that
   decides whether the wake/display task runs, and read what it tests.
2. **Determine if the `0x091ed3` computation loop is finite or infinite.** Snapshot
   registers/RAM across the loop; if an index/counter isn't advancing toward a
   limit, there's still a data or instruction bug. If it *is* advancing, it's a
   long computation gated on data.
3. **Verify the loaded data is correct.** The DMA loads NAND→RAM (e.g.
   `source=0x7854, dest=0x1840, length=0x840`). Confirm the NAND effective-address
   math (`emu/unsp_core.c: nand_recalc`) lands on the right bytes — cross-check a
   known structure (e.g. a personality file header) against `Furby-NAND/` extracted
   files. Wrong data here → wrong far-pointers → stuck loop.
4. **Read the personality format.** `Furby-Files/Furby-NAND/Personalities/*`
   (Base, DJ, etc.) and `furby.py` in the repo root document the DLC/personality
   section format (PAL/CEL/SPR/XLS/AMF/SEQ/MTR/LPS). The scheduler is likely walking
   these structures; understanding them reveals what state it wants.

**Success = the firmware writes a nonzero value to `0x707f`**, then starts writing
the PPU palette (`0x7300`) and sprite RAM (`0x7400`) — that's the eyes rendering.
From there, a tkinter front-end reading the PPU framebuffer shows the animated eyes.

---

## 6. Debug tooling available in the core
`cpu.set_cs_trap(limit)` (stop when code segment > limit; `cpu.trapped`,
`cpu.trap_from/to`), `cpu.peek/poke`, `cpu.mmio_reads/writes/last(addr)`,
`cpu.getreg/setreg`, `cpu.set_reador/readclear/ready(addr,val)` (model status
bits), `cpu.raise_irq`, telemetry (`nand_reads`, `dma_runs`, `cs_reads`,
`irq_taken`). Single-step via `cpu.step()`; the pure-Python core (`unsp_cpu.py`)
gives full introspection for lock-step verification.

The machine is correct and alive. The eyes are one well-scoped RE campaign away.

---

## 7. SESSION FINDINGS — the blocker is the NAND filesystem (FTL), root-caused

Two sub-agents + follow-up traced it to the bottom. The chain:

- The eyes-enable (`set_display_enable(1)` at `0x0574f6` ← `display_init 0x07ea98`)
  is a case of a **jump-table state machine in `0x08465a`** (scheduler head
  `0x084770`, dispatch on `RAM[0x4370]`, table at `0x09c3e0`).
- **The scheduler head runs 0 times.** Boot-init calls `0x07b388` (a **GameCode.bin
  checksum/self-verify**) that never returns: a `while(counter < limit)` where
  `limit = 0xFFFFFFFF` — the **"file not found" sentinel** from the FAT lookup of
  `A:\GameCode.bin` / `A:\Checksum.dat` (filename strings at `0x09af1f`/`0x09af2f`).
- The FAT lookup (`0x078730` → `0x08ec8c` → `0x082505` dir-search) **fails**: the
  directory it walks does not contain the filenames.

**Filesystem ground truth (verified):** the NAND is **FAT32** (512B sectors, 1
sector/cluster, 60 reserved, 2 FATs × 1730 sectors). Root dir @ byte `0x1b8000`.
`GAMECODEBIN` = **cluster 363 → byte `0x1e5200`**, which IS the real GameCode
(`PGpssiipps`). **So the FAT geometry is correct and my `nand_recalc` (eff =
sector×512) maps sectors → bytes correctly.**

**The actual root cause:** the firmware never gets a working filesystem because it
does a **flash-translation-layer (FTL) block scan** — its NAND reads step through
**sectors 0,1 → 0x40,0x41 → 0x80,0x81 …** (2 sectors at every 64-sector / 32KB
**block** boundary). That's the firmware reading per-block metadata to build a
**logical→physical sector map**. Our dump is **`furby-nand (Fixed OOB Data).bin`,
OOB-STRIPPED** — so the spare-area/FTL metadata the scan needs is gone. No FTL map
→ the BPB/directory reads resolve to the wrong physical bytes → the directory the
search walks has no filenames → lookup returns `0xffff` → infinite checksum loop.
(Confirmed: the BPB signature `58eb 4d90` never appears in RAM; the dir with
`GAMECODEBIN` is never loaded into searchable memory.)

### The two concrete fix paths (pick one)
1. **HLE the filesystem lookup.** Intercept `0x078730` (find-file-by-name) — and/or
   the sector-read `0x0844cb` / `0x082505` — to return correct results directly
   from the FAT we parsed (map name → cluster → byte, `eff = 0x1b8000 + (cluster-2)
   ×512`). This sidesteps the missing FTL entirely. Most tractable.
2. **Provide/reconstruct the FTL mapping.** Determine the firmware's logical→physical
   block map (what the block scan expects in sectors 0/1 of each 32KB block) and
   either synthesize it in the NAND image or model it in `nand_recalc`. Needs the
   OOB layout, which the stripped image lacks — harder.

Debug tooling added this session (in `unsp_core.c` / `unsp_native.py`): PC
watchpoints (`add_watch`/`watch_hits`), NAND access log (`nlog_*` — cmd/addr/type/
eff), DMA log (`dlog_*` — mode/src/dst/len/nand_eff). Use these to verify any fix:
success = `A:\GameCode.bin` resolves → checksum returns finite → `0x084770` runs →
`0x707f` gets a nonzero write → PPU palette/sprite writes = eyes rendering.

---

## 8. Roadmap: HLE now, real FTL later (for 100% fidelity)

The §7 HLE is the **fast path to visible eyes** — it feeds the firmware correct file
bytes directly, bypassing its own filesystem code. Output-accurate, but not the
real execution path.

**The 100%-accurate path is the real FTL**: let the firmware run its own
flash-translation-layer against real flash, resolving every file the way the
hardware does — no emulator shortcuts. That requires the **OOB spare-area metadata**
(per-block logical numbers, bad-block markers, ECC) that the current
`furby-nand (Fixed OOB Data).bin` dump has **stripped**. To do it properly:

1. **Get an OOB-preserving NAND dump** — the raw flash with the ~16-byte spare per
   512-byte page kept intact (or 64B/2KB page, per the real chip geometry), OR
2. **RE the firmware's FTL** (the block scan reading sectors 0/1 of each 32KB block)
   to learn the logical→physical block-map format, then **synthesize** that metadata
   into the image / model it in `nand_recalc` so the firmware's own scan builds the
   correct map.

Then the HLE hook can be removed and the firmware boots its filesystem for real —
which is what makes rendering (and every file access, DLC, personality swap) match
the physical toy exactly. Track this as the post-eyes fidelity milestone.

---

## 9. HLE infrastructure BUILT + corrected target (implement the handler next)

**Infrastructure is done and working** (`unsp_core.c` + `unsp_native.py`):
- `cpu.add_hle(pc, id)` / `clear_hle()` / `hle_calls(idx)` — register a PC-entry hook.
- The run loop dispatches: when `LPC == hle_pc`, it runs `hle_dispatch(c, id)` instead
  of the function body (reads args off the stack, does the work, returns via retf).
- `cpu.run_until(pc, max)` — run until an LPC is hit (handy for stepping to a fn).
- Example handler `id==1` (identity sector read) is in `hle_dispatch`.

**Target correction (verified by watchpoints):** the agent's `0x076e8d`
(and `0x052391`/`0x0523b1`) are **NEVER reached** (0 hits). The path actually used
during boot is:
- **`0x078730` find-file-by-name** — 4 hits. Arg: a **32-bit filename far-pointer**
  (caller does `r2=0xba81; r3=0x09; push` → filename at machine `0x09ba81`). Returns
  **`r2 == 0xffff` = not found**; on success `r2 != 0xffff` and **`r1` = a file
  descriptor** (caller stores it: `[bp+0xd]=r1`, then uses it). `0x078730` →
  `0x08ec8c` (FAT open) → `0x082505` (dir search) → `0x0844cb`.
- **`0x0844cb` storage-read** — 8 hits (the FTL-level primitive).

**Next step to land the eyes:** HLE `0x078730`. Read the filename string at the
far-pointer arg, look it up in the parsed FAT (name→cluster→size), and return a
**correct file descriptor** in `r1` with `r2 != 0xffff`. The one unknown is the
**descriptor struct format** — dump what a *successful* `0x078730` writes/returns
(e.g. force one lookup to succeed and inspect `r1`'s target), or trace `0x08ec8c`'s
success path. Once find-file returns a valid descriptor, the checksum count goes
finite and boot proceeds toward `0x707f`. (Alternatively HLE `0x0844cb` to return
identity-mapped sector data, but its interface is FTL-internal and messier.)

### find-file (`0x078730`) — full interface (RE'd, ready to implement)
- **Arg:** a 32-bit filename far-pointer on the stack at `[SP+3]`(lo):`[SP+4]`(hi)
  (caller builds it: `r2=name_lo; r3=name_hi; [r4++]=r2; [r4]=r3; call`). Filenames
  are **UTF-16** ("A:\GameCode.bin", "A:\Downloads", ...), located in the loaded
  image (e.g. machine `0x09ba81`).
- **Return:** a 32-bit value in **`r1`(lo):`r2`(hi)**; **`0xffffffff` = not found**
  (caller checks `r2 != 0xffff` AND `r1 != 0xffff`). This value is consumed by the
  checksum as its loop **count**, so it is the file **size** (or a handle carrying
  it). For GameCode.bin the FAT size is **916974 (0x0DFEAE)**.
- **Handler (id=2) to write in `hle_dispatch`:** read the far-ptr → read the UTF-16
  name from `c->mem[ptr]` → strip the "A:\" prefix → look it up in the FAT32 root
  directory (parse `nand` at byte `0x1b8000`, 32-byte entries, 8.3 names, LFN
  entries have attr byte `0x0f` at +11 — skip them; match 8.3 upper-case) → return
  size in `r1:r2` (and if a handle is needed, whatever the caller's `0x0786f4`
  reads). Then verify: checksum finite → `0x084770` runs → `0x707f` nonzero write.

**Note on constraints:** subagents hit a monthly spend limit mid-run; the FAT-lookup
handler above is the single remaining implementation step to reach the eyes.

---

## 10. STATUS: display path EXECUTES; graphics content still empty

The find-file HLE (`id==2`, name→FAT size) is implemented and **baked into
`default_furby_cpu`** (plus an SPI-status fix: `set_reador(0x7943, 0x07)` to pass a
busy-wait at `0x087755`). Result, verified:
- find-file fires 5× and resolves every lookup (GameCode.bin→916974, etc.).
- checksum `0x07b388` completes (was infinite).
- **`0x707f` PPU-enable written 12× (last 0x0069); sprite RAM `0x7400-74ff` written
  512×; `0x7000` PPU ctrl written** — all from a flat-zero baseline. The display
  path (`display_init`→`set_display_enable`) now runs where it never did.

**BUT no visible pixels yet:** palette `0x7300-73ff` writes = **0**, and sprite-RAM
values are all **0x0000**. The PPU is enabled and driven but with **empty graphics** —
because find-file only returns the file *size*; the file **content** reads still go
through the broken FTL storage-read (`0x0844cb`) and return zeros.

**Final step for real eyes:** make file *content* load correctly — HLE `0x0844cb`
(or the sector read it serves) to return **identity-mapped NAND data** (`byte =
logical_sector*512`) into the requested buffer, so graphics/palette data loads. Then
palette entries become nonzero and sprite RAM holds real eye bitmaps. Verify:
`sum(mmio_writes(0x7300+i))>0` and nonzero palette/sprite content.

### Update: the content read is a full FILE* API (not a single leaf)
Unlike find-file (a leaf returning one number), reading file *content* goes through
a real library stack, all on top of the broken FTL:
- `0x0786f4` (open+prep) → `0x090f7f` **open** (→ `0x090624` handle-getter,
  `0x087ff9`) and `0x091c93` **read** (→ `0x0901d2`, `0x091dfe`, `0x090628`).
- Handles flow between them; `0xffff`/`-1` = error, `r1=0` = ok.
The 512 sprite-RAM writes appear to be RAM *clearing* (init), not graphics; palette
load (0 writes) is gated behind file content that reads back zero.

**Two routes to real pixels:**
1. **HLE the file read API**: hook `0x090f7f` open to return a handle that carries
   the file's start cluster+size (from the FAT), and `0x091c93` read to copy
   identity-mapped bytes (`nand[0x1b8000+(cluster-2)*512 + offset]`, following the
   cluster chain in FAT) into the caller's buffer for the requested length. Bounded
   but multi-function; entry points above are mapped.
2. **Real FTL (§8)**: get an OOB-preserving NAND re-dump so the firmware's own FTL
   builds the correct logical→physical map and all file I/O works natively (removes
   every HLE). This is the true-fidelity path.

**Recurring blocker:** subagent runs keep terminating on the account monthly spend
limit mid-task; the inline path is also spend-gated. Work is checkpointed here so it
resumes cleanly.

---

## 11. BREAKTHROUGH: no OOB/hardware needed — all content is available

Two facts change the endgame:
1. **The entire NAND is already extracted as clean per-name files** in the repo /
   locally at `Furby-Files/Furby-NAND/`: `Graphics/LRGB.PAL` (256B palette),
   `LRGB.CEL` (12288B tiles), `LRGB.SPR` (1188B sprites), `Checksum.dat`, `color.dat`,
   `language.dat`, `FurbyData.dat`, `Personalities/{Base,Cat,DJ,...}/*.{PAL,CEL,SPR,AMF}`,
   `GameLogic/*`, `AudioMegafiles/*`, `Downloads/TOY.AMF`.
2. **The content is ALSO correctly placed in our linear NAND image** at the FAT
   cluster offsets (proved: GameCode = cluster 363 → byte 0x1e5200 = real bytes).

So the blocker was never missing data or OOB — it's purely that the firmware's
**stateful buffered file API** fails its own lookup through the FTL. The read stack
is deep: `0x090f7f` open → `0x091c93` read → `0x0901d2` → `0x091b7c` → … (a FILE*
library with an internal buffer + handle). Handle `0xffff` propagates because open
bails.

### Two clean finish paths (no hardware, no OOB dump)
- **A) Virtual-file-server HLE (fast):** register the extracted files by name in the
  core (Python loads `Furby-NAND/**`), then HLE **open** (`0x090f7f`: filename far-ptr
  `[SP+3:4]`, mode `[SP+5]` → return a synthetic handle in `r1`) and the **read**
  entry (`0x091c93`) to copy real file bytes into the caller's buffer and advance a
  C-side per-handle position; noop **close/seek**. Serve either the extracted files or
  `nand[0x1b8000+(cluster-2)*512 + pos]` (both correct). Risk: the buffered API's
  handle/seek semantics span ~5 fns — HLE at the app-call level (open/read/close) and
  keep handles opaque.
- **B) Synthesize FTL metadata (full fidelity):** RE what the block-scan reads from
  sectors 0/1 of each 32KB block (logical-block number / validity), write correct
  metadata into a rebuilt image so the firmware's own FTL builds an identity map —
  then the native file API works with NO HLE. This is the §8 100% path achieved
  WITHOUT an OOB dump (by reconstructing the metadata instead of re-dumping it).

Either lands real palette/sprite data → visible eyes. Content is no longer the
question; only the file-access mechanism remains.

---

## 12. FTL THEORY DISPROVEN — real blocker is the directory MATCHER

Deep verification this session **retires the FTL/OOB theory entirely**. There is no
flash-translation layer to feed and nothing to synthesize:
- Added `cpu_dbg_readsector` — the NAND controller reads **byte-perfect** at plain
  identity addresses: sector 0 (BPB `eb 58 90 4d…`), root dir (sector 0xdc0),
  GameCode (sector 0xf29 `PG…`) all MATCH raw NAND exactly.
- Unfiltered `nlog`: the firmware **requests correct identity sectors** — BPB=sec 0,
  root dir=sec 0xdc0 — with the normal cmd0/cmd0x30 large-page protocol.
- `dlog`: the root directory **DMAs correctly to RAM** (eff=0x1b8000 → dest 0x1840,
  len 0x840); BPB likewise. FAT data verified landing at 0x1840.
- The "block headers" at 32KB boundaries are just **ordinary FAT-table entries**
  (`01 02 00 00` = FAT32 links), not FTL metadata.

**So the data path is flawless.** Yet native boot (all HLEs cleared) still fails:
find-file `0x078730` runs 4×, returns not-found, checksum `0x07b388` loops, scheduler
`0x084770` never runs, `0x707f`=0. And **GAMECODEBIN sits at root-dir offset 0x140**
— inside the 0x840 the firmware reads — correctly in RAM. The native **matcher walks
past a filename that's right in front of it**, while the find-file HLE (same bytes)
matches it fine.

**Conclusion:** the true-fidelity blocker is the firmware's directory-name MATCH
logic failing on correct data — i.e. either a subtle **CPU-emulation bug** exercised
only by the 8.3/LFN string-compare path, or an **unmodeled dependency** the matcher
waits on. Not hardware, not a dump.

**Next step (bounded bug hunt):** single-step the native search
(`0x078730`→`0x08ec8c`→`0x082505`) on the "A:\GameCode.bin" lookup; watch it walk the
32-byte entries at the 0x1840 buffer, find where it reads offset 0x140 (GAMECODEBIN)
and the compare that *should* match but doesn't. Fix that (likely one opcode or one
peripheral), and the **native filesystem works with zero HLE** — the real §8 fidelity
goal, reached without any OOB dump.

---

## 13. CORRECTION to §12 — it IS an FTL/OOB scan (evidence is intricate)

§12's "FTL disproven" was premature. Deeper trace at the search point (`0x082505`,
native, all HLE cleared) shows: the directory-search buffer `0x1840` holds **garbage
that traces to `nand[0x1fb7fd0]`** (not the directory at `0x1b8000`), and the `dlog`
shows the firmware is running a **full-NAND block scan** — 2048+ DMAs, `src=0x7854`,
`eff` marching through blocks (…0x4b8200, 0x4c0000, 0x4c8000…) up to sector ~0xfdbf
(~1000 blocks deep), each dumped into the shared `0x1840` buffer. So at match time the
buffer has *scan* data, not the directory → matcher fails → find-file not-found.

**Reconciling the two:** the controller reads any single sector byte-perfect (§12,
`cpu_dbg_readsector` still true), AND the firmware requests correct identity sectors —
but its **mount-time FTL scan does not build a working filesystem**, consistent with
the per-block metadata (logical-block numbers) normally living in **stripped OOB**.
Result: wrong logical→physical map → directory resolves to `0x1fb7fd0`.

**Honest status:** the reproducible facts are firm (wrong-offset dir read; full-NAND
scan; controller fine in isolation), but the exact FTL mechanism — *how the firmware
expects to read block metadata from an OOB-stripped image* — is NOT yet nailed. This
subsystem's evidence has pointed both ways; treat §12's optimism with caution.

**To actually finish path B, the open question to answer first:** trace the scan loop
and find *what value* it reads from each block and *where* (which byte within the
block, or a separate OOB-read path via the `0x7856` type field) it expects the
logical-block id — then synthesize that metadata into the image (or model an OOB
region in the controller) so the scan builds an identity map. Until that read is
understood, neither synthesizing metadata nor "one opcode" is guaranteed. The
pragmatic route to *visible eyes* remains the file-API HLE (§11 path A).

---

## 14. DEFINITIVE ROOT CAUSE (MAME-confirmed): OOB stripped, 528-byte pages

Cross-checked against MAME's `generalplus_gpl1625x_soc.cpp` — the authoritative
gpac800 NAND model. `recalculate_calculate_effective_nand_address()` (line ~680):

```
type = 7856 & 0xf;  shift = (type==7)?4 : (type==11)?5 : 0;
page = type ? nandaddress : nandaddress>>8;
m_effectiveaddress = (page * 528 + page_offset) << shift;   // <-- 528, not 512
```

and `nand_7854_r` reads **linearly** `nand[eff + curblockaddr++]`. Register map:
`0x7855`=NF_INT_Ctrl, `0x7857-0x785f`=**ECC** subsystem. So the real geometry is
**528-byte pages = 512 data + 16 OOB**, and the OOB carries **ECC + bad-block markers
+ the FTL logical-block metadata** the mount-scan needs.

**Our image `furby-nand (Fixed OOB Data).bin` is OOB-STRIPPED**: exactly
223232 × 512 (= 114294784). 223232 × 528 would be the with-OOB size. So every page's
16 OOB bytes are gone. My controller's `page*512` reads single-page DATA correctly
(proved by `cpu_dbg_readsector`), but the firmware reads **528-byte pages
contiguously** and the FTL reads **per-block OOB** — neither of which a stripped image
can serve. Hence: mount-scan builds a broken map → directory resolves to the wrong
offset (`0x1fb7fd0`) → matcher fails → no eyes. This is certain now, not a theory.

Note MAME's own comment (~line 594): suspected unSP core-math bugs cause rendering
issues in these gpac800 titles — so faithful native rendering is a known-hard target
even for MAME.

### The real fix (path B, now fully specified)
**Reconstruct the OOB** → turn the stripped 512-byte-page image back into a 528-byte
page image (16 OOB bytes per page), then use MAME's `page*528` formula. The OOB must
contain, per page/block:
- **bad-block marker** = 0xFF (good) at the maker's byte position,
- **ECC** computed over each 512-byte page — follow MAME's ECC code (`0x7857-0x785f`,
  Hamming/BCH) or make the controller report ECC-clean so the firmware skips it,
- **FTL logical-block number** = identity (physical block N ↔ logical N), since our
  image is already a flat/linear FAT — this is what the mount-scan reads to build its
  map.

Then the firmware's own filesystem works natively (no HLE) — the true §8 fidelity
goal. The remaining unknown is only the **exact OOB byte layout** (which of the 16
bytes hold the block-id vs ECC vs marker) — derivable by tracing the mount-scan's OOB
reads (it reads curblockaddr 512-527 of each scanned page) and/or from the gpac800
datasheet. Everything upstream is now certain.

**Bottom line for DJ:** path B is real and fully diagnosed. Finishing it = rebuild the
image with reconstructed OOB (ECC + identity FTL ids). That's a bounded build job, not
a hardware dump. Path A (file-API HLE) still gets visible eyes faster if wanted.

---

## 15. OOB RECONSTRUCTION — infra built, ECC ruled out, one unknown left

Built and verified this session:
- **Controller page-size support** (`nand_page_size`, `cpu_set_nand_page_size` +
  Python `set_nand_page_size`): `nand_recalc` now does `page * page_size` so it can
  address a reconstructed **528-byte-page** image (MAME's geometry) instead of 512.
- **Image reconstruction** (Python, no numpy): re-interleave the stripped 512-byte
  pages with 16 OOB bytes each → 223232 × 528 = 117866496-byte image. Load via
  `load_nand` + `set_nand_page_size(528)`.
- **Verified the OOB now lands correctly**: with sentinel OOB (`OOB[k]=0xE0+k`), page
  P's 16 OOB bytes appear exactly at read-buffer + 256 words per 528-slot
  (0x1940, 0x1a48, 0x1b50, 0x1c58…). Alignment is right; behavior changed (native
  `lpc` moved 0x091ee5 → 0x0788ce).

**ECC ruled OUT as the blocker:** the firmware reads ECC result `0x7858` ~10500× (1/
page) and writes ECC_Ctrl `0x7857`, BUT MAME's model just returns **0 (no error)** for
all ECC status/flag reads (`nand_785e_r`, `nand_ecc_low_byte_error_flag_1_r`) and its
games work. My controller already returns 0 there — matches MAME. So the firmware
trusts the hardware "no-error" flag; it does not software-compare ECC parity.

**The one remaining unknown = the FTL block-id byte layout in OOB.** MAME works because
its image carries the **real OOB with real per-block logical ids**; ours stripped them,
so 0xFF/sentinel OOB gives the mount-scan no valid map → directory still resolves wrong.
Since our image is a flat/linear FAT, the correct ids are **identity** (physical block
N ↔ logical N). Need: *which of the 16 OOB bytes hold the id, in what size/endianness*.

## 16. 🩶 EYES RENDERING — filesystem HLE'd, display pipeline driven

The eyes render. The full chain now works end-to-end via HLE (no OOB dump needed):

**Filesystem HLE (baked into `default_furby_cpu`):**
- `id==2` find-file `0x078730` → size (existing).
- `id==4` **open** `0x090f7f`(name,mode) → resolves the UTF-16 path against the FAT
  (`vfs_resolve`), allocates a handle carrying start-cluster/size/pos, returns it in
  r1. Fixed the "opens 4× but reads 0×" wall (native open failed through the FTL).
- `id==5` **read-byte** `0x091c93`(handle) → fgetc-style, returns the next file byte
  from the cluster chain (`FAT_DATA_START_BYTE + (cl-2)*512 + off`, chain-followed).
  Verified: color.dat → 0x01 matches the real file.

**Display sync:** the eye-LCD compositor waits on `0x707c` bit15 (vblank/ready) and
reads `0x707f` bit7 — modeled via `set_reador`. Without them the post-event display
code spins.

**Main event loop:** after boot the firmware sits in an event-queue wait at
`0x06cd8b` (`[0x5a45]` consumer idx != `[0x5a46]` producer idx). Events are normally
posted by peripheral IRQs (audio DAC / display / sensors) not yet modeled. **Posting
events** (bumping `0x5a46`) drives the loop → the compositor runs → **palette + sprite
RAM fill with real graphics.**

**Result (measured):** `0x707f` PPU-enable written 13×; **palette `0x7300` = 107
nonzero RGB565 entries** (real colors, live from the PPU); sprite RAM `0x7400` = 999
writes with real data. From an all-zero baseline. The machine is drawing its eyes.

**Real event driver FOUND — IRQ line 5.** The IRQ vector table is at RAM `0x6ff0`
(8 entries × 2 words, `fe88 <handler>`): line0→`0x08f205`, line1→`0x08f219` (timer),
… **line5 (`0x6ffa`)→`0x08f23f`, the handler that calls the event-post `0x06d412`**
(increments `0x5a46`). So `cpu.raise_irq(5)` posts exactly one clean frame event per
fire (verified: 0x5a46 = 1,2,3,4,5,6) — the *correct* driver, replacing the hacky
`0x5a46` poke. Each frame streams ~1M bytes of tile graphics through the banked
window (`0x7810`), which is normal compositing traffic, not a pathology.

**Eye output is pure PPU compositing (confirmed):** full-address-space frame-diff
shows NO RAM framebuffer (only 28 state words change); 0 display DMAs; SPI `0x7942`
gets only ~440 writes/frame. The GPL16258 PPU composites sprites+tiles+palette to the
eye-LCD in hardware — to render a visible frame you emulate that compositor (sprite
table `0x7400`, tile data streamed via the banked window, palette `0x7300`). That's
the remaining piece for a *viewable* image; the firmware-side rendering is driven and
working.

---

**Concrete next step (path B / FTL, if pursued):** find the mount/scan function that reads the OOB region
(read-buffer + 256 words) and extracts the block id — disassemble its OOB access to
read the exact offset/width — then reconstruct the image with identity ids there (+
0xFF bad-block marker) and the native FS mounts. (Alt: locate a raw Furby NAND dump
*with* OOB to read the real id layout directly.) All other pieces are in place.

### Progress with 528-image (this session)
- **FTL granularity found:** the mount-scan reads pages **B×64 and B×64+1** for each
  32KB block (block = 64 pages), ~100 blocks — reading each block's OOB metadata.
- Tried identity-id encodings (block# 16-bit @OOB 0, @OOB 2, page# @0, block# 32-bit
  @0). **None mount** — but they DO change execution: with the 528-image the firmware
  leaves the old checksum spin (0x091ee5) and now *runs* through new code, lpc walking
  0x0788c2 → 0x08ffdb → 0x08ff8f → 0x08ff75 → 0x091f70 over 2e9 insns. So it's
  executing the FTL/mount path, not hard-stuck — but find-file still returns 4×/not-
  found, scheduler never runs. My id offset/format is wrong.
- **The block-id extractor is `0x08ff71`** — the per-block call inside the mount loop
  (`0x078889`→`0x0788ef`, counter `[bp+4:5]`). It's where the firmware spends its time
  (lpc 0x08ffxx). **Single-step `0x08ff71`** to read exactly which OOB byte(s) it loads
  as the logical id and how it compares them → that gives the true layout. Then encode
  identity there. This is the precise remaining task; everything else (528 geometry,
  reconstruction, alignment, ECC-clean) is done and verified.

## §17 — True native FTL: findings (the raw-OOB dump)

`furbhax/firmware/NANDmainFLASH.BIN` is the **raw physical NAND with OOB**
(262144 × 528-byte pages = 512 data + 16 spare). The core supports this geometry
(`set_nand_page_size(528)`). Goal: let the firmware's own flash-translation-layer
mount it (no HLE). Status: **runs the mount path, native find-file still misses.**
Characterized why:

- **Not a GameCode mismatch.** Our `GameCode.bin[:512]` is byte-identical inside the
  furbhax `no-dividers.bin` (at physical 0x3945200). Same firmware version.
- **The dump is in physical order; the FTL remaps blocks.** Measured physical→logical
  block (32 pages/block) for known files against our working *logical* image
  (`furby-nand (Fixed OOB Data).bin`, 223232 pages):
  | file | phys block | logical block |
  |---|---|---|
  | GameCode | 3665 | 121 |
  | Base.CEL | 385 | 0 |
  | Base.PAL | 1813 | 2309 |
- **The OOB is not a plaintext logical-block number.** First-page spare of GameCode's
  physical block reads `4d e5 43 7e 0a 13 a6 70 …` — gpac800 FTL metadata / ECC, not
  `121`. So the logical image can't be rebuilt by a simple "read LBN, sort blocks."

**Scoped next step (either path):**
1. *Reverse the gpac800 FTL spare format* — decode how the 16-byte OOB encodes the
   logical block number (+ ECC), then rebuild the logical image from the raw dump and
   confirm it matches the known-good `Fixed OOB Data` image byte-for-byte.
2. *Trace the firmware's mount* — instrument NAND spare-area reads (col 512–527) during
   boot with the 528 image; verify the FTL is being served OOB and where its map-build
   diverges. (MAME `generalplus_gpl16250`'s gpac800 bootstrap is the reference.)

The emulator already mounts + reads the real filesystem via the parsed FAT on the
logical image, so this is a fidelity upgrade (firmware-driven vs pre-fixed image),
not a functional blocker.

### §17.1 — FTL cracked structurally + map table located (major update)

Reversed the gpac800 FTL far enough to **reconstruct the logical image from the raw
physical dump, byte-exact.** `tools/ftl_reconstruct.py` does it and validates.

Proven, verifiable facts:
- **Block model:** 32 pages / 16 KiB per block, **whole-block remap** (no sub-block
  scrambling). Reconstruction is **6814/6814 mapped logical blocks byte-exact (100%)**.
- **Local layout:** a 2-plane, 8-block interleave — logical offset climbs `+8` every 8
  physical blocks (`log ≈ 16·(phys//8) + phys%8 + base`), with periodic zone resets and
  bad-block skips.
- **The FTL map table is located:** "system" blocks whose page-0 spare begins
  `c2 00 c3 00 c4 00 …` — physical blocks **3920–3927 and 4056–4057** — each an
  **8192-entry little-endian u16 table** (`482, 517, 519, 521, 523, 525, …`; the `+2`
  step is the plane bit). This is the firmware's own block-translation table.

Remaining for a **reference-free** rebuild (no known-good image): decode that table's
exact value encoding — zone base + plane bit + bad-block indirection. Direct and `>>1`
interpretations don't line up yet, so there's a header/zoning layer to pin. Once decoded,
the emulator can serve the firmware its own logical view from the raw OOB dump = the true
native FTL, no HLE.

### §17.2 — Where the FTL lives: PROVEN (firmware trace)

Traced every NAND page GameCode reads across a full boot on the raw OOB image
(`nlog` records page + triggering PC). Result, definitive:

- GameCode reads **only blocks 0–204**, and **never touches the system/map blocks
  (3920–3927, 4056–4057)** — not once, anywhere in boot.
- Just **7 NAND-reader PCs**, all in a tight low-level page driver (`0x76fea`–`0x7732c`).

Conclusion: **the FTL is not in GameCode — it's in the GPL16258 internal boot ROM.**
GameCode issues *logical* block reads and relies on the ROM having already built the
physical→logical map (by reading those system blocks) and presenting a logical NAND.
We HLE past the ROM, so on a raw physical dump those reads land on physical blocks and
miss. This is an **undumped-ROM boundary**, not a decoding bug — a true native
in-firmware FTL is impossible without a boot-ROM dump.

The resolution is what the emulator already does, made explicit: **`tools/ftl_reconstruct.py`
*is* the ROM's FTL, replicated** — raw physical dump → logical image, byte-exact. Pipeline:
`raw NAND (+OOB) → ftl_reconstruct → logical image → emulator boots`. The only open
purity item (reference-free reconstruction) is decoding the ROM's own system-block table
format, which the ROM — not the firmware — consumes.

## §18 — Audio: the .AMF megafile container cracked

Each personality has a `<Name>.AMF` audio megafile (Base.AMF ≈ 7.4 MB) plus shared
`AudioMegafiles/*.bin`. Container format (reverse-engineered, `tools/amf_extract.py`):

- **top-level u32 offset table**, self-sizing (`table_bytes == offsets[0]`), → categories
- each **category** is a second-level u32 offset table → **leaf clips**
- each **leaf clip**: `[u32 length][u16 sample_rate=16000][SACM data]`

Base.AMF category 0 alone yields **1584 clips** (~5.6 MB), all 16 kHz — the Furby's
speech library. `amf_extract.py` rewraps each as a standard GeneralPlus **`.a18`**
(`00 ff 00 ff` / `GENERALPLUS SP` header), so they're first-class files.

**Frontier:** the payload is GeneralPlus **SACM** — a proprietary, *entropy-coded*
codec (leaf entropy ≈ 7.9 bits/byte, so not plain ADPCM). PCM decode is a separate
codec-RE task (like the FTL table, likely needs the ROM/codec ref). Container = done.

## §19 — Eye animation format fully cracked (SPR/CEL/PAL)

Cross-referenced Furby-ReConnect's `furby.py` (l0ss/swarley) and reimplemented
dependency-free in `emu/furby_display.py`. The complete pipeline:

- **CEL**: one 64×64 cel = 0xC00 bytes, 64 rows × 48 bytes, 3 bytes → 4 six-bit
  pixels (MSB-first), each a palette index 0..63.
- **PAL**: 64-color RGB555 banks, 0x80 bytes each.
- **SPR**: 0xE0 header of **16 playlists** (framecount:u16, t2_off:u32, layer:u32,
  0x40), → per-playlist frame-pointer tables → **frames**. Each frame = **9 u16:
  `[cel0,pal0, cel1,pal1, cel2,pal2, cel3,pal3, 0xFFFF]`** — four 64×64 quarter-cels
  laid TL/TR/BL/BR into a **128×128** eye. **Playlist 8 is the eye animation.**

`run.py --eyes <Personality> --gif out.gif` now renders the *real* animation in the
firmware's own frame order (Base = 14 frames, verified flawless at palette bank 64).
Remaining nicety: the eye palette handle in frames is a fixed value (0x10F2) resolved
by firmware, so per-personality palette uses a preset (Base) / colorful-and-smooth
auto-detect for the rest — shapes/animation are exact for all 7 personalities.

## §20 — Live emulator frontend + the animation-engine frontier (honest status)

Built a real CLI **monitor** (`emu/furby_monitor.py`, `run.py --monitor`): it runs the
actual µ'nSP CPU on GameCode and shows the live machine — PC + disassembly, the event
loop, instruction count, NAND reads, and the display/palette state the firmware sets up.
Nothing is pre-rendered; every value is read from the running core.

**What it reveals (and the honest limit):** the firmware boots, mounts its FS, services
interrupts, then **idles in its event loop** (spins at 0x06cd8b; parks in the timer IRQ
at 0x08f219). It never animates the eyes because a real Furby starts animations from its
**behavior engine** reacting to sensors/wake/BLE — none of which exist in the emulator.
The snapshot sprite RAM / palette / display regs read **all-zero**: the firmware composes
no display while idle. So there is no live frame to show yet.

Naive attempts to force it (raising IRQ line 5) get *taken* but jump to a stray loop
(0x2205fb) with the event queue frozen — the vector isn't a clean frame handler in that
state. Driving it for real means reversing the **interrupt → event-queue → behavior →
SEQ/XLS → SPR-playlist** chain and injecting the trigger a sensor would, then reading the
animation engine's live frame index and bridging it to `furby_display`. That's the real
next mountain — not a GUI task.

`furby_live.html` / `emu/furby_live.py` are honestly **decoder-output viewers** (they
replay the decoded SPR playlist-8 frames), clearly labeled as such — not live emulation.

## §22 — Toward live animation: frame IRQ cracked, dispatch is the wall

Real progress on driving the firmware to animate:

- **Frame interrupt identified & verified.** The eye/display frame handler is **0x08f23f**,
  reached via **IRQ line 5** (vector trampoline at word 0x6ffa: `fe88 f23f` → `goto 0x08f23f`).
  Driving PC=0x6ffa manually executes it cleanly: it runs `call 0x06d412`, which **posts a
  frame event** (increments producer index 0x5a46). Verified.
- **`cpu_raise_irq` is buggy.** Firing it does not push/dispatch cleanly (SP unchanged, state
  corrupts, PC derails to low memory). Manual trampoline entry is the working workaround.
- **Frame events now flow.** Manually injecting the frame handler each "frame" climbs the
  producer 0x5a46 (1→60) and the firmware consumes one (consumer 0x5a45 0→1).
- **The wall: event/behavior dispatch derails.** On consuming an event the firmware runs
  RAM-resident routines (coherent code at 0x0000xx, bp-relative), then jumps to unmapped
  0x2xxxxx — it hits a runtime dependency the emulator doesn't satisfy (likely a peripheral/
  DSP/timer state or a data structure the behavior engine expects).

**Next real steps:** (1) fix `cpu_raise_irq` so interrupts dispatch through the vector table
like the manual path; (2) trace the dispatch from the event-loop consumer into the 0x2xxxxx
derail and identify the missing runtime state; (3) once it survives dispatch, find the
animation engine's live frame index and bridge it to `furby_display`. This is the deepest
remaining subsystem — genuine multi-session firmware+peripheral RE, meaningfully advanced here.

### §22.1 — Frame heartbeat working: firmware drives its live display pipeline

Fixed the injection: SP is register **0** (`enum { SP, R1, R2, R3, R4, BP, SR, PC }`), so the
earlier manual entry left a broken stack and the handler's RETI derailed. Pushing the return
context properly (via `poke`) before entering the vector makes the frame handler run and
return cleanly. Exposed as **`cpu.frame_tick()`** in `unsp_native.py`, wired into
`run.py --monitor`.

Result: with a frame heartbeat, the firmware leaves its idle loop and **runs its real display
pipeline live** — PC moves through the display code (0x08fxxx / 0x067xxx), and it loads real
palette entries (RGB565) + sprite-RAM/display-register state each frame (from all-zero at
boot). This is the emulated firmware genuinely driving the display, not replayed frames.

(`cpu_raise_irq`'s auto-dispatch still corrupts SP — `frame_tick` is the reliable path; fixing
the C dispatch to match is a cleanup. Next: trigger the behavior engine to start a full eye
animation so the pipeline composes a complete eye frame.)

## §24 — Fixed cpu_raise_irq: interrupts dispatch correctly

Root cause of the IRQ-injection crashes: `irq_vecbase` (line n → vecbase + 2n) was only
initialized inside `cpu_set_timer`, so `cpu_raise_irq` before a timer setup used vecbase=0
→ `PC = 0 + 2·line` → derail into low memory. Fixed by initializing `irq_vecbase = 0x6ff0`
in `cpu_reset` (it's a fixed SRAM trampoline table). Now `raise_irq(5)` vectors cleanly to
the frame handler 0x08f23f, and `frame_tick()` uses the proper vectored path (no more
manual stack-poke workaround). Verified: raise_irq(5) → 0x08f23f → 0x06d416 (event post),
and the heartbeat drives the live display pipeline as before.

## §25 — FIQ dispatch implemented (core correctness)

The core had `fiq_en`/`secbank` but **no FIQ service** — fast interrupts never dispatched.
Added `cpu_raise_fiq` + FIQ service in `cpu_run` (higher priority than IRQ, no nesting,
vectors to `fiq_vec`=0x6fec, RETI clears `in_fiq`). Verified: `raise_fiq()` vectors cleanly
into the FIQ handler 0x08f1df and the firmware stays stable. Honest note: the two FIQ
handlers are an empty stub (0x08f1dc) and a 0x78a1 fast-timer reader (0x08f1df) — driving
it does **not** trigger the eye animation, so this is a correctness fix, not the behavior
trigger (that remains the open behavior-state-machine RE from §22–§24).

## §26 — THE core issue: display-compositor deadlock on an unbuilt display list

Root-caused why the eyes never render (traced end to end):

1. A frame event → the main loop runs the **display compositor**, a recursive display-list
   tree-walker (`0x067f00`/`0x067eb0`), reading each node's child-count via `0x08fc17`→
   the far-read `0x08fe15` (0x7810-banked).
2. The node it reads is **garbage**: child-count `0x1004` (4100), far-pointers with a
   nonsense bank (`0x3071`). The **root display-list pointer is uninitialised** — the list
   was never built.
3. The 4100-wide × recursive walk is effectively **infinite** and never returns, so the
   **main event loop is blocked**. Measured: consumer stuck at 1 while producer climbs to
   30, unchanged over 80M+ instructions.
4. Deadlocked → no further events processed → the firmware can never set up a real
   animation → the display never renders.

So it was never "idle" — it's a **hard deadlock** the instant the display is driven before
the firmware has built a display list. `run.py --diag` now detects it (main-loop liveness).

**Next thread:** find why the display list is never built — the list-builder and its
trigger, and whether a subsystem the firmware waits on (audio DSP / motor / a "graphics
ready" handshake) isn't satisfied in the emulator, gating the build. Fix that and the
compositor gets a valid list → terminates → the pipeline renders.

### §26.1 — Likely culprit: the 0x7810 banked-window NAND mapping

The compositor reads display-list/graphics nodes from the **CS banked window**
(0x200000–0x3fffff → NAND, `read16`), selected by `0x7810 & 0x3f`. Two red flags:

- **`cs_base` is never set** (stays 0). It's documented as "word offset into NAND where
  cs-space starts", and `cpu_set_cs_base` has no caller — so if the graphics/CS region
  doesn't start at NAND offset 0, every banked read is shifted wrong.
- the offset formula `(a-0x200000) + bank*0x200000 - 0x20000 + cs_base` overruns the
  114 MB NAND for larger banks (bank·2 Mword ⇒ up to 252 MB), and the compositor was
  using banks that land it on garbage (child-count 0x1004, bank-ptr 0x3071).

**Hypothesis:** the banked-window mapping is misconfigured, so the compositor reads the
wrong NAND bytes and interprets graphics data as a display-list tree → the §26 deadlock.
**Fix direction:** derive the correct `cs_base`/formula by tracing a *known* banked read
(e.g. the firmware fetching a known CEL/SPR asset) and matching it to that asset's real
NAND offset; then the compositor should read a valid list and terminate.

(Also latent, not yet triggered: writes to 0x200000–0x3fffff go to flat `c->mem` while
reads route to NAND — a read/write asymmetry to fix if the firmware ever uses SDRAM there.)

## §27 — Root cause + CS/SDRAM fix (MAME-guided)

Reviewing MAME's `generalplus_gpl16250`/`gpl16250_nand` cracked the root cause open:

- MAME boots from the **internal ROM (NAND bootstrap)** and backs the CS space with real
  **SDRAM** (`m_sdram`/`m_sdram2`, `m_csbase = 0x20000`, `m_vectorbase = 0x6fe0` — matches
  ours). The banked window: `realoffset = offset + bank*0x200000 - csbase`, read through the
  CS space (cs0=SDRAM2, cs1=SDRAM, cs2=NAND).
- **Our emulator HLE-boots (skips the ROM)** and wrongly routed the whole CS window straight
  to NAND. And GameCode references 0x7810 (bank) 49× but the CS-size regs 0x7820–0x7824
  **zero times** — it *relies on the boot ROM to configure CS/SDRAM*, which we skip.

**Fixed** the CS window: backed it with writable zero-init SDRAM (`csram`, 4 M-words) +
NAND for the cs2 region, using MAME's formula/`csbase`. No regressions (boot/FS/IRQ/FIQ/
display all green); the main loop now survives to **2 events** (was 1) before the deadlock.

**Remaining root (the real wall):** even with correct SDRAM, the firmware never *builds* a
display list — because it never activates an animation, and the boot-ROM SDRAM/CS
initialization (which the firmware depends on) isn't emulated. Full fix = emulate the
GPL16258 boot-ROM bootstrap (SDRAM/CS init) per MAME's `gpl16250_nand`, or drive the
behavior engine to activate an animation. Both are real, scoped, multi-session work.

## §27.1 — Real boot init fully replicated; root is confirmed behavioral, not boot

Added the boot-ROM **CS-config defaults** at reset (MAME gpac800 bootstrap):
`0x7820=0x0047, 0x7821=0xff47, 0x7822=0x00c7, 0x7823=0x0047, 0x7824=0x0047`. Combined
with the CS/SDRAM backing (§27), our HLE boot now matches MAME's real bootstrap
(GameCode copy to 0x050000, entry 0x050020, vectorbase 0x6fe0, CS defaults, SDRAM).

**Result: the deadlock still persists.** So with the boot init *fully and correctly*
replicated, the firmware STILL never activates an animation / builds a display list.
This definitively rules out the boot init as the cause. The true remaining root is
**behavioral**: the firmware waits for a real event (sensor / wake / BLE / petting) to
start an animation, and nothing in the emulator supplies it. Driving the display before
that event is what produces the §26 compositor deadlock.

Everything cleanly-fixable is fixed (IRQ, FIQ, CS/SDRAM, boot defaults). The eyes need
the behavior engine driven by a synthetic sensor/wake event — the genuine open frontier.

## §28 — Both display paths confirmed; single root (animation activation)

Chased the alternate theory that the eyes bypass the PPU (per README). Confirmed:
- **PPU sprite compositor** (0x067eb0 tree-walker): the path I deadlock by driving frames.
- **Eye-LCD driver**: GPIO bit-banged over P_IOB/P_IOC ("LCD lines", regs 0x7869/0x7050/
  0x786a etc.) — a genuinely separate subsystem, NOT the PPU.

Both are gated on the **same single condition**: an animation being active. With none
active: the PPU compositor walks an unbuilt list (§26 deadlock) and the eye-LCD driver
never clocks a frame out. So every subsystem traced this campaign — PPU, eye-LCD, event
loop, compositor — converges on one root: **the behavior/personality state machine never
selects an animation.** The 14 behavior states each run the generic event loop; the
firmware sits in an idle one, waiting for a real-world input to transition.

That state machine (mood engine + XLS action tree + sequence player) is the sole
remaining frontier. Everything beneath it is verified correct and fixed.

## §29 — BREAKTHROUGH: the behavior state machine is mapped

Found and mapped the personality engine's core dispatcher — the exact mechanism that
gates every animation:

- **Dispatcher `0x06158b`**, gated by `[0x4370]==1` (open), switches on state var **`[0x4e8c]`**:
  - `0` → `0x0616fe` **idle** (where the firmware sits — confirmed via live stack walk)
  - `1` → advance to state 2
  - `2` → `0x07f680`; checks wake reason `[0x534f]` (currently `0xff` = "no trigger, don't advance")
  - `3` → `0x061608`: calls `0x061855` then **`0x083ec4(8)`** — sets animation selector
    **`[0x5a58]=8`** (playlist 8 = the eyes) and calls the play chain `0x07060d`→`0x0709f1`
  - `4`,`5` → further states
- **`[0x5a58]` = the animation selector; `[0x534f]` = the wake reason.**

Proven controllable: a synthetic in-context call to `0x083ec4(8)` set `[0x5a58]=8` and drove
**710 writes to the eye-LCD GPIO lines** (0x7869/0x7050) — the eye driver got exercised for
the first time. (Out-of-context it can't hold state, so no clean frame yet.)

**Why the eyes stay dark, now exact:** the firmware sits in state 0; advancing 0→…→3
needs a **wake trigger** that sets `[0x534f]` to a real reason (not `0xff`) and lets state 0's
event loop return so the dispatcher re-runs. That single transition is the whole ballgame.

**Next (precise, small):** find state 0's loop-exit / `[0x534f]` producer — the sensor/wake
input the firmware reads — and supply it. Then the machine marches 0→3 on its own and plays
playlist 8 through the real driver. This is no longer a search; it's one identified gate.

## §30 — MASSIVE BREAKTHROUGH: the firmware runs its whole wake sequence live

Got the real firmware to march its entire wake/boot behavior sequence and drive the
display hardware — the thing that was a black box all project. The recipe:

1. **Break the compositor deadlock** — HLE `id=6` on `0x08fc17` clamps absurd display-list
   child-counts to 0, so an unbuilt list can't infinite-loop. This alone let the state
   machine advance **0 → 2** on its own.
2. **Supply the wake reason** — `poke [0x534f]=1`. State 2 then marches **2 → 3 → 4**.
3. **Emulate the eye-LCD controller status** — `set_autoclear(0x7961,0x30)` (busy clears) +
   `set_reador(0x7961,0x80)` (ready set). State 4's eye-LCD driver (`0x080c2d`) then
   completes its transfer and marches **4 → 5**.

Observed live: **SPI TX `0x7942` fired ~1900 writes** (LCD command/init stream, 8 distinct
values), **PPU enable `0x707f` toggled 14×**, and a **192-entry palette loaded**. The full
display pipeline executes — states, eye-LCD driver, SPI, PPU — end to end.

**Honest limit:** the *content* is garbage — the palette is a 3-value repeat
(`0x0441/0x4110/0x1004`) and the animation id reads `0x1004`. The graphics are being read
from the wrong CS/NAND offset (the §27/§26.1 banked-window mapping), so the pipeline draws
noise, not the eye. **Final piece:** fix the graphics CS/NAND read offset so the real
CEL/PAL data loads — then the same live pipeline draws the actual eye. Everything upstream
(state machine, wake, eye-LCD handshake, SPI) is now proven working.

## §31 — The wake animation is a code-script (not a frame list)

Traced the wake animation from state 3's loader `0x08cfa7`: it stores the animation pointer
`0x09:0xc4ce` into `[0x53fe]` (a *separate* animation system from `[0x5a58]` — that `0x1004`
was a red herring). Crucially, **`0x9c4ce` is inside GameCode** (0x050000–0x0bd000), so the
wake/boot animation is **built into the firmware**, not a NAND personality file.

Its data at `0x9c4ce` is a **table of far pointers** (`0x08:0xcf9b, 0x08:0xcf9f, 0x06:0x…`)
and each target is **code** (`push bp` prologue) — i.e. the wake animation is a **scripted
sequence of ~30+ GameCode routines**, each driving one animation step, not a simple
`[cel,pal]` frame list. Rendering it = executing those routines, which the firmware does
when it plays the animation.

**Consequence for the "real pixels" goal:** two separate graphics paths exist —
(a) the **built-in wake script** (code-driven, in GameCode), and (b) the **NAND personality
animations** (the CEL/PAL/SPR playlist-8 format `emu/furby_display.py` already decodes).
The live pipeline (§30) currently runs path (a). Getting clean eye pixels means either
faithfully executing the wake script's routines, or steering the firmware to load a NAND
playlist through path (b). Both are multi-session RE. The live wake pipeline itself is done
and proven; this is the remaining content campaign, now precisely scoped.

## §32 — csram was a mis-model; the wake palette was a DEBUG FONT

Two corrections from chasing the pixels:

1. **The §27 csram/SDRAM window was a mis-model for this firmware.** The original edit had
   silently failed (anchor mismatch) so csram was never active — and that turned out to be
   *correct*: the Furby reads its graphics from **NAND through the 0x7810 bank**, not SDRAM.
   Actually enabling csram routed those reads to zeroed SDRAM and blanked the display
   (diagnostic regressed 9→8). csram is now allocated but **disabled by default**
   (`csram_words=0`, `cs_base=0`); `set_csram_words()` kept for experiments.
2. **The palette I was chasing in the forced-wake path is a debug font**, not the eyes:
   the loader `0x07ea98` reads the path string `A:\Graphics\DebugFont_Pal.bin` (at GameCode
   `0x9b8ad`) and loads it via `0x0785de`. So state 4's palette activity is a debug/overlay
   artifact. The real eye frames come from state 3's **wake animation code-script**
   (`[0x53fe]` pointer → the GameCode routine table at `0x9c4ce`), executed per-frame.

**Next:** find the per-frame executor that consumes `[0x53fe]`/`[0x53fc]` (the wake-script
interpreter) and capture *its* output to the eye-LCD — that's the true eye pixel path.

## §33 — The wake animation IS live; its render output is the gap

Confirmed the wake animation genuinely runs under the forced-wake gates:
- `[0x53fe]/[0x53ff]` = `0x09:0xc4ce` (the wake pointer, set correctly by the state-3 loader)
- `[0x53fc]` (frame index) **advances each frame** (observed 0→2) — it's animating
- executor **`0x08cfe4`** reads `[0x53fc]`/`[0x53fd]`, and per frame indexes a **6-word-per-
  frame table** at `0x9c4ce` (via `0x091efb`, frame×6 → 3 far-pointers/frame) and dispatches.

So the animation engine is executing. The remaining gap is purely its **visible render**:
bank-0 palette / sprite output stays empty, so no eye pixels materialize. The per-frame
routines' graphics output isn't being produced/captured in the emulator yet.

Also nailed down: state 5 waits on PPU flag `0x7072` (display-DMA done); faking it clear
pushes the CPU into banked code at `0x330000` (needs the real PPU display-DMA emulated).
And the state-4 palette is the **debug font**, not the eye.

**Next:** decode executor `0x08cfe4`'s per-frame dispatch (the 3 far-pointers/frame) to see
where each frame's pixels are written, and emulate the PPU display-DMA (0x7072) so the
rendered frame reaches the eye output.
