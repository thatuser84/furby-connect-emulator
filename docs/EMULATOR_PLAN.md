# Furby Connect — Full SoC Emulator: Architecture & Plan

> **Decision (locked):** Full-fidelity emulator. We emulate the µ'nSP 2.0 CPU +
> GPL16258 peripherals and *execute the real `GameCode.bin` firmware*. Not a
> behaviour puppet — the actual machine.

This document is the map. It records exactly what we're emulating, the prior art
we lean on, the build phases, and the known unknowns. Read it before writing
code.

---

## 0. What we're emulating (the target spec)

### 0.1 CPU — Sunplus µ'nSP 2.0 (ISA 2.0 / "ISA13")

- **Word machine.** 16-bit words are the atom; *no byte addressing*. Every
  offset in every Furby file is word-based for this reason.
- **Address space.** 22-bit bus → **4 M-words**, reached via segment registers.
- **Registers (8 × 16-bit):** `SP, R1, R2, R3, R4, BP, SR, PC`
  (plus R8–R15 as extended GP regs in the 2.0 core).
- **Status register `SR` packing** (MSB→LSB): `[DS:6][N Z S C][CS:6]`
  — the upper 6 bits of the **Data Segment** and **Code Segment** live *inside*
  the flags word. That's the banking mechanism. Plus a **hidden 4-bit shift
  value** (shift/rotate ops) and **IRQ/FIQ enable** bits.
- **Instructions:** 1 or 2 words. When 2, the second word is a 16-bit immediate
  or the low half of an address.
- **Interrupts:** reset `0xFFF7`, FIQ `0xFFF6`, IRQ0–7 from `0xFFF8` upward
  (confirm exact map vs MAME; GameCode.lod hints `0xFFF5`). 3 FIQ + 8 IRQ +
  `BREAK` software int.
- **Stack:** grows **downward**; **post-decrement on push, pre-increment on pop**.
- **Calling convention:** args pushed right-to-left on the stack; 16-bit `int`.

### 0.2 SoC — GeneralPlus GPL16258 (ROM-less, NAND-boot)

| Subsystem | What it does in the Furby | Emulation need |
|---|---|---|
| **Boot** | on-chip boot ROM loads code from external NAND (`BM1=1`) | **HLE the boot loader** (see §4, Risk R1) |
| **SRAM** | 28 K-word internal, `0x0000–0x6FFF`, single-cycle | flat array |
| **External banks** | 5 chip-selects × 256 pages × 64 K-word; NAND here | banked address decode |
| **SPU** | 16-ch PCM/ADPCM → mixer | register model → feed host audio |
| **DAC** | 16-bit stereo, 16×16 FIFO | pull samples to host sink |
| **GPIO (IOA–IOE)** | motor PWM, sensor reads, eye/LCD lines | pin model + host bridge |
| **Timers** | 6× 16-bit, PWM/capture/compare | tick + IRQ generation |
| **ADC (12-bit)** | mic, light sensor, tilt/antenna | inject host values |
| **Interrupt ctrl** | FIQ/IRQ prioritisation | vector dispatch |
| **RTC** | alarm/schedule | optional |
| **NAND ctrl** | reads NAND w/ 1/4/8-bit ECC | model reads (ECC can be stubbed) |

### 0.3 Data formats (already fully understood — see `HARDWARE.md`)

`furby.py` + our NAND analysis give us the complete DLC/personality container,
the AMF/a18 audio container, palettes, cels, and the action-tree → SEQ → APL →
AMF/MTR/LPS response pipeline. **This is done.** The emulator's job is to make
the *firmware* consume these, not to re-derive them.

---

## 1. Prior art we build on (do not reinvent)

| Source | Gives us |
|---|---|
| **MAME `src/devices/cpu/unsp/`** (`UNSP_20`) | a working, tested µ'nSP 2.0 interpreter — the ground-truth opcode semantics |
| **MAME `src/mame/tvgames/generalplus_gpl16250_nand.cpp`** | our exact SoC variant: NAND boot, peripheral memory map, banking (`cs0_r` / Port-D `m_upperbase`) |
| **MooglyGuy/unsp** | standalone µ'nSP ISA docs + raw opcode **bit-encodings** across revisions 1.0–2.0 |
| **This repo's `unSP Programming Tools Manual`** | the **assembly-level ISA**: mnemonics, addressing modes (`Imm6`/`A6`/`A22`/`MR`), register constraints (shift 1–4, push ≤7, bit-ops 0–15, `mul`/`muls`/`div`/`exp` rules), and the CS-via-SR hazard |
| **bluefluff / FurBLE / Furbhax / Hacksby** | BLE transport + a18 codec (needed later for content upload + audio verification) |
| **This repo** | firmware image, full NAND dump, datasheets, **assembler-level ISA reference**, complete data-format spec |

