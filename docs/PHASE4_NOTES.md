# Phase 4 — peripherals & boot-to-main-loop

With the memory map correct (Phase 3), the firmware boots. Phase 4 is feeding it
the hardware it polls, one wall at a time, until it reaches — and then runs — its
main loop. Config lives in `default_furby_cpu()` (`unsp_native.py`).

## Peripheral stubs so far (each unblocks a boot poll)

| reg | model | why |
|---|---|---|
| `0x780f` P_PowerState | reads `0x0002` | clock FSM "settled" (two boot polls) |
| `0x7819` P_Cache_Ctrl | bit1 auto-clears | cache-invalidate "done" |
| `0x7850` | bit15 forced high on read (`reador`) | command/done handshake — always "complete" |

New core capability: **`cpu_set_reador(addr, mask)`** — bits that always read high
even after the firmware writes the register (for hardware status/"done" bits).

## Bypass patches (like MAME's)

- **`0x0846f6`** — a `jmp self` fail-spin taken when the **"PROGRAM ROM" self-check**
  (`0x091cb4`) returns non-zero. That check (nested ROM/hardware verify) can't pass
  under emulation, so — exactly as MAME does — we NOP the spin so boot continues.

## Milestone: boots to the main loop

After the bypass the firmware runs **600,000,000 instructions with no trap/hang**
and touches **691 distinct peripheral registers** — full-chip device init (GPIO
ports, SPI, peripheral clusters). It then **settles into a bounded main loop**
(only ~6 code pages, hot at `0x0788xx`), having configured the interrupt
controller (`0x78a0`) and SPU (`0x7b80`).

The loop currently **spins**, because it's event-driven and nothing fires events:
the **TimeBase heartbeat (`0x78b0`) and interrupts are not running yet**. The eyes
(PPU `0x7300`/`0x7400`), audio (SPU), and motor stay idle waiting for that tick.

## Next: the heartbeat

Implement **interrupts + the TimeBase timer** in the core:
- TimeBase counts CPU cycles → raises a periodic IRQ (the 32768 Hz-derived tick).
- Interrupt controller (`0x78a0`) dispatch: on an enabled pending IRQ, push PC/SR,
  jump the vector, `reti` restores. (`int on/off`, FIQ/IRQ enables, `reti` already
  in the core — nothing *raises* an IRQ yet.)

Once the tick fires, the main loop starts doing work: driving the eye PPU, the SPU,
and the motor. That's when it stops being "boots" and becomes "alive."

---

# UPDATE — the heartbeat is built and beating

Implemented in the native core:
- **Interrupt dispatch**: on an enabled, pending IRQ (and not already in one) the CPU
  pushes PC+SR and jumps `irq_vecbase + 2*line` (`0x6ff0` — the IRQ0-7 `goto`
  trampoline table the firmware set up in SRAM); `reti` unwinds and clears in-IRQ.
- **Periodic timer** (`cpu_set_timer(line, period)`) raising an IRQ every N insns.
- **Status-bit models** so interrupt *dispatchers* route correctly:
  `reador` (bit always high), `readclear` (bit returned then cleared = pulsed
  status), and `cpu_set_timer_status(reg,bits)` (timer sets a source bit on tick).

**It wakes the firmware.** The IRQ handlers are dispatchers that read the interrupt
status reg `0x78a0` and branch on the source bit (IRQ0→bits4/5, IRQ1→bit7, …). Firing
**IRQ line 1 with `0x78a0` bit 7** set makes the main loop break its spin and start
device work — **+160 peripheral registers touched and a PPU palette write** appear.
So the timer/tick source is IRQ1 / `0x78a0:0x80`.

**Next wall — banking.** Right after waking, the firmware executes code in the
**`0x200000-0x3FFFFF` banked window** (trap `0x20ffff→0x210000`), which is switched
by **`P_BankSwitch_Ctrl` (0x7810)** — the banking we deferred in Phase 3. Modelling
that (read `0x7810` → select which file region the `0x2xxxxx` window maps) is the
next step; then the woken firmware can run its full program and actually animate.

New core knobs: `cpu_set_timer`, `cpu_raise_irq`, `cpu_set_timer_status`,
`cpu_set_readclear`, `cpu_irq_taken`.

---

# Banking analysis — and the full-NAND dependency

The woken firmware quickly does `goto mr` → machine `0x19593e`, which decodes back
through the header (`0x050008` = file byte 8) into random high memory. Tracing it
against MAME's memory model explains why.

**Two separate spaces exist:**
- **SDRAM** at machine `0x050000+` — the boot-copied GameCode image we run from
  (`machine 0x050000 == file 0`, verified).
- **CS space (raw NAND)** — a *different* backing, reached via two windows:
  - non-banked `map(0x020000, 0x1fffff)` → `cs[A - 0x020000]`
  - banked `map(0x200000, 0x3fffff)` → `cs[(A-0x200000) + (0x7810&0x3f)*0x200000 - 0x20000]`
  - `m_csbase = 0x20000`.

Cross-checking with our data: GameCode sits at **CS offset `0x030000`** (so the
non-banked window maps machine `A` → `file[A - 0x050000]`, matching what we run).

**The blocker:** `0x19593e` → `file[0x14593e]` — **beyond GameCode.bin's
`0x6ff37` words**. The woken firmware reaches into NAND regions our 917 KB
`GameCode.bin` doesn't contain (the other partitions: `Slots/`, `Personalities/`,
`AudioMegafiles/` — all in the **114 MB `furby-nand (Fixed OOB Data).bin`**, which is
an unpulled git-LFS pointer in our clone).

**So the next concrete step is:** `git lfs pull` the full NAND, strip its OOB the way
MAME's `nand_create_stripped_region()` does, and load it as the **CS space**. Then
implement the two-window banking formula above in the core (a `cs[]` buffer + the
`0x7810` bank math). At that point the woken firmware can follow its `goto mr`
targets into the real NAND and keep running — toward driving the eye PPU it already
started poking (that palette write was real).

The CPU, memory map, boot, peripherals, and interrupt heartbeat are all done and
working. What's left to see the eyes is: **the full NAND + the banking window.**

---

# UPDATE — it's a NAND controller, not memory banking (and it's wired)