> **Correction to an earlier draft:** I previously wrote that this repo contains
> *no* ISA reference. Wrong — the `unSP Programming Tools Manual` documents the
> assembly-level instruction set (mnemonics, addressing modes, operand
> constraints). What it *lacks* is the raw binary bit-encodings; those we take
> from MAME / MooglyGuy. So the disassembler is cross-checked against **both**:
> semantics from the manual, bit-layout from MAME.

**Strategy:** treat MAME's unsp core + gpl16250_nand driver as the *reference
implementation*. We port/translate semantics from it, validating our output
against it, rather than reverse-engineering opcodes from scratch.

---

## 2. Language / performance decision

- Pure-Python cannot run a 96 MHz core in real time. **But it doesn't need to** —
  Furby firmware is light (mood logic, audio triggers, servo timing). Target
  **functional correctness first**, real-time later.
- **Recommendation:** prototype the whole thing in **Python** (fast to write,
  easy to introspect/debug, PIL/tkinter/`afplay` already available). If the CPU
  loop is too slow once firmware boots, hoist only the hot interpreter into
  **C / Cython**, or bind MAME's `unsp` core via a thin shim. Keep the CPU behind
  a clean interface so the backend is swappable.

---

## 3. Component architecture

```
+------------------------------------------------------------------+
|  host front-end (tkinter): eye display, motor gauge, sensor      |
|  buttons, audio out (afplay), debugger/trace view                |
+------------------------------------------------------------------+
        |  sensor injection            ^  framebuffer / audio / motor
        v                              |
+------------------------------------------------------------------+
|  PERIPHERAL BUS  (memory-mapped register dispatch)               |
|   GPIO | Timers+IRQ | SPU+DAC | ADC | NAND ctrl | RTC            |
+------------------------------------------------------------------+
        |  reads/writes to mapped ranges
        v
+------------------------------------------------------------------+
|  MEMORY (banked): 28KW SRAM @0x0000 | ext banks (NAND-backed)    |
|                    segment/CS decode via SR                      |
+------------------------------------------------------------------+
        |  fetch / load / store
        v
+------------------------------------------------------------------+
|  µ'nSP 2.0 CORE: regs, SR/segments, ALU+flags, shifter,         |
|                   branch/call/ret, push/pop, IRQ/FIQ dispatch    |
+------------------------------------------------------------------+
        ^
        |  drives / validated by
+------------------------------------------------------------------+
|  µ'nSP 2.0 DISASSEMBLER (built first — our understanding tool)   |
+------------------------------------------------------------------+
```

Module layout:
- `unsp/disasm.py` — instruction decoder + pretty-printer
- `unsp/cpu.py` — core execution engine (swappable backend)
- `soc/memory.py` — banked memory + segment decode
- `soc/bus.py` — MMIO dispatch to peripherals
- `soc/periph/{gpio,timer,spu,dac,adc,nand,intc}.py`
- `soc/boot.py` — HLE boot loader (NAND → SRAM → jump reset)
- `host/gui.py`, `host/audio.py`, `host/sensors.py`
- `tests/` — opcode tests validated against MAME semantics

---

## 4. Phased build plan

### Phase 0 — Corpus & tooling (0.5 wk)
- `git lfs pull` the raw NAND (`furby-nand (Fixed OOB Data).bin`, 114 MB) — we
  need the real flash image + OOB, not just the extracted tree.
- Stand up the repo skeleton, test harness, and a hex/trace viewer.
- **Exit:** we can load `GameCode.bin` and the raw NAND into Python cleanly.

### Phase 1 — Disassembler (1–1.5 wk)  ← *the real "understand it" deliverable*
- Implement the µ'nSP 2.0 decoder from MooglyGuy/MAME encodings.
- Disassemble `GameCode.bin`; sanity-check against the vector table + `GameCode.lod`
  memory map. Identify reset entry, IRQ handlers, obvious functions.
- **Exit:** a readable disassembly of the firmware. This is where we *prove* we
  understand the CPU before trusting an interpreter.

### Phase 2 — CPU core (2–3 wk)
- Registers, SR (flags + DS/CS segments), hidden shifter, ALU, loads/stores,
  branches, `CALL`/`RET`, `PUSH`/`POP`, interrupt entry/return.
- Unit-test each opcode class against MAME-derived expected results.
- **Exit:** executes hand-assembled test programs correctly; passes opcode suite.