The "banking" turned out to be the wrong model. The Furby chip (gpac800) reaches
flash through a **NAND controller** (regs `0x7850-0x7857`), not a memory-mapped
window:
- `0x7850` NAND status (bit15 = ready — this is the reg I'd stubbed at boot)
- `0x7851` command · `0x7852/0x7853` address low/high · `0x7854` **data port** · `0x7856` type
- effective byte = `(page * 512 + page_offset) << shift` (512 = our OOB-stripped pages;
  GameCode's header sits at NAND byte `0x1e5200` = page 3881, 512-aligned ✓)

**Implemented** in `unsp_core.c` (`cpu_load_nand`, `nand_recalc`, the `0x785x`
read/write cases) and pulled the real **114 MB NAND** via the GitHub LFS media
endpoint. The firmware now drives the controller — it reads the NAND **ident** and
streams bytes through `0x7854`.

**Where it stands:** the firmware IDs the NAND at boot (24 ident reads) but does
**0 data reads** — the actual block loading happens later, in the main-loop phase.
After the heartbeat wakes it, it jumps to machine `0x0e0000+` (personality/game
code that lives *beyond* the GameCode SDRAM image, `>0x0bff37`) — which is **empty
because it hasn't streamed those blocks from NAND yet**.

**The last mile:** get the NAND **ident/geometry** exactly right (romtype 0 `c2 76`
vs 2 `ad f1 80 1d` — the `0xc200` in `P_NAND_DMA_Ctrl` hints romtype 0) so the
firmware's loader proceeds from ident → data reads → populates SDRAM at `0x0c0000+`,
then the woken main loop can actually run the personality code that drives the eyes.
The controller, NAND, CPU, memory map, and heartbeat are all in place — this is
dialing in the flash geometry + loader trigger.

---

# UPDATE — the ISR crash was EXTOP (fixed); firmware now runs stably

The post-wake derail was **register corruption**, proven by the derail point moving
with the timer period. Root cause: the interrupt handlers begin with
`push r1,bp` then **`0xff80` = EXTOP** (ISA 2.0 extended-opcode prefix) doing
`push R15..R8` — the ISR saves the **extended register bank (R8-R15)** and pops it
on exit, deliberately leaving R2/R3/R4 untouched. Our core (a) had no R8-R15 and
(b) mis-ran `0xff80` as a MULS that trashed R3/R4 (= MR), so `goto mr` flew into
garbage.

**Fixed** in `unsp_core.c`: added R8-R15 (`ext[8]`) and implemented **EXTOP**
(the register push/pop + 2-param ALU forms). Result:
- **No more derail** — the firmware runs 200M+ instructions post-wake with the
  timer firing, **trapped=False**, thousands of IRQs serviced cleanly.
- It settles into its **main scheduler loop** (`0x091ed3`) and **polls the ADC
  (`0x7964`)** — i.e. reading its sensors, waiting for interaction. A booted,
  stable, idle Furby.

## Remaining to see the eyes
The scheduler runs but hasn't loaded/run the **personality/game code** (still 0 NAND
data reads — cmd stays `0x90` ident; the code that issues `0x00` reads to stream
blocks into SDRAM at `0x0c0000+` hasn't been triggered). Next: find what triggers
the personality load / wake animation — likely a **sensor/ADC stimulus** on
`0x7964` (mic/light/tilt), the right **timer/tick semantics**, or the correct
**NAND geometry** so the load routine proceeds. The machine is stable and running;
this is now about giving it the right stimulus, not fixing crashes.

---

# UPDATE — system DMA implemented; hardware emulation essentially complete

Added the **system DMA** controller (`0x7a80-0x7a9f`, 4 channels — MAME's
`trigger_systemm_dma`): reads a source word address, writes a dest, `length`
words, with the byte/word source/dest modes. NAND→SDRAM loads use `source=0x7854`
(the streaming data port our controller already advances), so the personality/
game-code load path is now **fully in place**.

**State of the machine:** the firmware **boots, wakes, keeps time** (a 32-bit tick
counter advances in RAM ~`0x6f91`), **services interrupts cleanly** (EXTOP fixed),
and runs its **scheduler loop** with RAM actively churning. It's a stable, running,
ticking Furby.

**What does NOT yet trigger the eyes** (ruled out this pass):
- timer tick — advancing, but the scheduler doesn't act on it visually
- NAND / DMA — wired and ready, but the firmware never *triggers* a data load
  (cmd stays `0x90` ident, `dma_runs=0`)
- ADC / sensors (`0x7964`) — fed 0x000–0xfff, no reaction

**Conclusion:** the *hardware* emulation is essentially complete — CPU (full ISA
incl. EXTOP), memory map, boot, interrupts+timer, NAND, DMA, and the peripheral
stubs. The firmware runs stably on all of it. The remaining gap to visible eyes is
**application-level**: a specific condition in the firmware's scheduler state
machine that kicks off the wake animation / personality load. Finding it is deep
firmware RE (reading the scheduler's dispatch functions to locate the trigger) —
a distinct, substantial effort, not a hardware fix.

Every subsystem is built and the machine is alive and running. The eyes are gated
behind decoding the firmware's own application logic.

---

# UPDATE — the shift bug was the wall; the personality now LOADS

The firmware wasn't waiting on an event — it was **stuck in 32-bit math** producing
garbage. Root cause: the **16-bit variable-shift instructions** were wrong in the
core:
- shift amount masked `& 0xf` instead of **`& 0x1f`**
- `asr` was logical, not **arithmetic**
- the **multi-word `-or` variants (`asror` / `lslor` / `lsror`) were unimplemented**
  entirely — these read `rd` and write `R3`/`R4`, and are the backbone of every
  32-bit shift/divide helper the scheduler uses.

Also implemented the previously-nop'd **`EXP`** and **`DIVQ`** (iterative divide)
ops, and fixed **`secbank`** to use a bank-active flag.

**Result — a real leap:** with correct math the firmware computes correct load
addresses and **streams its personality out of NAND**:
- NAND data reads: **24 → 4.3 million**
- NAND command: `0x90` (ident) → **`0x30` (data)**
- **system DMA transfers: 0 → 2,052** — personality/game blocks copied NAND→SDRAM

So it now **boots → wakes → loads its personality**. After the load it returns to
its scheduler, sampling the ADC (`0x7964`), with the **PPU still disabled**
(`0x707f`=0) — a fully-loaded Furby that hasn't enabled its display yet. Feeding
ADC/GPIO sensor values (0x000–0xfff) doesn't trip it, so the wake/display-enable is
gated behind a further application condition (likely a specific startup-state
transition). The machine keeps advancing one real barrier at a time.

---

# UPDATE — banked memory window implemented (far-reads now flow)

Found the far-memory-read routine `0x08fe15`: for a 32-bit pointer whose segment
`>= 0x20`, it writes the **bank register `0x7810`** and reads the **`0x200000-
0x3fffff` banked window** — the memory-mapped path to NAND resource data
(fonts / graphics / animation). Implemented the window in `unsp_core.c`
(`cpu_set_cs_base`, MAME's `cs_bank_space_r` formula → NAND). Result: **4+ million
CS reads** now flow through it; the firmware is actively far-reading flash.

Full current pipeline that works: **boot → wake → load personality (2052 DMA) →
far-read resource data from NAND (banked)**. The machine does a huge amount of a
real Furby's power-on behavior.

**Two remaining unknowns for visible eyes — both deep firmware RE, not tweaks:**
1. **Exact banked-NAND mapping (`cs_base`).** The window works mechanically, but
   landing the far reads on the correct NAND bytes needs RE of how the firmware
   computes far pointers vs the NAND's FAT layout. Swept `cs_base` (0, 0xf2900,
   ...) with no display yet — the base isn't a lucky-guess quantity.
2. **Display-enable application logic.** The PPU-enable code (`0x707f`, ~86 sites)
   is never reached; even with correct data there may be a startup-state gate
   before the eyes turn on.

Everything provable has been proven and fixed (memory map, EXTOP, 16-bit shifts,
NAND controller, DMA, banked window). These last two need careful pointer/data
tracing, not another parameter guess.

---

# UPDATE — far-pointer expedition: NAND geometry fixed, overlap mapped

Traced the far reads (`0x08fe15`) live. Findings:
- The far pointers are `r3:r2`; the ones seen use **r3=0x1a → machine `0x1a06ea`**,
  streaming sequentially. That's in `0x020000-0x1fffff`, which is **SDRAM the
  firmware writes to** — mapping it to NAND breaks boot (confirmed). So it should
  read *loaded* data, but the region was empty.
- The **DMA loads NAND→RAM** with `source=0x7854, dest=0x1840, length=0x840`, so
  resource data lands in **low RAM**, not where the far pointer looked — i.e. the
  far pointer itself was computed from still-wrong upstream data.
- The **NAND geometry was wrong**: `type=7` applied a `<<4` OOB shift giving absurd
  effective addresses. Our image is OOB-stripped, so **removed the shift**
  (`eff = page*512`). Big effect: NAND reads **4.3M → 22M**, DMA transfers
  **2052 → 10,506** — the firmware now loads far more of its data correctly.

Net: the machine now boots and loads a large amount of personality/resource data
from flash with correct geometry. The **display still doesn't enable** — the gate
is upstream application logic (the personality/resource data structures and the
startup state machine), which black-box probing isn't cracking. That needs a
dedicated RE pass: follow the loaded data structures and the scheduler's state
transitions to the `0x707f` enable. Everything hardware-level is in place and
working; this is application-data archaeology now.