### Phase 3 — Memory, banking & boot (1–2 wk)
- 28 KW SRAM, banked external decode, segment resolution via SR.
- NAND read model; **HLE the boot loader** (§Risk R1): place firmware where the
  boot ROM would, set reset vector, start executing.
- **Exit:** the real firmware runs past reset and into its main loop without
  faulting.

### Phase 4 — Peripherals, incrementally (3–5 wk)
Bring up in dependency order, each gated by "does firmware stop hanging on it":
0. **Watchdog** (do this *first* — see Risk R7) — service it, or the firmware
   hard-resets in a loop and every later bring-up looks broken.
1. **Timers + TimeBase + interrupt controller** — the firmware heartbeat (32768 Hz
   TimeBase → 1 Hz–1024 Hz IRQs); nothing ticks without it.
2. **GPIO** — motor PWM out, sensor pins in (map to host buttons/gauges).
3. **SPU + DAC** — decode the PCM8/PCM16/ADPCM channels the firmware programs; stream to `afplay`.
4. **ADC** — mic/light/tilt injection.
5. **TFT-LCD controller / framebuffer** — capture the RGB565 eye framebuffer the
   firmware draws (128×128, fed by CEL+`A1R5G5B5` palette) → tkinter canvas.
- HLE any subsystem that's a rabbit hole (ECC, exact SPU mixing) once behaviour
  is faithful.
- **Exit:** eyes animate, sound plays, motor responds — driven by real firmware.

### Phase 5 — Host front-end & persistence (1–2 wk)
- tkinter shell: eye view, motor position, mood/sensor panel, event log, a
  step/trace debugger.
- Wire `FurbyData.dat` / `Checksum.dat` persistence.
- **Exit:** you can pet/feed/tickle the emulated Furby and it behaves like one.

### Phase 6 — Fidelity pass & (optional) BLE (open-ended)
- Compare against documented real-toy behaviour; fix timing/audio/motor.
- Optional: fake BLE peripheral (bluefluff protocol) so the real app can push DLC.

---

## 5. Known unknowns / risks

- **R1 — Boot ROM not dumped.** The GPL16258 is ROM-*less* for user code, but its
  tiny on-chip boot loader (NAND→RAM) is internal and *not in our dump*. Plan:
  **HLE the boot sequence** rather than emulate the boot ROM. MAME's
  `gpl16250_nand` shows how the loader stages code — copy that behaviour.
- **R2 — Exact peripheral register offsets.** Datasheets describe *functions*, not
  a full register map. Source of truth = MAME `gpl16250` + observing what the
  firmware pokes. Expect empirical bring-up.
- **R3 — Vector layout ambiguity.** `0xFFF5` (lod) vs `0xFFF6/7/8` (MooglyGuy).
  Resolve early in Phase 2 against MAME + the firmware's own vector block.
- **R4 — Performance.** Pure-Python may not hit real time. Mitigation in §2
  (swappable backend → C/Cython/MAME bind).
- **R5 — NAND ECC/OOB.** The "Fixed OOB Data" filename implies OOB matters. We
  can likely ignore ECC (assume clean reads) for a functional emulator.
- **R6 — SPU accuracy.** 16-channel ADPCM mixing is intricate. HLE: decode each
  active channel's a18 in Python, mix, play — don't gate-model the SPU.
- **R7 — Watchdog reset loop.** The GPL16258 watchdog (datasheet 5.11) resets the
  whole system if the firmware doesn't clear it within a software-set window.
  Until we identify and service that register, the firmware will reset-loop and
  *every* other subsystem will look broken. **Model/stub the watchdog before
  chasing any other Phase-4 bug** — it's the #1 false-alarm trap.
- **R8 — Display interface variant.** The eyes hang off the TFT-LCD controller
  (datasheet 5.7), but which mode (parallel RGB565 vs serial vs MPU-type) the
  Furby wires up determines how we capture the framebuffer. Confirm by watching
  which LCD registers the firmware programs at init.

---

## 6. Immediate next step

**Build the disassembler (Phase 1).** It's the honest first move: it forces us to
actually understand the ISA, it's independently useful (we can read the
firmware), and it's the validation oracle for the CPU core in Phase 2. Nothing
downstream is trustworthy until we can read what `GameCode.bin` says.

---

*Referenced: MAME (`cpu/unsp`, `tvgames/generalplus_gpl16250_nand.cpp`),
MooglyGuy/unsp, this repo's `GameCode.lod` / datasheets / `furby.py`, and
`HARDWARE.md`.*
